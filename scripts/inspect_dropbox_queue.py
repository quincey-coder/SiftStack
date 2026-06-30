"""Read-only Dropbox inspector — lists the photo queue without processing.

Enumerates {county}/{notice_type} folders under DROPBOX_ROOT_FOLDER and counts
image files in each. Flags any notice-type folder whose name does NOT normalize
to a value in dropbox_watcher.VALID_NOTICE_TYPES (the silent-skip case).

Does NOT download, delete, process, or upload anything.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
from dropbox.files import FileMetadata, FolderMetadata  # noqa: E402
from dropbox_watcher import _get_client, VALID_NOTICE_TYPES, VALID_EXTENSIONS  # noqa: E402


def normalize_type(raw: str) -> str:
    return raw.lower().replace("-", "_").replace(" ", "_")


def main() -> None:
    dbx = _get_client()
    root = config.DROPBOX_ROOT_FOLDER.strip("/")
    base = f"/{root}" if root else ""
    print(f"Root folder: {base or '(account root)'}\n")

    # county -> notice_type_folder -> [photo count, latest_modified]
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    latest: dict[tuple[str, str], str] = {}
    unrecognized: set[str] = set()

    res = dbx.files_list_folder(base or "", recursive=True, limit=2000)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)

    for e in entries:
        if not isinstance(e, FileMetadata):
            continue
        if Path(e.name).suffix.lower() not in VALID_EXTENSIONS:
            continue
        # path: /root/county/notice_type/file
        parts = e.path_display.strip("/").split("/")
        if root:
            rp = root.split("/")
            if parts[: len(rp)] == rp:
                parts = parts[len(rp):]
        if len(parts) < 3:
            continue
        county, type_raw = parts[0], parts[1]
        norm = normalize_type(type_raw)
        counts[county][type_raw] += 1
        mod = getattr(e, "client_modified", None)
        key = (county, type_raw)
        ms = str(mod) if mod else ""
        if ms and (key not in latest or ms > latest[key]):
            latest[key] = ms
        if norm not in VALID_NOTICE_TYPES:
            unrecognized.add(f"{county}/{type_raw}")

    if not counts:
        print("No image files found anywhere under the root folder.")
        print("→ The runner hasn't uploaded any courthouse photos.")
        return

    for county in sorted(counts):
        print(f"{county}/")
        for tfolder in sorted(counts[county]):
            norm = normalize_type(tfolder)
            ok = norm in VALID_NOTICE_TYPES
            flag = "" if ok else "   ⚠ NOT RECOGNIZED by watcher (would be skipped)"
            last = latest.get((county, tfolder), "?")
            print(f"    {tfolder:<22} {counts[county][tfolder]:>4} photos   last={last}{flag}")
        print()

    if unrecognized:
        print("⚠ Folders the watcher will SILENTLY SKIP (type not in VALID_NOTICE_TYPES):")
        for u in sorted(unrecognized):
            print(f"    {u}")
        print(f"\n  VALID_NOTICE_TYPES = {sorted(VALID_NOTICE_TYPES)}")


if __name__ == "__main__":
    main()
