"""
tests/test_orchestrator.py — Integration Tests สำหรับ Workflow
===============================================================
ทดสอบ workflow ทั้งหมดแบบ end-to-end (mock LLM, mock MT5)
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.state import TradingState
from core.event_bus import TradingEvent


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

MOCK_SNAPSHOT = {
    "symbol": "EURUSD",
    "timeframe": "H1",
    "timestamp": "2024-01-15T10:00:00+00:00",
    "open": 1.10000, "high": 1.10500,
    "low": 1.09800, "close": 1.10200,
    "volume": 1500.0, "spread": 1.5,
    "bid": 1.10198, "ask": 1.10200,
    "session": "london", "atr_14": 0.00120,
    "key_levels": [1.09500, 1.10000, 1.10500],
    "fetched_at": "2024-01-15T10:00:00+00:00",  # fresh data
}

MOCK_ACCOUNT = {
    "balance": 10000.0,
    "equity": 10000.0,
    "margin": 0.0,
    "margin_free": 10000.0,
    "margin_level": 9999.0,
    "profit": 0.0,
    "currency": "USD",
}

MOCK_DEPT_RESPONSE = json.dumps({
    "recommendation": "BUY",
    "confidence": 0.75,
    "reasoning": "Strong upward momentum with bullish structure",
    "key_points": ["Bullish engulfing at support", "Above 200 EMA", "Rising volume"],
})

MOCK_CEO_RESPONSE = json.dumps({
    "action": "BUY",
    "confidence": 0.80,
    "consensus_score": 0.71,
    "dissenting_agents": ["LearningOfficer"],
    "reasoning": "Strong consensus from 5/7 departments. Price action and macro aligned.",
    "sl_pips": 20.0,
    "tp_pips": 40.0,
    "entry_price": 1.10200,
})


@pytest.fixture
def mock_market_fetcher():
    """Mock MarketDataFetcher"""
    fetcher = MagicMock()
    fetcher.initialize.return_value = True
    fetcher.get_snapshot.return_value = MOCK_SNAPSHOT
    fetcher.get_account_info.return_value = MOCK_ACCOUNT
    fetcher.get_open_positions.return_value = []
    return fetcher


@pytest.fixture
def mock_audit_logger():
    """Mock AuditLogger"""
    logger = MagicMock()
    logger.initialize_schema = AsyncMock()
    logger.log_workflow_start = AsyncMock(return_value="test_trace_001")
    logger.log_agent_reports = AsyncMock()
    logger.log_ceo_decision = AsyncMock()
    logger.log_execution = AsyncMock()
    logger.finalize_run = AsyncMock()
    return logger


# ──────────────────────────────────────────────────────────────
# Test: Build Context Node
# ──────────────────────────────────────────────────────────────

class TestBuildContext:
    """ทดสอบ Step 1: build_context node"""

    @pytest.mark.asyncio
    async def test_context_built_successfully(self, mock_market_fetcher, mock_audit_logger):
        from core.orchestrator import WorkflowNodes
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry

        registry = IdempotencyRegistry(db_pool=None)
        safety = SafetyLayer(idempotency=registry)
        from agents.ceo_agent import CEOAgent
        nodes = WorkflowNodes(
            market_fetcher=mock_market_fetcher,
            safety_layer=safety,
            ceo_agent=CEOAgent(),
            audit_logger=mock_audit_logger,
        )

        initial_state: TradingState = {
            "symbol": "EURUSD",
            "event_type": "MANUAL_TRIGGER",
            "idempotency_key": "test_key",
            "triggered_at": "2024-01-15T10:00:00+00:00",
        }

        result = await nodes.build_context(initial_state)

        assert "trace_id" in result
        assert result.get("context_error") is None
        assert result["market_snapshot"]["symbol"] == "EURUSD"
        assert result["account_info"]["balance"] == 10000.0
        assert result["open_positions"] == []

    @pytest.mark.asyncio
    async def test_context_error_on_mt5_failure(self, mock_audit_logger):
        """ถ้า MT5 ล้มเหลว → context_error ต้อง set (ไม่ crash)"""
        from core.orchestrator import WorkflowNodes
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry
        from agents.ceo_agent import CEOAgent

        bad_fetcher = MagicMock()
        bad_fetcher.initialize.return_value = False
        bad_fetcher.get_snapshot.side_effect = RuntimeError("MT5 connection failed")
        bad_fetcher.get_account_info.return_value = MOCK_ACCOUNT
        bad_fetcher.get_open_positions.return_value = []

        registry = IdempotencyRegistry(db_pool=None)
        safety = SafetyLayer(idempotency=registry)
        nodes = WorkflowNodes(
            market_fetcher=bad_fetcher,
            safety_layer=safety,
            ceo_agent=CEOAgent(),
            audit_logger=mock_audit_logger,
        )

        result = await nodes.build_context({
            "symbol": "EURUSD", "event_type": "MANUAL_TRIGGER",
            "idempotency_key": "key1", "triggered_at": "2024-01-15T10:00:00+00:00",
        })

        assert result["context_error"] is not None
        assert "MT5" in result["context_error"]


# ──────────────────────────────────────────────────────────────
# Test: Department Agents Parallel Runner
# ──────────────────────────────────────────────────────────────

class TestRunDepartments:
    """ทดสอบ Step 2: run_departments (parallel)"""

    @pytest.mark.asyncio
    async def test_all_agents_return_reports(self, mock_market_fetcher, mock_audit_logger):
        """ทุก agent ต้อง return report (ไม่ crash pipeline)"""
        from core.orchestrator import WorkflowNodes, DEPARTMENT_AGENTS
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry
        from agents.ceo_agent import CEOAgent

        # Mock LLM response สำหรับทุก agent
        mock_llm_response = MagicMock()
        mock_llm_response.content = MOCK_DEPT_RESPONSE

        registry = IdempotencyRegistry(db_pool=None)
        safety = SafetyLayer(idempotency=registry)
        nodes = WorkflowNodes(
            market_fetcher=mock_market_fetcher,
            safety_layer=safety,
            ceo_agent=CEOAgent(),
            audit_logger=mock_audit_logger,
        )

        state: TradingState = {
            "symbol": "EURUSD",
            "event_type": "MANUAL_TRIGGER",
            "trace_id": "test_trace",
            "idempotency_key": "key1",
            "market_snapshot": MOCK_SNAPSHOT,
            "account_info": MOCK_ACCOUNT,
            "open_positions": [],
            "error_log": [],
            "node_timings": {},
        }

        with patch("agents.base_agent.BaseAgent._get_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(return_value=mock_llm_response)
            mock_get_llm.return_value = mock_llm

            result = await nodes.run_departments(state)

        reports = result.get("department_reports", {})
        # ต้องมีครบ 7 reports
        assert len(reports) == len(DEPARTMENT_AGENTS)
        for agent_name, report in reports.items():
            assert "recommendation" in report
            assert "confidence" in report

    @pytest.mark.asyncio
    async def test_failed_agent_returns_hold(self, mock_market_fetcher, mock_audit_logger):
        """Agent ที่ fail ต้องคืน HOLD report ไม่ crash ทั้งระบบ"""
        from core.orchestrator import WorkflowNodes
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry
        from agents.ceo_agent import CEOAgent

        registry = IdempotencyRegistry(db_pool=None)
        safety = SafetyLayer(idempotency=registry)
        nodes = WorkflowNodes(
            market_fetcher=mock_market_fetcher,
            safety_layer=safety,
            ceo_agent=CEOAgent(),
            audit_logger=mock_audit_logger,
        )

        state: TradingState = {
            "symbol": "EURUSD", "event_type": "MANUAL_TRIGGER",
            "trace_id": "test", "idempotency_key": "key1",
            "market_snapshot": MOCK_SNAPSHOT, "account_info": MOCK_ACCOUNT,
            "open_positions": [], "error_log": [], "node_timings": {},
        }

        # Force LLM to throw exception
        with patch("agents.base_agent.BaseAgent._get_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM timeout"))
            mock_get_llm.return_value = mock_llm

            result = await nodes.run_departments(state)

        reports = result.get("department_reports", {})
        # ทุก agent ที่ fail ต้องได้ HOLD
        for report in reports.values():
            assert report["recommendation"] == "HOLD"
            assert report["status"] in ("error", "timeout")


# ──────────────────────────────────────────────────────────────
# Test: End-to-End Workflow (HOLD path)
# ──────────────────────────────────────────────────────────────

class TestEndToEndWorkflow:
    """ทดสอบ workflow ทั้ง pipeline"""

    @pytest.mark.asyncio
    async def test_workflow_returns_hold_on_bad_data(
        self, mock_audit_logger
    ):
        """ถ้าข้อมูลค้าง → workflow สุดท้าย HOLD"""
        import time
        from datetime import datetime, timezone

        # Stale snapshot
        stale_snap = {**MOCK_SNAPSHOT, "fetched_at": "2020-01-01T00:00:00+00:00"}

        bad_fetcher = MagicMock()
        bad_fetcher.initialize.return_value = True
        bad_fetcher.get_snapshot.return_value = stale_snap
        bad_fetcher.get_account_info.return_value = MOCK_ACCOUNT
        bad_fetcher.get_open_positions.return_value = []

        from core.orchestrator import TradingOrchestrator
        from safety.safety_layer import SafetyLayer
        from safety.idempotency import IdempotencyRegistry
        from agents.ceo_agent import CEOAgent

        registry = IdempotencyRegistry(db_pool=None)
        safety = SafetyLayer(idempotency=registry)

        orch = TradingOrchestrator(
            market_fetcher=bad_fetcher,
            safety_layer=safety,
            ceo_agent=CEOAgent(),
            audit_logger=mock_audit_logger,
            symbols=["EURUSD"],
            timeframe="H1",
        )
        await orch.initialize()

        event = TradingEvent("MANUAL_TRIGGER", "EURUSD")

        mock_llm_resp = MagicMock()
        mock_llm_resp.content = MOCK_DEPT_RESPONSE

        with patch("agents.base_agent.BaseAgent._get_llm") as mock_get_llm, \
             patch("agents.ceo_agent.CEOAgent._get_llm") as mock_ceo_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke = AsyncMock(return_value=mock_llm_resp)
            mock_get_llm.return_value = mock_llm

            mock_ceo = AsyncMock()
            ceo_resp = MagicMock()
            ceo_resp.content = MOCK_CEO_RESPONSE
            mock_ceo.ainvoke = AsyncMock(return_value=ceo_resp)
            mock_ceo_llm.return_value = mock_ceo

            final_state = await orch.handle_event(event)

        # ข้อมูลค้าง → ต้อง HOLD
        assert final_state.get("final_action") == "HOLD"
        validation = final_state.get("validation_result", {})
        assert validation.get("status") in ("FAIL", "SKIP")
