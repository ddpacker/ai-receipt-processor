# receipt-parsing

Parses receipt PDFs via Claude vision and extracts structured data to CSV.

## Requirements

```bash
pip install -r requirements.txt
```

```
anthropic
PyMuPDF
python-dotenv
```

## Setup

Create a `.env` file in the project root:

```ini
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides (these are the defaults)
INPUT_DIR=./receipts
OUTPUT_DIR=./output
ARCHIVE_DIR=./archive
RASTER_DPI=200
MAX_TOKENS=4096
```

## PDF Naming Convention

PDFs must be named in the format `MMDDYY_StoreNameInPascalCase.pdf`:

```
042326_Aldi.pdf
052624_WholeFoods.pdf
011525_TraderJoes.pdf
```

Date and store name are parsed directly from the filename and the vision model only extracts line items, prices, and the total.

## Usage

```bash
# Process all PDFs in INPUT_DIR
python parse_receipts.py

# Reprocess already-processed PDFs (ignores manifest) (note that you will need to manually move your PDFs back from ARCHIVE_DIR to INPUT_DIR)
python parse_receipts.py --reprocess
```

Processed PDFs are moved to `ARCHIVE_DIR` automatically. Rerunning is safe as the manifest tracks completed files and skips them.

## Output

`output/receipts_raw.csv` with one row per line item:

| Field | Description |
|---|---|
| source_file | Original PDF filename |
| store | Parsed from filename |
| date | Parsed from filename (YYYY-MM-DD) |
| total | Final receipt total |
| raw_name | Verbatim item text from receipt |
| interp_name | Cleaned name if confidently interpretable |
| category | Category if confidently assignable |
| price | Item price |

## Categories

These exist within the prompt in parse_receipts.py currently; add or remove as necessary...

`Produce`, `Dairy`, `Meat`, `Pantry`, `Frozen`, `Beverages`, `Deli`, `Snacks & Candy`, `Personal Care`, `Cleaning Supplies`, `Pet`, `Pharmacy`

Items the model isn't confident about are left with empty `interp_name` and `category` fields for downstream processing.