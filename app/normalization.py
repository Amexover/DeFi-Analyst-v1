"""Finite numbers and explicit UTC daily windows for upstream data."""
from datetime import datetime, timedelta, timezone
import math


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def date_from_epoch(value):
    if isinstance(value, bool):
        return None
    try:
        stamp = float(value)
        if not math.isfinite(stamp):
            return None
        return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def daily_window(days, include_current_day=False, *, today=None):
    today = today or datetime.now(timezone.utc).date()
    end = today if include_current_day else today - timedelta(days=1)
    return {
        "start": (end - timedelta(days=days - 1)).isoformat(),
        "end": end.isoformat(),
        "timezone": "UTC",
        "includes_partial_day": include_current_day,
    }


def normalize_daily(rows, kind, days=None, include_current_day=False, *, today=None):
    """Keep the latest timestamp per UTC date, with last input winning ties.

    Duplicate observations are replacement snapshots, never additive flows.
    Missing days are reported and never interpolated or filled with zero.
    """
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError(f"{kind} history must be a list")
    today = today or datetime.now(timezone.utc).date()
    window = daily_window(days, include_current_day, today=today) if days else None
    end = window["end"] if window else today.isoformat()
    selected, invalid, future, partial, duplicates = {}, 0, 0, 0, 0
    for row in rows:
        if kind == "tvl":
            stamp = row.get("date") if isinstance(row, dict) else None
            value = number(row.get("totalLiquidityUSD")) if isinstance(row, dict) else None
        else:
            stamp = row[0] if isinstance(row, list) and len(row) >= 2 else None
            value = number(row[1]) if isinstance(row, list) and len(row) >= 2 else None
        day = date_from_epoch(stamp)
        if day is None or value is None:
            invalid += 1
            continue
        if day > today.isoformat():
            future += 1
            continue
        if day == today.isoformat() and window and not include_current_day:
            partial += 1
            continue
        if day > end or (window and day < window["start"]):
            continue
        timestamp = float(stamp)
        if day in selected:
            duplicates += 1
        if day not in selected or timestamp >= selected[day][0]:
            selected[day] = (timestamp, value)
    field = "tvl_usd" if kind == "tvl" else "value_usd"
    points = [{"date": day, field: selected[day][1]} for day in sorted(selected)]
    warnings = []
    if duplicates:
        warnings.append(f"{duplicates} duplicate UTC-day observations replaced; values were not summed")
    if invalid:
        warnings.append(f"{invalid} invalid or non-finite observations skipped")
    if future:
        warnings.append(f"{future} future-dated observations skipped")
    if partial:
        warnings.append("Current UTC day excluded because it is incomplete")
    missing = []
    if window:
        start = datetime.fromisoformat(window["start"]).date()
        missing = [(start + timedelta(days=i)).isoformat() for i in range(days)
                   if (start + timedelta(days=i)).isoformat() not in selected]
        if missing:
            warnings.append(f"{len(missing)} UTC days have no valid observation; no zero filling applied")
    coverage = {
        "observations": len(points), "missing_days": missing,
        "duplicate_observations": duplicates, "invalid_observations": invalid,
    }
    return points, window, coverage, warnings
