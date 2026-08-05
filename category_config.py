"""Load grocery categories from categories.json (override with CATEGORIES_FILE)."""

import json
import os
from pathlib import Path

DEFAULT_CATEGORIES_FILE = Path(__file__).resolve().parent / "categories.json"


def load_categories(path: Path | None = None) -> list[str]:
    categories_path = path or Path(os.getenv("CATEGORIES_FILE", str(DEFAULT_CATEGORIES_FILE)))
    if not categories_path.exists():
        raise FileNotFoundError(f"Categories file not found: {categories_path}")

    data = json.loads(categories_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(c, str) and c.strip() for c in data):
        raise ValueError(f"Categories file must be a JSON array of non-empty strings: {categories_path}")

    return [c.strip() for c in data]


def grocery_category_map(categories: list[str] | None = None) -> dict[str, str]:
    """Identity map for known categories, plus Other as Notion fallback."""
    cats = categories if categories is not None else load_categories()
    return {name: name for name in cats} | {"Other": "Other"}


def format_categories_for_prompt(categories: list[str] | None = None) -> str:
    cats = categories if categories is not None else load_categories()
    # Wrap for readability in the vision prompt (≈4 per line)
    lines = []
    for i in range(0, len(cats), 4):
        chunk = ", ".join(f'"{c}"' for c in cats[i : i + 4])
        lines.append(f"    {chunk}")
    return "\n".join(lines)
