from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .config import OrderingStrategy, SynthesisConfig


class CapabilityLabel(str, Enum):
    # Pillar 1: Intra-Table (Entity)
    TA_FC = "TA-FC"       # 1.1 Format Canonicalization
    TA_US = "TA-US"       # 1.2 Unit Standardization
    TA_EM = "TA-EM"       # 1.3 Enum Mapping

    RI_AC = "RI-AC"       # 2.1 Arithmetic Calculation
    RI_LD = "RI-LD"       # 2.2 Logical Derivation
    RI_TC = "RI-TC"       # 2.3 Temporal Calculation
    RI_MI = "RI-MI"       # 2.4 Multi-hop Inference

    TD_AS = "TD-AS"       # 3.1 Attribute Selection
    TD_DF = "TD-DF"       # 3.2 Distractor Filtering
    TD_CA = "TD-CA"       # 3.3 Conflict Arbitration

    EF_ND = "EF-ND"       # 4.1 Null Detection
    EF_HC = "EF-HC"       # 4.2 Hallucination Check
    EF_RC = "EF-RC"       # 4.3 Redundancy Control

    # Pillar 2: Inter-Table (Relationship)
    SN_ED = "SN-ED"       # 5.1 Entity Decomposition
    SN_TN = "SN-TN"       # 5.2 Term Normalization

    RL_IFK = "RL-IFK"     # 6.1 Implicit Foreign Key
    RL_CK = "RL-CK"       # 6.2 Composite Key
    RL_MI = "RL-MI"       # 6.3 Multi-hop Inference
    RL_O2M = "RL-O2M"     # 6.4 One-to-Many Allocation
    RL_MB = "RL-MB"       # 6.5 Multi-Entity Binding
    RL_CL = "RL-CL"       # 6.6 Conditional Linkage

    IDR_GD = "IDR-GD"     # 7.1 Global Deduplication
    IDR_CR = "IDR-CR"     # 7.2 Coreference Resolution
    IDR_ED = "IDR-ED"     # 7.3 Entity Disambiguation
    IDR_NR = "IDR-NR"     # 7.4 Null Reference Detection

    GR_TI = "GR-TI"       # 8.1 Transitive Inference
    GR_CTA = "GR-CTA"     # 8.2 Cross-Table Aggregation
    GR_DC = "GR-DC"       # 8.3 Dynamic Change

    IC_NR = "IC-NR"       # 9.1 Null Relation Extraction
    # IC_NNP = "IC-NNP"     # 9.2 NotNull Propagation
    IC_ME = "IC-ME"       # 9.3 Mutual Exclusion


class ColumnSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dtype: str
    nullable: bool = True
    description: Optional[str] = None
    range: Optional[str] = Field(
        default=None,
        description="normal range from schema metadata (e.g. Laboratory.csv).",
    )


class TableSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    columns: list[ColumnSchema]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: dict[str, str] = Field(default_factory=dict)
    table_type: str | None = None  # "entity" | "relation", None = entity (default)


class CellRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_index: int
    column_name: str
    value: Any | None = None


class Table(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    schema: TableSchema
    rows: list[dict[str, Any]]
    capability_assignments: dict[str, Any] | None = None
    """Cached assignment supports both flat and nested forms.

    Flat: {row_key: {column_name: [label_code, ...]}}
    Nested(relation): {row_key: {"row": [label_code, ...], "col": {column_name: [label_code, ...]}}}
    """


class RelationalDatabase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_id: str
    tables: list[Table] = Field(default_factory=list)
    source_root: str | None = None
    tables_subdir: str = "tables"


class AnnotatedCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    ref: CellRef
    labels: list[CapabilityLabel] = Field(default_factory=list)
    hide_final_value: bool = False


class CapabilityMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    cells: list[AnnotatedCell] = Field(default_factory=list)


class EvidenceFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment_id: str
    table_id: str
    source_cell: CellRef
    source_labels: list[CapabilityLabel] = Field(default_factory=list)
    text: str
    taboo_final_value: bool = False


class EvidencePool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    fragments: list[EvidenceFragment] = Field(default_factory=list)


class ProfilerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    real_world_document_text: str = Field(min_length=1)
    max_source_chars: int = Field(default=12000, ge=1000)

class TaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment_id: str
    source_cell: CellRef
    text: str


class TaskBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    block_index: int
    items: list[TaskItem] = Field(default_factory=list)


class TaskQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: OrderingStrategy
    blocks: list[TaskBlock] = Field(default_factory=list)


class HistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str = ""
    masked_text: str = ""


class DocumentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    block_index: int
    text: str
    used_fragment_ids: list[str] = Field(default_factory=list)


class ProvenanceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    blocks: list[DocumentBlock] = Field(default_factory=list)
    full_text: str = ""


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    block_id: Optional[str] = None
    fragment_ids: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_pass: bool
    soft_pass: bool
    accepted: bool
    coverage_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: list[ValidationIssue] = Field(default_factory=list)


class LabelingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: Table
    config: SynthesisConfig
    write_back_path: Path | None = None


class RefinerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: Table
    capability_matrix: CapabilityMatrix
    config: SynthesisConfig
    write_back_path: Path | None = None


class SerializerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_pool: EvidencePool
    config: SynthesisConfig


class WriterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: TaskBlock
    history: HistorySummary
    config: SynthesisConfig
    total_blocks: int = Field(default=1, ge=1)


class ValidatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: Table
    evidence_pool: EvidencePool
    document: ProvenanceDocument
    config: SynthesisConfig


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: ProvenanceDocument
    validation: ValidationReport
    attempts: int


class DatabasePipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_id: str
    table_results: dict[str, PipelineResult] = Field(default_factory=dict)
