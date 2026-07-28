from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field
from .document_styles import DocumentStyle

class OrderingStrategy(str, Enum):
    ROW_MAJOR = "row_major"
    COL_MAJOR = "col_major"
    HARD = "hard"
    RANDOM = "random"


class NullRepresentationMode(str, Enum):
    EXPLICIT_OMISSION = "explicit_omission"
    IMPLICIT_GAP = "implicit_gap"
    CONDITIONAL_NA = "conditional_na"
    TEMPORAL_REVOCATION = "temporal_revocation"


class IOConfig(BaseModel):
    """Input/output paths used by demo runner and batch jobs."""

    model_config = ConfigDict(extra="forbid")

    # single_table | database_dir
    input_mode: str = "database_dir"

    # For single_table mode
    input_path: str = "examples/minimal_input.json"

    # For database_dir mode
    database_root: str = "data_construction/dataset/spider/database/customers_and_products_contacts"
    tables_subdir: str = "tables"
    schema_filename: str = "schema.json"

    # Output
    output_path: str = f"outputs/{database_root.split('/')[-1]}.json"


_DEFAULT_IO_ROOT = IOConfig().database_root


class ConcurrencyConfig(BaseModel):
    """Concurrency knobs for future async/batch execution."""

    model_config = ConfigDict(extra="forbid")

    max_parallel_task: int = 100
    max_concurrent_requests: int = 1000


class LLMRuntimeConfig(BaseModel):
    """LLM client/runtime settings (kept for compatibility with DTBench-style config)."""

    model_config = ConfigDict(extra="forbid")

    validation_api_key: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout_seconds: int = 600
    max_retries: int = 3

    # labeling_model: str = "gemini-2.5-pro"
    evidence_model: str = "gemini-2.5-pro"
    writing_model: str = "gemini-2.5-pro"
    validation_model: str = "gemini-2.5-pro"
    
    null_generation_model: str = "gpt-4o"



class BaseParametersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # If False, writer falls back to LEGACY_WRITER_BLOCK_PROMPT.
    use_injected_parameters: bool = True
    # If True and profiler agent exists, pipeline generates/updates reference guide file.
    enable_profiler_alignment: bool = True
    # Shared path: profiler writes reference guide here, writer reads from here.
    reference_document_path: str = _DEFAULT_IO_ROOT + "/template/template.md"
    # reference_document_path: str = "data_construction/dataset/template/university/sampled_pages.mmd"
    reference_guide_path: str = _DEFAULT_IO_ROOT + "/template/template.json"
    # reference_guide_path: str = ""

    document_length_tokens: int = Field(default=10240, ge=128)
    document_style: DocumentStyle | DocumentStyle = None
    section_templates: list[str] = Field(default_factory=list)
    noise_level: float = Field(default=0.5, ge=0.0, le=1.0)
    linguistic_complexity: float = Field(default=0.5, ge=0.0, le=1.0)
    # Optional prompt protocol overrides for WriterAgent.
    # - None: use auto-generated protocol text (backward compatible)
    # - "": pass empty protocol to prompt
    # - non-empty string: pass custom protocol text verbatim
    hard_cases_protocol: str | None = ""
    noise_protocol: str | None = None
    complexity_protocol: str | None = ""

    label_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    if_primary_allowed: bool = Field(default=False)


class HardCasesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_entity_disambiguation: bool = True
    disambiguation_alias_pool: list[str] = Field(
        default_factory=lambda: ["he", "she", "it", "the stakeholder", "the asset"]
    )
    enable_temporal_evolution: bool = True
    temporal_state_machine_arc: list[str] = Field(
        default_factory=lambda: ["initial_assertion", "intermediate_modification", "final_revocation"]
    )


class QualityDimensionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    null_modes: list[NullRepresentationMode] = Field(default_factory=list)
    explicit_omissions_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    implicit_gaps_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    conditional_na_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    temporal_revocation_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    deduplication_challenge_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    relationship_mapping_fidelity: float = Field(default=1.0, ge=0.0, le=1.0)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordering_strategy: OrderingStrategy = OrderingStrategy.HARD
    chunk_size: int = Field(default=16, ge=1)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sliding_window_blocks: int = Field(default=2, ge=1)
    max_previous_context_chars: int = Field(default=2000, ge=256)
    max_validation_retries: int = Field(default=2, ge=0)
    # 若为 False，则仅执行 hard 校验，不触发 LLM soft 校验
    enable_soft_validation: bool = Field(default=False)
    # 若为 True，则 write_back_path 不为空，可写入/读取 evidence 缓存并跳过已处理
    if_cached: bool = Field(default=True)


class SynthesisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: BaseParametersConfig = BaseParametersConfig()
    hard_cases: HardCasesConfig = HardCasesConfig()
    quality: QualityDimensionConfig = QualityDimensionConfig()
    strategy: StrategyConfig = StrategyConfig()
    runtime: RuntimeConfig = RuntimeConfig()


class AppConfig(BaseModel):
    """Single entrypoint config that bundles all runtime and synthesis settings."""

    model_config = ConfigDict(extra="forbid")

    io: IOConfig = IOConfig()
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    llm: LLMRuntimeConfig = LLMRuntimeConfig()
    synthesis: SynthesisConfig = SynthesisConfig()


def get_default_config() -> AppConfig:
    """Return default app config instance."""
    return AppConfig()
