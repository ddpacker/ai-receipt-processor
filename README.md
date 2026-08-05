# receipt-parsing

Two-step pipeline: parse receipt JPGs with Claude vision into CSV, then upload to Notion (ReceiptDB + GroceryDB).

## Requirements

```bash
pip install -r requirements.txt
```

```
anthropic
python-dotenv
Pillow
httpx
```

## Setup

Create a `.env` file in the project root:

```ini
ANTHROPIC_API_KEY=sk-ant-...
NOTION_TOKEN=ntn_...
RECEIPT_DB_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
GROCERY_DB_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Optional overrides (these are the defaults)
INPUT_DIR=./receipts
OUTPUT_DIR=./output
ARCHIVE_DIR=./archive
MAX_TOKENS=4096
IMAGE_MAX_EDGE=1568
JPEG_QUALITY=85
# INPUT_CSV=./output/080526_receipts_raw.csv   # optional; default = latest dated run CSV
```

`NOTION_TOKEN`, `RECEIPT_DB_ID`, and `GROCERY_DB_ID` are required for `upload_to_notion.py`. Copy each database ID from its Notion URL, and share your Notion integration with both databases. If you override `OUTPUT_DIR`, upload still looks there for the latest dated run CSV (or set `INPUT_CSV` / `--input`).

Before sending to Claude, each JPG is auto-oriented, converted to grayscale, downscaled so the long edge is at most `IMAGE_MAX_EDGE` (Claude's standard vision sweet spot), and re-encoded at `JPEG_QUALITY`. Raise `IMAGE_MAX_EDGE` toward ~2576 only if small print is still hard to read.

## JPG Naming Convention

JPGs must be named in the format `MMDDYY_StoreNameInPascalCase.jpg`:

```
042326_Aldi.jpg
052624_WholeFoods.jpg
011525_TraderJoes.jpg
```

Date and store name are parsed directly from the filename and the vision model only extracts line items, prices, and the total.

## Usage

### 1. Parse receipts → CSV

```bash
# Process all JPGs in INPUT_DIR
python parse_receipts.py

# Reprocess already-processed JPGs (ignores manifest)
# Move JPGs back from ARCHIVE_DIR to INPUT_DIR first
python parse_receipts.py --reprocess
```

Each invocation writes a **new** per-run CSV named for the day you run the script (not receipt dates):

```
080526_receipts_raw.csv      # first run that day
080526_receipts_raw_2.csv    # second run / reprocess same day
080626_receipts_raw.csv      # next calendar day
```

Same-day collisions get a `_N` suffix — never overwrite. Processed JPGs move to `ARCHIVE_DIR`. `processed_manifest.json` stays a cumulative skip list across runs.

### 2. Upload CSV → Notion

```bash
# Push from the latest dated run CSV in OUTPUT_DIR
python upload_to_notion.py

# Explicit CSV (e.g. an older run)
python upload_to_notion.py --input ./output/080526_receipts_raw.csv

# Re-push all receipts in that CSV (ignores push manifest; creates duplicate Notion pages)
python upload_to_notion.py --repush
```

Pushed `source_file` values are tracked in `OUTPUT_DIR/push_manifest.json` (also cumulative). Each receipt becomes one ReceiptDB page; each line item becomes one GroceryDB page related to that receipt.

## Output

Per-run CSV in `OUTPUT_DIR` (`MMDDYY_receipts_raw.csv`) with one row per line item:

| Field | Description |
|---|---|
| source_file | Original JPG filename |
| store | Parsed from filename |
| date | Parsed from filename (YYYY-MM-DD) |
| total | Final receipt total |
| raw_name | Verbatim item text from receipt |
| interp_name | Cleaned name if confidently interpretable |
| category | Category if confidently assignable |
| price | Item price |

## Categories

Edit [`categories.json`](categories.json) to add or remove grocery categories. Both `parse_receipts.py` (Claude prompt) and `upload_to_notion.py` (Notion select mapping) load from that file. Override the path with `CATEGORIES_FILE` if needed.

Unmapped / empty grocery categories become `Other` in Notion (always available as a fallback; not listed in the extraction prompt).

## Notion schemas

Property names and types must match exactly. Select option names are usually created automatically by the Notion API when first used.

### ReceiptDB

| Property | Type | Notes |
|---|---|---|
| Name | Title | Set to the JPG filename (`source_file`) |
| Store | Select | From filename (option is created/used per store name) |
| Date | Date | From filename (`YYYY-MM-DD`) |
| Amount | Number | Receipt total |
| Category | Select | `Grocery` or `Restaurant` (inferred from store name) |

### GroceryDB

| Property | Type | Notes |
|---|---|---|
| Name | Title | Prefer `interp_name`, else `raw_name` |
| Line Item | Rich text | Verbatim `raw_name` from the receipt |
| Amount | Number | Item price |
| Category | Select | Options from `categories.json`, plus `Other` |
| Receipt | Relation | Points at the parent ReceiptDB page |

**ReceiptDB Category options:** `Grocery`, `Restaurant`
