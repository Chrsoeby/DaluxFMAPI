# Free dummy deployment — GitHub Actions + Power BI

A zero-cost proof-of-concept: a daily cloud job pulls the Dalux **sandbox** data
and commits CSVs to a GitHub repo; Power BI refreshes from those files.

```
GitHub Actions (daily 05:00 UTC) → daily_export.py → commits data/*.csv
        │ raw.githubusercontent.com URLs
        ▼
Power BI Desktop (Web connector) → model → publish → Power BI Service
        ▼
Power BI scheduled refresh (free, My Workspace) → pulls new CSVs daily
```

---

## Part A — the free cloud scheduler (GitHub Actions)

### 1. Put the project in a GitHub repo
```bash
cd "/Users/christiansobyborgqvist/Desktop/Kodning/Dalux API"
git init
git add .
git commit -m "Dalux FM tooling"
# create an EMPTY repo on github.com first, then:
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```

### 2. ⚠️ Decide public vs private (key safety)
`dalux_fm.py` contains the embedded **sandbox** key. So:
- **Private repo (recommended):** the key stays out of public view. Power BI
  refresh from a private repo needs a token (see B‑4 note).
- **Public repo:** simplest for Power BI (anonymous refresh), **but remove the
  embedded key first** — delete the `DEFAULT_API_KEY` value in `dalux_fm.py` and
  rely only on the GitHub secret below. (Sandbox key only; it also expires.)

### 3. Add the key as a repo secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `DALUX_API_KEY`
- Value: your sandbox JWT

### 4. The workflow is already included
`.github/workflows/daily-export.yml` runs daily and commits `data/`. Trigger a
first run now: repo → **Actions → Daily Dalux export → Run workflow**.
After it finishes you'll see `data/history/*.csv` and `data/latest/*.csv` updated
in the repo.

---

## Part B — Power BI (on Windows / VM)

### 1. Get the raw CSV URLs
On GitHub, open e.g. `data/history/workorders.csv` → **Raw** button → copy the URL:
```
https://raw.githubusercontent.com/<you>/<repo>/main/data/history/workorders.csv
```

### 2. Connect in Power BI Desktop
- **Home → Get data → Web** → paste the raw URL → **OK**.
- Repeat for each file you want (the `history/` files give you trend data via the
  `export_date` column; `latest/` gives current state).
- Power BI auto-detects CSV columns; set data types (dates, numbers).

### 3. Build the model & report
- Treat `history/workorders.csv` and `history/tickets.csv` as **fact** tables.
- Load `latest/buildings.csv`, plus the lookup values, as **dimension** tables and
  relate them on IDs.
- Build visuals: open vs closed work orders, overdue, **backlog trend over
  `export_date`**, MTTR, tickets by topic, cost by estate.

### 4. Publish & schedule the refresh (free)
- **Home → Publish** → choose **My workspace**.
- In Power BI Service: dataset → **Settings → Scheduled refresh** → set a time
  **after 05:00 UTC** (e.g. 06:00). Turn on failure email notifications.
- **Data source credentials:** for a **public** repo the Web source is
  *Anonymous* — refresh just works. For a **private** repo, use the Web
  connector with an `Authorization: token <GitHub PAT>` header (or a tokenised
  URL) so the Service can read the file.

> Note: a **free** Power BI license supports scheduled refresh in *My Workspace*.
> Sharing dashboards with other people requires Power BI **Pro** — fine to add
> later; not needed for the dummy.

---

## The daily loop, once set up
1. **05:00 UTC** — GitHub Actions runs `daily_export.py`, commits fresh CSVs.
2. **06:00 UTC** — Power BI Service refresh pulls the new CSVs.
3. Dashboards are current before the workday starts. Cost: **$0**.

## Moving from dummy → production later
Swap, one layer at a time: GitHub Actions → Azure Container job; CSV-in-repo →
Azure SQL; free Power BI → Power BI Pro with shared workspace + RLS. The pipeline
code stays the same — only the *load target* and *host* change.
