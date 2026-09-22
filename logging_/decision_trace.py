"""logging_/decision_trace.py — Human-readable decision trace"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def print_decision_trace(state: dict) -> str:
    """
    สรุปผลการตัดสินใจทั้ง workflow เป็น human-readable string
    ใช้สำหรับ console output และ log file
    """
    lines = [
        "=" * 65,
        f"  DECISION TRACE | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 65,
        f"  Symbol:       {state.get('symbol')}",
        f"  Event:        {state.get('event_type')}",
        f"  Trace ID:     {state.get('trace_id', '')[:16]}...",
        f"  Duration:     {state.get('workflow_duration_ms', 0):.0f}ms",
        "",
        "DEPARTMENT VOTES:",
    ]

    reports = state.get("department_reports", {})
    for name, r in reports.items():
        status = "[OK]" if r.get("status") == "ok" else "[ERR]"
        lines.append(
            f"  {status} {name:<22} → {r.get('recommendation'):<5} "
            f"(conf={r.get('confidence', 0):.2f}, {r.get('latency_ms', 0):.0f}ms)"
        )

    ceo = state.get("ceo_decision", {})
    lines += [
        "",
        "CEO DECISION:",
        f"  Action:       {ceo.get('action')}",
        f"  Confidence:   {ceo.get('confidence', 0):.2f}",
        f"  Consensus:    {ceo.get('consensus_score', 0):.2f}",
        f"  Dissenting:   {ceo.get('dissenting_agents', [])}",
        f"  SL/TP Pips:   {ceo.get('sl_pips')}/{ceo.get('tp_pips')}",
        "",
        "SAFETY VALIDATION:",
    ]

    val = state.get("validation_result", {})
    lines += [
        f"  Status:       {val.get('status')}",
        f"  Reason:       {val.get('reason', '')}",
        f"  Checks Pass:  {val.get('checks_passed', [])}",
        f"  Checks Fail:  {val.get('checks_failed', [])}",
    ]

    params = state.get("trade_params")
    if params:
        lines += [
            "",
            "TRADE PARAMETERS (Python-calculated):",
            f"  Lot Size:     {params.get('lot_size')}",
            f"  SL Price:     {params.get('sl_price')}",
            f"  TP Price:     {params.get('tp_price')}",
            f"  Risk %:       {params.get('risk_pct', 0)*100:.2f}%",
            f"  Risk Amount:  ${params.get('risk_amount', 0):.2f}",
        ]

    lines.append("=" * 65)
    trace = "\n".join(lines)
    logger.info(f"\n{trace}")
    return trace
