#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import random
from pathlib import Path
from typing import Any

from ..config import LLMRuntimeConfig
from ..llm import OpenAILLMClient


def _collect_used_ids(payload: dict[str, Any]) -> set[int]:
    used: set[int] = set()

    for row in payload.get("rows", []):
        if isinstance(row, dict) and "id" in row:
            try:
                used.add(int(row["id"]))
            except (TypeError, ValueError):
                pass

    cap = payload.get("capability_assignments", {})
    if isinstance(cap, dict):
        for k in cap.keys():
            try:
                used.add(int(k))
            except (TypeError, ValueError):
                pass
    return used


def _infer_col_template(payload: dict[str, Any]) -> dict[str, list[Any]]:
    cap = payload.get("capability_assignments", {})
    if isinstance(cap, dict) and cap:
        for v in cap.values():
            if isinstance(v, dict) and isinstance(v.get("col"), dict):
                return {str(k): [] for k in v["col"].keys()}

    rows = payload.get("rows", [])
    if rows and isinstance(rows[0], dict):
        return {k: [] for k in rows[0].keys() if k != "id"}

    return {}


def _load_anchor_cols_map(json_path: Path) -> dict[str, list[str]]:
    # dataset root for .../tables/MenuPage.json => .../menu
    dataset_root = json_path.parent.parent
    config_path = dataset_root / "config.py"
    if not config_path.exists():
        return {}

    spec = importlib.util.spec_from_file_location(
        f"dataset_cfg_{config_path.stem}",
        str(config_path),
    )
    if spec is None or spec.loader is None:
        return {}

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = getattr(module, "CONFIG", None)
    if not isinstance(cfg, dict):
        return {}

    raw_anchor_cols = cfg.get("anchor_cols")
    if not isinstance(raw_anchor_cols, list) or not raw_anchor_cols:
        return {}

    out: dict[str, list[str]] = {}
    for item in raw_anchor_cols:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            if isinstance(k, str) and isinstance(v, list):
                cols = [c for c in v if isinstance(c, str) and c.strip()]
                if cols:
                    out[k.strip().lower()] = cols
    return out


def _load_fk_targets_from_schema(json_path: Path) -> dict[str, str]:
    """Load FK column -> target entity table from dataset schema for current table."""
    dataset_root = json_path.parent.parent
    schema_path = dataset_root / "schema.json"
    if not schema_path.exists():
        return {}

    try:
        schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(schema_payload, list):
        return {}

    table_name = json_path.stem.strip().lower()
    target_table: dict[str, Any] | None = None
    for item in schema_payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip().lower() == table_name:
            target_table = item
            break
    if not target_table:
        return {}

    fields = target_table.get("fields")
    if not isinstance(fields, list):
        return {}

    out: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        col_name = field.get("name")
        constraints = field.get("constraints")
        if not isinstance(col_name, str) or not isinstance(constraints, dict):
            continue
        fk = constraints.get("foreign_key")
        if not isinstance(fk, dict):
            continue
        fk_table = fk.get("table")
        if not isinstance(fk_table, str) or not fk_table.strip():
            continue
        out[col_name.strip().lower()] = fk_table.strip().lower()
    return out


def _infer_fk_target_table(fk_column: str) -> str | None:
    # menu_id -> menu
    if not isinstance(fk_column, str):
        return None
    name = fk_column.strip()
    lower_name = name.lower()
    if not lower_name.endswith("_id"):
        return None
    entity = lower_name[:-3].strip()
    return entity if entity else None


def _build_anchor_value(entity_table: str, attrs: dict[str, str]) -> str:
    # menu.name=... | menu.physical_description=...
    parts: list[str] = []
    for k, v in attrs.items():
        vv = str(v).strip()
        if not vv:
            continue
        parts.append(f"{entity_table}.{k}={vv}")
    return " | ".join(parts)


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    content = text.strip()
    if "```json" in content:
        content = content.split("```json", 1)[1]
        content = content.split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1]
        content = content.split("```", 1)[0].strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _load_table_rows_by_name(json_path: Path, table_name: str) -> list[dict[str, Any]]:
    tables_dir = json_path.parent
    target_file = None
    for candidate in tables_dir.glob("*.json"):
        if candidate.stem.lower() == table_name.lower():
            target_file = candidate
            break
    if target_file is None or not target_file.exists():
        return []
    try:
        payload = json.loads(target_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _extract_rows(payload)


def _collect_entity_ids(rows: list[dict[str, Any]]) -> set[int]:
    used: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id")
        try:
            used.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    return used


def _generate_anchor_attrs_with_llm(
    llm: OpenAILLMClient,
    entity_table: str,
    anchor_cols: list[str],
    relation_id: int,
    fk_column: str,
    fk_value: Any,
    real_examples: list[dict[str, Any]],
    matched_example: dict[str, Any] | None,
) -> dict[str, str]:
    # Use real values from entity table as example schema, never synthetic placeholders.
    source_example = matched_example or (real_examples[0] if real_examples else None)
    example_obj = {
        col: "" if source_example is None else str(source_example.get(col, ""))
        for col in anchor_cols
    }
    example_json = json.dumps(example_obj, ensure_ascii=False)
    sample_values = [
        {col: str(row.get(col, "")) for col in anchor_cols}
        for row in real_examples[:3]
    ]

    # Keep prompt small and strict JSON-only
    prompt = (
        "Generate fictional but plausible anchor values for a referenced entity.\n"
        f"Entity table: {entity_table}\n"
        f"New relation row id: {relation_id}\n"
        f"Selected FK: {fk_column}={fk_value}\n"
        f"Target anchor columns: {anchor_cols}\n"
        f"Real anchor examples from {entity_table}: {json.dumps(sample_values, ensure_ascii=False)}\n"
        "Return JSON object only with exactly these keys.\n"
        f"Example: {example_json}"
    )
    print(f"LLM prompt for generating anchor values:\n{prompt}\n")
    # exit()

    raw = llm.generate(
        prompt,
        task="null_generation",
        temperature=1.0,
        max_tokens=512,
    )
    parsed = _extract_first_json_object(raw)

    print(parsed)
    if not parsed:
        raise ValueError("LLM did not return valid JSON object")

    out: dict[str, str] = {}
    for col in anchor_cols:
        val = parsed.get(col)
        if val is None:
            out[col] = ""
        else:
            out[col] = str(val).strip()
    return out


def _generate_multi_fk_anchor_attrs_with_llm(
    llm: OpenAILLMClient,
    relation_table: str,
    relation_id: int,
    fk_specs: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Generate anchor attrs for all FK targets in one LLM call for a single relation row."""
    if not fk_specs:
        return {}

    task_specs: list[dict[str, Any]] = []
    output_example: dict[str, dict[str, str]] = {}
    for spec in fk_specs:
        fk_col = str(spec["fk_column"])
        entity_table = str(spec["entity_table"])
        fk_value = spec["fk_value"]
        anchor_cols = [str(c) for c in spec.get("anchor_cols", []) if str(c).strip()]
        examples = spec.get("real_examples", [])

        sample_values = [
            {col: str(row.get(col, "")) for col in anchor_cols}
            for row in examples[:3]
            if isinstance(row, dict)
        ]
        task_specs.append(
            {
                "fk_column": fk_col,
                "entity_table": entity_table,
                "fk_value": fk_value,
                "anchor_cols": anchor_cols,
                "real_anchor_examples": sample_values,
            }
        )

        matched_example = spec.get("matched_example")
        example_obj = {
            col: "" if not isinstance(matched_example, dict) else str(matched_example.get(col, ""))
            for col in anchor_cols
        }
        output_example[fk_col] = example_obj

    prompt = (
        "Generate fictional but plausible anchor values for multiple referenced entities in ONE relation row.\n"
        f"Relation table: {relation_table}\n"
        f"New relation row id: {relation_id}\n"
        f"FK tasks: {json.dumps(task_specs, ensure_ascii=False)}\n"
        "Return ONE single-line JSON object only.\n"
        "Top-level keys must be fk_column names, and each value must be an object with exactly that FK task's anchor_cols.\n"
        f"Example: {json.dumps(output_example, ensure_ascii=False)}"
    )
    print(f"LLM prompt for generating multi-FK anchor values:\n{prompt}\n")

    raw = llm.generate(
        prompt,
        task="null_generation",
        temperature=1.0,
        max_tokens=768,
    )
    parsed = _extract_first_json_object(raw)
    if not parsed:
        raise ValueError("LLM did not return valid JSON object for multi-FK anchors")

    out: dict[str, dict[str, str]] = {}
    for spec in fk_specs:
        fk_col = str(spec["fk_column"])
        anchor_cols = [str(c) for c in spec.get("anchor_cols", []) if str(c).strip()]
        raw_obj = parsed.get(fk_col)
        attrs_obj = raw_obj if isinstance(raw_obj, dict) else {}
        out[fk_col] = {
            col: "" if attrs_obj.get(col) is None else str(attrs_obj.get(col)).strip()
            for col in anchor_cols
        }
    return out


def _fallback_anchor_attrs(entity_table: str, anchor_cols: list[str], idx: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for col in anchor_cols:
        if col.lower() == "name":
            out[col] = f"Synthetic {entity_table.title()} {idx}"
        elif col.lower() in {"physical_description", "description"}:
            out[col] = f"Fictional description for synthetic {entity_table} record {idx}"
        else:
            out[col] = f"synthetic_{entity_table}_{col}_{idx}"
    return out


def _sample_anchor_attrs_from_real_examples(
    real_examples: list[dict[str, Any]],
    anchor_cols: list[str],
    matched_example: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Sample anchor values from real entity rows for no-LLM mode."""
    source_row = matched_example if isinstance(matched_example, dict) else None
    if source_row is None and real_examples:
        source_row = random.choice(real_examples)
    if source_row is None:
        return None

    return {
        col: "" if source_row.get(col) is None else str(source_row.get(col)).strip()
        for col in anchor_cols
    }


def _make_new_row_from_template(
    template_row: dict[str, Any],
    new_id: int,
    fk_anchor_values: dict[str, str],
) -> dict[str, Any]:
    # Keep schema keys, but do not copy source values from template_row.
    new_row: dict[str, Any] = {
        k: ""
        for k in template_row.keys()
        if k != "id"
    }
    new_row["id"] = new_id
    for fk_col, val in fk_anchor_values.items():
        new_row[fk_col] = val
    return new_row

def _collect_grouped_fk_values(
    rows: list[dict[str, Any]],
    cap: dict[str, Any],
    fk_column: str,
) -> set[Any]:
    """Collect FK values that belong to rows with group capability labels."""
    GROUP_LABELS = {"RL_O2M", "RL_MB", "RL_MI"}
    grouped_row_ids: set[str] = set()
    for row_id, assignment in cap.items():
        if not isinstance(assignment, dict):
            continue
        row_labels = assignment.get("row", [])
        if not isinstance(row_labels, list):
            continue
        if any(lbl in GROUP_LABELS for lbl in row_labels):
            grouped_row_ids.add(str(row_id))

    blocked_fk_values: set[Any] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id", ""))
        if row_id in grouped_row_ids:
            fk_val = row.get(fk_column)
            if fk_val is not None:
                blocked_fk_values.add(fk_val)
    return blocked_fk_values

def add_null_relations(
    json_path: Path,
    count: int,
    seed: int | None = None,
    use_llm: bool = True,
) -> list[int]:
    raw_payload = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(raw_payload, list):
        payload: dict[str, Any] = {
            "rows": raw_payload,
            "capability_assignments": {},
        }
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        raise ValueError("JSON top-level must be object or array")

    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows is empty or invalid")

    if seed is not None:
        random.seed(seed)

    if "capability_assignments" not in payload or not isinstance(payload["capability_assignments"], dict):
        payload["capability_assignments"] = {}

    cap: dict[str, Any] = payload["capability_assignments"]
    used_ids = _collect_used_ids(payload)
    col_template = _infer_col_template(payload)
    anchor_cols_map = _load_anchor_cols_map(json_path)
    schema_fk_targets = _load_fk_targets_from_schema(json_path)

    llm: OpenAILLMClient | None = None
    if use_llm:
        cfg = LLMRuntimeConfig()
        llm = OpenAILLMClient(config=cfg, api_key=cfg.api_key)

    added_ids: list[int] = []
    for i in range(count):
        # 1) random relation id
        while True:
            new_id = random.randint(1990, 2000)
            if new_id not in used_ids:
                break
        used_ids.add(new_id)
        added_ids.append(new_id)

        # 2) pick a seed row and synthesize FK anchor strings
        template_row = random.choice(rows) if rows else {}
        fk_anchor_values: dict[str, str] = {}
        fk_specs: list[dict[str, Any]] = []
        for col_name in template_row.keys():
            col_key = str(col_name).strip().lower()
            target_entity = schema_fk_targets.get(col_key) or _infer_fk_target_table(col_name)
            if not target_entity:
                continue
            anchor_cols = anchor_cols_map.get(target_entity, [])
            if not anchor_cols:
                continue

            entity_rows = _load_table_rows_by_name(json_path, target_entity)

            # Generate FK id (e.g., menu_id) instead of reusing real ids.
            entity_used_ids = _collect_entity_ids(entity_rows)

            blocked_fk_values = _collect_grouped_fk_values(rows, cap, col_name)

            
            # while True:
            #     selected_fk_value = random.randint(10, 20)
            #     if selected_fk_value not in entity_used_ids:
            #         break
            attempt = 0
            while True:
                selected_fk_value = random.randint(10, 20)
                attempt += 1
                if (
                    selected_fk_value not in entity_used_ids
                    and selected_fk_value not in blocked_fk_values
                ):
                    break
                if attempt > 200:
                    # expand range if exhausted
                    selected_fk_value = random.randint(2001, 9999)
                    break

            # Use a real row only as style/example reference for LLM prompt.
            matched_example = random.choice(entity_rows) if entity_rows else None

            fk_specs.append(
                {
                    "fk_column": col_name,
                    "entity_table": target_entity,
                    "fk_value": selected_fk_value,
                    "anchor_cols": anchor_cols,
                    "real_examples": entity_rows,
                    "matched_example": matched_example,
                }
            )

        multi_attrs_by_fk: dict[str, dict[str, str]] = {}
        if llm is not None and fk_specs:
            try:
                multi_attrs_by_fk = _generate_multi_fk_anchor_attrs_with_llm(
                    llm=llm,
                    relation_table=json_path.stem,
                    relation_id=new_id,
                    fk_specs=fk_specs,
                )
            except Exception:
                multi_attrs_by_fk = {}

        for spec in fk_specs:
            col_name = str(spec["fk_column"])
            target_entity = str(spec["entity_table"])
            anchor_cols = [str(c) for c in spec.get("anchor_cols", []) if str(c).strip()]
            real_examples = [
                row for row in spec.get("real_examples", [])
                if isinstance(row, dict)
            ]
            matched_example = (
                spec.get("matched_example")
                if isinstance(spec.get("matched_example"), dict)
                else None
            )

            attrs: dict[str, str]
            if multi_attrs_by_fk.get(col_name):
                attrs = multi_attrs_by_fk[col_name]
            else:
                sampled_attrs = _sample_anchor_attrs_from_real_examples(
                    real_examples=real_examples,
                    anchor_cols=anchor_cols,
                    matched_example=matched_example,
                )
                attrs = sampled_attrs or _fallback_anchor_attrs(target_entity, anchor_cols, i + 1)

            fk_anchor_values[col_name] = _build_anchor_value(target_entity, attrs)

        # append synthetic row so row_key can be matched by labeling/refiner
        rows.append(
            _make_new_row_from_template(
                template_row=template_row,
                new_id=new_id,
                fk_anchor_values=fk_anchor_values,
            )
        )

        # 3) assignment for the same new id
        cap[str(new_id)] = {
            "row": ["IC_NR"],
            "col": {k: [] for k in col_template.keys()},
        }

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return added_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Append synthetic IC_NR relation rows and assignments")
    parser.add_argument(
        "--json-path",
        type=Path,
        required=True,
        help="Target table json path, e.g. .../tables/MenuPage.json",
    )
    parser.add_argument("--count", type=int, default=3, help="How many synthetic rows to add")
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM generation and use fallback templates",
    )
    args = parser.parse_args()

    if args.count <= 0:
        raise ValueError("--count must be > 0")
    if not args.json_path.exists():
        raise FileNotFoundError(f"File not found: {args.json_path}")

    added = add_null_relations(
        json_path=args.json_path,
        count=args.count,
        seed=args.seed,
        use_llm=not args.no_llm,
    )
    print(f"Added {len(added)} synthetic IC_NR rows: {added}")


if __name__ == "__main__":
    main()
