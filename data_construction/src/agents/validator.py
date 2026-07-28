from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from .base import BaseAgent
from ..config import get_default_config
from ..llm.base import BaseLLMClient
from ..llm.openai_client import OpenAILLMClient
from ..models import (
    DocumentBlock,
    EvidenceFragment,
    EvidencePool,
    ProvenanceDocument,
    Table,
    ValidationIssue,
    ValidationReport,
    ValidatorInput,
)
from ..utils.input_loader import load_database_from_directory

_TABLE_HEADER_ALIASES: dict[str, dict[str, str]] = {}


VERIFY_SECTION_SYSTEM = (
    "You are a meticulous content verifier responsible for ensuring that written sections "
    "contain all required facts, follow their specific facts correctly, and maintain "
    "complete factual accuracy."
)

REPAIR_SECTION_SYSTEM = (
    "You are a precise content editor responsible for fixing verification errors in written "
    "sections while preserving the narrative flow and ensuring all other facts remain correct."
)

VERIFY_SECTION_PROMPT = """
**Your Goal:**
Verify whether the generated text is faithful to the table content without fabrication.

**Table Content:**
---
{markdown_table}
---

**Generated Text:**
---
{generated_text}
---

**Verification Checks:**
1.  **Cell Extraction Consistency:** Key values stated in `generated_text` must be extractable or inferrable from `Table Content`.

**Output Format:**
Respond with ONLY a single, valid JSON object:
---
```json
{{
    "ok": true/false,
    "errors": [
        {{
            "description": "Error description",
            "suggestion": "How to fix"
        }}
    ]
}}
```
---

**FINAL INSTRUCTION: Output ONLY the valid JSON object. If any check fails, set `ok` to false and include all issues in `errors`.**
"""

REPAIR_SECTION_PROMPT = """
**Goal:** Repair a section of text that failed verification.

**Input:**
* **Original Text:**
{content}
* **Errors:**
{errors}
* **Required Facts:**
{facts}

**Instructions:**
1. Repair the section to fix the reported errors.
2. Ensure all required facts remain correct.
3. Keep the narrative flow natural.
4. Preserve all provenance-style tags already present in the original text.

**Output Format:**
Return ONLY the repaired text content. No titles, no metadata, no markdown code blocks.
"""


class ValidatorAgent(BaseAgent[ValidatorInput, ValidationReport]):
    def __init__(self, llm: BaseLLMClient | None = None) -> None:
        self.llm = llm

    @staticmethod
    def _safe_parse_json(text: str) -> dict[str, Any] | None:
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

    @staticmethod
    def _extract_title(text: str, fallback: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:120]
        return fallback

    @staticmethod
    def _rows_to_markdown(
        rows: list[dict[str, Any]],
        row_indices: set[int],
        header_aliases: dict[str, str] | None = None,
        exclude_columns: set[str] | None = None,
    ) -> str:
        if not rows:
            return "| |\n|---|\n| |"
        excluded = exclude_columns or set()
        headers = [h for h in rows[0].keys() if h not in excluded]
        if not headers:
            return "| |\n|---|\n| |"
        picked_rows = [rows[i] for i in sorted(row_indices) if 0 <= i < len(rows)]
        if not picked_rows:
            picked_rows = rows[: min(len(rows), 5)]
        display_headers: list[str] = []
        aliases = header_aliases or {}
        for header in headers:
            if header in aliases and aliases[header].strip():
                display_headers.append(aliases[header].strip())
                continue
            is_anchor_like = False
            for row in picked_rows:
                value = row.get(header)
                if isinstance(value, str) and "=" in value and ("|" in value or "." in value):
                    is_anchor_like = True
                    break
            display_headers.append(f"{header} (anchor)" if is_anchor_like else header)
        header_line = "| " + " | ".join(display_headers) + " |"
        divider_line = "|" + "|".join(["---"] * len(headers)) + "|"

        def _escape_markdown_cell(value: Any) -> str:
            return str(value).replace("|", r"\|")

        body = [
            "| " + " | ".join(_escape_markdown_cell(row.get(h, "")) for h in headers) + " |"
            for row in picked_rows
        ]
        return "\n".join([header_line, divider_line, *body])

    @staticmethod
    def _build_facts(required_frags: list[EvidenceFragment]) -> str:
        lines: list[str] = []
        for frag in required_frags:
            lines.append(
                "\n".join(
                    [
                        f"- **Fragment ID:** {frag.fragment_id}",
                        f"  **Cell:** r{frag.source_cell.row_index} + {frag.source_cell.column_name}",
                        f"  **Fact:** {frag.text}",
                    ]
                )
            )
        return "\n".join(lines) if lines else "(empty)"

    @staticmethod
    def _cell_tag_from_fragment_id(fragment_id: str, fallback_row_index: int, fallback_column_name: str) -> str:
        match = re.search(r"_r(\d+)_c(\d+)", fragment_id)
        if match:
            row_idx, col_idx = match.groups()
            return f"r{row_idx}_c{col_idx}"
        fallback_col = 0
        col_match = re.search(r"(\d+)$", fallback_column_name or "")
        if col_match:
            fallback_col = int(col_match.group(1))
        return f"r{fallback_row_index}_c{fallback_col}"

    @staticmethod
    def _expand_cell_tag(tag: str) -> set[str]:
        # Support merged tags like r1,2,3_c4 emitted by writer.
        match = re.fullmatch(r"r(\d+(?:,\d+)*)_c(\d+)", tag.strip())
        if not match:
            return set()
        rows_part, col_part = match.groups()
        col = int(col_part)
        tags: set[str] = set()
        for row_text in rows_part.split(","):
            row_text = row_text.strip()
            if not row_text:
                continue
            tags.add(f"r{int(row_text)}_c{col}")
        return tags

    @staticmethod
    def _extract_tagged_sentences(text: str) -> dict[str, list[str]]:
        tag_to_sentences: dict[str, list[str]] = {}
        pattern = re.compile(r"<(r\d+(?:,\d+)*_c\d+)>(.*?)</\1>", re.DOTALL)
        for match in pattern.finditer(text):
            raw_tag = match.group(1)
            sentence = match.group(2).strip()
            if not sentence:
                continue
            for expanded_tag in ValidatorAgent._expand_cell_tag(raw_tag):
                tag_to_sentences.setdefault(expanded_tag, []).append(sentence)
        return tag_to_sentences

    @staticmethod
    def _lookup_cell_value(table: Table, frag: EvidenceFragment) -> str:
        if frag.source_cell.value is not None:
            return str(frag.source_cell.value)
        row_index = frag.source_cell.row_index
        column_name = frag.source_cell.column_name
        if 0 <= row_index < len(table.rows):
            return str(table.rows[row_index].get(column_name, ""))
        return ""

    def _build_cell_verification_items(
        self,
        table: Table,
        block_text: str,
        required_frags: list[EvidenceFragment],
    ) -> tuple[str, list[str]]:
        tag_to_sentences = self._extract_tagged_sentences(block_text)
        cell_order: list[str] = []
        cell_meta: dict[str, dict[str, Any]] = {}
        for frag in required_frags:
            cell_tag = self._cell_tag_from_fragment_id(
                fragment_id=frag.fragment_id,
                fallback_row_index=frag.source_cell.row_index,
                fallback_column_name=frag.source_cell.column_name,
            )
            if cell_tag not in cell_meta:
                cell_order.append(cell_tag)
                cell_meta[cell_tag] = {
                    "value": self._lookup_cell_value(table, frag),
                    "facts": [],
                    "fragment_ids": [],
                }
            cell_meta[cell_tag]["facts"].append(frag.text)
            cell_meta[cell_tag]["fragment_ids"].append(frag.fragment_id)

        items: list[str] = []
        precheck_errors: list[str] = []
        for cell_tag in cell_order:
            generated_list = tag_to_sentences.get(cell_tag, [])
            generated_text = " ".join(part.strip() for part in generated_list if part.strip()).strip()
            if not generated_text:
                frag_ids = ",".join(cell_meta[cell_tag]["fragment_ids"])
                precheck_errors.append(
                    f"[cell:{cell_tag}] missing generated_text mapped by <{cell_tag}> (fragments={frag_ids})"
                )
            facts_lines = "\n".join(f"    - {fact}" for fact in cell_meta[cell_tag]["facts"])
            items.append(
                "\n".join(
                    [
                        f"- Cell Tag: {cell_tag}",
                        f"  fragment_ids: {', '.join(cell_meta[cell_tag]['fragment_ids'])}",
                        f"  cell: {cell_meta[cell_tag]['value']}",
                        f"  generated_text: {generated_text if generated_text else '(missing)'}",
                        "  facts:",
                        f"{facts_lines if facts_lines else '    - (empty)'}",
                    ]
                )
            )
        return ("\n".join(items) if items else "(empty)"), precheck_errors

    def _soft_verify_and_repair(
        self,
        input: ValidatorInput,
        block: DocumentBlock,
        required_frags: list[EvidenceFragment],
    ) -> tuple[bool, list[str]]:
        if self.llm is None:
            print(f"[SOFT] block={block.block_id} skipped (llm is None)")
            return True, []

        print(
            f"[SOFT] block={block.block_id} start "
            f"(required_frags={len(required_frags)})"
        )

        row_indices = {frag.source_cell.row_index for frag in required_frags}
        markdown_table = self._rows_to_markdown(
            input.table.rows,
            row_indices=row_indices,
            header_aliases=_TABLE_HEADER_ALIASES.get(input.table.table_id),
            exclude_columns=set(input.table.schema.primary_key),
        )
        max_rounds = max(1, input.config.runtime.max_validation_retries + 1)
        current_text = block.text
        for round_idx in range(max_rounds):
            verify_prompt = VERIFY_SECTION_PROMPT.format(
                markdown_table=markdown_table,
                generated_text=current_text,
            )
            print(verify_prompt)
            # exit()
            raw = self.llm.generate(
                verify_prompt,
                system_prompt=VERIFY_SECTION_SYSTEM,
                task="validation",
                temperature=0.0,
            )
            print(raw)
            parsed = self._safe_parse_json(raw)
            if isinstance(parsed, dict) and parsed.get("ok") is True:
                print(f"[SOFT] block={block.block_id} round={round_idx + 1}/{max_rounds} verify=PASS")
                if current_text != block.text:
                    block.text = current_text
                    print(f"[SOFT] block={block.block_id} text updated after repair")
                return True, []

            errors_payload = parsed.get("errors") if isinstance(parsed, dict) else None
            errors: list[str] = []
            if isinstance(errors_payload, list):
                for item in errors_payload:
                    if not isinstance(item, dict):
                        continue
                    desc = item.get("description")
                    sugg = item.get("suggestion")
                    if isinstance(desc, str) and isinstance(sugg, str):
                        errors.append(f"{desc}; suggestion: {sugg}")
                    elif isinstance(desc, str):
                        errors.append(desc)
            if not errors:
                errors.append("soft verification failed with invalid verifier output")
            print(
                f"[SOFT] block={block.block_id} round={round_idx + 1}/{max_rounds} verify=FAIL "
                f"(errors={len(errors)})"
            )

            facts = self._build_facts(required_frags)
            repair_prompt = REPAIR_SECTION_PROMPT.format(
                content=current_text,
                errors="\n".join(f"- {e}" for e in errors),
                facts=facts,
            )
            repaired = self.llm.generate(
                repair_prompt,
                system_prompt=REPAIR_SECTION_SYSTEM,
                task="validation",
                temperature=0.0,
            ).strip()
            print(repaired)
            if repaired:
                current_text = repaired
                print(f"[SOFT] block={block.block_id} round={round_idx + 1}/{max_rounds} repair=APPLIED")
            else:
                print(f"[SOFT] block={block.block_id} round={round_idx + 1}/{max_rounds} repair=EMPTY")

        if current_text != block.text:
            block.text = current_text
            print(f"[SOFT] block={block.block_id} text updated at final fallback")
        print(f"[SOFT] block={block.block_id} final=FAIL after {max_rounds} rounds")
        return False, ["soft verification still failed after repair attempts"]

    def run(self, input: ValidatorInput) -> ValidationReport:
        """Step 5: perform hard provenance checks and soft extractability checks for acceptance."""
        print(
            f"[VALIDATOR] start table={input.table.table_id} "
            f"blocks={len(input.document.blocks)} fragments={len(input.evidence_pool.fragments)}"
        )
        all_expected = list(input.evidence_pool.fragments)
        expected_tags = {
            self._cell_tag_from_fragment_id(
                fragment_id=frag.fragment_id,
                fallback_row_index=frag.source_cell.row_index,
                fallback_column_name=frag.source_cell.column_name,
            )
            for frag in all_expected
        }
        found_tags_all_blocks: set[str] = set()

        issues: list[ValidationIssue] = []
        hard_pass = True
        tag_pattern = re.compile(r"<(r\d+(?:,\d+)*_c\d+)>")
        frag_map = {frag.fragment_id: frag for frag in all_expected}

        for block in input.document.blocks:
            block_found_tags: set[str] = set()
            for raw_tag in tag_pattern.findall(block.text):
                block_found_tags.update(self._expand_cell_tag(raw_tag))
            found_tags_all_blocks.update(block_found_tags)

            required_tags_for_block: set[str] = set()
            for fid in block.used_fragment_ids:
                frag = frag_map.get(fid)
                if frag is None:
                    continue
                required_tags_for_block.add(
                    self._cell_tag_from_fragment_id(
                        fragment_id=fid,
                        fallback_row_index=frag.source_cell.row_index,
                        fallback_column_name=frag.source_cell.column_name,
                    )
                )

            missing_tags_for_block = sorted(required_tags_for_block - block_found_tags)
            if missing_tags_for_block:
                hard_pass = False
                print(
                    f"[HARD] block={block.block_id} FAIL "
                    f"(required={len(required_tags_for_block)}, found={len(block_found_tags)}, "
                    f"missing={len(missing_tags_for_block)})"
                )
                missing_fragment_ids = [
                    fid
                    for fid in block.used_fragment_ids
                    if (
                        (frag := frag_map.get(fid)) is not None
                        and self._cell_tag_from_fragment_id(
                            fragment_id=fid,
                            fallback_row_index=frag.source_cell.row_index,
                            fallback_column_name=frag.source_cell.column_name,
                        ) in missing_tags_for_block
                    )
                ]
                print(
                    f"[HARD] block={block.block_id} missing_fragment_ids="
                    f"{missing_fragment_ids if missing_fragment_ids else '(none)'}"
                )
                issues.append(
                    ValidationIssue(
                        code="HARD_BLOCK_TAG_MISSING",
                        message=f"Missing required cell tags in block: {missing_tags_for_block}",
                        block_id=block.block_id,
                        fragment_ids=missing_fragment_ids,
                    )
                )
            else:
                print(
                    f"[HARD] block={block.block_id} PASS "
                    f"(required={len(required_tags_for_block)}, found={len(block_found_tags)})"
                )

        if len(expected_tags) == 0:
            coverage_rate = 1.0
        else:
            coverage_rate = len(found_tags_all_blocks & expected_tags) / len(expected_tags)
        hard_pass = hard_pass and coverage_rate >= 1.0
        print(
            f"[HARD] coverage={coverage_rate:.4f} "
            f"(found_tags={len(found_tags_all_blocks)}, expected_tags={len(expected_tags)}) "
            f"final={'PASS' if hard_pass else 'FAIL'}"
        )
        soft_pass = True

        if not hard_pass:
            missing = sorted(expected_tags - found_tags_all_blocks)
            issues.append(
                ValidationIssue(
                    code="PROV_COVERAGE_INCOMPLETE",
                    message=f"Missing cell coverage tags: {missing}",
                    fragment_ids=missing,
                )
            )
            print(f"[VALIDATOR] hard failed, skip soft check")

        if hard_pass and input.config.runtime.enable_soft_validation:
            for block in input.document.blocks:
                required = [frag_map[fid] for fid in block.used_fragment_ids if fid in frag_map]
                ok, errors = self._soft_verify_and_repair(input=input, block=block, required_frags=required)
                if not ok:
                    soft_pass = False
                    print(f"[SOFT] block={block.block_id} FAIL (errors={len(errors)})")
                    issues.append(
                        ValidationIssue(
                            code="SOFT_EXTRACTABILITY_FAILED",
                            message="; ".join(errors) if errors else "soft extractability verification failed",
                            block_id=block.block_id,
                            fragment_ids=[frag.fragment_id for frag in required],
                        )
                    )
                else:
                    print(f"[SOFT] block={block.block_id} PASS")
            if input.document.blocks:
                input.document.full_text = "\n\n".join(block.text for block in input.document.blocks)
        accepted = hard_pass and soft_pass
        print(
            f"[VALIDATOR] done hard_pass={hard_pass} soft_pass={soft_pass} "
            f"accepted={accepted} issues={len(issues)}"
        )
        return ValidationReport(
            hard_pass=hard_pass,
            soft_pass=soft_pass,
            accepted=accepted,
            coverage_rate=coverage_rate,
            issues=issues,
        )


def _load_table(database_root: Path, table_id: str, tables_subdir: str, schema_filename: str) -> Table:
    database = load_database_from_directory(
        database_root=str(database_root),
        tables_subdir=tables_subdir,
        schema_filename=schema_filename,
    )
    dataset_config = _load_dataset_config(database_root)
    anchor_map = _build_anchor_map(dataset_config)
    tables_by_name = {table.table_id: table for table in database.tables}
    for table in database.tables:
        if table.table_id == table_id:
            prepared, header_aliases = _prepare_table_for_validator(
                table=table,
                tables_by_name=tables_by_name,
                anchor_map=anchor_map,
            )
            _TABLE_HEADER_ALIASES[table_id] = header_aliases
            return prepared
    raise ValueError(f"table_id '{table_id}' not found under {database_root}")


def _load_dataset_config(database_root: Path) -> dict[str, Any] | None:
    config_path = database_root / "config.py"
    if not config_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"validator_dataset_config_{config_path.stem}",
        str(config_path),
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = getattr(module, "CONFIG", None)
    return config if isinstance(config, dict) else None


def _build_anchor_map(dataset_config: dict[str, Any] | None) -> dict[str, list[str]]:
    if not dataset_config:
        return {}
    return merge_anchor_cols_config(dataset_config.get("anchor_cols"))


def _lookup_anchor_cols(anchor_map: dict[str, list[str]], table_name: str) -> list[str]:
    if table_name in anchor_map:
        return anchor_map[table_name]
    lower_map = {k.lower(): v for k, v in anchor_map.items() if isinstance(k, str)}
    return lower_map.get(table_name.lower(), [])


def _parse_fk_target(fk_target: str) -> tuple[str | None, str | None]:
    if "." not in fk_target:
        return None, None
    table_name, col_name = fk_target.split(".", 1)
    table_name = table_name.strip()
    col_name = col_name.strip()
    if not table_name or not col_name:
        return None, None
    return table_name, col_name


def _resolve_anchor_pairs_for_row(
    *,
    table: Table,
    row: dict[str, Any],
    tables_by_name: dict[str, Table],
    anchor_map: dict[str, list[str]],
    fallback_key_col: str,
    fallback_anchor_cols: list[str],
    visited: set[str],
    depth: int,
) -> list[str]:
    if depth <= 0:
        return []
    visit_key = f"{table.table_id}:{row.get(fallback_key_col)}"
    if visit_key in visited:
        return []
    visited = set(visited)
    visited.add(visit_key)

    pairs: list[str] = []
    anchor_cols = _lookup_anchor_cols(anchor_map, table.table_id) or fallback_anchor_cols
    for col in anchor_cols:
        value = str(row.get(col, "")).strip()
        if value:
            pairs.append(f"{table.table_id}.{col}={value}")
    if pairs:
        return pairs

    for fk_col, fk_target in table.schema.foreign_keys.items():
        child_table_name, child_key_col = _parse_fk_target(fk_target)
        if not child_table_name or not child_key_col:
            continue
        child_table = tables_by_name.get(child_table_name)
        if child_table is None or not child_table.rows:
            continue
        fk_value = row.get(fk_col)
        for child_row in child_table.rows:
            if child_row.get(child_key_col) != fk_value:
                continue
            child_pairs = _resolve_anchor_pairs_for_row(
                table=child_table,
                row=child_row,
                tables_by_name=tables_by_name,
                anchor_map=anchor_map,
                fallback_key_col=child_key_col,
                fallback_anchor_cols=_lookup_anchor_cols(anchor_map, child_table_name) or [child_key_col],
                visited=visited,
                depth=depth - 1,
            )
            if child_pairs:
                return child_pairs
    return []


def _build_multihop_fk_anchor_lookup(
    *,
    table_name: str,
    key_col: str,
    tables_by_name: dict[str, Table],
    anchor_map: dict[str, list[str]],
    fallback_anchor_cols: list[str],
) -> dict[Any, str]:
    table = tables_by_name.get(table_name)
    if table is None or not table.rows:
        return {}
    lookup: dict[Any, str] = {}
    for row in table.rows:
        key = row.get(key_col)
        if key in (None, ""):
            continue
        pairs = _resolve_anchor_pairs_for_row(
            table=table,
            row=row,
            tables_by_name=tables_by_name,
            anchor_map=anchor_map,
            fallback_key_col=key_col,
            fallback_anchor_cols=fallback_anchor_cols,
            visited=set(),
            depth=4,
        )
        if not pairs:
            continue
        anchor_text = " | ".join(pairs)
        lookup[key] = anchor_text
        lookup[str(key)] = anchor_text
    return lookup


def _prepare_table_for_validator(
    *,
    table: Table,
    tables_by_name: dict[str, Table],
    anchor_map: dict[str, list[str]],
) -> tuple[Table, dict[str, str]]:
    prepared = table.model_copy(deep=True)
    header_aliases: dict[str, str] = {}
    is_relation = (prepared.schema.table_type or "").strip().lower() == "relation"
    if not is_relation or not prepared.rows:
        return prepared, header_aliases

    for fk_col, fk_target in prepared.schema.foreign_keys.items():
        if fk_col not in prepared.rows[0]:
            continue
        target_table_name, target_col = _parse_fk_target(fk_target)
        if not target_table_name or not target_col:
            continue
        target_table = tables_by_name.get(target_table_name)
        if target_table is None or not target_table.rows:
            continue
        target_row_cols = set(target_table.rows[0].keys())
        target_anchor_cols = [
            col for col in _lookup_anchor_cols(anchor_map, target_table_name) if col in target_row_cols
        ] or [target_col]
        header_aliases[fk_col] = "/".join(f"{target_table_name}.{col}" for col in target_anchor_cols)
        lookup = _build_multihop_fk_anchor_lookup(
            table_name=target_table_name,
            key_col=target_col,
            tables_by_name=tables_by_name,
            anchor_map=anchor_map,
            fallback_anchor_cols=target_anchor_cols,
        )
        if not lookup:
            continue
        for row in prepared.rows:
            raw_fk_value = row.get(fk_col)
            if raw_fk_value in lookup:
                row[fk_col] = lookup[raw_fk_value]
            else:
                text_key = str(raw_fk_value)
                if text_key in lookup:
                    row[fk_col] = lookup[text_key]
    return prepared, header_aliases


def _load_document(document_path: Path, table_id: str | None) -> ProvenanceDocument:
    payload = json.loads(document_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if "table_id" in payload and "blocks" in payload:
            return ProvenanceDocument.model_validate(payload)

        doc_payload = payload.get("document")
        if isinstance(doc_payload, dict):
            return ProvenanceDocument.model_validate(doc_payload)

        table_results = payload.get("table_results")
        if isinstance(table_results, dict) and table_results:
            chosen_table_id = table_id
            if not chosen_table_id:
                chosen_table_id = next(iter(table_results.keys()))
            if chosen_table_id not in table_results:
                raise ValueError(
                    f"table_id '{chosen_table_id}' not found in document table_results"
                )
            target = table_results[chosen_table_id]
            if not isinstance(target, dict) or not isinstance(target.get("document"), dict):
                raise ValueError(f"invalid document payload for table '{chosen_table_id}'")
            return ProvenanceDocument.model_validate(target["document"])

    raise ValueError(f"unsupported document json format: {document_path}")


def _load_evidence(evidence_path: Path) -> EvidencePool:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    return EvidencePool.model_validate(payload)


# python -m src.agents.validator \
#   --database-root "data_construction/dataset/BIRD/ours/processed/sales" \
#   --table-id "Products" \
#   --document-json "data_construction/outputs/sales.json" \
#   --enable-soft-check \  

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone validator for generated data_construction documents.")
    parser.add_argument("--database-root", type=str, required=True, help="Database root containing schema.json and tables/")
    parser.add_argument("--table-id", type=str, required=False, default=None, help="Table id to validate (required for multi-table outputs)")
    parser.add_argument("--document-json", type=str, required=True, help="Path to document json (ProvenanceDocument or pipeline output json)")
    parser.add_argument("--evidence-json", type=str, required=False, default=None, help="Path to evidence json (default: <database-root>/tables/evidence/<table-id>.evidence.json)")
    parser.add_argument("--tables-subdir", type=str, default="tables", help="Tables subdir under database root")
    parser.add_argument("--schema-filename", type=str, default="schema.json", help="Schema filename under database root")
    parser.add_argument("--enable-soft-check", action="store_true", help="Enable model-based soft verify+repair")
    parser.add_argument("--output-json", type=str, default=None, help="Optional path to save validation report json")
    args = parser.parse_args()

    db_root = Path(args.database_root)
    document = _load_document(Path(args.document_json), table_id=args.table_id)
    table_id = args.table_id or document.table_id
    if not table_id:
        raise ValueError("Failed to resolve table_id. Please pass --table-id.")

    evidence_path = (
        Path(args.evidence_json)
        if args.evidence_json
        else db_root / args.tables_subdir / "evidence" / f"{table_id}.evidence.json"
    )
    if not evidence_path.exists():
        raise FileNotFoundError(f"evidence json not found: {evidence_path}")

    table = _load_table(
        database_root=db_root,
        table_id=table_id,
        tables_subdir=args.tables_subdir,
        schema_filename=args.schema_filename,
    )
    evidence_pool = _load_evidence(evidence_path)

    app_config = get_default_config()
    synthesis_config = app_config.synthesis
    llm: BaseLLMClient | None = None
    if args.enable_soft_check:
        llm = OpenAILLMClient(config=app_config.llm)
    validator = ValidatorAgent(llm=llm)
    report = validator.run(
        ValidatorInput(
            table=table,
            evidence_pool=evidence_pool,
            document=document,
            config=synthesis_config,
        )
    )

    output_payload = {
        "table_id": table_id,
        "hard_pass": report.hard_pass,
        "soft_pass": report.soft_pass,
        "accepted": report.accepted,
        "coverage_rate": report.coverage_rate,
        "issues": [issue.model_dump() for issue in report.issues],
    }
    output_text = json.dumps(output_payload, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(output_text, encoding="utf-8")
    print(output_text)
