#!/usr/bin/env python3
"""
parse_receipts.py — Process 1
Optionally converts SCAN_DIR PDFs (one receipt per page) into GUID JPGs in
INPUT_DIR, then reads receipt JPGs, downscales/compresses them for Claude
vision, extracts structured item/price/total/date/store data, renames JPGs
when a store is known (real date or literal MMDDYY placeholder), and writes
a per-run CSV (MMDDYY_receipts_raw.csv, sequenced on same-day collisions).

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
import uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass

from PIL import Image, ImageOps
import fitz  # PyMuPDF
import anthropic

from category_config import format_categories_for_prompt, load_categories
from receipt_complete import incomplete_receipts
from run_csv import next_run_csv_path

load_dotenv()

# ── Default Config ──────────────────────
@dataclass
class Config:
    input_dir:    Path
    output_dir:   Path
    archive_dir:  Path
    scan_dir:     Path | None
    manifest_name:str
    model:        str
    max_tokens:   int
    image_max_edge: int
    jpeg_quality: int

DEFAULT_INPUT_DIR     = Path("./receipts")
DEFAULT_OUTPUT_DIR    = Path("./output")
ARCHIVE_DIR           = Path("./archive")
MANIFEST_NAME         = "processed_manifest.json"
MODEL                 = "claude-sonnet-4-6"
MAX_TOKENS            = 4096
# Claude's standard vision tier sweet spot: long edge <= 1568px avoids
# server-side downscale while keeping receipt text readable.
IMAGE_MAX_EDGE        = 1568
JPEG_QUALITY          = 85
# Internal PDF render DPI only — not an env knob; IMAGE_MAX_EDGE caps size after.
INTERNAL_RENDER_DPI   = 200

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
PDF_EXTENSIONS   = {".pdf"}
GUID_STEM_RE     = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

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
_PROMPT_RULES = f"""Known Categories:
{format_categories_for_prompt(_CATEGORIES)}

Rules:
- raw_name must be verbatim — do not interpret, clean, or expand abbreviations.
- Only provide interp_name and category if you are reasonably confident; otherwise leave as empty string.
- Exclude tax lines, subtotals, totals, discount/coupon lines, payment method lines.
- Do not invent items not visible in the image.
- total is the final amount charged (look for: Total, Amount Due, Balance Due, Total Due).
- If a price is partially visible or unclear, use empty string.
"""

# Named JPGs (MMDDYY_StoreName.jpg): date/store come from the filename.
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

{_PROMPT_RULES}"""

# GUID JPGs from SCAN_DIR PDF conversion: also ask Claude for date/store to rename.
EXTRACTION_PROMPT_GUID = f"""You are extracting structured data from a receipt image.

Extract the following and respond ONLY with a valid JSON object — no markdown fences, no explanation:
{{
  "purchase_date": "MMDDYY date of purchase if visible, else empty string",
  "store_name": "store / merchant name if visible, else empty string",
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

{_PROMPT_RULES}- purchase_date must be exactly six digits MMDDYY when known (e.g. 042326 for April 23, 2026); otherwise empty string.
- store_name should be the merchant as printed on the receipt when known; otherwise empty string.
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


def find_scan_pdfs(scan_dir: Path) -> list[Path]:
    """Collect .pdf files (case-insensitive) from the scan directory."""
    return sorted(
        p for p in scan_dir.iterdir()
        if p.is_file() and p.suffix.lower() in PDF_EXTENSIONS
    )


def normalize_receipt_image(img: Image.Image, config: Config) -> bytes:
    """
    Orient, convert to grayscale, downscale long edge, and JPEG-compress.
    Matches Claude's standard vision tier so we don't pay for pixels the API
    would discard anyway, while keeping receipt text sharp enough to read.
    """
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


def prepare_image(path: Path, config: Config) -> bytes:
    with Image.open(path) as img:
        return normalize_receipt_image(img, config)


def image_to_base64(path: Path, config: Config) -> list[tuple[str, str]]:
    """Return [(media_type, b64)] for a single prepared JPEG receipt."""
    jpeg_bytes = prepare_image(path, config)
    data = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
    return [("image/jpeg", data)]


def pdf_pages_to_jpgs(pdf_path: Path, config: Config) -> list[Path]:
    """
    Rasterize each PDF page and write a GUID JPG into INPUT_DIR.
    Applies the same IMAGE_MAX_EDGE / JPEG_QUALITY rules as prepare_image.
    """
    doc = fitz.open(str(pdf_path))
    mat = fitz.Matrix(INTERNAL_RENDER_DPI / 72, INTERNAL_RENDER_DPI / 72)
    written: list[Path] = []
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            jpeg_bytes = normalize_receipt_image(img, config)
            out_path = config.input_dir / f"{uuid.uuid4()}.jpg"
            out_path.write_bytes(jpeg_bytes)
            written.append(out_path)
            print(f" INFO: Wrote {out_path.name} from {pdf_path.name} page {page.number + 1}")
    finally:
        doc.close()
    return written


def convert_scan_pdfs(config: Config) -> int:
    """
    Convert PDFs in SCAN_DIR to JPGs in INPUT_DIR. Archive each PDF after
    all of its pages are written. Returns number of JPG pages created.
    """
    if config.scan_dir is None:
        print(" INFO: SCAN_DIR not set; skipping PDF conversion")
        return 0
    if not config.scan_dir.exists():
        print(f" INFO: SCAN_DIR not found ({config.scan_dir}); skipping PDF conversion")
        return 0

    config.input_dir.mkdir(parents=True, exist_ok=True)
    pdfs = find_scan_pdfs(config.scan_dir)
    if not pdfs:
        print(f" INFO: No PDFs found in {config.scan_dir}")
        return 0

    total_pages = 0
    for pdf_path in pdfs:
        print(f" INFO: Converting {pdf_path.name}")
        try:
            pages = pdf_pages_to_jpgs(pdf_path, config)
        except Exception as e:
            print(f" ERROR: Failed to convert {pdf_path.name}: {e}")
            continue
        if not pages:
            print(f" WARN: No pages written for {pdf_path.name}")
            continue
        dest = config.archive_dir / pdf_path.name
        if dest.exists():
            stem, suffix = pdf_path.stem, pdf_path.suffix
            n = 2
            while True:
                dest = config.archive_dir / f"{stem}_{n}{suffix}"
                if not dest.exists():
                    break
                n += 1
        shutil.move(str(pdf_path), dest)
        print(f" INFO: Archived {pdf_path.name} -> {dest}")
        total_pages += len(pages)

    return total_pages


def extract_receipt_data(
    client: anthropic.Anthropic,
    images: list[tuple[str, str]],
    config: Config,
    *,
    ask_date_store: bool = False,
) -> dict:
    """
    Send receipt images to Claude vision and parse the JSON response.
    For multi-image receipts, concatenate into one call.
    When ask_date_store is True (GUID filenames), also request purchase_date/store_name.
    """
    prompt = EXTRACTION_PROMPT_GUID if ask_date_store else EXTRACTION_PROMPT
    content = []
    for media_type, b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}
        })
    content.append({"type": "text", "text": prompt})

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


def is_guid_name(image_path: Path) -> bool:
    """True if the stem is a UUID (SCAN_DIR PDF conversion output)."""
    return bool(GUID_STEM_RE.match(image_path.stem))


def parse_filename(image_path: Path) -> tuple[str, str]:
    """Extract date and store from MMDDYY_StoreName.jpg format."""
    stem = image_path.stem
    parts = stem.split("_", 1)

    date_str = ""
    store    = ""

    if len(parts) == 2:
        raw_date, raw_store = parts
        # Strip collision suffix like _2 from Aldi_2
        raw_store = re.sub(r"_\d+$", "", raw_store)
        store = re.sub(r'(?<!^)(?=[A-Z])', ' ', raw_store).strip()
        try:
            date_str = datetime.strptime(raw_date, "%m%d%y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    return date_str, store


def sanitize_store_for_filename(store_name: str) -> str:
    """Convert a store name to PascalCase alphanumerics for the JPG stem."""
    parts = re.findall(r"[A-Za-z0-9]+", store_name)
    return "".join(part.capitalize() for part in parts)


def name_is_taken(name: str, dirs: list[Path], current: Path) -> bool:
    """True if name exists in any dir, excluding the file being renamed."""
    current_resolved = current.resolve()
    for directory in dirs:
        candidate = directory / name
        if candidate.exists() and candidate.resolve() != current_resolved:
            return True
    return False


def unique_receipt_path(
    directory: Path,
    date_part: str,
    store: str,
    current: Path,
    occupied_dirs: list[Path],
    *,
    require_seq: bool = False,
) -> Path:
    """
    Build a unique receipt JPG path under directory.
    Considers collisions in all occupied_dirs (typically INPUT_DIR + ARCHIVE_DIR)
    so sequencing survives files already archived mid-run.
    - require_seq=False: date_Store.jpg, then date_Store_2.jpg, …
    - require_seq=True:  date_Store_1.jpg, date_Store_2.jpg, … (always sequenced)
    """
    base = f"{date_part}_{store}"
    if require_seq:
        n = 1
        while True:
            name = f"{base}_{n}.jpg"
            if not name_is_taken(name, occupied_dirs, current):
                return directory / name
            n += 1

    name = f"{base}.jpg"
    if not name_is_taken(name, occupied_dirs, current):
        return directory / name
    n = 2
    while True:
        name = f"{base}_{n}.jpg"
        if not name_is_taken(name, occupied_dirs, current):
            return directory / name
        n += 1


def maybe_rename_from_extraction(image_path: Path, data: dict, config: Config) -> Path:
    """
    Rename when Claude returns a store_name:
    - date + store → mmddyy_StoreName.jpg (collision → _2, _3, …)
    - store only   → MMDDYY_StoreName_1.jpg, _2, … (literal MMDDYY placeholder)
    - date only / neither → leave GUID unchanged
    """
    purchase_date = str(data.get("purchase_date") or "").strip()
    store_name = str(data.get("store_name") or "").strip()

    store = sanitize_store_for_filename(store_name) if store_name else ""
    if not store:
        return image_path

    date_ok = False
    if purchase_date:
        try:
            datetime.strptime(purchase_date, "%m%d%y")
            date_ok = True
        except ValueError:
            print(f" WARN: Invalid purchase_date '{purchase_date}' for {image_path.name}; using MMDDYY placeholder")

    occupied = [config.input_dir, config.archive_dir]
    if date_ok:
        dest = unique_receipt_path(
            image_path.parent, purchase_date, store, image_path, occupied
        )
    else:
        dest = unique_receipt_path(
            image_path.parent, "MMDDYY", store, image_path, occupied, require_seq=True
        )

    if dest.resolve() == image_path.resolve():
        return image_path

    image_path.rename(dest)
    print(f" INFO: Renamed {image_path.name} -> {dest.name}")
    return dest


def unique_archive_dest(archive_dir: Path, filename: str) -> Path:
    """Avoid overwriting an existing archived file with the same name."""
    dest = archive_dir / filename
    if not dest.exists():
        return dest
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 2
    while True:
        dest = archive_dir / f"{stem}_{n}{suffix}"
        if not dest.exists():
            return dest
        n += 1


def process_receipt(client: anthropic.Anthropic, image_path: Path, csv_path: Path, config: Config) -> Path | None:
    """
    Process a single JPG: prepare > extract > optional rename > write CSV rows.
    Returns the final image path on success, None on failure.
    """
    print(f" INFO: Processing {image_path.name}")
    ask_date_store = is_guid_name(image_path)

    try:
        images = image_to_base64(image_path, config)
    except Exception as e:
        print(f" ERROR: Failed to prepare {image_path.name}: {e}")
        return None

    try:
        data = extract_receipt_data(
            client, images, config, ask_date_store=ask_date_store
        )
    except json.JSONDecodeError as e:
        print(f" ERROR: JSON parse error for {image_path.name}: {e}")
        return None
    except Exception as e:
        print(f" ERROR: API error for {image_path.name}: {e}")
        return None

    if ask_date_store:
        image_path = maybe_rename_from_extraction(image_path, data, config)
    date, store = parse_filename(image_path)

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
    return image_path


def create_run_csv(raw_csv: Path):
    """Create a fresh per-run CSV with header (never reuse an existing path)."""
    with open(raw_csv, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=RAW_CSV_FIELDS).writeheader()


def report_incomplete_receipts(csv_path: Path):
    """Print receipts in the run CSV that still need store and/or date filled in."""
    if not csv_path.exists():
        return
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return

    receipts: dict[str, dict] = {}
    for row in rows:
        key = row.get("source_file", "")
        if key and key not in receipts:
            receipts[key] = {
                "store": row.get("store", ""),
                "date":  row.get("date", ""),
            }

    incomplete = incomplete_receipts(receipts)
    if not incomplete:
        print("All receipts have store and date — CSV is ready for upload.")
        return

    print(f"Incomplete receipts ({len(incomplete)}) — fill store/date before upload_to_notion:")
    for source_file, missing in incomplete:
        print(f"  {source_file}: missing {', '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(description="Parse JPG receipts with Claude vision and extract structured data to CSV.")
    parser.add_argument("--reprocess", action="store_true", help="Ignore manifest and reprocess all JPGs")
    args = parser.parse_args()

    scan_dir_env = os.getenv("SCAN_DIR")
    config = Config(
        input_dir     = Path(os.getenv("INPUT_DIR", DEFAULT_INPUT_DIR)),
        output_dir    = Path(os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)),
        archive_dir   = Path(os.getenv("ARCHIVE_DIR", ARCHIVE_DIR)),
        scan_dir      = Path(scan_dir_env) if scan_dir_env else None,
        manifest_name = os.getenv("MANIFEST_NAME", MANIFEST_NAME),
        model         = os.getenv("MODEL", MODEL),
        max_tokens    = int(os.getenv("MAX_TOKENS", MAX_TOKENS)),
        image_max_edge= int(os.getenv("IMAGE_MAX_EDGE", IMAGE_MAX_EDGE)),
        jpeg_quality  = int(os.getenv("JPEG_QUALITY", JPEG_QUALITY)),
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = config.output_dir / config.manifest_name

    convert_scan_pdfs(config)

    if not config.input_dir.exists():
        print(f" ERROR: Input directory not found: {config.input_dir}")
        sys.exit(1)

    images = find_receipt_images(config.input_dir)
    if not images:
        print(f" ERROR: No JPGs found in {config.input_dir}")
        sys.exit(0)

    csv_path = next_run_csv_path(config.output_dir)
    create_run_csv(csv_path)

    manifest = set() if args.reprocess else load_manifest(manifest_path)
    client   = anthropic.Anthropic()

    print(f"\n{'='*50}")
    print(f"Receipt Parser: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Scan Directory:     {config.scan_dir or '(not set)'}")
    print(f"Input Directory:    {config.input_dir}")
    print(f"Output CSV:         {csv_path}")
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

        final_path = process_receipt(client, image_path, csv_path, config)
        if final_path is not None:
            manifest.add(final_path.name)
            save_manifest(manifest_path, manifest)
            archive_dest = unique_archive_dest(config.archive_dir, final_path.name)
            shutil.move(str(final_path), archive_dest)
            success_count += 1
        else:
            fail_count += 1

    print(f"\n{'='*50}")
    print(f"Done. Processed: {success_count} | Skipped: {skip_count} | Failed: {fail_count}")
    print(f"Raw CSV: {csv_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Archived JPGs moved to: {config.archive_dir}")
    report_incomplete_receipts(csv_path)
    print(f"{'='*50}\n")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
