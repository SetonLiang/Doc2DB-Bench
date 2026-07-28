# Doc2DB-Bench Evaluation 使用说明

`evaluation/` 是 `Doc2DB-Bench` 中负责抽取评测、baseline 对比和结果汇总的目录。  
它不只包含 `test_dataset_evaluate.py`，还包括多个基线方法、结果分析 notebook，以及公共评估工具。

## 目录结构

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

## 各文件作用

### 1. 主评测脚本

- [test_dataset_evaluate.py](./test_dataset_evaluate.py)  
  主评测脚本。负责处理文档、保存 `output_tables.json`，并计算结构化抽取指标。

### 2. 抽取脚本

| Script | Method | Official Link |
|---|---|---|
| [llm_evaluate_cascade.py](./llm_evaluate_cascade.py) | In-repo LLM cascade extraction baseline. Supports `oracle`, `pipeline`, and `pipeline-oracle` modes. | In-repo implementation |
| [docetl_evaluate.py](./docetl_evaluate.py) | Structured extraction and evaluation based on DocETL. | [DocETL GitHub](https://github.com/ucbepic/docetl) |
| [langchain_evaluate.py](./langchain_evaluate.py) | Structured extraction and evaluation based on LangChain + PydanticOutputParser. | [LangChain GitHub](https://github.com/langchain-ai/langchain), [LangChain Website](https://www.langchain.com) |
| [langextract_evaluate.py](./langextract_evaluate.py) | Structured extraction and evaluation based on LangExtract. | [LangExtract GitHub](https://github.com/google/langextract) |


### 3. 结果分析

- [fine_grained_capability_evaluation.ipynb](./fine_grained_capability_evaluation.ipynb)  
  细粒度能力分析 notebook。

- [result.ipynb](./result.ipynb)  
  结果整理和分析 notebook。



## 快速评估

一个常见的快速评估流程如下：

### 1. 调用 LLM extraction / 其他 baseline 抽取

先选择一种抽取方法，生成结构化抽取结果，例如：

```bash
python llm_evaluate_cascade.py --eval-mode pipeline --model gpt-4o
python docetl_evaluate.py --model gpt-4o
python langchain_evaluate.py --model gpt-4o
python langextract_evaluate.py --model gpt-4o
```

这一步的目标是得到：

- `output_tables.json`
- `extracted_data.json`
- `metadata.json`

### 2. 用 `test_dataset_evaluate.py` 评估

当抽取结果已经生成后，用主评测脚本统一计算结构化指标，例如：

```bash
python test_dataset_evaluate.py --evaluate-only
python test_dataset_evaluate.py --evaluate-all-methods
```

这一步通常会产出：

- `*_evaluate.json`
- 可选的 Excel 汇总结果

### 3. 用 `result.ipynb` 得到细粒度分析结果

评测结束后，使用 [result.ipynb](./result.ipynb) 查看和整理结果。

如果需要更细的能力拆解，再使用：

- [fine_grained_capability_evaluation.ipynb](./fine_grained_capability_evaluation.ipynb)


## 评测对象与数据流

`evaluation/` 主要覆盖两类任务：

### 1. 结构化抽取评测

结构化抽取评测脚本。它的流程是：

1. 扫描测试集目录
2. 处理文档并生成抽取结果
3. 从结果中提取表数据
4. 保存 `output_tables.json`
5. 计算结构化抽取指标
6. 生成 `*_evaluate.json`
7. 可选汇总为 Excel 报告

### 适用场景

- 评测 Doc2DB 抽取结果
- 统一对比多个输出目录
- 批量生成结构化指标和汇总报告

### 常见用法

完整处理 + 评估：

```bash
python test_dataset_evaluate.py
python test_dataset_evaluate.py --model gpt-4o
python test_dataset_evaluate.py --model gpt-4o --document-mode multi
python test_dataset_evaluate.py --model gpt-4o --run-name exp_001
```

只评估已有输出：

```bash
python test_dataset_evaluate.py --model gpt-4o --evaluate-only
python test_dataset_evaluate.py --model gpt-4o --document-mode multi --evaluate-only --run-name evaluation_001
```

评估所有方法输出：

```bash
python test_dataset_evaluate.py --evaluate-all-methods
python test_dataset_evaluate.py --evaluate-all-methods --force-reevaluate
```

生成 Excel 汇总：

```bash
python test_dataset_evaluate.py --generate-excel
python test_dataset_evaluate.py --generate-excel --excel-output ./report.xlsx
```

## Baseline 调用

`evaluation/` 下其余几个脚本主要用于 baseline 对比。它们通常直接读取本地数据集和文档。

### 1. LLM Cascade Baseline

脚本：[llm_evaluate_cascade.py](./llm_evaluate_cascade.py)

三种模式：

- `oracle`：关系表抽取时使用 GT 上下文
- `pipeline`：关系表抽取时使用模型自己前面抽出的结果
- `pipeline-oracle`：实体表走预测，关系表使用 GT 实体参考

示例：

```bash
python llm_evaluate_cascade.py --eval-mode oracle
python llm_evaluate_cascade.py --eval-mode pipeline
python llm_evaluate_cascade.py --eval-mode pipeline-oracle
python llm_evaluate_cascade.py --model gpt-4o --run-name exp_001 --eval-mode oracle
python llm_evaluate_cascade.py --force-rerun --eval-mode pipeline
python llm_evaluate_cascade.py --db-name medical --case-name case2 --model glm-5.1 --eval-mode pipeline-oracle
```

### 2. DocETL Baseline

脚本：[docetl_evaluate.py](./docetl_evaluate.py)

示例：

```bash
python docetl_evaluate.py
python docetl_evaluate.py --model gpt-4o
python docetl_evaluate.py --force-rerun
python docetl_evaluate.py --db-name transportation --case-name case1 --model gpt-5.4
python docetl_evaluate.py --model gpt-4o --run-name baseline_test --force-rerun
```

### 3. LangChain Baseline

脚本：[langchain_evaluate.py](./langchain_evaluate.py)

特点：

- 使用 `ChatOpenAI`
- 使用 `PydanticOutputParser`
- 直接产出结构化 JSON 再参与评测


### 4. LangExtract Baseline

脚本：[langextract_evaluate.py](./langextract_evaluate.py)

示例：

```bash
python langextract_evaluate.py
python langextract_evaluate.py --model gpt-4o
python langextract_evaluate.py --model gpt-5.4 --extraction-passes 1
python langextract_evaluate.py --data-name medical --case-name case2 --model gpt-5.4
python langextract_evaluate.py --model gpt-4o --extraction-passes 1 --run-name baseline_test
```

## 输出与结果文件

不同脚本的输出结构不完全一致，但整体遵循“按方法 / 数据库 / case / 模型”组织的方式。

常见文件包括：

- `output_tables.json`  
  标准化后的表格抽取结果，后续评测和对比通常都围绕它进行。

- `extracted_data.json`  
  baseline 直接抽取出的原始结构化结果。

- `metadata.json`  
  记录模型、方法、时间戳、运行名称等元信息。

- `processing_steps.json`  
  记录处理流程中的步骤信息。

- `*_evaluate.json`  
  单个案例的评测结果。

- `*.xlsx`  
  汇总报告。

### `output_tables.json` 的角色

它是结构化评测里最关键的中间结果格式。  
无论来源是：

- baseline 抽取结果
- 从 `full_result` / `snapshot` / `table_data` 兜底提取

最后都会尽量归一化成：

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

## 评测指标

### 1. Cell 级别指标

- `Cell Recall`
- `Cell Precision`
- `Cell F1`

### 2. 结构级别指标

- `Table Recall / Precision`
- `Field Recall / Precision`
- `Entity / Relation F1`

### 3. LLM 细粒度评分

部分脚本会额外调用 LLM 做质量评估，输出：

- `llm_score`
- `llm_accuracy`
- `llm_completeness`
- `llm_consistency`
- `llm_coverage`
- `llm_comments`

### 4. Notebook 分析

1. `fine_grained_capability_evaluation.ipynb`

- capability 级别误差分析
- case / relation type 细分
- 输出目录对比

2. `result.ipynb`

- 整体结果浏览
- 汇总数据整理
- 表格化分析
