"""
agents/ceo_agent.py — CEO Agent: Debate Synthesis & Final Decision
==================================================================
รับรายงานจากทุก Department → ไกล่เกลี่ยความขัดแย้ง → ตัดสินใจขั้นสุดท้าย
Note: CEO ไม่คำนวณ Lot size — แค่กำหนด sl_pips, tp_pips, entry_price
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage

from agents.base_agent import BaseAgent
from config import get_settings
from core.state import CEODecision, DepartmentReport, TradingState
from safety.json_repair import CEO_DECISION_SCHEMA, repair_json_output, validate_schema

logger = logging.getLogger(__name__)
settings = get_settings()


CEO_SYSTEM_PROMPT = """You are the CEO of an elite proprietary trading firm.
You receive reports from 7 specialized department heads and make the FINAL trading decision.

Your decision-making process:
1. READ all department reports carefully
2. Identify CONSENSUS vs DISSENT among departments
3. Weight departments: Risk Officer VETO overrides all (HOLD if Risk says HOLD)
4. Consider: Operations HOLD = execution impossible, override all
5. Synthesize the majority view while addressing minority concerns
6. Set precise SL and TP levels based on the analysis (in PIPS, not dollars)
7. Provide your confidence and reasoning

Department Weights (for consensus scoring):
- TradingStrategy: 25% (primary technical signal)
- MarketIntelligence: 20% (macro context)
- ScenarioPlanner: 15% (big picture alignment)
- RiskOfficer: 20% (VETO power if HOLD)
- PositionManager: 10% (portfolio management)
- LearningOfficer: 5% (historical bias)
- Operations: 5% (VETO power if HOLD for execution issues)

CRITICAL:
- If Risk Officer OR Operations recommends HOLD: YOUR decision MUST be HOLD
- NEVER calculate lot sizes, dollar amounts, or risk percentages
- SL and TP must be in PIPS (positive numbers)
- entry_price should be the current bid/ask (for BUY: current ask, SELL: current bid)
- confidence must reflect true certainty (not inflated)

Respond ONLY with valid JSON:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "consensus_score": 0.0-1.0,
  "dissenting_agents": ["list of dissenting agent names"],
  "reasoning": "comprehensive synthesis and rationale",
  "sl_pips": 20.0,
  "tp_pips": 40.0,
  "entry_price": 1.23456
}"""


class CEOAgent:
    """
    CEO Agent — รับ 7 Department Reports → Final Decision
    (ใช้ base agent infrastructure แต่ไม่ extend เพราะ output schema ต่างกัน)
    """

    _llm: Optional[ChatOllama] = None
    _llm_lock = asyncio.Lock()

    async def decide(self, state: TradingState) -> TradingState:
        """
        Main entry point — synthesize dept reports → return updated state
        """
        start_ms = time.monotonic() * 1000

        try:
            # สร้าง prompt จาก department reports
            user_prompt = self._build_synthesis_prompt(state)
            
            # เรียก LLM
            raw_output = await self._call_llm(user_prompt)
            
            # Parse + Validate
            ceo_decision = self._parse_ceo_output(raw_output, state)

            latency = time.monotonic() * 1000 - start_ms
            logger.info(
                f"[CEO] Decision: {ceo_decision['action']} | "
                f"confidence={ceo_decision['confidence']:.2f} | "
                f"consensus={ceo_decision['consensus_score']:.2f} | "
                f"lat={latency:.0f}ms"
            )

            updated = dict(state)
            updated["ceo_decision"] = ceo_decision
            updated["ceo_error"] = None
            return updated  # type: ignore

        except Exception as exc:
            latency = time.monotonic() * 1000 - start_ms
            logger.error(f"[CEO] Error: {exc}")
            updated = dict(state)
            updated["ceo_decision"] = CEODecision(
                action="HOLD",
                confidence=0.0,
                consensus_score=0.0,
                dissenting_agents=[],
                reasoning=f"CEO agent error: {exc}",
                sl_pips=0.0,
                tp_pips=0.0,
                entry_price=0.0,
                raw_llm_output="",
            )
            updated["ceo_error"] = str(exc)
            return updated  # type: ignore

    def _build_synthesis_prompt(self, state: TradingState) -> str:
        """สร้าง prompt รวมรายงานทุกแผนก"""
        reports = state.get("department_reports", {})
        symbol = state.get("symbol", "")
        snap = state.get("market_snapshot", {})
        
        dept_text = ""
        for agent_name, report in reports.items():
            status_emoji = "[OK]" if report.get("status") == "ok" else "[ERR]"
            dept_text += f"""
--- {status_emoji} {agent_name} ---
Recommendation: {report.get('recommendation')} (confidence: {report.get('confidence', 0):.2f})
Reasoning: {report.get('reasoning', 'N/A')}
Key Points:
"""
            for pt in report.get("key_points", []):
                dept_text += f"  • {pt}\n"

        # Compute simple vote tally
        votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for r in reports.values():
            rec = r.get("recommendation", "HOLD").upper()
            votes[rec] = votes.get(rec, 0) + 1

        return f"""BOARD MEETING — TRADING DECISION REQUIRED

INSTRUMENT: {symbol}
Current Price: Bid={snap.get('bid')} Ask={snap.get('ask')}
Spread: {snap.get('spread')} pts | ATR14: {snap.get('atr_14')}
Session: {snap.get('session')}

DEPARTMENT VOTE TALLY:
  BUY: {votes['BUY']} | SELL: {votes['SELL']} | HOLD: {votes['HOLD']}

DEPARTMENT REPORTS:
{dept_text}

CEO TASK:
1. Review all 7 department reports above
2. Apply the department weights to compute consensus_score
3. If Risk Officer OR Operations says HOLD → your action MUST be HOLD
4. Provide sl_pips and tp_pips as positive pip distances (NOT prices)
5. Set entry_price to current {'ask' if votes['BUY'] >= votes['SELL'] else 'bid'}: {snap.get('ask' if votes['BUY'] >= votes['SELL'] else 'bid')}

Respond with your final board decision JSON:"""

    def _parse_ceo_output(self, raw: str, state: TradingState) -> CEODecision:
        """Parse CEO LLM output → CEODecision TypedDict"""
        try:
            parsed, attempts = repair_json_output(
                raw_text=raw,
                required_keys=CEO_DECISION_SCHEMA["required"],
                max_retries=settings.json_repair_max_retries,
            )
            errors = validate_schema(parsed, CEO_DECISION_SCHEMA)
            if errors:
                raise ValueError(f"Schema: {errors}")

            # Check Risk/Operations VETO
            reports = state.get("department_reports", {})
            risk_report = reports.get("RiskOfficer", {})
            ops_report = reports.get("Operations", {})

            veto_reason = None
            if risk_report.get("recommendation") == "HOLD":
                veto_reason = f"RiskOfficer VETO: {risk_report.get('reasoning', '')[:100]}"
            elif ops_report.get("recommendation") == "HOLD":
                veto_reason = f"Operations VETO: {ops_report.get('reasoning', '')[:100]}"

            action = "HOLD" if veto_reason else parsed["action"].upper()
            reasoning = veto_reason if veto_reason else parsed["reasoning"]

            return CEODecision(
                action=action,
                confidence=max(0.0, min(1.0, float(parsed["confidence"]))),
                consensus_score=max(0.0, min(1.0, float(parsed.get("consensus_score", 0.5)))),
                dissenting_agents=list(parsed.get("dissenting_agents", [])),
                reasoning=reasoning,
                sl_pips=abs(float(parsed["sl_pips"])),
                tp_pips=abs(float(parsed["tp_pips"])),
                entry_price=float(parsed["entry_price"]),
                raw_llm_output=raw,
            )

        except (ValueError, KeyError) as e:
            logger.error(f"[CEO] Parse error: {e}")
            snap = state.get("market_snapshot", {})
            return CEODecision(
                action="HOLD",
                confidence=0.0,
                consensus_score=0.0,
                dissenting_agents=[],
                reasoning=f"CEO parse error — defaulting HOLD: {e}",
                sl_pips=0.0,
                tp_pips=0.0,
                entry_price=float(snap.get("bid", 0)),
                raw_llm_output=raw,
            )

    async def _call_llm(self, prompt: str) -> str:
        """เรียก Gemma3 ผ่าน Ollama"""
        llm = await self._get_llm()
        messages = [
            SystemMessage(content=CEO_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        response = await llm.ainvoke(messages)
        return response.content

    @classmethod
    async def _get_llm(cls) -> ChatOllama:
        if cls._llm is None:
            async with cls._llm_lock:
                if cls._llm is None:
                    cls._llm = ChatOllama(
                        model=settings.ollama_model,
                        base_url=settings.ollama_base_url,
                        temperature=settings.llm_temperature * 0.5,  # CEO ใช้ temp ต่ำกว่า = decisive
                        num_predict=settings.llm_max_tokens,
                    )
        return cls._llm
