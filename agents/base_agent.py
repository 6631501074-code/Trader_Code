"""
agents/base_agent.py — Abstract Base Class สำหรับ Department Agents ทุกตัว
=========================================================================
ทุก agent inherit จาก BaseAgent และ implement method `analyze()`
การเรียก LLM ทำผ่าน _call_llm() ที่มี retry + JSON repair built-in
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage

from config import get_settings
from core.state import DepartmentReport, TradingState
from safety.json_repair import (
    DEPARTMENT_REPORT_SCHEMA,
    repair_json_output,
    validate_schema,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class BaseAgent(ABC):
    """
    Abstract Base Agent
    
    ทุก Department Agent ต้อง:
    1. Override `agent_name` property
    2. Override `system_prompt` property
    3. Implement `build_prompt(state)` — สร้าง user prompt จาก state
    4. Optionally override `parse_output(raw)` ถ้า format ต่างจาก standard
    """

    # --- LLM shared instance (lazy init) ---
    _llm: Optional[ChatOllama] = None
    _llm_lock = asyncio.Lock()

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """ชื่อ agent เช่น 'TradingStrategy'"""
        ...

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """System prompt สำหรับ agent นี้"""
        ...

    @abstractmethod
    def build_prompt(self, state: TradingState) -> str:
        """สร้าง user prompt จาก TradingState"""
        ...

    # ──────────────────────────────────────────────────────────────
    # Main analyze method — เรียกโดย Orchestrator
    # ──────────────────────────────────────────────────────────────

    async def analyze(self, state: TradingState) -> DepartmentReport:
        """
        รัน agent: build prompt → call LLM → parse → validate → return report
        ถ้า error ใดๆ → return HOLD report พร้อม error message
        """
        start_ms = time.monotonic() * 1000

        try:
            user_prompt = self.build_prompt(state)
            raw_output = await self._call_llm(user_prompt)
            report = self._parse_output(raw_output)

            latency = time.monotonic() * 1000 - start_ms
            report["agent_name"] = self.agent_name
            report["latency_ms"] = latency
            report["status"] = "ok"
            report["raw_llm_output"] = raw_output

            logger.info(
                f"[{self.agent_name}] [OK] rec={report['recommendation']} "
                f"conf={report['confidence']:.2f} lat={latency:.0f}ms"
            )
            return report

        except Exception as exc:
            latency = time.monotonic() * 1000 - start_ms
            logger.error(f"[{self.agent_name}] [ERR] Error: {exc}")
            return DepartmentReport(
                agent_name=self.agent_name,
                status="error",
                recommendation="HOLD",
                confidence=0.0,
                reasoning=f"Agent error: {exc}",
                key_points=[f"ERROR: {str(exc)[:200]}"],
                raw_llm_output="",
                latency_ms=latency,
            )

    # ──────────────────────────────────────────────────────────────
    # LLM Call — shared Ollama instance with retry
    # ──────────────────────────────────────────────────────────────

    async def _call_llm(self, user_prompt: str, max_retries: int = 2) -> str:
        """เรียก Gemma3 4b ผ่าน Ollama พร้อม retry"""
        llm = await self._get_llm()
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=user_prompt),
        ]

        for attempt in range(1, max_retries + 1):
            try:
                response = await llm.ainvoke(messages)
                return response.content
            except Exception as exc:
                if attempt == max_retries:
                    raise
                wait = 2 ** attempt
                logger.warning(f"[{self.agent_name}] LLM retry {attempt}/{max_retries} in {wait}s: {exc}")
                await asyncio.sleep(wait)

        raise RuntimeError("LLM call failed after all retries")  # unreachable

    @classmethod
    async def _get_llm(cls) -> ChatOllama:
        """Lazy-init shared ChatOllama instance"""
        if cls._llm is None:
            async with cls._llm_lock:
                if cls._llm is None:
                    cls._llm = ChatOllama(
                        model=settings.ollama_model,
                        base_url=settings.ollama_base_url,
                        temperature=settings.llm_temperature,
                        num_predict=settings.llm_max_tokens,
                    )
                    logger.info(f"[BaseAgent] LLM initialized: {settings.ollama_model}")
        return cls._llm

    # ──────────────────────────────────────────────────────────────
    # Output Parser — JSON Repair Loop
    # ──────────────────────────────────────────────────────────────

    def _parse_output(self, raw: str) -> DepartmentReport:
        """Parse LLM output → DepartmentReport (with JSON repair)"""
        try:
            parsed, attempts = repair_json_output(
                raw_text=raw,
                required_keys=DEPARTMENT_REPORT_SCHEMA["required"],
                max_retries=settings.json_repair_max_retries,
            )
            errors = validate_schema(parsed, DEPARTMENT_REPORT_SCHEMA)
            if errors:
                raise ValueError(f"Schema validation failed: {errors}")

            return DepartmentReport(
                agent_name=self.agent_name,
                status="ok",
                recommendation=parsed["recommendation"].upper(),
                confidence=max(0.0, min(1.0, float(parsed["confidence"]))),
                reasoning=str(parsed["reasoning"]),
                key_points=list(parsed.get("key_points", [])),
                raw_llm_output=raw,
                latency_ms=0.0,  # filled by analyze()
            )

        except ValueError as e:
            logger.warning(f"[{self.agent_name}] JSON parse failed, defaulting HOLD: {e}")
            return DepartmentReport(
                agent_name=self.agent_name,
                status="error",
                recommendation="HOLD",
                confidence=0.0,
                reasoning=f"JSON parse error: {e}",
                key_points=[],
                raw_llm_output=raw,
                latency_ms=0.0,
            )

    # ──────────────────────────────────────────────────────────────
    # Prompt Helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _format_snapshot(state: TradingState) -> str:
        """สรุป market snapshot เป็น text สำหรับใส่ใน prompt"""
        snap = state.get("market_snapshot", {})
        return (
            f"Symbol: {snap.get('symbol')} | Timeframe: {snap.get('timeframe')}\n"
            f"OHLC: O={snap.get('open')} H={snap.get('high')} "
            f"L={snap.get('low')} C={snap.get('close')}\n"
            f"Bid/Ask: {snap.get('bid')}/{snap.get('ask')} | Spread: {snap.get('spread')}pts\n"
            f"Session: {snap.get('session')} | ATR14: {snap.get('atr_14')}\n"
            f"Key Levels: {snap.get('key_levels', [])}\n"
            f"Volume: {snap.get('volume')}"
        )

    @staticmethod
    def _format_positions(state: TradingState) -> str:
        """สรุป open positions"""
        positions = state.get("open_positions", [])
        if not positions:
            return "No open positions"
        lines = []
        for p in positions:
            lines.append(
                f"  #{p.get('ticket')} {p.get('symbol')} {p.get('type')} "
                f"lot={p.get('volume')} profit={p.get('profit'):.2f}"
            )
        return "\n".join(lines)

    @staticmethod
    def _json_response_format() -> str:
        """คำแนะนำ format JSON ที่ LLM ต้องตอบ"""
        return """
Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
{
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "detailed explanation",
  "key_points": ["point 1", "point 2", "point 3"]
}
"""
