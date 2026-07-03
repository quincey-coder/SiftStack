"""Upload CSV and summary files to Google Drive via service account."""

import base64
import json
import logging
from datetime import datetime
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _build_service(service_account_key_b64: str):
    """Build an authenticated Google Drive API service from a base64-encoded key."""
    key_json = json.loads(base64.b64decode(service_account_key_b64))
    creds = Credentials.from_service_account_info(key_json, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


MIME_MAP = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
}

FOLDER_MIME = "application/vnd.google-apps.folder"

# Cache of (parent_id, child_name) → folder_id so a single run doesn't re-query
# Drive for the same YYYY/YYYY-MM/... path on every file.
_FOLDER_CACHE: dict[tuple[str, str], str] = {}


def _get_or_create_folder(service, parent_id: str, name: str) -> str:
    """Return the ID of child folder ``name`` under ``parent_id``, creating it if absent."""
    cache_key = (parent_id, name)
    if cache_key in _FOLDER_CACHE:
        return _FOLDER_CACHE[cache_key]

    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe}' and '{parent_id}' in parents "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    resp = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    matches = resp.get("files", [])
    if matches:
        folder_id = matches[0]["id"]
    else:
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        folder = service.files().create(
            body=meta, fields="id", supportsAllDrives=True,
        ).execute()
        folder_id = folder["id"]
        logger.info("Created Drive subfolder: %s", name)

    _FOLDER_CACHE[cache_key] = folder_id
    return folder_id


def ensure_folder_path(service, root_folder_id: str, path_parts) -> str:
    """Get-or-create a nested folder path under ``root_folder_id``; return the leaf ID.

    Example: ``ensure_folder_path(svc, ROOT, ["2026", "2026-07", "probate"])``.
    Blank/None parts are skipped so partial metadata never breaks the upload.
    """
    parent = root_folder_id
    for part in path_parts or []:
        part = (str(part) if part is not None else "").strip()
        if not part:
            continue
        parent = _get_or_create_folder(service, parent, part)
    return parent


def upload_file(
    file_path: Path,
    folder_id: str,
    service_account_key_b64: str,
    filename: str | None = None,
    mimetype: str | None = None,
    subfolder_parts=None,
) -> str | None:
    """Upload any file to Google Drive and return the shareable webViewLink.

    Args:
        file_path: Local path to the file.
        folder_id: Google Drive folder ID to upload into.
        service_account_key_b64: Base64-encoded JSON service account key.
        filename: Drive filename (default: use local filename).
        mimetype: MIME type (default: auto-detect from extension).
        subfolder_parts: Optional list of subfolder names to get-or-create under
            ``folder_id`` (e.g. ["2026", "2026-07", "probate"]); the file lands in
            the leaf. None/empty → upload straight into ``folder_id`` (flat, legacy).

    Returns:
        Google Drive webViewLink on success, None on failure.
    """
    try:
        service = _build_service(service_account_key_b64)
        file_path = Path(file_path)

        if not filename:
            filename = file_path.name
        if not mimetype:
            mimetype = MIME_MAP.get(file_path.suffix.lower(), "application/octet-stream")

        target_folder = folder_id
        if subfolder_parts:
            target_folder = ensure_folder_path(service, folder_id, subfolder_parts)

        file_metadata = {
            "name": filename,
            "parents": [target_folder],
        }
        media = MediaFileUpload(str(file_path), mimetype=mimetype)

        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()

        link = file.get("webViewLink", "")
        logger.info("Uploaded %s to Drive: %s (%s)", mimetype, filename, link)
        return link

    except Exception:
        logger.exception("Failed to upload file to Google Drive: %s", file_path)
        return None


def upload_csv(
    csv_path: Path,
    folder_id: str,
    service_account_key_b64: str,
    record_count: int,
) -> str | None:
    """Upload a CSV file to Google Drive.

    Args:
        csv_path: Local path to the CSV file.
        folder_id: Google Drive folder ID to upload into.
        service_account_key_b64: Base64-encoded JSON service account key.
        record_count: Number of records in the CSV (for filename).

    Returns:
        Google Drive file ID on success, None on failure.
    """
    try:
        service = _build_service(service_account_key_b64)

        date_str = datetime.now().strftime("%Y-%m-%d")
        drive_filename = f"TX_Notices_{date_str}_{record_count}_records.csv"

        file_metadata = {
            "name": drive_filename,
            "parents": [folder_id],
        }
        media = MediaFileUpload(str(csv_path), mimetype="text/csv")

        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()

        file_id = file.get("id")
        link = file.get("webViewLink", "")
        logger.info("Uploaded CSV to Drive: %s (%s)", drive_filename, link)
        return file_id

    except Exception:
        logger.exception("Failed to upload CSV to Google Drive")
        return None


def upload_summary(
    notices_by_type: dict[str, int],
    notices_by_county: dict[str, int],
    total: int,
    folder_id: str,
    service_account_key_b64: str,
) -> str | None:
    """Upload a summary text file to Google Drive.

    Returns:
        Google Drive file ID on success, None on failure.
    """
    try:
        service = _build_service(service_account_key_b64)

        date_str = datetime.now().strftime("%Y-%m-%d")
        summary_filename = f"TX_Notices_{date_str}_summary.txt"

        # Build summary content
        lines = [
            f"SiftStack Scrape Summary — {date_str}",
            f"Total notices: {total}",
            "",
            "By type:",
        ]
        for ntype, count in sorted(notices_by_type.items()):
            lines.append(f"  {ntype}: {count}")
        lines.append("")
        lines.append("By county:")
        for county, count in sorted(notices_by_county.items()):
            lines.append(f"  {county}: {count}")

        summary_text = "\n".join(lines)

        # Write to temp file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(summary_text)
            temp_path = f.name

        file_metadata = {
            "name": summary_filename,
            "parents": [folder_id],
        }
        media = MediaFileUpload(temp_path, mimetype="text/plain")

        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()

        # Clean up temp file
        Path(temp_path).unlink(missing_ok=True)

        file_id = file.get("id")
        logger.info("Uploaded summary to Drive: %s", summary_filename)
        return file_id

    except Exception:
        logger.exception("Failed to upload summary to Google Drive")
        return None
