"""
agents/operations_agent.py — Operations & Data Integrity Agent
=============================================================
ตรวจสอบความสมบูรณ์ของข้อมูล, สุขภาพระบบ, และเงื่อนไขการเทรด
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class OperationsAgent(BaseAgent):
    """
    Head of Operations & Data Integrity — System Health & Execution Readiness
    """

    @property
    def agent_name(self) -> str:
        return "Operations"

    @property
    def system_prompt(self) -> str:
        return """You are the Head of Operations at a systematic trading firm.
You ensure system integrity, data quality, and execution readiness before any trade.

Your role:
- Verify data quality (spread, volume, price consistency)
- Check broker connection and execution conditions
- Assess market microstructure (liquidity, manipulation signs)
- Identify if trading conditions are abnormal (holiday, rollover, etc.)
- Check spread vs normal range — high spread = avoid
- Verify no weekend gaps or session gaps present
- Confirm we are within allowed trading hours

Critical rules:
- NEVER suggest position sizes or dollar amounts
- If any data quality issue found, recommend HOLD
- Spreads >3x normal = automatic HOLD recommendation
- Low volume periods (early Asian session for non-Asian pairs) = HOLD

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "operational assessment",
  "key_points": ["spread status", "liquidity check", "system health"],
  "execution_ready": true | false,
  "data_quality": "good" | "degraded" | "poor"
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        symbol = state.get('symbol', '')
        session = state.get('market_snapshot', {}).get('session', 'unknown')
        spread = state.get('market_snapshot', {}).get('spread', 0)
        volume = state.get('market_snapshot', {}).get('volume', 0)
        
        # Normal spread reference (rough estimates per symbol type)
        normal_spread_ref = {
            'EURUSD': 1.5, 'GBPUSD': 2.0, 'USDJPY': 1.5,
            'XAUUSD': 30.0, 'GBPJPY': 4.0
        }
        ref_spread = normal_spread_ref.get(symbol, 3.0)
        spread_ratio = spread / ref_spread if ref_spread > 0 else 1.0
        
        return f"""OPERATIONAL READINESS CHECK

INSTRUMENT: {symbol} | SESSION: {session}

MARKET MICROSTRUCTURE:
{snap}

SPREAD ANALYSIS:
  - Current Spread: {spread:.1f} pts
  - Normal Spread Reference: {ref_spread:.1f} pts
  - Spread Ratio: {spread_ratio:.1f}x (>3x = reject)

VOLUME: {volume}

Task: Assess operational readiness for executing a trade on {symbol}.

Check:
1. Is current spread within acceptable range (< 2x normal for this session)?
2. Is volume/liquidity sufficient for reliable execution?
3. Are we in a high-risk time period? (rollover, news blackout, market open gap)
4. Any signs of price manipulation or abnormal tick behavior?
5. Is the broker connection healthy? (assume yes unless data shows anomalies)
6. Is it an appropriate time to trade this instrument in this session?

If spread_ratio > 3.0, MUST recommend HOLD
If session == 'off', recommend HOLD

{self._json_response_format()}"""
