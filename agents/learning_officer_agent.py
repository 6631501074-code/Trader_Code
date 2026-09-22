"""
agents/learning_officer_agent.py — Learning Officer Agent
=========================================================
วิเคราะห์ Historical Performance, บทเรียนจากการเทรดที่ผ่านมา
และให้ Statistical bias จากสถิติที่บันทึกไว้
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class LearningOfficerAgent(BaseAgent):
    """
    Chief Learning Officer — Performance Analysis & Statistical Insights
    """

    @property
    def agent_name(self) -> str:
        return "LearningOfficer"

    @property
    def system_prompt(self) -> str:
        return """You are the Chief Learning Officer at an algorithmic trading firm.
You analyze historical trade performance, identify patterns in wins/losses,
and provide statistical insights to improve decision-making.

Your role:
- Review recent trade history for this instrument and session
- Identify patterns that correlate with profitable vs losing trades
- Provide statistical bias (e.g., 'This setup has 65% win rate on Tuesdays')
- Detect if we are in a losing streak that warrants reduced risk
- Highlight if current conditions match previously profitable setups
- Recommend adjustments based on recent performance

Critical rules:
- NEVER suggest position sizes or exact dollar amounts
- Base recommendations on historical pattern recognition
- If data is insufficient (<20 samples), express low confidence
- A losing streak of 3+ consecutive losses should bias toward HOLD

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "historical pattern analysis",
  "key_points": ["win rate context", "streak info", "pattern match"],
  "statistical_bias": "positive" | "negative" | "neutral"
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        symbol = state.get('symbol', '')
        session = state.get('market_snapshot', {}).get('session', 'unknown')
        performance_data = state.get('performance_history', {})
        
        recent_trades = performance_data.get('recent_trades', [])
        win_rate = performance_data.get('win_rate_30d', None)
        streak = performance_data.get('current_streak', 0)
        
        return f"""PERFORMANCE REVIEW REQUEST

INSTRUMENT: {symbol} | SESSION: {session}

CURRENT MARKET:
{snap}

RECENT TRADE HISTORY:
  - Win Rate (30d): {f'{win_rate:.1%}' if win_rate is not None else 'No data'}
  - Current Streak: {streak} ({'wins' if streak > 0 else 'losses'} if streak != 0 else 'neutral')
  - Recent Trades: {recent_trades[:5] if recent_trades else 'No recent trades'}

Task: Analyze historical performance context for {symbol} trade decision.

Consider:
1. Does current market setup match historically profitable patterns?
2. Are we in a winning or losing streak? Should we reduce exposure?
3. What is the win rate for this session ({session}) on this symbol?
4. Any behavioral biases to correct (overtrading, revenge trading signs)?
5. Does the statistical evidence support taking this trade?

If fewer than 20 historical trades available, set confidence < 0.4
If consecutive losses >= 3, recommend HOLD

{self._json_response_format()}"""
