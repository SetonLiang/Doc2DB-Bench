from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from .base import BaseAgent
from ..capabilities import get_capability_definitions, PILLAR1_KEYS, PILLAR2_KEYS
from ..llm.base import BaseLLMClient
from ..models import AnnotatedCell, CapabilityLabel, CapabilityMatrix, CellRef, LabelingInput, Table
from ..utils.input_loader import write_table_with_assignments


CAPABILITY_ASSIGNMENT_SYSTEM = (
    "You are a professional writer responsible for assigning writing strategies to specific rows and cells "
    "in a table. Your goal is to make the text generated based on this table highly diverse and structurally complex. "
)

PILLAR1_ASSIGNMENT_PROMPT = """
Goal: Assign intra-table (Pillar 1) capability labels to target cells in the entity table.

Input Table:
{markdown_table}

Non-target Key Columns (STRICTLY DO NOT LABEL THESE):
{non_target_key_columns}

Attribute Schema (All Columns with Descriptions):
{attribute_descriptions}

Capability Definitions (Pillar 1 - Entity Extraction & Reasoning):
{capability_definitions}

Target Label Ratio: 0.3

Instructions:
1. Assign labels that increase single-table extraction difficulty while preserving correctness.
2. Type-Strategy Compatibility (CRITICAL): You MUST independently analyze the semantic nature and data type of each attribute. Only assign strategies that logically and mathematically make sense for the cell's specific value type.
3. A cell may have zero, one or multiple strategies. The default is no strategy.
4. Assign strategies creatively but ensure the text remains fluent. Avoid over-complicating simple facts.
5. DO NOT assign labels to Non-target Key Columns (Primary Keys and Anchor Columns).
6. For composite primary key rows, use ", " as the row key separator.
7. Label coverage constraint: at least the target ratio of assignable cells must contain non-empty labels.
8. Return JSON only.

Output JSON schema:
```json
{{
    "assignments": {{
        "<primary_key_1>": {{
            "<attribute_1>": []
            "<attribute_2>": []
            ...
        }},
        ...
    }}
}}
```


**Example 1 - Single Primary Key:**
* **Table:**
| a | b | c | d |
|----|----|----|----|
| 1 | 5 | 10 | 15 |
| 2 | 20 | 25 |  |
* **Primary Key:** a

**Example Output:**
```json
{{
    "assignments": {{
        "1": {{
            "b": ["TA_US"],
            "c": [],
            "d": []
        }},
        "2": {{
            "b": ["RI_AC"],
            "c": ["TA_US"]
        }}
    }}
}}
```

**Example 2 - Composite Primary Key:**
* **Table:**
| Year | Quarter | Revenue | Profit |
|------|---------|---------|--------|
| 2023 | Q1      | 100M    | 20M    |
| 2023 | Q2      | 120M    | 25M    |
* **Primary Key:** [Year, Quarter] (composite key)

**Example Output:**
```json
{{
    "assignments": {{
        "2023, Q1": {{
            "Revenue": ["TA_FC"],
            "Profit": ["RI_TC"]
        }},
        "2023, Q2": {{
            "Revenue": [],
            "Profit": ["TA_FC"]
        }}
    }}
}}
```
"""

PILLAR2_ASSIGNMENT_PROMPT = """
Goal: Assign inter-table (Pillar 2) capability labels to target cells in the relation table.

Input Table:
{markdown_table}

Table Schema Context:
{relation_context}

Attribute Schema (All Columns with Descriptions):
{attribute_descriptions}

Capability Definitions (Pillar 2 - Structural & Relational Complexity):
{capability_definitions}

Target Label Ratio:
{label_ratio}

Label Assignment Constraints:
- `IDR_ED`, `RL_O2M`, `RL_MB`, and `RL_MI` must each appear alone. Do NOT combine any of them with other labels on the same row.
- `IDR_ED`, `RL_O2M` requires that the same foreign key entity appears in at least two rows. Do NOT assign `RL_O2M` to a row whose referenced entity is unique in the table.


Instructions:
1. Assign labels that increase the inter-table linking, topology, and structural extraction difficulty.
2. Topology-Strategy Compatibility (CRITICAL): You MUST independently analyze the structural distribution and relationship of the rows. Only assign relational strategies that are logically suitable for the given topological context.
3. Output ONLY row-level labels per relation row_key. Do NOT output per-attribute labels for Pillar 2.
4. A row may have zero, one or multiple strategies. The default is no strategy.
5. The `assignments` top-level key MUST use the relation table primary key string.
6. For composite primary keys, key format must be `<pk1>, <pk2>` (for example: `12, 10`).
7. Label coverage constraint: at least the target ratio of rows must contain non-empty row-level labels.
8. Return JSON only.

Output JSON schema:
```json
{{
    "assignments": {{
        "<primary_key_1>": [],
        "<primary_key_2>": [],
        ...
    }}
}}
```

**Example 1:**
* **Table:**
| course_id | student_id | grade | sat |
|----|----|----|----|
| 12 | 10 | A | 5 |
| 3 | 13 | C | 2 |
* **Primary Key:** [course_id, student_id] (composite key)

**Example Output:**
```json
{{
    "assignments": {{
        "12, 10": ["GR_TS"],
        "3, 13": [],
    }}
}}
```

"""


class LabelingAgent(BaseAgent[LabelingInput, CapabilityMatrix]):
    def __init__(self, llm: BaseLLMClient | None = None) -> None:
        self.llm = llm
        self._fk_anchor_lookup_cache: dict[str, dict[Any, str]] = {}

    def run(self, input: LabelingInput) -> CapabilityMatrix:
        """Step 1: annotate table cells with multi-label capability tags and hidden-result flags."""
        if input.table.capability_assignments:
            print("LabelingAgent: 111")
            return self._build_matrix_from_assignment(
                input, input.table.capability_assignments
            )

        if self.llm is not None:
            print("LabelingAgent: 222")
            assignment = self._assign_capabilities_with_llm(input)
            if assignment is not None:
                matrix = self._build_matrix_from_assignment(input, assignment)
                if input.write_back_path:
                    self._write_assignments(
                        input.write_back_path,
                        input.table,
                        assignment,
                    )
                return matrix

        matrix = self._heuristic_fallback(input)
        if input.write_back_path:
            assignment = self._matrix_to_assignment(matrix, input.table, input=input)
            is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
            if is_relation:
                assignment = self._format_relation_assignment_for_writeback(
                    merged_assignments=assignment,
                    p2_raw_assignments=None,
                    input=input,
                )
            self._write_assignments(
                input.write_back_path,
                input.table,
                assignment,
            )
        return matrix

    def _assign_capabilities_with_llm(self, input: LabelingInput) -> dict[str, dict[str, list[str]]] | None:
        markdown_table = self._rows_to_markdown(input.table.rows)
        attribute_descriptions = self._format_attribute_descriptions(input.table)
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        ratio = self._target_label_ratio(input)
        ratio_hint = f"{ratio:.0%} (0-1 scale: {ratio:.2f})"

        if not is_relation:
            capability_dict = {
                key: value
                for key, value in get_capability_definitions(input.table.schema.table_type).items()
                if key in PILLAR1_KEYS
            }
            capability_defs = "\n".join(
                [f"{idx + 1}. {text}" for idx, text in enumerate(capability_dict.values())]
            )
            pk_display = self._format_primary_key(input.table.schema.primary_key)
            anchor_cols = sorted(self._load_anchor_columns(input))
            anchor_display = ", ".join(anchor_cols) if anchor_cols else "None"
            non_target_key_columns = (
                f"Primary Key: {pk_display}; Entity Anchor Columns: {anchor_display}"
            )
            prompt = PILLAR1_ASSIGNMENT_PROMPT.format(
                markdown_table=markdown_table,
                non_target_key_columns=non_target_key_columns,
                attribute_descriptions=attribute_descriptions,
                capability_definitions=capability_defs,
                label_ratio=ratio_hint,
            )
            print(prompt)
            # exit()
            return self._request_assignments_with_prompt(
                prompt=prompt,
                input=input,
                require_full=True,
                assignment_mode="cell",
            )

        # Relation tables: run Pillar1 (non-FK attributes) first, then Pillar2 and merge.
        p1_defs_dict = {
            key: value
            for key, value in get_capability_definitions(None).items()
            if key in PILLAR1_KEYS
        }
        p1_defs = "\n".join([f"{idx + 1}. {text}" for idx, text in enumerate(p1_defs_dict.values())])

        pk_display = self._format_primary_key(input.table.schema.primary_key)
        fk_cols = sorted(input.table.schema.foreign_keys.keys())
        fk_display = ", ".join(fk_cols) if fk_cols else "None"
        non_target_key_columns = (
            f"Primary Key: {pk_display}; Relation FK Columns: {fk_display}"
        )
        p1_prompt = PILLAR1_ASSIGNMENT_PROMPT.format(
            markdown_table=markdown_table,
            non_target_key_columns=non_target_key_columns,
            attribute_descriptions=attribute_descriptions,
            capability_definitions=p1_defs,
            label_ratio=ratio_hint,
        )
        print(p1_prompt)
        # exit()
        p2_defs_dict = {
            key: value
            for key, value in get_capability_definitions(input.table.schema.table_type).items()
            if key in PILLAR2_KEYS
        }
        p2_defs = "\n".join([f"{idx + 1}. {text}" for idx, text in enumerate(p2_defs_dict.values())])
        relation_context = self._format_relation_context(input)
        p2_prompt = PILLAR2_ASSIGNMENT_PROMPT.format(
            markdown_table=markdown_table,
            relation_context=relation_context,
            attribute_descriptions=attribute_descriptions,
            capability_definitions=p2_defs,
            label_ratio=ratio_hint,
        )
        # print(p2_prompt)
        # exit()
        p1_assignments = self._request_assignments_with_prompt(
            prompt=p1_prompt,
            input=input,
            require_full=False,
            assignment_mode="cell",
        )
        p2_raw_assignments = self._request_assignments_with_prompt(
            prompt=p2_prompt,
            input=input,
            require_full=False,
            assignment_mode="row",
        )
        print(p2_raw_assignments)
        # p1_assignments = None
        merged = self._merge_assignments(p1_assignments, p2_raw_assignments)

        # print("----")
        print(merged)
        # exit()
        return merged

    def _request_assignments_with_prompt(
        self,
        prompt: str,
        input: LabelingInput,
        require_full: bool,
        assignment_mode: str = "cell",
    ) -> dict[str, dict[str, list[str]]] | None:
        retries = max(1, input.config.runtime.max_validation_retries + 1)
        for _ in range(retries):
            raw = self.llm.generate(
                prompt,
                system_prompt=CAPABILITY_ASSIGNMENT_SYSTEM,
                task="labeling",
                temperature=1.0,
            )
            parsed = self._safe_parse_json(raw)
            if not parsed:
                continue

            assignments = parsed.get("assignments")
            if not isinstance(assignments, dict):
                continue
            assignments = self._fill_missing_assignments(
                assignments=assignments,
                input=input,
                assignment_mode=assignment_mode,
            )
            if not require_full:
                return assignments
            if self._validate_assignment(assignments, input):
                return assignments
        return None

    def _target_label_ratio(self, input: LabelingInput) -> float:
        value = getattr(getattr(input.config, "base", None), "label_ratio", 0.5)
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            ratio = 0.5
        return max(0.0, min(1.0, ratio))

    def _fill_missing_assignments(
        self,
        assignments: dict[str, Any],
        input: LabelingInput,
        assignment_mode: str,
    ) -> dict[str, Any]:
        filled: dict[str, Any] = dict(assignments)
        excluded_columns = self._resolve_excluded_columns(input)
        row_keys: list[str] = []
        row_to_columns: dict[str, list[str]] = {}

        for r_idx, row in enumerate(input.table.rows):
            row_key = self._build_row_key(
                row=row,
                primary_key=input.table.schema.primary_key,
                row_index=r_idx,
                input=input,
            )
            row_keys.append(row_key)
            row_to_columns[row_key] = [
                col_name
                for col_name, value in row.items()
                if col_name not in excluded_columns and value not in (None, "")
            ]

        if assignment_mode == "row":
            for row_key in row_keys:
                payload = filled.get(row_key)
                if isinstance(payload, list):
                    continue
                filled[row_key] = []
            return filled

        for row_key in row_keys:
            payload = filled.get(row_key)
            if not isinstance(payload, dict):
                payload = {}

            expected_cols = row_to_columns.get(row_key, [])
            for col_name in expected_cols:
                col_labels = payload.get(col_name)
                if not isinstance(col_labels, list):
                    payload[col_name] = []
            filled[row_key] = payload
        return filled

    def _merge_assignments(
        self,
        first: dict[str, Any] | None,
        second: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        first = first or {}
        second = second or {}

        all_rows = {
            row_key
            for row_key in list(first.keys()) + list(second.keys())
            if isinstance(row_key, str)
        }

        for row_key in all_rows:
            row_labels: list[str] = []
            col_labels: dict[str, list[str]] = {}

            first_row = first.get(row_key)
            if isinstance(first_row, dict):
                # first 默认是列级输出：{row_key: {col: [labels]}}
                # 也兼容嵌套格式：{row_key: {"col": {...}, "row": [...]}}
                nested_row = first_row.get("row")
                if isinstance(nested_row, list):
                    for label in nested_row:
                        if isinstance(label, str) and label not in row_labels:
                            row_labels.append(label)

                first_cols = first_row.get("col") if isinstance(first_row.get("col"), dict) else first_row
                if isinstance(first_cols, dict):
                    for col_name, labels in first_cols.items():
                        if col_name in {"row", "col"} or not isinstance(col_name, str) or not isinstance(labels, list):
                            continue
                        bucket = col_labels.setdefault(col_name, [])
                        for label in labels:
                            if isinstance(label, str) and label not in bucket:
                                bucket.append(label)

            second_row = second.get(row_key)
            if isinstance(second_row, list):
                for label in second_row:
                    if isinstance(label, str) and label not in row_labels:
                        row_labels.append(label)
            elif isinstance(second_row, dict):
                nested_row = second_row.get("row")
                if isinstance(nested_row, list):
                    for label in nested_row:
                        if isinstance(label, str) and label not in row_labels:
                            row_labels.append(label)

                second_cols = second_row.get("col") if isinstance(second_row.get("col"), dict) else second_row
                if isinstance(second_cols, dict):
                    for col_name, labels in second_cols.items():
                        if col_name in {"row", "col"} or not isinstance(col_name, str) or not isinstance(labels, list):
                            continue
                        bucket = col_labels.setdefault(col_name, [])
                        for label in labels:
                            if isinstance(label, str) and label not in bucket:
                                bucket.append(label)

            merged[row_key] = {"row": row_labels, "col": col_labels}

        return merged


    def _format_relation_context(self, input: LabelingInput) -> str:
        """Inject structural relation schema context for Pillar 2 prompting."""
        pk_display = self._format_primary_key(input.table.schema.primary_key)
        fk_map = input.table.schema.foreign_keys
        if not fk_map:
            return f"- Primary Key: {pk_display}\n- Foreign Keys: None"

        fk_lines: list[str] = []
        for local_col, target in fk_map.items():
            fk_lines.append(f"  * Column '{local_col}' -> references '{target}'")
        return f"- Primary Key: {pk_display}\n- Foreign Keys:\n" + "\n".join(fk_lines)

    def _get_cell_labels_from_row_payload(
        self, row_payload: dict[str, Any], col_name: str
    ) -> list[str]:
        """Extract labels for a column from row payload. Supports flat and nested col format."""
        if not isinstance(row_payload, dict):
            return []
        # Nested format: {"row": [labels], "col": {col_name: [labels], ...}}
        row_level = row_payload.get("row")
        label_codes: list[str] = []
        if isinstance(row_level, list):
            label_codes.extend(c for c in row_level if isinstance(c, str) and c)

        col_labels = row_payload.get("col")
        if isinstance(col_labels, dict):
            # Backward compatibility for old data where row-level labels were stored in col.capability.
            capability = col_labels.get("capability")
            if isinstance(capability, list):
                for c in capability:
                    if isinstance(c, str) and c not in label_codes:
                        label_codes.append(c)
            col_specific = col_labels.get(col_name)
            if isinstance(col_specific, list):
                for c in col_specific:
                    if isinstance(c, str) and c not in label_codes:
                        label_codes.append(c)
            return label_codes
        # Flat format: {col_name: [labels], ...}
        labels = row_payload.get(col_name)
        if isinstance(labels, list):
            for c in labels:
                if isinstance(c, str) and c not in label_codes:
                    label_codes.append(c)
        return label_codes

    def _build_matrix_from_assignment(
        self,
        input: LabelingInput,
        assignments: dict[str, Any],
    ) -> CapabilityMatrix:
        excluded_columns = self._resolve_excluded_columns(input)
        allowed_labels = self._allowed_labels_for_table(input.table.schema.table_type)
        cells: list[AnnotatedCell] = []

        for r_idx, row in enumerate(input.table.rows):
            row_payload = self._resolve_assignment_row_payload(
                assignments=assignments,
                row=row,
                primary_key=input.table.schema.primary_key,
                row_index=r_idx,
            ) or {}
            for col_name, value in row.items():
                label_codes = self._get_cell_labels_from_row_payload(row_payload, col_name)
                labels = self._map_label_codes(label_codes, allowed=allowed_labels)

                # Non-target cells are always emitted with empty labels for complete matrix coverage.
                if col_name in excluded_columns or value in (None, ""):
                    labels = []

                hide_final_value = any(label in {CapabilityLabel.RI_AC, CapabilityLabel.TD_CA} for label in labels)
                cells.append(
                    AnnotatedCell(
                        table_id=input.table.table_id,
                        ref=CellRef(row_index=r_idx, column_name=col_name),
                        labels=labels,
                        hide_final_value=hide_final_value,
                    )
                )

        return CapabilityMatrix(table_id=input.table.table_id, cells=cells)

    def _heuristic_fallback(self, input: LabelingInput) -> CapabilityMatrix:
        excluded_columns = self._resolve_excluded_columns(input)
        fk_columns = set(input.table.schema.foreign_keys.keys())
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        cells: list[AnnotatedCell] = []
        for r_idx, row in enumerate(input.table.rows):
            for col_name, value in row.items():
                labels: list[CapabilityLabel] = []

                if col_name in excluded_columns or value in (None, ""):
                    labels = []
                elif is_relation:
                    if col_name in fk_columns:
                        labels.append(CapabilityLabel.RL_IFK)
                        labels.append(CapabilityLabel.IC_RI)
                    else:
                        labels.append(CapabilityLabel.GR_CTA)
                else:
                    if isinstance(value, (int, float)):
                        labels.append(CapabilityLabel.RI_AC)
                        labels.append(CapabilityLabel.TD_AS)
                    elif isinstance(value, str) and any(char.isdigit() for char in value):
                        labels.append(CapabilityLabel.TA_FC)
                    else:
                        labels.append(CapabilityLabel.TD_AS)

                hide_final_value = any(label in {CapabilityLabel.RI_AC, CapabilityLabel.TD_CA} for label in labels)
                cells.append(
                    AnnotatedCell(
                        table_id=input.table.table_id,
                        ref=CellRef(row_index=r_idx, column_name=col_name),
                        labels=labels,
                        hide_final_value=hide_final_value,
                    )
                )

        return CapabilityMatrix(table_id=input.table.table_id, cells=cells)

    def _validate_assignment(self, assignments: dict[str, dict[str, list[str]]], input: LabelingInput) -> bool:
        excluded_columns = self._resolve_excluded_columns(input)
        for r_idx, row in enumerate(input.table.rows):
            row_payload = self._resolve_assignment_row_payload(
                assignments=assignments,
                row=row,
                primary_key=input.table.schema.primary_key,
                row_index=r_idx,
            )
            if not isinstance(row_payload, dict):
                row_payload = {}

            for col_name, value in row.items():
                if col_name in excluded_columns or value in (None, ""):
                    continue
                labels = row_payload.get(col_name)
                if not isinstance(labels, list):
                    return False
        return True

    def _expected_assignable_cells(self, input: LabelingInput) -> set[str]:
        expected: set[str] = set()
        excluded_columns = self._resolve_excluded_columns(input)
        for r_idx, row in enumerate(input.table.rows):
            row_key = self._build_row_key(
                row=row,
                primary_key=input.table.schema.primary_key,
                row_index=r_idx,
                input=input,
            )
            for col_name, value in row.items():
                if col_name in excluded_columns:
                    continue
                if value in (None, ""):
                    continue
                expected.add(f"{row_key},{col_name}")
        return expected

    def _build_row_key(
        self,
        row: dict[str, Any],
        primary_key: list[str],
        row_index: int,
        input: LabelingInput | None = None,
    ) -> str:
        return self._build_legacy_row_key(row=row, primary_key=primary_key, row_index=row_index)

    def _build_legacy_row_key(self, row: dict[str, Any], primary_key: list[str], row_index: int) -> str:
        if primary_key:
            parts = [str(row.get(col, "")) for col in primary_key]
            return ", ".join(parts)
        return str(row_index)

    def _assignment_key_candidates(
        self,
        row: dict[str, Any],
        primary_key: list[str],
        row_index: int,
    ) -> list[str]:
        keys: list[str] = []
        composite = self._build_legacy_row_key(row=row, primary_key=primary_key, row_index=row_index)
        if composite:
            keys.append(composite)

        row_id = row.get("id")
        if row_id not in (None, ""):
            id_key = str(row_id)
            if id_key and id_key not in keys:
                keys.append(id_key)

        index_key = str(row_index)
        if index_key not in keys:
            keys.append(index_key)
        return keys

    def _resolve_assignment_row_payload(
        self,
        assignments: dict[str, Any],
        row: dict[str, Any],
        primary_key: list[str],
        row_index: int,
    ) -> dict[str, Any] | None:
        for key in self._assignment_key_candidates(row, primary_key, row_index):
            payload = assignments.get(key)
            if isinstance(payload, dict):
                return payload
        return None

    def _build_relation_row_key(self, row: dict[str, Any], input: LabelingInput) -> str | None:
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        if not is_relation:
            return None

        fk_map = input.table.schema.foreign_keys
        if not fk_map:
            return None

        preferred_fk_cols = [col for col in input.table.schema.primary_key if col in fk_map]
        if not preferred_fk_cols:
            preferred_fk_cols = list(fk_map.keys())

        for fk_col in preferred_fk_cols:
            fk_value = row.get(fk_col)
            if fk_value in (None, ""):
                continue
            resolved = self._resolve_fk_anchor_value(input, fk_col=fk_col, fk_value=fk_value)
            if resolved:
                return f"row_key: {resolved}"
            return f"row_key: {str(fk_value).strip()}"
        return None

    def _resolve_fk_anchor_value(
        self,
        input: LabelingInput,
        fk_col: str,
        fk_value: Any,
    ) -> str | None:
        fk_target = input.table.schema.foreign_keys.get(fk_col)
        if not fk_target:
            return None
        target_table, target_col = self._parse_fk_target(fk_target)
        if not target_table or not target_col:
            return None

        lookup = self._load_fk_anchor_lookup(
            input=input,
            target_table=target_table,
            target_col=target_col,
        )
        if not lookup:
            return None
        if fk_value in lookup:
            return lookup[fk_value]
        text_key = str(fk_value)
        if text_key in lookup:
            return lookup[text_key]
        return None

    def _load_fk_anchor_lookup(
        self,
        input: LabelingInput,
        target_table: str,
        target_col: str,
    ) -> dict[Any, str]:
        if input.write_back_path is None:
            return {}
        table_dir = input.write_back_path.parent
        target_path = table_dir / f"{target_table}.json"
        if not target_path.exists():
            return {}

        anchor_cols = self._load_target_anchor_columns(input, target_table)
        cache_key = "|".join([str(target_path), target_col, ",".join(anchor_cols)])
        if cache_key in self._fk_anchor_lookup_cache:
            return self._fk_anchor_lookup_cache[cache_key]

        try:
            payload = json.loads(target_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            return {}

        lookup: dict[Any, str] = {}
        for target_row in rows:
            if not isinstance(target_row, dict):
                continue
            key = target_row.get(target_col)
            if key in (None, ""):
                continue
            values = [str(target_row.get(col, "")).strip() for col in anchor_cols]
            values = [v for v in values if v]
            if not values:
                continue
            anchor_text = " ".join(values)
            lookup[key] = anchor_text
            lookup[str(key)] = anchor_text
        self._fk_anchor_lookup_cache[cache_key] = lookup
        return lookup

    def _parse_fk_target(self, fk_target: str) -> tuple[str | None, str | None]:
        if "." not in fk_target:
            return None, None
        table_name, col_name = fk_target.split(".", 1)
        table_name = table_name.strip()
        col_name = col_name.strip()
        if not table_name or not col_name:
            return None, None
        return table_name, col_name

    def _load_target_anchor_columns(self, input: LabelingInput, target_table: str) -> list[str]:
        if input.write_back_path is None:
            return [target_table]

        config_path = input.write_back_path.parent.parent / "config.py"
        if not config_path.exists():
            return [target_table]

        spec = importlib.util.spec_from_file_location(
            f"data_construction_dataset_config_{config_path.stem}",
            str(config_path),
        )
        if spec is None or spec.loader is None:
            return [target_table]

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = getattr(module, "CONFIG", None)
        if not isinstance(config, dict):
            return [target_table]

        raw_anchor_cols = config.get("anchor_cols")
        if not isinstance(raw_anchor_cols, list) or not raw_anchor_cols:
            return [target_table]
        first = raw_anchor_cols[0]
        if not isinstance(first, dict):
            return [target_table]

        cols = first.get(target_table, [])
        if isinstance(cols, str):
            cols = [cols]
        if not isinstance(cols, list):
            return [target_table]
        normalized = [col for col in cols if isinstance(col, str) and col.strip()]
        return normalized or [target_table]

    def _format_primary_key(self, primary_key: list[str]) -> str:
        if not primary_key:
            return "None (fallback to row_index)"
        if len(primary_key) == 1:
            return primary_key[0]
        return f"[{', '.join(primary_key)}] (composite key)"

    def _format_attribute_descriptions(self, table: Table) -> str:
        """Render all columns and their descriptions for labeling prompts."""
        if not table.schema.columns:
            return "- (No schema columns provided)"

        lines: list[str] = []
        for column in table.schema.columns:
            col_name = str(column.name).strip()
            if not col_name:
                continue
            dtype = str(column.dtype).strip() if getattr(column, "dtype", None) else "unknown"
            description = (column.description or "").strip()
            if description:
                lines.append(f"- {col_name} [{dtype}]: {description}")
            else:
                lines.append(f"- {col_name} [{dtype}]: (no description)")
        return "\n".join(lines) if lines else "- (No schema columns provided)"

    def _rows_to_markdown(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "| |\n|---|\n| |"

        headers = list(rows[0].keys())
        header_line = "| " + " | ".join(headers) + " |"
        divider_line = "|" + "|".join(["---"] * len(headers)) + "|"
        body_lines: list[str] = []
        for row in rows:
            values = ["" if row.get(h) is None else str(row.get(h)) for h in headers]
            body_lines.append("| " + " | ".join(values) + " |")
        return "\n".join([header_line, divider_line, *body_lines])

    def _safe_parse_json(self, text: str) -> dict[str, Any] | None:
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

        if not isinstance(parsed, dict):
            return None
        return parsed

    def _write_assignments(
        self,
        path: Path,
        table: Table,
        assignment: dict[str, dict[str, list[str]]],
    ) -> None:
        """Write assignment result back to table JSON file."""
        write_table_with_assignments(path, table.rows, assignment)

    def _matrix_to_assignment(
        self,
        matrix: CapabilityMatrix,
        table: Table,
        input: LabelingInput | None = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Convert CapabilityMatrix back to assignment dict for persistence."""
        pk = table.schema.primary_key
        assignment: dict[str, dict[str, list[str]]] = {}
        for cell in matrix.cells:
            if not cell.labels:
                continue
            row = table.rows[cell.ref.row_index]
            row_key = self._build_row_key(
                row=row,
                primary_key=pk,
                row_index=cell.ref.row_index,
                input=input,
            )
            if row_key not in assignment:
                assignment[row_key] = {}
            assignment[row_key][cell.ref.column_name] = [
                label.name for label in cell.labels
            ]
        return assignment

    def _allowed_labels_for_table(self, table_type: str | None) -> frozenset[str] | None:
        """Return allowed capability keys for given table type, or None for all."""
        if (table_type or "").strip().lower() == "relation":
            return frozenset(set(PILLAR1_KEYS) | set(PILLAR2_KEYS))
        return PILLAR1_KEYS

    def _map_label_codes(
        self,
        label_codes: list[str],
        allowed: frozenset[str] | None = None,
    ) -> list[CapabilityLabel]:
        mapped: list[CapabilityLabel] = []
        for code in label_codes:
            normalized = code.strip().upper().replace("-", "_")
            if normalized in CapabilityLabel.__members__:
                label = CapabilityLabel[normalized]
                if allowed is None or label.name in allowed:
                    mapped.append(label)
        return mapped

    def _resolve_excluded_columns(self, input: LabelingInput) -> set[str]:
        if getattr(getattr(input.config, "base", None), "if_primary_allowed", False):
            return set()
        excluded = set(input.table.schema.primary_key)
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        if is_relation:
            return excluded
        excluded.update(self._load_anchor_columns(input))
        return excluded

    def _load_anchor_columns(self, input: LabelingInput) -> set[str]:
        if input.write_back_path is None:
            return set()

        config_path = input.write_back_path.parent.parent / "config.py"
        if not config_path.exists():
            return set()

        spec = importlib.util.spec_from_file_location(
            f"data_construction_dataset_config_{config_path.stem}",
            str(config_path),
        )
        if spec is None or spec.loader is None:
            return set()

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = getattr(module, "CONFIG", None)
        if not isinstance(config, dict):
            return set()

        raw_anchor_cols = config.get("anchor_cols")
        if not isinstance(raw_anchor_cols, list) or not raw_anchor_cols:
            return set()

        first = raw_anchor_cols[0]
        if not isinstance(first, dict):
            return set()

        cols = first.get(input.table.table_id)
        if cols is None:
            lower_first = {str(k).lower(): v for k, v in first.items() if isinstance(k, str)}
            cols = lower_first.get(input.table.table_id.lower(), [])
        if isinstance(cols, str):
            cols = [cols]
        if not isinstance(cols, list):
            return set()

        row_columns = set(input.table.rows[0].keys()) if input.table.rows else set()
        return {col for col in cols if isinstance(col, str) and col in row_columns}
