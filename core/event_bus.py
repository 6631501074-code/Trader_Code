"""
core/event_bus.py — Event-Driven Trigger System
ระบบ Event ที่ปลุกระบบเมื่อเกิดเหตุการณ์สำคัญ (ไม่ใช่ Timer-based)
รองรับ: ราคาเข้าโซน, Session Open, Bar Close, ข่าวสำคัญ
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ============================================================
# Event Data Class
# ============================================================

@dataclass
class TradingEvent:
    event_type: str
    symbol: str
    data: dict[str, Any] = field(default_factory=dict)
    triggered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def idempotency_key(self) -> str:
        """
        SHA-256 hash ของ event_type + symbol + ชั่วโมง
        ป้องกัน event เดิมถูกประมวลผลซ้ำในชั่วโมงเดียวกัน
        """
        hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        raw = f"{self.event_type}:{self.symbol}:{hour_bucket}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "symbol": self.symbol,
            "data": self.data,
            "triggered_at": self.triggered_at,
            "idempotency_key": self.idempotency_key,
        }


# ============================================================
# EventBus — Async Pub/Sub
# ============================================================

HandlerType = Callable[[TradingEvent], Coroutine[Any, Any, None]]


class EventBus:
    """
    Async event bus เชื่อม MT5 price callbacks → Orchestrator
    
    Usage:
        bus = EventBus()
        bus.subscribe("PRICE_ZONE_ENTRY", orchestrator.handle_event)
        await bus.publish(TradingEvent("PRICE_ZONE_ENTRY", "XAUUSD"))
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[HandlerType]] = {}
        self._queue: asyncio.Queue[TradingEvent] = asyncio.Queue(maxsize=100)
        self._running = False

    def subscribe(self, event_type: str, handler: HandlerType) -> None:
        """ลงทะเบียน handler สำหรับ event_type"""
        self._handlers.setdefault(event_type, []).append(handler)
        logger.debug(f"[EventBus] Subscribed {handler.__qualname__} → {event_type}")

    async def publish(self, event: TradingEvent) -> None:
        """เพิ่ม event เข้า queue (non-blocking)"""
        try:
            await self._queue.put(event)
            logger.info(
                f"[EventBus] Published {event.event_type} "
                f"symbol={event.symbol} key={event.idempotency_key}"
            )
        except asyncio.QueueFull:
            logger.error("[EventBus] Queue full — dropping event!")

    async def start(self) -> None:
        """เริ่ม event loop — consume queue และเรียก handlers"""
        self._running = True
        logger.info("[EventBus] Started")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue  # ไม่มี event — รอต่อ
            except Exception as exc:
                logger.exception(f"[EventBus] Dispatch error: {exc}")

    async def _dispatch(self, event: TradingEvent) -> None:
        """ส่ง event ไปยัง handler ที่ลงทะเบียน"""
        handlers = self._handlers.get(event.event_type, [])
        handlers += self._handlers.get("*", [])  # wildcard handlers

        if not handlers:
            logger.warning(f"[EventBus] No handler for {event.event_type}")
            return

        tasks = [asyncio.create_task(h(event)) for h in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"[EventBus] Handler error: {r}")

    def stop(self) -> None:
        self._running = False
        logger.info("[EventBus] Stopped")


# ============================================================
# MT5 Price Monitor — สร้าง Events จากการเปลี่ยนแปลงราคา
# ============================================================

class MT5PriceMonitor:
    """
    Monitor ราคา MT5 และสร้าง Event เมื่อเงื่อนไขตรง
    ทำงานเป็น background task ใน asyncio
    """

    def __init__(self, bus: EventBus, symbols: list[str], key_levels: dict[str, list[float]]) -> None:
        self._bus = bus
        self._symbols = symbols
        self._key_levels = key_levels   # {"XAUUSD": [1900.0, 1950.0, ...]}
        self._last_prices: dict[str, float] = {}
        self._running = False
        self._check_interval = 0.5      # ตรวจทุก 0.5 วินาที

    async def start(self) -> None:
        """เริ่ม monitor loop"""
        import MetaTrader5 as mt5
        self._running = True
        logger.info(f"[MT5Monitor] Watching: {self._symbols}")

        while self._running:
            for symbol in self._symbols:
                try:
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        continue

                    current_price = tick.bid
                    last_price = self._last_prices.get(symbol)

                    # ตรวจว่าราคาเข้าโซน Key Level
                    if last_price is not None:
                        await self._check_zone_entry(symbol, last_price, current_price)

                    self._last_prices[symbol] = current_price

                except Exception as exc:
                    logger.error(f"[MT5Monitor] Error checking {symbol}: {exc}")

            await asyncio.sleep(self._check_interval)

    async def _check_zone_entry(
        self, symbol: str, prev_price: float, curr_price: float
    ) -> None:
        """ตรวจว่าราคาผ่าน Key Level → publish event"""
        zone_buffer_pips = 5.0
        levels = self._key_levels.get(symbol, [])

        for level in levels:
            # buffer zone รอบ key level
            zone_low = level - zone_buffer_pips * 0.0001
            zone_high = level + zone_buffer_pips * 0.0001

            if not (zone_low <= prev_price <= zone_high) and (zone_low <= curr_price <= zone_high):
                event = TradingEvent(
                    event_type="PRICE_ZONE_ENTRY",
                    symbol=symbol,
                    data={"key_level": level, "current_price": curr_price},
                )
                await self._bus.publish(event)
                logger.info(f"[MT5Monitor] {symbol} entered zone @ {level}")

    def stop(self) -> None:
        self._running = False


# ============================================================
# Session Monitor — Event เมื่อเซสชันเปิด
# ============================================================

class SessionMonitor:
    """สร้าง SESSION_OPEN event เมื่อเวลาตรงกับเซสชันหลัก (UTC)"""

    SESSION_OPENS_UTC = {
        "london":   {"hour": 8,  "minute": 0},
        "newyork":  {"hour": 13, "minute": 30},
        "asia":     {"hour": 0,  "minute": 0},
    }

    def __init__(self, bus: EventBus, symbols: list[str]) -> None:
        self._bus = bus
        self._symbols = symbols
        self._fired_today: set[str] = set()

    async def start(self) -> None:
        while True:
            now_utc = datetime.now(timezone.utc)
            today = now_utc.strftime("%Y%m%d")

            for session_name, t in self.SESSION_OPENS_UTC.items():
                key = f"{today}:{session_name}"
                if key in self._fired_today:
                    continue

                if now_utc.hour == t["hour"] and now_utc.minute == t["minute"]:
                    for sym in self._symbols:
                        event = TradingEvent(
                            event_type="SESSION_OPEN",
                            symbol=sym,
                            data={"session": session_name},
                        )
                        await self._bus.publish(event)
                    self._fired_today.add(key)
                    logger.info(f"[SessionMonitor] {session_name} session opened")

            # ล้าง cache ทุกเที่ยงคืน UTC
            if now_utc.hour == 0 and now_utc.minute == 0:
                self._fired_today.clear()

            await asyncio.sleep(30)  # ตรวจทุก 30 วิ
