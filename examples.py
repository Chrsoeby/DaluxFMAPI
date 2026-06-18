"""
examples.py — runnable recipes for the Dalux FM client.

Set your key first:
    export DALUX_API_KEY="xxxxxxxx"      # macOS/Linux
    setx DALUX_API_KEY "xxxxxxxx"        # Windows (new shell after)

Then run a single recipe:
    python examples.py smoke_test
    python examples.py export_assets_csv
"""

import csv
import os
import sys

from dalux_fm import DaluxFMClient, DaluxFMAPIError


def client() -> DaluxFMClient:
    key = os.environ.get("DALUX_API_KEY")
    if not key:
        sys.exit("Set the DALUX_API_KEY environment variable first.")
    # Flip stage=True while testing so you never touch production data.
    return DaluxFMClient(key, stage=False)


# --------------------------------------------------------------------------- #
# 0. Smoke test — confirms the key works and prints a few counts.
# --------------------------------------------------------------------------- #
def smoke_test():
    fm = client()
    try:
        print("estates  :", fm.estates.count())
        print("buildings:", fm.buildings.count())
        print("assets   :", fm.assets.count())
        print("OK — API key is valid.")
    except DaluxFMAPIError as exc:
        print(f"API error: HTTP {exc.status_code} {exc.error_code} {exc.error_message}")
        raise


# --------------------------------------------------------------------------- #
# PULL recipes
# --------------------------------------------------------------------------- #
def list_estates():
    fm = client()
    for e in fm.estates.iter():
        print(e.get("estateId"), "-", e.get("name"))


def export_assets_csv(path: str = "assets.csv"):
    """Stream every asset to a CSV without holding them all in memory."""
    fm = client()
    fields = ["assetId", "name", "description", "label", "classification", "placement"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        n = 0
        for asset in fm.assets.iter():
            writer.writerow(asset)
            n += 1
    print(f"Wrote {n} assets to {path}")


def walk_hierarchy():
    """Estate -> buildings -> floors -> rooms, using nested endpoints."""
    fm = client()
    for estate in fm.estates.iter(max_items=3):
        print(f"Estate: {estate.get('name')}")
        for b in fm.buildings_in_estate(estate["estateId"]):
            print(f"  Building: {b.get('name')}")
            for f in fm.floors_in_building(b["buildingId"]):
                rooms = list(fm.rooms_on_floor(f["floorId"]))
                print(f"    Floor {f.get('name')}: {len(rooms)} rooms")


def open_workorders():
    """Filter example — pass query params as keyword args to .iter()/.list()."""
    fm = client()
    # Available filters depend on the endpoint; inspect statuses first:
    print("Statuses:", [(s.get("id"), s.get("name")) for s in fm.workorder_statuses()])
    for wo in fm.workorders.iter(max_items=20):
        print(wo.get("workOrderId"), wo.get("title"))


# --------------------------------------------------------------------------- #
# PUSH recipes
# --------------------------------------------------------------------------- #
def create_estate_and_building():
    fm = client()
    estate = fm.estates.create({"name": "North Campus", "description": "Created via API"})
    print("Created estate:", estate.get("estateId"))

    building = fm.buildings.create({
        "name": "Building A",
        "alternativeName": "A",
        "estateRef": {"estateId": estate["estateId"]},
        "owned": True,
    })
    print("Created building:", building.get("buildingId"))


def update_asset(asset_id: str):
    """PATCH is a partial update — send only the fields you want to change."""
    fm = client()
    updated = fm.assets.update(asset_id, {"description": "Serviced 2026-06-13"})
    print("Updated:", updated.get("assetId"))


def bulk_import_buildings_from_csv(path: str, estate_id: str):
    """Read a CSV of buildings and create them, continuing past errors."""
    fm = client()
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [
            {"name": r["name"], "estateRef": {"estateId": estate_id}}
            for r in csv.DictReader(fh)
        ]
    results = fm.buildings.create_many(rows, stop_on_error=False)
    ok = sum(1 for r in results if "_error" not in r)
    print(f"Created {ok}/{len(results)} buildings.")
    for r in results:
        if "_error" in r:
            print("  FAILED:", r["_input"].get("name"), "->", r["_error"])


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if len(sys.argv) < 2:
        funcs = [k for k, v in sorted(globals().items()) if callable(v) and not k.startswith("_") and v.__module__ == "__main__"]
        print("Usage: python examples.py <recipe> [args...]\n\nRecipes:")
        for f in funcs:
            if f != "client":
                print("  ", f)
        sys.exit(0)
    name, *args = sys.argv[1:]
    fn = globals().get(name)
    if not callable(fn):
        sys.exit(f"Unknown recipe: {name}")
    fn(*args)
