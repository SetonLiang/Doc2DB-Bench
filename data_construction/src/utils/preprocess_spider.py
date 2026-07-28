from __future__ import annotations

import json
import pprint
import random
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional


_CREATE_TABLE_PATTERN = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*;',
    flags=re.IGNORECASE | re.DOTALL,
)

_PRIMARY_KEY_PATTERN = re.compile(r"PRIMARY\s+KEY\s*\((.*?)\)", flags=re.IGNORECASE | re.DOTALL)
_FOREIGN_KEY_PATTERN = re.compile(
    r'FOREIGN\s+KEY\s*\((.*?)\)\s+REFERENCES\s+(`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)',
    flags=re.IGNORECASE | re.DOTALL,
)
_REFERENCES_PATTERN = re.compile(
    r'REFERENCES\s+(`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)',
    flags=re.IGNORECASE | re.DOTALL,
)


def spider_preprocess(
    schema_sql_path: str | Path,
    output_json_path: str | Path | None = None,
    output_config_path: str | Path | None = None,
    output_tables_dir: str | Path | None = None,
    sqlite_path: str | Path | None = None,
    write_config: bool = True,
    write_tables: bool = True,
    max_records_per_table: Optional[int] = None,
) -> Path:
    """
    Convert Spider-style schema.sql into data_construction schema.json format.

    max_records_per_table: 与 preprocess_bird.preprocess_database 一致；非 None 时在写出
    tables/*.json 前按已生成的 schema.json + config.py 做 FK 感知采样（random.seed(42)）。

    Output structure matches loader-supported format:
    [
      {
        "id": "1",
        "name": "table_name",
        "type": "entity|relation",
        "fields": [
          {"name": "...", "type": "...", "constraints": {...}}
        ]
      }
    ]
    """
    sql_path = Path(schema_sql_path)
    if not sql_path.exists():
        raise FileNotFoundError(f"schema sql not found: {sql_path}")

    output_path = Path(output_json_path) if output_json_path else sql_path.with_name("schema.json")
    config_path = Path(output_config_path) if output_config_path else sql_path.with_name("config.py")
    tables_dir = Path(output_tables_dir) if output_tables_dir else sql_path.with_name("tables")
    sql_text = sql_path.read_text(encoding="utf-8")

    schema_items: list[dict[str, object]] = []
    for idx, (table_name, body) in enumerate(_CREATE_TABLE_PATTERN.findall(sql_text), start=1):
        normalized_table_name = _normalize_identifier(table_name)
        columns, primary_key, foreign_keys = _parse_table_body(body)
        table_type = _infer_table_type(
            table_name=normalized_table_name,
            columns=columns,
            primary_key=primary_key,
            foreign_keys=foreign_keys,
        )
        schema_items.append(
            {
                "id": str(idx),
                "name": normalized_table_name,
                "type": table_type,
                "fields": _build_fields(columns, primary_key, foreign_keys),
            }
        )

    _attach_inferred_foreign_keys(schema_items)

    output_path.write_text(json.dumps(schema_items, ensure_ascii=False, indent=2), encoding="utf-8")
    if write_config:
        config_payload = _build_config(schema_items)
        config_path.write_text(_render_python_config(config_payload), encoding="utf-8")
    if write_tables:
        _dump_tables_as_json(
            schema_items=schema_items,
            sql_text=sql_text,
            sql_path=sql_path,
            tables_dir=tables_dir,
            sqlite_path=Path(sqlite_path) if sqlite_path else None,
            sampling_root=output_path.parent,
            max_records_per_table=max_records_per_table,
        )
    return output_path


def _dump_tables_as_json(
    schema_items: list[dict[str, object]],
    sql_text: str,
    sql_path: Path,
    tables_dir: Path,
    sqlite_path: Path | None,
    sampling_root: Path,
    max_records_per_table: Optional[int],
) -> None:
    table_names = [str(item.get("name")) for item in schema_items if item.get("name")]
    if not table_names:
        return

    resolved_sqlite = _resolve_sqlite_path(sql_path=sql_path, sqlite_path=sqlite_path)
    conn: sqlite3.Connection | None = None
    try:
        if resolved_sqlite is not None:
            conn = sqlite3.connect(str(resolved_sqlite))
        else:
            # Fallback: build an in-memory sqlite from schema.sql, then export table rows.
            conn = sqlite3.connect(":memory:")
            conn.executescript(sql_text)
        conn.row_factory = sqlite3.Row

        tables_dir.mkdir(parents=True, exist_ok=True)
        all_tables: dict[str, list[dict[str, Any]]] = {}
        for table_name in table_names:
            all_tables[table_name] = _extract_table_rows(conn, table_name)

        if max_records_per_table is not None:
            from .preprocess_bird import _sample_tables_from_generated_files

            random.seed(42)
            tables_to_write = _sample_tables_from_generated_files(
                all_tables=all_tables,
                output_dir=str(sampling_root),
                max_records_per_table=max_records_per_table,
            )
        else:
            tables_to_write = all_tables

        for table_name, rows in tables_to_write.items():
            _write_rows_json(tables_dir / f"{table_name}.json", rows)
    finally:
        if conn is not None:
            conn.close()


def _resolve_sqlite_path(sql_path: Path, sqlite_path: Path | None) -> Path | None:
    if sqlite_path is not None and sqlite_path.exists():
        return sqlite_path

    db_name = sql_path.parent.name
    candidates = [
        sql_path.with_suffix(".sqlite"),
        sql_path.with_suffix(".db"),
        sql_path.parent / f"{db_name}.sqlite",
        sql_path.parent / f"{db_name}.db",
        sql_path.parents[3] / "dataset" / "spider" / "database" / db_name / f"{db_name}.sqlite"
        if len(sql_path.parents) >= 4
        else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate

    for candidate in sorted(sql_path.parent.glob("*.sqlite")) + sorted(sql_path.parent.glob("*.db")):
        if candidate.exists():
            return candidate
    return None


def _extract_table_rows(conn: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(f'SELECT * FROM "{table_name}"')
    rows = cursor.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            result.append({key: row[key] for key in row.keys()})
        else:
            result.append(dict(row))
    return result


def _write_rows_json(output_path: Path, rows: list[dict[str, Any]]) -> None:
    # Keep serialization style aligned with preprocess.py save logic.
    with output_path.open("w", encoding="utf-8") as f:
        f.write("[\n")
        for idx, row in enumerate(rows):
            line = json.dumps(row, ensure_ascii=False)
            if idx < len(rows) - 1:
                f.write(f"{line},\n")
            else:
                f.write(f"{line}\n")
        f.write("]\n")


def _build_config(schema_items: list[dict[str, Any]]) -> dict[str, Any]:
    tables = [str(item.get("name", "")) for item in schema_items if item.get("name")]
    if not tables:
        return {
            "anchor_table": [],
            "fk_relations": [],
            "relation_pairs": [],
            "anchor_cols": [{}],
            "cluster_cols": [],
        }

    table_meta = {str(item["name"]): item for item in schema_items if item.get("name")}
    fk_map: dict[str, list[tuple[str, str, str]]] = {}
    pk_map: dict[str, set[str]] = {}
    type_map: dict[str, str] = {}
    for table_name, item in table_meta.items():
        fields = item.get("fields", [])
        fk_map[table_name] = _extract_foreign_keys(fields)
        pk_map[table_name] = {
            str(field.get("name"))
            for field in fields
            if isinstance(field, dict)
            and isinstance(field.get("constraints"), dict)
            and bool(field["constraints"].get("primary_key"))
        }
        type_map[table_name] = str(item.get("type", "entity"))

    relation_tables = [table for table in tables if type_map.get(table) == "relation"]
    if not relation_tables:
        relation_tables = [table for table in tables if len(fk_map.get(table, [])) >= 2]

    fk_relations = _build_fk_relations(relation_tables, fk_map)
    relation_pairs = _build_relation_pairs(relation_tables, fk_map)
    anchor_cols = [_build_anchor_cols(table_meta, pk_map, fk_map, relation_tables=set(relation_tables))]
    cluster_cols: list[dict[str, Any]] = []

    return {
        "anchor_table": relation_tables,
        "fk_relations": fk_relations,
        "relation_pairs": relation_pairs,
        "anchor_cols": anchor_cols,
        "cluster_cols": cluster_cols,
    }


def _render_python_config(config_payload: dict[str, Any]) -> str:
    rendered = pprint.pformat(config_payload, sort_dicts=False, width=100)
    return f"CONFIG = {rendered}\n"


def _extract_foreign_keys(fields: Any) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    if not isinstance(fields, list):
        return result
    for field in fields:
        if not isinstance(field, dict):
            continue
        constraints = field.get("constraints")
        if not isinstance(constraints, dict):
            continue
        fk = constraints.get("foreign_key")
        if not isinstance(fk, dict):
            continue
        local_col = str(field.get("name", "")).strip()
        ref_table = str(fk.get("table", "")).strip()
        ref_col = str(fk.get("column", "")).strip()
        if local_col and ref_table and ref_col:
            result.append((local_col, ref_table, ref_col))
    return result


def _build_fk_relations(
    relation_tables: list[str],
    fk_map: dict[str, list[tuple[str, str, str]]],
) -> list[str]:
    # Keep only direct PK/FK links, no multi-hop expansion.
    relation_strings: list[str] = []
    for source in relation_tables:
        for _, target, _ in fk_map.get(source, []):
            relation_strings.append(f"{source}→{target}")

    deduped: list[str] = []
    seen = set()
    for rel in relation_strings:
        if rel not in seen:
            seen.add(rel)
            deduped.append(rel)
    return deduped


def _build_relation_pairs(
    relation_tables: list[str],
    fk_map: dict[str, list[tuple[str, str, str]]],
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for relation_table in relation_tables:
        # Merge into one pair per anchor/relation table, e.g.
        # Undergoes_patient_procedures_stay_physician_nurse.
        ordered_targets: list[tuple[str, str]] = []
        seen_target_tables: set[str] = set()
        for _, target_table, target_col in fk_map.get(relation_table, []):
            if target_table in seen_target_tables:
                continue
            seen_target_tables.add(target_table)
            ordered_targets.append((target_table, target_col))

        if len(ordered_targets) < 1:
            continue

        pair_name_suffix = "_".join(table.lower() for table, _ in ordered_targets)
        pairs.append(
            {
                "name": f"{relation_table}_{pair_name_suffix}",
                "output_fields": [(table, col) for table, col in ordered_targets],
            }
        )
    return pairs


def _build_anchor_cols(
    table_meta: dict[str, dict[str, Any]],
    pk_map: dict[str, set[str]],
    fk_map: dict[str, list[tuple[str, str, str]]],
    relation_tables: set[str],
) -> dict[str, list[str]]:
    anchor_map: dict[str, list[str]] = {}
    for table_name, item in table_meta.items():
        if table_name in relation_tables:
            continue
        fields = item.get("fields", [])
        if not isinstance(fields, list):
            continue
        fk_cols = {local for local, _, _ in fk_map.get(table_name, [])}
        candidate_cols = [
            str(field.get("name", "")).strip()
            for field in fields
            if isinstance(field, dict)
            and str(field.get("name", "")).strip()
            and str(field.get("name", "")).strip() not in pk_map.get(table_name, set())
            and str(field.get("name", "")).strip() not in fk_cols
        ]
        prioritized = _pick_anchor_columns(candidate_cols)
        if prioritized:
            anchor_map[table_name] = prioritized
    return anchor_map


def _pick_anchor_columns(columns: list[str]) -> list[str]:
    if not columns:
        return []
    # Prefer semantic attribute columns and avoid technical/audit-like suffixes.
    preferred_tokens = (
        "name",
        "title",
        "email",
        "phone",
        "address",
        "type",
        "status",
        "description",
        "gender",
        "grade",
        "cost",
        "date",
        "start",
        "end",
    )
    low_priority_tokens = ("id", "code", "key", "num")

    preferred = [col for col in columns if any(token in col.lower() for token in preferred_tokens)]
    if preferred:
        return preferred[:2]

    filtered = [col for col in columns if not any(token in col.lower() for token in low_priority_tokens)]
    if filtered:
        return filtered[:2]
    return columns[:2]


def _parse_table_body(body: str) -> tuple[list[dict[str, object]], list[str], dict[str, str]]:
    segments = _split_top_level_commas(body)
    columns: list[dict[str, object]] = []
    primary_key: list[str] = []
    foreign_keys: dict[str, str] = {}

    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment:
            continue

        upper = segment.upper()
        if upper.startswith("CONSTRAINT "):
            constraint_body = segment.split(None, 2)[-1].strip()
            _parse_constraint(constraint_body, primary_key, foreign_keys)
            continue
        if upper.startswith("PRIMARY KEY") or upper.startswith("FOREIGN KEY"):
            _parse_constraint(segment, primary_key, foreign_keys)
            continue

        column = _parse_column_definition(segment)
        if column is None:
            continue
        columns.append(column)

        col_name = str(column["name"])
        rest_upper = str(column["rest"]).upper()
        if "PRIMARY KEY" in rest_upper and col_name not in primary_key:
            primary_key.append(col_name)

        ref_match = _REFERENCES_PATTERN.search(str(column["rest"]))
        if ref_match:
            ref_table = ref_match.group(1).strip()
            ref_cols = _parse_name_list(ref_match.group(2))
            if ref_cols:
                foreign_keys[col_name] = f"{ref_table}.{ref_cols[0]}"

    column_order = [str(col["name"]) for col in columns]
    sorted_primary_key = [col for col in column_order if col in primary_key]
    for col in primary_key:
        if col not in sorted_primary_key:
            sorted_primary_key.append(col)

    return columns, sorted_primary_key, foreign_keys


def _parse_constraint(segment: str, primary_key: list[str], foreign_keys: dict[str, str]) -> None:
    pk_match = _PRIMARY_KEY_PATTERN.search(segment)
    if pk_match:
        for col_name in _parse_name_list(pk_match.group(1)):
            if col_name not in primary_key:
                primary_key.append(col_name)

    fk_match = _FOREIGN_KEY_PATTERN.search(segment)
    if fk_match:
        local_cols = _parse_name_list(fk_match.group(1))
        ref_table = _normalize_identifier(fk_match.group(2))
        ref_cols = _parse_name_list(fk_match.group(3))
        for local_col, ref_col in zip(local_cols, ref_cols):
            foreign_keys[local_col] = f"{ref_table}.{ref_col}"


def _parse_column_definition(segment: str) -> dict[str, object] | None:
    match = re.match(
        r'^\s*(`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?)\s*(.*)$',
        segment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    col_name = _normalize_identifier(match.group(1))
    col_type = match.group(2).strip()
    rest = match.group(3).strip()
    nullable = "NOT NULL" not in rest.upper()
    return {"name": col_name, "type": col_type, "nullable": nullable, "rest": rest}


def _build_fields(
    columns: list[dict[str, object]],
    primary_key: list[str],
    foreign_keys: dict[str, str],
) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    pk_set = set(primary_key)
    for col in columns:
        col_name = str(col["name"])
        constraints: dict[str, object] = {
            "nullable": bool(col.get("nullable", True)),
        }
        if col_name in pk_set:
            constraints["primary_key"] = True
        ref = foreign_keys.get(col_name)
        if ref:
            table_name, column_name = ref.split(".", 1)
            constraints["foreign_key"] = {"table": table_name, "column": column_name}

        fields.append(
            {
                "name": col_name,
                "type": str(col["type"]),
                "constraints": constraints,
            }
        )
    return fields


def _infer_table_type(
    table_name: str,
    columns: list[dict[str, object]],
    primary_key: list[str],
    foreign_keys: dict[str, str],
) -> str:
    if len(foreign_keys) >= 2:
        return "relation"
    if primary_key and len(primary_key) >= 2 and all(col in foreign_keys for col in primary_key):
        return "relation"

    # Heuristic for Spider-like schemas:
    # if a table has its own id column and at least one other *_id column,
    # treat it as a relation table even when explicit FK constraints are absent.
    col_names = {str(col.get("name", "")).strip().lower() for col in columns if isinstance(col, dict)}
    normalized_table_name = table_name.strip().lower()
    table_tokens = [token for token in re.split(r"[^a-z0-9]+", normalized_table_name) if token]

    own_id_candidates = {"id", f"{normalized_table_name}_id"}

    def _add_token_candidates(token: str) -> None:
        own_id_candidates.add(f"{token}_id")
        if token.endswith("s") and len(token) > 1:
            own_id_candidates.add(f"{token[:-1]}_id")
        if token.endswith("es") and len(token) > 2:
            own_id_candidates.add(f"{token[:-2]}_id")

    compact_table_name = "_".join(table_tokens)
    if compact_table_name:
        _add_token_candidates(compact_table_name)
    if table_tokens:
        # Prefer the head noun (last token), e.g. contacts -> contact_id,
        # customer_orders -> order_id.
        _add_token_candidates(table_tokens[-1])

    has_own_id = any(candidate in col_names for candidate in own_id_candidates)
    other_id_cols = {col_name for col_name in col_names if col_name.endswith("_id") and col_name not in own_id_candidates}
    if has_own_id and other_id_cols:
        return "relation"

    return "entity"


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_single_quote = False
    in_double_quote = False

    for idx, ch in enumerate(text):
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif not in_single_quote and not in_double_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(depth - 1, 0)
            elif ch == "," and depth == 0:
                parts.append(text[start:idx])
                start = idx + 1
    parts.append(text[start:])
    return parts


def _parse_name_list(raw_text: str) -> list[str]:
    items = []
    for piece in raw_text.split(","):
        normalized = _normalize_identifier(piece)
        if normalized:
            items.append(normalized)
    return items


def _normalize_identifier(raw_identifier: str) -> str:
    normalized = raw_identifier.strip()
    if not normalized:
        return normalized
    if (normalized[0] == normalized[-1] and normalized[0] in ('"', "'", "`")) or (
        normalized[0] == "[" and normalized[-1] == "]"
    ):
        return normalized[1:-1].strip()
    return normalized

def _attach_inferred_foreign_keys(schema_items: list[dict[str, Any]]) -> None:
    table_pk_cols: dict[str, set[str]] = {}
    for item in schema_items:
        table_name = str(item.get("name", "")).strip()
        fields = item.get("fields", [])
        if not table_name or not isinstance(fields, list):
            continue
        pk_cols = {
            str(field.get("name", "")).strip()
            for field in fields
            if isinstance(field, dict)
            and isinstance(field.get("constraints"), dict)
            and bool(field["constraints"].get("primary_key"))
        }
        table_pk_cols[table_name] = pk_cols

    for item in schema_items:
        source_table = str(item.get("name", "")).strip()
        fields = item.get("fields", [])
        if not source_table or not isinstance(fields, list):
            continue

        for field in fields:
            if not isinstance(field, dict):
                continue
            col_name = str(field.get("name", "")).strip()
            if not col_name or not col_name.lower().endswith("_id"):
                continue

            constraints = field.get("constraints")
            if not isinstance(constraints, dict):
                constraints = {}
                field["constraints"] = constraints
            if isinstance(constraints.get("foreign_key"), dict):
                continue

            candidates = _pick_fk_candidates(
                source_table=source_table,
                column_name=col_name,
                table_pk_cols=table_pk_cols,
            )
            if len(candidates) == 1:
                target_table = candidates[0]
                constraints["foreign_key"] = {"table": target_table, "column": col_name}


def _pick_fk_candidates(
    source_table: str,
    column_name: str,
    table_pk_cols: dict[str, set[str]],
) -> list[str]:
    direct_matches = [
        table_name
        for table_name, pk_cols in table_pk_cols.items()
        if table_name != source_table and column_name in pk_cols
    ]
    if len(direct_matches) <= 1:
        return direct_matches

    id_token = column_name[:-3].strip("_").lower()
    narrowed = [table_name for table_name in direct_matches if _table_name_matches_token(table_name, id_token)]
    if narrowed:
        return narrowed
    return direct_matches


def _table_name_matches_token(table_name: str, token: str) -> bool:
    lowered = table_name.lower()
    table_tokens = [piece for piece in re.split(r"[^a-z0-9]+", lowered) if piece]
    candidates = {lowered}
    for piece in table_tokens:
        candidates.add(piece)
        if piece.endswith("s") and len(piece) > 1:
            candidates.add(piece[:-1])
        if piece.endswith("es") and len(piece) > 2:
            candidates.add(piece[:-2])
    return token in candidates

