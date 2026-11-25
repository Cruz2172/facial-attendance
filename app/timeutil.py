"""Timezone helpers.

Rule used across the app:
  * Timestamps are STORED in UTC (naive datetimes from ``utcnow()``).
  * "Today", session dates and times shown to people use the configured local
    timezone (APP_TIMEZONE, default Africa/Lagos).

This module has no dependency on Flask or the database.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_tz_cache = {}


def get_tz(name=None):
    name = name or os.environ.get('APP_TIMEZONE', 'Africa/Lagos')
    if name in _tz_cache:
        return _tz_cache[name]
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(name)
        except Exception:  # missing tzdata or bad name
            logger.warning("Timezone %r not available (install 'tzdata'); falling back to UTC.", name)
    if tz is None:
        tz = timezone.utc
    _tz_cache[name] = tz
    return tz


def utcnow():
    """Naive UTC 'now' (what the database columns store)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_local(tz_name=None):
    """Naive datetime in the app's local timezone."""
    return datetime.now(get_tz(tz_name)).replace(tzinfo=None)


def today_local(tz_name=None):
    return now_local(tz_name).date()


def to_local(dt, tz_name=None):
    """Convert a naive-UTC datetime (as stored) to a naive local datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz(tz_name)).replace(tzinfo=None)


def format_local(dt, fmt='%Y-%m-%d %H:%M', tz_name=None):
    local = to_local(dt, tz_name)
    return local.strftime(fmt) if local else ''
