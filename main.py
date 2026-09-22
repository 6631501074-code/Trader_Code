"""
main.py — Application Entry Point
===================================
Bootstrap ระบบ Multi-Agent Trading:
1. สร้าง dependencies (DB pool, market fetcher, safety layer, etc.)
2. เริ่ม EventBus และ MT5 Price Monitor
3. รัน Orchestrator เมื่อได้รับ TradingEvent

Run:
    python main.py                     # production mode
    python main.py --simulate          # development (no MT5)
    python main.py --once XAUUSD      # รันครั้งเดียว (test)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import structlog
from rich.logging import RichHandler

from agents.ceo_agent import CEOAgent
from config import get_settings
from core.event_bus import EventBus, MT5PriceMonitor, SessionMonitor, TradingEvent
from core.orchestrator import TradingOrchestrator
from data.market_data import MarketDataFetcher
from logging_.audit_logger import AuditLogger, create_db_pool
from safety.idempotency import IdempotencyRegistry
from safety.lot_calculator import LotCalculator
from safety.safety_layer import SafetyLayer

# ──────────────────────────────────────────────────────────────
# Logging Setup
# ──────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO") -> None:
    # Force UTF-8 encoding on Windows (PowerShell defaults to cp1252 which breaks emoji)
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass  # Python < 3.7 fallback

    from rich.console import Console
    console = Console(
        stderr=False,
        force_terminal=True,
        highlight=False,
        width=200,
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True)],
    )

    # Quieten noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)
settings = get_settings()

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────

WATCH_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD"]  # ปรับตามต้องการ
TIMEFRAME = "H1"

# Key levels สำหรับ Price Zone Monitor (ปรับตาม analysis ของคุณ)
KEY_LEVELS = {
    "XAUUSD": [2300.0, 2320.0, 2350.0, 2380.0, 2400.0],
    "EURUSD": [1.0700, 1.0750, 1.0800, 1.0850, 1.0900],
    "GBPUSD": [1.2600, 1.2650, 1.2700, 1.2750, 1.2800],
}


# ──────────────────────────────────────────────────────────────
# Application Bootstrap
# ──────────────────────────────────────────────────────────────

async def create_dependencies():
    """สร้าง dependency tree ทั้งหมด"""
    # PostgreSQL connection pool
    db_pool = await create_db_pool(settings.database_url)

    # Core components
    market_fetcher = MarketDataFetcher()
    audit_logger   = AuditLogger(db_pool)
    idempotency    = IdempotencyRegistry(db_pool)
    lot_calculator = LotCalculator(
        min_lot=settings.min_lot,
        max_lot=settings.max_lot,
    )
    safety_layer  = SafetyLayer(idempotency, lot_calculator)
    ceo_agent     = CEOAgent()

    return {
        "db_pool":        db_pool,
        "market_fetcher": market_fetcher,
        "audit_logger":   audit_logger,
        "idempotency":    idempotency,
        "safety_layer":   safety_layer,
        "ceo_agent":      ceo_agent,
    }


async def run_app(simulate: bool = False) -> None:
    """Main application loop"""
    setup_logging(settings.log_level)
    logger.info(f"[START] Starting Multi-Agent Trading System | env={settings.environment}")
    logger.info(f"   LLM: {settings.ollama_model} @ {settings.ollama_base_url}")
    logger.info(f"   Symbols: {WATCH_SYMBOLS} | TF: {TIMEFRAME}")

    # ── Build dependencies ──
    deps = await create_dependencies()

    # ── Orchestrator ──
    orchestrator = TradingOrchestrator(
        market_fetcher=deps["market_fetcher"],
        safety_layer=deps["safety_layer"],
        ceo_agent=deps["ceo_agent"],
        audit_logger=deps["audit_logger"],
        symbols=WATCH_SYMBOLS,
        timeframe=TIMEFRAME,
    )
    await orchestrator.initialize()

    # ── Event Bus ──
    bus = EventBus()
    bus.subscribe("*", orchestrator.handle_event)  # subscribe to all events

    # ── Background monitors ──
    tasks = [asyncio.create_task(bus.start(), name="event_bus")]

    if not simulate:
        price_monitor = MT5PriceMonitor(bus, WATCH_SYMBOLS, KEY_LEVELS)
        session_monitor = SessionMonitor(bus, WATCH_SYMBOLS)
        tasks += [
            asyncio.create_task(price_monitor.start(), name="price_monitor"),
            asyncio.create_task(session_monitor.start(), name="session_monitor"),
        ]
        logger.info("[OK] MT5 monitors started (live mode)")
    else:
        logger.info("[SIM] Running in SIMULATE mode — no MT5 connection")

    # ── Graceful shutdown ──
    loop = asyncio.get_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info(f"[Shutdown] Signal {sig.name} received")
        bus.stop()
        for t in tasks:
            t.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig)
        except NotImplementedError:
            # Windows ไม่รองรับ add_signal_handler ทุกตัว
            pass

    logger.info("[OK] System ready — waiting for events...")
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("[Shutdown] Cancelled")
    finally:
        if deps["db_pool"]:
            await deps["db_pool"].close()
        logger.info("[BYE] Shutdown complete")


async def run_once(symbol: str, simulate: bool = True) -> None:
    """รันครั้งเดียวสำหรับ testing/debugging (simulate=True โดย default)"""
    setup_logging("DEBUG")
    logger.info(f"[TEST] Single-run test | symbol={symbol} simulate={simulate}")

    deps = await create_dependencies()
    orchestrator = TradingOrchestrator(
        market_fetcher=deps["market_fetcher"],
        safety_layer=deps["safety_layer"],
        ceo_agent=deps["ceo_agent"],
        audit_logger=deps["audit_logger"],
        symbols=[symbol],
        timeframe=TIMEFRAME,
    )
    await orchestrator.initialize(simulate=simulate)

    # Trigger manual event
    event = TradingEvent(
        event_type="MANUAL_TRIGGER",
        symbol=symbol,
        data={"reason": "manual test run"},
    )

    final_state = await orchestrator.handle_event(event)

    print("\n" + "="*60)
    print("WORKFLOW RESULT")
    print("="*60)
    print(f"Symbol:      {final_state.get('symbol')}")
    print(f"Trace ID:    {final_state.get('trace_id')}")
    print(f"Final Action:{final_state.get('final_action')}")
    print(f"Validation:  {final_state.get('validation_result', {}).get('status')}")
    print(f"Duration:    {final_state.get('workflow_duration_ms', 0):.0f}ms")

    ceo = final_state.get("ceo_decision", {})
    print(f"\nCEO Decision:")
    print(f"  Action:     {ceo.get('action')}")
    print(f"  Confidence: {ceo.get('confidence', 0):.2f}")
    print(f"  Consensus:  {ceo.get('consensus_score', 0):.2f}")
    print(f"  Reasoning:  {ceo.get('reasoning', '')[:200]}")

    if final_state.get("trade_params"):
        tp = final_state["trade_params"]
        print(f"\nTrade Parameters (Python-calculated):")
        print(f"  Lot Size:   {tp.get('lot_size')}")
        print(f"  SL Price:   {tp.get('sl_price')}")
        print(f"  TP Price:   {tp.get('tp_price')}")
        print(f"  Risk:       {tp.get('risk_pct', 0)*100:.2f}%")
        print(f"  Risk Amt:   ${tp.get('risk_amount', 0):.2f}")

    if deps["db_pool"]:
        await deps["db_pool"].close()


# ──────────────────────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Agent LLM Trading System")
    parser.add_argument("--simulate", action="store_true", help="Run without MT5")
    parser.add_argument("--once", metavar="SYMBOL", help="Run single test cycle")
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once(args.once, simulate=args.simulate))
    else:
        asyncio.run(run_app(simulate=args.simulate))


if __name__ == "__main__":
    main()
