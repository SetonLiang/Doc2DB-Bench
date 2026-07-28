from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import ColumnSchema, RelationalDatabase, Table, TableSchema


def write_table_with_assignments(
    path: str | Path,
    rows: list[dict[str, Any]],
    assignments: dict[str, Any],
) -> None:
    """Write table JSON with rows and capability_assignments."""
    payload = {"rows": rows, "capability_assignments": assignments}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_single_table_json(input_path: str | Path) -> Table:
    """Load legacy single-table JSON payload."""
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    return Table.model_validate(payload)


def load_database_from_directory(
    database_root: str | Path,
    tables_subdir: str = "tables",
    schema_filename: str = "schema.json",
) -> RelationalDatabase:
    """
    Load database-level input from:
    - <database_root>/<schema_filename>
    - <database_root>/<tables_subdir>/*.json
    """
    root = Path(database_root)
    schema_path = root / schema_filename
    tables_dir = root / tables_subdir

    if not schema_path.exists():
        raise FileNotFoundError(f"schema file not found: {schema_path}")
    if not tables_dir.exists():
        raise FileNotFoundError(f"tables directory not found: {tables_dir}")

    schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_map = _build_schema_map(schema_payload)

    table_models: list[Table] = []
    for table_path in sorted(tables_dir.glob("*.json")):
        table_name = table_path.stem
        raw_payload = json.loads(table_path.read_text(encoding="utf-8"))

        rows_payload: list[dict[str, Any]]
        capability_assignments: dict[str, Any] | None = None

        if isinstance(raw_payload, list):
            rows_payload = raw_payload
        elif isinstance(raw_payload, dict) and "rows" in raw_payload:
            rows_payload = raw_payload["rows"]
            assignments = raw_payload.get("capability_assignments")
            if isinstance(assignments, dict) and assignments:
                capability_assignments = assignments
        else:
            raise ValueError(f"table file must be a JSON array or object with 'rows': {table_path}")

        if not isinstance(rows_payload, list):
            raise ValueError(f"table rows must be a list: {table_path}")
        if any(not isinstance(row, dict) for row in rows_payload):
            raise ValueError(f"table rows must be JSON objects: {table_path}")

        table_schema = schema_map.get(table_name) or _infer_schema(table_name, rows_payload)
        table_models.append(
            Table(
                table_id=table_name,
                schema=table_schema,
                rows=rows_payload,
                capability_assignments=capability_assignments,
            )
        )

    return RelationalDatabase(
        database_id=root.name,
        tables=table_models,
        source_root=str(root.resolve()),
        tables_subdir=tables_subdir,
    )


def _build_schema_map(schema_payload: Any) -> dict[str, TableSchema]:
    """
    Support:
    - top-level array: [{name, fields:[...]}, ...]  (e.g. BIRD/cs_semester)
    - object with tables key: {"tables":[{name, fields:[...]}]}
    - normalized format: {"tables":[{table_name, columns:[...], primary_key:[...]}]}
    """
    if isinstance(schema_payload, list):
        tables = schema_payload
    elif isinstance(schema_payload, dict):
        tables = schema_payload.get("tables", [])
    else:
        tables = []
    if not isinstance(tables, list):
        return {}

    schema_map: dict[str, TableSchema] = {}
    for raw_table in tables:
        if not isinstance(raw_table, dict):
            continue

        table_name = str(raw_table.get("name") or raw_table.get("table_name") or "").strip()
        if not table_name:
            continue

        if isinstance(raw_table.get("columns"), list):
            columns = [
                ColumnSchema.model_validate(col)
                for col in raw_table["columns"]
                if isinstance(col, dict)
            ]
            primary_key = raw_table.get("primary_key") or []
            foreign_keys = raw_table.get("foreign_keys") or {}
            table_type = raw_table.get("type")
            schema_map[table_name] = TableSchema(
                table_name=table_name,
                columns=columns,
                primary_key=[str(col) for col in primary_key],
                foreign_keys={str(k): str(v) for k, v in foreign_keys.items()},
                table_type=str(table_type) if table_type else None,
            )
            continue

        fields = raw_table.get("fields", [])
        if not isinstance(fields, list):
            continue

        columns: list[ColumnSchema] = []
        primary_key: list[str] = []
        foreign_keys: dict[str, str] = {}
        for field in fields:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name", "")).strip()
            if not field_name:
                continue

            constraints = field.get("constraints")
            constraints = constraints if isinstance(constraints, dict) else {}

            rng = field.get("range")
            columns.append(
                ColumnSchema(
                    name=field_name,
                    dtype=str(field.get("type", "unknown")),
                    nullable=bool(constraints.get("nullable", True)),
                    description=field.get("description"),
                    range=str(rng).strip() if isinstance(rng, str) and rng.strip() else None,
                )
            )

            if bool(constraints.get("primary_key")):
                primary_key.append(field_name)

            fk = constraints.get("foreign_key")
            if isinstance(fk, dict) and fk.get("table") and fk.get("column"):
                foreign_keys[field_name] = f"{fk['table']}.{fk['column']}"

        table_type = raw_table.get("type")
        schema_map[table_name] = TableSchema(
            table_name=table_name,
            columns=columns,
            primary_key=primary_key,
            foreign_keys=foreign_keys,
            table_type=str(table_type) if table_type else None,
        )

    return schema_map


def _infer_schema(table_name: str, rows: list[dict[str, Any]]) -> TableSchema:
    """Best-effort schema fallback when schema entry is missing."""
    if not rows:
        return TableSchema(table_name=table_name, columns=[], primary_key=[], foreign_keys={}, table_type=None)

    all_columns: set[str] = set()
    for row in rows:
        all_columns.update(row.keys())

    columns: list[ColumnSchema] = []
    for col in sorted(all_columns):
        values = [row.get(col) for row in rows]
        non_null_values = [v for v in values if v is not None]
        sample = non_null_values[0] if non_null_values else None
        dtype = type(sample).__name__ if sample is not None else "unknown"
        nullable = any(v is None for v in values)
        columns.append(ColumnSchema(name=col, dtype=dtype, nullable=nullable, description=None))

    return TableSchema(table_name=table_name, columns=columns, primary_key=[], foreign_keys={}, table_type=None)
