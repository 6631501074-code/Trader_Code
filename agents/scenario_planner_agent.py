"""
agents/scenario_planner_agent.py — Scenario Planner Agent
=========================================================
สร้างแผนการเทรด Monthly -> Weekly -> Daily
และตรวจสอบว่า setup ปัจจุบัน align กับ higher timeframe plan
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class ScenarioPlannerAgent(BaseAgent):
    """
    Scenario Planner — Top-Down Analysis & Contextual Alignment
    Monthly → Weekly → Daily → Intraday
    """

    @property
    def agent_name(self) -> str:
        return "ScenarioPlanner"

    @property
    def system_prompt(self) -> str:
        return """You are the Scenario Planning Director at a systematic trading fund.
You specialize in top-down analysis and ensuring every trade aligns with
the broader market narrative across multiple timeframes.

Your role:
- Maintain awareness of Monthly/Weekly trend direction
- Create and track scenario plans (Bull/Bear/Base case)
- Ensure intraday setups align with higher timeframe context
- Identify trade sequences and where we are in the market cycle
- Flag if current setup conflicts with the big picture

Critical rules:
- NEVER suggest position sizes or dollar amounts
- Think in terms of market cycles, swing highs/lows, and narrative
- Clearly state if we are trading WITH or AGAINST the higher TF trend

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "multi-timeframe scenario analysis",
  "key_points": ["higher TF alignment", "scenario context", "cycle position"]
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        symbol = state.get('symbol', '')
        
        return f"""INSTRUMENT: {symbol}

CURRENT TIMEFRAME DATA:
{snap}

Task: Evaluate if the current setup aligns with higher timeframe plans.

Consider:
1. What is the Monthly/Weekly trend direction for {symbol}?
2. Are we in a trending market or a range-bound consolidation?
3. Does this intraday setup align with the higher TF narrative? (WITH/AGAINST trend)
4. What stage of the market cycle are we in? (expansion/retracement/reversal)
5. Is there a logical scenario sequence that supports this trade?
6. What is the primary risk to this scenario?

Note: If uncertain about higher TF context, recommend HOLD.

{self._json_response_format()}"""
