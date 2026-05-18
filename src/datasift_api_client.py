"""RAWPIPE — direct HTTP client for DataSift's internal API.

Replaces the Playwright wizard at src/datasift_uploader.py for uploads.
Same input (a CSV path + list name + tags); roughly 100x faster and far
less fragile because there is no UI in the loop.

Discovered via reverse-engineering on 2026-05-17. Endpoints used:

  POST /api/token/                         — login (email/password → JWT pair)
  POST /api/token/refresh/                 — exchange refresh JWT for new pair
  GET  /api/internal/upload/presigned-upload-url/   — get an S3 presigned POST
  POST <s3>                                — upload the CSV bytes (multipart)
  POST /api/internal/upload/suggested-mapping/      — auto-map CSV → schema
  POST /api/internal/upload/               — commit (returns 204)

Optional but supported by the commit (per OPTIONS schema):
  data_enriching: {enrich_property, enrich_owner, replace_owner}
  purchase_date, list_source, filter_id, replace_owners, updated_fields

What this client deliberately does NOT do (yet):
  - Skip trace. The /skip-trace/ endpoint did not surface in the captured
    traffic. Falls back to the Playwright path in datasift_uploader.py
    until a follow-up capture reveals it.
  - The 7 wizard-style "Stay Organized" prompts (list_source, purchase
    date, etc.). They're all optional on the commit; we send sensible
    defaults but expose them as kwargs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

API_BASE = "https://apiv2.reisift.io"

# This UI-version string is sent on every request. Missing it returns 403.
# It can go stale if DataSift bumps it server-side; if every call starts
# returning 403, capture one fresh value from a logged-in browser and
# bump this constant.
UI_VERSION = "2022.02.01.7"

# Where we persist refresh tokens between runs. Lets cron / Apify
# Actor avoid hitting the login endpoint with credentials every time.
TOKEN_CACHE_FILE = Path("datasift_api_tokens.json")

# Valid upload_type enum values (from OPTIONS /api/internal/upload/).
UPLOAD_TYPES = frozenset({
    "new_properties",
    "new_owners",
    "add_owners_to_properties",
    "add_properties_to_owners",
    "add_tags_to_phones",
    "update_by_mailing_address",
    "replace_by_mailing_address",
    "update_by_property_address",
    "replace_by_property_address",
    "sift_map",
    "dataflik",
})


# ── Errors ────────────────────────────────────────────────────────────

class DataSiftAPIError(Exception):
    """An API call returned a non-success status."""

    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"DataSift API {status} on {url}: {body[:300]}")


# ── Client ────────────────────────────────────────────────────────────

class DataSiftAPIClient:
    """Direct API client. One instance == one authenticated user session."""

    def __init__(self, access_token: str, refresh_token: str | None = None):
        self._access = access_token
        self._refresh = refresh_token
        self._session = requests.Session()
        self._apply_auth_headers()

    # ─── Construction ────────────────────────────────────────────

    @classmethod
    def login(cls, email: str, password: str) -> "DataSiftAPIClient":
        """Exchange email/password for an access+refresh JWT pair."""
        resp = requests.post(
            f"{API_BASE}/api/token/",
            json={
                "email": email,
                "password": password,
                "remember": True,
                "agree": True,
            },
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise DataSiftAPIError(resp.status_code, resp.text, f"{API_BASE}/api/token/")
        data = resp.json()
        client = cls(data["access"], data.get("refresh"))
        client._save_tokens()
        logger.info("DataSift API login successful")
        return client

    @classmethod
    def from_env(cls) -> "DataSiftAPIClient":
        """Build a client without prompting for credentials.

        Strategy:
          1. If a cached refresh token exists, try refreshing it.
          2. Else fall back to DATASIFT_EMAIL / DATASIFT_PASSWORD.
        """
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        cached = cls._load_tokens()
        if cached and cached.get("refresh"):
            try:
                temp = cls(cached.get("access", ""), cached["refresh"])
                temp._do_refresh()
                logger.info("DataSift API: restored session from cached refresh token")
                return temp
            except DataSiftAPIError as e:
                logger.info("Cached refresh token rejected (%s), falling back to login", e.status)

        import os
        email = os.getenv("DATASIFT_EMAIL")
        password = os.getenv("DATASIFT_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "No cached refresh token, and DATASIFT_EMAIL / DATASIFT_PASSWORD "
                "are not set. Cannot authenticate."
            )
        return cls.login(email, password)

    # ─── Auth ────────────────────────────────────────────────────

    def _apply_auth_headers(self) -> None:
        self._session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self._access}",
            "X-REISIFT-UI-VERSION": UI_VERSION,
        })

    def _do_refresh(self) -> None:
        """Exchange the refresh token for a new access+refresh pair."""
        if not self._refresh:
            raise DataSiftAPIError(401, "No refresh token available", "")
        resp = requests.post(
            f"{API_BASE}/api/token/refresh/",
            json={"refresh": self._refresh},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise DataSiftAPIError(
                resp.status_code, resp.text, f"{API_BASE}/api/token/refresh/"
            )
        data = resp.json()
        self._access = data["access"]
        # Refresh tokens ROTATE — new refresh is returned each time.
        if "refresh" in data:
            self._refresh = data["refresh"]
        self._apply_auth_headers()
        self._save_tokens()

    def _save_tokens(self) -> None:
        try:
            TOKEN_CACHE_FILE.write_text(
                json.dumps({
                    "access": self._access,
                    "refresh": self._refresh,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }),
                encoding="utf-8",
            )
        except OSError as e:
            logger.debug("Failed to persist tokens: %s", e)

    @staticmethod
    def _load_tokens() -> dict | None:
        if not TOKEN_CACHE_FILE.exists():
            return None
        try:
            return json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ─── Core request wrapper with 401 retry ─────────────────────

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        url = f"{API_BASE}{path}" if path.startswith("/") else path
        resp = self._session.request(method, url, timeout=30, **kw)
        if resp.status_code == 401 and self._refresh:
            logger.debug("401 on %s — refreshing and retrying once", path)
            self._do_refresh()
            resp = self._session.request(method, url, timeout=30, **kw)
        return resp

    def _request_json(self, method: str, path: str, **kw) -> dict | list:
        resp = self._request(method, path, **kw)
        if resp.status_code >= 400:
            raise DataSiftAPIError(resp.status_code, resp.text, path)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ─── High-level endpoints ────────────────────────────────────

    def get_user(self) -> dict:
        return self._request_json("GET", "/api/internal/user/")

    def list_lists(self, limit: int = 100) -> list[dict]:
        data = self._request_json("GET", f"/api/internal/list/?limit={limit}")
        return data.get("results", [])

    def list_tags(self, limit: int = 100) -> list[dict]:
        data = self._request_json("GET", f"/api/internal/tag/?limit={limit}")
        return data.get("results", [])

    def get_upload_usage(self, storage_key: str | None = None) -> dict:
        """Return {upload_usage, upload_limit, line_count?}."""
        body = {"storage_key": storage_key} if storage_key else {}
        return self._request_json("POST", "/api/internal/upload/usage/", json=body)

    def get_subscription(self) -> dict:
        """Return subscription info: {name, price, interval, valid_until, addons}."""
        return self._request_json("GET", "/api/internal/account/subscription/")

    # ─── Skip-trace ──────────────────────────────────────────────
    #
    # Endpoint discovered from the SPA's bundled main.min.js (DataSift
    # API client class). Body shape is built by `generateSkiptracePayload`
    # in the SPA — it produces a filter query in Elasticsearch-style
    # `query.must` shape. `property_type: "clean"` is forced unless
    # `allow_dirty=True` (which lets you re-skip-trace already-traced
    # records).
    #
    # Crucially: passing `estimate=True` returns the cost preview WITHOUT
    # consuming balance. The Playwright wizard always submits the real
    # call after the user clicks "I Agree with the terms".

    def skip_trace_list(
        self,
        list_uuid: str,
        *,
        tags: list[str] | None = None,
        estimate: bool = True,
        allow_dirty: bool = False,
    ) -> dict:
        """Skip-trace all clean records in a single list.

        Args:
            list_uuid: UUID of the target list (from list_lists()).
            tags: Optional tag strings to apply to every traced record.
            estimate: If True (default), return cost estimate only — does
                NOT consume balance. Flip to False to actually run.
            allow_dirty: If True, includes already-skip-traced records.
                Default False (matches UI behaviour for fresh traces).

        Returns:
            On estimate: usually {records: N, cost: $X, ...} (shape varies).
            On real run: usually {} or job-id metadata.

        Raises:
            DataSiftAPIError: 400 "Insufficient balance!" if the account
                has zero skip-trace credits and is not on an unlimited
                subscription. Unlimited-plan users bypass this check.
        """
        payload: dict = {
            "query": {
                "must": {"all_lists": [list_uuid]},
            },
        }
        if not allow_dirty:
            payload["query"]["must"]["property_type"] = "clean"
        if tags:
            payload["tags"] = tags
        if estimate:
            payload["estimate"] = 1
        return self._request_json(
            "POST", "/api/internal/property/skip-trace/", json=payload
        )

    def skip_trace_properties(
        self,
        property_uuids: list[str],
        *,
        tags: list[str] | None = None,
        estimate: bool = True,
        allow_dirty: bool = False,
    ) -> dict:
        """Skip-trace an explicit list of property UUIDs.

        Same gotchas as skip_trace_list — default is estimate-only.
        The SPA's `actionsFilterQuery` uses `properties: [...uuids...]` as
        the selection key on the property page (and `owners: [...]` on the
        owner page). We use `properties` for property uuids.
        """
        payload: dict = {
            "query": {
                "must": {"properties": list(property_uuids)},
            },
        }
        if not allow_dirty:
            payload["query"]["must"]["property_type"] = "clean"
        if tags:
            payload["tags"] = tags
        if estimate:
            payload["estimate"] = 1
        return self._request_json(
            "POST", "/api/internal/property/skip-trace/", json=payload
        )

    # ─── Upload pipeline ─────────────────────────────────────────

    def _get_presigned_upload(self) -> dict:
        """Return {presigned_post: {url, fields}, key} for S3 upload."""
        return self._request_json("GET", "/api/internal/upload/presigned-upload-url/")

    def _upload_file_to_s3(self, csv_path: Path, presigned: dict) -> None:
        """POST the CSV to S3 using the presigned form fields."""
        url = presigned["presigned_post"]["url"]
        fields = presigned["presigned_post"]["fields"]
        with csv_path.open("rb") as f:
            # Field order matters for some S3 setups: form fields BEFORE file.
            resp = requests.post(
                url,
                data=fields,
                files={"file": (csv_path.name, f, "text/csv")},
                timeout=120,
            )
        if resp.status_code not in (200, 201, 204):
            raise DataSiftAPIError(resp.status_code, resp.text, url)

    def _get_suggested_mapping(self, storage_key: str, upload_type: str) -> dict:
        """Ask DataSift to auto-detect column → schema mapping."""
        data = self._request_json(
            "POST",
            "/api/internal/upload/suggested-mapping/",
            json={"storage_key": storage_key, "upload_type": upload_type},
        )
        return data["suggested_mapping"]

    def upload_csv(
        self,
        csv_path: str | Path,
        list_name: str,
        *,
        tags: list[str] | None = None,
        upload_type: str = "new_properties",
        enrich_property: bool = False,
        enrich_owner: bool = False,
        replace_owner: bool = False,
        mapping_override: dict | None = None,
        verify_landed: bool = True,
    ) -> dict:
        """Upload a CSV to DataSift via the direct API path.

        Args:
            csv_path: Path to a UTF-8 CSV with headers in row 1.
            list_name: Target list. Created server-side if missing.
            tags: Tag strings. Created server-side if missing.
            upload_type: One of UPLOAD_TYPES.
            enrich_property / enrich_owner / replace_owner: Enrichment knobs.
                These map directly to the `data_enriching` nested object
                on the commit. Setting any True kicks off enrichment as
                part of the same upload — no separate call needed.
            mapping_override: If provided, used verbatim as `mapping` and
                the auto-mapping step is skipped. Use when DataSift's
                auto-detect picks wrong columns (e.g. when CSV has many
                similarly-named headers).
            verify_landed: If True, GET /list/ after upload to confirm the
                target list exists. Catches the silent-success bugs that
                burned the Playwright path historically.

        Returns:
            {success, storage_key, list_name, suggested_mapping, line_count}
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        if upload_type not in UPLOAD_TYPES:
            raise ValueError(
                f"Unknown upload_type={upload_type!r}. "
                f"Must be one of: {sorted(UPLOAD_TYPES)}"
            )

        # 1. Get presigned S3 URL
        logger.info("[1/4] Getting presigned upload URL…")
        presigned = self._get_presigned_upload()
        storage_key = presigned["key"]
        logger.debug("    storage_key=%s", storage_key)

        # 2. Upload bytes directly to S3
        logger.info("[2/4] Uploading %s to S3…", csv_path.name)
        self._upload_file_to_s3(csv_path, presigned)

        # 3. Ask for an auto-mapping (or use the caller's override)
        if mapping_override is not None:
            logger.info("[3/4] Using caller-provided mapping override")
            mapping = mapping_override
        else:
            logger.info("[3/4] Asking DataSift for suggested mapping…")
            mapping = self._get_suggested_mapping(storage_key, upload_type)

        # Pull the validated line count from the usage endpoint — this is
        # how the SPA confirms the file parsed correctly.
        usage = self.get_upload_usage(storage_key)
        line_count = usage.get("line_count")
        logger.info("    DataSift sees %s records in the file", line_count)

        # 4. Commit
        logger.info("[4/4] Committing upload as list %r…", list_name)
        commit_body: dict = {
            "storage_key": storage_key,
            "mapping": mapping,
            "upload_type": upload_type,
            "tags": tags or [],
            "lists": [list_name],
            "filename": csv_path.name,
        }
        if enrich_property or enrich_owner or replace_owner:
            commit_body["data_enriching"] = {
                "enrich_property": enrich_property,
                "enrich_owner": enrich_owner,
                "replace_owner": replace_owner,
            }

        resp = self._request("POST", "/api/internal/upload/", json=commit_body)
        if resp.status_code not in (200, 201, 202, 204):
            raise DataSiftAPIError(resp.status_code, resp.text, "/api/internal/upload/")

        # 5. Verify the list landed (catch silent-success regressions)
        verified = None
        list_uuid: str | None = None
        if verify_landed:
            # DataSift creates the list asynchronously; give it a moment.
            import time
            time.sleep(2)
            try:
                lists = self.list_lists(limit=200)
                matching = next(
                    (l for l in lists if l.get("title") == list_name), None,
                )
                verified = matching is not None
                if matching:
                    list_uuid = matching.get("uuid")
                else:
                    logger.warning(
                        "Upload returned %d but list %r is not visible yet. "
                        "It may still be processing — check the DataSift UI.",
                        resp.status_code, list_name,
                    )
            except DataSiftAPIError as e:
                logger.warning("Could not verify upload landed: %s", e)

        return {
            "success": resp.status_code in (200, 201, 202, 204),
            "storage_key": storage_key,
            "list_name": list_name,
            "list_uuid": list_uuid,
            "filename": csv_path.name,
            "line_count": line_count,
            "verified_in_lists": verified,
            "suggested_mapping": mapping,
        }
