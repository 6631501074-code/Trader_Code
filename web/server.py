"""
web/server.py — FastAPI Web Server + WebSocket Hub
===================================================
เปิดใช้งานด้วย: python web/server.py
จากนั้นเปิด Browser ที่: http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# เพิ่ม project root ใน sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from config import get_settings
from core.event_bus import TradingEvent
from core.orchestrator import TradingOrchestrator
from data.market_data import MarketDataFetcher
from logging_.audit_logger import AuditLogger, create_db_pool
from safety.idempotency import IdempotencyRegistry
from safety.lot_calculator import LotCalculator
from safety.safety_layer import SafetyLayer
from agents.ceo_agent import CEOAgent
from web.ws_broadcaster import manager
from web.news_fetcher import get_cached_events
from web.scanner import AutoScanner

logger = logging.getLogger(__name__)
settings = get_settings()

WATCH_SYMBOLS = ["GOLD", "EURUSD", "GBPUSD", "USDJPY"]
TIMEFRAME = "H1"

# --- ตัวแปร Global ---
_orchestrator: TradingOrchestrator | None = None
_scanner: AutoScanner | None = None
_news_cache: dict = {}
_latest_results: dict[str, dict] = {}   # symbol → latest scan result
_live_prices: dict[str, dict] = {}      # symbol → {bid, ask, spread}
_price_monitor_task = None


# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(title="AI Trading Dashboard", version="1.0.0")

# Static files
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.on_event("startup")
async def startup_event():
    """เริ่มต้นระบบทั้งหมดเมื่อ server start"""
    global _orchestrator, _scanner, _price_monitor_task

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s:%(lineno)d - %(message)s",
    )

    logger.info("[Server] Starting AI Trading Dashboard...")

    # สร้าง dependencies
    try:
        db_pool = await create_db_pool(settings.database_url)
    except Exception as e:
        logger.warning(f"[Server] DB connection failed (using memory only): {e}")
        db_pool = None

    fetcher = MarketDataFetcher()
    fetcher.initialize(simulate=False)

    audit_logger = AuditLogger(db_pool)
    await audit_logger.initialize_schema()

    idempotency = IdempotencyRegistry(db_pool)
    lot_calculator = LotCalculator(min_lot=settings.min_lot, max_lot=settings.max_lot)
    safety_layer = SafetyLayer(idempotency, lot_calculator)
    ceo_agent = CEOAgent()

    _orchestrator = TradingOrchestrator(
        market_fetcher=fetcher,
        safety_layer=safety_layer,
        ceo_agent=ceo_agent,
        audit_logger=audit_logger,
        symbols=WATCH_SYMBOLS,
        timeframe=TIMEFRAME,
    )
    await _orchestrator.initialize(simulate=False)

    # Auto-scanner (M15)
    _scanner = AutoScanner(
        orchestrator=_orchestrator,
        broadcaster=manager,
        symbols=WATCH_SYMBOLS,
        interval_minutes=15,
    )
    _scanner.start()

    # Live price monitor (รันในพื้นหลัง)
    _price_monitor_task = asyncio.create_task(_live_price_loop(fetcher))

    logger.info("[Server] Ready at http://localhost:8000")


@app.on_event("shutdown")
async def shutdown_event():
    if _scanner:
        _scanner.stop()
    if _price_monitor_task:
        _price_monitor_task.cancel()
    logger.info("[Server] Shutdown complete")


# ============================================================
# Live Price Monitor (background task)
# ============================================================

async def _live_price_loop(fetcher: MarketDataFetcher) -> None:
    """ดึงราคา Bid/Ask ทุก 2 วินาที และ broadcast ไปยัง clients"""
    while True:
        try:
            for symbol in WATCH_SYMBOLS:
                try:
                    snap = fetcher.get_snapshot(symbol, TIMEFRAME)
                    price_data = {
                        "symbol": symbol,
                        "bid": snap.get("bid"),
                        "ask": snap.get("ask"),
                        "spread": snap.get("spread"),
                        "session": snap.get("session"),
                        "atr": snap.get("atr_14"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    _live_prices[symbol] = price_data
                except Exception:
                    pass

            if manager.connection_count > 0 and _live_prices:
                await manager.broadcast("prices", _live_prices)

        except Exception as e:
            logger.debug(f"[PriceLoop] {e}")

        await asyncio.sleep(2)


# ============================================================
# REST API Endpoints
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve main dashboard HTML"""
    html_file = _static_dir / "index.html"
    if html_file.exists():
        return FileResponse(str(html_file))
    return HTMLResponse("<h1>Loading...</h1>")


@app.get("/api/status")
async def api_status():
    """Server status and scanner info"""
    return {
        "status": "running",
        "scanner": _scanner.status if _scanner else {},
        "ws_connections": manager.connection_count,
        "symbols": WATCH_SYMBOLS,
        "execution_mode": settings.execution_mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/prices")
async def api_prices():
    """Current prices for all symbols"""
    return _live_prices


@app.get("/api/results")
async def api_results():
    """Latest scan results for all symbols"""
    return _latest_results


@app.get("/api/news")
async def api_news():
    """Economic calendar events"""
    events = await get_cached_events(_news_cache)
    return {"events": events, "count": len(events)}


@app.post("/api/scan/{symbol}")
async def api_force_scan(symbol: str):
    """Force scan a specific symbol immediately"""
    symbol = symbol.upper()
    if symbol not in WATCH_SYMBOLS:
        return {"error": f"Unknown symbol: {symbol}. Valid: {WATCH_SYMBOLS}"}

    if not _scanner:
        return {"error": "Scanner not initialized"}

    logger.info(f"[API] Force scan requested for {symbol}")

    # Broadcast "force scan started" เพื่อให้ browser รู้ก่อน
    await manager.broadcast("force_scan_started", {
        "symbol": symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # รัน scan (await = รอผลก่อน return)
    result = await _scanner.force_scan(symbol)
    _latest_results[symbol] = result
    return result


@app.get("/api/accuracy")
async def api_accuracy():
    """Paper trading accuracy stats"""
    try:
        from analytics.performance_tracker import calculate_metrics
        from data.paper_trades_store import load_paper_trades
        trades = load_paper_trades()
        metrics = calculate_metrics(trades)
        return {"metrics": metrics, "trade_count": len(trades)}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# WebSocket Endpoint
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket connection for real-time updates"""
    await manager.connect(ws)
    try:
        # ส่งข้อมูลเริ่มต้นให้ client ใหม่
        await manager.send_to(ws, "init", {
            "symbols": WATCH_SYMBOLS,
            "execution_mode": settings.execution_mode,
            "latest_results": _latest_results,
            "prices": _live_prices,
            "scanner": _scanner.status if _scanner else {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # ส่งข่าวด้วย
        events = await get_cached_events(_news_cache)
        await manager.send_to(ws, "news", {"events": events})

        # รับ messages จาก client (เช่น force scan request)
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("action") == "force_scan":
                    symbol = msg.get("symbol", "GOLD").upper()
                    await manager.broadcast("force_scan_started", {"symbol": symbol})
                    result = await _scanner.force_scan(symbol)
                    _latest_results[symbol] = result
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        logger.error(f"[WS] Error: {e}")
        await manager.disconnect(ws)


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "web.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
