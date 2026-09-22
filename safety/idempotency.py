"""
safety/idempotency.py — Idempotency Key Registry
=================================================
ป้องกัน event เดิมถูกประมวลผลซ้ำ
เก็บ key ที่ใช้แล้วใน PostgreSQL + in-memory cache
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class IdempotencyRegistry:
    """
    Thread-safe Idempotency Key Registry
    
    - in-memory set สำหรับ fast lookup
    - PostgreSQL สำหรับ persistence ข้าม restart
    - TTL: 24 ชั่วโมง (หลังจากนั้น key หมดอายุ)
    """

    TTL_HOURS = 24

    def __init__(self, db_pool=None) -> None:
        """
        Args:
            db_pool: asyncpg connection pool (ถ้าไม่มี ใช้ in-memory เท่านั้น)
        """
        self._seen: dict[str, datetime] = {}   # key → timestamp
        self._lock = asyncio.Lock()
        self._db_pool = db_pool

    async def is_duplicate(self, key: str) -> bool:
        """
        ตรวจว่า key เคยใช้แล้วหรือไม่

        Returns:
            True  = ซ้ำ → ควร skip
            False = ใหม่ → ดำเนินการต่อ
        """
        async with self._lock:
            # 1. ตรวจ in-memory ก่อน (เร็วสุด)
            if key in self._seen:
                age = datetime.now(timezone.utc) - self._seen[key]
                if age < timedelta(hours=self.TTL_HOURS):
                    logger.info(f"[Idempotency] DUPLICATE key={key} age={age}")
                    return True
                else:
                    # หมดอายุแล้ว ลบออก
                    del self._seen[key]

            # 2. ตรวจ PostgreSQL (ถ้ามี)
            if self._db_pool:
                exists = await self._check_db(key)
                if exists:
                    self._seen[key] = datetime.now(timezone.utc)  # reload to cache
                    return True

            return False

    async def register(self, key: str) -> None:
        """บันทึก key ว่าใช้แล้ว"""
        async with self._lock:
            now = datetime.now(timezone.utc)
            self._seen[key] = now

            if self._db_pool:
                await self._save_db(key, now)

            logger.debug(f"[Idempotency] Registered key={key}")

    async def _check_db(self, key: str) -> bool:
        """ตรวจ PostgreSQL"""
        try:
            async with self._db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1 FROM idempotency_keys
                    WHERE key = $1
                      AND created_at > NOW() - INTERVAL '24 hours'
                    """,
                    key,
                )
                return row is not None
        except Exception as exc:
            logger.error(f"[Idempotency] DB check error: {exc}")
            return False  # ถ้า DB error → assume not duplicate (fail-open สำหรับ idempotency)

    async def _save_db(self, key: str, timestamp: datetime) -> None:
        """บันทึก key ลง PostgreSQL"""
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO idempotency_keys (key, created_at)
                    VALUES ($1, $2)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    key,
                    timestamp,
                )
        except Exception as exc:
            logger.error(f"[Idempotency] DB save error: {exc}")

    def cleanup_expired(self) -> int:
        """ลบ key ที่หมดอายุจาก in-memory cache"""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.TTL_HOURS)
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in expired:
            del self._seen[k]
        if expired:
            logger.debug(f"[Idempotency] Cleaned {len(expired)} expired keys")
        return len(expired)


# ============================================================
# DB Schema (สำหรับ migration)
# ============================================================

IDEMPOTENCY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        VARCHAR(64) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_idempotency_created_at
    ON idempotency_keys (created_at);
"""
