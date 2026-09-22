"""
agents/position_manager_agent.py — Position Manager Agent
=========================================================
ติดตาม Open Positions, ประเมิน Exit Scenarios, Trailing SL
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class PositionManagerAgent(BaseAgent):
    """
    Position Manager — Open Trade Management & Exit Strategy
    """

    @property
    def agent_name(self) -> str:
        return "PositionManager"

    @property
    def system_prompt(self) -> str:
        return """You are the Head of Position Management at a professional trading desk.
You monitor and manage existing open positions and evaluate whether new entries
are appropriate given current portfolio context.

Your role:
- Review all open positions and their current P&L status
- Identify if any positions need to be closed before opening new ones
- Evaluate if a new trade would be redundant or beneficial
- Recommend Trailing SL adjustments if applicable
- Assess if current positions are at risk of being stopped out
- Consider correlation with proposed new trade

Critical rules:
- NEVER calculate lot sizes or dollar amounts
- Focus on trade management decisions, not entry signals
- If open positions are in strong drawdown, recommend HOLD for new trades

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "position management analysis",
  "key_points": ["portfolio status", "trade interaction", "management action"],
  "action_required": "none" | "close_existing" | "adjust_sl" | "scale_in"
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        positions = self._format_positions(state)
        symbol = state.get('symbol', '')
        action_event = state.get('event_type', '')
        
        return f"""POSITION MANAGEMENT REVIEW

PROPOSED NEW TRADE: {symbol} (Event: {action_event})

CURRENT MARKET:
{snap}

OPEN POSITIONS:
{positions}

Task: Evaluate position portfolio and advise on new trade appropriateness.

Consider:
1. How many positions are currently open on {symbol}?
2. Are any open positions in significant profit/loss? Should we manage them first?
3. Would a new {symbol} trade be correlated or hedging with existing positions?
4. Is the portfolio overloaded or has capacity for a new trade?
5. Any positions that should be closed before adding a new one?

Note: If same-direction position already open on same symbol with profit, 
consider recommending HOLD to avoid pyramiding excessively.

{self._json_response_format()}"""
