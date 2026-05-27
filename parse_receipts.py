#!/usr/bin/env python3
"""
parse_receipts.py — Process 1
Rasterizes receipt PDFs via PyMuPDF, sends each page image to Claude vision,
extracts structured item/price/total data, and appends to receipts_raw.csv.

Idempotent: skips already-processed files tracked in processed_manifest.json.

Usage:
    python parse_receipts.py
    python parse_receipts.py --input ./my/pdfs --output ./my/output
    python parse_receipts.py --reprocess  # ignore manifest, reprocess all
"""

import base64
import csv
import json
import os
import re
import shutil
import sys
import argparse
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass

import fitz  # PyMuPDF
import anthropic

load_dotenv()

# ── Default Config ──────────────────────
@dataclass
class Config:
    input_dir:    Path
    output_dir:   Path
    archive_dir:  Path
    raw_csv_name: str
    manifest_name:str
    raster_dpi:   int
    model:        str
    max_tokens:   int

DEFAULT_INPUT_DIR     = Path("./receipts")
DEFAULT_OUTPUT_DIR    = Path("./output")
ARCHIVE_DIR           = Path("./archive")
RAW_CSV_NAME          = "receipts_raw.csv"
MANIFEST_NAME         = "processed_manifest.json"
RASTER_DPI            = 200
MODEL                 = "claude-sonnet-4-6"
MAX_TOKENS            = 4096

RAW_CSV_FIELDS = [
    "source_file",      # original PDF filename - in the format "MMDDYY_StoreName.pdf"
    "store",            # parsed from PDF filename
    "date",             # parsed from PDF filename, in MM-DD-YY format
    "total",
    "raw_name",         # verbatim from receipt
    "interp_name",      # cleaned up name if possible, but not required
    "category",         # either a known category, or empty string if unknown
    "price",
]

# ── Prompt ────────────────────────────
# TODO: Parameterize the known categories from a config file for easier updates without code changes. For now, hardcoded in the prompt for simplicity.
EXTRACTION_PROMPT = """You are extracting structured data from a receipt image.

Extract the following and respond ONLY with a valid JSON object — no markdown fences, no explanation:
{
  "total": numeric final total after tax (no $ sign), or empty string,
  "items": [
    { 
        "raw_name": EXACT text from receipt, 
        "interp_name": Cleaned up name if possible, or an empty string if not,
        "category": Either a known category, or an empty string if unknown,
        "price": numeric or "" if the price is illegible 
    }
  ]
}

Known Categories:
    "Produce", "Dairy", "Meat", "Pantry", "Frozen", "Beverages",
    "Deli", "Snacks & Candy", "Personal Care", "Cleaning Supplies",
    "Pet", "Pharmacy"

Rules:
- raw_name must be verbatim — do not interpret, clean, or expand abbreviations.
- Only provide interp_name and category if you are reasonably confident; otherwise leave as empty string.
- Exclude tax lines, subtotals, totals, discount/coupon lines, payment method lines.
- Do not invent items not visible in the image.
- total is the final amount charged (look for: Total, Amount Due, Balance Due, Total Due).
- If a price is partially visible or unclear, use empty string.
"""


def load_manifest(path: Path) -> set:
    if path.exists():
        return set(json.loads(path.read_text()))
    return set()


def save_manifest(path: Path, processed: set):
    path.write_text(json.dumps(sorted(processed), indent=2))


def pdf_to_base64_images(pdf_path: Path, config: Config) -> list[str]:
    """Rasterize each page of a PDF and return list of base64-encoded PNGs."""
    doc = fitz.open(str(pdf_path))
    images = []
    mat = fitz.Matrix(config.raster_dpi / 72, config.raster_dpi / 72)
    for page in doc:
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        png_bytes = pix.tobytes("png")
        images.append(base64.standard_b64encode(png_bytes).decode("utf-8"))
    doc.close()
    return images


def extract_receipt_data(client: anthropic.Anthropic, b64_images: list[str], config: Config) -> dict:
    """
    Send receipt page images to Claude vision and parse the JSON response.
    For multi-page receipts, concatenate pages into one call.
    """
    content = []
    for b64 in b64_images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64}
        })
    content.append({"type": "text", "text": EXTRACTION_PROMPT})

    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        messages=[{"role": "user", "content": content}]
    )

    raw_text = response.content[0].text.strip()

    # Strip accidental markdown fences if model wraps anyway
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    return json.loads(raw_text)


def append_to_csv(csv_path: Path, rows: list[dict]):
    file_exists = csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

def parse_filename(pdf_path: Path) -> tuple[str, str]:
    """Extract date and store from MMDDYY_StoreName.pdf format."""
    stem = pdf_path.stem
    parts = stem.split("_", 1)
    
    date_str = ""
    store    = ""
    
    if len(parts) == 2:
        raw_date, raw_store = parts
        store = re.sub(r'(?<!^)(?=[A-Z])', ' ', raw_store).strip()
        try:
            date_str = datetime.strptime(raw_date, "%m%d%y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    return date_str, store

def process_pdf(client: anthropic.Anthropic, pdf_path: Path, csv_path: Path, config: Config) -> bool:
    """
    Process a single PDF: rasterize > extract > write CSV rows.
    Returns True on success, False on failure.
    """
    date, store = parse_filename(pdf_path)
    print(f" INFO: Processing {pdf_path.name}")

    try:
        b64_images = pdf_to_base64_images(pdf_path, config)
    except Exception as e:
        print(f" ERROR: Failed to rasterize {pdf_path.name}: {e}")
        return False

    try:
        data = extract_receipt_data(client, b64_images, config)
    except json.JSONDecodeError as e:
        print(f" ERROR: JSON parse error for {pdf_path.name}: {e}")
        return False
    except Exception as e:
        print(f" ERROR: API error for {pdf_path.name}: {e}")
        return False

    rows = []
    items = data.get("items", [])
    total = data.get("total", "")

    if not items:
        print(f" WARN: No items extracted from {pdf_path.name}. Writing receipt header row only.")

    for item in items:
        rows.append({
            "source_file":  pdf_path.name,
            "store":        store or "",
            "date":         date or "",
            "total":        total,
            "raw_name":     item.get("raw_name", ""),
            "interp_name":  item.get("interp_name", ""),
            "category":     item.get("category", ""),
            "price":        item.get("price", ""),
        })

    if not rows:
        rows.append({
            "source_file":  pdf_path.name,
            "store":        store,
            "date":         date,
            "total":        total,
            "raw_name":     "",
            "interp_name":  "",
            "category":     "",
            "price":        "",
        })

    append_to_csv(csv_path, rows)
    print(f" INFO: {len(items)} items written for {pdf_path.name}")
    return True

def ensure_output_files(raw_csv: Path):
    if not raw_csv.exists():
        with open(raw_csv, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=RAW_CSV_FIELDS).writeheader()

def main():
    parser = argparse.ArgumentParser(description="Parse PDF receipts with Claude vision and extract structured data to CSV.")
    parser.add_argument("--reprocess", action="store_true", help="Ignore manifest and reprocess all PDFs")
    args = parser.parse_args()

    config = Config(
        input_dir    = Path(os.getenv("INPUT_DIR", DEFAULT_INPUT_DIR)),
        output_dir   = Path(os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)),
        archive_dir  = Path(os.getenv("ARCHIVE_DIR", ARCHIVE_DIR)),
        raw_csv_name = os.getenv("RAW_CSV_NAME", RAW_CSV_NAME),
        manifest_name= os.getenv("MANIFEST_NAME", MANIFEST_NAME),
        raster_dpi   = int(os.getenv("RASTER_DPI", RASTER_DPI)),
        model        = os.getenv("MODEL", MODEL),
        max_tokens   = int(os.getenv("MAX_TOKENS", MAX_TOKENS)),
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)

    csv_path      = config.output_dir / config.raw_csv_name
    manifest_path = config.output_dir / config.manifest_name

    ensure_output_files(csv_path)

    if not config.input_dir.exists():
        print(f" ERROR: Input directory not found: {config.input_dir}")
        sys.exit(1)

    pdfs = sorted(config.input_dir.glob("*.pdf"))
    if not pdfs:
        print(f" ERROR: No PDFs found in {config.input_dir}")
        sys.exit(0)

    manifest = set() if args.reprocess else load_manifest(manifest_path)
    client   = anthropic.Anthropic()

    print(f"\n{'='*50}")
    print(f"Receipt Parser: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input Directory:    {config.input_dir}")
    print(f"PDFs found: {len(pdfs)} | Already processed: {len(manifest)}")
    print(f"{'='*50}\n")

    success_count = 0
    skip_count    = 0
    fail_count    = 0

    for pdf_path in pdfs:
        if pdf_path.name in manifest:
            print(f"  Skipping (already processed): {pdf_path.name}")
            skip_count += 1
            continue

        success = process_pdf(client, pdf_path, csv_path, config)
        if success:
            manifest.add(pdf_path.name)
            save_manifest(manifest_path, manifest)
            shutil.move(str(pdf_path), config.archive_dir / pdf_path.name)
            success_count += 1
        else:
            fail_count += 1

    print(f"\n{'='*50}")
    print(f"Done. Processed: {success_count} | Skipped: {skip_count} | Failed: {fail_count}")
    print(f"Raw CSV: {csv_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Archived PDFs moved to: {config.archive_dir}")
    print(f"{'='*50}\n")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
