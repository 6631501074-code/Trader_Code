"""
safety/lot_calculator.py — Deterministic Lot Size Calculator
============================================================
[WARN]  กฎเหล็ก: LLM ห้ามเรียก / แก้ไข / Override ฟังก์ชันในไฟล์นี้เด็ดขาด
    การคำนวณ Lot size ต้องเป็น Pure Python เท่านั้น
============================================================

สูตร:
    risk_amount = account_balance × risk_pct
    lot_size    = risk_amount / (sl_pips × pip_value_per_lot)

pip_value_per_lot:
    - Forex 5-digit (EURUSD, GBPUSD) : 10 USD per pip per 1 lot (standard)
    - XAUUSD (Gold)                  : 1 USD per 0.01 pip = 100 USD per pip per 1 lot
    - USDJPY                         : ~6.9 USD per pip (vary with rate)
    → ควรดึงจาก MT5 symbol info จริง (tick_value × (1/tick_size))
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================
# Result object
# ============================================================

@dataclass(frozen=True)
class LotCalculationResult:
    lot_size: float         # lot ที่คำนวณได้ (clipped ตาม min/max)
    risk_amount: float      # จำนวนเงินที่ risk จริง (account currency)
    risk_pct_actual: float  # % risk จริงหลัง clipping
    sl_price: float
    tp_price: float
    pip_value: float
    is_clipped: bool        # True ถ้า lot ถูก clip ด้วย min/max
    notes: str


# ============================================================
# Core Calculator — Pure Python, No LLM
# ============================================================

class LotCalculator:
    """
    Deterministic lot size calculator.
    Input มาจาก CEO Decision (sl_pips, tp_pips, entry_price, direction)
    และ account info จาก MT5.
    """

    def __init__(
        self,
        min_lot: float = 0.01,
        max_lot: float = 5.0,
        lot_step: float = 0.01,
    ) -> None:
        self.min_lot = min_lot
        self.max_lot = max_lot
        self.lot_step = lot_step

    def calculate(
        self,
        account_balance: float,
        risk_pct: float,            # 0.01 = 1%
        sl_pips: float,             # ระยะ SL เป็น Pips
        pip_value_per_lot: float,   # USD per pip per 1 lot (ดึงจาก MT5)
        entry_price: float,
        action: str,                # "BUY" | "SELL"
        tp_pips: float = 0.0,
        symbol: str = "",
    ) -> LotCalculationResult:
        """
        คำนวณ lot size จาก Risk% และระยะ SL จริง

        Args:
            account_balance: ยอด balance ปัจจุบัน (USD)
            risk_pct: สัดส่วน risk ต่อไม้ (เช่น 0.01 = 1%)
            sl_pips: ระยะ Stop Loss เป็น Pips (ต้อง > 0)
            pip_value_per_lot: มูลค่า 1 pip per 1 lot (USD)
            entry_price: ราคาเข้า
            action: "BUY" หรือ "SELL"
            tp_pips: ระยะ Take Profit เป็น Pips
            symbol: ชื่อ instrument (สำหรับ logging)

        Returns:
            LotCalculationResult — immutable

        Raises:
            ValueError: ถ้า input ไม่ valid (SL = 0, balance <=0 ฯลฯ)
        """
        # --- Validation ---
        self._validate_inputs(account_balance, risk_pct, sl_pips, pip_value_per_lot)

        # --- Core Formula ---
        risk_amount = account_balance * risk_pct
        raw_lot = risk_amount / (sl_pips * pip_value_per_lot)

        # --- Clip to broker limits ---
        clipped_lot = max(self.min_lot, min(self.max_lot, raw_lot))
        is_clipped = abs(clipped_lot - raw_lot) > 1e-9

        # --- Round to lot step ---
        lot_size = self._round_lot(clipped_lot)

        # --- Recalculate actual risk after rounding ---
        actual_risk_amount = lot_size * sl_pips * pip_value_per_lot
        actual_risk_pct = actual_risk_amount / account_balance

        # --- SL / TP Prices ---
        pip_unit = 0.0001  # standard forex; gold uses 0.01 → override ข้างนอก
        if "JPY" in symbol.upper():
            pip_unit = 0.01
        elif "XAU" in symbol.upper() or "GOLD" in symbol.upper():
            pip_unit = 0.1  # XAUUSD: 1 pip = 0.1 USD

        if action.upper() == "BUY":
            sl_price = entry_price - (sl_pips * pip_unit)
            tp_price = entry_price + (tp_pips * pip_unit) if tp_pips else 0.0
        else:  # SELL
            sl_price = entry_price + (sl_pips * pip_unit)
            tp_price = entry_price - (tp_pips * pip_unit) if tp_pips else 0.0

        notes = ""
        if is_clipped:
            notes = (
                f"Raw lot {raw_lot:.4f} clipped → {lot_size:.2f} "
                f"(min={self.min_lot}, max={self.max_lot})"
            )

        logger.info(
            f"[LotCalc] {symbol} {action} | balance={account_balance:.2f} "
            f"risk={risk_pct*100:.1f}% SL={sl_pips}pips "
            f"pip_val={pip_value_per_lot:.2f} → lot={lot_size:.2f} "
            f"risk_amount={actual_risk_amount:.2f}"
        )

        return LotCalculationResult(
            lot_size=lot_size,
            risk_amount=actual_risk_amount,
            risk_pct_actual=actual_risk_pct,
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            pip_value=pip_value_per_lot,
            is_clipped=is_clipped,
            notes=notes,
        )

    def _round_lot(self, lot: float) -> float:
        """ปัดค่า lot ลงตาม lot_step ของโบรกเกอร์"""
        steps = math.floor(lot / self.lot_step)
        return round(steps * self.lot_step, 2)

    @staticmethod
    def _validate_inputs(
        balance: float,
        risk_pct: float,
        sl_pips: float,
        pip_value: float,
    ) -> None:
        errors = []
        if balance <= 0:
            errors.append(f"balance must be > 0, got {balance}")
        if not (0 < risk_pct <= 0.1):
            errors.append(f"risk_pct must be in (0, 0.1], got {risk_pct}")
        if sl_pips <= 0:
            errors.append(f"sl_pips must be > 0, got {sl_pips}")
        if pip_value <= 0:
            errors.append(f"pip_value_per_lot must be > 0, got {pip_value}")
        if errors:
            raise ValueError(f"LotCalculator validation failed: {'; '.join(errors)}")


# ============================================================
# MT5 Pip Value Helper — ดึงจาก broker จริง
# ============================================================

def get_pip_value_from_mt5(symbol: str) -> float:
    """
    ดึง pip value per lot จาก MT5 symbol info
    tick_value = มูลค่าต่อ 1 tick ต่อ 1 lot
    pip_value  = tick_value × (pip_size / tick_size)
    """
    try:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Symbol {symbol} not found or MT5 not connected")

        tick_size  = info.trade_tick_size   # เช่น 0.00001 สำหรับ EURUSD
        tick_value = info.trade_tick_value  # USD per tick per lot

        # pip_size: standard = 0.0001, JPY = 0.01, Gold (XAUUSD/GOLD) = 0.1 (broker-dependent)
        if "JPY" in symbol.upper():
            pip_size = 0.01
        elif "XAU" in symbol.upper() or "GOLD" in symbol.upper():
            pip_size = 0.1
        else:
            pip_size = 0.0001

        pip_value = tick_value * (pip_size / tick_size)
        logger.debug(f"[PipValue] {symbol}: tick_val={tick_value} tick_sz={tick_size} pip_val={pip_value:.4f}")
        return pip_value

    except Exception as exc:
        # Fallback สำหรับ test และ dev mode (ไม่มี MT5 หรือ MT5 ยังไม่ connected)
        logger.debug(f"[PipValue] Using fallback default for {symbol} ({exc})")
        defaults = {"EURUSD": 10.0, "GBPUSD": 10.0, "XAUUSD": 10.0, "GOLD": 10.0, "USDJPY": 9.0}
        return defaults.get(symbol, 10.0)

