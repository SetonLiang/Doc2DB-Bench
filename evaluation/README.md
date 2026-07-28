# Doc2DB-Bench Evaluation Guide

`evaluation/` is the directory in `Doc2DB-Bench` used for extraction evaluation, baseline comparison, and result aggregation.  
It includes more than `test_dataset_evaluate.py`: it also contains several baseline methods, analysis notebooks, and shared evaluation utilities.

## Directory Structure

```text
evaluation/
├── README.md
├── utils.py
├── test_dataset_evaluate.py
├── llm_evaluate_cascade.py
├── docetl_evaluate.py
├── langchain_evaluate.py
├── langextract_evaluate.py
├── clean_llamaextract_result.py
├── fine_grained_capability_evaluation.ipynb
├── result.ipynb
```

## File Overview

### 1. Main Evaluation Script

- [test_dataset_evaluate.py](./test_dataset_evaluate.py)  
  Main evaluation script. It processes documents, saves `output_tables.json`, and computes structured extraction metrics.

### 2. Extraction Scripts

| Script | Method | Official Link |
|---|---|---|
| [llm_evaluate_cascade.py](./llm_evaluate_cascade.py) | In-repo LLM cascade extraction baseline. Supports `oracle`, `pipeline`, and `pipeline-oracle` modes. | In-repo implementation |
| [docetl_evaluate.py](./docetl_evaluate.py) | Structured extraction and evaluation based on DocETL. | [DocETL GitHub](https://github.com/ucbepic/docetl) |
| [langchain_evaluate.py](./langchain_evaluate.py) | Structured extraction and evaluation based on LangChain + PydanticOutputParser. | [LangChain GitHub](https://github.com/langchain-ai/langchain), [LangChain Website](https://www.langchain.com) |
| [langextract_evaluate.py](./langextract_evaluate.py) | Structured extraction and evaluation based on LangExtract. | [LangExtract GitHub](https://github.com/google/langextract) |

### 3. Result Analysis

- [fine_grained_capability_evaluation.ipynb](./fine_grained_capability_evaluation.ipynb)  
  Notebook for fine-grained capability analysis.

- [result.ipynb](./result.ipynb)  
  Notebook for result organization and analysis.

## Quick Evaluation

A common quick evaluation workflow looks like this:

### 1. Run LLM extraction or another baseline

Choose one extraction method first and generate structured extraction outputs, for example:

```bash
python llm_evaluate_cascade.py --eval-mode pipeline --model gpt-4o
python docetl_evaluate.py --model gpt-4o
python langchain_evaluate.py --model gpt-4o
python langextract_evaluate.py --model gpt-4o
```

The goal of this step is to produce:

- `output_tables.json`
- `extracted_data.json`
- `metadata.json`

### 2. Evaluate with `test_dataset_evaluate.py`

Once extraction outputs are ready, use the main evaluator to compute structured metrics, for example:

```bash
python test_dataset_evaluate.py --evaluate-only
python test_dataset_evaluate.py --evaluate-all-methods
```

This step usually produces:

- `*_evaluate.json`
- optional Excel summary reports

### 3. Use `result.ipynb` for fine-grained analysis

After evaluation finishes, use [result.ipynb](./result.ipynb) to inspect and organize results.

If you need finer capability breakdowns, also use:

- [fine_grained_capability_evaluation.ipynb](./fine_grained_capability_evaluation.ipynb)

## Evaluation Scope and Data Flow

`evaluation/` mainly covers structured extraction evaluation.

### Structured Extraction Evaluation

The goal is to compare extraction quality for the task “documents -> table data”.

Typical inputs:

- `docs/`
- `schema.json`
- `answer.json` or equivalent ground-truth answer directories

Typical outputs:

- `output_tables.json`
- `metadata.json`
- `processing_steps.json`
- `*_evaluate.json`

## Main Evaluation: `test_dataset_evaluate.py`

This is the most complete structured extraction evaluation script in the directory. Its workflow is:

1. Scan the test dataset directory
2. Process documents and generate extraction outputs
3. Extract table data from the results
4. Save `output_tables.json`
5. Compute structured extraction metrics
6. Generate `*_evaluate.json`
7. Optionally aggregate results into Excel reports

### Typical Use Cases

- Evaluate Doc2DB extraction results
- Compare multiple output directories in a unified way
- Generate batch structured metrics and summary reports

### Common Usage

Full processing + evaluation:

```bash
python test_dataset_evaluate.py
python test_dataset_evaluate.py --model gpt-4o
python test_dataset_evaluate.py --model gpt-4o --document-mode multi
python test_dataset_evaluate.py --model gpt-4o --run-name exp_001
```

Evaluate existing outputs only:

```bash
python test_dataset_evaluate.py --model gpt-4o --evaluate-only
python test_dataset_evaluate.py --model gpt-4o --document-mode multi --evaluate-only --run-name evaluation_001
```

Evaluate outputs from all methods:

```bash
python test_dataset_evaluate.py --evaluate-all-methods
python test_dataset_evaluate.py --evaluate-all-methods --force-reevaluate
```

Generate Excel summaries:

```bash
python test_dataset_evaluate.py --generate-excel
python test_dataset_evaluate.py --generate-excel --excel-output ./report.xlsx
```

## Baseline Usage

The other scripts under `evaluation/` are mainly for baseline comparison. They typically read local datasets and documents directly.

### 1. LLM Cascade Baseline

Script: [llm_evaluate_cascade.py](./llm_evaluate_cascade.py)

Supported modes:

- `oracle`: relation-table extraction uses ground-truth context
- `pipeline`: relation-table extraction uses the model’s previously extracted results
- `pipeline-oracle`: entity tables are predicted, relation tables use GT entity references

Examples:

```bash
python llm_evaluate_cascade.py --eval-mode oracle
python llm_evaluate_cascade.py --eval-mode pipeline
python llm_evaluate_cascade.py --eval-mode pipeline-oracle
python llm_evaluate_cascade.py --model gpt-4o --run-name exp_001 --eval-mode oracle
python llm_evaluate_cascade.py --force-rerun --eval-mode pipeline
python llm_evaluate_cascade.py --db-name medical --case-name case2 --model glm-5.1 --eval-mode pipeline-oracle
```

### 2. DocETL Baseline

Script: [docetl_evaluate.py](./docetl_evaluate.py)

Examples:

```bash
python docetl_evaluate.py
python docetl_evaluate.py --model gpt-4o
python docetl_evaluate.py --force-rerun
python docetl_evaluate.py --db-name transportation --case-name case1 --model gpt-5.4
python docetl_evaluate.py --model gpt-4o --run-name baseline_test --force-rerun
```

### 3. LangChain Baseline

Script: [langchain_evaluate.py](./langchain_evaluate.py)

Characteristics:

- Uses `ChatOpenAI`
- Uses `PydanticOutputParser`
- Produces structured JSON directly for downstream evaluation

### 4. LangExtract Baseline

Script: [langextract_evaluate.py](./langextract_evaluate.py)

Examples:

```bash
python langextract_evaluate.py
python langextract_evaluate.py --model gpt-4o
python langextract_evaluate.py --model gpt-5.4 --extraction-passes 1
python langextract_evaluate.py --data-name medical --case-name case2 --model gpt-5.4
python langextract_evaluate.py --model gpt-4o --extraction-passes 1 --run-name baseline_test
```

## Output and Result Files

Different scripts produce slightly different output layouts, but they generally follow a structure organized by method / database / case / model.

Common files include:

- `output_tables.json`  
  Normalized table extraction output. Most downstream evaluation and comparison use this file.

- `extracted_data.json`  
  Raw structured extraction output directly generated by a baseline.

- `metadata.json`  
  Metadata such as model, method, timestamp, and run name.

- `processing_steps.json`  
  Step-level processing information.

- `*_evaluate.json`  
  Per-case evaluation results.

- `*.xlsx`  
  Aggregated summary reports.

### Role of `output_tables.json`

This is the key intermediate format in structured evaluation.  
Whether the source comes from:

- baseline extraction output
- fallback extraction from `full_result`, `snapshot`, or `table_data`

the final result is normalized as much as possible into:

```json
{
  "table_name_1": [
    {"col_a": "value1", "col_b": "value2"}
  ],
  "table_name_2": [
    {"col_x": "value3"}
  ]
}
```

## Evaluation Metrics

### 1. Cell-level Metrics

- `Cell Recall`
- `Cell Precision`
- `Cell F1`

### 2. Structure-level Metrics

- `Table Recall / Precision`
- `Field Recall / Precision`
- `Entity / Relation F1`

### 3. LLM Fine-grained Scoring

Some scripts additionally call an LLM to score output quality. Typical fields include:

- `llm_score`
- `llm_accuracy`
- `llm_completeness`
- `llm_consistency`
- `llm_coverage`
- `llm_comments`

### 4. Notebook Analysis

1. `fine_grained_capability_evaluation.ipynb`

- capability-level error analysis
- case / relation-type breakdown
- output-directory comparison

2. `result.ipynb`

- overall result browsing
- summary organization
- tabular analysis
