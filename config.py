"""
config.py — Centralized configuration via Pydantic Settings.
ดึงค่าจาก .env อัตโนมัติ พร้อม Type validation
"""
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096

    # --- MT5 ---
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = r"C:\Program Files\XM Global MT5 Terminal\terminal64.exe"

    # --- PostgreSQL ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "trader_db"
    postgres_user: str = "trader_user"
    postgres_password: str = ""
    database_url: str = "postgresql+asyncpg://trader_user:password@localhost:5432/trader_db"

    # --- Safety Limits ---
    max_risk_pct: float = Field(default=0.02, ge=0.001, le=0.20)  # สูงสุด 20% ต่อไม้
    min_lot: float = 0.01
    max_lot: float = 5.0
    data_staleness_seconds: int = 30
    json_repair_max_retries: int = 3

    # --- Agent Settings ---
    agent_timeout_seconds: float = 300.0   # timeout ต่อ agent (วินาที)

    # --- System ---
    log_level: str = "INFO"
    environment: str = "development"
    execution_mode: str = "paper"          # "paper" หรือ "live"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings — ใช้ร่วมกันทั้งระบบ"""
    return Settings()

