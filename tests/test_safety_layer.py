"""
tests/test_safety_layer.py — Unit Tests สำหรับ Safety Layer & Lot Calculator
=============================================================================
ทดสอบ:
  - Lot size calculation (edge cases)
  - Fail-Closed behavior
  - JSON Repair Loop
  - Idempotency
  - Data staleness detection
"""
from __future__ import annotations

import asyncio
import json
import pytest

from safety.lot_calculator import LotCalculator, LotCalculationResult
from safety.json_repair import repair_json_output, validate_schema, CEO_DECISION_SCHEMA
from safety.idempotency import IdempotencyRegistry


# ============================================================
# Lot Calculator Tests
# ============================================================

class TestLotCalculator:
    """ทดสอบ LotCalculator — ห้ามผ่านถ้า calculation ผิด"""

    def setup_method(self):
        self.calc = LotCalculator(min_lot=0.01, max_lot=5.0, lot_step=0.01)

    def test_basic_calculation(self):
        """ทดสอบสูตรพื้นฐาน: 1% risk, 20 pips SL, $10/pip"""
        # risk_amount = 10000 * 0.01 = 100
        # lot = 100 / (20 * 10) = 0.50
        result = self.calc.calculate(
            account_balance=10000.0,
            risk_pct=0.01,
            sl_pips=20.0,
            pip_value_per_lot=10.0,
            entry_price=1.10000,
            action="BUY",
            tp_pips=40.0,
            symbol="EURUSD",
        )
        assert result.lot_size == 0.50
        assert result.risk_amount == pytest.approx(100.0, rel=0.01)
        assert not result.is_clipped

    def test_lot_clipped_to_max(self):
        """ถ้า calculated lot > max_lot → clip ลงมา"""
        # risk_amount = 100000 * 0.05 = 5000
        # raw_lot = 5000 / (10 * 10) = 50 → clip to 5.0
        result = self.calc.calculate(
            account_balance=100000.0,
            risk_pct=0.05,
            sl_pips=10.0,
            pip_value_per_lot=10.0,
            entry_price=1.10000,
            action="SELL",
            symbol="EURUSD",
        )
        assert result.lot_size == 5.0
        assert result.is_clipped is True

    def test_lot_clipped_to_min(self):
        """ถ้า calculated lot < min_lot → clip ขึ้นมา"""
        # very small balance
        result = self.calc.calculate(
            account_balance=100.0,
            risk_pct=0.01,
            sl_pips=100.0,
            pip_value_per_lot=10.0,
            entry_price=1.10000,
            action="BUY",
            symbol="EURUSD",
        )
        assert result.lot_size == 0.01
        assert result.is_clipped is True

    def test_buy_sl_tp_prices(self):
        """BUY: SL ต่ำกว่า entry, TP สูงกว่า entry"""
        result = self.calc.calculate(
            account_balance=10000.0,
            risk_pct=0.01,
            sl_pips=20.0,
            pip_value_per_lot=10.0,
            entry_price=1.10000,
            action="BUY",
            tp_pips=40.0,
            symbol="EURUSD",
        )
        assert result.sl_price < 1.10000  # SL ต่ำกว่า entry
        assert result.tp_price > 1.10000  # TP สูงกว่า entry
        assert result.sl_price == pytest.approx(1.09800, abs=0.00001)
        assert result.tp_price == pytest.approx(1.10400, abs=0.00001)

    def test_sell_sl_tp_prices(self):
        """SELL: SL สูงกว่า entry, TP ต่ำกว่า entry"""
        result = self.calc.calculate(
            account_balance=10000.0,
            risk_pct=0.01,
            sl_pips=20.0,
            pip_value_per_lot=10.0,
            entry_price=1.10000,
            action="SELL",
            tp_pips=40.0,
            symbol="EURUSD",
        )
        assert result.sl_price > 1.10000
        assert result.tp_price < 1.10000

    def test_invalid_sl_zero_raises(self):
        """SL = 0 ต้อง raise ValueError (ป้องกัน division by zero)"""
        with pytest.raises(ValueError, match="sl_pips must be > 0"):
            self.calc.calculate(
                account_balance=10000.0,
                risk_pct=0.01,
                sl_pips=0.0,
                pip_value_per_lot=10.0,
                entry_price=1.10000,
                action="BUY",
            )

    def test_invalid_balance_raises(self):
        """Balance <= 0 ต้อง raise ValueError"""
        with pytest.raises(ValueError, match="balance must be > 0"):
            self.calc.calculate(
                account_balance=0.0,
                risk_pct=0.01,
                sl_pips=20.0,
                pip_value_per_lot=10.0,
                entry_price=1.10000,
                action="BUY",
            )

    def test_risk_pct_too_high_raises(self):
        """Risk% > 10% ต้อง raise ValueError"""
        with pytest.raises(ValueError, match="risk_pct must be in"):
            self.calc.calculate(
                account_balance=10000.0,
                risk_pct=0.20,  # 20% — too high
                sl_pips=20.0,
                pip_value_per_lot=10.0,
                entry_price=1.10000,
                action="BUY",
            )

    def test_lot_step_rounding(self):
        """ผลลัพธ์ต้องปัดตาม lot_step เสมอ"""
        result = self.calc.calculate(
            account_balance=10000.0,
            risk_pct=0.01,
            sl_pips=33.0,    # ทำให้ lot ไม่ลงตัว
            pip_value_per_lot=10.0,
            entry_price=1.10000,
            action="BUY",
        )
        # lot_size ต้องเป็น multiple ของ 0.01
        assert round(result.lot_size / self.calc.lot_step, 6).is_integer()

    def test_xauusd_pip_unit(self):
        """XAUUSD ใช้ pip unit 0.1 — SL/TP ต้องคำนวณถูก"""
        result = self.calc.calculate(
            account_balance=10000.0,
            risk_pct=0.01,
            sl_pips=50.0,        # 50 pips XAUUSD = $50 move
            pip_value_per_lot=100.0,
            entry_price=2350.0,
            action="BUY",
            tp_pips=100.0,
            symbol="XAUUSD",
        )
        # SL ต้องอยู่ต่ำกว่า entry
        assert result.sl_price < 2350.0


# ============================================================
# JSON Repair Tests
# ============================================================

class TestJsonRepair:
    """ทดสอบ JSON Repair Loop"""

    def test_valid_json_no_repair_needed(self):
        """JSON ที่ถูกต้องไม่ควร fail"""
        valid = json.dumps({
            "recommendation": "BUY",
            "confidence": 0.8,
            "reasoning": "Strong bullish signal",
            "key_points": ["trend up", "near support"],
        })
        result, attempts = repair_json_output(valid, ["recommendation", "confidence", "reasoning", "key_points"])
        assert result["recommendation"] == "BUY"
        assert attempts == 1

    def test_markdown_fence_stripped(self):
        """JSON ที่ wrap ด้วย ```json ... ``` ต้องถูก strip"""
        wrapped = """```json
{
  "recommendation": "SELL",
  "confidence": 0.7,
  "reasoning": "Bearish divergence",
  "key_points": ["rsi divergence"]
}
```"""
        result, _ = repair_json_output(wrapped, ["recommendation", "confidence", "reasoning", "key_points"])
        assert result["recommendation"] == "SELL"

    def test_trailing_comma_repaired(self):
        """Trailing comma ต้องถูก fix"""
        bad_json = '{"recommendation": "HOLD", "confidence": 0.5, "reasoning": "unclear", "key_points": [],}'
        result, _ = repair_json_output(bad_json, ["recommendation", "confidence", "reasoning", "key_points"])
        assert result["recommendation"] == "HOLD"

    def test_missing_required_key_raises(self):
        """ถ้า required key ขาด → raise ValueError"""
        incomplete = json.dumps({
            "recommendation": "BUY",
            # missing: confidence, reasoning, key_points
        })
        with pytest.raises(ValueError, match="Missing required keys"):
            repair_json_output(incomplete, ["recommendation", "confidence", "reasoning", "key_points"])

    def test_ceo_schema_validation_pass(self):
        """CEO JSON ที่ถูกต้องต้องผ่าน schema"""
        ceo_json = json.dumps({
            "action": "BUY",
            "confidence": 0.85,
            "reasoning": "Strong consensus",
            "sl_pips": 20.0,
            "tp_pips": 40.0,
            "entry_price": 1.10200,
        })
        result, _ = repair_json_output(ceo_json, CEO_DECISION_SCHEMA["required"])
        errors = validate_schema(result, CEO_DECISION_SCHEMA)
        assert len(errors) == 0

    def test_invalid_action_enum_fails_schema(self):
        """action ที่ไม่อยู่ใน enum ต้องผ่าน repair แต่ fail schema"""
        bad_action = json.dumps({
            "action": "MAYBE",  # invalid
            "confidence": 0.5,
            "reasoning": "test",
            "sl_pips": 20.0,
            "tp_pips": 40.0,
            "entry_price": 1.10000,
        })
        parsed, _ = repair_json_output(bad_action, CEO_DECISION_SCHEMA["required"])
        errors = validate_schema(parsed, CEO_DECISION_SCHEMA)
        assert any("action" in e for e in errors)


# ============================================================
# Idempotency Tests
# ============================================================

class TestIdempotency:
    """ทดสอบ Idempotency Registry"""

    def test_new_key_not_duplicate(self):
        """Key ใหม่ต้องไม่ถูกตรวจว่าซ้ำ"""
        registry = IdempotencyRegistry(db_pool=None)
        result = asyncio.run(registry.is_duplicate("new_key_123"))
        assert result is False

    def test_registered_key_is_duplicate(self):
        """Key ที่ register แล้วต้องถูกตรวจว่าซ้ำ"""
        registry = IdempotencyRegistry(db_pool=None)
        key = "test_key_abc"
        asyncio.run(registry.register(key))
        result = asyncio.run(registry.is_duplicate(key))
        assert result is True

    def test_different_keys_not_duplicate(self):
        """Key ที่ต่างกันต้องไม่ซ้ำกัน"""
        registry = IdempotencyRegistry(db_pool=None)
        asyncio.run(registry.register("key_1"))
        assert asyncio.run(registry.is_duplicate("key_2")) is False


# ============================================================
# Safety Layer Integration Tests
# ============================================================

class TestSafetyLayerIntegration:
    """Integration tests สำหรับ SafetyLayer"""

    def _make_state(self, action="BUY", sl_pips=20.0, staleness_seconds=0):
        """Helper สร้าง TradingState สำหรับ test"""
        from datetime import datetime, timezone, timedelta
        from core.state import TradingState

        fetched_at = (
            datetime.now(timezone.utc) - timedelta(seconds=staleness_seconds)
        ).isoformat()

        ceo_decision = {
            "action": action,
            "confidence": 0.8,
            "consensus_score": 0.7,
            "dissenting_agents": [],
            "reasoning": "Test reasoning",
            "sl_pips": sl_pips,
            "tp_pips": 40.0,
            "entry_price": 1.10200,
            "raw_llm_output": json.dumps({
                "action": action,
                "confidence": 0.8,
                "reasoning": "Test",
                "sl_pips": sl_pips,
                "tp_pips": 40.0,
                "entry_price": 1.10200,
            }),
        }

        return {
            "symbol": "EURUSD",
            "event_type": "MANUAL_TRIGGER",
            "idempotency_key": "test_key_001",
            "trace_id": "test_trace_001",
            "market_snapshot": {
                "symbol": "EURUSD",
                "timeframe": "H1",
                "timestamp": "2024-01-01T10:00:00+00:00",
                "open": 1.10000, "high": 1.10500,
                "low": 1.09800, "close": 1.10200,
                "volume": 1000.0, "spread": 1.5,
                "bid": 1.10198, "ask": 1.10200,
                "session": "london", "atr_14": 0.00120,
                "key_levels": [1.09500, 1.10000],
                "fetched_at": fetched_at,
            },
            "account_info": {
                "balance": 10000.0, "equity": 10000.0,
                "margin_level": 500.0,
            },
            "open_positions": [],
            "department_reports": {},
            "ceo_decision": ceo_decision,
            "error_log": [],
            "node_timings": {},
        }

    def test_valid_buy_passes(self):
        """Valid BUY trade ต้องผ่าน Safety Layer"""
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry

        registry = IdempotencyRegistry(db_pool=None)
        layer = SafetyLayer(idempotency=registry)
        state = self._make_state(action="BUY", sl_pips=20.0)

        result = asyncio.run(layer.validate(state))
        assert result["validation_result"]["status"] == "PASS"
        assert result["final_action"] == "BUY"
        assert result["trade_params"]["lot_size"] > 0

    def test_stale_data_fails_closed(self):
        """ข้อมูลค้าง > 30 วิ ต้อง FAIL (Fail-Closed)"""
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry

        registry = IdempotencyRegistry(db_pool=None)
        layer = SafetyLayer(idempotency=registry)
        from config import get_settings
        threshold = get_settings().data_staleness_seconds
        state = self._make_state(staleness_seconds=threshold + 30)

        result = asyncio.run(layer.validate(state))
        assert result["validation_result"]["status"] == "FAIL"
        assert result["final_action"] == "HOLD"
        assert "stale" in result["validation_result"]["reason"].lower()

    def test_hold_action_passes_without_lot(self):
        """HOLD action ต้องผ่านโดยไม่คำนวณ lot"""
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry

        registry = IdempotencyRegistry(db_pool=None)
        layer = SafetyLayer(idempotency=registry)
        state = self._make_state(action="HOLD")

        result = asyncio.run(layer.validate(state))
        assert result["validation_result"]["status"] == "PASS"
        assert result["final_action"] == "HOLD"
        # ไม่มี trade_params เพราะเป็น HOLD
        assert result.get("trade_params") is None

    def test_duplicate_key_skipped(self):
        """Event ซ้ำต้อง SKIP"""
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry

        registry = IdempotencyRegistry(db_pool=None)
        layer = SafetyLayer(idempotency=registry)
        state = self._make_state(action="BUY")

        # Register key ก่อน
        asyncio.run(registry.register(state["idempotency_key"]))

        result = asyncio.run(layer.validate(state))
        assert result["validation_result"]["status"] == "SKIP"
