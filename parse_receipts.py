#!/usr/bin/env python3
"""
parse_receipts.py — Process 1
Reads receipt JPGs, downscales/compresses them for Claude vision, extracts
structured item/price/total data, and appends to receipts_raw.csv.

Idempotent: skips already-processed files tracked in processed_manifest.json.

Usage:
    python parse_receipts.py
    python parse_receipts.py --reprocess  # ignore manifest, reprocess all
"""

import base64
import csv
import io
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

from PIL import Image, ImageOps
import anthropic

from category_config import format_categories_for_prompt, load_categories

load_dotenv()

# ── Default Config ──────────────────────
@dataclass
class Config:
    input_dir:    Path
    output_dir:   Path
    archive_dir:  Path
    raw_csv_name: str
    manifest_name:str
    model:        str
    max_tokens:   int
    image_max_edge: int
    jpeg_quality: int

DEFAULT_INPUT_DIR     = Path("./receipts")
DEFAULT_OUTPUT_DIR    = Path("./output")
ARCHIVE_DIR           = Path("./archive")
RAW_CSV_NAME          = "receipts_raw.csv"
MANIFEST_NAME         = "processed_manifest.json"
MODEL                 = "claude-sonnet-4-6"
MAX_TOKENS            = 4096
# Claude's standard vision tier sweet spot: long edge <= 1568px avoids
# server-side downscale while keeping receipt text readable.
IMAGE_MAX_EDGE        = 1568
JPEG_QUALITY          = 85

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}

RAW_CSV_FIELDS = [
    "source_file",      # original JPG filename - in the format "MMDDYY_StoreName.jpg"
    "store",            # parsed from JPG filename
    "date",             # parsed from JPG filename, in MM-DD-YY format
    "total",
    "raw_name",         # verbatim from receipt
    "interp_name",      # cleaned up name if possible, but not required
    "category",         # either a known category, or empty string if unknown
    "price",
]

# ── Prompt ────────────────────────────
_CATEGORIES = load_categories()
EXTRACTION_PROMPT = f"""You are extracting structured data from a receipt image.

Extract the following and respond ONLY with a valid JSON object — no markdown fences, no explanation:
{{
  "total": numeric final total after tax (no $ sign), or empty string,
  "items": [
    {{ 
        "raw_name": EXACT text from receipt, 
        "interp_name": Cleaned up name if possible, or an empty string if not,
        "category": Either a known category, or an empty string if unknown,
        "price": numeric or "" if the price is illegible 
    }}
  ]
}}

Known Categories:
{format_categories_for_prompt(_CATEGORIES)}

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


def find_receipt_images(input_dir: Path) -> list[Path]:
    """Collect .jpg / .jpeg files (case-insensitive) from the input directory."""
    return sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def prepare_image(path: Path, config: Config) -> bytes:
    """
    Orient, convert to grayscale, downscale long edge, and JPEG-compress.
    Matches Claude's standard vision tier so we don't pay for pixels the API
    would discard anyway, while keeping receipt text sharp enough to read.
    """
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")  # grayscale — color adds tokens, not readability

        w, h = img.size
        long_edge = max(w, h)
        if long_edge > config.image_max_edge:
            scale = config.image_max_edge / long_edge
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=config.jpeg_quality, optimize=True)
        return buf.getvalue()


def image_to_base64(path: Path, config: Config) -> list[tuple[str, str]]:
    """Return [(media_type, b64)] for a single prepared JPEG receipt."""
    jpeg_bytes = prepare_image(path, config)
    data = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
    return [("image/jpeg", data)]


def extract_receipt_data(
    client: anthropic.Anthropic,
    images: list[tuple[str, str]],
    config: Config,
) -> dict:
    """
    Send receipt images to Claude vision and parse the JSON response.
    For multi-image receipts, concatenate into one call.
    """
    content = []
    for media_type, b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}
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

def parse_filename(image_path: Path) -> tuple[str, str]:
    """Extract date and store from MMDDYY_StoreName.jpg format."""
    stem = image_path.stem
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

def process_receipt(client: anthropic.Anthropic, image_path: Path, csv_path: Path, config: Config) -> bool:
    """
    Process a single JPG: prepare > extract > write CSV rows.
    Returns True on success, False on failure.
    """
    date, store = parse_filename(image_path)
    print(f" INFO: Processing {image_path.name}")

    try:
        images = image_to_base64(image_path, config)
    except Exception as e:
        print(f" ERROR: Failed to prepare {image_path.name}: {e}")
        return False

    try:
        data = extract_receipt_data(client, images, config)
    except json.JSONDecodeError as e:
        print(f" ERROR: JSON parse error for {image_path.name}: {e}")
        return False
    except Exception as e:
        print(f" ERROR: API error for {image_path.name}: {e}")
        return False

    rows = []
    items = data.get("items", [])
    total = data.get("total", "")

    if not items:
        print(f" WARN: No items extracted from {image_path.name}. Writing receipt header row only.")

    for item in items:
        rows.append({
            "source_file":  image_path.name,
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
            "source_file":  image_path.name,
            "store":        store,
            "date":         date,
            "total":        total,
            "raw_name":     "",
            "interp_name":  "",
            "category":     "",
            "price":        "",
        })

    append_to_csv(csv_path, rows)
    print(f" INFO: {len(items)} items written for {image_path.name}")
    return True

def ensure_output_files(raw_csv: Path):
    if not raw_csv.exists():
        with open(raw_csv, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=RAW_CSV_FIELDS).writeheader()

def main():
    parser = argparse.ArgumentParser(description="Parse JPG receipts with Claude vision and extract structured data to CSV.")
    parser.add_argument("--reprocess", action="store_true", help="Ignore manifest and reprocess all JPGs")
    args = parser.parse_args()

    config = Config(
        input_dir     = Path(os.getenv("INPUT_DIR", DEFAULT_INPUT_DIR)),
        output_dir    = Path(os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)),
        archive_dir   = Path(os.getenv("ARCHIVE_DIR", ARCHIVE_DIR)),
        raw_csv_name  = os.getenv("RAW_CSV_NAME", RAW_CSV_NAME),
        manifest_name = os.getenv("MANIFEST_NAME", MANIFEST_NAME),
        model         = os.getenv("MODEL", MODEL),
        max_tokens    = int(os.getenv("MAX_TOKENS", MAX_TOKENS)),
        image_max_edge= int(os.getenv("IMAGE_MAX_EDGE", IMAGE_MAX_EDGE)),
        jpeg_quality  = int(os.getenv("JPEG_QUALITY", JPEG_QUALITY)),
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)

    csv_path      = config.output_dir / config.raw_csv_name
    manifest_path = config.output_dir / config.manifest_name

    ensure_output_files(csv_path)

    if not config.input_dir.exists():
        print(f" ERROR: Input directory not found: {config.input_dir}")
        sys.exit(1)

    images = find_receipt_images(config.input_dir)
    if not images:
        print(f" ERROR: No JPGs found in {config.input_dir}")
        sys.exit(0)

    manifest = set() if args.reprocess else load_manifest(manifest_path)
    client   = anthropic.Anthropic()

    print(f"\n{'='*50}")
    print(f"Receipt Parser: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input Directory:    {config.input_dir}")
    print(f"JPGs found: {len(images)} | Already processed: {len(manifest)}")
    print(f"{'='*50}\n")

    success_count = 0
    skip_count    = 0
    fail_count    = 0

    for image_path in images:
        if image_path.name in manifest:
            print(f"  Skipping (already processed): {image_path.name}")
            skip_count += 1
            continue

        success = process_receipt(client, image_path, csv_path, config)
        if success:
            manifest.add(image_path.name)
            save_manifest(manifest_path, manifest)
            shutil.move(str(image_path), config.archive_dir / image_path.name)
            success_count += 1
        else:
            fail_count += 1

    print(f"\n{'='*50}")
    print(f"Done. Processed: {success_count} | Skipped: {skip_count} | Failed: {fail_count}")
    print(f"Raw CSV: {csv_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Archived JPGs moved to: {config.archive_dir}")
    print(f"{'='*50}\n")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
