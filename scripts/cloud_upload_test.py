"""Cloud upload test: run the Actor in upload-only mode on a few rows.

Proves the DataSift upload wizard works from an Apify run (not just from the
operator box) without running the scrape:

  1. picks N rows per CSV from an already-uploaded batch (so DataSift merges
     them by address instead of creating records), blanks Notes, and tags them
     with one test tag,
  2. puts the CSVs + UPLOAD_MANIFEST into a throwaway named key-value store,
  3. starts the Actor with the production schedule's input + upload_only=true
     pointed at that store (no enrich, no skip trace, no Slack),
  4. waits, then downloads the log, UPLOAD_RESULT and every wizard screenshot.

Usage:
  python scripts/cloud_upload_test.py prepare --batch output/pending_upload_2026-11-11 --tag apify_upload_test
  python scripts/cloud_upload_test.py start   [--build latest]
  python scripts/cloud_upload_test.py fetch   <run_id> [--wait]
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
TOK = os.environ["APIFY_TOKEN"]
API = "https://api.apify.com/v2"
ACTOR = "TPcb87d3OJHlI6Yfy"
STORE = "sift-stack-upload-test"
OUT = ROOT / "output" / "cloud_upload_test"


def _store_id() -> str:
    r = requests.post(f"{API}/key-value-stores", params={"token": TOK, "name": STORE}, timeout=60)
    r.raise_for_status()
    return r.json()["data"]["id"]


def _put(sid: str, key: str, body: bytes, ctype: str) -> None:
    r = requests.put(f"{API}/key-value-stores/{sid}/records/{key}", params={"token": TOK},
                     data=body, headers={"Content-Type": ctype}, timeout=120)
    r.raise_for_status()


def prepare(args) -> None:
    batch = ROOT / args.batch
    picks = []          # (source csv, list name, row indexes)
    for spec in args.pick:
        name, lst, idx = spec.split(":")
        picks.append((name, lst, [int(i) for i in idx.split(",")]))
    OUT.mkdir(parents=True, exist_ok=True)
    sid = _store_id()
    manifest = {"batch_tag": "", "csvs": []}
    for name, lst, idxs in picks:
        src = next(batch.glob(f"datasift_{name}_*.csv"))
        with open(src, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
            header = list(rows[0].keys())
        chosen = [dict(rows[i]) for i in idxs]
        for r in chosen:
            assert r["Property Street Address"][:1].isdigit(), r["Property Street Address"]
            r["Tags"] = args.tag
            r["Notes"] = ""
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
        w.writeheader()
        w.writerows(chosen)
        key = f"test_{name}.csv"
        (OUT / key).write_text(buf.getvalue(), encoding="utf-8")
        _put(sid, key, buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8")
        manifest["csvs"].append({"key": key, "label": lst, "list_name": lst})
        for r in chosen:
            print(f"  {key}: {r['Property Street Address']}, {r['Property City']} {r['Property ZIP Code']}"
                  f" | {r['Owner First Name']} {r['Owner Last Name']} {r['FULL NAME/COMPANY/TRUST']}")
    _put(sid, "UPLOAD_MANIFEST", json.dumps(manifest).encode(), "application/json")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print("store", STORE, sid, "manifest:", manifest)


def _schedule_input() -> dict:
    r = requests.get(f"{API}/schedules", params={"token": TOK}, timeout=60)
    r.raise_for_status()
    for s in r.json()["data"]["items"]:
        d = requests.get(f"{API}/schedules/{s['id']}", params={"token": TOK}, timeout=60).json()["data"]
        for a in d.get("actions") or []:
            if a.get("actorId") == ACTOR:
                body = (a.get("runInput") or {}).get("body") or "{}"
                return json.loads(body)
    raise SystemExit("no schedule found for the actor")


def start(args) -> None:
    inp = _schedule_input()
    have = {k: bool(inp.get(k)) for k in ("datasift_email", "datasift_password", "apify_token")}
    print("schedule input has:", have)
    inp.update({
        "upload_only": True, "upload_datasift": True, "state_kvs_name": STORE,
        "datasift_enrich_after_upload": False, "datasift_skip_trace_after_upload": False,
        "notify_slack": False, "use_residential_proxy": False,
    })
    r = requests.post(f"{API}/acts/{ACTOR}/runs",
                      params={"token": TOK, "build": args.build, "timeout": 1500, "memory": 4096},
                      json=inp, timeout=120)
    r.raise_for_status()
    d = r.json()["data"]
    print("run", d["id"], d["status"], "build", d.get("buildNumber"))
    (OUT / "last_run.txt").write_text(d["id"])


def fetch(args) -> None:
    rid = args.run_id or (OUT / "last_run.txt").read_text().strip()
    while True:
        d = requests.get(f"{API}/actor-runs/{rid}", params={"token": TOK}, timeout=60).json()["data"]
        if not args.wait or d["status"] not in ("READY", "RUNNING"):
            break
        time.sleep(20)
    print("run", rid, d["status"], d.get("statusMessage"), "build", d.get("buildNumber"))
    dest = OUT / rid
    dest.mkdir(parents=True, exist_ok=True)
    log = requests.get(f"{API}/logs/{rid}", params={"token": TOK}, timeout=120).text
    (dest / "log.txt").write_text(log, encoding="utf-8")
    kv = d["defaultKeyValueStoreId"]
    keys = requests.get(f"{API}/key-value-stores/{kv}/keys", params={"token": TOK, "limit": 1000},
                        timeout=60).json()["data"]["items"]
    for k in keys:
        key = k["key"]
        if key == "INPUT":
            continue
        b = requests.get(f"{API}/key-value-stores/{kv}/records/{key}", params={"token": TOK}, timeout=300).content
        name = key if "." in key else key + ".json"
        (dest / name).write_bytes(b)
    print("saved", len(keys) - 1, "record(s) + log to", dest)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--batch", default="output/pending_upload_2026-11-11")
    p.add_argument("--tag", default="apify_upload_test")
    p.add_argument("--pick", nargs="+", default=["foreclosure:Foreclosure:0,1", "probate:Probate:0"],
                   help="csv-name:List Name:row,row")
    p.set_defaults(fn=prepare)
    p = sub.add_parser("start")
    p.add_argument("--build", default="latest")
    p.set_defaults(fn=start)
    p = sub.add_parser("fetch")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(fn=fetch)
    a = ap.parse_args()
    a.fn(a)
