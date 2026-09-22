"""
core/orchestrator.py — LangGraph Workflow Engine
=================================================
4-Step Workflow ด้วย LangGraph StateGraph:
  Step 1: build_context     → ดึงข้อมูลตลาด + สร้าง Idempotency Key
  Step 2: run_departments   → asyncio.gather() ทั้ง 7 agent พร้อมกัน
  Step 3: ceo_synthesize    → CEO รวมรายงาน + ตัดสินใจ
  Step 4: safety_validate   → Safety Layer → Lot Calc → Broker / HOLD

LangGraph node ทุกตัว: (state: TradingState) -> TradingState
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

from langgraph.graph import END, StateGraph

from agents.ceo_agent import CEOAgent
from agents.learning_officer_agent import LearningOfficerAgent
from agents.market_intelligence_agent import MarketIntelligenceAgent
from agents.operations_agent import OperationsAgent
from agents.position_manager_agent import PositionManagerAgent
from agents.risk_officer_agent import RiskOfficerAgent
from agents.scenario_planner_agent import ScenarioPlannerAgent
from agents.trading_strategy_agent import TradingStrategyAgent
from config import get_settings
from core.state import TradingState, ValidationStatus
from data.market_data import MarketDataFetcher
from logging_.audit_logger import AuditLogger
from safety.safety_layer import SafetyLayer

logger = logging.getLogger(__name__)
settings = get_settings()

# ============================================================
# Department Agent Registry
# ============================================================

DEPARTMENT_AGENTS = [
    TradingStrategyAgent(),
    MarketIntelligenceAgent(),
    ScenarioPlannerAgent(),
    RiskOfficerAgent(),
    PositionManagerAgent(),
    LearningOfficerAgent(),
    OperationsAgent(),
]


# ============================================================
# LangGraph Nodes
# ============================================================

class WorkflowNodes:
    """
    Encapsulates all LangGraph node functions with their dependencies.
    ทุก node รับ TradingState และ return dict (partial update)
    """

    def __init__(
        self,
        market_fetcher: MarketDataFetcher,
        safety_layer: SafetyLayer,
        ceo_agent: CEOAgent,
        audit_logger: AuditLogger,
        timeframe: str = "H1",
    ) -> None:
        self._fetcher    = market_fetcher
        self._safety     = safety_layer
        self._ceo        = ceo_agent
        self._audit      = audit_logger
        self._timeframe  = timeframe

    # ──────────────────────────────────────────────────────────────
    # NODE 1: build_context
    # ──────────────────────────────────────────────────────────────

    async def build_context(self, state: TradingState) -> dict:
        """
        Step 1 — ดึงข้อมูลตลาด, account, positions
        ถ้าข้อมูลไม่ได้ → set context_error → Safety layer จะ HOLD
        """
        start = time.monotonic() * 1000
        trace_id = str(uuid.uuid4())
        updates: dict = {
            "trace_id": trace_id,
            "node_timings": {},
            "error_log": [],
        }

        logger.info(
            f"[Step1] build_context | trace={trace_id} "
            f"symbol={state.get('symbol')} event={state.get('event_type')}"
        )

        # บันทึก workflow start ลง DB
        await self._audit.log_workflow_start({**state, **updates})

        try:
            symbol = state.get("symbol", "EURUSD")

            # Market snapshot
            snapshot = self._fetcher.get_snapshot(symbol, self._timeframe)
            updates["market_snapshot"] = snapshot
            updates["context_error"] = None

            # Account info
            updates["account_info"] = self._fetcher.get_account_info()

            # Open positions
            updates["open_positions"] = self._fetcher.get_open_positions(symbol)

        except Exception as exc:
            logger.error(f"[Step1] Context error: {exc}")
            updates["context_error"] = str(exc)
            updates["error_log"] = [f"context: {exc}"]

        updates["node_timings"] = {"build_context": time.monotonic() * 1000 - start}
        return updates

    # ──────────────────────────────────────────────────────────────
    # NODE 2: run_departments
    # ──────────────────────────────────────────────────────────────

    async def run_departments(self, state: TradingState) -> dict:
        """
        Step 2 — รัน 7 Department Agents พร้อมกันด้วย asyncio.gather()
        ทุก agent ที่ error → return HOLD report (ไม่ crash ทั้งระบบ)
        """
        start = time.monotonic() * 1000
        logger.info(f"[Step2] run_departments | trace={state.get('trace_id')}")

        # รัน agents พร้อมกัน (parallel asyncio)
        # Ollama ประมวลผลทีละ 1 request บน GPU เดียว → queue อัตโนมัติ
        # timeout ต่อ agent = 180s (7 agents × ~25s/agent ≈ 175s total max)
        AGENT_TIMEOUT = 180.0
        tasks = [
            asyncio.create_task(
                asyncio.wait_for(agent.analyze(state), timeout=AGENT_TIMEOUT),
                name=agent.agent_name,
            )
            for agent in DEPARTMENT_AGENTS
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        department_reports = {}
        errors = []

        for agent, result in zip(DEPARTMENT_AGENTS, results):
            if isinstance(result, Exception):
                logger.error(f"[Step2] {agent.agent_name} failed: {result}")
                errors.append(f"{agent.agent_name}: {result}")
                department_reports[agent.agent_name] = {
                    "agent_name": agent.agent_name,
                    "status": "timeout" if isinstance(result, asyncio.TimeoutError) else "error",
                    "recommendation": "HOLD",
                    "confidence": 0.0,
                    "reasoning": f"Agent failed: {result}",
                    "key_points": [],
                    "raw_llm_output": "",
                    "latency_ms": 60000.0,
                }
            else:
                department_reports[agent.agent_name] = result

        # Log ลง DB
        await self._audit.log_agent_reports({
            **state,
            "department_reports": department_reports,
        })

        elapsed = time.monotonic() * 1000 - start
        logger.info(
            f"[Step2] Done | {len(department_reports)} reports | "
            f"errors={len(errors)} | lat={elapsed:.0f}ms"
        )

        return {
            "department_reports": department_reports,
            "departments_error": "; ".join(errors) if errors else None,
            "node_timings": {
                **state.get("node_timings", {}),
                "run_departments": elapsed,
            },
            "error_log": state.get("error_log", []) + errors,
        }

    # ──────────────────────────────────────────────────────────────
    # NODE 3: ceo_synthesize
    # ──────────────────────────────────────────────────────────────

    async def ceo_synthesize(self, state: TradingState) -> dict:
        """
        Step 3 — CEO Agent synthesizes department reports → final decision
        """
        start = time.monotonic() * 1000
        logger.info(f"[Step3] ceo_synthesize | trace={state.get('trace_id')}")

        updated_state = await self._ceo.decide(state)

        # Log CEO decision
        await self._audit.log_ceo_decision(updated_state)

        elapsed = time.monotonic() * 1000 - start
        return {
            "ceo_decision": updated_state.get("ceo_decision"),
            "ceo_error": updated_state.get("ceo_error"),
            "node_timings": {
                **state.get("node_timings", {}),
                "ceo_synthesize": elapsed,
            },
        }

    # ──────────────────────────────────────────────────────────────
    # NODE 4: safety_validate
    # ──────────────────────────────────────────────────────────────

    async def safety_validate(self, state: TradingState) -> dict:
        """
        Step 4 — Deterministic Safety Layer → Lot Calculation → Execute / HOLD
        """
        start = time.monotonic() * 1000
        logger.info(f"[Step4] safety_validate | trace={state.get('trace_id')}")

        # Safety checks (Deterministic — NO LLM)
        validated_state = await self._safety.validate(state)

        elapsed = time.monotonic() * 1000 - start
        total_duration = (
            elapsed
            + sum(validated_state.get("node_timings", {}).values())
        )

        result_updates = {
            "validation_result": validated_state.get("validation_result"),
            "trade_params": validated_state.get("trade_params"),
            "final_action": validated_state.get("final_action", "HOLD"),
            "workflow_duration_ms": total_duration,
            "node_timings": {
                **state.get("node_timings", {}),
                "safety_validate": elapsed,
            },
        }

        # Execute trade ถ้า PASS
        final_action = result_updates["final_action"]
        validation_status = validated_state.get("validation_result", {}).get("status")

        if validation_status == ValidationStatus.PASS and final_action in ("BUY", "SELL"):
            broker_result = await self._execute_trade(validated_state)
            result_updates["execution_result"] = broker_result
            await self._audit.log_execution(validated_state, broker_result)
        else:
            logger.info(
                f"[Step4] No trade executed | "
                f"status={validation_status} action={final_action}"
            )
            await self._audit.log_execution(validated_state, None)

        # Finalize audit log
        await self._audit.finalize_run({**state, **result_updates})

        logger.info(
            f"[Step4] Complete | action={final_action} | "
            f"total={total_duration:.0f}ms"
        )
        return result_updates

    # ──────────────────────────────────────────────────────────────
    # Trade Execution (MT5)
    # ──────────────────────────────────────────────────────────────

    async def _execute_trade(self, state: TradingState) -> dict:
        """ส่งคำสั่งเทรดไปยัง MT5 broker (XM)"""
        try:
            import MetaTrader5 as mt5

            params = state["trade_params"]
            action = state["final_action"]
            symbol = state["symbol"]

            order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL

            request = {
                "action":      mt5.TRADE_ACTION_DEAL,
                "symbol":      symbol,
                "volume":      params["lot_size"],
                "type":        order_type,
                "price":       mt5.symbol_info_tick(symbol).ask if action == "BUY"
                               else mt5.symbol_info_tick(symbol).bid,
                "sl":          params["sl_price"],
                "tp":          params["tp_price"],
                "deviation":   20,
                "magic":       20240001,  # Magic number ระบุว่าเป็น bot นี้
                "comment":     f"AGT:{state.get('trace_id', '')[:8]}",
                "type_time":   mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result is None:
                return {"error": "MT5 order_send returned None", "retcode": -1}

            logger.info(
                f"[Execute] MT5 result: retcode={result.retcode} "
                f"order={result.order} comment={result.comment}"
            )

            return {
                "retcode": result.retcode,
                "retcode_name": result.retcode_name if hasattr(result, 'retcode_name') else str(result.retcode),
                "order": result.order,
                "deal": result.deal if hasattr(result, 'deal') else None,
                "volume": result.volume if hasattr(result, 'volume') else params["lot_size"],
                "price": result.price if hasattr(result, 'price') else 0.0,
                "comment": result.comment,
                "symbol": symbol,
                "action": action,
            }

        except ImportError:
            # Development mode ไม่มี MT5
            logger.warning("[Execute] MT5 not available — simulated execution")
            return {
                "retcode": 10009,
                "retcode_name": "TRADE_RETCODE_DONE (simulated)",
                "order": 99999,
                "symbol": state.get("symbol"),
                "action": state.get("final_action"),
                "volume": state.get("trade_params", {}).get("lot_size"),
                "simulated": True,
            }

        except Exception as exc:
            logger.error(f"[Execute] Error: {exc}")
            return {"error": str(exc), "retcode": -1}


# ============================================================
# LangGraph Workflow Builder
# ============================================================

def build_workflow(
    market_fetcher: MarketDataFetcher,
    safety_layer: SafetyLayer,
    ceo_agent: CEOAgent,
    audit_logger: AuditLogger,
    timeframe: str = "H1",
) -> StateGraph:
    """
    สร้าง LangGraph StateGraph สำหรับ 4-step workflow
    
    Graph:
        START → build_context → run_departments → ceo_synthesize → safety_validate → END
    """
    nodes = WorkflowNodes(
        market_fetcher=market_fetcher,
        safety_layer=safety_layer,
        ceo_agent=ceo_agent,
        audit_logger=audit_logger,
        timeframe=timeframe,
    )

    # สร้าง StateGraph ด้วย TradingState
    workflow = StateGraph(TradingState)

    # เพิ่ม Nodes
    workflow.add_node("build_context",   nodes.build_context)
    workflow.add_node("run_departments", nodes.run_departments)
    workflow.add_node("ceo_synthesize",  nodes.ceo_synthesize)
    workflow.add_node("safety_validate", nodes.safety_validate)

    # เพิ่ม Edges (เส้นตรง — Sequential)
    workflow.set_entry_point("build_context")
    workflow.add_edge("build_context",   "run_departments")
    workflow.add_edge("run_departments", "ceo_synthesize")
    workflow.add_edge("ceo_synthesize",  "safety_validate")
    workflow.add_edge("safety_validate", END)

    return workflow.compile()


# ============================================================
# Orchestrator (Facade)
# ============================================================

class TradingOrchestrator:
    """
    Main orchestrator class — interface กลางสำหรับ event bus และ external calls
    
    Usage:
        orch = TradingOrchestrator(...)
        await orch.initialize()
        await orch.handle_event(trading_event)
    """

    def __init__(
        self,
        market_fetcher: MarketDataFetcher,
        safety_layer: SafetyLayer,
        ceo_agent: CEOAgent,
        audit_logger: AuditLogger,
        symbols: list[str],
        timeframe: str = "H1",
    ) -> None:
        self._fetcher  = market_fetcher
        self._safety   = safety_layer
        self._ceo      = ceo_agent
        self._audit    = audit_logger
        self._symbols  = symbols
        self._graph    = build_workflow(
            market_fetcher, safety_layer, ceo_agent, audit_logger, timeframe
        )

    async def initialize(self, simulate: bool = False) -> None:
        """Bootstrap: เชื่อมต่อ MT5, สร้าง DB schema"""
        self._fetcher.initialize(simulate=simulate)
        await self._audit.initialize_schema()
        logger.info(f"[Orchestrator] Initialized and ready | simulate={simulate}")

    async def handle_event(self, event) -> TradingState:
        """
        Entry point สำหรับ EventBus
        รับ TradingEvent → สร้าง initial state → รัน LangGraph workflow
        """
        if event.symbol not in self._symbols:
            logger.debug(f"[Orchestrator] Skipping {event.symbol} — not in watchlist")
            return {}

        initial_state: TradingState = {
            "event_type":    event.event_type,
            "symbol":        event.symbol,
            "triggered_at":  event.triggered_at,
            "idempotency_key": event.idempotency_key,
            "trace_id":      str(uuid.uuid4()),
            "error_log":     [],
            "node_timings":  {},
        }

        logger.info(
            f"[Orchestrator] Running workflow | "
            f"event={event.event_type} symbol={event.symbol}"
        )

        try:
            final_state = await self._graph.ainvoke(initial_state)
            logger.info(
                f"[Orchestrator] Workflow complete | "
                f"action={final_state.get('final_action')} | "
                f"duration={final_state.get('workflow_duration_ms', 0):.0f}ms"
            )
            return final_state
        except Exception as exc:
            logger.exception(f"[Orchestrator] Workflow error: {exc}")
            return {**initial_state, "error_log": [str(exc)], "final_action": "HOLD"}
