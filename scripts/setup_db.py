"""
scripts/setup_db.py — Database Setup & Diagnostic Utility
=========================================================
ทดสอบการเชื่อมต่อ PostgreSQL และสร้าง Table Schema สำหรับ Audit Log
รันคำสั่ง:
    python scripts/setup_db.py
"""
import asyncio
import sys
import os

# เพิ่ม root directory ใน sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import get_settings
from logging_.audit_logger import create_db_pool, SCHEMA_DDL


async def setup_database():
    settings = get_settings()
    print("=" * 60)
    print("PostgreSQL Database Setup & Diagnostics")
    print("=" * 60)
    print(f"Target Database URL: {settings.database_url}")
    print(f"Host: {settings.postgres_host}:{settings.postgres_port}")
    print(f"Database: {settings.postgres_db}")
    print(f"User: {settings.postgres_user}")
    print("-" * 60)

    print("[*] Connecting to PostgreSQL...")
    pool = await create_db_pool(settings.database_url)

    if pool is None:
        print("\n[!] Connection FAILED: Could not create asyncpg connection pool.")
        print("คำแนะนำสำหรับการตั้งค่า PostgreSQL:")
        print("1. ตรวจสอบว่า PostgreSQL service กำลังทำงานอยู่")
        print("2. สร้าง user และ database ตามที่ระบุใน .env เช่น:")
        print(f"   CREATE USER {settings.postgres_user} WITH PASSWORD '{settings.postgres_password}';")
        print(f"   CREATE DATABASE {settings.postgres_db} OWNER {settings.postgres_user};")
        print("3. อัปเดตไฟล์ .env ให้ตรงกับรหัสผ่านจริง")
        print("\nหมายเหตุ: ระบบสามารถทำงานในโหมด --simulate ได้โดยไม่ต้องมี PostgreSQL (Log จะแสดงผลทาง Console)")
        return False

    try:
        async with pool.acquire() as conn:
            print("[+] Connection successful! Executing DDL schemas...")
            await conn.execute(SCHEMA_DDL)
            print("[+] All tables and indexes verified/created successfully:")
            print("    - workflow_runs")
            print("    - agent_reports")
            print("    - ceo_decisions")
            print("    - trade_executions")
            print("    - idempotency_keys")

            # ตรวจสอบจำนวน table
            rows = await conn.fetch(
                """
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public'
                ORDER BY table_name;
                """
            )
            tables = [r["table_name"] for r in rows]
            print(f"[+] Public tables found: {', '.join(tables)}")

        print("\n[OK] Database setup complete and ready for live trading!")
        return True

    except Exception as exc:
        print(f"\n[!] Error during schema execution: {exc}")
        return False
    finally:
        await pool.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    asyncio.run(setup_database())
