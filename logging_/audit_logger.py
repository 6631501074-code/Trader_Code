"""
logging_/audit_logger.py — PostgreSQL Audit Logger
====================================================
บันทึกทุก workflow execution ลง PostgreSQL
เก็บ: trace_id, decision chain, ทุก agent report, timing, และผล execution
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# DB Schema DDL
# ============================================================

SCHEMA_DDL = """
-- Main workflow run table
CREATE TABLE IF NOT EXISTS workflow_runs (
    trace_id          UUID PRIMARY KEY,
    idempotency_key   VARCHAR(64) UNIQUE,
    symbol            VARCHAR(20) NOT NULL,
    event_type        VARCHAR(50) NOT NULL,
    triggered_at      TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    duration_ms       FLOAT,
    final_action      VARCHAR(10),
    validation_status VARCHAR(10),
    error_log         JSONB DEFAULT '[]',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Department agent reports
CREATE TABLE IF NOT EXISTS agent_reports (
    id                BIGSERIAL PRIMARY KEY,
    trace_id          UUID REFERENCES workflow_runs(trace_id),
    agent_name        VARCHAR(50) NOT NULL,
    recommendation    VARCHAR(10),
    confidence        FLOAT,
    reasoning         TEXT,
    key_points        JSONB DEFAULT '[]',
    status            VARCHAR(20),
    latency_ms        FLOAT,
    raw_llm_output    TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- CEO decisions
CREATE TABLE IF NOT EXISTS ceo_decisions (
    id                BIGSERIAL PRIMARY KEY,
    trace_id          UUID REFERENCES workflow_runs(trace_id),
    action            VARCHAR(10) NOT NULL,
    confidence        FLOAT,
    consensus_score   FLOAT,
    dissenting_agents JSONB DEFAULT '[]',
    reasoning         TEXT,
    sl_pips           FLOAT,
    tp_pips           FLOAT,
    entry_price       FLOAT,
    raw_llm_output    TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Trade executions
CREATE TABLE IF NOT EXISTS trade_executions (
    id                BIGSERIAL PRIMARY KEY,
    trace_id          UUID REFERENCES workflow_runs(trace_id),
    symbol            VARCHAR(20),
    action            VARCHAR(10),
    lot_size          FLOAT,
    entry_price       FLOAT,
    sl_price          FLOAT,
    tp_price          FLOAT,
    risk_pct          FLOAT,
    risk_amount       FLOAT,
    ticket            BIGINT,
    execution_status  VARCHAR(20),
    broker_response   JSONB,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Idempotency keys (moved here from safety module)
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key               VARCHAR(64) PRIMARY KEY,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_runs_symbol       ON workflow_runs (symbol);
CREATE INDEX IF NOT EXISTS idx_runs_action       ON workflow_runs (final_action);
CREATE INDEX IF NOT EXISTS idx_runs_created_at   ON workflow_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_reports_trace     ON agent_reports (trace_id);
CREATE INDEX IF NOT EXISTS idx_idem_created_at   ON idempotency_keys (created_at);
"""


# ============================================================
# AuditLogger class
# ============================================================

class AuditLogger:
    """
    Async PostgreSQL Audit Logger
    บันทึกทุก step ของ workflow สำหรับ post-analysis และ debugging
    
    Usage:
        logger = AuditLogger(db_pool)
        await logger.initialize_schema()
        trace_id = await logger.log_workflow_start(state)
        await logger.log_agent_reports(state)
        await logger.log_ceo_decision(state)
        await logger.log_execution(state, broker_result)
        await logger.finalize_run(state)
    """

    def __init__(self, db_pool=None) -> None:
        self._pool = db_pool

    async def initialize_schema(self) -> None:
        """สร้าง tables ถ้ายังไม่มี"""
        if not self._pool:
            logger.warning("[AuditLogger] No DB pool — logging disabled")
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(SCHEMA_DDL)
            logger.info("[AuditLogger] Schema initialized")
        except Exception as exc:
            logger.error(f"[AuditLogger] Schema init failed: {exc}")

    async def log_workflow_start(self, state: dict) -> str:
        """บันทึก workflow run เมื่อเริ่มต้น — return trace_id"""
        trace_id = state.get("trace_id") or str(uuid.uuid4())
        if not self._pool:
            return trace_id

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO workflow_runs
                        (trace_id, idempotency_key, symbol, event_type, triggered_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (trace_id) DO NOTHING
                    """,
                    trace_id,
                    state.get("idempotency_key"),
                    state.get("symbol"),
                    state.get("event_type"),
                    _parse_ts(state.get("triggered_at")),
                )
        except Exception as exc:
            logger.error(f"[AuditLogger] log_workflow_start error: {exc}")

        return trace_id

    async def log_agent_reports(self, state: dict) -> None:
        """บันทึก department reports ทั้งหมด"""
        if not self._pool:
            return

        trace_id = state.get("trace_id")
        reports = state.get("department_reports", {})

        try:
            async with self._pool.acquire() as conn:
                for agent_name, report in reports.items():
                    await conn.execute(
                        """
                        INSERT INTO agent_reports
                            (trace_id, agent_name, recommendation, confidence,
                             reasoning, key_points, status, latency_ms, raw_llm_output)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        """,
                        trace_id,
                        report.get("agent_name", agent_name),
                        report.get("recommendation"),
                        report.get("confidence"),
                        report.get("reasoning"),
                        json.dumps(report.get("key_points", [])),
                        report.get("status"),
                        report.get("latency_ms"),
                        report.get("raw_llm_output", "")[:5000],  # ตัดถ้ายาวเกิน
                    )
        except Exception as exc:
            logger.error(f"[AuditLogger] log_agent_reports error: {exc}")

    async def log_ceo_decision(self, state: dict) -> None:
        """บันทึก CEO decision"""
        if not self._pool:
            return

        ceo = state.get("ceo_decision", {})
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ceo_decisions
                        (trace_id, action, confidence, consensus_score,
                         dissenting_agents, reasoning, sl_pips, tp_pips,
                         entry_price, raw_llm_output)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    """,
                    state.get("trace_id"),
                    ceo.get("action"),
                    ceo.get("confidence"),
                    ceo.get("consensus_score"),
                    json.dumps(ceo.get("dissenting_agents", [])),
                    ceo.get("reasoning"),
                    ceo.get("sl_pips"),
                    ceo.get("tp_pips"),
                    ceo.get("entry_price"),
                    ceo.get("raw_llm_output", "")[:5000],
                )
        except Exception as exc:
            logger.error(f"[AuditLogger] log_ceo_decision error: {exc}")

    async def log_execution(self, state: dict, broker_response: Optional[dict] = None) -> None:
        """บันทึกผลการ execute trade"""
        if not self._pool:
            return

        params = state.get("trade_params", {})
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trade_executions
                        (trace_id, symbol, action, lot_size, entry_price,
                         sl_price, tp_price, risk_pct, risk_amount,
                         ticket, execution_status, broker_response)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    """,
                    state.get("trace_id"),
                    state.get("symbol"),
                    state.get("final_action"),
                    params.get("lot_size"),
                    params.get("sl_price"),
                    params.get("sl_price"),
                    params.get("tp_price"),
                    params.get("risk_pct"),
                    params.get("risk_amount"),
                    broker_response.get("order") if broker_response else None,
                    broker_response.get("retcode_name", "HOLD") if broker_response else "HOLD",
                    json.dumps(broker_response or {}),
                )
        except Exception as exc:
            logger.error(f"[AuditLogger] log_execution error: {exc}")

    async def finalize_run(self, state: dict) -> None:
        """Update workflow_runs เมื่อ workflow เสร็จสิ้น"""
        if not self._pool:
            return

        validation = state.get("validation_result", {})
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE workflow_runs SET
                        completed_at      = NOW(),
                        duration_ms       = $2,
                        final_action      = $3,
                        validation_status = $4,
                        error_log         = $5
                    WHERE trace_id = $1
                    """,
                    state.get("trace_id"),
                    state.get("workflow_duration_ms"),
                    state.get("final_action", "HOLD"),
                    validation.get("status"),
                    json.dumps(state.get("error_log", [])),
                )
        except Exception as exc:
            logger.error(f"[AuditLogger] finalize_run error: {exc}")


# ============================================================
# DB Connection Pool Factory
# ============================================================

async def create_db_pool(database_url: str):
    """สร้าง asyncpg connection pool"""
    try:
        import asyncpg
        # แปลง sqlalchemy URL เป็น asyncpg format
        url = database_url.replace("postgresql+asyncpg://", "postgresql://")
        pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("[DB] PostgreSQL pool created")
        return pool
    except ImportError:
        logger.warning("[DB] asyncpg not installed — DB logging disabled")
        return None
    except Exception as exc:
        logger.error(f"[DB] Pool creation failed: {exc}")
        return None


def _parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """แปลง ISO string เป็น datetime"""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
