"""
dalux_fm.py — a reusable Python client for the Dalux FM API (v2.x / v3.x).

Docs:
  - https://support.dalux.com/hc/en-us/articles/9544314902556-Dalux-API
  - https://app.swaggerhub.com/apis-docs/Dalux/DaluxFM-API/2.3.0

Design goals
------------
* One dependency: `requests`.
* Auth via the `X-API-Key` header.
* Bookmark-based pagination handled for you (iterate lazily or collect a list).
* Automatic retries on 429 (rate limit) and 5xx, honoring `Retry-After`.
* Rich, structured errors that surface Dalux's `errorCode` / `errorCodeMessage`.
* A generic layer (`get/post/patch/paginate`) *and* a resource layer
  (`client.assets.list()`, `client.assets.create({...})`, ...) so you can work
  at whatever altitude you like — "lots of options for pulling and pushing".

Quick start
-----------
    from dalux_fm import DaluxFMClient

    fm = DaluxFMClient("YOUR_API_KEY")            # production
    # fm = DaluxFMClient("YOUR_API_KEY", stage=True)

    # PULL — lazily stream every asset across all pages
    for asset in fm.assets.iter():
        print(asset["name"])

    # PULL — collect up to 250 buildings matching a filter
    rows = fm.buildings.list(max_items=250)

    # PUSH — create an estate, then a building under it
    estate = fm.estates.create({"name": "North Campus"})
    fm.buildings.create({
        "name": "Building A",
        "estateRef": {"estateId": estate["estateId"]},
    })

A note on endpoint versions
---------------------------
Dalux versions endpoints *per resource*: most reads are 2.0–2.2, while several
create endpoints live under 3.0. Those paths are configured per-resource below
and are easy to override if Dalux bumps a version — see `Resource`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union
from urllib.parse import urljoin

import requests

__all__ = [
    "DaluxFMClient",
    "DaluxFMError",
    "DaluxFMAPIError",
    "DaluxFMAuthError",
    "DaluxFMRateLimitError",
    "Resource",
    "build",
]

log = logging.getLogger("dalux_fm")

JSON = Dict[str, Any]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class DaluxFMError(Exception):
    """Base class for all client errors."""


class DaluxFMAPIError(DaluxFMError):
    """A non-2xx response from the API.

    Attributes
    ----------
    status_code : int
    error_code : str | None        Dalux code, e.g. "E40101".
    error_message : str | None     Dalux message, e.g. "InvalidApiKey".
    method, url : str
    body : Any                     Parsed response body (dict) or raw text.
    """

    def __init__(self, status_code, method, url, body):
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body
        self.error_code = None
        self.error_message = None
        if isinstance(body, dict):
            self.error_code = body.get("errorCode") or body.get("error_code")
            self.error_message = (
                body.get("errorCodeMessage")
                or body.get("message")
                or body.get("error")
            )
        detail = self.error_code or self.error_message or str(body)[:300]
        super().__init__(f"{method} {url} -> HTTP {status_code}: {detail}")


class DaluxFMAuthError(DaluxFMAPIError):
    """401 / 403 — invalid, inactive, expired, or forbidden API key."""


class DaluxFMRateLimitError(DaluxFMAPIError):
    """429 — too many requests (raised only after retries are exhausted)."""


# --------------------------------------------------------------------------- #
# Core client
# --------------------------------------------------------------------------- #
class DaluxFMClient:
    """Thin, retrying HTTP wrapper around the Dalux FM REST API."""

    PROD_BASE_URL = "https://fm-api.dalux.com/api"
    STAGE_BASE_URL = "https://fm-stage-api.dalux.com/api"

    # API key resolution order: explicit `api_key=` arg -> DALUX_API_KEY env var
    # -> DEFAULT_API_KEY below. Keep DEFAULT_API_KEY empty in committed code and
    # provide the key via the DALUX_API_KEY environment variable (locally or as a
    # CI secret) so no credential is ever committed.
    DEFAULT_API_KEY = ""

    # Dalux's documented hard cap on page size.
    MAX_LIMIT = 100

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        stage: bool = False,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_factor: float = 0.5,
        session: Optional[requests.Session] = None,
        user_agent: str = "dalux-fm-python/1.0",
    ):
        api_key = api_key or os.environ.get("DALUX_API_KEY") or self.DEFAULT_API_KEY
        if not api_key:
            raise ValueError(
                "No API key. Pass api_key=... or set the DALUX_API_KEY environment variable."
            )
        self.api_key = api_key
        self.base_url = (base_url or (self.STAGE_BASE_URL if stage else self.PROD_BASE_URL)).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "X-API-Key": api_key,
                "Accept": "application/json",
                "User-Agent": user_agent,
            }
        )

        # Resource accessors — see _build_resources().
        self._build_resources()

    # ------------------------------------------------------------------ #
    # Low-level request plumbing
    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
        raw_response: bool = False,
    ) -> Any:
        """Make a request, retrying transient failures.

        Returns the parsed JSON body (dict/list) by default. With
        `raw_response=True`, returns the `requests.Response` untouched —
        useful for binary downloads.
        """
        url = self._url(path)
        # Drop None-valued params so callers can pass optional filters freely.
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        attempt = 0
        while True:
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=self.timeout,
                stream=stream,
            )

            if resp.status_code < 400:
                if raw_response:
                    return resp
                if resp.status_code == 204 or not resp.content:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.json()
                return resp.content

            # ---- error path ----
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < self.max_retries:
                wait = self._retry_wait(resp, attempt)
                log.warning(
                    "Dalux %s %s -> %s; retry %d/%d in %.1fs",
                    method, url, resp.status_code, attempt + 1, self.max_retries, wait,
                )
                time.sleep(wait)
                attempt += 1
                continue

            raise self._error_for(resp, method, url)

    def _retry_wait(self, resp: requests.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        # Exponential backoff: backoff_factor * 2**attempt.
        return self.backoff_factor * (2 ** attempt)

    @staticmethod
    def _error_for(resp: requests.Response, method: str, url: str) -> DaluxFMAPIError:
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code in (401, 403):
            return DaluxFMAuthError(resp.status_code, method, url, body)
        if resp.status_code == 429:
            return DaluxFMRateLimitError(resp.status_code, method, url, body)
        return DaluxFMAPIError(resp.status_code, method, url, body)

    # ------------------------------------------------------------------ #
    # Generic verbs
    # ------------------------------------------------------------------ #
    def get(self, path: str, *, params: Optional[Dict[str, Any]] = None, unwrap: bool = True) -> Any:
        """GET a single resource. Unwraps the `data` envelope by default."""
        body = self.request("GET", path, params=params)
        if unwrap and isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    @staticmethod
    def _wrap(body: JSON, wrap: bool) -> JSON:
        """Dalux expects write payloads enveloped as {"data": {...}}.

        We wrap automatically unless `wrap=False`, or the caller already
        supplied a single-key {"data": ...} envelope.
        """
        if not wrap:
            return body
        if isinstance(body, dict) and set(body.keys()) == {"data"}:
            return body
        return {"data": body}

    def post(self, path: str, body: JSON, *, params: Optional[Dict[str, Any]] = None,
             unwrap: bool = True, wrap: bool = True) -> Any:
        resp = self.request("POST", path, params=params, json=self._wrap(body, wrap))
        if unwrap and isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    def patch(self, path: str, body: JSON, *, params: Optional[Dict[str, Any]] = None,
              unwrap: bool = True, wrap: bool = True) -> Any:
        resp = self.request("PATCH", path, params=params, json=self._wrap(body, wrap))
        if unwrap and isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    # ------------------------------------------------------------------ #
    # Pagination
    # ------------------------------------------------------------------ #
    def paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        limit: int = MAX_LIMIT,
        item_key: str = "items",
        max_items: Optional[int] = None,
        unwrap_items: bool = True,
    ) -> Iterator[JSON]:
        """Lazily yield every record across all pages of a list endpoint.

        Dalux paginates with a *bookmark* cursor: each list response carries
        `metadata.nextBookmark`, which we feed back as the `bookmark` query
        parameter until it comes back empty.

        Each item in a Dalux list is itself enveloped as
        ``{"data": {...record...}, "links": [...]}``. With ``unwrap_items=True``
        (the default) we yield the inner record; pass ``False`` to get the full
        envelope including HATEOAS ``links``.

        Parameters
        ----------
        path         : the list endpoint, e.g. "2.0/assets".
        params       : extra query parameters (filters) — merged into each request.
        limit        : page size, capped at 100 by the API.
        item_key     : the array key in the response (default "items").
        max_items    : stop after yielding this many records.
        unwrap_items : strip each item's `data` envelope (default True).
        """
        limit = min(limit, self.MAX_LIMIT)
        q: Dict[str, Any] = dict(params or {})
        q["limit"] = limit
        yielded = 0

        while True:
            body = self.request("GET", path, params=q)
            if not isinstance(body, dict):
                return

            items = body.get(item_key)
            if items is None:
                # Tolerate alternative envelopes.
                items = body.get("data") or []
            for item in items:
                if unwrap_items and isinstance(item, dict) and isinstance(item.get("data"), dict):
                    item = item["data"]
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return

            meta = body.get("metadata") or {}
            next_bookmark = meta.get("nextBookmark")
            if not next_bookmark:
                return
            q["bookmark"] = next_bookmark

    def collect(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        limit: int = MAX_LIMIT,
        item_key: str = "items",
        max_items: Optional[int] = None,
    ) -> List[JSON]:
        """Eagerly collect a list endpoint into a Python list."""
        return list(
            self.paginate(
                path, params=params, limit=limit, item_key=item_key, max_items=max_items
            )
        )

    def count(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Return `metadata.totalItems` for a list endpoint (one request)."""
        q = dict(params or {})
        q["limit"] = 1
        body = self.request("GET", path, params=q)
        if isinstance(body, dict):
            return (body.get("metadata") or {}).get("totalItems")
        return None

    # ------------------------------------------------------------------ #
    # Resource registry
    # ------------------------------------------------------------------ #
    def _build_resources(self) -> None:
        # Read-only reference data
        self.locations = Resource(self, list_path="2.0/locations")
        self.companies = Resource(
            self, list_path="2.0/companies", item_path="2.0/companies/{id}"
        )
        self.checklists = Resource(
            self, list_path="2.0/checklists", item_path="2.0/checklists/{id}"
        )

        # Spatial hierarchy
        self.estates = Resource(
            self,
            list_path="2.0/estates",
            item_path="2.0/estates/{id}",
            create_path="3.0/estates",
            update_path="2.0/estates/{id}",
        )
        self.lots = Resource(
            self,
            list_path="2.1/lots",
            item_path="2.1/lots/{id}",
            create_path="3.0/lots",
            update_path="2.1/lots/{id}",
        )
        self.buildings = Resource(
            self,
            list_path="2.0/buildings",
            item_path="2.0/buildings/{id}",
            create_path="3.0/buildings",
            update_path="2.0/buildings/{id}",
        )
        self.floors = Resource(
            self,
            list_path="2.0/floors",
            item_path="2.0/floors/{id}",
            create_path="2.0/floors",
            update_path="2.0/floors/{id}",
        )
        self.rooms = Resource(
            self,
            list_path="2.0/rooms",
            item_path="2.0/rooms/{id}",
            update_path="3.0/rooms/{id}",
        )

        # Assets & products
        self.assets = Resource(
            self,
            list_path="2.0/assets",
            item_path="2.0/assets/{id}",
            create_path="3.0/assets",
            update_path="2.0/assets/{id}",
        )
        self.products = Resource(
            self,
            list_path="2.0/products",
            item_path="2.0/products/{id}",
            create_path="3.0/products",
            update_path="2.0/products/{id}",
        )

        # Maintenance & operations
        self.workorders = Resource(
            self,
            list_path="2.2/workorders",
            item_path="2.2/workorders/{id}",
            create_path="2.2/workorders",
            update_path="2.2/workorders/{id}",
        )
        self.tickets = Resource(
            self,
            list_path="2.0/tickets",
            item_path="2.0/tickets/{id}",
            create_path="2.0/tickets",
            update_path="2.0/tickets/{id}",
        )
        self.invoices = Resource(
            self,
            list_path="2.0/invoices",
            item_path="2.0/invoices/{id}",
            create_path="2.0/invoices",
            update_path="2.0/invoices/{id}",
        )
        self.documents = Resource(
            self,
            list_path="2.0/documents",
            item_path="2.0/documents/{id}",
            update_path="2.0/documents/{id}",
        )

    # ------------------------------------------------------------------ #
    # Convenience: lookup / reference endpoints used when building payloads
    # ------------------------------------------------------------------ #
    def workorder_statuses(self) -> List[JSON]:
        return self.collect("2.0/workorders/statuses")

    def workorder_priorities(self) -> List[JSON]:
        return self.collect("2.0/workorders/priorities")

    def workorder_teams(self) -> List[JSON]:
        return self.collect("2.0/workorders/teams")

    def workorder_templates(self) -> List[JSON]:
        return self.collect("2.0/workorders/templates")

    def ticket_statuses(self) -> List[JSON]:
        return self.collect("2.0/tickets/statuses")

    def ticket_topics(self) -> List[JSON]:
        return self.collect("2.0/tickets/topics")

    def asset_classifications(self) -> List[JSON]:
        return self.collect("2.0/assets/classifications")

    def invoice_statuses(self) -> List[JSON]:
        return self.collect("2.0/invoices/statuses")

    # ------------------------------------------------------------------ #
    # Convenience: hierarchy walkers (server-side nested list endpoints)
    # ------------------------------------------------------------------ #
    def buildings_in_estate(self, estate_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/estates/{estate_id}/buildings", params=params)

    def floors_in_building(self, building_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/buildings/{building_id}/floors", params=params)

    def assets_in_building(self, building_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/buildings/{building_id}/assets", params=params)

    def rooms_on_floor(self, floor_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/floors/{floor_id}/rooms", params=params)

    def documents_for_asset(self, asset_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/assets/{asset_id}/documents", params=params)

    def workorders_for_asset(self, asset_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.2/assets/{asset_id}/workorders", params=params)

    def workorder_history(self, work_order_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/workorders/{work_order_id}/history", params=params)

    # ------------------------------------------------------------------ #
    # Convenience: document download
    # ------------------------------------------------------------------ #
    def download_document(self, document_id: Union[str, int], dest_path: Optional[str] = None) -> bytes:
        """Download a document's binary content.

        Returns the bytes, and also writes them to `dest_path` if given.
        """
        resp = self.request(
            "GET",
            f"2.0/documents/{document_id}/binaryFileDownload",
            raw_response=True,
            stream=True,
        )
        content = resp.content
        if dest_path:
            with open(dest_path, "wb") as fh:
                fh.write(content)
        return content

    def document_filelink(self, document_id: Union[str, int]) -> JSON:
        """Get a (temporary) direct download link for a document."""
        return self.get(f"2.0/documents/{document_id}/filelink")

    # ------------------------------------------------------------------ #
    # Convenience: one-call creates using the payload builders
    # ------------------------------------------------------------------ #
    def create_estate(self, name: str, **kwargs) -> JSON:
        """Create an estate. See `build.estate` for accepted kwargs."""
        return self.estates.create(build.estate(name, **kwargs))

    def create_building(self, name: str, estate_id: Union[str, int], **kwargs) -> JSON:
        """Create a building under an estate. See `build.building` for kwargs.

        `address` may be a dict from `build.address(...)`.
        """
        return self.buildings.create(build.building(name, estate_id=estate_id, **kwargs))

    def create_asset(self, name: str, classification_id: Union[str, int], **kwargs) -> JSON:
        """Create an asset. Pass `building_ids=[...]` (and/or floor/room ids) to
        place it; see `build.asset` for the full set of kwargs."""
        return self.assets.create(build.asset(name, classification_id=classification_id, **kwargs))

    def create_ticket(self, topic_id, reporter_email: str, description: str, **kwargs) -> JSON:
        """Create a ticket. Required: a placement — pass `building_ids=[...]`
        (and/or floor/room ids). See `build.ticket` for kwargs."""
        return self.tickets.create(
            build.ticket(topic_id=topic_id, reporter_email=reporter_email,
                         description=description, **kwargs)
        )

    def create_workorder(self, template_id, team_id, start_date, deadline_date, **kwargs) -> JSON:
        """Create a work order. Required: template_id, team_id, start_date,
        deadline_date (ISO strings). See `build.workorder` for optional kwargs
        like name, description, priority_id, building_ids."""
        return self.workorders.create(
            build.workorder(template_id=template_id, team_id=team_id,
                            start_date=start_date, deadline_date=deadline_date, **kwargs)
        )

    # ------------------------------------------------------------------ #
    # File upload (shared by tickets, work orders, invoices, documents)
    # ------------------------------------------------------------------ #
    def start_upload_session(self) -> str:
        """Create a file-upload session and return its `sessionId` (a.k.a.
        uploadId). One session holds exactly one file."""
        resp = self.request("GET", "2.0/fileUploadSession")
        inner = resp.get("data", resp) if isinstance(resp, dict) else {}
        session_id = inner.get("sessionId") if isinstance(inner, dict) else None
        if not session_id:
            raise DaluxFMError(f"no sessionId in fileUploadSession response: {resp!r}")
        return session_id

    def upload_file(self, file: Union[str, bytes, bytearray], *, filename: Optional[str] = None) -> str:
        """Upload a file and return its `uploadId` (session id).

        `file` may be a path (str) or raw bytes. The returned id is what you
        reference when attaching the file to a ticket, work order, etc.

        Note: this is the single-shot path (`/3.0/fileUpload`). Very large files
        can instead be chunked via `/3.0/filePartUpload` with an `X-Upload-Range`
        header — not implemented here.
        """
        if isinstance(file, (bytes, bytearray)):
            content = bytes(file)
            if not filename:
                raise ValueError("filename is required when uploading raw bytes")
        else:
            import os as _os
            with open(file, "rb") as fh:
                content = fh.read()
            filename = filename or _os.path.basename(str(file))

        session_id = self.start_upload_session()
        self.request(
            "POST", f"3.0/fileUpload/{session_id}",
            data=content,
            headers={"Content-Type": "application/octet-stream", "X-File-Name": filename},
        )
        return session_id

    # ------------------------------------------------------------------ #
    # Ticket messages & photos
    # ------------------------------------------------------------------ #
    def add_ticket_message(self, ticket_id: Union[str, int], message: str,
                           *, upload_ids: Optional[List[str]] = None) -> JSON:
        """Post a message (optionally with already-uploaded photos) to a ticket's
        conversation thread. `message` must be non-empty (API requirement)."""
        body = {
            "message": message,
            "photos": [{"uploadId": u} for u in (upload_ids or [])],
        }
        return self.post(f"2.0/tickets/{ticket_id}/sendMessagePhoto", body)

    def add_ticket_photo(self, ticket_id: Union[str, int],
                         image: Union[str, bytes, bytearray],
                         *, message: str = "Photo attached via API",
                         filename: Optional[str] = None) -> JSON:
        """Attach an image to a ticket (uploads then posts it as a message).

        `image` is a file path or raw bytes. Because Dalux only exposes the
        message-photo path for tickets, the image appears in the ticket's
        message thread (not the `GET /photos` collection). A non-empty `message`
        is required by the API.
        """
        upload_id = self.upload_file(image, filename=filename)
        return self.add_ticket_message(ticket_id, message, upload_ids=[upload_id])

    def ticket_history(self, ticket_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/tickets/{ticket_id}/history", params=params)

    def ticket_photos(self, ticket_id: Union[str, int], **params) -> Iterator[JSON]:
        return self.paginate(f"2.0/tickets/{ticket_id}/photos", params=params)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DaluxFMClient base_url={self.base_url!r}>"


# --------------------------------------------------------------------------- #
# Resource — generic CRUD over one endpoint family
# --------------------------------------------------------------------------- #
class Resource:
    """A list/get/create/update facade over a single Dalux resource family.

    Capabilities are inferred from which paths are supplied: a resource with no
    `create_path` raises if you call `.create()`, etc. This keeps read-only
    resources (locations, companies, ...) honest.
    """

    def __init__(
        self,
        client: DaluxFMClient,
        *,
        list_path: str,
        item_path: Optional[str] = None,
        create_path: Optional[str] = None,
        update_path: Optional[str] = None,
        item_key: str = "items",
    ):
        self._client = client
        self._list_path = list_path
        self._item_path = item_path
        self._create_path = create_path
        self._update_path = update_path
        self._item_key = item_key

    # ---- PULL ---------------------------------------------------------- #
    def iter(self, *, limit: int = DaluxFMClient.MAX_LIMIT, max_items: Optional[int] = None, **filters) -> Iterator[JSON]:
        """Lazily stream every record (auto-paginated). Pass filters as kwargs."""
        return self._client.paginate(
            self._list_path,
            params=filters or None,
            limit=limit,
            item_key=self._item_key,
            max_items=max_items,
        )

    def list(self, *, limit: int = DaluxFMClient.MAX_LIMIT, max_items: Optional[int] = None, **filters) -> List[JSON]:
        """Collect records into a list (auto-paginated). Pass filters as kwargs."""
        return list(self.iter(limit=limit, max_items=max_items, **filters))

    def get(self, item_id: Union[str, int], *, unwrap: bool = True) -> JSON:
        """Fetch a single record by id."""
        if not self._item_path:
            raise DaluxFMError(f"{self._list_path} has no single-item endpoint")
        return self._client.get(self._item_path.format(id=item_id), unwrap=unwrap)

    def count(self, **filters) -> Optional[int]:
        """Total number of matching records (via metadata.totalItems)."""
        return self._client.count(self._list_path, params=filters or None)

    def first(self, **filters) -> Optional[JSON]:
        """Return the first matching record, or None."""
        for item in self.iter(max_items=1, **filters):
            return item
        return None

    # ---- PUSH ---------------------------------------------------------- #
    def create(self, body: JSON, *, unwrap: bool = True) -> JSON:
        """Create a new record (POST). Returns the created object."""
        if not self._create_path:
            raise DaluxFMError(f"{self._list_path} is read-only (no create endpoint)")
        return self._client.post(self._create_path, body, unwrap=unwrap)

    def update(self, item_id: Union[str, int], body: JSON, *, unwrap: bool = True) -> JSON:
        """Partially update a record (PATCH). Send only fields you want changed."""
        if not self._update_path:
            raise DaluxFMError(f"{self._list_path} is read-only (no update endpoint)")
        return self._client.patch(self._update_path.format(id=item_id), body, unwrap=unwrap)

    def create_many(self, bodies: Iterable[JSON], *, stop_on_error: bool = True) -> List[JSON]:
        """Bulk-create. Returns created objects; on error, raises (or collects).

        With `stop_on_error=False`, failures are captured as
        `{"_error": <DaluxFMAPIError str>, "_input": <body>}` entries so a
        partial import can keep going.
        """
        results: List[JSON] = []
        for body in bodies:
            try:
                results.append(self.create(body))
            except DaluxFMAPIError as exc:
                if stop_on_error:
                    raise
                results.append({"_error": str(exc), "_input": body})
        return results

    def __repr__(self) -> str:  # pragma: no cover
        caps = "".join(
            c for c, ok in [
                ("L", True),
                ("G", self._item_path),
                ("C", self._create_path),
                ("U", self._update_path),
            ] if ok
        )
        return f"<Resource {self._list_path} [{caps}]>"


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #
def _prune(d: JSON) -> JSON:
    """Drop keys whose value is None or an empty list/dict (Dalux dislikes
    half-populated reference objects)."""
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


class build:
    """Builders that pre-shape Dalux write payloads (the `data` envelope is
    added by the client on POST/PATCH, so these return the *inner* object).

    These encode the quirks discovered against the live API:
      * complex references like `classification` are themselves enveloped in
        `{"data": {...}}`;
      * placement uses *arrays* of refs (`buildingRefs`, `floorRefs`, ...);
      * empty/None fields are pruned.

    Usage::

        fm.assets.create(build.asset("Roof 1", classification_id="7",
                                     building_ids=["1004"]))
        fm.buildings.create(build.building("Hovedhuset", estate_id="2",
                                           address=build.address("Main St", "1")))
    """

    # ---- atomic references ---- #
    @staticmethod
    def estate_ref(estate_id) -> JSON:
        return {"estateId": str(estate_id)}

    @staticmethod
    def building_ref(building_id) -> JSON:
        return {"buildingId": str(building_id)}

    @staticmethod
    def floor_ref(floor_id) -> JSON:
        return {"floorId": str(floor_id)}

    @staticmethod
    def room_ref(room_id) -> JSON:
        return {"roomId": str(room_id)}

    @staticmethod
    def location_ref(location_id) -> JSON:
        return {"locationId": str(location_id)}

    @staticmethod
    def product_ref(product_id) -> JSON:
        return {"productId": str(product_id)}

    @staticmethod
    def classification(classification_id) -> JSON:
        """Note the inner `data` envelope — required by the asset create API."""
        return {"data": {"classificationId": str(classification_id)}}

    @staticmethod
    def address(road=None, number=None, zip_code=None, city=None) -> JSON:
        return _prune({"road": road, "number": number, "zipCode": zip_code, "city": city})

    @staticmethod
    def placement(building_ids=None, floor_ids=None, room_ids=None, coordinates=None) -> JSON:
        """Build a Placement. IDs are passed as lists; `coordinates` is a list
        of (lat, lng) tuples or {"lat":..,"lng":..} dicts."""
        coords = None
        if coordinates:
            coords = [
                c if isinstance(c, dict) else {"lat": c[0], "lng": c[1]}
                for c in coordinates
            ]
        return _prune({
            "buildingRefs": [build.building_ref(b) for b in (building_ids or [])] or None,
            "floorRefs": [build.floor_ref(f) for f in (floor_ids or [])] or None,
            "roomRefs": [build.room_ref(r) for r in (room_ids or [])] or None,
            "coordinates": coords,
        })

    # ---- full payloads ---- #
    @staticmethod
    def estate(name=None, *, description=None, location_id=None, user_defined_fields=None) -> JSON:
        return _prune({
            "name": name,
            "description": description,
            "locationRef": build.location_ref(location_id) if location_id is not None else None,
            "userDefinedFields": user_defined_fields,
        })

    @staticmethod
    def building(
        name=None, *, estate_id=None, address=None, owned=None,
        alternative_name=None, label=None, gross_area=None, net_area=None,
        user_defined_fields=None,
    ) -> JSON:
        return _prune({
            "name": name,
            "alternativeName": alternative_name,
            "estateRef": build.estate_ref(estate_id) if estate_id is not None else None,
            "address": address,
            "owned": owned,
            "label": label,
            "grossArea": gross_area,
            "netArea": net_area,
            "userDefinedFields": user_defined_fields,
        })

    @staticmethod
    def asset(
        name=None, *, classification_id=None,
        building_ids=None, floor_ids=None, room_ids=None, coordinates=None,
        description=None, warranty_start=None, warranty_end=None,
        installation_date=None, bim_instance_refs=None, user_defined_fields=None,
        placement=None,
    ) -> JSON:
        """Build an asset create/update payload.

        `classification_id` is required by the API on create. Either pass
        `building_ids`/`floor_ids`/`room_ids` (a placement is assembled for you)
        or pass a ready-made `placement` dict.
        """
        if placement is None and (building_ids or floor_ids or room_ids or coordinates):
            placement = build.placement(building_ids, floor_ids, room_ids, coordinates)
        return _prune({
            "name": name,
            "description": description,
            "classification": build.classification(classification_id) if classification_id is not None else None,
            "placement": placement,
            "bimInstanceRefs": bim_instance_refs,
            "warrantyStartDate": warranty_start,
            "warrantyEndDate": warranty_end,
            "installationDate": installation_date,
            "userDefinedFields": user_defined_fields,
        })

    @staticmethod
    def ticket(
        *, topic_id, reporter_email, description,
        building_ids=None, floor_ids=None, room_ids=None, coordinates=None,
        placement=None, **extra,
    ) -> JSON:
        """Build a ticket create payload.

        Required by the API: ``topic_id``, ``reporter_email``, ``description``,
        and a ``placement`` (supply ``building_ids`` and/or floor/room ids, or a
        ready-made ``placement`` dict). Status defaults to New (1) and a team is
        auto-assigned server-side. Any other fields can be passed via ``extra``.
        """
        if placement is None and (building_ids or floor_ids or room_ids or coordinates):
            placement = build.placement(building_ids, floor_ids, room_ids, coordinates)
        body = {
            "topic": {"topicId": str(topic_id)},
            "placement": placement,
            "reporterEmail": reporter_email,
            "description": description,
        }
        body.update(extra)
        return _prune(body)

    @staticmethod
    def workorder(
        *, template_id, team_id, start_date, deadline_date,
        name=None, description=None, priority_id=None, responsible_email=None,
        work_type=None, building_ids=None, estate_ids=None, location_ids=None,
        placement=None, **extra,
    ) -> JSON:
        """Build a work order create payload.

        Required by the API: ``template_id``, ``team_id``, ``start_date``,
        ``deadline_date`` (the template mandates the two dates). Dates are ISO-8601
        strings, e.g. ``"2026-07-01T12:00:00Z"``.

        Optional but usually wanted: ``name``, ``description``, ``priority_id``,
        ``responsible_email``, and a placement (pass ``building_ids`` /
        ``estate_ids`` / ``location_ids``, or a ready-made ``placement`` dict).
        Anything else can go through ``extra``.
        """
        if placement is None and (building_ids or estate_ids or location_ids):
            placement = _prune({
                "buildingRefs": [build.building_ref(b) for b in (building_ids or [])] or None,
                "estateRefs": [build.estate_ref(e) for e in (estate_ids or [])] or None,
                "locationRefs": [build.location_ref(l) for l in (location_ids or [])] or None,
            })
        body = {
            "templateRef": {"templateId": str(template_id)},
            "teamRef": {"teamId": str(team_id)},
            "startDate": start_date,
            "deadlineDate": deadline_date,
            "name": name,
            "description": description,
            "priorityRef": {"priorityId": str(priority_id)} if priority_id is not None else None,
            "responsibleUserEmail": responsible_email,
            "type": work_type,
            "placement": placement,
        }
        body.update(extra)
        return _prune(body)
