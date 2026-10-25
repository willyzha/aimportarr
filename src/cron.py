"""
Cron parsing, validation, and schedule evaluation utilities for AImportarr.
Provides high-performance cron calculations with an optional croniter dependency,
featuring a robust zero-dependency pure-Python fallback for standard 5-field cron syntax.
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Optional, Set

logger = logging.getLogger("aimportarr.cron")

NICKNAMES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _parse_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """Parse a single cron expression field (*, */N, N,M, N-M, N-M/S)."""
    values: Set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            subparts = part.split("/")
            if len(subparts) != 2:
                raise ValueError(f"Invalid step format: '{part}'")
            step = int(subparts[1])
            if step <= 0:
                raise ValueError(f"Step must be positive: '{part}'")
            subrange = subparts[0]
            if subrange == "*":
                start, end = min_val, max_val
            elif "-" in subrange:
                start, end = map(int, subrange.split("-"))
            else:
                start, end = int(subrange), max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = map(int, part.split("-"))
            if start > end:
                raise ValueError(f"Range start > end: '{part}'")
            values.update(range(start, end + 1))
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        else:
            val = int(part)
            if val < min_val or val > max_val:
                raise ValueError(f"Value {val} outside bounds [{min_val}, {max_val}]")
            values.add(val)
    return values


def is_valid_cron(expr: str) -> bool:
    """Validate whether a string is a valid 5-field cron expression or recognized nickname."""
    if not expr or not isinstance(expr, str):
        return False
    raw = NICKNAMES.get(expr.strip().lower(), expr.strip())
    fields = raw.split()
    if len(fields) != 5:
        return False
    try:
        _parse_field(fields[0], 0, 59)
        _parse_field(fields[1], 0, 23)
        _parse_field(fields[2], 1, 31)
        _parse_field(fields[3], 1, 12)
        _parse_field(fields[4], 0, 7)
        return True
    except Exception:
        return False


def normalize_cron_expr(expr: str, fallback: str = "0 2 * * *") -> str:
    """Normalize and validate a cron expression string, returning `fallback` if invalid."""
    if not expr or not isinstance(expr, str):
        return fallback
    clean = expr.strip()
    if clean.lower() in NICKNAMES:
        return clean.lower()
    return clean if is_valid_cron(clean) else fallback


def interval_hours_to_cron(hours: int) -> str:
    """Convert a legacy interval in hours to an equivalent standard cron expression."""
    h = max(1, int(hours))
    if 24 % h == 0:
        if h == 24:
            return "0 2 * * *"  # Run once daily at 2:00 AM
        return f"0 */{h} * * *"
    elif h < 24:
        return f"0 */{h} * * *"
    else:
        days = max(1, h // 24)
        return f"0 2 */{days} * *"


def get_next_cron_run(expr: str, now: Optional[datetime] = None) -> datetime:
    """
    Calculate the next execution datetime matching `expr` after `now`.
    Uses croniter if available, otherwise falls back to the pure-Python engine.
    Always returns an explicit UTC datetime.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. Try croniter if installed
    try:
        import croniter  # type: ignore
        normalized = NICKNAMES.get(expr.strip().lower(), expr.strip())
        itr = croniter.croniter(normalized, now)
        next_dt = itr.get_next(datetime)
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        return next_dt
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"croniter failed for '{expr}', falling back to internal parser: {e}")

    # 2. Pure-Python 5-field parser fallback
    raw = NICKNAMES.get(expr.strip().lower(), expr.strip())
    fields = raw.split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: '{expr}' (expected 5 fields)")

    valid_minutes = _parse_field(fields[0], 0, 59)
    valid_hours = _parse_field(fields[1], 0, 23)
    valid_dom = _parse_field(fields[2], 1, 31)
    valid_months = _parse_field(fields[3], 1, 12)
    raw_dow = _parse_field(fields[4], 0, 7)
    valid_dow = {0 if d == 7 else d for d in raw_dow}

    curr = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    max_steps = 525600 * 5  # Scan up to 5 years into the future

    while max_steps > 0:
        if curr.month not in valid_months:
            if curr.month == 12:
                curr = curr.replace(year=curr.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                curr = curr.replace(month=curr.month + 1, day=1, hour=0, minute=0)
            continue

        cron_weekday = (curr.weekday() + 1) % 7
        dom_restricted = (fields[2] != "*")
        dow_restricted = (fields[4] != "*")

        if dom_restricted and dow_restricted:
            day_match = (curr.day in valid_dom) or (cron_weekday in valid_dow)
        elif dom_restricted:
            day_match = (curr.day in valid_dom)
        elif dow_restricted:
            day_match = (cron_weekday in valid_dow)
        else:
            day_match = True

        if not day_match:
            curr = (curr + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        if curr.hour not in valid_hours:
            curr = (curr + timedelta(hours=1)).replace(minute=0)
            continue

        if curr.minute not in valid_minutes:
            curr += timedelta(minutes=1)
            continue

        return curr

    raise ValueError(f"Could not compute next cron execution for '{expr}' within 5 years")
