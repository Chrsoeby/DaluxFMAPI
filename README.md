# Dalux FM API — Python client

A small, dependency-light client for the [Dalux FM API](https://app.swaggerhub.com/apis-docs/Dalux/DaluxFM-API/2.3.0).
One file (`dalux_fm.py`), one dependency (`requests`).

## Install

```bash
pip install -r requirements.txt
export DALUX_API_KEY="your-key-here"      # macOS/Linux
```

## Authentication

The client sends your key as the `X-API-Key` header. Base URLs:

- Production: `https://fm-api.dalux.com/api`
- Staging:    `https://fm-stage-api.dalux.com/api` (`DaluxFMClient(key, stage=True)`)

## Pulling data

```python
from dalux_fm import DaluxFMClient
fm = DaluxFMClient("YOUR_KEY")

fm.assets.list()                 # collect all assets (auto-paginated, list)
fm.assets.iter()                 # stream all assets (lazy generator)
fm.assets.list(max_items=500)    # cap the number returned
fm.assets.get("123")             # single asset by id
fm.assets.count()                # total via metadata.totalItems
fm.assets.first()                # first record or None
fm.assets.iter(label="HVAC")     # any kwarg becomes a query-string filter
```

Pagination is **bookmark-based** under the hood: the client reads
`metadata.nextBookmark` from each page and feeds it back as the `bookmark`
parameter until the cursor is exhausted. Page size is capped at 100 by Dalux.

Walk the spatial hierarchy with the nested helpers:

```python
fm.buildings_in_estate(estate_id)
fm.floors_in_building(building_id)
fm.rooms_on_floor(floor_id)
fm.assets_in_building(building_id)
fm.documents_for_asset(asset_id)
fm.workorders_for_asset(asset_id)
```

Download a document's bytes:

```python
fm.download_document(document_id, dest_path="manual.pdf")
```

## Pushing data

```python
estate = fm.estates.create({"name": "North Campus"})
fm.buildings.create({"name": "Bldg A", "estateRef": {"estateId": estate["estateId"]}})
fm.assets.update("123", {"description": "Serviced today"})   # PATCH = partial update
fm.buildings.create_many(rows, stop_on_error=False)          # bulk import
```

Server-managed fields (ids, `lastChangeDate`, `createdBy`, `qrCodes`, ...) are
**ignored on POST/PATCH** — only send fields you actually own.

## Resources available

| Accessor | List | Get | Create | Update |
|---|:-:|:-:|:-:|:-:|
| `fm.estates` | ✓ | ✓ | ✓ | ✓ |
| `fm.lots` | ✓ | ✓ | ✓ | ✓ |
| `fm.buildings` | ✓ | ✓ | ✓ | ✓ |
| `fm.floors` | ✓ | ✓ | ✓ | ✓ |
| `fm.rooms` | ✓ | ✓ | — | ✓ |
| `fm.assets` | ✓ | ✓ | ✓ | ✓ |
| `fm.products` | ✓ | ✓ | ✓ | ✓ |
| `fm.documents` | ✓ | ✓ | — | ✓ |
| `fm.workorders` | ✓ | ✓ | ✓ | ✓ |
| `fm.tickets` | ✓ | ✓ | ✓ | ✓ |
| `fm.invoices` | ✓ | ✓ | ✓ | ✓ |
| `fm.companies` | ✓ | ✓ | — | — |
| `fm.checklists` | ✓ | ✓ | — | — |
| `fm.locations` | ✓ | — | — | — |

Reference/lookup lists (for building valid payloads):
`fm.workorder_statuses()`, `fm.workorder_priorities()`, `fm.workorder_teams()`,
`fm.workorder_templates()`, `fm.ticket_statuses()`, `fm.ticket_topics()`,
`fm.asset_classifications()`, `fm.invoice_statuses()`.

## Error handling

```python
from dalux_fm import DaluxFMAPIError, DaluxFMAuthError, DaluxFMRateLimitError

try:
    fm.assets.create({...})
except DaluxFMAuthError as e:        # 401/403 — bad/inactive/expired key
    print(e.error_code, e.error_message)
except DaluxFMRateLimitError as e:   # 429 after retries exhausted
    ...
except DaluxFMAPIError as e:         # any other 4xx/5xx
    print(e.status_code, e.error_code, e.body)
```

`429` and `5xx` are retried automatically (honoring `Retry-After`, then
exponential backoff). Dalux error codes you may see: `E40101` InvalidApiKey,
`E40102` InactiveApiKey, `E40103` InvalidApiKeyHeader, `E40104` ExpiredApiKey,
`E40301` Forbidden, `E40901` Conflict, `E42901` TooManyRequests.

## Runnable recipes

See `examples.py`:

```bash
python examples.py smoke_test
python examples.py export_assets_csv
python examples.py walk_hierarchy
```

## Note on endpoint versions

Dalux versions endpoints per resource (reads mostly 2.0–2.2, several creates on
3.0). These are configured per-resource in `_build_resources()` and are trivial
to override if Dalux bumps a version — each `Resource` just takes path strings.
If a create/update call 404s, double-check the version against the live spec and
adjust the path there.
