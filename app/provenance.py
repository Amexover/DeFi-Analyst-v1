"""Request-scoped provenance; cache hits retain the original fetch timestamp."""
from contextvars import ContextVar
from datetime import datetime, timezone

fetch_records = ContextVar("fetch_records", default=None)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def record_fetch(record):
    records = fetch_records.get()
    if records is not None:
        records.append(record)


def metadata(sources=None, warnings=None):
    records = list(fetch_records.get() or [])
    served = now()
    return {
        "retrieved_at": min((r["retrieved_at"] for r in records), default=served),
        "served_at": served,
        "sources": sources or [],
        "source_fetches": records,
        "warnings": list(dict.fromkeys(warnings or [])),
    }
