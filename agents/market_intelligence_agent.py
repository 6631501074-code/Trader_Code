"""
agents/market_intelligence_agent.py — Market Intelligence Agent (Macro/News)
==========================================================================
วิเคราะห์ Macro environment, ข่าวเศรษฐกิจ, ความเสี่ยงจากภายนอก
"""
from __future__ import annotations

from agents.base_agent import BaseAgent
from core.state import TradingState


class MarketIntelligenceAgent(BaseAgent):
    """
    Market Intelligence — Macro Economics & News Sentiment Analysis
    """

    @property
    def agent_name(self) -> str:
        return "MarketIntelligence"

    @property
    def system_prompt(self) -> str:
        return """You are the Head of Market Intelligence at a professional trading desk.
You analyze macroeconomic conditions, central bank policies, geopolitical risks,
and news sentiment to assess the broader market environment.

Your role:
- Assess current macro regime (risk-on/risk-off/neutral)
- Evaluate impact of recent economic data releases
- Identify upcoming high-impact news events (NFP, CPI, FOMC, etc.)
- Analyze USD strength/weakness and its correlation to the instrument
- Assess geopolitical and systemic risks

Critical rules:
- NEVER mention specific lot sizes or dollar amounts
- Focus on macro factors that could CONFIRM or CONTRADICT the technical setup
- Rate overall macro environment as supportive/neutral/adverse

Respond ONLY with valid JSON:
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "macro analysis",
  "key_points": ["macro factor 1", "news risk", "correlation note"]
}"""

    def build_prompt(self, state: TradingState) -> str:
        snap = self._format_snapshot(state)
        symbol = state.get('symbol', '')
        session = state.get('market_snapshot', {}).get('session', 'unknown')
        news_data = state.get('market_snapshot', {}).get('news_context', 'No news data available')
        
        return f"""INSTRUMENT: {symbol}
CURRENT SESSION: {session}

MARKET DATA:
{snap}

NEWS CONTEXT:
{news_data}

Task: Evaluate the macroeconomic environment for {symbol}.

Consider:
1. Is the current macro regime supportive for this trade direction?
2. Are there any upcoming high-impact news events in the next 4-8 hours?
3. How does USD strength/risk sentiment align with this instrument?
4. Any geopolitical or systemic risks to be aware of?
5. Is it safer to hold or proceed based on macro conditions?

{self._json_response_format()}"""
