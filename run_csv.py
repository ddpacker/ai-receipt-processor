"""Per-run dated CSV naming: MMDDYY_receipts_raw.csv (+ _N on collision)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# 080526_receipts_raw.csv  or  080526_receipts_raw_2.csv
RUN_CSV_PATTERN = re.compile(r"^(\d{6})_receipts_raw(?:_(\d+))?\.csv$", re.IGNORECASE)


def run_date_stamp(when: datetime | None = None) -> str:
    """Local calendar date as MMDDYY (run day, not receipt dates)."""
    return (when or datetime.now()).strftime("%m%d%y")


def parse_run_csv_key(path: Path) -> tuple[datetime, int] | None:
    """
    Return (run_date, sequence) for sorting. Base file (no suffix) is sequence 1.
    """
    match = RUN_CSV_PATTERN.match(path.name)
    if not match:
        return None
    stamp, seq = match.group(1), match.group(2)
    try:
        run_date = datetime.strptime(stamp, "%m%d%y")
    except ValueError:
        return None
    return run_date, int(seq) if seq else 1


def next_run_csv_path(output_dir: Path, when: datetime | None = None) -> Path:
    """
    First unused path for this run day: MMDDYY_receipts_raw.csv,
    then MMDDYY_receipts_raw_2.csv, etc. Never overwrites.
    """
    stamp = run_date_stamp(when)
    candidate = output_dir / f"{stamp}_receipts_raw.csv"
    if not candidate.exists():
        return candidate

    n = 2
    while True:
        candidate = output_dir / f"{stamp}_receipts_raw_{n}.csv"
        if not candidate.exists():
            return candidate
        n += 1


def find_latest_run_csv(output_dir: Path) -> Path | None:
    """Newest dated run CSV in output_dir by (date stamp, sequence)."""
    if not output_dir.exists():
        return None

    ranked: list[tuple[tuple[str, int], Path]] = []
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        key = parse_run_csv_key(path)
        if key is not None:
            ranked.append((key, path))

    if not ranked:
        return None

    ranked.sort(key=lambda item: item[0])
    return ranked[-1][1]
