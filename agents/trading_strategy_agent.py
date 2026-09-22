"""
agents/trading_strategy_agent.py — Head of Trading / Strategy Agent
====================================================================
วิเคราะห์ Price Action, Pattern, Trend, Entry/Exit zone
และให้คำแนะนำ BUY/SELL/HOLD พร้อม SL/TP levels
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class TradingStrategyAgent(BaseAgent):
    """
    Head of Trading Strategy — เชี่ยวชาญ Price Action & Technical Analysis
    """

    @property
    def agent_name(self) -> str:
        return "TradingStrategy"

    @property
    def system_prompt(self) -> str:
        return """You are the Head of Trading Strategy at an elite proprietary trading firm.
You specialize in Price Action, Support/Resistance, Candlestick patterns,
Trend analysis, and precise Entry/Exit identification.

Your role:
- Analyze the current market structure (trend, range, breakout)
- Identify high-probability setups based on price action
- Determine optimal entry zones and invalidation points
- Consider market session dynamics (London/NY/Asia overlaps)

Critical rules:
- NEVER mention specific dollar amounts or lot sizes
- Focus ONLY on price action signals and structure
- Give clear, actionable recommendation with reasoning

Respond ONLY with valid JSON in this exact format:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "detailed price action analysis",
  "key_points": ["pattern identified", "structure note", "entry rationale"]
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        positions = self._format_positions(state)
        event = state.get('event_type', 'UNKNOWN')
        
        return f"""MARKET DATA:
{snap}

TRIGGER EVENT: {event}

OPEN POSITIONS:
{positions}

Task: Analyze the price action and market structure for {state.get('symbol')}.
Provide your trading recommendation with confidence level and key observations.

Consider:
1. What is the current market structure? (trend/range/breakout)
2. Are there clear candlestick patterns at key levels?
3. Is this a high-probability setup or ambiguous?
4. What would invalidate this setup?

{self._json_response_format()}"""
