"""data/staleness_checker.py — Data Freshness Validator"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)


def check_staleness(fetched_at_iso: str, max_age_seconds: int = 30) -> tuple[bool, float]:
    """
    ตรวจอายุข้อมูล
    Returns: (is_stale: bool, age_seconds: float)
    """
    try:
        fetched = datetime.fromisoformat(fetched_at_iso)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        return age > max_age_seconds, age
    except (ValueError, TypeError):
        return True, float("inf")
