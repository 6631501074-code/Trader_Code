"""
agents/risk_officer_agent.py — Risk Officer Agent
==================================================
ประเมินความเสี่ยงรวมของ portfolio และ สภาวะตลาด
CRITICAL: ไม่คำนวณ lot size หรือเงิน — แค่ประเมิน risk level
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class RiskOfficerAgent(BaseAgent):
    """
    Chief Risk Officer — Portfolio Risk Assessment (Qualitative only)
    [WARN]  ห้ามคำนวณ lot size, dollar amounts ใน agent นี้
    """

    @property
    def agent_name(self) -> str:
        return "RiskOfficer"

    @property
    def system_prompt(self) -> str:
        return """You are the Chief Risk Officer at a regulated trading firm.
You assess QUALITATIVE risk levels and portfolio exposure — you do NOT calculate
position sizes or dollar amounts (that is handled by separate deterministic code).

Your role:
- Assess overall portfolio risk exposure (low/medium/high/extreme)
- Check for over-correlation in open positions
- Evaluate market volatility conditions (ATR context)
- Identify tail risks and black swan scenarios
- Recommend maximum risk level for this specific trade
- Veto trades during extreme conditions (news events, low liquidity)

Critical rules:
- NEVER calculate lot sizes, pip values, or dollar amounts
- Express risk only as: low/medium/high/extreme or 1-5 scale
- Your HOLD recommendation carries a VETO on the trade

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "risk assessment reasoning",
  "key_points": ["portfolio exposure level", "volatility assessment", "tail risk note"],
  "risk_level": "low" | "medium" | "high" | "extreme",
  "veto_reason": null | "reason if vetoing"
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        positions = self._format_positions(state)
        account = state.get('account_info', {})
        
        margin_level = account.get('margin_level', 0)
        equity = account.get('equity', 0)
        balance = account.get('balance', 0)
        
        return f"""RISK ASSESSMENT REQUEST

INSTRUMENT: {state.get('symbol')}
ACCOUNT STATUS:
  - Equity: {equity:.2f} USD
  - Balance: {balance:.2f} USD
  - Margin Used: {state.get('account_info', {}).get('margin', 0):.2f} USD
  - Margin Free: {state.get('account_info', {}).get('margin_free', 0):.2f} USD
  - Margin Level: {margin_level:.1f}%
  - Leverage: 1:{state.get('account_info', {}).get('leverage', 100)}

MARGIN LEVEL INTERPRETATION (IMPORTANT):
  - Margin Level = (Equity / Margin Used) × 100%
  - >500% = Excellent (very safe, plenty of room)
  - 200-500% = Good (healthy account)
  - 100-200% = Caution (getting tight)
  - <100%    = DANGER (margin call zone — must HOLD)
  Current: {margin_level:.1f}% → {"EXCELLENT - safe to trade" if margin_level > 500 else "GOOD" if margin_level > 200 else "CAUTION" if margin_level > 100 else "DANGER - DO NOT TRADE"}

MARKET CONDITIONS:
{snap}

CURRENT OPEN POSITIONS:
{positions}

Task: Perform qualitative risk assessment for adding a NEW position on {state.get('symbol')}.

Assess:
1. Is the margin level safe for adding another position? (refer to interpretation above)
2. Is market volatility (ATR) favorable for new entries?
3. Are there correlation risks with existing positions?
4. Any tail risks that could cause rapid adverse moves?
5. Should this trade be vetoed? (HOLD) If so, why?

RULES:
- If margin_level < 200%, recommend HOLD (margin too tight)
- If margin_level >= 200%, margin is NOT a reason to HOLD
- If more than 3 open positions on same symbol, recommend HOLD

{self._json_response_format()}"""
