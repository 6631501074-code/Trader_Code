"""
web/scanner.py — Auto-Scan Scheduler (APScheduler)
====================================================
สแกนตลาดอัตโนมัติทุก M15 และ Broadcast ผลผ่าน WebSocket
"""
from __future__ import annotations

import asyncio
import logging
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import get_settings
from core.event_bus import TradingEvent
from core.orchestrator import TradingOrchestrator

logger = logging.getLogger(__name__)
settings = get_settings()

WATCH_SYMBOLS = ["GOLD", "EURUSD", "GBPUSD", "USDJPY"]
SCAN_INTERVAL_MINUTES = 15  # สแกนทุก M15


class AutoScanner:
    """
    Auto-scan ตลาดทุก SCAN_INTERVAL_MINUTES นาที
    ใช้ APScheduler (AsyncIO) ทำงานร่วมกับ FastAPI event loop
    """

    def __init__(
        self,
        orchestrator: TradingOrchestrator,
        broadcaster,           # web.ws_broadcaster.ConnectionManager
        symbols: list[str] = WATCH_SYMBOLS,
        interval_minutes: int = SCAN_INTERVAL_MINUTES,
    ) -> None:
        self._orchestrator = orchestrator
        self._broadcaster = broadcaster
        self._symbols = symbols
        self._interval = interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._scanning = False
        self._last_scan_at: dict[str, str] = {}
        self._scan_count = 0

    def start(self) -> None:
        """เริ่ม APScheduler"""
        self._scheduler.add_job(
            self._scan_all_symbols,
            CronTrigger(minute=f"*/{self._interval}"),
            id="auto_scan",
            replace_existing=True,
            misfire_grace_time=60,
        )
        self._scheduler.start()
        logger.info(f"[Scanner] Auto-scan every {self._interval} min | Symbols: {self._symbols}")

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def force_scan(self, symbol: str) -> dict:
        """Force scan ทันที (เรียกจาก API endpoint)"""
        return await self._scan_symbol(symbol)

    async def _scan_all_symbols(self) -> None:
        """สแกนทุก symbol พร้อมกัน (ทุก interval)"""
        if self._scanning:
            logger.warning("[Scanner] Previous scan still running — skipping")
            return

        self._scanning = True
        self._scan_count += 1
        scan_num = self._scan_count
        logger.info(f"[Scanner] Auto-scan #{scan_num} started | {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

        await self._broadcaster.broadcast("scan_started", {
            "scan_num": scan_num,
            "symbols": self._symbols,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        try:
            # สแกนทีละ symbol เพื่อไม่ให้ Ollama (local LLM) overload
            results = []
            for s in self._symbols:
                res = await self._scan_symbol(s)
                results.append(res)
            logger.info(f"[Scanner] Auto-scan #{scan_num} done | {len(results)} symbols")
        except Exception as e:
            logger.error(f"[Scanner] Scan error: {e}")
        finally:
            self._scanning = False

        await self._broadcaster.broadcast("scan_completed", {
            "scan_num": scan_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def _scan_symbol(self, symbol: str) -> dict:
        """
        สแกน 1 symbol ผ่าน Orchestrator และ Broadcast ผลแต่ละขั้น
        """
        try:
            # แจ้ง browser ว่ากำลังเริ่มสแกน symbol นี้
            await self._broadcaster.broadcast("symbol_scan_start", {
                "symbol": symbol,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            event = TradingEvent(
                event_type="BAR_CLOSE",
                symbol=symbol,
                data={"reason": f"auto_scan_M{self._interval}"},
            )

            final_state = await self._orchestrator.handle_event(event)

            # แปลงผลสรุปส่ง WebSocket
            result = _state_to_summary(symbol, final_state)
            self._last_scan_at[symbol] = datetime.now(timezone.utc).isoformat()

            await self._broadcaster.broadcast("symbol_result", result)
            return result

        except Exception as e:
            logger.error(f"[Scanner] {symbol} scan error: {e}")
            err_result = {
                "symbol": symbol,
                "action": "ERROR",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await self._broadcaster.broadcast("symbol_result", err_result)
            return err_result

    @property
    def status(self) -> dict:
        return {
            "running": self._scheduler.running,
            "scanning": self._scanning,
            "scan_count": self._scan_count,
            "interval_minutes": self._interval,
            "last_scan_at": self._last_scan_at,
            "symbols": self._symbols,
        }


def _state_to_summary(symbol: str, state: dict) -> dict:
    """แปลง TradingState → JSON สำหรับส่ง WebSocket"""
    ceo = state.get("ceo_decision", {})
    snap = state.get("market_snapshot", {})
    validation = state.get("validation_result", {})
    trade_params = state.get("trade_params")
    reports = state.get("department_reports", {})

    # สรุป dept reports
    dept_summary = {}
    for name, rep in reports.items():
        dept_summary[name] = {
            "recommendation": rep.get("recommendation", "HOLD"),
            "confidence": rep.get("confidence", 0),
            "reasoning": rep.get("reasoning", "")[:200],
            "key_points": rep.get("key_points", [])[:3],
        }

    action = state.get("final_action", "HOLD")

    result = {
        "symbol": symbol,
        "action": action,
        "confidence": ceo.get("confidence", 0),
        "consensus": ceo.get("consensus_score", 0),
        "reasoning": ceo.get("reasoning", "")[:400],
        "validation_status": validation.get("status", ""),
        "price": {
            "bid": snap.get("bid"),
            "ask": snap.get("ask"),
            "spread": snap.get("spread"),
            "atr": snap.get("atr_14"),
            "session": snap.get("session"),
        },
        "departments": dept_summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": state.get("trace_id", ""),
    }

    if trade_params and action in ("BUY", "SELL"):
        result["trade"] = {
            "lot_size": trade_params.get("lot_size"),
            "entry_price": ceo.get("entry_price"),
            "sl_price": trade_params.get("sl_price"),
            "tp_price": trade_params.get("tp_price"),
            "risk_pct": trade_params.get("risk_pct", 0),
            "risk_amount": trade_params.get("risk_amount", 0),
        }
    else:
        result["trade"] = None

    return result
