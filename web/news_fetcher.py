"""
web/news_fetcher.py — Economic Calendar Fetcher
================================================
ดึง Economic Calendar events จาก ForexFactory (public JSON)
แสดง NFP, CPI, FOMC และ events ที่กระทบ USD/GOLD
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ForexFactory calendar API (public, no auth needed)
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Impact levels ที่สนใจ
HIGH_IMPACT_CURRENCIES = {"USD", "XAU"}


def _parse_ff_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse ForexFactory date+time → UTC datetime"""
    try:
        # ForexFactory ใช้ US Eastern Time
        import pytz
        eastern = pytz.timezone("America/New_York")
        dt_str = f"{date_str} {time_str}"
        # หลาย format ที่อาจเกิดขึ้น
        for fmt in ("%m-%d-%Y %I:%M%p", "%m-%d-%Y %I:%M am", "%m-%d-%Y %I:%M pm"):
            try:
                dt_local = datetime.strptime(dt_str, fmt)
                dt_local = eastern.localize(dt_local)
                return dt_local.astimezone(timezone.utc)
            except ValueError:
                continue
        return None
    except Exception:
        return None


async def fetch_calendar_events() -> list[dict]:
    """
    ดึง Economic Calendar จาก ForexFactory
    Returns list ของ events เรียงตามเวลา
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(FF_URL)
            resp.raise_for_status()
            raw_events = resp.json()
    except Exception as e:
        logger.warning(f"[News] ForexFactory fetch failed: {e}")
        return _get_fallback_events()

    now_utc = datetime.now(timezone.utc)
    events: list[dict] = []

    for ev in raw_events:
        currency = ev.get("currency", "").upper()
        impact = ev.get("impact", "").lower()
        title = ev.get("title", "")

        # กรองเฉพาะ USD / High-impact events
        if currency not in ("USD", "EUR", "GBP", "JPY", "XAU") and impact != "high":
            continue

        # Parse time
        ev_date = ev.get("date", "")
        ev_time = ev.get("time", "")
        ev_dt = None
        if ev_date and ev_time and ev_time.lower() not in ("all day", "tentative", ""):
            ev_dt = _parse_ff_datetime(ev_date, ev_time)

        # คำนวณนับถอยหลัง
        seconds_until = None
        is_past = False
        if ev_dt:
            diff = (ev_dt - now_utc).total_seconds()
            seconds_until = int(diff)
            is_past = diff < 0

        events.append({
            "title": title,
            "currency": currency,
            "impact": impact,
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
            "actual": ev.get("actual", ""),
            "time_utc": ev_dt.isoformat() if ev_dt else None,
            "seconds_until": seconds_until,
            "is_past": is_past,
        })

    # Sort by time (upcoming first, then past)
    upcoming = sorted(
        [e for e in events if not e["is_past"] and e["time_utc"]],
        key=lambda x: x["seconds_until"]
    )
    past = sorted(
        [e for e in events if e["is_past"] and e["time_utc"]],
        key=lambda x: x["seconds_until"],
        reverse=True
    )
    no_time = [e for e in events if not e["time_utc"]]

    return upcoming[:10] + no_time[:3] + past[:5]


def _get_fallback_events() -> list[dict]:
    """Fallback ถ้า ForexFactory ไม่ตอบสนอง"""
    return [
        {
            "title": "ไม่สามารถดึงข้อมูล Calendar ได้",
            "currency": "USD",
            "impact": "low",
            "forecast": "",
            "previous": "",
            "actual": "",
            "time_utc": None,
            "seconds_until": None,
            "is_past": False,
        }
    ]


async def get_cached_events(cache: dict) -> list[dict]:
    """
    Return cached events (refresh ทุก 5 นาที)
    cache = shared dict {"events": [], "fetched_at": datetime}
    """
    now = datetime.now(timezone.utc)
    last_fetch = cache.get("fetched_at")
    if last_fetch is None or (now - last_fetch).total_seconds() > 300:
        events = await fetch_calendar_events()
        cache["events"] = events
        cache["fetched_at"] = now
        logger.info(f"[News] Refreshed calendar: {len(events)} events")
    return cache.get("events", [])
