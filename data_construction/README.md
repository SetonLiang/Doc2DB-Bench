# Data Construction

`data_construction` is the module in `Doc2DB-Bench` responsible for generating document data from structured databases. The current entrypoint is [run_demo.py](./run_demo.py), and the core workflow is orchestrated by [src/pipeline.py](./src/pipeline.py).

This workflow supports two input types:

- single-table JSON
- a single database directory

When the input is a database directory, the code first checks whether the directory has already been preprocessed. If not, it automatically preprocesses the input based on Spider or BIRD directory patterns before entering the document generation pipeline.

## Directory Structure

```text
data_construction/
├── README.md
├── pyproject.toml
├── run_demo.py
├── clean.py
├── ocr.py
├── dataset/
│   ├── spider/
│   ├── BIRD/
│   └── template/
├── OCR/
│   └── DeepSeek-OCR/
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── models.py
│   ├── capabilities.py
│   ├── parameters.py
│   ├── document_styles.py
│   ├── pipeline.py
│   ├── agents/
│   ├── llm/
│   └── utils/
└── tests/
```

## Main Files and Responsibilities

- [run_demo.py](./run_demo.py)  
  Main entrypoint. Reads the default config, preprocesses input when needed, loads the database, constructs the pipeline, and writes output files.

- [src/config.py](./src/config.py)  
  Global default configuration, including input/output paths, model settings, templates, cache, and retry behavior.

- [src/pipeline.py](./src/pipeline.py)  
  Core orchestrator. Invokes the labeling, refiner, serializer, writer, validator, and profiler agents.

- [src/agents/](./src/agents)  
  Implementations for each stage.
  - `labeling.py`: generates the capability matrix
  - `refiner.py`: generates the evidence pool
  - `serializer.py`: splits evidence into writing blocks
  - `writer.py`: generates document blocks
  - `validator.py`: validates generated documents
  - `profiler.py`: generates a reference guide from a reference document

- [src/utils/input_loader.py](./src/utils/input_loader.py)  
  Loads either single-table JSON or a database directory into `Table` / `RelationalDatabase`.

- [src/utils/preprocess_spider.py](./src/utils/preprocess_spider.py)  
  Spider preprocessing. Reads `schema.sql` and sqlite, then generates `schema.json`, `config.py`, and `tables/*.json`.

- [src/utils/preprocess_bird_fast.py](./src/utils/preprocess_bird_fast.py)  
  Fast BIRD preprocessing. Reads sqlite plus structural metadata such as `dev_tables.json` / `train_tables.json`, then generates `schema.json`, `config.py`, and `tables/*.json`.

- [clean.py](./clean.py)  
  Cleans generated markdown files.

- [ocr.py](./ocr.py)  
  OCR utility script, independent from the main document generation pipeline.

## Supported Input Formats

### 1. Single-table Mode

Controlled by `input_mode = "single_table"` in [src/config.py](./src/config.py).

The input file must be a JSON payload accepted by `Table.model_validate(...)`.

Example:

```text
examples/minimal_input.json
```

### 2. Database-directory Mode

Controlled by `input_mode = "database_dir"`.

`database_root` must point to a single database directory, not a parent directory containing multiple databases.

Two states are supported:

- preprocessed directory
- raw Spider / BIRD single-database directory

#### Requirements for a Preprocessed Directory

The directory must contain at least:

```text
<database_root>/
├── config.py
├── schema.json
└── tables/
    ├── TableA.json
    └── TableB.json
```

#### Requirements for a Raw Spider Directory

The directory must contain at least:

```text
<database_root>/
├── schema.sql
└── <db_name>.sqlite
```

The current Spider preprocessing flow does not depend on `dev.json`, `train_spider.json`, or `tables.json`.

If you want the complete Spider dataset rather than preparing a single database directory manually, use the official release:

- Spider: <https://github.com/taoyds/spider>

#### Requirements for a Raw BIRD Directory

The directory must contain at least:

```text
<database_root>/
└── <db_name>.sqlite
```

In addition, the code reads from `dataset/BIRD/{dev|train}/`:

- `dev_tables.json` / `train_tables.json`
- `config.py`

So `database_root` should ideally stay inside the existing BIRD directory layout to ensure split detection and schema lookup work correctly.

If you want the complete BIRD dataset rather than preparing a single database directory manually, use the official release:

- BIRD: <https://bird-bench.github.io>

## Quick Start

### 1. Configure the Input Path

Set `database_root` inside `IOConfig` in [src/config.py](./src/config.py).

For both Spider and BIRD, `database_root` must point to a single database directory rather than a parent folder containing multiple databases.

Spider example:

```python
database_root = "dataset/spider/database/hospital_1"
```

BIRD example:

```python
database_root = "data_construction/dataset/BIRD/dev/dev_databases/financial"
```

### 2. Configure the LLM

Set the following fields in `LLMRuntimeConfig` in [src/config.py](./src/config.py):

- `labeling_model`
- `evidence_model`
- `writing_model`
- `validation_model`
- `base_url`
- `api_key`

### 3. Run `run_demo`

From the repository root:

```bash
python -m data_construction.run_demo
```

## `run_demo` Execution Flow

`run_demo.py` executes in the following order.

### Step 1. Read the Default Configuration

Call [get_default_config()](./src/config.py) to build an `AppConfig`:

- `io`: input and output paths
- `llm`: model and API configuration
- `synthesis`: pipeline parameters
- `concurrency`: reserved concurrency parameters

### Step 2. Initialize the LLM Client and Agents

`run_demo.py` creates:

- `OpenAILLMClient`
- `LabelingAgent`
- `RefinerAgent`
- `SerializerAgent`
- `WriterAgent`
- `ValidatorAgent`
- `ProfilerAgent`

These agents are then injected into `DocumentSynthesisPipeline`.

### Step 3. Determine the Input Mode

If `input_mode == "single_table"`:

- call `load_single_table_json(...)`

If `input_mode == "database_dir"`:

- read `database_root`
- check whether the directory has already been preprocessed

The preprocessing check in [run_demo.py](./run_demo.py) requires:

- `config.py`
- `schema.json`
- `tables/`

### Step 4. Automatic Preprocessing

If the directory has not been preprocessed:

- if the path contains `spider`, call `spider_preprocess(...)`
- if the path contains `bird`, call `preprocess_bird_database_fast(...)`

After preprocessing, the input is loaded uniformly via `load_database_from_directory(...)`.

### Step 5. Run the Pipeline

The entrypoint is [DocumentSynthesisPipeline.run(...)](./src/pipeline.py).

If the input is a database, it enters `run_database()`. If the input is a single table, it enters `run_table()`.

## Pipeline Stages

The current pipeline executes in the following order.

### 1. profiler (optional)

If you want the profiler to build a guide from a real reference document, first prepare a markdown file for `reference_document_path`.

A recommended workflow is to place the reference PDF under:

```text
data_construction/dataset/reference_doc/
```

Then use OCR to convert it into markdown, for example:

```bash
python data_construction/ocr.py single \
  data_construction/dataset/reference_doc/example.pdf \
  --ocr_type deepseek_ocr \
  --output data_construction/dataset/reference_doc/example.md
```

Then set the following in [src/config.py](./src/config.py):

```python
reference_document_path = "data_construction/dataset/reference_doc/example.md"
reference_guide_path = "data_construction/dataset/reference_doc/example.json"
```

If `enable_profiler_alignment=True`, and:

- `reference_document_path` is non-empty
- `reference_guide_path` is non-empty
- `reference_guide_path` does not already contain a usable file

then the pipeline calls `ProfilerAgent` to generate a guide from the reference document and writes it to `reference_guide_path`.

See [src/pipeline.py](./src/pipeline.py).

### 2. labeling

`LabelingAgent.run(...)` reads the table data and outputs a capability matrix.

### 3. refiner

`RefinerAgent.run(...)` generates an evidence pool from the table and capability matrix.

If cache is enabled and the following file already exists:

```text
<database_root>/tables/evidence/<table>.evidence.json
```

the cached result is used and regeneration is skipped.

### 4. serializer

`SerializerAgent.run(...)` converts the evidence pool into a `TaskQueue`, where each task corresponds to one writing block.

### 5. writer

`WriterAgent.run(...)` writes each block in parallel.

- The first pass uses upstream evidence to construct history context.
- Retry passes use generated text from neighboring blocks as context.
- The maximum number of worker threads is `min(8, block_count)`.

### 6. validator

`ValidatorAgent.run(...)` validates the final document.

If validation fails:

- failed blocks are extracted from `issues`
- those blocks are retried within `max_validation_retries`

### 7. Save Markdown

After validation succeeds, markdown is written to:

```text
<database_root>/docs/<table_id>.md
```

If validation still fails after the last retry, the final version is still written to disk.

### 8. Save the Final Result JSON

`save_result_json(...)` merges the current run into the existing output JSON instead of overwriting it.

Output format:

```json
{
  "database_id": "...",
  "table_results": {
    "table_name": {
      "...": "..."
    }
  }
}
```

## Configuration

All configuration is centralized in [src/config.py](./src/config.py).

### `IOConfig`

- `input_mode`: `single_table` or `database_dir`
- `input_path`: input file for single-table mode
- `database_root`: single database directory
- `tables_subdir`: defaults to `tables`
- `schema_filename`: defaults to `schema.json`
- `output_path`: output path for the final result JSON

### `LLMRuntimeConfig`

- `api_key`
- `base_url`
- `timeout_seconds`
- `max_retries`
- `labeling_model`
- `evidence_model`
- `writing_model`
- `validation_model`
- `null_generation_model`

### `BaseParametersConfig`

- `enable_profiler_alignment`
- `reference_document_path`
- `reference_guide_path`
- `document_length_tokens`
- `noise_level`
- `linguistic_complexity`
- `label_ratio`
- `if_primary_allowed`

### `RuntimeConfig`

- `sliding_window_blocks`
- `max_previous_context_chars`
- `max_validation_retries`
- `enable_soft_validation`
- `if_cached`

## Common Scripts

### Main Workflow

```bash
python -m data_construction.run_demo
```

### Validate a Generated Document Directly

```bash
python data_construction/src/agents/validator.py \
  --database-root <db_root> \
  --table-id <table_name> \
  --document-json <document_json>
```

### Clean Markdown Files Under `docs/`

```bash
python data_construction/clean.py
```

### OCR

```bash
python data_construction/ocr.py --help
```

### Preprocessing Utilities

You can either call these modules directly or run the corresponding scripts:

- [src/utils/preprocess_spider.py](./src/utils/preprocess_spider.py)
- [src/utils/preprocess_bird.py](./src/utils/preprocess_bird.py)
- [src/utils/preprocess_bird_fast.py](./src/utils/preprocess_bird_fast.py)
- [src/utils/group_evidence.py](./src/utils/group_evidence.py)
- [src/utils/add_null_relation.py](./src/utils/add_null_relation.py)
