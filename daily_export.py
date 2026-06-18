"""
daily_export.py — v1 daily export pipeline for Dalux FM (BI-ready).

For each entity (work orders, tickets, estates, buildings, assets) it:
  1. EXTRACTS every record (full pull, auto-paginated, with retries).
  2. LANDS an immutable raw snapshot   -> data/raw/<entity>/<date>.jsonl
  3. WRITES a flat dated snapshot       -> data/snapshots/<entity>/<date>.csv
  4. UPDATES a "latest" convenience copy -> data/latest/<entity>.csv
  5. APPENDS to an append-only history  -> data/history/<entity>.csv
                                            (with an `export_date` column —
                                             this is what powers BI trend
                                             reporting). Re-running the same day
                                             REPLACES that day's rows, so it's
                                             safe to retry (idempotent).
  6. LOGS the run                        -> data/run_log.csv

Why CSV for v1: zero extra dependencies, opens everywhere, and every BI tool
(Power BI, Tableau, Excel) reads a folder of CSVs natively. Swapping the write
step to Parquet or a database later is a localized change.

Run it:
    python daily_export.py                 # uses ./data
    python daily_export.py --data-dir D    # custom output root
    python daily_export.py --only tickets  # one entity

Config via environment:
    DALUX_API_KEY    production key (falls back to the embedded sandbox key)
    DALUX_DATA_DIR   output root (default: ./data)

Exit code is non-zero if any entity fails, so a scheduler can detect problems.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, date, timezone

# Safe Unicode on every console (incl. older Windows).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from dalux_fm import DaluxFMClient
import dalux_excel


ENTITIES = ["workorders", "tickets", "estates", "buildings", "assets"]


# --------------------------------------------------------------------------- #
# small fs + value helpers
# --------------------------------------------------------------------------- #
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def csv_value(v):
    """Normalise a cell for CSV: datetimes -> ISO text, None -> empty string."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def write_csv(path, header, rows):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow([csv_value(c) for c in row])


def append_history(path, header, rows, run_date):
    """Append today's rows to the entity's history, keyed by export_date.

    Idempotent: any existing rows for `run_date` are dropped first, so a re-run
    on the same day replaces rather than duplicates. Header is
    ['export_date', *entity columns].
    """
    ensure_dir(os.path.dirname(path))
    hist_header = ["export_date"] + header
    kept = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            r = csv.reader(fh)
            next(r, None)  # skip old header
            for row in r:
                if row and row[0] != run_date:
                    kept.append(row)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(hist_header)
        w.writerows(kept)
        for row in rows:
            w.writerow([run_date] + [csv_value(c) for c in row])


def append_run_log(path, record):
    ensure_dir(os.path.dirname(path))
    fields = ["timestamp", "run_date", "entity", "rows", "status", "duration_sec", "error"]
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(record)


# --------------------------------------------------------------------------- #
# per-entity export
# --------------------------------------------------------------------------- #
def export_entity(fm, entity, run_date, data_dir):
    started = time.time()
    log_path = os.path.join(data_dir, "run_log.csv")

    # 1) extract (also capture raw records for the raw snapshot)
    d = dalux_excel.DATASETS[entity]
    ctx = d["ctx"](fm) if d["ctx"] else {}
    columns = d["columns"]
    header = [h for h, _ in columns]
    raw_records = list(d["records"](fm))

    # 2) raw snapshot (immutable audit trail / replay source)
    raw_path = os.path.join(data_dir, "raw", entity, f"{run_date}.jsonl")
    ensure_dir(os.path.dirname(raw_path))
    with open(raw_path, "w", encoding="utf-8") as fh:
        for rec in raw_records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 3) flatten using the SAME column definitions as the Excel export
    rows = [[extract(rec, ctx) for _, extract in columns] for rec in raw_records]

    # 4) dated snapshot + latest copy
    write_csv(os.path.join(data_dir, "snapshots", entity, f"{run_date}.csv"), header, rows)
    write_csv(os.path.join(data_dir, "latest", f"{entity}.csv"), header, rows)

    # 5) append-only history (the BI trend source)
    append_history(os.path.join(data_dir, "history", f"{entity}.csv"), header, rows, run_date)

    # 6) run log
    duration = round(time.time() - started, 2)
    append_run_log(log_path, {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_date": run_date, "entity": entity, "rows": len(rows),
        "status": "OK", "duration_sec": duration, "error": "",
    })
    print(f"  {entity:<11} {len(rows):>5} rows   ({duration}s)")
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Daily Dalux FM export (BI-ready CSVs).")
    ap.add_argument("--data-dir", default=os.environ.get("DALUX_DATA_DIR", "data"),
                    help="output root (default: ./data or $DALUX_DATA_DIR)")
    ap.add_argument("--only", choices=ENTITIES, help="export a single entity")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="export_date label (default: today, YYYY-MM-DD)")
    args = ap.parse_args()

    data_dir = args.data_dir
    run_date = args.date
    entities = [args.only] if args.only else ENTITIES

    # API key: env override, else the client's embedded sandbox key.
    fm = DaluxFMClient(api_key=os.environ.get("DALUX_API_KEY"))

    print(f"Dalux daily export  | date={run_date}  env={fm.base_url}  out={os.path.abspath(data_dir)}")
    failures = []
    for entity in entities:
        try:
            export_entity(fm, entity, run_date, data_dir)
        except Exception as e:  # keep going; record the failure
            failures.append(entity)
            append_run_log(os.path.join(data_dir, "run_log.csv"), {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run_date": run_date, "entity": entity, "rows": 0,
                "status": "FAIL", "duration_sec": 0, "error": str(e)[:300],
            })
            print(f"  {entity:<11}  FAILED: {e}")

    if failures:
        print(f"\nDone with errors: {', '.join(failures)} failed. See {os.path.join(data_dir,'run_log.csv')}")
        sys.exit(1)
    print(f"\nDone. History updated under {os.path.join(data_dir, 'history')}/")


if __name__ == "__main__":
    main()
