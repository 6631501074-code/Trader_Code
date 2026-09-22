"""
safety/json_repair.py — JSON Repair Loop for LLM Output
=========================================================
LLM บางครั้ง output JSON ไม่สมบูรณ์ (ขาด bracket, มี trailing comma ฯลฯ)
โมดูลนี้พยายาม repair สูงสุด N รอบก่อน fail-closed
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# พยายาม import json-repair library ก่อน ถ้าไม่มีใช้ fallback
try:
    from json_repair import repair_json as _lib_repair
    _HAS_JSON_REPAIR = True
except ImportError:
    _HAS_JSON_REPAIR = False
    logger.warning("[JSONRepair] json-repair library not found, using regex fallback only")


# ============================================================
# Main Repair Function
# ============================================================

def repair_json_output(
    raw_text: str,
    required_keys: list[str],
    max_retries: int = 3,
) -> tuple[dict[str, Any], int]:
    """
    พยายาม parse + repair JSON จาก LLM output

    Args:
        raw_text:      raw string จาก LLM
        required_keys: list ของ key ที่ต้องมีในผลลัพธ์
        max_retries:   จำนวนรอบ repair สูงสุด

    Returns:
        (parsed_dict, attempts_used)  — attempts_used = รอบที่สำเร็จ

    Raises:
        ValueError: ถ้า repair ไม่สำเร็จหลัง max_retries รอบ
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            candidate = _extract_and_repair(raw_text, attempt)
            parsed = json.loads(candidate)

            # ตรวจ required keys
            missing = [k for k in required_keys if k not in parsed]
            if missing:
                raise ValueError(f"Missing required keys: {missing}")

            logger.debug(f"[JSONRepair] Success on attempt {attempt}")
            return parsed, attempt

        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            logger.warning(f"[JSONRepair] Attempt {attempt}/{max_retries} failed: {e}")

    raise ValueError(
        f"JSON repair failed after {max_retries} attempts. "
        f"Last error: {last_error}. Raw text preview: {raw_text[:200]!r}"
    )


# ============================================================
# Repair Strategy Pipeline
# ============================================================

def _extract_and_repair(raw_text: str, attempt: int) -> str:
    """
    ลำดับกลยุทธ์ repair ตาม attempt:
    1. ดึง JSON block ออกจาก markdown code fence
    2. ใช้ json-repair library (ถ้ามี)
    3. Regex-based cleanup (trailing commas, single quotes)
    4. ตัด text ก่อน/หลัง JSON bracket แรก
    """
    text = raw_text.strip()

    # Strategy 1: Extract from markdown code block ```json ... ```
    text = _strip_markdown_fence(text)

    if attempt >= 2 and _HAS_JSON_REPAIR:
        # Strategy 2: json-repair library
        text = _lib_repair(text, return_objects=False)

    if attempt >= 2:
        # Strategy 3: Regex cleanup
        text = _regex_cleanup(text)

    if attempt >= 3:
        # Strategy 4: Find outermost JSON object
        text = _extract_outermost_json(text)

    return text


def _strip_markdown_fence(text: str) -> str:
    """ลบ ```json ... ``` หรือ ``` ... ``` wrapper"""
    pattern = r"```(?:json)?\s*([\s\S]*?)```"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()
    return text


def _regex_cleanup(text: str) -> str:
    """แก้ปัญหา JSON ทั่วไปด้วย regex"""
    # แก้ trailing commas: , } และ , ]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # แก้ single quotes → double quotes (ระวัง apostrophe ใน value)
    text = re.sub(r"(?<![\\])'([^']*)':", r'"\1":', text)
    # ลบ comments // ...
    text = re.sub(r"//[^\n]*", "", text)
    # ลบ trailing comma หน้า }
    text = re.sub(r",(\s*})", r"\1", text)
    return text


def _extract_outermost_json(text: str) -> str:
    """ดึง JSON object ที่สมบูรณ์จาก text โดยนับ bracket"""
    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return text[start:]  # ถ้าหา end ไม่เจอ return ส่วนที่เหลือ


# ============================================================
# Schema Validator
# ============================================================

CEO_DECISION_SCHEMA = {
    "required": ["action", "confidence", "reasoning", "sl_pips", "tp_pips", "entry_price"],
    "types": {
        "action": str,
        "confidence": (int, float),
        "reasoning": str,
        "sl_pips": (int, float),
        "tp_pips": (int, float),
        "entry_price": (int, float),
    },
    "enums": {
        "action": ["BUY", "SELL", "HOLD"],
    },
}

DEPARTMENT_REPORT_SCHEMA = {
    "required": ["recommendation", "confidence", "reasoning", "key_points"],
    "types": {
        "recommendation": str,
        "confidence": (int, float),
        "reasoning": str,
        "key_points": list,
    },
    "enums": {
        "recommendation": ["BUY", "SELL", "HOLD"],
    },
}


def validate_schema(data: dict, schema: dict) -> list[str]:
    """
    ตรวจสอบ schema ของ dict
    Returns: list ของ error messages (empty = pass)
    """
    errors: list[str] = []

    for key in schema.get("required", []):
        if key not in data:
            errors.append(f"Missing key: '{key}'")

    for key, expected_type in schema.get("types", {}).items():
        if key in data and not isinstance(data[key], expected_type):
            errors.append(
                f"Key '{key}': expected {expected_type}, got {type(data[key])}"
            )

    for key, allowed in schema.get("enums", {}).items():
        if key in data and data[key] not in allowed:
            errors.append(f"Key '{key}': value '{data[key]}' not in {allowed}")

    return errors
