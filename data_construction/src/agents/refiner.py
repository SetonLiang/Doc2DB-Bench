from __future__ import annotations

import importlib.util
import json
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .base import BaseAgent
from ..capabilities import get_capability_definitions
from ..config import LLMRuntimeConfig
from ..llm import OpenAILLMClient
from ..llm.base import BaseLLMClient
from ..models import CapabilityLabel, EvidenceFragment, EvidencePool, RefinerInput


REFINER_SYSTEM_PROMPT = """You are the Refiner Agent in a Reverse Document Synthesis Pipeline. Your objective is to reverse-engineer structured database cells into specific writing instructions and "Atomic Evidence Fragments" based on assigned Capability Strategies. You guide downstream Writer Agents to incorporate data in ways that are natural yet highly challenging (requiring reasoning or conflict resolution) for information extraction systems."""

REFINER_USER_PROMPT = """
**Your Goal:**
Generate a specific decomposition rationale and a set of "Atomic Evidence Fragments" for a single table cell. This will guide a downstream writer to incorporate the data point into a narrative, strictly adhering to the assigned strategies.

**Input Context:**
* **Table Context:**
{markdown_table}
* **Target Entity (Primary/Anchor Key):** {primary_key}
* **Target Attribute (Column):** {attribute}
* **Target Attribute Description:** {attribute_description}
* **Ground Truth Value:** {value}
* **Assigned Strategies (Capability Labels):** {strategies}

**Your Task & Constraints:**
1. **Analyze Context:** Understand the specific `{value}` within the context of the `{primary_key}` and `{attribute}`.
2. **Apply Strategies:** Strictly follow the provided strategy definitions (`{strategies}`). 
3. **The Taboo Rule (CRITICAL & ABSOLUTE):** - IF the assigned strategy involves **Reasoning (e.g., Arithmetic/Logic)** or **Conflict Resolution (e.g., Temporal/Source)**, the exact `{value}` is strictly **TABOO**. 
   - You MUST NEVER explicitly state the exact `{value}` in your generated fragments. Instead, break it down into logical constituents (e.g., mathematical components, historical states) so the final value must be deduced or calculated.
4. **Atomicity:** Your generated evidence must be broken down into indivisible, simple natural language phrases. One fragment = One discrete fact. Do not use complex, multi-clause sentences.
5. **Factual Precision:** The original `{value}` must be 100% mathematically or logically inferable and fully recoverable from the text alone.
6. **Isolation:** Focus ONLY on the target `{value}`. Do not hallucinate or pull values from other cells in the table unless specifically required by the strategy.

**Strategy Definitions:**
{detailed_strategy_definitions}

**Output Format:**
Return ONLY a valid JSON object matching this schema. Replace `rX_cY` with the actual row and column indices if available, otherwise use `r1_c1`.

```json
{{
    "evidence_pool": [
        {{
            "frag_id": "cell_rX_cY_frag1",
            "content": "The first atomic natural language fragment."
        }},
        {{
            "frag_id": "cell_rX_cY_frag2",
            "content": "The second atomic natural language fragment (if needed)."
        }}
    ]
}}
"""

REFINER_RELATION_USER_PROMPT = """
**Your Goal:**
Generate atomic evidence fragments for a **relation-table cell** by executing a JOINT STRATEGY. You must simultaneously encode the structural topology (Row Strategies) and the attribute disguise (Attribute Strategies).

**Input Context:**
* **Table Context:**
{markdown_table}
* **Relation Edge (Linked Entities):** {primary_key}
* **Target Edge Attribute:** {attribute} = {value}
* **Target Attribute Description:** {attribute_description}
* **Row-Level Strategies (How to hide the connection):** {row_strategies}
* **Attribute-Level Strategies (How to hide the value):** {attribute_strategies}
* **Multi-FK / shared expanded keys:** If several FK columns expand to the same logical field but headers are disambiguated with parentheses (e.g. `district.A2(account)` vs `district.A2(client)`), each column is a different referencing-entity path; generate evidence fragments that match the correct path for the target cell and never conflate values across paths.

**Your Task & Constraints:**
1. **Joint Strategy Execution (CRITICAL):** You MUST simultaneously execute both relation-level (topology) and attribute-level (value) disguises.
   - Apply row_strategies to determine HOW the entities in `{primary_key}` are connected (e.g., introducing a middle-man entity for Transitive Inference).
   - Apply attribute_strategies to disguise the specific `{value}` of this connection. IF NO attribute strategy is assigned, you MUST state the exact target values directly and clearly.
2. **Implicit Edge & Relation Awareness:** Contextualize how the two linked entities are related. However, if a Pillar 2 Relation strategy (e.g., Transitive, Multi-hop) is present, NEVER explicitly state that Entity A and Entity B interact directly. The relationship edge must be mathematically or logically deduced.
3. **Atomicity:** One fragment = one discrete fact or one single logical step. Keep fragments simple and composable. Avoid complex, multi-clause sentences.
4. **Exact Inference & Recoverability:** Maintain the intended challenge signals in your fragment design, ensuring that the exact {value} and structural relationships remain 100% mathematically or logically inferable and fully recoverable from the text alone.
5. **Multi-FK Path Isolation:** Headers disambiguated by parentheses (e.g., `district.A2(account)` vs `district.A2(client)`) represent strictly distinct relational paths. You must align evidence exactly with its assigned path and NEVER cross-contaminate or conflate values between them.
6. **No Hallucination:** Ground your generation exclusively in the provided table context and strategy constraints. Do not invent external entities, rules, or unprovided data.

**Strategy Definitions:**
{detailed_strategy_definitions}

**Output Format:**
Return ONLY a valid JSON object matching this schema.

```json
{{
    "evidence_pool": [
        {{
            "frag_id": "cell_rX_cY_frag1",
            "content": "The first atomic natural language fragment."
        }}
    ]
}}
"""

REFINER_GROUP_USER_PROMPT = """
**Your Goal:**
Generate evidence fragments that JOINTLY encapsulate multiple relation records using an aggregation or grouping strategy (e.g., One-to-Many Allocation, Cross-Table Aggregation). You must simultaneously encode the structural topology (Row Strategies) and the attribute disguise (Attribute Strategies).

**Input Context:**
* **Table Context (Target Group Rows):**
{markdown_table}
* **Relation Anchor (The Parent Entity):** {anchor_key}
* **Target Edge Attribute:** {attribute} = {value}
* **Target Attribute Description:** {attribute_description}
* **Row-Level Strategies (Applies to the whole group topology):** {row_strategies}
* **Target Records & Strategy Mapping:** {target_records_json}
* **Multi-FK / shared expanded keys:** If several FK columns expand to the same logical field but headers are disambiguated with parentheses (e.g. `district.A2(account)` vs `district.A2(client)`), each column is a different referencing-entity path; generate evidence fragments that match the correct path for each target record and never conflate values across paths.

**Your Task & Constraints:**
1. **Joint Compression & Execution (CRITICAL):** You MUST simultaneously execute relation-level grouping and attribute-level value disguises.
   - Apply `{row_strategies}` to compress ALL the records from the provided table context into a unified narrative structure (e.g., using percentages, totals, or grouped logic). 
   - Apply `{attribute_strategies}` **ONLY** to the specific values that have an attribute strategy explicitly assigned in the `Target Records & Strategy Mapping`. IF NO attribute strategy is assigned, you MUST state the exact target values directly and clearly.
2. **Avoid Repetition:** Do not write separate, parallel sentences for each record in the group. Combine them logically!
3. **Implicit Edge & Relation Awareness:** Contextualize how the parent `{anchor_key}` relates to the grouped child entities. However, if a Pillar 2 Relation strategy is present, NEVER explicitly state direct interactions. The relationship edges must be deduced.
4. **Exact Inference & Recoverability:** Maintain the intended challenge signals in your fragment design, ensuring that the exact `{value}` and structural relationships for EVERY record in the group remain 100% mathematically or logically inferable and fully recoverable from the text alone.
5. **Multi-FK Path Isolation:** Headers disambiguated by parentheses (e.g., `district.A2(account)` vs `district.A2(client)`) represent strictly distinct relational paths. You must align evidence exactly with its assigned path and NEVER cross-contaminate or conflate values between them.
6. **No Hallucination:** Ground your generation exclusively in the provided table context and strategy constraints. Do not invent external entities, rules, or unprovided data.

**Strategy Definitions:**
{detailed_strategy_definitions}

**Output Format:**
Return ONLY a valid JSON object matching this schema:
```json
{{
    "evidence_pool": [
        {{
            "frag_id": "group_{anchor_key}_frag1",
            "content": "The unified compressed fragment covering multiple target rows."
        }}
    ]
}}
```
"""


REFINER_VERIFY_SYSTEM_SYSTEM = "You are an expert data-to-text instruction verifier. Your responsibility is to ensure that writing guidance correctly follows assigned strategies, maintains strict data integrity, establishes unambiguous entity relationships (especially for relational data), and prevents any hallucination or misuse of table data."

REFINER_VERIFY_USER_PROMPT = """
**Your Goal:**
Verify a writing guidance for a specific table cell. Determine if it correctly handles the data value, strictly follows the assigned strategies, maintains data integrity, and (if applicable) successfully establishes the relationship between connected entities.

**Input:**
* **Table Type:** {table_type}
* **Table:**
{markdown_table}
* **Entity/Relation Anchor:** {primary_key}
* **Attribute:** {attribute}
* **Value:** {value}
* **Assigned Strategies:** {strategies}
* **Guidance:**
{detailed_guidance}

**Strategy Definitions:**
{strategy_definitions}

**Your Task (Step-by-Step):**
1.  **Strategy Verification:** Check if the 'Guidance' follows the 'Assigned Strategies'. If it doesn't, identify the specific issue.
2.  **Value Verification:** Check whether the 'Value' can be expressed correctly according to the guidance. For guidance involving calculations, ensure the calculation result is mathematically exact.
3.  **Relational Extraction Verification (CRITICAL):** * If the `Table Type` is "Relation" (or if the `Entity/Relation Anchor` involves multiple entities/foreign keys), verify that the guidance explicitly instructs to link these entities together. 
    * **Test:** A downstream information extraction model MUST be able to unambiguously extract the complete relation tuple (e.g., Entity A -> Entity B -> Attribute=Value) based purely on the text generated from this guidance. If the guidance loses the connection between the entities, it fails.
4.  **Data Integrity Verification:**
    * Check if the guidance hallucinates/invents any data that looks like it belongs to the table.
    * Check if the guidance incorrectly references or uses values from *other* unrelated cells (cross-contamination). The guidance should focus ONLY on the target attribute/value and its specific anchor entities.
5.  **Final Assessment:** If the guidance meets all requirements, set `ok` to true and `errors` to an empty list. Otherwise, set `ok` to false and provide **specific, actionable modification suggestions** explaining how to fix the guidance.

**Output Format:**
Respond with ONLY a single, valid JSON object:
```json
{{
    "ok": true/false,
    "errors": [
        {{
            "description": "Error description (e.g., 'Fails to link Supplier to Project')",
            "suggestion": "How to fix it (e.g., 'Instruct the writer to explicitly mention both the supplier name and the project name when describing the value')"
        }}
    ]
}}
```
"""

REFINER_GROUP_VERIFY_USER_PROMPT = """
**Your Goal:**
Verify a writing guidance designed for a group of related table records. Determine if the guidance successfully handles the grouped data, strictly follows the assigned strategies, maintains data integrity, and unambiguously establishes the 1-to-N relationship between the anchor entity and ALL target values.

**Input:**
* **Table Type:** {table_type}
* **Table Context:**
{markdown_table}
* **Group Anchor (Entity/Relation):** {anchor_key}
* **Target Attribute:** {attribute}
* **Target Group Payload (MUST be fully recoverable):** {group_payload_json}
* **Assigned Strategies:** {strategies}
* **Guidance:**
{detailed_guidance}

**Strategy Definitions:**
{strategy_definitions}

**Your Task (Step-by-Step):**
1.  **Strategy Verification:** Check if the 'Guidance' follows the 'Assigned Strategies' (especially strategies related to grouping, aggregating, or listing multiple records). Identify any missing or misapplied strategies.
2.  **Full-Group Recoverability Verification (CRITICAL):** Check whether following this guidance will result in text that expresses *ALL* values present in the `Target Group Payload`. 
    * **Rule:** Partial recoverability is a failure. If the guidance misses any target record/value, or merges them in a way that the original exact values cannot be deterministically inferred, it fails.
3.  **Relational Extraction Verification:** * Verify that the guidance explicitly instructs to link the single `Group Anchor` to *every* value in the group. 
    * **Test:** A downstream information extraction model MUST be able to unambiguously extract the complete set of relation tuples (e.g., [Anchor -> Attribute=Value 1], [Anchor -> Attribute=Value 2], ...) based purely on the text generated from this guidance. 
4.  **Data Integrity Verification:**
    * Check if the guidance hallucinates/invents any values or entities not present in the `Target Group Payload`.
    * Check if the guidance incorrectly references values from unrelated cells (cross-contamination).
5.  **Final Assessment:** If the guidance meets all requirements, set `ok` to true and `errors` to an empty list. Otherwise, set `ok` to false and provide **specific, actionable modification suggestions**.

**Output Format:**
Respond with ONLY a single, valid JSON object:
```json
{{
    "ok": true/false,
    "errors": [
        {{
            "description": "Error description (e.g., 'Guidance only mentions the first project, missing the other two in the payload', or 'Fails to link the Supplier to the grouped items')",
            "suggestion": "How to fix it (e.g., 'Instruct the writer to list all three project names associated with this Supplier')"
        }}
    ]
}}
```
"""

class RefinerAgent(BaseAgent[RefinerInput, EvidencePool]):
    GROUP_STRATEGIES: set[CapabilityLabel] = {
        CapabilityLabel.RL_O2M,
        CapabilityLabel.IDR_ED,
        CapabilityLabel.RL_MI,
        CapabilityLabel.RL_MB,
    }

    def __init__(
        self,
        llm: BaseLLMClient | None = None,
        *,
        verification_llm: BaseLLMClient | None = None,
        llm_config: LLMRuntimeConfig | None = None,
    ) -> None:
        self.llm = llm
        self.last_evidence_pool: EvidencePool | None = None
        _v_cfg = llm_config or LLMRuntimeConfig()
        self.verification_llm: BaseLLMClient = (
            verification_llm
            if verification_llm is not None
            else OpenAILLMClient(
                config=_v_cfg,
                api_key=_v_cfg.validation_api_key,
            )
        )

    def run(self, input: RefinerInput) -> EvidencePool:
        """Step 2: decompose annotated cells into atomic evidence fragments with provenance IDs."""
        existing_pool: EvidencePool | None = None
        if input.write_back_path is not None:
            output_path = self._derive_evidence_output_path(input.write_back_path)
            if output_path.exists():
                print("RefinerAgent: 111")
                try:
                    existing_pool = EvidencePool.model_validate(
                        json.loads(output_path.read_text(encoding="utf-8"))
                    )
                except (OSError, json.JSONDecodeError, ValueError):
                    existing_pool = None
        print("RefinerAgent: 222")
        new_fragments: list[EvidenceFragment] = []
        existing_fragments: list[EvidenceFragment] = (
            list(existing_pool.fragments) if existing_pool is not None else []
        )
        llm_retries = max(1, input.config.runtime.max_validation_retries + 1)
        excluded_columns = self._resolve_excluded_columns(input)
        prompt_excluded_columns = self._resolve_prompt_excluded_columns(input)
        anchor_cols_entity_tables = self._load_anchor_cols_entity_table_names(input)
        markdown_table = self._rows_to_markdown(
            input.table.rows,
            foreign_keys=input.table.schema.foreign_keys,
            table_type=input.table.schema.table_type,
            excluded_columns=prompt_excluded_columns,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        cells = [
            cell
            for cell in input.capability_matrix.cells
            if cell.ref.column_name not in excluded_columns
        ]
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        if_primary_allowed = getattr(getattr(input.config, "base", None), "if_primary_allowed", False)

        if (
            is_relation
            and not if_primary_allowed
            and self._is_fk_only_relation_table(input)
        ):
            existing_cell_keys = (
                self._collect_evidence_cell_keys(existing_pool.fragments)
                if existing_pool is not None
                else set()
            )
            fk_only_fragments = self._process_fk_only_relation_rows(
                input=input,
                llm_retries=llm_retries,
                prompt_excluded_columns=prompt_excluded_columns,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
                existing_cell_keys=existing_cell_keys,
            )
            if existing_pool is not None and not fk_only_fragments:
                self.last_evidence_pool = existing_pool
                return existing_pool
            pool = EvidencePool(
                table_id=input.table.table_id,
                fragments=[*existing_fragments, *fk_only_fragments],
            )
            self.last_evidence_pool = pool
            self._write_evidence_json(input, pool)
            return pool

        if existing_pool is not None:
            existing_cell_keys = self._collect_evidence_cell_keys(existing_pool.fragments)
            missing_cells = [
                cell
                for cell in cells
                if (cell.ref.row_index, cell.ref.column_name) not in existing_cell_keys
            ]
            if not missing_cells:
                self.last_evidence_pool = existing_pool
                return existing_pool
            cells = missing_cells

        if not cells:
            pool = EvidencePool(
                table_id=input.table.table_id,
                fragments=existing_fragments,
            )
            self.last_evidence_pool = pool
            self._write_evidence_json(input, pool)
            return pool

        grouped_cells: dict[tuple[tuple[str, ...], str, str], list[Any]] = defaultdict(list)
        single_cells: list[Any] = []
        ic_nr_cells_by_row: dict[int, list[Any]] = defaultdict(list)

        for cell in cells:
            print(cell)
            row = input.table.rows[cell.ref.row_index]
            _, _, effective_labels = self._resolve_effective_labels_for_cell(
                input=input,
                cell=cell,
                row=row,
            )
            # if CapabilityLabel.IC_NR in effective_labels:
            #     # IC_NR should use row-level context and store one representative-column evidence per row.
            #     ic_nr_cells_by_row[cell.ref.row_index].append(cell)
            #     continue
            # Skip cells that have RL_O2M or IDR_ED type
            # if CapabilityLabel.RL_MB not in effective_labels:
                # continue
            group_strategy_labels = self._extract_group_strategy_labels(effective_labels)
            should_group = (
                is_relation
                and bool(group_strategy_labels)
            )
            print(f"should_group: {should_group}")
            # continue
            if should_group:
                if CapabilityLabel.RL_O2M in group_strategy_labels:
                    strategy_signature = (CapabilityLabel.RL_O2M.name,)
                elif CapabilityLabel.IDR_ED in group_strategy_labels:
                    strategy_signature = (CapabilityLabel.IDR_ED.name,)
                else:
                    strategy_signature = tuple(label.name for label in group_strategy_labels)
                column_name = cell.ref.column_name
                relation_anchor_key = self._build_relation_group_key(
                    row=row,
                    primary_key=input.table.schema.primary_key,
                    foreign_keys=input.table.schema.foreign_keys,
                    target_column=column_name,
                    rows=input.table.rows,
                    if_primary_allowed=if_primary_allowed,
                    row_index=cell.ref.row_index,
                    anchor_cols_entity_tables=anchor_cols_entity_tables,
                    group_strategy_labels=group_strategy_labels,
                )
                # Same strategy + same column + same relation anchor -> one group.
                grouped_cells[(strategy_signature, column_name, relation_anchor_key)].append(cell)
            else:
                single_cells.append(cell)


        group_items = list(grouped_cells.items())

        if group_items:
            def process_one(item: tuple) -> list[EvidenceFragment]:
                (_, _, relation_anchor_key), cells_in_group = item
                return self._process_grouped_cells(
                    input=input,
                    anchor_key=relation_anchor_key,
                    cells=cells_in_group,
                    markdown_table=markdown_table,
                    llm_retries=llm_retries,
                    excluded_columns=excluded_columns,
                    prompt_excluded_columns=prompt_excluded_columns,
                    anchor_cols_entity_tables=anchor_cols_entity_tables,
                )

            max_group_workers = self._resolve_max_workers(len(group_items))
            if max_group_workers == 1:
                for item in group_items:
                    new_fragments.extend(process_one(item))
            else:
                with ThreadPoolExecutor(max_workers=max_group_workers) as executor:
                    for frag_list in executor.map(process_one, group_items):
                        new_fragments.extend(frag_list)

        if ic_nr_cells_by_row:
            new_fragments.extend(
                self._process_ic_nr_rows(
                    input=input,
                    cells_by_row=ic_nr_cells_by_row,
                    llm_retries=llm_retries,
                    prompt_excluded_columns=prompt_excluded_columns,
                    anchor_cols_entity_tables=anchor_cols_entity_tables,
                )
            )
        # print(f"fragments: {fragments}")
        # print(f"single_cells: {single_cells}")
        # print(f"grouped_cells: {grouped_cells}")
        # exit()
        if single_cells:
            max_workers = self._resolve_max_workers(len(single_cells))
            if max_workers == 1:
                for cell in single_cells:
                    new_fragments.extend(
                        self._process_single_cell(
                            input=input,
                            cell=cell,
                            markdown_table=markdown_table,
                            llm_retries=llm_retries,
                            excluded_columns=excluded_columns,
                            prompt_excluded_columns=prompt_excluded_columns,
                            anchor_cols_entity_tables=anchor_cols_entity_tables,
                        )
                    )
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    # executor.map preserves input order, keeping deterministic output ordering.
                    results = executor.map(
                        lambda c: self._process_single_cell(
                            input=input,
                            cell=c,
                            markdown_table=markdown_table,
                            llm_retries=llm_retries,
                            excluded_columns=excluded_columns,
                            prompt_excluded_columns=prompt_excluded_columns,
                            anchor_cols_entity_tables=anchor_cols_entity_tables,
                        ),
                        single_cells,
                    )
                    for frag_list in results:
                        new_fragments.extend(frag_list)

        pool = EvidencePool(
            table_id=input.table.table_id,
            fragments=[*existing_fragments, *new_fragments],
        )
        self.last_evidence_pool = pool
        self._write_evidence_json(input, pool)
        return pool

    def _collect_evidence_cell_keys(
        self,
        fragments: list[EvidenceFragment],
    ) -> set[tuple[int, str]]:
        keys: set[tuple[int, str]] = set()
        for fragment in fragments:
            source_cell = fragment.source_cell
            keys.add((source_cell.row_index, source_cell.column_name))
        return keys

    def _process_single_cell(
        self,
        input: RefinerInput,
        cell: Any,
        markdown_table: str,
        llm_retries: int,
        excluded_columns: set[str] | None = None,
        prompt_excluded_columns: set[str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> list[EvidenceFragment]:
        if excluded_columns and cell.ref.column_name in excluded_columns:
            return []
        cell_fragments: list[EvidenceFragment] = []
        # Use only header + target row for prompt (not full table)
        prompt_cols = self._compute_prompt_included_columns(input, {cell.ref.column_name})
        markdown_table = self._rows_to_markdown(
            input.table.rows,
            foreign_keys=input.table.schema.foreign_keys,
            table_type=input.table.schema.table_type,
            row_indices={cell.ref.row_index},
            excluded_columns=prompt_excluded_columns,
            included_columns=prompt_cols,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        verify_markdown_table = self._rows_to_markdown(
            input.table.rows,
            foreign_keys=input.table.schema.foreign_keys,
            table_type=input.table.schema.table_type,
            row_indices={cell.ref.row_index},
            excluded_columns=prompt_excluded_columns,
            included_columns=None,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        row = input.table.rows[cell.ref.row_index]
        raw_value = row.get(cell.ref.column_name)
        column_index = self._get_column_index(input.table.rows, cell.ref.column_name)
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        if is_relation:
            pk_repr = self._format_relation_edge_value(
                row=row,
                foreign_keys=input.table.schema.foreign_keys,
                primary_key=input.table.schema.primary_key,
                row_index=cell.ref.row_index,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )
        else:
            anchor_columns = self._load_anchor_columns(input)
            pk_repr = self._format_entity_anchor_value(
                row=row,
                anchor_columns=anchor_columns,
                primary_key=input.table.schema.primary_key,
                row_index=cell.ref.row_index,
            )
        row_strategy_labels, attr_strategy_labels, effective_labels = (
            self._resolve_effective_labels_for_cell(input=input, cell=cell, row=row)
        )
        taboo = self._is_taboo(effective_labels)
        attribute_description = self._get_attribute_description(input, cell.ref.column_name)
        if_primary_allowed = getattr(getattr(input.config, "base", None), "if_primary_allowed", False)
        prompt_column_name = (
            self._resolve_relation_target_display_name(
                input.table.rows,
                cell.ref.column_name,
                if_primary_allowed,
                foreign_keys=input.table.schema.foreign_keys,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )
            if is_relation
            else cell.ref.column_name
        )
        texts = self._generate_evidence_texts(
            row_index=cell.ref.row_index,
            column_index=column_index,
            column_name=cell.ref.column_name,
            attribute_description=attribute_description,
            primary_key_repr=pk_repr,
            markdown_table=markdown_table,
            verify_markdown_table=verify_markdown_table,
            raw_value=raw_value,
            labels=effective_labels,
            context=row,
            taboo=taboo,
            table_type=input.table.schema.table_type,
            llm_retries=llm_retries,
            row_strategy_labels=row_strategy_labels,
            attribute_strategy_labels=attr_strategy_labels,
            prompt_column_name=prompt_column_name,
            suppress_target_attribute=False,
        )

        for i, text in enumerate(texts, start=1):
            fragment_id = self._build_fragment_id(
                row_index=cell.ref.row_index,
                column_index=column_index,
                frag_index=i,
            )
            source_cell = cell.ref.model_copy(update={"value": raw_value})
            cell_fragments.append(
                EvidenceFragment(
                    fragment_id=fragment_id,
                    table_id=input.table.table_id,
                    source_cell=source_cell,
                    source_labels=effective_labels,
                    text=text,
                    taboo_final_value=taboo,
                )
            )
        return cell_fragments

    def _process_fk_only_relation_rows(
        self,
        input: RefinerInput,
        llm_retries: int,
        prompt_excluded_columns: set[str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
        existing_cell_keys: set[tuple[int, str]] | None = None,
    ) -> list[EvidenceFragment]:
        fk_cols = set(input.table.schema.foreign_keys.keys())
        if not fk_cols:
            return []

        cells_by_row: dict[int, list[Any]] = defaultdict(list)
        for cell in input.capability_matrix.cells:
            if cell.ref.column_name in fk_cols:
                cells_by_row[cell.ref.row_index].append(cell)

        if not cells_by_row:
            return []

        fragments: list[EvidenceFragment] = []
        for row_index in sorted(cells_by_row.keys()):
            row_cells = cells_by_row[row_index]
            if existing_cell_keys:
                row_cells = [
                    c for c in row_cells
                    if (c.ref.row_index, c.ref.column_name) not in existing_cell_keys
                ]
            if not row_cells:
                continue

            row = input.table.rows[row_index]
            representative = row_cells[0]
            rep_column = representative.ref.column_name
            rep_column_index = self._get_column_index(input.table.rows, rep_column)

            prompt_cols = self._compute_prompt_included_columns(input, set())
            markdown_table = self._rows_to_markdown(
                input.table.rows,
                foreign_keys=input.table.schema.foreign_keys,
                table_type=input.table.schema.table_type,
                row_indices={row_index},
                excluded_columns=prompt_excluded_columns,
                included_columns=prompt_cols,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )
            verify_markdown_table = self._rows_to_markdown(
                input.table.rows,
                foreign_keys=input.table.schema.foreign_keys,
                table_type=input.table.schema.table_type,
                row_indices={row_index},
                excluded_columns=prompt_excluded_columns,
                included_columns=None,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )

            pk_repr = self._format_relation_edge_value(
                row=row,
                foreign_keys=input.table.schema.foreign_keys,
                primary_key=input.table.schema.primary_key,
                row_index=row_index,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )

            union_row_labels: list[CapabilityLabel] = []
            union_attr_labels: list[CapabilityLabel] = []
            union_effective_labels: list[CapabilityLabel] = []
            per_cell_effective: dict[tuple[int, str], list[CapabilityLabel]] = {}
            for c in row_cells:
                row_labels, attr_labels, effective_labels = self._resolve_effective_labels_for_cell(
                    input=input,
                    cell=c,
                    row=row,
                )
                union_row_labels = self._merge_label_lists(union_row_labels, row_labels)
                union_attr_labels = self._merge_label_lists(union_attr_labels, attr_labels)
                union_effective_labels = self._merge_label_lists(union_effective_labels, effective_labels)
                per_cell_effective[(c.ref.row_index, c.ref.column_name)] = effective_labels

            taboo = self._is_taboo(union_effective_labels)
            texts = self._generate_evidence_texts(
                row_index=row_index,
                column_index=rep_column_index,
                column_name=rep_column,
                attribute_description="",
                primary_key_repr=pk_repr,
                markdown_table=markdown_table,
                verify_markdown_table=verify_markdown_table,
                raw_value="",
                labels=union_effective_labels,
                context=row,
                taboo=taboo,
                table_type=input.table.schema.table_type,
                llm_retries=llm_retries,
                row_strategy_labels=union_row_labels,
                attribute_strategy_labels=union_attr_labels,
                prompt_column_name="",
                suppress_target_attribute=True,
            )

            for c in row_cells:
                raw_value = row.get(c.ref.column_name)
                column_index = self._get_column_index(input.table.rows, c.ref.column_name)
                effective_labels = per_cell_effective.get((c.ref.row_index, c.ref.column_name), c.labels)
                cell_taboo = self._is_taboo(effective_labels)
                source_cell = c.ref.model_copy(update={"value": raw_value})
                for i, text in enumerate(texts, start=1):
                    fragments.append(
                        EvidenceFragment(
                            fragment_id=self._build_fragment_id(
                                row_index=c.ref.row_index,
                                column_index=column_index,
                                frag_index=i,
                            ),
                            table_id=input.table.table_id,
                            source_cell=source_cell,
                            source_labels=effective_labels,
                            text=text,
                            taboo_final_value=cell_taboo,
                        )
                    )
        return fragments

    def _process_ic_nr_rows(
        self,
        input: RefinerInput,
        cells_by_row: dict[int, list[Any]],
        llm_retries: int,
        prompt_excluded_columns: set[str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> list[EvidenceFragment]:
        fragments: list[EvidenceFragment] = []
        if not cells_by_row:
            return fragments

        for row_index in sorted(cells_by_row.keys()):
            row_cells = cells_by_row[row_index]
            if not row_cells:
                continue

            row = input.table.rows[row_index]
            representative = min(
                row_cells,
                key=lambda c: self._get_column_index(input.table.rows, c.ref.column_name),
            )
            rep_column = representative.ref.column_name
            rep_column_index = self._get_column_index(input.table.rows, rep_column)

            # For IC_NR, context must be whole-row (header + row), not one cell.
            prompt_cols = self._compute_prompt_included_columns(input, set())
            markdown_table = self._rows_to_markdown(
                input.table.rows,
                foreign_keys=input.table.schema.foreign_keys,
                table_type=input.table.schema.table_type,
                row_indices={row_index},
                excluded_columns=prompt_excluded_columns,
                included_columns=prompt_cols,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )
            verify_markdown_table = self._rows_to_markdown(
                input.table.rows,
                foreign_keys=input.table.schema.foreign_keys,
                table_type=input.table.schema.table_type,
                row_indices={row_index},
                excluded_columns=prompt_excluded_columns,
                included_columns=None,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )

            is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
            if is_relation:
                pk_repr = self._format_relation_edge_value(
                    row=row,
                    foreign_keys=input.table.schema.foreign_keys,
                    primary_key=input.table.schema.primary_key,
                    row_index=row_index,
                    anchor_cols_entity_tables=anchor_cols_entity_tables,
                )
            else:
                anchor_columns = self._load_anchor_columns(input)
                pk_repr = self._format_entity_anchor_value(
                    row=row,
                    anchor_columns=anchor_columns,
                    primary_key=input.table.schema.primary_key,
                    row_index=row_index,
                )

            union_row_labels: list[CapabilityLabel] = []
            union_attr_labels: list[CapabilityLabel] = []
            union_effective_labels: list[CapabilityLabel] = []
            per_cell_effective: dict[tuple[int, str], list[CapabilityLabel]] = {}
            for c in row_cells:
                row_labels, attr_labels, effective_labels = self._resolve_effective_labels_for_cell(
                    input=input,
                    cell=c,
                    row=row,
                )
                union_row_labels = self._merge_label_lists(union_row_labels, row_labels)
                union_attr_labels = self._merge_label_lists(union_attr_labels, attr_labels)
                union_effective_labels = self._merge_label_lists(union_effective_labels, effective_labels)
                per_cell_effective[(c.ref.row_index, c.ref.column_name)] = effective_labels

            if CapabilityLabel.IC_NR not in union_effective_labels:
                continue

            taboo = self._is_taboo(union_effective_labels)
            texts = self._generate_evidence_texts(
                row_index=row_index,
                column_index=rep_column_index,
                column_name=rep_column,
                attribute_description="",
                primary_key_repr=pk_repr,
                markdown_table=markdown_table,
                verify_markdown_table=verify_markdown_table,
                raw_value="",
                labels=union_effective_labels,
                context=row,
                taboo=taboo,
                table_type=input.table.schema.table_type,
                llm_retries=llm_retries,
                row_strategy_labels=union_row_labels,
                attribute_strategy_labels=union_attr_labels,
                prompt_column_name="",
                suppress_target_attribute=True,
            )

            rep_raw_value = row.get(representative.ref.column_name)
            rep_effective_labels = per_cell_effective.get(
                (representative.ref.row_index, representative.ref.column_name),
                representative.labels,
            )
            rep_taboo = self._is_taboo(rep_effective_labels)
            rep_source_cell = representative.ref.model_copy(update={"value": rep_raw_value})
            for i, text in enumerate(texts, start=1):
                fragments.append(
                    EvidenceFragment(
                        fragment_id=self._build_fragment_id(
                            row_index=representative.ref.row_index,
                            column_index=rep_column_index,
                            frag_index=i,
                        ),
                        table_id=input.table.table_id,
                        source_cell=rep_source_cell,
                        source_labels=rep_effective_labels,
                        text=text,
                        taboo_final_value=rep_taboo,
                    )
                )
        return fragments

    def _resolve_effective_labels_for_cell(
        self,
        input: RefinerInput,
        cell: Any,
        row: dict[str, Any],
    ) -> tuple[list[CapabilityLabel], list[CapabilityLabel], list[CapabilityLabel]]:
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        row_strategy_labels: list[CapabilityLabel] = []
        attr_strategy_labels: list[CapabilityLabel] = []
        if is_relation:
            row_strategy_labels, attr_strategy_labels = self._resolve_relation_strategies(
                input=input,
                row=row,
                row_index=cell.ref.row_index,
                column_name=cell.ref.column_name,
            )
        effective_labels = self._merge_label_lists(cell.labels, row_strategy_labels, attr_strategy_labels)
        return row_strategy_labels, attr_strategy_labels, effective_labels

    def _resolve_relation_target_display_name(
        self,
        rows: list[dict[str, Any]],
        target_column: str,
        if_primary_allowed: bool,
        foreign_keys: dict[str, str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> str:
        """When PK/FK columns participate in prompts, align |target= and attribute lines with expanded FK headers."""
        if not if_primary_allowed:
            return target_column
        expanded = self._infer_expanded_anchor_headers(rows, target_column)
        if foreign_keys:
            expanded = self._filter_expanded_headers_for_anchor_policy(
                expanded,
                target_column,
                foreign_keys,
                anchor_cols_entity_tables,
            )
        if expanded:
            return expanded[0]
        return target_column

    def _build_relation_group_key(
        self,
        row: dict[str, Any],
        primary_key: list[str],
        foreign_keys: dict[str, str],
        target_column: str,
        rows: list[dict[str, Any]] | None = None,
        if_primary_allowed: bool = False,
        row_index: int = 0,
        anchor_cols_entity_tables: set[str] | None = None,
        group_strategy_labels: list | None = None,
    ) -> str:
        lookup_rows = rows if rows is not None else [row]
        target_suffix = self._resolve_relation_target_display_name(
            lookup_rows,
            target_column,
            if_primary_allowed,
            foreign_keys=foreign_keys,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        # Match grouped payload primary_key: include every FK edge (e.g. EMPLOYEE + DEPARTMENT),
        # not only the first FK column.
        # edge_repr = self._format_relation_edge_value(
        #     row=row,
        #     foreign_keys=foreign_keys,
        #     primary_key=primary_key,
        #     row_index=row_index,
        #     exclude_column=target_column,
        #     anchor_cols_entity_tables=anchor_cols_entity_tables,
        # )
        # return f"{edge_repr}|target={target_suffix}"

        # 提取 candidates（列名），供下方所有策略分支共用
        candidates: list[str] = []
        for fk_col in foreign_keys.keys():
            if fk_col != target_column and fk_col not in candidates:
                candidates.append(fk_col)
        if not candidates:
            for pk_col in primary_key:
                if pk_col != target_column and pk_col not in candidates:
                    candidates.append(pk_col)
        if not candidates:
            for col in row.keys():
                if col != target_column:
                    candidates.append(col)
                    break

        # RL_MB / RL_MI: 必须 group 在一起！
        # 因此这里【只使用列名】作为 anchor_str，而不去 fetch row.get() 具体的 value。
        # 这样同一张表的所有行，其 candidates 相同，生成的 key 就完全一致（例如 "Episode.title, Person.name|target=..."），从而成功 Group。
        if group_strategy_labels:
            has_rl_o2m = CapabilityLabel.RL_O2M in group_strategy_labels or CapabilityLabel.IDR_ED in group_strategy_labels
            has_group_only = any(
                lbl in (CapabilityLabel.RL_MB, CapabilityLabel.RL_MI)
                for lbl in group_strategy_labels
            )
            if has_group_only and not has_rl_o2m:
                anchor_cols_to_use = candidates if candidates else [target_column]
                anchor_str = ", ".join(anchor_cols_to_use)
                return f"{anchor_str}|target={target_suffix}"

        # RL_O2M/IDR_ED: 正常的 anchor 逻辑，取单行的具体值 (row.get) 来切分 group
        anchor_col = candidates[0] if candidates else target_column
        anchor_val = row.get(anchor_col)

        if isinstance(anchor_val, str):
            normalized_val = anchor_val.strip()
            if normalized_val and "=" in normalized_val:
                return f"{normalized_val}|target={target_suffix}"
            return f"{anchor_col}={normalized_val}|target={target_suffix}"
        return f"{anchor_col}={anchor_val}|target={target_suffix}"

    def _process_grouped_cells(
        self,
        input: RefinerInput,
        anchor_key: str,
        cells: list[Any],
        markdown_table: str,
        llm_retries: int,
        excluded_columns: set[str] | None = None,
        prompt_excluded_columns: set[str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> list[EvidenceFragment]:
        if excluded_columns:
            cells = [cell for cell in cells if cell.ref.column_name not in excluded_columns]
        if not cells:
            return []

        group_payload: list[dict[str, Any]] = []
        union_labels: list[CapabilityLabel] = []
        union_row_labels: list[CapabilityLabel] = []
        union_attr_labels: list[CapabilityLabel] = []
        cell_effective_labels: dict[tuple[int, str], list[CapabilityLabel]] = {}
        group_raw_values: list[Any] = []
        primary_key = input.table.schema.primary_key

        for cell in cells:
            row = input.table.rows[cell.ref.row_index]
            raw_value = row.get(cell.ref.column_name)
            column_index = self._get_column_index(input.table.rows, cell.ref.column_name)
            row_strategy_labels, attr_strategy_labels, effective_labels = self._resolve_effective_labels_for_cell(input=input, cell=cell, row=row)
            union_labels = self._merge_label_lists(union_labels, effective_labels)
            union_row_labels = self._merge_label_lists(union_row_labels, row_strategy_labels)
            union_attr_labels = self._merge_label_lists(union_attr_labels, attr_strategy_labels)
            cell_effective_labels[(cell.ref.row_index, cell.ref.column_name)] = effective_labels
            group_raw_values.append(raw_value)
            group_payload.append(
                {
                    "row_index": cell.ref.row_index,
                    "column_index": column_index,
                    "column_name": cell.ref.column_name,
                    "primary_key": self._format_relation_edge_value(
                        row=row,
                        foreign_keys=input.table.schema.foreign_keys,
                        primary_key=primary_key,
                        row_index=cell.ref.row_index,
                        anchor_cols_entity_tables=anchor_cols_entity_tables,
                    ),
                    "value": raw_value,
                    "labels": [label.name for label in effective_labels],
                }
            )

        # Use only header + target rows for prompt (not full table)
        target_row_indices = {p["row_index"] for p in group_payload}
        prompt_targets = {c.ref.column_name for c in cells}
        prompt_cols = self._compute_prompt_included_columns(input, prompt_targets)
        markdown_table = self._rows_to_markdown(
            input.table.rows,
            foreign_keys=input.table.schema.foreign_keys,
            table_type=input.table.schema.table_type,
            row_indices=target_row_indices,
            excluded_columns=prompt_excluded_columns,
            included_columns=prompt_cols,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        verify_markdown_table = self._rows_to_markdown(
            input.table.rows,
            foreign_keys=input.table.schema.foreign_keys,
            table_type=input.table.schema.table_type,
            row_indices=target_row_indices,
            excluded_columns=prompt_excluded_columns,
            included_columns=None,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )

        group_taboo = self._is_taboo(union_labels)
        attribute_name = group_payload[0]["column_name"] if group_payload else ""
        attribute_description = self._get_attribute_description(input, attribute_name)
        texts = self._generate_group_evidence_texts(
            anchor_key=anchor_key,
            markdown_table=markdown_table,
            verify_markdown_table=verify_markdown_table,
            group_payload=group_payload,
            attribute_description=attribute_description,
            labels=union_labels,
            row_strategy_labels=union_row_labels,
            attribute_strategy_labels=union_attr_labels,
            table_type=input.table.schema.table_type,
            llm_retries=llm_retries,
            taboo=group_taboo,
            raw_values=group_raw_values,
            rows=input.table.rows,
            if_primary_allowed=getattr(getattr(input.config, "base", None), "if_primary_allowed", False),
            foreign_keys=input.table.schema.foreign_keys,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        if not texts:
            return []

        fragments: list[EvidenceFragment] = []
        for cell in cells:
            row = input.table.rows[cell.ref.row_index]
            raw_value = row.get(cell.ref.column_name)
            column_index = self._get_column_index(input.table.rows, cell.ref.column_name)
            effective_labels = cell_effective_labels.get((cell.ref.row_index, cell.ref.column_name), cell.labels)
            taboo = self._is_taboo(effective_labels)
            source_cell = cell.ref.model_copy(update={"value": raw_value})
            for i, text in enumerate(texts, start=1):
                fragments.append(
                    EvidenceFragment(
                        fragment_id=self._build_fragment_id(
                            row_index=cell.ref.row_index,
                            column_index=column_index,
                            frag_index=i,
                        ),
                        table_id=input.table.table_id,
                        source_cell=source_cell,
                        source_labels=effective_labels,
                        text=text,
                        taboo_final_value=taboo,
                    )
                )
        return fragments

    def _generate_group_evidence_texts(
        self,
        anchor_key: str,
        markdown_table: str,
        verify_markdown_table: str | None,
        group_payload: list[dict[str, Any]],
        attribute_description: str,
        labels: list[CapabilityLabel],
        row_strategy_labels: list[CapabilityLabel],
        attribute_strategy_labels: list[CapabilityLabel],
        table_type: str | None,
        llm_retries: int,
        taboo: bool,
        raw_values: list[Any],
        rows: list[dict[str, Any]],
        if_primary_allowed: bool,
        foreign_keys: dict[str, str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> list[str]:
        llm_texts: list[str] | None = None
        if self.llm is not None:
            llm_texts = self._generate_group_with_llm(
                anchor_key=anchor_key,
                markdown_table=markdown_table,
                verify_markdown_table=verify_markdown_table,
                group_payload=group_payload,
                attribute_description=attribute_description,
                labels=labels,
                row_strategy_labels=row_strategy_labels,
                attribute_strategy_labels=attribute_strategy_labels,
                table_type=table_type,
                llm_retries=llm_retries,
                rows=rows,
                if_primary_allowed=if_primary_allowed,
                foreign_keys=foreign_keys,
                anchor_cols_entity_tables=anchor_cols_entity_tables,
            )
        print("------")
        print(llm_texts)
        print("------")

        if llm_texts:
            cleaned = self._enforce_taboo_values(llm_texts, raw_values, taboo)
            if cleaned:
                return cleaned

        fallback = self._heuristic_group_texts(anchor_key=anchor_key, group_payload=group_payload, taboo=taboo)
        fallback_cleaned = self._enforce_taboo_values(fallback, raw_values, taboo)

        # If fallback degenerates to heuristic grouped templates (e.g. "...links to grouped values: ..."),
        # force additional LLM regenerations using LLMRuntimeConfig.max_retries from the client.
        if (
            self.llm is not None
            and self._is_heuristic_group_fallback_text(anchor_key=anchor_key, texts=fallback_cleaned)
        ):
            extra_retries = self._resolve_llm_retry_limit()
            for _ in range(extra_retries):
                regenerated = self._generate_group_with_llm(
                    anchor_key=anchor_key,
                    markdown_table=markdown_table,
                    verify_markdown_table=verify_markdown_table,
                    group_payload=group_payload,
                    attribute_description=attribute_description,
                    labels=labels,
                    row_strategy_labels=row_strategy_labels,
                    attribute_strategy_labels=attribute_strategy_labels,
                    table_type=table_type,
                    llm_retries=1,
                    rows=rows,
                    if_primary_allowed=if_primary_allowed,
                    foreign_keys=foreign_keys,
                    anchor_cols_entity_tables=anchor_cols_entity_tables,
                )
                if not regenerated:
                    continue
                regenerated_cleaned = self._enforce_taboo_values(regenerated, raw_values, taboo)
                if regenerated_cleaned and not self._is_heuristic_group_fallback_text(
                    anchor_key=anchor_key,
                    texts=regenerated_cleaned,
                ):
                    return regenerated_cleaned
        return fallback_cleaned

    def _generate_group_with_llm(
        self,
        anchor_key: str,
        markdown_table: str,
        verify_markdown_table: str | None,
        group_payload: list[dict[str, Any]],
        attribute_description: str,
        labels: list[CapabilityLabel],
        row_strategy_labels: list[CapabilityLabel],
        attribute_strategy_labels: list[CapabilityLabel],
        table_type: str | None,
        llm_retries: int,
        rows: list[dict[str, Any]],
        if_primary_allowed: bool,
        foreign_keys: dict[str, str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> list[str] | None:
        strategy_labels = [label.name for label in labels]
        detailed_defs = self._build_strategy_definitions(strategy_labels, table_type)
        attribute_raw = group_payload[0]["column_name"] if group_payload else ""
        attribute = self._resolve_relation_target_display_name(
            rows,
            attribute_raw,
            if_primary_allowed,
            foreign_keys=foreign_keys,
            anchor_cols_entity_tables=anchor_cols_entity_tables,
        )
        value_repr = json.dumps([p["value"] for p in group_payload], ensure_ascii=False)
        target_records_json = json.dumps(group_payload, ensure_ascii=False, indent=2)

        if labels and any(lbl.name in ("RL_MB", "RL_MI") for lbl in labels):
            # Extract unique primary keys from the grouped payload
            unique_entities = list(dict.fromkeys(p["primary_key"] for p in group_payload))
            display_anchor = f"{', '.join(unique_entities)} | target={attribute}"
        else:
            display_anchor = anchor_key
            
        prompt = REFINER_GROUP_USER_PROMPT.format(
            markdown_table=markdown_table,
            anchor_key=display_anchor,
            attribute=attribute,
            value=value_repr,
            attribute_description=attribute_description,
            row_strategies=", ".join(label.name for label in row_strategy_labels) if row_strategy_labels else "[]",
            attribute_strategies=", ".join(label.name for label in attribute_strategy_labels) if attribute_strategy_labels else "[]",
            target_records_json=target_records_json,
            detailed_strategy_definitions=detailed_defs,
        )
                    
        print(prompt)
        for _ in range(llm_retries):
            raw = self.llm.generate(
                prompt,
                system_prompt=REFINER_SYSTEM_PROMPT,
                task="evidence",
                temperature=1.0,
            )
            parsed = self._safe_parse_json(raw)
            texts = self._extract_texts_from_payload(parsed, row_index=0, column_index=0)
            return texts
            if texts:
                ok, errors = self._verify_group_evidence_recoverability(
                    anchor_key=anchor_key,
                    group_payload=group_payload,
                    texts=texts,
                    table_type=table_type,
                    markdown_table=verify_markdown_table or markdown_table,
                    attribute=attribute,
                    attribute_description=attribute_description,
                    strategies=", ".join(strategy_labels) if strategy_labels else "[]",
                    strategy_definitions=detailed_defs,
                )
                if ok:
                    return texts
                verification_feedback = "\n".join(f"- {err}" for err in errors) if errors else "- unknown reason"
                prompt = (
                    prompt
                    + "\n\nEvidence verification failed. Please revise evidence_pool so all target values "
                    + "(for each record in the group) are recoverable from the fragments.\n"
                    + f"Verifier feedback:\n{verification_feedback}"
                    + f"\n\nASSISTANT_PREVIOUS_OUTPUT:\n{raw}"
                )
                continue
            prompt = (
                prompt
                + "\n\nPrevious output was invalid or empty. "
                + "Please return valid JSON with non-empty evidence_pool content only."
                + f"\n\nASSISTANT_PREVIOUS_OUTPUT:\n{raw}"
            )
        return None

    def _heuristic_group_texts(
        self,
        anchor_key: str,
        group_payload: list[dict[str, Any]],
        taboo: bool,
    ) -> list[str]:
        if not group_payload:
            return []
        value_tokens = [str(item.get("value")) for item in group_payload if item.get("value") not in (None, "")]
        if taboo:
            return [
                f"{anchor_key} is connected to multiple related records in this grouped set.",
                "The exact target values are recoverable by combining the grouped row facts.",
            ]
        if value_tokens:
            joined = ", ".join(value_tokens)
            return [f"{anchor_key} links to grouped values: {joined}."]
        return [f"{anchor_key} has grouped relation evidence across multiple rows."]

    def _enforce_taboo_values(
        self,
        texts: list[str],
        raw_values: list[Any],
        taboo: bool,
    ) -> list[str]:
        if not taboo:
            return texts
        needles = {str(value).strip() for value in raw_values if value not in (None, "") and str(value).strip()}
        if not needles:
            return texts
        filtered = [text for text in texts if all(needle not in text for needle in needles)]
        if not filtered and texts:
            return texts
        return filtered

    def _resolve_max_workers(self, total_cells: int) -> int:
        if total_cells <= 1:
            return 1
        # IO-bound LLM calls benefit from moderate threading; cap to avoid flooding provider.
        cpu = os.cpu_count() or 4
        return max(1, min(16, total_cells, cpu * 2))

    def _resolve_llm_retry_limit(self) -> int:
        if self.llm is None:
            return 0
        value = getattr(self.llm, "_max_retries", 0)
        try:
            retries = int(value)
        except (TypeError, ValueError):
            retries = 0
        return max(0, retries)

    def _is_heuristic_group_fallback_text(self, anchor_key: str, texts: list[str]) -> bool:
        if not texts:
            return False
        normalized = [t.strip() for t in texts if isinstance(t, str) and t.strip()]
        if not normalized:
            return False
        for text in normalized:
            if text.startswith(f"{anchor_key} links to grouped values:"):
                return True
            if text == f"{anchor_key} has grouped relation evidence across multiple rows.":
                return True
            if text == f"{anchor_key} is connected to multiple related records in this grouped set.":
                return True
        return False

    def _generate_evidence_texts(
        self,
        row_index: int,
        column_index: int,
        column_name: str,
        attribute_description: str,
        primary_key_repr: str,
        markdown_table: str,
        verify_markdown_table: str | None,
        raw_value: Any,
        labels: list[CapabilityLabel],
        context: dict[str, Any],
        taboo: bool,
        table_type: str | None,
        llm_retries: int,
        row_strategy_labels: list[CapabilityLabel] | None = None,
        attribute_strategy_labels: list[CapabilityLabel] | None = None,
        prompt_column_name: str | None = None,
        suppress_target_attribute: bool = False,
    ) -> list[str]:
        attr_for_prompt = prompt_column_name if prompt_column_name is not None else column_name
        if self.llm is not None:
            llm_texts = self._generate_with_llm(
                row_index=row_index,
                column_index=column_index,
                column_name=column_name,
                attribute_description=attribute_description,
                primary_key_repr=primary_key_repr,
                markdown_table=markdown_table,
                verify_markdown_table=verify_markdown_table,
                raw_value=raw_value,
                labels=labels,
                table_type=table_type,
                llm_retries=llm_retries,
                row_strategy_labels=row_strategy_labels or [],
                attribute_strategy_labels=attribute_strategy_labels or [],
                attribute_for_prompt=attr_for_prompt,
                suppress_target_attribute=suppress_target_attribute,
            )
            if llm_texts:
                cleaned = self._enforce_taboo(llm_texts, raw_value, taboo)
                if cleaned:
                    return cleaned

        fallback = self._heuristic_texts(
            column_name=column_name,
            raw_value=raw_value,
            labels=labels,
            context=context,
            taboo=taboo,
            display_column_name=attr_for_prompt,
        )
        return self._enforce_taboo(fallback, raw_value, taboo) or [
            f"{attr_for_prompt} is derived from related facts."
        ]

    def _generate_with_llm(
        self,
        row_index: int,
        column_index: int,
        column_name: str,
        attribute_description: str,
        primary_key_repr: str,
        markdown_table: str,
        verify_markdown_table: str | None,
        raw_value: Any,
        labels: list[CapabilityLabel],
        table_type: str | None,
        llm_retries: int,
        row_strategy_labels: list[CapabilityLabel],
        attribute_strategy_labels: list[CapabilityLabel],
        attribute_for_prompt: str | None = None,
        suppress_target_attribute: bool = False,
    ) -> list[str] | None:
        strategy_labels = [label.name for label in labels]
        detailed_defs = self._build_strategy_definitions(strategy_labels, table_type)
        is_relation = (table_type or "").strip().lower() == "relation"
        attr_prompt = attribute_for_prompt if attribute_for_prompt is not None else column_name
        if is_relation:
            if suppress_target_attribute:
                prompt_attribute = ""
                prompt_value = ""
                prompt_attribute_description = ""
            else:
                prompt_attribute = attr_prompt
                prompt_value = raw_value
                prompt_attribute_description = attribute_description
            prompt = REFINER_RELATION_USER_PROMPT.format(
                markdown_table=markdown_table,
                primary_key=primary_key_repr,
                attribute=prompt_attribute,
                value=prompt_value,
                attribute_description=prompt_attribute_description,
                row_strategies=", ".join(label.name for label in row_strategy_labels) if row_strategy_labels else "[]",
                attribute_strategies=", ".join(label.name for label in attribute_strategy_labels) if attribute_strategy_labels else "[]",
                strategies=", ".join(strategy_labels) if strategy_labels else "[]",
                detailed_strategy_definitions=detailed_defs,
            )
        else:
            prompt = REFINER_USER_PROMPT.format(
                markdown_table=markdown_table,
                primary_key=primary_key_repr,
                attribute=attr_prompt,
                value=raw_value,
                attribute_description=attribute_description,
                strategies=", ".join(strategy_labels) if strategy_labels else "[]",
                detailed_strategy_definitions=detailed_defs,
            )
        messages: list[tuple[str, str]] = []

        print(prompt)
        # exit()
        for _ in range(llm_retries):
            raw = self.llm.generate(
                prompt,
                system_prompt=REFINER_SYSTEM_PROMPT,
                task="evidence",
                temperature=1.0,
            )
            parsed = self._safe_parse_json(raw)
            texts = self._extract_texts_from_payload(parsed, row_index=row_index, column_index=column_index)
            print(texts)
            return texts
            if texts:
                ok, errors = self._verify_evidence_recoverability(
                    row_index=row_index,
                    column_index=column_index,
                    column_name=column_name,
                    attribute_description=attribute_description,
                    raw_value=raw_value,
                    texts=texts,
                    table_type=table_type,
                    markdown_table=verify_markdown_table or markdown_table,
                    primary_key=primary_key_repr,
                    strategies=", ".join(strategy_labels) if strategy_labels else "[]",
                    strategy_definitions=detailed_defs,
                )
                print(f"ok: {ok}, errors: {errors}")
                if ok:
                    return texts
                verification_feedback = "\n".join(f"- {err}" for err in errors) if errors else "- unknown reason"
                prompt = (
                    prompt
                    + "\n\nEvidence verification failed. Please revise evidence_pool so the exact target value "
                    + "is recoverable from the fragments.\n"
                    + f"Verifier feedback:\n{verification_feedback}"
                )
                messages.append(("assistant", raw))
                for role, content in messages[-1:]:
                    prompt += f"\n\n{role.upper()}_PREVIOUS_OUTPUT:\n{content}"
                continue

            messages.append(("assistant", raw))
            prompt = (
                prompt
                + "\n\nPrevious output was invalid or empty. "
                + "Please return valid JSON with non-empty evidence_pool content only."
            )
            for role, content in messages[-1:]:
                prompt += f"\n\n{role.upper()}_PREVIOUS_OUTPUT:\n{content}"
        return None

    def _verify_evidence_recoverability(
        self,
        row_index: int,
        column_index: int,
        column_name: str,
        attribute_description: str,
        raw_value: Any,
        texts: list[str],
        table_type: str | None,
        markdown_table: str,
        primary_key: str,
        strategies: str,
        strategy_definitions: str,
    ) -> tuple[bool, list[str]]:
        evidence_payload = [
            {
                "frag_id": self._build_fragment_id(
                    row_index=row_index,
                    column_index=column_index,
                    frag_index=idx,
                ),
                "content": text,
            }
            for idx, text in enumerate(texts, start=1)
        ]
        attribute_display = f"{column_name} (Description: {attribute_description})" if attribute_description else column_name
        verify_prompt = REFINER_VERIFY_USER_PROMPT.format(
            table_type=table_type,
            markdown_table=markdown_table,
            primary_key=primary_key,
            attribute=attribute_display,
            value=raw_value,
            strategies=strategies,
            detailed_guidance=json.dumps(evidence_payload, ensure_ascii=False, indent=2),
            strategy_definitions=strategy_definitions,
        )
        print("------")
        print(verify_prompt)
        print("------")
        # exit()
        raw = self.verification_llm.generate(
            verify_prompt,
            system_prompt=REFINER_VERIFY_SYSTEM_SYSTEM,
            task="validation",
        )
        parsed = self._safe_parse_json(raw)
        if not isinstance(parsed, dict):
            return False, ["verifier output is not valid JSON"]

        if parsed.get("ok") is True:
            return True, []

        errors: list[str] = []
        raw_errors = parsed.get("errors")
        if isinstance(raw_errors, list):
            for item in raw_errors:
                if not isinstance(item, dict):
                    continue
                description = item.get("description")
                suggestion = item.get("suggestion")
                if isinstance(description, str) and isinstance(suggestion, str):
                    errors.append(f"{description}; suggestion: {suggestion}")
                elif isinstance(description, str):
                    errors.append(description)
        if not errors:
            errors.append("verifier marked evidence as not recoverable")
        return False, errors

    def _verify_group_evidence_recoverability(
        self,
        anchor_key: str,
        group_payload: list[dict[str, Any]],
        texts: list[str],
        table_type: str | None,
        markdown_table: str,
        attribute: str,
        attribute_description: str,
        strategies: str,
        strategy_definitions: str,
    ) -> tuple[bool, list[str]]:
        evidence_payload = [
            {
                "frag_id": f"group_{anchor_key}_frag{idx}",
                "content": text,
            }
            for idx, text in enumerate(texts, start=1)
        ]
        
        attribute_display = f"{attribute} (Description: {attribute_description})" if attribute_description else attribute
        verify_prompt = REFINER_GROUP_VERIFY_USER_PROMPT.format(
            table_type=table_type,
            markdown_table=markdown_table,
            anchor_key=anchor_key,
            attribute=attribute_display,
            group_payload_json=json.dumps(group_payload, ensure_ascii=False, indent=2),
            strategies=strategies,
            detailed_guidance=json.dumps(evidence_payload, ensure_ascii=False, indent=2),
            strategy_definitions=strategy_definitions,
        )
        raw = self.verification_llm.generate(
            verify_prompt,
            system_prompt=REFINER_VERIFY_SYSTEM_SYSTEM,
            task="validation",
        )
        parsed = self._safe_parse_json(raw)
        if not isinstance(parsed, dict):
            return False, ["group verifier output is not valid JSON"]

        if parsed.get("ok") is True:
            return True, []

        errors: list[str] = []
        raw_errors = parsed.get("errors")
        if isinstance(raw_errors, list):
            for item in raw_errors:
                if not isinstance(item, dict):
                    continue
                description = item.get("description")
                suggestion = item.get("suggestion")
                if isinstance(description, str) and isinstance(suggestion, str):
                    errors.append(f"{description}; suggestion: {suggestion}")
                elif isinstance(description, str):
                    errors.append(description)
        if not errors:
            errors.append("group verifier marked evidence as not fully recoverable")
        return False, errors

    def _resolve_relation_strategies(
        self,
        input: RefinerInput,
        row: dict[str, Any],
        row_index: int,
        column_name: str,
    ) -> tuple[list[CapabilityLabel], list[CapabilityLabel]]:
        assignments = input.table.capability_assignments
        if not isinstance(assignments, dict):
            return [], []

        row_payload = self._resolve_assignment_row_payload(
            assignments=assignments,
            row=row,
            primary_key=input.table.schema.primary_key,
            row_index=row_index,
        )
        if not isinstance(row_payload, dict):
            # Compatibility fallback: some datasets keep relation assignments under row-index keys.
            # Only promote this fallback when IC_NR exists, so other strategies keep strict key matching.
            fallback_payload = assignments.get(str(row_index))
            if isinstance(fallback_payload, dict) and self._payload_contains_ic_nr(fallback_payload):
                row_payload = fallback_payload
        if not isinstance(row_payload, dict):
            return [], []

        raw_row_codes: list[str] = []
        row_section = row_payload.get("row")
        if isinstance(row_section, list):
            raw_row_codes.extend([code for code in row_section if isinstance(code, str)])

        col_section = row_payload.get("col")
        raw_attr_codes: list[str] = []
        if isinstance(col_section, dict):
            col_codes = col_section.get(column_name)
            if isinstance(col_codes, list):
                raw_attr_codes.extend([code for code in col_codes if isinstance(code, str)])
        else:
            # Compatibility: flat payload {column_name: [codes], ...}
            flat_codes = row_payload.get(column_name)
            if isinstance(flat_codes, list):
                raw_attr_codes.extend([code for code in flat_codes if isinstance(code, str)])

        return self._decode_strategy_codes(raw_row_codes), self._decode_strategy_codes(raw_attr_codes)

    def _payload_contains_ic_nr(self, row_payload: dict[str, Any]) -> bool:
        row_section = row_payload.get("row")
        if isinstance(row_section, list):
            for code in row_section:
                if isinstance(code, str) and code.strip().replace("-", "_").upper() == "IC_NR":
                    return True

        col_section = row_payload.get("col")
        if isinstance(col_section, dict):
            for col_codes in col_section.values():
                if not isinstance(col_codes, list):
                    continue
                for code in col_codes:
                    if isinstance(code, str) and code.strip().replace("-", "_").upper() == "IC_NR":
                        return True
        return False

    def _build_assignment_row_key(self, row: dict[str, Any], primary_key: list[str], row_index: int) -> str:
        if primary_key:
            return ", ".join(str(row.get(col, "")) for col in primary_key)
        return str(row_index)

    def _assignment_key_candidates(
        self,
        row: dict[str, Any],
        primary_key: list[str],
        row_index: int,
    ) -> list[str]:
        keys: list[str] = []
        composite = self._build_assignment_row_key(row, primary_key, row_index)
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

    def _decode_strategy_codes(self, raw_codes: list[str]) -> list[CapabilityLabel]:
        decoded: list[CapabilityLabel] = []
        for code in raw_codes:
            normalized = code.strip()
            if not normalized:
                continue
            by_name = normalized.replace("-", "_").upper()
            label: CapabilityLabel | None = CapabilityLabel.__members__.get(by_name)
            if label is None:
                try:
                    label = CapabilityLabel(normalized.replace("_", "-").upper())
                except ValueError:
                    label = None
            if label is not None and label not in decoded:
                decoded.append(label)
        return decoded

    def _merge_label_lists(self, *groups: list[CapabilityLabel]) -> list[CapabilityLabel]:
        merged: list[CapabilityLabel] = []
        for group in groups:
            for label in group:
                if label not in merged:
                    merged.append(label)
        return merged

    def _extract_group_strategy_labels(self, labels: list[CapabilityLabel]) -> list[CapabilityLabel]:
        extracted = [label for label in labels if label in self.GROUP_STRATEGIES]
        return sorted(extracted, key=lambda label: label.name)

    def _format_relation_pair_context(
        self,
        row: dict[str, Any],
        foreign_keys: dict[str, str],
    ) -> str:
        parts: list[str] = []
        for fk_col, fk_target in foreign_keys.items():
            value = row.get(fk_col)
            if value in (None, ""):
                continue
            parts.append(f"{fk_col}={value} -> {fk_target}")
        return "; ".join(parts) if parts else "No explicit foreign-key pair context found."

    def _extract_texts_from_payload(
        self,
        parsed: dict[str, Any] | None,
        row_index: int,
        column_index: int,
    ) -> list[str]:
        if not parsed:
            return []
        # Optional field from the prompt schema; parsed for compatibility but not persisted.
        _ = parsed.get("decomposition_rationale")
        pool = parsed.get("evidence_pool")
        if not isinstance(pool, list):
            return []

        texts: list[str] = []
        for item in pool:
            if not isinstance(item, dict):
                continue
            frag_id = item.get("frag_id")
            if isinstance(frag_id, str):
                expected_prefix = f"cell_r{row_index}_c{column_index}_frag"
                if not frag_id.startswith(expected_prefix):
                    # Ignore malformed ids from model output; we enforce deterministic ids downstream.
                    pass
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
        return texts

    def _heuristic_texts(
        self,
        column_name: str,
        raw_value: Any,
        labels: list[CapabilityLabel],
        context: dict[str, Any],
        taboo: bool,
        display_column_name: str | None = None,
    ) -> list[str]:
        label = display_column_name if display_column_name is not None else column_name
        if taboo:
            support = self._pick_context_facts(context, exclude_column=column_name, limit=2)
            if support:
                return [
                    *[f for f in support],
                    f"The value of {label} is inferred from the above facts under a defined rule.",
                ]
            return [f"The value of {label} is inferred from multiple related attributes."]

        label_names = {label.name for label in labels}
        if "TA_US" in label_names and isinstance(raw_value, (int, float)):
            return [
                f"{label} is expressed in a standardized unit system.",
                f"The normalized magnitude is approximately {raw_value}.",
            ]

        if "RI_AC" in label_names:
            support = self._pick_context_facts(context, exclude_column=column_name, limit=2)
            if support:
                return [
                    *support,
                    f"{label} is obtained via arithmetic combination of related fields.",
                ]
            return [f"{label} is computed from arithmetic operations on related attributes."]

        return [f"{label}: {raw_value}"]

    def _pick_context_facts(
        self,
        context: dict[str, Any],
        exclude_column: str,
        limit: int,
    ) -> list[str]:
        facts: list[str] = []
        for key, value in context.items():
            if key == exclude_column:
                continue
            if value in (None, ""):
                continue
            facts.append(f"{key} is {value}.")
            if len(facts) >= limit:
                break
        return facts

    def _is_taboo(self, labels: list[CapabilityLabel]) -> bool:
        # RI* and conflict-like labels trigger taboo rule.
        for label in labels:
            if label.name.startswith("RI_"):
                return True
            if label.name.startswith("CR_"):
                return True
            if label.name == "TD_CA":
                return True
        return False

    def _enforce_taboo(
        self,
        texts: list[str],
        raw_value: Any,
        taboo: bool,
    ) -> list[str]:
        if not taboo:
            return texts
        if raw_value in (None, ""):
            return texts
        needle = str(raw_value).strip()
        if not needle:
            return texts
        filtered = [text for text in texts if needle not in text]
        if not filtered and texts:
            return texts
        return filtered

    def _build_fragment_id(self, row_index: int, column_index: int, frag_index: int) -> str:
        return f"cell_r{row_index}_c{column_index}_frag{frag_index}"

    def _get_column_index(self, rows: list[dict[str, Any]], column_name: str) -> int:
        if not rows:
            return 0
        headers = list(rows[0].keys())
        try:
            return headers.index(column_name)
        except ValueError:
            return 0

    def _format_labeled_anchor_part(self, column_name: str, value: str) -> str:
        """Single field for prompts: `Sony (product_name)` — value plus disambiguating column name."""
        if value:
            return f"{value} ({column_name})"
        return f"({column_name})"

    def _format_primary_key_value(self, row: dict[str, Any], primary_key: list[str], row_index: int) -> str:
        if not primary_key:
            return f"row_index={row_index}"
        pairs = [(col, str(row.get(col, "")).strip()) for col in primary_key]
        if len(pairs) == 1:
            col, val = pairs[0]
            return self._format_labeled_anchor_part(col, val)
        # Prefer all human-readable anchor values (non-id keys) as target entity context.
        def is_human_readable_key(col: str) -> bool:
            c = col.strip().lower()
            return c != "id" and not c.endswith("_id")

        labeled = [
            self._format_labeled_anchor_part(col, val)
            for col, val in pairs
            if val and is_human_readable_key(col)
        ]
        if labeled:
            return ", ".join(labeled)
        labeled_all = [self._format_labeled_anchor_part(col, val) for col, val in pairs if val]
        if labeled_all:
            return ", ".join(labeled_all)
        return ", ".join(self._format_labeled_anchor_part(col, val) for col, val in pairs)

    def _format_entity_anchor_value(
        self,
        row: dict[str, Any],
        anchor_columns: set[str],
        primary_key: list[str],
        row_index: int,
    ) -> str:
        if anchor_columns:
            parts: list[str] = []
            for col in row.keys():
                if col not in anchor_columns:
                    continue
                val = str(row.get(col, "")).strip()
                if val:
                    parts.append(self._format_labeled_anchor_part(col, val))
            if parts:
                return ", ".join(parts)
        return self._format_primary_key_value(
            row=row,
            primary_key=primary_key,
            row_index=row_index,
        )

    def _format_relation_edge_value(
        self,
        row: dict[str, Any],
        foreign_keys: dict[str, str],
        primary_key: list[str],
        row_index: int,
        exclude_column: str | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> str:
        preferred_fk_cols = self._preferred_composite_fk_columns(foreign_keys)
        fk_parts: list[str] = []
        for fk_col in foreign_keys.keys():
            if exclude_column is not None and fk_col == exclude_column:
                continue
            fk_target = foreign_keys.get(fk_col, "")
            table_name, _ = self._parse_fk_target(fk_target)
            if table_name and preferred_fk_cols.get(table_name) not in (None, fk_col):
                continue
            raw_value = row.get(fk_col)
            if raw_value in (None, ""):
                continue
            value = str(raw_value).strip()
            if not value:
                continue
            # If value already encodes "table.col=value", keep it as-is.
            if "=" in value:
                value = self._trim_fk_expanded_value_to_immediate_target(
                    value, fk_col, foreign_keys, anchor_cols_entity_tables
                )
                if not value.strip():
                    continue
                fk_parts.append(value)
            else:
                fk_parts.append(f"{fk_col}={value}")
        if fk_parts:
            return ", ".join(fk_parts)
        # Fallback for malformed schema rows that do not expose FK columns.
        return self._format_primary_key_value(row, primary_key, row_index)

    def _compute_prompt_included_columns(
        self,
        input: RefinerInput,
        target_column_names: set[str],
    ) -> set[str] | None:
        """
        LLM prompt tables: only entity anchors (FK / PK / configured anchor cols) and target
        attribute columns — omit other row attributes (e.g. STU_DOB, STU_PHONE).
        """
        if not input.table.rows:
            return None
        available = set(input.table.rows[0].keys())
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        keep: set[str] = set()
        for name in target_column_names:
            if name in available:
                keep.add(name)
        if is_relation:
            for fk in input.table.schema.foreign_keys.keys():
                if fk in available:
                    keep.add(fk)
            for pk in input.table.schema.primary_key:
                if pk in available:
                    keep.add(pk)
            # ------
            # For relation tables like Student, keep configured anchor columns
            # (e.g. LName/Fname) in prompt context when present.
            keep.update(self._load_anchor_columns(input))
            # ----
        else:
            for pk in input.table.schema.primary_key:
                if pk in available:
                    keep.add(pk)
            keep.update(self._load_anchor_columns(input))
        return keep if keep else None

    def _rows_to_markdown(
        self,
        rows: list[dict[str, Any]],
        foreign_keys: dict[str, str] | None = None,
        table_type: str | None = None,
        row_indices: set[int] | None = None,
        excluded_columns: set[str] | None = None,
        included_columns: set[str] | None = None,
        anchor_cols_entity_tables: set[str] | None = None,
    ) -> str:
        """Convert rows to markdown. If row_indices is set, only include those rows (header + subset)."""
        if not rows:
            return "| |\n|---|\n| |"
        headers = list(rows[0].keys())
        if included_columns:
            narrowed = [h for h in headers if h in included_columns]
            if narrowed:
                headers = narrowed
        if row_indices is not None:
            rows = [rows[i] for i in sorted(row_indices) if 0 <= i < len(rows)]
        fk_map = foreign_keys or {}
        is_relation = (table_type or "").strip().lower() == "relation"
        preferred_fk_cols = self._preferred_composite_fk_columns(fk_map) if is_relation else {}

        # When multiple FK columns expand to the same anchor key (e.g. both account_id and
        # client_id yield district.A2), disambiguate display headers with the immediate
        # referenced table: district.A2(account) vs district.A2(client). Lookup keys stay
        # unchanged (expanded_header).
        fk_sources_per_expanded: dict[str, set[str]] = defaultdict(set)
        if is_relation:
            for h in headers:
                if excluded_columns and h in excluded_columns:
                    continue
                if h in fk_map:
                    expanded_for_h = self._infer_expanded_anchor_headers(rows, h)
                    expanded_for_h = self._filter_expanded_headers_for_anchor_policy(
                        expanded_for_h, h, fk_map, anchor_cols_entity_tables
                    )
                    for eh in expanded_for_h:
                        fk_sources_per_expanded[eh].add(h)

        render_specs: list[tuple[str, str, str | None]] = []
        for header in headers:
            if excluded_columns and header in excluded_columns:
                continue
            if is_relation and header in fk_map:
                fk_target = fk_map.get(header, "")
                table_name, _ = self._parse_fk_target(fk_target)
                if table_name and preferred_fk_cols.get(table_name) not in (None, header):
                    continue
                expanded_headers = self._infer_expanded_anchor_headers(rows, header)
                expanded_headers = self._filter_expanded_headers_for_anchor_policy(
                    expanded_headers, header, fk_map, anchor_cols_entity_tables
                )
                if expanded_headers:
                    for expanded_header in expanded_headers:
                        display_name = expanded_header
                        if len(fk_sources_per_expanded.get(expanded_header, set())) > 1:
                            ref_table = self._fk_referenced_table_name(fk_map, header)
                            if ref_table:
                                # display_name = f"{expanded_header}({ref_table})"
                                display_name = f"{header}.{expanded_header.split('.')[-1]}"
                        render_specs.append((header, display_name, expanded_header))
                    continue
            render_specs.append(
                (
                    header,
                    self._display_header_name(
                        col_name=header,
                        foreign_keys=fk_map,
                        table_type=table_type,
                    ),
                    None,
                )
            )

        header_display = [spec[1] for spec in render_specs]
        header_line = "| " + " | ".join(header_display) + " |"
        divider_line = "|" + "|".join(["---"] * len(render_specs)) + "|"
        body_lines: list[str] = []
        for row in rows:
            values: list[str] = []
            for source_col, _, expanded_key in render_specs:
                raw_value = row.get(source_col)
                if expanded_key is None:
                    values.append("" if raw_value is None else str(raw_value))
                    continue
                anchor_map = self._parse_anchor_kv(raw_value)
                values.append(anchor_map.get(expanded_key, ""))
            body_lines.append("| " + " | ".join(values) + " |")
        return "\n".join([header_line, divider_line, *body_lines])

    def _infer_expanded_anchor_headers(self, rows: list[dict[str, Any]], column_name: str) -> list[str]:
        for row in rows:
            anchor_map = self._parse_anchor_kv(row.get(column_name))
            if anchor_map:
                return list(anchor_map.keys())
        return []

    def _parse_anchor_kv(self, value: Any) -> dict[str, str]:
        if not isinstance(value, str):
            return {}
        if "=" not in value:
            return {}

        parsed: dict[str, str] = {}
        for chunk in value.split("|"):
            piece = chunk.strip()
            if not piece or "=" not in piece:
                continue
            key, val = piece.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key:
                parsed[key] = val
        return parsed

    def _display_header_name(
        self,
        col_name: str,
        foreign_keys: dict[str, str],
        table_type: str | None,
    ) -> str:
        is_relation = (table_type or "").strip().lower() == "relation"
        if not is_relation:
            return col_name
        fk_target = foreign_keys.get(col_name)
        if not fk_target:
            return col_name
        target_table, _ = self._parse_fk_target(fk_target)
        if not target_table:
            return col_name
        return f"{target_table}_anchor"

    def _parse_fk_target(self, fk_target: str) -> tuple[str | None, str | None]:
        if "." not in fk_target:
            return None, None
        table_name, col_name = fk_target.split(".", 1)
        table_name = table_name.strip()
        col_name = col_name.strip()
        if not table_name or not col_name:
            return None, None
        return table_name, col_name

    def _preferred_composite_fk_columns(self, foreign_keys: dict[str, str]) -> dict[str, str]:
        """Pick a single FK column for composite-key tables (same target table, different target cols)."""
        # by_table: dict[str, list[tuple[str, str]]] = defaultdict(list)
        # for fk_col, fk_target in foreign_keys.items():
        #     table_name, col_name = self._parse_fk_target(fk_target)
        #     if not table_name or not col_name:
        #         continue
        #     by_table[table_name].append((fk_col, col_name))

        # preferred: dict[str, str] = {}
        # for table_name, items in by_table.items():
        #     if len(items) <= 1:
        #         continue
        #     target_cols = {col for _, col in items}
        #     # If multiple FK cols point to different target columns, treat as composite.
        #     if len(target_cols) <= 1:
        #         continue
        #     chosen = items[0][0]
        #     for fk_col, col_name in items:
        #         if "id" in fk_col.lower() or col_name.lower().endswith("id"):
        #             chosen = fk_col
        #             break
        #     preferred[table_name] = chosen
        # return preferred

        return {}

    def _fk_referenced_table_name(
        self,
        foreign_keys: dict[str, str],
        fk_column: str,
    ) -> str | None:
        """Immediate FK target table for a column (e.g. account_id -> account)."""
        fk_target = foreign_keys.get(fk_column)
        if not fk_target:
            return None
        table_name, _ = self._parse_fk_target(fk_target)
        return table_name

    def _immediate_fk_target_table_upper(
        self,
        fk_column: str,
        foreign_keys: dict[str, str],
    ) -> str | None:
        fk_target = foreign_keys.get(fk_column)
        if not fk_target:
            return None
        table_name, _ = self._parse_fk_target(fk_target)
        return table_name.strip().upper() if table_name else None

    def _expanded_header_entity_table(self, expanded_header: str) -> str | None:
        if "." not in expanded_header:
            return None
        return expanded_header.split(".", 1)[0].strip().upper()

    def _entity_table_from_anchor_chunk(self, chunk: str) -> str | None:
        piece = chunk.strip()
        if "=" not in piece:
            return None
        left = piece.split("=", 1)[0].strip()
        if "." not in left:
            return None
        return left.split(".", 1)[0].strip().upper()

    def _filter_expanded_headers_for_anchor_policy(
        self,
        expanded_headers: list[str],
        fk_column: str,
        foreign_keys: dict[str, str],
        anchor_entity_tables: set[str] | None,
    ) -> list[str]:
        if not expanded_headers or not anchor_entity_tables:
            return expanded_headers
        immediate = self._immediate_fk_target_table_upper(fk_column, foreign_keys)
        if not immediate or immediate not in anchor_entity_tables:
            return expanded_headers
        filtered = [
            h
            for h in expanded_headers
            if self._expanded_header_entity_table(h) == immediate
        ]
        return filtered if filtered else expanded_headers

    def _trim_fk_expanded_value_to_immediate_target(
        self,
        value: str,
        fk_column: str,
        foreign_keys: dict[str, str],
        anchor_entity_tables: set[str] | None,
    ) -> str:
        if not anchor_entity_tables or not value or "=" not in value:
            return value
        immediate = self._immediate_fk_target_table_upper(fk_column, foreign_keys)
        if not immediate or immediate not in anchor_entity_tables:
            return value
        if "|" not in value:
            t = self._entity_table_from_anchor_chunk(value)
            return value if t == immediate else ""
        kept: list[str] = []
        for chunk in value.split("|"):
            c = chunk.strip()
            if not c:
                continue
            t = self._entity_table_from_anchor_chunk(c)
            if t == immediate:
                kept.append(c)
        return " | ".join(kept)

    def _build_strategy_definitions(self, strategies: list[str], table_type: str | None) -> str:
        detailed_defs = get_capability_definitions(table_type, detailed=True)
        global_defs = get_capability_definitions(None, detailed=True)
        if not strategies:
            return "- NO_LABEL: Direct extraction is allowed unless taboo is triggered by policy."
        lines: list[str] = []
        for code in strategies:
            normalized_code = code.strip()
            if not normalized_code:
                continue
            definition = (
                detailed_defs.get(normalized_code)
                or detailed_defs.get(normalized_code.replace("-", "_"))
                or global_defs.get(normalized_code)
                or global_defs.get(normalized_code.replace("-", "_"))
            )
            if definition:
                lines.append(definition)
            else:
                lines.append(f"* **{normalized_code}:** Apply the strategy conservatively with atomic facts.")
        return "\n".join(lines)

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

    def _get_attribute_description(self, input: RefinerInput, column_name: str) -> str:
        """Lookup column description from loaded table schema for prompt grounding."""
        if not column_name:
            return ""

        def _merge_desc(column: Any) -> str:
            desc = (column.description or "").strip()
            rng = (getattr(column, "range", None) or "").strip()
            if rng:
                return f"{desc}. Normal range: {rng}".strip(". ") if desc else f"Normal range: {rng}"
            return desc if desc else column.name

        for column in input.table.schema.columns:
            if column.name == column_name:
                merged = _merge_desc(column)
                return merged if merged else column_name
        lowered = column_name.lower()
        for column in input.table.schema.columns:
            if column.name.lower() == lowered:
                merged = _merge_desc(column)
                return merged if merged else column_name
        return column_name

    def _write_evidence_json(self, input: RefinerInput, pool: EvidencePool) -> None:
        if input.write_back_path is None:
            return
        output_path = self._derive_evidence_output_path(input.write_back_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = pool.model_dump(mode="json")
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _derive_evidence_output_path(self, write_back_path: Path) -> Path:
        suffixes = "".join(write_back_path.suffixes)
        if suffixes:
            base_name = write_back_path.name[: -len(suffixes)]
            filename = f"{base_name}.evidence{suffixes}"
        else:
            filename = f"{write_back_path.name}.evidence.json"
        return write_back_path.parent / "evidence" / filename

    def _resolve_excluded_columns(self, input: RefinerInput) -> set[str]:
        if getattr(getattr(input.config, "base", None), "if_primary_allowed", False):
            return set()
        excluded = set(input.table.schema.primary_key)
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        if is_relation:
            excluded.update(input.table.schema.foreign_keys.keys())
            excluded.update(self._load_relation_primary_key_columns_from_schema(input))
            return excluded
        excluded.update(self._load_anchor_columns(input))
        return excluded

    def _resolve_prompt_excluded_columns(self, input: RefinerInput) -> set[str]:
        if getattr(getattr(input.config, "base", None), "if_primary_allowed", False):
            return set()
        excluded = set(input.table.schema.primary_key)
        is_relation = (input.table.schema.table_type or "").strip().lower() == "relation"
        if is_relation:
            excluded.update(self._load_relation_primary_key_columns_from_schema(input))
            # If anchor_cols are defined for a relation table, ensure they are NOT excluded from the prompt display
            anchor_cols = self._load_anchor_columns(input)
            if anchor_cols:
                excluded.difference_update(anchor_cols)

            return excluded

        # excluded = set(input.table.schema.primary_key)
        # excluded.update(self._load_anchor columns(input))
        excluded.update(self._load_anchor_columns(input))

        return excluded

    def _is_fk_only_relation_table(self, input: RefinerInput) -> bool:
        if (input.table.schema.table_type or "").strip().lower() != "relation":
            return False
        if not input.table.rows:
            return False
        fk_cols = set(input.table.schema.foreign_keys.keys())
        if not fk_cols:
            return False
        row_columns = set(input.table.rows[0].keys())
        non_pk_columns = {
            col
            for col in row_columns
            if col not in set(input.table.schema.primary_key)
        }
        return bool(non_pk_columns) and non_pk_columns.issubset(fk_cols)

    def _load_relation_primary_key_columns_from_schema(self, input: RefinerInput) -> set[str]:
        if input.write_back_path is None:
            return set()

        schema_path = input.write_back_path.parent.parent / "schema.json"
        if not schema_path.exists():
            return set()

        try:
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(payload, list):
            return set()

        row_columns = set(input.table.rows[0].keys()) if input.table.rows else set()
        for table_spec in payload:
            if not isinstance(table_spec, dict):
                continue
            table_name = table_spec.get("name")
            if not isinstance(table_name, str) or table_name != input.table.table_id:
                continue

            raw_fields = table_spec.get("fields")
            if not isinstance(raw_fields, list):
                return set()

            primary_key_cols: set[str] = set()
            for field in raw_fields:
                if not isinstance(field, dict):
                    continue
                field_name = field.get("name")
                constraints = field.get("constraints")
                if not isinstance(field_name, str) or not isinstance(constraints, dict):
                    continue
                if constraints.get("primary_key") is True and field_name in row_columns:
                    primary_key_cols.add(field_name)
            return primary_key_cols
        return set()

    def _load_anchor_columns(self, input: RefinerInput) -> set[str]:
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

    def _load_anchor_cols_entity_table_names(self, input: RefinerInput) -> set[str] | None:
        """Entity table names listed under CONFIG['anchor_cols'][0] (e.g. COURSE, DEPARTMENT)."""
        if input.write_back_path is None:
            return None

        config_path = input.write_back_path.parent.parent / "config.py"
        if not config_path.exists():
            return None

        spec = importlib.util.spec_from_file_location(
            f"data_construction_dataset_config_{config_path.stem}",
            str(config_path),
        )
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = getattr(module, "CONFIG", None)
        if not isinstance(config, dict):
            return None

        raw_anchor_cols = config.get("anchor_cols")
        if not isinstance(raw_anchor_cols, list) or not raw_anchor_cols:
            return None

        first = raw_anchor_cols[0]
        if not isinstance(first, dict):
            return None

        names = {str(k).strip().upper() for k in first if isinstance(k, str) and str(k).strip()}
        return names if names else None
