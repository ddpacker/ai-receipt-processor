#!/usr/bin/env python3
"""
upload_to_notion.py — Process 2
Reads a per-run receipts CSV from parse_receipts.py and pushes each receipt
+ its line items to Notion (ReceiptDB + GroceryDB).

Refuses the entire file if any receipt is missing store or a YYYY-MM-DD date.
Idempotent: tracks pushed source_files in push_manifest.json.

Usage:
    python upload_to_notion.py
    python upload_to_notion.py --input ./output/080526_receipts_raw.csv
    python upload_to_notion.py --repush    # ignore push manifest, re-push all
"""

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from category_config import grocery_category_map
from receipt_complete import incomplete_receipts
from run_csv import find_latest_run_csv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    input_csv:          Path
    output_dir:         Path
    push_manifest_name: str
    notion_token:       str
    receipt_db_id:      str
    grocery_db_id:      str


DEFAULT_OUTPUT_DIR    = Path("./output")
DEFAULT_MANIFEST_NAME = "push_manifest.json"

# ── Notion REST API ───────────────────────────────────────────────────────────
NOTION_API_BASE    = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"

GROCERY_CATEGORY_MAP = grocery_category_map()


# ── CSV helpers ───────────────────────────────────────────────────────────────
def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_push_manifest(path: Path) -> set:
    if path.exists():
        return set(json.loads(path.read_text()))
    return set()


def save_push_manifest(path: Path, pushed: set):
    path.write_text(json.dumps(sorted(pushed), indent=2))


# ── Receipt grouping ──────────────────────────────────────────────────────────
def group_rows_by_receipt(rows: list[dict]) -> dict[str, dict]:
    receipts: dict[str, dict] = {}
    for row in rows:
        key = row["source_file"]
        if key not in receipts:
            receipts[key] = {
                "store":  row.get("store", "UNKNOWN"),
                "date":   row.get("date", ""),
                "total":  row.get("total", ""),
                "items":  [],
            }
        if row.get("raw_name", "").strip():
            receipts[key]["items"].append(row)
    return receipts


# ── Notion helpers ────────────────────────────────────────────────────────────
def notion_headers(token: str) -> dict:
    return {
        "Authorization":  f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type":   "application/json",
    }


def notion_request(
    method: str,
    endpoint: str,
    token: str,
    payload: dict | None = None,
    retries: int = 3,
) -> dict:
    url = f"{NOTION_API_BASE}{endpoint}"
    for attempt in range(retries):
        try:
            resp = httpx.request(
                method,
                url,
                headers=notion_headers(token),
                json=payload,
                timeout=30,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2"))
                print(f"    Rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                # Notion puts the actionable reason (bad property name/type, etc.)
                # in the response body, not the status line.
                raise RuntimeError(f"{resp.status_code} from {method} {endpoint}: {resp.text}")
            return resp.json()
        except httpx.TimeoutException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Notion API call failed after {retries} attempts: {method} {endpoint}")


def map_receipt_category(store: str) -> str:
    """Map to ReceiptDB Category select: Grocery | Restaurant."""
    restaurant_keywords = [
        "restaurant", "cafe", "coffee", "pizza", "burger", "grill",
        "kitchen", "diner", "bistro", "sushi", "taco", "mcdonald",
        "subway", "wendys", "chick-fil", "starbucks", "dunkin", "chipotle",
    ]
    if any(kw in store.lower() for kw in restaurant_keywords):
        return "Restaurant"
    return "Grocery"


def build_receipt_payload(source_file: str, receipt_data: dict, config: Config) -> dict:
    store    = receipt_data["store"]
    date     = receipt_data["date"]
    total    = receipt_data["total"]
    category = map_receipt_category(store)
    page_name = Path(source_file).stem

    props: dict = {
        "Name": {
            "title": [{"text": {"content": page_name}}]
        },
        "Store": {
            "select": {"name": store}
        },
        "Date": {
            "date": {"start": date}
        },
        "Category": {
            "select": {"name": category}
        },
    }

    if total not in ("", None):
        try:
            props["Amount"] = {"number": float(total)}
        except (ValueError, TypeError):
            pass

    return {
        "parent": {"database_id": config.receipt_db_id},
        "properties": props,
    }


def build_grocery_payload(item: dict, receipt_page_id: str, config: Config) -> dict:
    name     = (item.get("interp_name") or item.get("raw_name", "Unknown")).strip()
    raw_name = item.get("raw_name", "").strip()
    category = GROCERY_CATEGORY_MAP.get(item.get("category", ""), "Other")
    price    = item.get("price", "")

    props: dict = {
        "Name": {
            "title": [{"text": {"content": name or raw_name or "Unknown"}}]
        },
        "Line Item": {
            "rich_text": [{"text": {"content": raw_name}}]
        },
        "Receipt": {
            "relation": [{"id": receipt_page_id}]
        },
        "Category": {
            "select": {"name": category}
        },
    }

    if price not in ("", None):
        try:
            props["Amount"] = {"number": float(price)}
        except (ValueError, TypeError):
            pass

    return {
        "parent": {"database_id": config.grocery_db_id},
        "properties": props,
    }


def push_receipts_to_notion(
    receipts: dict[str, dict],
    push_manifest: set,
    repush: bool,
    config: Config,
) -> set:
    newly_pushed: set = set()

    for source_file, receipt_data in receipts.items():
        if not repush and source_file in push_manifest:
            print(f"  Skipping (already pushed): {source_file}")
            continue

        items = receipt_data["items"]
        print(f"  Pushing: {source_file} ({len(items)} items)...")

        try:
            receipt_payload = build_receipt_payload(source_file, receipt_data, config)
            receipt_resp    = notion_request("POST", "/pages", config.notion_token, receipt_payload)
            receipt_page_id = receipt_resp["id"]
        except Exception as e:
            print(f"  ✗ Failed to create ReceiptDB entry for {source_file}: {e}")
            continue

        item_errors = 0
        for item in items:
            try:
                grocery_payload = build_grocery_payload(item, receipt_page_id, config)
                notion_request("POST", "/pages", config.notion_token, grocery_payload)
                time.sleep(0.15)  # stay under Notion's 3 req/s per integration limit
            except Exception as e:
                print(f"    ⚠ Failed to create item '{item.get('raw_name', '?')}': {e}")
                item_errors += 1

        if item_errors == 0:
            print(f"  ✓ {source_file}: receipt + {len(items)} items pushed")
        else:
            print(f"  ⚠ {source_file}: pushed with {item_errors} item error(s) — check Notion")
        newly_pushed.add(source_file)

    return push_manifest | newly_pushed


def resolve_input_csv(args_input: Path | None, output_dir: Path) -> Path:
    """Prefer --input, then INPUT_CSV, else the latest dated run CSV in output_dir."""
    if args_input is not None:
        return args_input

    env_csv = os.getenv("INPUT_CSV")
    if env_csv:
        return Path(env_csv)

    latest = find_latest_run_csv(output_dir)
    if latest is None:
        print(f"✗ No dated run CSV found in {output_dir}")
        print("  Expected files like MMDDYY_receipts_raw.csv (from parse_receipts.py)")
        print("  Or pass --input / set INPUT_CSV.")
        sys.exit(1)
    return latest


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Upload a per-run receipts CSV to Notion (ReceiptDB + GroceryDB).")
    parser.add_argument("--input",  type=Path, default=None, help="Path to a dated run CSV (default: latest in OUTPUT_DIR)")
    parser.add_argument("--repush", action="store_true",     help="Re-push all receipts regardless of manifest")
    args = parser.parse_args()

    output_dir = Path(os.getenv("OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
    config = Config(
        input_csv          = resolve_input_csv(args.input, output_dir),
        output_dir         = output_dir,
        push_manifest_name = os.getenv("PUSH_MANIFEST_NAME", DEFAULT_MANIFEST_NAME),
        notion_token       = os.getenv("NOTION_TOKEN", "").strip(),
        receipt_db_id      = os.getenv("RECEIPT_DB_ID", "").strip(),
        grocery_db_id      = os.getenv("GROCERY_DB_ID", "").strip(),
    )

    missing = []
    if not config.notion_token:
        missing.append("NOTION_TOKEN")
    if not config.receipt_db_id:
        missing.append("RECEIPT_DB_ID")
    if not config.grocery_db_id:
        missing.append("GROCERY_DB_ID")
    if missing:
        print(f"✗ Required env var(s) not set: {', '.join(missing)}")
        print("  Add them to your .env file (see README Setup).")
        sys.exit(1)

    if not config.input_csv.exists():
        print(f"✗ Input CSV not found: {config.input_csv}")
        sys.exit(1)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    push_manifest_path = config.output_dir / config.push_manifest_name

    print(f"\n{'='*50}")
    print(f"Notion Uploader — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input:  {config.input_csv}")
    print(f"{'='*50}\n")

    rows     = read_csv(config.input_csv)
    receipts = group_rows_by_receipt(rows)
    print(f"Loaded {len(rows)} rows → {len(receipts)} receipt(s).\n")

    incomplete = incomplete_receipts(receipts)
    if incomplete:
        print("✗ CSV is not ready for Notion — every receipt needs store and date.\n")
        for source_file, missing in incomplete:
            print(f"  {source_file}: missing {', '.join(missing)}")
        print(f"\n{len(incomplete)} incomplete receipt(s). Fill store/date in the CSV, then re-run.")
        print(f"{'='*50}\n")
        sys.exit(1)

    push_manifest = load_push_manifest(push_manifest_path)
    print(f"Already pushed: {len(push_manifest)} receipt(s)\n")

    print("── Pushing to Notion ───────────────────────────────────────")
    updated_manifest = push_receipts_to_notion(receipts, push_manifest, args.repush, config)
    save_push_manifest(push_manifest_path, updated_manifest)

    newly = updated_manifest - push_manifest
    print(f"\n✓ Done. New entries pushed: {len(newly)}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
