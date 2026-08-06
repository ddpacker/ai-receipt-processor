"""Receipt store/date completeness checks shared by parse and upload."""

from datetime import datetime


def missing_receipt_fields(store: str, date: str) -> list[str]:
    """Return which required fields are missing: 'store' and/or 'date'."""
    missing: list[str] = []
    store = (store or "").strip()
    date = (date or "").strip()

    if not store or store.upper() == "UNKNOWN":
        missing.append("store")

    if not date:
        missing.append("date")
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            missing.append("date")

    return missing


def receipt_is_complete(store: str, date: str) -> bool:
    return not missing_receipt_fields(store, date)


def incomplete_receipts(receipts: dict[str, dict]) -> list[tuple[str, list[str]]]:
    """
    Return [(source_file, missing_fields), ...] for receipts lacking store
    and/or a YYYY-MM-DD date. Sorted by source_file.
    """
    result: list[tuple[str, list[str]]] = []
    for source_file, data in sorted(receipts.items()):
        missing = missing_receipt_fields(data.get("store", ""), data.get("date", ""))
        if missing:
            result.append((source_file, missing))
    return result
