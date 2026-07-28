#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 baseline 抽取结果清洗为统一格式：
{
  "table_a": [ {...}, {...} ],
  "table_b": [ {...} ]
}

典型输入（如 llamaextract）：
{
  "data": {
    "episodes": [...],
    "persons": [...],
    "awards": [...],
    "credits": [...],
    "votes": [...]
  },
  "extract_metadata": {...}
}

单 case：
  python clean_baseline_result.py \\
    --input .../result/education/case1/llama \\
    --output .../llamaextract/education/case1/education_case1_llamaextract/extracted_data.json \\
    --schema .../result/education/case1/schema.json

整数据集（--input/--output 指向根目录即可）：
  python clean_baseline_result.py \\
    --input dataset/case/base_latest_output/result \\
    --output dataset/case/base_latest_output/llamaextract \\
    --schema dataset/case/base_latest
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _singularize(name: str) -> str:
    """简单英文单数化：episodes->episode, categories->category。"""
    s = name.strip().lower()
    if s.endswith("ies") and len(s) > 3:
        return s[:-3] + "y"
    if s.endswith("ses") and len(s) > 3:
        return s[:-2]  # e.g. classes -> class
    if s.endswith("s") and not s.endswith("ss") and len(s) > 1:
        return s[:-1]
    return s


def _pluralize(name: str) -> str:
    s = name.strip().lower()
    if s.endswith("y") and len(s) > 1 and s[-2] not in "aeiou":
        return s[:-1] + "ies"
    if s.endswith(("s", "x", "z", "ch", "sh")):
        return s + "es"
    return s + "s"


def _extract_schema_table_names(schema_payload: Any) -> List[str]:
    tables: List[str] = []
    if isinstance(schema_payload, list):
        for t in schema_payload:
            if isinstance(t, dict):
                name = t.get("name") or t.get("table_name")
                if isinstance(name, str) and name.strip():
                    tables.append(name.strip())
    elif isinstance(schema_payload, dict):
        if isinstance(schema_payload.get("tables"), list):
            for t in schema_payload["tables"]:
                if isinstance(t, dict):
                    name = t.get("name") or t.get("table_name")
                    if isinstance(name, str) and name.strip():
                        tables.append(name.strip())
        else:
            name = schema_payload.get("name") or schema_payload.get("table_name")
            if isinstance(name, str) and name.strip():
                tables.append(name.strip())
    return tables


def _build_table_alias_map(schema_path: Optional[Path]) -> Dict[str, str]:
    """
    构建别名映射：episodes/Episode -> episode（以 schema 为准）。
    返回 alias(lower) -> canonical(lower)。
    """
    alias_map: Dict[str, str] = {}
    if not schema_path:
        return alias_map
    if not schema_path.exists():
        raise FileNotFoundError(f"schema 文件不存在: {schema_path}")

    with schema_path.open("r", encoding="utf-8") as f:
        schema_payload = json.load(f)

    for raw_name in _extract_schema_table_names(schema_payload):
        canonical = raw_name.strip().lower()
        aliases = {
            canonical,
            _singularize(canonical),
            _pluralize(canonical),
            raw_name.strip().lower(),
            _singularize(raw_name.strip().lower()),
            _pluralize(raw_name.strip().lower()),
        }
        for a in aliases:
            alias_map[a] = canonical
    return alias_map


def _canonical_table_name(name: str, alias_map: Dict[str, str]) -> str:
    key = name.strip().lower()
    if key in alias_map:
        return alias_map[key]
    singular = _singularize(key)
    if singular in alias_map:
        return alias_map[singular]
    return singular


def _extract_table_hint_from_filename(path: Path) -> Optional[str]:
    """
    示例：
    llama-extract-xxx-Credit.json -> credit
    """
    m = re.search(r"-([^-]+)\.json$", path.name, flags=re.IGNORECASE)
    if not m:
        return None
    hint = m.group(1).strip().lower()
    if hint in {"export", "result", "data"}:
        return None
    return hint


def _extract_table_rows(payload: Any) -> Dict[str, List[Any]]:
    """
    尝试从不同格式中提取 {table: rows}。
    """
    if not isinstance(payload, dict):
        return {}

    # 1) llamaextract: {"data": {...}, "extract_metadata": {...}}
    if isinstance(payload.get("data"), dict):
        source = payload["data"]
    else:
        # 2) 已经是 {table: rows}
        source = payload

    out: Dict[str, List[Any]] = {}
    for k, v in source.items():
        if isinstance(v, list):
            out[str(k)] = v
    return out


def clean_baseline_result(
    input_paths: List[Path],
    output_path: Path,
    schema_path: Optional[Path] = None,
    keep_empty_tables: bool = False,
) -> Dict[str, List[Any]]:
    alias_map = _build_table_alias_map(schema_path)
    merged: Dict[str, List[Any]] = {}

    for input_path in input_paths:
        with input_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        table_rows = _extract_table_rows(payload)
        file_table_hint = _extract_table_hint_from_filename(input_path)

        # 只在“文件中只有一个非空表”时使用 filename 作为兜底提示
        non_empty_keys = [k for k, rows in table_rows.items() if isinstance(rows, list) and len(rows) > 0]
        hint_canonical = (
            _canonical_table_name(file_table_hint, alias_map) if (file_table_hint and len(non_empty_keys) == 1) else None
        )

        for raw_table, rows in table_rows.items():
            if not isinstance(rows, list):
                continue
            if not rows and not keep_empty_tables:
                continue

            canonical = _canonical_table_name(raw_table, alias_map)

            # 若 key 明显是空占位（如 episodes/persons/... 但本文件主表来自文件名），用 hint 修正
            if hint_canonical and raw_table not in non_empty_keys:
                canonical = hint_canonical

            merged.setdefault(canonical, [])
            merged[canonical].extend(rows)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    return merged


def _collect_input_files(input_path: Path) -> List[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        # 默认跳过 *-export.json，避免与分表结果重复
        return sorted(
            [
                p
                for p in input_path.glob("*.json")
                if p.is_file() and not p.name.lower().endswith("-export.json")
            ]
        )
    raise FileNotFoundError(f"输入路径不存在: {input_path}")


def _resolve_schema_path(case_dir: Path, schema_root: Optional[Path]) -> Optional[Path]:
    """优先 case 内 schema.json，其次 schema_root/{domain}/{case}/schema.json。"""
    local = case_dir / "schema.json"
    if local.exists():
        return local
    if schema_root is None:
        return None
    # --schema 也可直接指向某个 schema.json（单 case）
    if schema_root.is_file():
        return schema_root if schema_root.exists() else None
    domain = case_dir.parent.name
    candidate = schema_root / domain / case_dir.name / "schema.json"
    if candidate.exists():
        return candidate
    return None


def _iter_result_cases(result_root: Path) -> List[Tuple[str, str, Path]]:
    """返回 [(domain, case, case_dir), ...]。"""
    cases: List[Tuple[str, str, Path]] = []
    if not result_root.is_dir():
        raise FileNotFoundError(f"输入目录不存在: {result_root}")

    for domain_dir in sorted(p for p in result_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for case_dir in sorted(
            p for p in domain_dir.iterdir() if p.is_dir() and p.name.startswith("case")
        ):
            cases.append((domain_dir.name, case_dir.name, case_dir))
    return cases


def _is_dataset_root(path: Path) -> bool:
    """判断是否为 result 根目录：存在 {domain}/case*/llama 结构。"""
    if not path.is_dir():
        return False
    for domain_dir in path.iterdir():
        if not domain_dir.is_dir() or domain_dir.name.startswith("."):
            continue
        for case_dir in domain_dir.iterdir():
            if case_dir.is_dir() and case_dir.name.startswith("case"):
                if (case_dir / "llama").is_dir() or (case_dir / "schema.json").exists():
                    return True
    return False


def _write_metadata(
    output_dir: Path,
    domain: str,
    case: str,
    input_files: List[Path],
    method_name: str = "llamaextract",
) -> None:
    metadata = {
        "model": method_name,
        "run_name": None,
        "method": method_name,
        "db_name": domain,
        "case_name": case,
        "timestamp": datetime.now().isoformat(),
        "source_files": [p.name for p in input_files],
        "pipeline_oracle": {
            "result_data_file": "extracted_data.json",
        },
    }
    with (output_dir / "metadata.json").open("w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def process_dataset(
    result_root: Path,
    output_root: Path,
    schema_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    扫描 result_root/{domain}/{case}/llama/*.json，
    写出到 output_root/{domain}/{case}/{domain}_{case}_llamaextract/extracted_data.json
    """
    summary = {
        "total_cases": 0,
        "processed": 0,
        "skipped_no_input": 0,
        "failed": 0,
        "details": [],
    }

    for domain, case, case_dir in _iter_result_cases(result_root):
        summary["total_cases"] += 1
        llama_dir = case_dir / "llama"
        detail: Dict[str, Any] = {
            "domain": domain,
            "case": case,
            "status": None,
            "input_dir": str(llama_dir),
        }

        try:
            if not llama_dir.is_dir():
                detail["status"] = "skipped_no_llama_dir"
                summary["skipped_no_input"] += 1
                summary["details"].append(detail)
                print(f"[SKIP] {domain}/{case}: 无 llama/")
                continue

            input_files = _collect_input_files(llama_dir)
            if not input_files:
                detail["status"] = "skipped_no_json"
                summary["skipped_no_input"] += 1
                summary["details"].append(detail)
                print(f"[SKIP] {domain}/{case}: llama/ 下无可用 JSON")
                continue

            schema_path = _resolve_schema_path(case_dir, schema_root)
            output_dir = output_root / domain / case / f"{domain}_{case}_llamaextract"
            output_path = output_dir / "extracted_data.json"

            cleaned = clean_baseline_result(
                input_paths=input_files,
                output_path=output_path,
                schema_path=schema_path,
            )
            _write_metadata(
                output_dir=output_dir,
                domain=domain,
                case=case,
                input_files=input_files,
            )

            detail.update(
                {
                    "status": "ok",
                    "output": str(output_path),
                    "schema": str(schema_path) if schema_path else None,
                    "n_input_files": len(input_files),
                    "n_tables": len(cleaned),
                    "tables": {t: len(rows) for t, rows in cleaned.items()},
                }
            )
            summary["processed"] += 1
            summary["details"].append(detail)
            print(
                f"[OK] {domain}/{case}: {len(input_files)} files -> "
                f"{output_path} ({len(cleaned)} tables)"
            )
        except Exception as exc:
            detail["status"] = "failed"
            detail["error"] = str(exc)
            summary["failed"] += 1
            summary["details"].append(detail)
            print(f"[FAIL] {domain}/{case}: {exc}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="清洗 baseline 结果为 {table: rows} 格式")
    parser.add_argument(
        "--input",
        required=True,
        help="输入：单 case 的 JSON/目录，或整数据集 result 根目录",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出：单 case 的 JSON 路径，或整数据集 llamaextract 根目录",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="可选：单 case 的 schema.json，或整数据集 schema 根目录",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    schema_path = Path(args.schema) if args.schema else None

    # 整数据集：--input/--output 都是根目录
    if _is_dataset_root(input_path):
        summary = process_dataset(
            result_root=input_path,
            output_root=output_path,
            schema_root=schema_path,
        )
        print(
            f"\n完成: total={summary['total_cases']}, "
            f"processed={summary['processed']}, "
            f"skipped={summary['skipped_no_input']}, "
            f"failed={summary['failed']}"
        )
        return

    input_files = _collect_input_files(input_path)
    if not input_files:
        raise ValueError(f"未找到可处理的 JSON 文件: {input_path}")

    cleaned = clean_baseline_result(
        input_paths=input_files,
        output_path=output_path,
        schema_path=schema_path,
    )

    print(f"输入文件数: {len(input_files)}")
    print(f"输出路径: {output_path}")
    print(f"输出表数: {len(cleaned)}")
    for t, rows in cleaned.items():
        print(f"  - {t}: {len(rows)} rows")


if __name__ == "__main__":
    main()
