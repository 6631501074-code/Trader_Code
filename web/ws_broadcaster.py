"""
web/ws_broadcaster.py — WebSocket Connection Manager
=====================================================
จัดการ WebSocket connections ทั้งหมด + broadcast messages ไปยัง clients
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages all active WebSocket connections.
    Thread-safe broadcast to all connected clients.
    """

    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.active.append(ws)
        logger.info(f"[WS] Client connected. Total: {len(self.active)}")

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)
        logger.info(f"[WS] Client disconnected. Total: {len(self.active)}")

    async def broadcast(self, msg_type: str, data: Any) -> None:
        """Send JSON message to ALL connected clients."""
        payload = json.dumps({"type": msg_type, "data": data}, ensure_ascii=False, default=str)
        disconnected: list[WebSocket] = []
        async with self._lock:
            clients = list(self.active)
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            await self.disconnect(ws)

    async def send_to(self, ws: WebSocket, msg_type: str, data: Any) -> None:
        """Send JSON message to ONE specific client."""
        payload = json.dumps({"type": msg_type, "data": data}, ensure_ascii=False, default=str)
        try:
            await ws.send_text(payload)
        except Exception as e:
            logger.warning(f"[WS] send_to failed: {e}")

    @property
    def connection_count(self) -> int:
        return len(self.active)


# Global singleton
manager = ConnectionManager()
