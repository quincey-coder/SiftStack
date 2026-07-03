#!/usr/bin/env python3
"""Preflight check for the SiftStack → Google Drive upload.

Confirms the service account can SEE and WRITE to your two destination folders
(00_Inbox/daily and 05_Deep-Prospecting-Reports) exactly the way the pipeline
does — it get-or-creates a nested Year/Month/County/Type test path, uploads a
tiny file, then deletes everything it made.

Reads GOOGLE_SERVICE_ACCOUNT_KEY from the environment (.env). Folder IDs come
from the command line or from GOOGLE_DRIVE_FOLDER_ID / GOOGLE_DRIVE_REPORTS_FOLDER_ID.

Usage:
    python verify_drive.py                          # uses IDs from .env
    python verify_drive.py <inbox_id> <reports_id>  # or pass them directly
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from googleapiclient.http import MediaInMemoryUpload
import drive_uploader as d


def check(service, label, folder_id):
    print(f"\n=== {label} ===")
    print(f"folder id: {folder_id or '(not set)'}")
    if not folder_id:
        print("✗ folder id not set — skipping")
        return False

    # (info) Try to read the folder's name. May be blocked by the drive.file
    # scope even when writes work, so this is informational, not pass/fail.
    try:
        meta = service.files().get(
            fileId=folder_id, fields="id,name,driveId", supportsAllDrives=True,
        ).execute()
        drive_kind = "Shared drive" if meta.get("driveId") else "My Drive"
        print(f"• folder name: '{meta.get('name')}'  ({drive_kind})")
    except Exception:
        print("• (couldn't read folder name — normal under drive.file scope; "
              "the write test below is what matters)")

    try:
        # 1. Replicate the pipeline: get-or-create a nested subfolder path.
        top = d._get_or_create_folder(service, folder_id, "_siftstack_verify")
        leaf = d.ensure_folder_path(service, top, ["2026", "07-July", "Travis", "probate"])
        print("✓ created nested folders (Year/Month/County/Type) — auto-foldering works")

        # 2. Upload a tiny test file into the leaf.
        f = service.files().create(
            body={"name": "verify.txt", "parents": [leaf]},
            media_body=MediaInMemoryUpload(b"siftstack ok", mimetype="text/plain"),
            fields="id", supportsAllDrives=True,
        ).execute()
        print("✓ uploaded a test file — write access confirmed")
    except Exception as e:
        print(f"✗ FAILED (write access): {e}")
        print("  → Fix: add the service account as **Content Manager** on this "
              "drive/folder, and double-check the folder ID.")
        return False

    # Write access is everything the pipeline needs — it only creates folders and
    # uploads files, it never deletes. Cleanup below is best-effort; a missing
    # delete permission does NOT affect the pipeline, it just leaves the harmless
    # test folder behind.
    print("PASS ✅  — service account can create folders and upload here.")
    try:
        service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
        service.files().delete(fileId=top, supportsAllDrives=True).execute()
        print("✓ cleaned up the test folder")
    except Exception:
        print("• couldn't auto-remove the '_siftstack_verify' test folder — the SA "
              "can add but not delete (fine; the pipeline never deletes). Delete "
              "that folder by hand when convenient.")
    return True


def main():
    key = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY", "")
    if not key:
        print("✗ GOOGLE_SERVICE_ACCOUNT_KEY is not set. Put it in .env or export it.")
        sys.exit(1)

    try:
        service = d._build_service(key)
    except Exception as e:
        print(f"✗ Bad service-account key — could not authenticate: {e}")
        sys.exit(1)

    # All four Drive destinations. Inbox/reports can be overridden via argv;
    # cleanup/forensics come from the environment (both optional).
    folders = [
        ("00_Inbox/daily (records)",
         sys.argv[1] if len(sys.argv) > 1 else os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")),
        ("05_Deep-Prospecting-Reports (PDFs)",
         sys.argv[2] if len(sys.argv) > 2 else os.getenv("GOOGLE_DRIVE_REPORTS_FOLDER_ID", "")),
        ("03_Sold-Cleanup (cleanup CSV)", os.getenv("GOOGLE_DRIVE_CLEANUP_FOLDER_ID", "")),
        ("04_Forensics-&-Audit (diff JSON)", os.getenv("GOOGLE_DRIVE_FORENSICS_FOLDER_ID", "")),
    ]

    print("Authenticated with service account. Checking folders…")
    results = []
    for label, fid in folders:
        if not fid:
            print(f"\n=== {label} ===")
            print("• not set — skipping (optional; falls back to the inbox folder)")
            continue
        results.append(check(service, label, fid))

    print("\n" + "=" * 40)
    if results and all(results):
        print("ALL GOOD ✅  — every configured folder is writable.")
        sys.exit(0)
    print("SOME CHECKS FAILED ✗  — fix the notes above, then re-run.")
    sys.exit(1)


if __name__ == "__main__":
    main()
