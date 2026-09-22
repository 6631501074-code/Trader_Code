"""
safety/safety_layer.py — Deterministic Safety & Validation Layer
================================================================
[WARN]  ชั้นนี้เป็น Deterministic Python เท่านั้น ห้ามเรียก LLM
    ทุก check ที่ fail → HOLD ทันที (Fail-Closed)

ลำดับการตรวจ:
    1. Idempotency   — event ซ้ำ? → SKIP
    2. Data Freshness— ข้อมูลเก่าเกิน threshold? → HOLD
    3. JSON Validity — CEO output ผิด format? → Repair loop → HOLD
    4. Action Whitelist — action อยู่ใน [BUY, SELL, HOLD]?
    5. Numeric Sanity — SL > 0, confidence ∈ [0,1]?
    6. Risk Guard     — risk% ไม่เกิน max_risk_pct?
    7. Lot Calculation— คำนวณ lot โดย LotCalculator (Python only)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from config import get_settings
from core.state import TradingState, ValidationStatus, CEODecision
from safety.idempotency import IdempotencyRegistry
from safety.json_repair import (
    CEO_DECISION_SCHEMA,
    repair_json_output,
    validate_schema,
)
from safety.lot_calculator import LotCalculator, get_pip_value_from_mt5

logger = logging.getLogger(__name__)

settings = get_settings()

# Whitelist ของ action ที่ยอมรับได้
VALID_ACTIONS = {"BUY", "SELL", "HOLD"}


class SafetyLayer:
    """
    Deterministic Safety Gate — ทุก trade ต้องผ่านชั้นนี้ก่อน execute
    
    Usage:
        layer = SafetyLayer(idempotency_registry, lot_calculator)
        state = await layer.validate(state)
        if state["validation_result"]["status"] == "PASS":
            # safe to execute
    """

    def __init__(
        self,
        idempotency: IdempotencyRegistry,
        lot_calculator: Optional[LotCalculator] = None,
    ) -> None:
        self._idempotency = idempotency
        self._lot_calc = lot_calculator or LotCalculator(
            min_lot=settings.min_lot,
            max_lot=settings.max_lot,
        )

    async def validate(self, state: TradingState) -> TradingState:
        """
        Main validation entry point
        Returns updated state พร้อม validation_result และ trade_params
        """
        checks_passed: list[str] = []
        checks_failed: list[str] = []
        json_repair_attempts = 0

        # ─────────────────────────────────────────────────────────
        # CHECK 1: Idempotency
        # ─────────────────────────────────────────────────────────
        idem_key = state.get("idempotency_key", "")
        if await self._idempotency.is_duplicate(idem_key):
            return self._make_result(
                state=state,
                status=ValidationStatus.SKIP,
                reason=f"Duplicate idempotency key: {idem_key}",
                checks_passed=checks_passed,
                checks_failed=["idempotency"],
            )
        checks_passed.append("idempotency")

        # ─────────────────────────────────────────────────────────
        # CHECK 2: Data Freshness (Fail-Closed)
        # ─────────────────────────────────────────────────────────
        staleness_error = self._check_data_freshness(state)
        if staleness_error:
            checks_failed.append("data_freshness")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=staleness_error,
                checks_passed=checks_passed,
                checks_failed=checks_failed,
            )
        checks_passed.append("data_freshness")

        # ─────────────────────────────────────────────────────────
        # CHECK 3: Context Errors (Fail-Closed)
        # ─────────────────────────────────────────────────────────
        if state.get("context_error"):
            checks_failed.append("context_no_error")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=f"Context error: {state['context_error']}",
                checks_passed=checks_passed,
                checks_failed=checks_failed,
            )
        checks_passed.append("context_no_error")

        # ─────────────────────────────────────────────────────────
        # CHECK 4: CEO JSON Repair + Schema Validation
        # ─────────────────────────────────────────────────────────
        ceo_raw = state.get("ceo_decision", {}).get("raw_llm_output", "")
        try:
            ceo_parsed, json_repair_attempts = repair_json_output(
                raw_text=ceo_raw if ceo_raw else str(state.get("ceo_decision", {})),
                required_keys=CEO_DECISION_SCHEMA["required"],
                max_retries=settings.json_repair_max_retries,
            )
            schema_errors = validate_schema(ceo_parsed, CEO_DECISION_SCHEMA)
            if schema_errors:
                raise ValueError(f"Schema errors: {schema_errors}")
            checks_passed.append("json_schema")

        except ValueError as e:
            checks_failed.append("json_schema")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=f"CEO JSON invalid after repair: {e}",
                checks_passed=checks_passed,
                checks_failed=checks_failed,
                json_repair_attempts=json_repair_attempts,
            )

        # ─────────────────────────────────────────────────────────
        # CHECK 5: Action Whitelist
        # ─────────────────────────────────────────────────────────
        action = state.get("ceo_decision", {}).get("action", "").upper()
        if action not in VALID_ACTIONS:
            checks_failed.append("action_whitelist")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=f"Invalid action '{action}' — must be BUY/SELL/HOLD",
                checks_passed=checks_passed,
                checks_failed=checks_failed,
            )
        checks_passed.append("action_whitelist")

        # ─────────────────────────────────────────────────────────
        # CHECK 6: HOLD → skip lot calculation
        # ─────────────────────────────────────────────────────────
        if action == "HOLD":
            await self._idempotency.register(idem_key)
            return self._make_result(
                state=state,
                status=ValidationStatus.PASS,
                reason="Action=HOLD — no trade",
                checks_passed=checks_passed + ["hold_acknowledged"],
                checks_failed=checks_failed,
                final_action="HOLD",
            )

        # ─────────────────────────────────────────────────────────
        # CHECK 7: Numeric Sanity
        # ─────────────────────────────────────────────────────────
        ceo = state.get("ceo_decision", {})
        sanity_error = self._sanity_check(ceo)
        if sanity_error:
            checks_failed.append("numeric_sanity")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=sanity_error,
                checks_passed=checks_passed,
                checks_failed=checks_failed,
            )
        checks_passed.append("numeric_sanity")

        # ─────────────────────────────────────────────────────────
        # STEP 8: Lot Calculation (Deterministic Python — NO LLM)
        # ─────────────────────────────────────────────────────────
        try:
            trade_params = self._calculate_trade_params(state, action)
        except Exception as exc:
            checks_failed.append("lot_calculation")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=f"Lot calculation error: {exc}",
                checks_passed=checks_passed,
                checks_failed=checks_failed,
            )
        checks_passed.append("lot_calculation")

        # ─────────────────────────────────────────────────────────
        # CHECK 9: Risk Guard — lot size ผ่าน → risk% ไม่เกิน max
        # ─────────────────────────────────────────────────────────
        if trade_params["risk_pct"] > settings.max_risk_pct:
            checks_failed.append("risk_guard")
            return self._make_result(
                state=state,
                status=ValidationStatus.FAIL,
                reason=(
                    f"Risk {trade_params['risk_pct']*100:.2f}% "
                    f"exceeds max {settings.max_risk_pct*100:.1f}%"
                ),
                checks_passed=checks_passed,
                checks_failed=checks_failed,
            )
        checks_passed.append("risk_guard")

        # ─────────────────────────────────────────────────────────
        # ALL PASS — Register idempotency key
        # ─────────────────────────────────────────────────────────
        await self._idempotency.register(idem_key)

        logger.info(
            f"[SafetyLayer] [OK] PASS | action={action} "
            f"lot={trade_params['lot_size']} "
            f"risk={trade_params['risk_pct']*100:.2f}%"
        )

        updated = dict(state)
        updated["trade_params"] = trade_params
        updated["final_action"] = action
        updated["validation_result"] = {
            "status": "PASS",
            "reason": "All checks passed",
            "checks_passed": checks_passed,
            "checks_failed": [],
            "json_repair_attempts": json_repair_attempts,
        }
        return updated  # type: ignore

    # ──────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────

    def _check_data_freshness(self, state: TradingState) -> Optional[str]:
        """ตรวจอายุข้อมูล — ถ้าเก่าเกิน threshold → error string"""
        snapshot = state.get("market_snapshot")
        if not snapshot:
            return "No market snapshot available"

        fetched_at_str = snapshot.get("fetched_at", "")
        if not fetched_at_str:
            return "market_snapshot.fetched_at is missing"

        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()

            if age_seconds > settings.data_staleness_seconds:
                return (
                    f"Market data is stale: {age_seconds:.0f}s old "
                    f"(max {settings.data_staleness_seconds}s)"
                )
        except ValueError as e:
            return f"Invalid fetched_at format: {e}"

        return None

    @staticmethod
    def _sanity_check(ceo: dict) -> Optional[str]:
        """ตรวจค่า numeric ของ CEO decision"""
        try:
            sl = float(ceo.get("sl_pips", 0))
            tp = float(ceo.get("tp_pips", 0))
            conf = float(ceo.get("confidence", -1))
            entry = float(ceo.get("entry_price", 0))

            if sl <= 0:
                return f"sl_pips must be > 0, got {sl}"
            if tp < 0:
                return f"tp_pips must be >= 0, got {tp}"
            if not (0.0 <= conf <= 1.0):
                return f"confidence must be in [0,1], got {conf}"
            if entry <= 0:
                return f"entry_price must be > 0, got {entry}"
        except (TypeError, ValueError) as e:
            return f"Numeric parsing error: {e}"
        return None

    def _calculate_trade_params(self, state: TradingState, action: str) -> dict:
        """เรียก LotCalculator และ return trade_params dict"""
        ceo = state["ceo_decision"]
        account = state.get("account_info", {})
        symbol = state.get("symbol", "")

        balance = float(account.get("balance", 0))
        if balance <= 0:
            raise ValueError(f"Invalid account balance: {balance}")

        pip_value = get_pip_value_from_mt5(symbol)

        result = self._lot_calc.calculate(
            account_balance=balance,
            risk_pct=settings.max_risk_pct,
            sl_pips=float(ceo["sl_pips"]),
            pip_value_per_lot=pip_value,
            entry_price=float(ceo["entry_price"]),
            action=action,
            tp_pips=float(ceo.get("tp_pips", 0)),
            symbol=symbol,
        )

        return {
            "lot_size": result.lot_size,
            "sl_price": result.sl_price,
            "tp_price": result.tp_price,
            "risk_amount": result.risk_amount,
            "risk_pct": result.risk_pct_actual,
            "pip_value": result.pip_value,
            "account_balance": balance,
        }

    @staticmethod
    def _make_result(
        state: TradingState,
        status: ValidationStatus,
        reason: str,
        checks_passed: list[str],
        checks_failed: list[str],
        json_repair_attempts: int = 0,
        final_action: str = "HOLD",
    ) -> TradingState:
        updated = dict(state)
        # ใช้ .value เพื่อเก็บเป็น string ธรรมดา ("PASS"/"FAIL"/"SKIP") ไม่ใช่ Enum object
        status_str = status.value if hasattr(status, "value") else str(status)
        updated["validation_result"] = {
            "status": status_str,
            "reason": reason,
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "json_repair_attempts": json_repair_attempts,
        }
        if status_str != "PASS":
            updated["final_action"] = "HOLD"
            logger.warning(
                f"[SafetyLayer] [ERR] {status_str} | {reason} | "
                f"failed={checks_failed}"
            )
        else:
            updated["final_action"] = final_action
        return updated  # type: ignore
