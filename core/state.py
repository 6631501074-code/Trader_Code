"""
core/state.py — TradingState TypedDict
State กลางที่ไหลผ่านทุก Node ใน LangGraph workflow
LangGraph ต้องการ state เป็น TypedDict หรือ Pydantic model
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from typing_extensions import TypedDict


# ============================================================
# Enums
# ============================================================

class TradeAction(str, Enum):
    BUY   = "BUY"
    SELL  = "SELL"
    HOLD  = "HOLD"


class EventType(str, Enum):
    PRICE_ZONE_ENTRY  = "PRICE_ZONE_ENTRY"   # ราคาเข้าโซน key level
    SESSION_OPEN      = "SESSION_OPEN"         # เปิดเซสชัน (London/NY/Asia)
    BAR_CLOSE         = "BAR_CLOSE"            # ไม้ปิด (ตามไทม์เฟรมที่กำหนด)
    NEWS_EVENT        = "NEWS_EVENT"           # ข่าวสำคัญออก
    POSITION_UPDATE   = "POSITION_UPDATE"      # SL/TP ถูกชน หรือ position เปลี่ยน
    MANUAL_TRIGGER    = "MANUAL_TRIGGER"       # สั่งรันมือ


class ValidationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"   # Idempotency — ซ้ำ ข้ามได้


# ============================================================
# Sub-TypedDicts (nested state)
# ============================================================

class MarketSnapshot(TypedDict):
    """ข้อมูลตลาด ณ เวลานั้น — ดึงจาก MT5"""
    symbol: str
    timeframe: str          # "M5", "H1", "D1" ฯลฯ
    timestamp: str          # ISO-8601
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float           # หน่วย Points
    bid: float
    ask: float
    session: str            # "london" | "newyork" | "asia" | "off"
    atr_14: float           # ATR 14 สำหรับคำนวณ SL
    key_levels: list[float] # Support/Resistance levels
    fetched_at: str         # ISO-8601 เวลาที่ดึงข้อมูล (ใช้ตรวจ staleness)


class DepartmentReport(TypedDict):
    """ผลรายงานของแต่ละ Department Agent"""
    agent_name: str
    status: str             # "ok" | "error" | "timeout"
    recommendation: str     # BUY | SELL | HOLD
    confidence: float       # 0.0 – 1.0
    reasoning: str
    key_points: list[str]
    raw_llm_output: str
    latency_ms: float


class CEODecision(TypedDict):
    """ผลการตัดสินใจของ CEO Agent"""
    action: str             # BUY | SELL | HOLD
    confidence: float       # 0.0 – 1.0
    consensus_score: float  # เปอร์เซ็นต์แผนกที่เห็นด้วย
    dissenting_agents: list[str]
    reasoning: str
    sl_pips: float          # ระยะ Stop Loss (Pips) — เพื่อส่งต่อ Lot Calculator
    tp_pips: float          # ระยะ Take Profit (Pips)
    entry_price: float
    raw_llm_output: str


class TradeParams(TypedDict):
    """พารามิเตอร์ Trade — คำนวณโดย Python เท่านั้น ห้าม LLM แตะ"""
    lot_size: float
    sl_price: float
    tp_price: float
    risk_amount: float      # จำนวนเงินที่ risk (หน่วย account currency)
    risk_pct: float         # เปอร์เซ็นต์ risk จาก balance
    pip_value: float
    account_balance: float


class ValidationResult(TypedDict):
    """ผลการตรวจสอบจาก Safety Layer"""
    status: str             # ValidationStatus
    reason: str
    checks_passed: list[str]
    checks_failed: list[str]
    json_repair_attempts: int


# ============================================================
# Main TradingState — ไหลผ่านทุก Node ใน LangGraph
# ============================================================

class TradingState(TypedDict, total=False):
    # --- Event ---
    event_type: str                     # EventType
    symbol: str                         # เช่น "XAUUSD", "EURUSD"
    triggered_at: str                   # ISO-8601 เวลาที่ event เกิด

    # --- Identifiers ---
    trace_id: str                       # UUID สำหรับ trace ทั้งกระบวนการ
    idempotency_key: str                # SHA-256 hash ของ event ป้องกันซ้ำ

    # --- Step 1: Context ---
    market_snapshot: MarketSnapshot
    account_info: dict[str, Any]        # balance, equity, margin_free ฯลฯ
    open_positions: list[dict]          # positions ที่เปิดอยู่
    context_error: Optional[str]        # ถ้าดึงข้อมูลไม่ได้

    # --- Step 2: Department Reports ---
    department_reports: dict[str, DepartmentReport]
    departments_error: Optional[str]

    # --- Step 3: CEO Decision ---
    ceo_decision: CEODecision
    ceo_error: Optional[str]

    # --- Step 4: Safety & Execution ---
    validation_result: ValidationResult
    trade_params: TradeParams           # คำนวณโดย lot_calculator.py เท่านั้น
    execution_result: Optional[dict]    # ผลจาก MT5 order
    final_action: str                   # TradeAction ที่ execute จริง

    # --- Metadata ---
    workflow_duration_ms: float
    node_timings: dict[str, float]      # latency แต่ละ node
    error_log: list[str]                # error สะสม
