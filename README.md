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

# Optional overrides (these are the defaults)
INPUT_DIR=./receipts
OUTPUT_DIR=./output
ARCHIVE_DIR=./archive
INPUT_CSV=./output/receipts_raw.csv
MAX_TOKENS=4096
IMAGE_MAX_EDGE=1568
JPEG_QUALITY=85
# RECEIPT_DB_ID=...
# GROCERY_DB_ID=...
```

Share your Notion integration with both databases. If you override `OUTPUT_DIR`, set `INPUT_CSV` to the same folder’s `receipts_raw.csv` (or pass `--input` when uploading).

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

Processed JPGs are moved to `ARCHIVE_DIR` automatically. Rerunning is safe as the manifest tracks completed files and skips them.

### 2. Upload CSV → Notion

```bash
# Push new receipts from INPUT_CSV (default: ./output/receipts_raw.csv)
python upload_to_notion.py

# Explicit CSV path
python upload_to_notion.py --input ./output/receipts_raw.csv

# Re-push all receipts (ignores push manifest; creates duplicate Notion pages)
python upload_to_notion.py --repush
```

Pushed `source_file` values are tracked in `OUTPUT_DIR/push_manifest.json`. Each receipt becomes one ReceiptDB page; each line item becomes one GroceryDB page related to that receipt.

## Output

`output/receipts_raw.csv` with one row per line item:

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

These exist within the prompt in `parse_receipts.py` currently; add or remove as necessary...

`Produce`, `Dairy`, `Meat`, `Pantry`, `Frozen`, `Beverages`, `Deli`, `Snacks & Candy`, `Personal Care`, `Cleaning Supplies`, `Pet`, `Pharmacy`

Items the model isn't confident about are left with empty `interp_name` and `category` fields for downstream processing. Unmapped / empty grocery categories become `Other` in Notion.

## Notion schemas

Property names and types must match exactly. Select option names must exist in the database.

### ReceiptDB

| Property | Type | Notes |
|---|---|---|
| Name | Title | Set to the JPG filename (`source_file`) |
| Store | Rich text | From filename |
| Date | Date | From filename (`YYYY-MM-DD`) |
| Amount | Number | Receipt total |
| Category | Select | `Grocery` or `Restaurant` (inferred from store name) |
| Data Source | Select | Always set to `Cowork` |
| Processed | Checkbox | Always set to checked |

### GroceryDB

| Property | Type | Notes |
|---|---|---|
| Name | Title | Prefer `interp_name`, else `raw_name` |
| Line Item | Rich text | Verbatim `raw_name` from the receipt |
| Amount | Number | Item price |
| Category | Select | See options below |
| Receipt | Relation | Points at the parent ReceiptDB page |

**GroceryDB Category options:** `Produce`, `Dairy`, `Meat`, `Pantry`, `Frozen`, `Beverages`, `Deli`, `Snacks & Candy`, `Personal Care`, `Cleaning Supplies`, `Pet`, `Pharmacy`, `Other`

**ReceiptDB Category options:** `Grocery`, `Restaurant`

**ReceiptDB Data Source options:** at least `Cowork`
