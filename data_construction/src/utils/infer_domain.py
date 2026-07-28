from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ..config import LLMRuntimeConfig
from ..llm.base import BaseLLMClient
from ..llm.openai_client import OpenAILLMClient

INFER_DOMAIN_SYSTEM_PROMPT = """
    You are an expert Data Analyst and Technical Writer.
    Your task is to analyze the provided database schema and determine:
    1. The **Domain** of the data (e.g., Healthcare, Finance, Education, Retail, Logistics).
    2. Conceptualize **3 to 5 distinct Document Variants**. For each variant, define:
       - **Style**: The narrative tone and format (e.g., Forensic Audit, Stakeholder Storytelling, Internal Correspondence, Strategic Briefing).
       - **Description**: A vivid one-sentence premise of the document, describes the type of comprehensive professional document that could be generated from this data (e.g., "a comprehensive operation report for a university course registration system covering a full academic period", "a detailed financial audit report of client transactions").
    
    Return the result strictly in JSON format.
"""

INFER_DOMAIN_PROMPT = """
# Database Schema
{schema_text}

# Task:
Identify the Domain and generate 3-5 variants of a Document Description, each with a unique Style.

# Output Format (JSON only):
{{
    "domain": "...",
    "variants": [
        {{
            "style": "...",
            "description": "..."
        }}
    ]
}}
"""


def _fallback_result() -> dict[str, Any]:
    return {
        "domain": "General",
        "variants": [
            {
                "style": "Professional Report",
                "description": "A professional report generated from the provided database schema.",
            }
        ],
    }


def _extract_json_block(text: str) -> str:
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    first = raw.find("{")
    last = raw.rfind("}")
    if first != -1 and last != -1 and last > first:
        return raw[first : last + 1].strip()
    return raw


def _normalize_variants(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        style = str(item.get("style", "")).strip()
        description = str(item.get("description", "")).strip()
        if not style:
            continue
        normalized.append({"style": style, "description": description})
    return normalized


def schema_to_text(schema_data: object) -> str:
    """将 schema 数据转换为简明文本描述，用于推断 domain/variants。"""
    text_parts: list[str] = []

    if isinstance(schema_data, list):
        tables = schema_data
    elif isinstance(schema_data, dict):
        tables = schema_data.get("tables", [])
        db_name = str(schema_data.get("db_name", "")).strip()
        if db_name:
            text_parts.append(f"Database Name: {db_name}")
        db_desc = str(schema_data.get("db_description", "")).strip()
        if db_desc:
            text_parts.append(f"Database Description: {db_desc}")
    else:
        tables = []

    for table in tables:
        if not isinstance(table, dict):
            continue
        table_name = table.get("table_name") or table.get("name")
        if not table_name:
            continue

        description = str(table.get("description", "")).strip()
        columns = table.get("columns") or table.get("fields", [])
        col_names = []
        if isinstance(columns, list):
            col_names = [str(c.get("name", "")).strip() for c in columns if isinstance(c, dict)]
            col_names = [c for c in col_names if c]

        table_text = f"Table: {table_name}"
        if description:
            table_text += f" (Description: {description})"
        table_text += f"\nColumns: {', '.join(col_names)}"
        text_parts.append(table_text)

    return "\n\n".join(text_parts)


def infer_domain_and_description(
    schema_data: object,
    model: str | None = "gpt-4o",
    llm: BaseLLMClient | None = None,
) -> dict[str, Any]:
    """根据 schema 推断 domain 与 variants，返回统一 JSON 结构。"""
    llm_client = llm or OpenAILLMClient(config=LLMRuntimeConfig())
    prompt = INFER_DOMAIN_PROMPT.format(schema_text=schema_to_text(schema_data))

    try:
        response = llm_client.generate(
            prompt,
            system_prompt=INFER_DOMAIN_SYSTEM_PROMPT,
            model=model,
            temperature=0.7,
            task="writing",
        ).strip()
    except Exception as e:
        print(f"Error inferring domain/description: {e}")
        return _fallback_result()

    if not response:
        return _fallback_result()

    try:
        payload = json.loads(_extract_json_block(response))
    except json.JSONDecodeError:
        return _fallback_result()

    if not isinstance(payload, dict):
        return _fallback_result()

    domain = str(payload.get("domain", "")).strip() or "General"
    variants = _normalize_variants(payload.get("variants", []))
    if not variants:
        variants = _fallback_result()["variants"]
    return {"domain": domain, "variants": variants}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Infer Domain and Description from Schema")
    parser.add_argument("schema_path", type=str, help="Path to schema.json")
    parser.add_argument("--model", type=str, default="gpt-4o", help="Model to use")
    args = parser.parse_args()

    schema_path = Path(args.schema_path)
    if not schema_path.exists():
        print(f"File not found: {schema_path}")
        raise SystemExit(1)

    schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
    result = infer_domain_and_description(schema_data, model=args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
