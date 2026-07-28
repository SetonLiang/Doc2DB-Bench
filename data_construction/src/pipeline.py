from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from .agents.labeling import LabelingAgent
from .agents.profiler import ProfilerAgent
from .agents.refiner import RefinerAgent
from .agents.serializer import SerializerAgent
from .agents.writer import WriterAgent
from .agents.validator import ValidatorAgent
from .config import SynthesisConfig
from .models import (
    CapabilityMatrix,
    DatabasePipelineResult,
    DocumentBlock,
    EvidencePool,
    HistorySummary,
    LabelingInput,
    PipelineResult,
    ProvenanceDocument,
    RelationalDatabase,
    RefinerInput,
    SerializerInput,
    Table,
    TaskQueue,
    ValidationReport,
    ValidatorInput,
    WriterInput,
    ProfilerInput,
)
from .utils.add_null_relation import add_null_relations
from .utils.utils import infer_fk_relation_types_from_config

class PipelineState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_matrix: CapabilityMatrix | None = None
    evidence_pool: EvidencePool | None = None
    task_queue: TaskQueue | None = None
    generated_blocks: list[DocumentBlock] = Field(default_factory=list)


class DocumentSynthesisPipeline:
    def __init__(
        self,
        config: SynthesisConfig,
        labeling_agent: LabelingAgent,
        refiner_agent: RefinerAgent,
        serializer_agent: SerializerAgent,
        writer_agent: WriterAgent,
        validator_agent: ValidatorAgent,
        profiler_agent: ProfilerAgent | None = None,
    ) -> None:
        self.config = config
        self.labeling_agent = labeling_agent
        self.refiner_agent = refiner_agent
        self.serializer_agent = serializer_agent
        self.writer_agent = writer_agent
        self.validator_agent = validator_agent
        self.profiler_agent = profiler_agent
        self._reference_guide_ready = False

    def run(
        self, input_data: Table | RelationalDatabase
    ) -> PipelineResult | DatabasePipelineResult:
        """Run synthesis for one table or all tables in a relational database."""
        if isinstance(input_data, Table):
            return self.run_table(input_data)
        return self.run_database(input_data)

    def run_database(self, database: RelationalDatabase) -> DatabasePipelineResult:
        """Run synthesis table-by-table for an entire database."""
        self._maybe_prepare_reference_guide()
        results: dict[str, PipelineResult] = {}
        write_back_root = Path(database.source_root) if database.source_root else None
        if hasattr(self.writer_agent, "set_database_root"):
            self.writer_agent.set_database_root(database.source_root)
        tables_subdir = database.tables_subdir
        dataset_config = self._load_dataset_config(database.source_root)
        anchor_map = self._build_anchor_map(dataset_config)
        tables_by_name = {table.table_id: table for table in database.tables}
        for table in database.tables:
            print(table.table_id)
            if table.table_id != "Order_Items":  
                continue
            write_back_path = None
            if write_back_root and self.config.runtime.if_cached:
                write_back_path = write_back_root / tables_subdir / f"{table.table_id}.json"
            markdown_path = self._markdown_path_for_table(table.table_id, write_back_path)
            if markdown_path.exists():
                continue
            results[table.table_id] = self.run_table(
                table,
                write_back_path=write_back_path,
                tables_by_name=tables_by_name,
                anchor_map=anchor_map,
            )
            # break
        return DatabasePipelineResult(database_id=database.database_id, table_results=results)

    def run_table(
        self,
        table: Table,
        write_back_path: Path | None = None,
        tables_by_name: dict[str, Table] | None = None,
        anchor_map: dict[str, list[str]] | None = None,
    ) -> PipelineResult:
        """
        Execute 5-step reverse synthesis:
        1) labeling -> 2) evidence -> 3) serialization -> 4) block writing -> 5) validation with retries.
        """
        self._maybe_prepare_reference_guide()
        if write_back_path is not None:
            self.writer_agent.set_database_root(str(write_back_path.parent.parent))

            try:
                # 1. 提取 db_config 和当前所有的 table schema 构造字典
                db_config = self._load_dataset_config(str(write_back_path.parent.parent)) or {}
                schema_dict = {"tables": [], "columns": {}, "primary_keys": {}, "foreign_keys": {}}
                
                source_tables = tables_by_name if tables_by_name else {table.table_id: table}
                for t_name, t_obj in source_tables.items():
                    schema_dict["tables"].append(t_name)
                    cols = t_obj.rows[0].keys() if t_obj.rows else []
                    schema_dict["columns"][t_name] = [{"name": c} for c in cols]
                    schema_dict["primary_keys"][t_name] = t_obj.schema.primary_key
                    schema_dict["foreign_keys"][t_name] = t_obj.schema.foreign_keys

                # 2. 调用关系推断
                relation_types_map = infer_fk_relation_types_from_config(db_config, schema_dict)
                
                meta_path = write_back_path.parent.parent / "meta.json"
                meta_content = {
                    "if_primary_allowed": {},
                    "type": {}
                }
                
                if meta_path.exists():
                    try:
                        loaded_content = json.loads(meta_path.read_text(encoding="utf-8"))
                        if isinstance(loaded_content, dict):
                            meta_content["if_primary_allowed"] = loaded_content.get("if_primary_allowed", {})
                            meta_content["type"] = loaded_content.get("type", {})
                    except (OSError, json.JSONDecodeError):
                        pass
                
                table_name = table.table_id
                
                # 若主键不为空则认为 primary_allowed (此处依据逻辑可自定义)
                if_primary_allowed = len(table.schema.primary_key) > 0
                
                # 在 relation_types_map 查找当前表相关的 type
                relation_type = "unknown"
                for rel_key, r_type in relation_types_map.items():
                    if table_name in rel_key:
                        relation_type = r_type
                        break
                        
                meta_content["if_primary_allowed"][table_name] = if_primary_allowed
                meta_content["type"][table_name] = relation_type 
                
                meta_path.write_text(json.dumps(meta_content, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"[Warning] Failed to update meta.json for {table.table_id}: {e}")

        # return
        state = PipelineState()

        state.capability_matrix = self.labeling_agent.run(
            LabelingInput(
                table=table,
                config=self.config,
                write_back_path=write_back_path,
            )
        )

        # labeling 完成后再注入 IC_RI 行，避免新增 assignments 影响本轮 labeling
        # is_relation = (table.schema.table_type or "").strip().lower() == "relation"
        # if is_relation and write_back_path is not None and write_back_path.exists():
        #     try:
        #         added_ids = add_null_relations(
        #             json_path=write_back_path,
        #             count=3,
        #             seed=42,
        #             use_llm=False,
        #         )
        #         payload = json.loads(write_back_path.read_text(encoding="utf-8"))
        #         if isinstance(payload, dict):
        #             rows = payload.get("rows")
        #             assignments = payload.get("capability_assignments")
        #             if isinstance(rows, list):
        #                 table = table.model_copy(update={"rows": rows})
        #             if isinstance(assignments, dict):
        #                 table = table.model_copy(update={"capability_assignments": assignments})
        #             if tables_by_name is not None:
        #                 tables_by_name[table.table_id] = table
        #         print(f"[add_null_relation] {table.table_id}: added {len(added_ids)} ids -> {added_ids}")
        #     except Exception as exc:
        #         print(f"[add_null_relation] {table.table_id}: skipped, reason={exc}")
        # exit()

        refiner_table = self._prepare_table_for_refiner(
            table=table,
            tables_by_name=tables_by_name or {table.table_id: table},
            anchor_map=anchor_map or {},
        )
        cached_evidence_pool = self._load_cached_evidence_pool(write_back_path)
        print(write_back_path)
        # print(cached_evidence_pool)
        # exit()
        if cached_evidence_pool is not None:
            state.evidence_pool = cached_evidence_pool
        else:
            state.evidence_pool = self.refiner_agent.run(
                RefinerInput(
                    table=refiner_table,
                    capability_matrix=state.capability_matrix,
                    config=self.config,
                    write_back_path=write_back_path,
                )
            )
        # exit()

        state.task_queue = self.serializer_agent.run(
            SerializerInput(
                evidence_pool=state.evidence_pool,
                config=self.config,
            )
        )
        # print(state.task_queue)
        # exit()

        attempt = 0
        last_report = ValidationReport(
            hard_pass=False,
            soft_pass=False,
            accepted=False,
            coverage_rate=0.0,
            issues=[],
        )

        while attempt <= self.config.runtime.max_validation_retries:
            total_blocks = len(state.task_queue.blocks)
            if len(state.generated_blocks) != total_blocks:
                state.generated_blocks = [None] * total_blocks
            retry_block_ids = {
                issue.block_id
                for issue in last_report.issues
                if issue.block_id
            }
            if attempt == 0 or not retry_block_ids:
                target_indices = list(range(total_blocks))
            else:
                target_indices = [
                    idx
                    for idx, block in enumerate(state.task_queue.blocks)
                    if block.block_id in retry_block_ids
                ]
                if not target_indices:
                    target_indices = list(range(total_blocks))

            if attempt == 0:
                histories = self._build_parallel_histories(state.task_queue)
            else:
                histories = self._build_retry_histories_with_neighbors(
                    task_queue=state.task_queue,
                    generated_blocks=state.generated_blocks,
                )
            max_workers = min(8, max(1, len(target_indices)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for i in target_indices:
                    block = state.task_queue.blocks[i]
                    print(
                        f"  - Processing Block {i + 1}/{total_blocks}: "
                        f"{block.block_id} ({len(block.items)} items)..."
                    )
                    future = executor.submit(
                        self.writer_agent.run,
                        WriterInput(
                            block=block,
                            history=histories[i],
                            config=self.config,
                            total_blocks=total_blocks,
                        ),
                    )
                    futures[future] = i

                for future in as_completed(futures):
                    idx = futures[future]
                    state.generated_blocks[idx] = future.result()

            ready_blocks = [b for b in state.generated_blocks if isinstance(b, DocumentBlock)]
            document = self._assemble_document(table.table_id, ready_blocks)

            # print(document)
            last_report = self.validator_agent.run(
                ValidatorInput(
                    table=table,
                    evidence_pool=state.evidence_pool,
                    document=document,
                    config=self.config,
                )
            )
            
            if last_report.accepted:
                self._save_markdown_document(
                    table_id=table.table_id,
                    document=document,
                    write_back_path=write_back_path,
                )
                return PipelineResult(document=document, validation=last_report, attempts=attempt + 1)
            # self._save_markdown_document(
            #     table_id=table.table_id,
            #     document=document,
            #     write_back_path=write_back_path,
            # )
            # return PipelineResult(document=document, validation=last_report, attempts=attempt + 1)

            attempt += 1

        final_blocks = [b for b in state.generated_blocks if isinstance(b, DocumentBlock)]
        last_document = self._assemble_document(table.table_id, final_blocks)
        self._save_markdown_document(
            table_id=table.table_id,
            document=last_document,
            write_back_path=write_back_path,
        )
        return PipelineResult(document=last_document, validation=last_report, attempts=attempt)

    def _maybe_prepare_reference_guide(self) -> None:
        if self._reference_guide_ready:
            return

        base = self.config.base
        self._reference_guide_ready = True
        if not base.enable_profiler_alignment:
            return
        if self.profiler_agent is None:
            return
        doc_path_text = base.reference_document_path.strip()
        if not doc_path_text:
            return
        guide_path_text = base.reference_guide_path.strip()
        if not guide_path_text:
            return
        guide_path = Path(guide_path_text)
        if guide_path.exists():
            existing = guide_path.read_text(encoding="utf-8").strip()
            if existing:
                return

        doc_path = Path(doc_path_text)
        if not doc_path.exists():
            return
        source_text = doc_path.read_text(encoding="utf-8").strip()
        if not source_text:
            return

        guide = self.profiler_agent.run(
            ProfilerInput(
                real_world_document_text=source_text,
            )
        )
        guide_path.parent.mkdir(parents=True, exist_ok=True)
        guide_path.write_text(
            json.dumps(guide.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_history_summary(self, previous_blocks: list[DocumentBlock]) -> HistorySummary:
        """Build sliding-window history summary."""
        window_size = self.config.runtime.sliding_window_blocks
        context_blocks = previous_blocks[-window_size:]
        raw_text = "\n\n".join(block.text for block in context_blocks)
        return HistorySummary(raw_text=raw_text, masked_text=raw_text)

    def _build_parallel_histories(self, task_queue: TaskQueue) -> list[HistorySummary]:
        """Precompute per-block history from upstream evidence blocks for parallel writing."""
        window_size = self.config.runtime.sliding_window_blocks
        histories: list[HistorySummary] = []
        seed_blocks: list[str] = []
        for block in task_queue.blocks:
            seed_blocks.append("\n".join(item.text for item in block.items))

        for i in range(len(task_queue.blocks)):
            start = max(0, i - window_size)
            raw_text = "\n\n".join(seed_blocks[start:i])
            histories.append(HistorySummary(raw_text=raw_text, masked_text=raw_text))
        return histories

    def _build_retry_histories_with_neighbors(
        self,
        task_queue: TaskQueue,
        generated_blocks: list[DocumentBlock | None],
    ) -> list[HistorySummary]:
        """Build retry histories from generated text, including both previous and next neighbors."""
        window_size = self.config.runtime.sliding_window_blocks
        block_texts: list[str] = []
        for idx, block in enumerate(task_queue.blocks):
            generated = generated_blocks[idx] if idx < len(generated_blocks) else None
            if isinstance(generated, DocumentBlock):
                block_texts.append(generated.text)
            else:
                block_texts.append("\n".join(item.text for item in block.items))

        histories: list[HistorySummary] = []
        total = len(task_queue.blocks)
        for i in range(total):
            prev_start = max(0, i - window_size)
            next_end = min(total, i + 1 + window_size)
            prev_text = "\n\n".join(block_texts[prev_start:i]).strip()
            next_text = "\n\n".join(block_texts[i + 1 : next_end]).strip()

            if prev_text and next_text:
                raw_text = f"[Previous Blocks]\n{prev_text}\n\n[Next Blocks]\n{next_text}"
            elif prev_text:
                raw_text = prev_text
            elif next_text:
                raw_text = f"[Next Blocks]\n{next_text}"
            else:
                raw_text = ""
            histories.append(HistorySummary(raw_text=raw_text, masked_text=raw_text))
        return histories

    def _assemble_document(self, table_id: str, blocks: list[DocumentBlock]) -> ProvenanceDocument:
        """Merge block texts into final provenance-enhanced document."""
        full_text = "\n\n".join(block.text for block in blocks)
        return ProvenanceDocument(table_id=table_id, blocks=blocks, full_text=full_text)

    def _markdown_path_for_table(
        self, table_id: str, write_back_path: Path | None
    ) -> Path:
        """Return the markdown path for a table."""
        if write_back_path is not None:
            markdown_dir = write_back_path.parent.parent / "docs"
        else:
            markdown_dir = Path("outputs") / "docs"
        return markdown_dir / f"{table_id}.md"

    def _save_markdown_document(
        self,
        table_id: str,
        document: ProvenanceDocument,
        write_back_path: Path | None,
    ) -> None:
        """Persist final document text as a markdown file under docs directory."""
        markdown_path = self._markdown_path_for_table(table_id, write_back_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(document.full_text, encoding="utf-8")

    def _load_dataset_config(self, source_root: str | None) -> dict[str, Any] | None:
        if not source_root:
            return None
        config_path = Path(source_root) / "config.py"
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
        return config if isinstance(config, dict) else None

    def _dict_to_ns(self, obj):
        if isinstance(obj, dict):
            return SimpleNamespace(**{k: self._dict_to_ns(v) for k, v in obj.items()})
        elif isinstance(obj, list):
            return [self._dict_to_ns(i) for i in obj]
        return obj
    
    def _load_cached_evidence_pool(self, write_back_path: Path | None) -> EvidencePool | None:
        if write_back_path is None or not self.config.runtime.if_cached:
            return None
        evidence_path = self._derive_evidence_output_path(write_back_path)
        print(evidence_path)
        if not evidence_path.exists():
            return None
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            # print(payload)
            return EvidencePool.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        
    
    def _derive_evidence_output_path(self, write_back_path: Path) -> Path:
        suffixes = "".join(write_back_path.suffixes)
        if suffixes:
            base_name = write_back_path.name[: -len(suffixes)]
            filename = f"{base_name}.evidence{suffixes}"
        else:
            filename = f"{write_back_path.name}.evidence.json"
        return write_back_path.parent / "evidence" / filename

    def _build_anchor_map(self, dataset_config: dict[str, Any] | None) -> dict[str, list[str]]:
        if not dataset_config:
            return {}

        anchor_cols = dataset_config.get("anchor_cols")
        if not isinstance(anchor_cols, list) or not anchor_cols:
            return {}
        first = anchor_cols[0]
        if not isinstance(first, dict):
            return {}

        anchor_map: dict[str, list[str]] = {}
        for table_name, cols in first.items():
            if not isinstance(table_name, str):
                continue
            if isinstance(cols, list):
                normalized = [str(col) for col in cols if isinstance(col, str) and col.strip()]
            elif isinstance(cols, str) and cols.strip():
                normalized = [cols]
            else:
                normalized = []
            if normalized:
                anchor_map[table_name] = normalized
        return anchor_map

    def _lookup_anchor_cols(self, anchor_map: dict[str, list[str]], table_name: str) -> list[str]:
        """Case-insensitive anchor column lookup for table names from schema/config."""
        if table_name in anchor_map:
            return anchor_map[table_name]
        lower_map = {k.lower(): v for k, v in anchor_map.items() if isinstance(k, str)}
        return lower_map.get(table_name.lower(), [])

    def _prepare_table_for_refiner(
        self,
        table: Table,
        tables_by_name: dict[str, Table],
        anchor_map: dict[str, list[str]],
    ) -> Table:
        prepared = table.model_copy(deep=True)
        original_rows = [dict(row) for row in table.rows]
        original_primary_key = list(table.schema.primary_key)

        if prepared.rows:
            row_columns = set(prepared.rows[0].keys())
            anchor_cols = [
                col for col in self._lookup_anchor_cols(anchor_map, prepared.table_id) if col in row_columns
            ]
            if anchor_cols:
                # Keep original PKs for exclusion while appending anchor columns for display context.
                merged_keys: list[str] = []
                for col in [*prepared.schema.primary_key, *anchor_cols]:
                    if col not in merged_keys:
                        merged_keys.append(col)
                prepared.schema.primary_key = merged_keys

        is_relation = (prepared.schema.table_type or "").strip().lower() == "relation"
        if not is_relation:
            return prepared
        if not prepared.rows:
            return prepared

        for fk_col, fk_target in prepared.schema.foreign_keys.items():
            if fk_col not in prepared.rows[0]:
                continue
            target_table_name, target_col = self._parse_fk_target(fk_target)
            if not target_table_name or not target_col:
                continue

            target_table = tables_by_name.get(target_table_name)
            if target_table is None or not target_table.rows:
                continue

            target_row_cols = set(target_table.rows[0].keys())
            target_anchor_cols = [
                col for col in self._lookup_anchor_cols(anchor_map, target_table_name) if col in target_row_cols
            ]

            lookup = self._build_multihop_fk_anchor_lookup(
                table_name=target_table_name,
                key_col=target_col,
                tables_by_name=tables_by_name,
                anchor_map=anchor_map,
                fallback_anchor_cols=target_anchor_cols or [target_col],
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

        assignments = prepared.capability_assignments
        if isinstance(assignments, dict) and assignments:
            remapped_assignments: dict[str, Any] = {}
            consumed_keys: set[str] = set()
            for idx, prepared_row in enumerate(prepared.rows):
                original_row = original_rows[idx] if idx < len(original_rows) else {}
                payload, matched_key = self._resolve_assignment_payload(
                    assignments=assignments,
                    row=original_row,
                    primary_key=original_primary_key,
                    row_index=idx,
                )
                if payload is None:
                    continue
                if matched_key is not None:
                    consumed_keys.add(matched_key)

                for new_key in self._assignment_key_candidates(
                    row=prepared_row,
                    primary_key=prepared.schema.primary_key,
                    row_index=idx,
                ):
                    remapped_assignments[new_key] = payload

            # Keep any untouched assignment entries to avoid accidental strategy loss.
            for key, payload in assignments.items():
                if key in consumed_keys or key in remapped_assignments:
                    continue
                remapped_assignments[key] = payload

            if remapped_assignments:
                prepared.capability_assignments = remapped_assignments

        return prepared

    def _build_assignment_row_key(
        self,
        row: dict[str, Any],
        primary_key: list[str],
        row_index: int,
    ) -> str:
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
        composite = self._build_assignment_row_key(row=row, primary_key=primary_key, row_index=row_index)
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

    def _resolve_assignment_payload(
        self,
        assignments: dict[str, Any],
        row: dict[str, Any],
        primary_key: list[str],
        row_index: int,
    ) -> tuple[Any | None, str | None]:
        for key in self._assignment_key_candidates(row=row, primary_key=primary_key, row_index=row_index):
            if key in assignments:
                return assignments.get(key), key
        return None, None

    def _parse_fk_target(self, fk_target: str) -> tuple[str | None, str | None]:
        if "." not in fk_target:
            return None, None
        table_name, col_name = fk_target.split(".", 1)
        table_name = table_name.strip()
        col_name = col_name.strip()
        if not table_name or not col_name:
            return None, None
        return table_name, col_name

    def _build_fk_anchor_lookup(
        self,
        target_rows: list[dict[str, Any]],
        target_col: str,
        anchor_cols: list[str],
        target_table_name: str | None = None,
    ) -> dict[Any, str]:
        lookup: dict[Any, str] = {}
        for target_row in target_rows:
            key = target_row.get(target_col)
            if key in (None, ""):
                continue
            pairs = []
            for col in anchor_cols:
                raw_value = target_row.get(col, "")
                value = str(raw_value).strip()
                if not value:
                    continue
                if target_table_name:
                    pairs.append(f"{target_table_name}.{col}={value}")
                else:
                    pairs.append(f"{col}={value}")
            if not pairs:
                continue
            # Keep per-anchor-field values separated for downstream prompting readability.
            anchor_text = " | ".join(pairs)
            lookup[key] = anchor_text
            lookup[str(key)] = anchor_text
        return lookup

    def _build_multihop_fk_anchor_lookup(
        self,
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
            pairs = self._resolve_anchor_pairs_for_row(
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

    def _resolve_anchor_pairs_for_row(
        self,
        table: Table,
        row: dict[str, Any],
        tables_by_name: dict[str, Table],
        anchor_map: dict[str, list[str]],
        fallback_key_col: str,
        fallback_anchor_cols: list[str],
        visited: set[tuple[str, str]],
        depth: int,
    ) -> list[str]:
        if depth <= 0:
            return []
        table_key = table.table_id.lower()
        row_key = self._build_assignment_row_key(row, table.schema.primary_key, 0)
        visit_token = (table_key, row_key)
        if visit_token in visited:
            return []
        visited = set(visited)
        visited.add(visit_token)

        # 1) Only config-listed anchor columns count as "local" anchors. Do not merge
        # fallback_anchor_cols here — otherwise intermediate tables (e.g. account) would
        # emit account_id and never traverse order→account→district style multi-hop FKs.
        explicit_cols = self._lookup_anchor_cols(anchor_map, table.table_id)
        pairs: list[str] = []
        for col in explicit_cols:
            raw_value = row.get(col, "")
            value = str(raw_value).strip()
            if not value:
                continue
            pairs.append(f"{table.table_id}.{col}={value}")
        if pairs:
            return self._dedupe_keep_order(pairs)

        # 2) Follow schema foreign keys for any table type (entity or relation) so paths like
        # order→account→district work even when intermediate tables are marked "entity".
        if table.schema.foreign_keys:
            aggregated: list[str] = []
            for fk_col, fk_target in table.schema.foreign_keys.items():
                fk_value = row.get(fk_col)
                if fk_value in (None, ""):
                    continue
                child_table_name, child_key_col = self._parse_fk_target(fk_target)
                if not child_table_name or not child_key_col:
                    continue
                child_table = tables_by_name.get(child_table_name)
                if child_table is None or not child_table.rows:
                    continue
                child_row = self._find_row_by_key(child_table.rows, child_key_col, fk_value)
                if child_row is None:
                    continue
                child_pairs = self._resolve_anchor_pairs_for_row(
                    table=child_table,
                    row=child_row,
                    tables_by_name=tables_by_name,
                    anchor_map=anchor_map,
                    fallback_key_col=child_key_col,
                    fallback_anchor_cols=self._lookup_anchor_cols(anchor_map, child_table_name) or [child_key_col],
                    visited=visited,
                    depth=depth - 1,
                )
                aggregated.extend(child_pairs)
            if aggregated:
                return self._dedupe_keep_order(aggregated)

        # 3) Fallback: encode non-anchor identifiers when FK expansion failed.
        for col in fallback_anchor_cols:
            raw_value = row.get(col, "")
            value = str(raw_value).strip()
            if not value:
                continue
            pairs.append(f"{table.table_id}.{col}={value}")
        if pairs:
            return self._dedupe_keep_order(pairs)

        fallback_value = row.get(fallback_key_col, "")
        if fallback_value in (None, ""):
            return []
        return [f"{table.table_id}.{fallback_key_col}={fallback_value}"]

    def _find_row_by_key(
        self,
        rows: list[dict[str, Any]],
        key_col: str,
        key_value: Any,
    ) -> dict[str, Any] | None:
        key_text = str(key_value).strip()
        for row in rows:
            value = row.get(key_col)
            if value == key_value:
                return row
            if str(value).strip() == key_text:
                return row
        return None

    def _dedupe_keep_order(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def save_result_json(
        self,
        output_path: Path,
        result: PipelineResult | DatabasePipelineResult,
    ) -> dict[str, Any]:
        """Merge current result into existing output json instead of overwriting it."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        existing_payload: dict[str, Any] = {}
        if output_path.exists():
            try:
                parsed = json.loads(output_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing_payload = parsed
            except json.JSONDecodeError:
                existing_payload = {}

        merged_table_results: dict[str, Any] = {}
        existing_table_results = existing_payload.get("table_results")
        if isinstance(existing_table_results, dict):
            merged_table_results.update(existing_table_results)

        if isinstance(result, DatabasePipelineResult):
            for table_id, table_result in result.table_results.items():
                merged_table_results[table_id] = table_result.model_dump(mode="json")
            database_id = result.database_id
        else:
            table_id = result.document.table_id
            merged_table_results[table_id] = result.model_dump(mode="json")
            database_id = (
                existing_payload.get("database_id")
                if isinstance(existing_payload.get("database_id"), str)
                else "single_table"
            )

        merged_payload = {
            "database_id": database_id,
            "table_results": merged_table_results,
        }
        output_path.write_text(
            json.dumps(merged_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return merged_payload
