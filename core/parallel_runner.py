"""
core/parallel_runner.py — Async Parallel Agent Runner
=====================================================
รัน Department Agents พร้อมกัน (Concurrent execution)
พร้อมการจัดการ Timeout, Error Isolation, และ Concurrency Limit
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Coroutine, Sequence

from agents.base_agent import BaseAgent
from core.state import DepartmentReport, TradingState

logger = logging.getLogger(__name__)


class ParallelDepartmentRunner:
    """
    ตัวจัดการการรัน Department Agents พร้อมกัน
    - ควบคุม timeout ต่อ agent
    - ป้องกันไม่ให้ agent ตัวใดตัวหนึ่งที่ล้มเหลวทำให้ทั้งระบบหยุดทำงาน
    - บันทึก latency และสร้าง default HOLD report เมื่อเกิดข้อผิดพลาด
    """

    def __init__(self, timeout_seconds: float = 180.0, max_concurrency: int = 10) -> None:
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run_single_agent(self, agent: BaseAgent, state: TradingState) -> DepartmentReport:
        """รัน 1 agent พร้อม semaphore และ timeout"""
        start = time.monotonic() * 1000
        async with self._semaphore:
            try:
                report = await asyncio.wait_for(
                    agent.analyze(state),
                    timeout=self.timeout_seconds,
                )
                return report
            except asyncio.TimeoutError:
                elapsed = time.monotonic() * 1000 - start
                logger.error(f"[ParallelRunner] {agent.agent_name} timed out after {self.timeout_seconds}s")
                return DepartmentReport(
                    agent_name=agent.agent_name,
                    status="timeout",
                    recommendation="HOLD",
                    confidence=0.0,
                    reasoning=f"Agent timed out after {self.timeout_seconds}s",
                    key_points=["TIMEOUT"],
                    raw_llm_output="",
                    latency_ms=elapsed,
                )
            except Exception as exc:
                elapsed = time.monotonic() * 1000 - start
                logger.error(f"[ParallelRunner] {agent.agent_name} error: {exc}")
                return DepartmentReport(
                    agent_name=agent.agent_name,
                    status="error",
                    recommendation="HOLD",
                    confidence=0.0,
                    reasoning=f"Agent runtime error: {exc}",
                    key_points=["ERROR"],
                    raw_llm_output="",
                    latency_ms=elapsed,
                )

    async def run_all(
        self, agents: Sequence[BaseAgent], state: TradingState
    ) -> tuple[dict[str, DepartmentReport], list[str]]:
        """
        รัน agents ทุกตัวพร้อมกัน
        Returns:
            (reports_dict, errors_list)
        """
        tasks = [
            asyncio.create_task(
                self.run_single_agent(agent, state),
                name=f"agent_{agent.agent_name}",
            )
            for agent in agents
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        reports: dict[str, DepartmentReport] = {}
        errors: list[str] = []

        for agent, res in zip(agents, results):
            if isinstance(res, Exception):
                errors.append(f"{agent.agent_name}: {res}")
                reports[agent.agent_name] = DepartmentReport(
                    agent_name=agent.agent_name,
                    status="error",
                    recommendation="HOLD",
                    confidence=0.0,
                    reasoning=f"Unhandled exception: {res}",
                    key_points=["CRITICAL_ERROR"],
                    raw_llm_output="",
                    latency_ms=0.0,
                )
            else:
                reports[agent.agent_name] = res
                if res.get("status") in ("error", "timeout"):
                    errors.append(f"{agent.agent_name}: {res.get('reasoning')}")

        return reports, errors
