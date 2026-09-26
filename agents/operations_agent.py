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
- Spreads >3x normal = automatic HOLD recommendation and execution_ready: false
- Spreads up to 2.5x normal are acceptable and execution_ready must be true
- Low volume periods (early Asian session for non-Asian pairs) = HOLD
- If any critical data quality issue found, recommend HOLD

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "operational assessment",
  "key_points": ["spread status", "liquidity check", "system health"],
  "execution_ready": true | false,
  "data_quality": "good" | "degraded" | "poor"
}"""

    async def analyze(self, state: TradingState):
        report = await super().analyze(state)
        # Deterministic Guard: ป้องกัน LLM สับสนตัวเลข (เช่น เข้าใจผิดว่า 55 มากกว่า 65)
        snap = state.get("market_snapshot", {})
        spread = float(snap.get("spread", 0))
        symbol = state.get("symbol", "")
        session = snap.get("session", "unknown")

        normal_spread_ref = {
            'EURUSD': 1.8, 'GBPUSD': 2.2, 'USDJPY': 1.8,
            'GOLD': 60.0, 'XAUUSD': 60.0, 'GBPJPY': 4.5
        }
        ref_spread = normal_spread_ref.get(symbol, 60.0 if ("GOLD" in symbol or "XAU" in symbol) else 3.0)
        spread_ratio = spread / ref_spread if ref_spread > 0 else 1.0

        if spread_ratio <= 2.5 and session != "off":
            report["execution_ready"] = True
            report["data_quality"] = "good"
            # ปรับ reasoning ให้สอดคล้องกับความเป็นจริงของสเปรด
            if "exceed" in report.get("reasoning", "").lower() or "higher than" in report.get("reasoning", "").lower():
                report["reasoning"] = f"Spread is healthy ({spread:.1f} pts, ratio {spread_ratio:.2f}x <= 2.5x). Execution conditions are ready."

        return report

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        symbol = state.get('symbol', '')
        session = state.get('market_snapshot', {}).get('session', 'unknown')
        spread = state.get('market_snapshot', {}).get('spread', 0)
        volume = state.get('market_snapshot', {}).get('volume', 0)
        
        # Normal spread reference (rough estimates per symbol type in points/cents)
        normal_spread_ref = {
            'EURUSD': 1.8, 'GBPUSD': 2.2, 'USDJPY': 1.8,
            'GOLD': 60.0, 'XAUUSD': 60.0, 'GBPJPY': 4.5
        }
        ref_spread = normal_spread_ref.get(symbol, 60.0 if ("GOLD" in symbol or "XAU" in symbol) else 3.0)
        spread_ratio = spread / ref_spread if ref_spread > 0 else 1.0
        
        return f"""OPERATIONAL READINESS CHECK

INSTRUMENT: {symbol} | SESSION: {session}

MARKET MICROSTRUCTURE:
{snap}

SPREAD ANALYSIS:
  - Current Spread: {spread:.1f} pts
  - Normal Spread Reference: {ref_spread:.1f} pts (For GOLD, 35-65 pts is typical normal range)
  - Spread Ratio: {spread_ratio:.1f}x (>3x = reject)

VOLUME: {volume}

Task: Assess operational readiness for executing a trade on {symbol}.

Check:
1. Is current spread within acceptable range? (spread_ratio <= 2.5x is acceptable and execution_ready must be TRUE)
2. Is volume/liquidity sufficient for reliable execution?
3. Are we in a high-risk time period? (rollover, news blackout, market open gap)
4. Any signs of price manipulation or abnormal tick behavior?
5. Is the broker connection healthy? (assume yes unless data shows anomalies)
6. Is it an appropriate time to trade this instrument in this session?

RULES:
- If spread_ratio <= 2.5, spread is NORMAL → execution_ready must be true, data_quality: "good"
- Spreads between 35.0 and 70.0 pts for GOLD are completely normal on retail MT5 brokers
- ONLY set execution_ready: false if spread_ratio > 3.0 or broker data is corrupt/missing
- If spread_ratio > 3.0, MUST recommend HOLD and set execution_ready: false
- If session == 'off', recommend HOLD

{self._json_response_format()}"""
