"""
data/market_data.py — Market Data Fetcher (MT5/XM)
===================================================
ดึงข้อมูลตลาดจาก MT5 และสร้าง MarketSnapshot
ถ้าดึงไม่ได้ → raise exception → Safety Layer จับแล้ว HOLD
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.state import MarketSnapshot

logger = logging.getLogger(__name__)

# Session boundaries (UTC)
SESSION_MAP = {
    "asia":    (0, 8),
    "london":  (8, 13),
    "newyork": (13, 22),
    "off":     (22, 24),
}


class MarketDataFetcher:
    """
    ดึงข้อมูลตลาดจาก MT5 terminal ที่เชื่อมกับ XM
    ทุก method return None หรือ raise Exception ถ้าข้อมูลผิดพลาด
    """

    def __init__(self) -> None:
        self._initialized = False

    def initialize(self, simulate: bool = False) -> bool:
        """
        เชื่อมต่อ MT5 terminal
        ถ้า simulate=True หรือ MT5 ไม่พร้อม → ใช้ mock data แทน (ไม่ crash)
        """
        if simulate:
            logger.info("[MT5] Simulate mode — using mock data (no MT5 connection)")
            self._initialized = False
            return False

        try:
            import MetaTrader5 as mt5
            from config import get_settings
            settings = get_settings()

            if not mt5.initialize(
                path=settings.mt5_path,
                login=settings.mt5_login,
                password=settings.mt5_password,
                server=settings.mt5_server,
            ):
                error = mt5.last_error()
                logger.warning(f"[MT5] Initialize failed: {error} — falling back to mock data")
                self._initialized = False
                return False

            self._initialized = True
            info = mt5.terminal_info()
            logger.info(f"[MT5] Connected: {info.name} build={info.build}")
            return True

        except ImportError:
            logger.warning("[MT5] MetaTrader5 not installed — using mock data")
            self._initialized = False
            return False

    def get_snapshot(self, symbol: str, timeframe: str = "H1") -> MarketSnapshot:
        """
        ดึง OHLCV + tick + ATR สำหรับ symbol
        
        Raises:
            RuntimeError: ถ้าดึงข้อมูลไม่ได้ (Safety Layer จะ catch → HOLD)
        """
        if not self._initialized:
            return self._mock_snapshot(symbol, timeframe)

        try:
            import MetaTrader5 as mt5

            tf_map = {
                "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
            }
            mt5_tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)

            # ดึง OHLCV (50 แท่งล่าสุด)
            rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, 50)
            if rates is None or len(rates) == 0:
                raise RuntimeError(f"No OHLCV data for {symbol}")

            # Tick ล่าสุด
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(f"No tick data for {symbol}")

            # Symbol info
            sym_info = mt5.symbol_info(symbol)
            if sym_info is None:
                raise RuntimeError(f"Symbol {symbol} not found")

            # คำนวณ ATR 14
            atr_14 = self._calculate_atr(rates, period=14)

            # Key levels (ใช้ swing highs/lows 20 แท่ง)
            key_levels = self._find_key_levels(rates, lookback=20)

            # Session
            session = self._get_current_session()

            last_bar = rates[-1]
            return MarketSnapshot(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=datetime.fromtimestamp(last_bar["time"], tz=timezone.utc).isoformat(),
                open=float(last_bar["open"]),
                high=float(last_bar["high"]),
                low=float(last_bar["low"]),
                close=float(last_bar["close"]),
                volume=float(last_bar["tick_volume"]),
                spread=float(sym_info.spread),
                bid=float(tick.bid),
                ask=float(tick.ask),
                session=session,
                atr_14=atr_14,
                key_levels=key_levels,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as exc:
            logger.error(f"[MT5] get_snapshot error: {exc}")
            raise RuntimeError(f"Market data fetch failed: {exc}") from exc

    def get_account_info(self) -> dict:
        """ดึงข้อมูล account จาก MT5"""
        if not self._initialized:
            # mock data สำหรับ development
            # หมายเหตุ: margin_level สูง (>200%) = ดี = มี margin เหลือเยอะ
            # margin_level ต่ำ (<100%) = อันตราย = margin call
            return {
                "balance": 10000.0,
                "equity": 10200.0,       # equity สูงกว่า balance เล็กน้อย (มีกำไรลอยตัว)
                "margin": 200.0,          # margin ใช้อยู่ $200 (มี 1 position เล็กๆ)
                "margin_free": 10000.0,   # margin เหลือใช้ $10000
                "margin_level": 510.0,    # 510% = healthy (ไม่ถึง margin call เลย)
                "profit": 200.0,          # กำไรลอยตัว $200
                "currency": "USD",
                "leverage": 100,
            }
        try:
            import MetaTrader5 as mt5
            acc = mt5.account_info()
            if acc is None:
                raise RuntimeError("Cannot get account info")
            return {
                "balance": acc.balance,
                "equity": acc.equity,
                "margin": acc.margin,
                "margin_free": acc.margin_free,
                "margin_level": acc.margin_level,
                "profit": acc.profit,
                "currency": acc.currency,
                "leverage": acc.leverage,
            }
        except Exception as exc:
            raise RuntimeError(f"Account info failed: {exc}") from exc

    def get_open_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """ดึง open positions จาก MT5"""
        if not self._initialized:
            return []
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
            if positions is None:
                return []
            return [
                {
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": "BUY" if p.type == 0 else "SELL",
                    "volume": p.volume,
                    "open_price": p.price_open,
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": p.profit,
                    "swap": p.swap,
                    "open_time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
                }
                for p in positions
            ]
        except Exception as exc:
            logger.error(f"[MT5] get_positions error: {exc}")
            return []

    # ──────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_atr(rates, period: int = 14) -> float:
        """คำนวณ ATR (Average True Range)"""
        if len(rates) < period + 1:
            return 0.0
        highs  = np.array([r["high"] for r in rates])
        lows   = np.array([r["low"] for r in rates])
        closes = np.array([r["close"] for r in rates])

        prev_closes = closes[:-1]
        true_ranges = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                abs(highs[1:] - prev_closes),
                abs(lows[1:] - prev_closes)
            )
        )
        return float(np.mean(true_ranges[-period:]))

    @staticmethod
    def _find_key_levels(rates, lookback: int = 20) -> list[float]:
        """หา Support/Resistance จาก swing highs/lows"""
        if len(rates) < lookback:
            return []
        recent = rates[-lookback:]
        highs = [float(r["high"]) for r in recent]
        lows  = [float(r["low"]) for r in recent]
        
        # Swing highs/lows (ง่ายๆ: max/min ของ lookback)
        levels = sorted(set([
            round(max(highs), 5),
            round(min(lows), 5),
            round(np.mean(highs), 5),
            round(np.mean(lows), 5),
        ]))
        return levels

    @staticmethod
    def _get_current_session() -> str:
        """ระบุ trading session ปัจจุบันจาก UTC hour"""
        hour = datetime.now(timezone.utc).hour
        for session, (start, end) in SESSION_MAP.items():
            if start <= hour < end:
                return session
        return "off"

    @staticmethod
    def _mock_snapshot(symbol: str, timeframe: str) -> MarketSnapshot:
        """
        Mock data สำหรับ development (ไม่มี MT5)
        ใช้ราคาและ context ที่ realistic ต่อ instrument
        """
        now = datetime.now(timezone.utc)
        session = "london" if 8 <= now.hour < 13 else (
            "newyork" if 13 <= now.hour < 22 else
            "asia" if now.hour < 8 else "off"
        )

        # ราคา mock ตาม instrument
        if "XAU" in symbol.upper() or "GOLD" in symbol.upper():
            # XAUUSD — Gold ราคาประมาณ $2,320
            o, h, l, c = 2318.50, 2325.80, 2315.20, 2322.40
            bid, ask, spread, atr = 2322.35, 2322.65, 30.0, 8.50
            levels = [2300.0, 2310.0, 2320.0, 2330.0, 2340.0]
        elif "GBP" in symbol.upper() and "JPY" in symbol.upper():
            o, h, l, c = 197.50, 198.20, 197.10, 197.85
            bid, ask, spread, atr = 197.83, 197.87, 4.0, 0.65
            levels = [196.50, 197.00, 197.50, 198.00, 198.50]
        elif "JPY" in symbol.upper():
            o, h, l, c = 149.50, 150.10, 149.30, 149.85
            bid, ask, spread, atr = 149.83, 149.85, 2.0, 0.55
            levels = [148.50, 149.00, 149.50, 150.00, 150.50]
        else:
            # EURUSD default
            o, h, l, c = 1.08500, 1.08750, 1.08300, 1.08620
            bid, ask, spread, atr = 1.08618, 1.08620, 1.5, 0.00085
            levels = [1.08000, 1.08250, 1.08500, 1.08750, 1.09000]

        return MarketSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=now.isoformat(),
            open=o, high=h, low=l, close=c,
            volume=1250.0,
            spread=spread,
            bid=bid,
            ask=ask,
            session=session,
            atr_14=atr,
            key_levels=levels,
            fetched_at=now.isoformat(),
        )
