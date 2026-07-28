# Data Construction

`data_construction` 是 `Doc2DB-Bench` 中负责从结构化数据库生成文档数据的模块。当前主入口是 [run_demo.py](./run_demo.py)，核心流程由 [src/pipeline.py](./src/pipeline.py) 驱动。

这套流程支持两类输入：

- 单表 JSON
- 单个数据库目录

当输入是数据库目录时，代码会先检查该目录是否已经预处理；如果没有，会按 Spider 或 BIRD 的目录特征自动预处理，再进入文档生成 pipeline。

## 目录结构

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

## 主要文件与职责

- [run_demo.py](./run_demo.py)  
  主入口。负责读取默认配置、自动预处理输入、加载数据库、构造 pipeline 并落盘结果。

- [src/config.py](./src/config.py)  
  全局默认配置，包括输入路径、输出路径、模型、模板、缓存与重试参数。

- [src/pipeline.py](./src/pipeline.py)  
  核心调度器。负责调用 labeling、refiner、serializer、writer、validator、profiler 等 agent。

- [src/agents/](./src/agents)  
  各阶段 agent 实现。
  - `labeling.py`: 生成 capability matrix
  - `refiner.py`: 生成 evidence pool
  - `serializer.py`: 切分写作 block
  - `writer.py`: 生成文档 block
  - `validator.py`: 校验文档
  - `profiler.py`: 从参考文档生成 reference guide

- [src/utils/input_loader.py](./src/utils/input_loader.py)  
  负责把单表 JSON 或数据库目录加载成 `Table` / `RelationalDatabase`。

- [src/utils/preprocess_spider.py](./src/utils/preprocess_spider.py)  
  Spider 预处理。读取 `schema.sql` 和 sqlite，生成 `schema.json`、`config.py`、`tables/*.json`。

- [src/utils/preprocess_bird_fast.py](./src/utils/preprocess_bird_fast.py)  
  BIRD 快速预处理。读取 sqlite、`dev_tables.json` / `train_tables.json` 等结构信息，生成 `schema.json`、`config.py`、`tables/*.json`。

- [clean.py](./clean.py)  
  清洗生成后的 markdown。

- [ocr.py](./ocr.py)  
  OCR 相关脚本，独立于主文档生成 pipeline。

## 支持的输入格式

### 1. 单表模式

由 [src/config.py](./src/config.py) 中的 `input_mode = "single_table"` 控制。

要求输入文件是一个可被 `Table.model_validate(...)` 接受的 JSON。

示例：

```text
examples/minimal_input.json
```

### 2. 数据库目录模式

由 `input_mode = "database_dir"` 控制。

`database_root` 必须指向一个“单库目录”，不是多个库的父目录。

支持两种状态：

- 已预处理目录
- 原始 Spider / BIRD 单库目录

#### 已预处理目录要求

目录下至少有：

```text
<database_root>/
├── config.py
├── schema.json
└── tables/
    ├── TableA.json
    └── TableB.json
```

#### Spider 原始目录要求

目录下至少有：

```text
<database_root>/
├── schema.sql
└── <db_name>.sqlite
```

当前 Spider 预处理不依赖 `dev.json`、`train_spider.json`、`tables.json`。

如果你希望获取完整的 Spider 数据集，而不是手动准备单个数据库目录，可以使用官方发布地址：

- Spider: <https://github.com/taoyds/spider>

#### BIRD 原始目录要求

目录下至少有：

```text
<database_root>/
└── <db_name>.sqlite
```

同时代码会从 `dataset/BIRD/{dev|train}/` 下读取：

- `dev_tables.json` / `train_tables.json`
- `config.py`

所以 `database_root` 最好位于 BIRD 既有目录体系下，便于自动识别 split 和 schema。

如果你希望获取完整的 BIRD 数据集，而不是手动准备单个数据库目录，可以使用官方发布地址：

- BIRD: <https://bird-bench.github.io>

## 快速调用

### 1. 配置输入路径

在 [src/config.py](./src/config.py) 的 `IOConfig` 中设置 `database_root`。

Spider 和 BIRD 都要求 `database_root` 指向单个数据库目录，而不是多个数据库的父目录。

Spider 示例：

```python
database_root = "dataset/spider/database/hospital_1"
```

BIRD 示例：

```python
database_root = "data_construction/dataset/BIRD/dev/dev_databases/financial"
```

### 2. 配置 LLM

在 [src/config.py](./src/config.py) 的 `LLMRuntimeConfig` 中设置：

- `labeling_model`
- `evidence_model`
- `writing_model`
- `validation_model`
- `base_url`
- `api_key`


### 3. 运行 run_demo

从仓库根执行：

```bash
python -m data_construction.run_demo
```

## run_demo 调用逻辑

`run_demo.py` 的执行顺序如下。

### Step 1. 读取默认配置

调用 [get_default_config()](./src/config.py)，得到 `AppConfig`：

- `io`: 输入输出路径
- `llm`: 模型与 API 配置
- `synthesis`: pipeline 参数
- `concurrency`: 预留并发参数

### Step 2. 初始化 LLM client 和各个 agent

`run_demo.py` 会创建：

- `OpenAILLMClient`
- `LabelingAgent`
- `RefinerAgent`
- `SerializerAgent`
- `WriterAgent`
- `ValidatorAgent`
- `ProfilerAgent`

然后把这些 agent 注入 `DocumentSynthesisPipeline`。

### Step 3. 判断输入模式

如果 `input_mode == "single_table"`：

- 调 `load_single_table_json(...)`

如果 `input_mode == "database_dir"`：

- 读取 `database_root`
- 判断目录是否已经预处理

预处理判定逻辑在 [run_demo.py](./run_demo.py)：

- `config.py` 存在
- `schema.json` 存在
- `tables/` 存在

### Step 4. 自动预处理

如果目录未预处理：

- 路径中包含 `spider` 时，调用 `spider_preprocess(...)`
- 路径中包含 `bird` 时，调用 `preprocess_bird_database_fast(...)`

预处理完成后，再通过 `load_database_from_directory(...)` 统一加载输入。

### Step 5. 执行 pipeline

入口在 [DocumentSynthesisPipeline.run(...)](./src/pipeline.py)。

如果输入是数据库，会进入 `run_database()`；如果输入是单表，会进入 `run_table()`。

## Pipeline 步骤

当前 pipeline 的核心顺序如下。

### 1. profiler（可选）

如果你希望 profiler 基于真实参考文档生成 guide，先准备 `reference_document_path` 对应的 markdown 文件。

推荐做法是先把参考 PDF 放到：

```text
data_construction/dataset/reference_doc/
```

然后用 OCR 先转成 markdown，例如：

```bash
python data_construction/ocr.py single \
  data_construction/dataset/reference_doc/example.pdf \
  --ocr_type deepseek_ocr \
  --output data_construction/dataset/reference_doc/example.md
```

之后在 [src/config.py](./src/config.py) 中设置：

```python
reference_document_path = "data_construction/dataset/reference_doc/example.md"
reference_guide_path = "data_construction/dataset/reference_doc/example.json"
```

如果 `enable_profiler_alignment=True`，且：

- `reference_document_path` 有内容
- `reference_guide_path` 有内容
- `reference_guide_path` 还没有可用文件

则调用 `ProfilerAgent` 从参考文档生成 guide，写到 `reference_guide_path`。

见 [src/pipeline.py](./src/pipeline.py)。

### 2. labeling

`LabelingAgent.run(...)` 读取表数据，输出 capability matrix。

### 3. refiner

`RefinerAgent.run(...)` 根据表和 capability matrix 生成 evidence pool。

如果开启缓存并且已经存在：

```text
<database_root>/tables/evidence/<table>.evidence.json
```

则优先读缓存，跳过重新生成。

### 4. serializer

`SerializerAgent.run(...)` 把 evidence pool 组织成 `TaskQueue`，每个 task 对应一个待写 block。

### 5. writer

`WriterAgent.run(...)` 并发写每个 block。

- 首轮使用上游 evidence 构造历史上下文
- 重试时使用前后邻居 block 的生成文本构造上下文
- 最大并发 worker 数为 `min(8, block_count)`

### 6. validator

`ValidatorAgent.run(...)` 校验最终文档。

如果校验失败：

- 根据 `issues` 提取失败 block
- 在 `max_validation_retries` 范围内重试对应 block

### 7. 保存 markdown

校验通过后，markdown 会写到：

```text
<database_root>/docs/<table_id>.md
```

如果最终重试后仍未通过，也会落盘最后一版文档。

### 8. 保存总结果 JSON

`save_result_json(...)` 会把当前结果 merge 到输出 JSON，而不是直接覆盖。

输出格式：

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

## 配置说明

配置集中在 [src/config.py](./src/config.py)。

### IOConfig

- `input_mode`: `single_table` 或 `database_dir`
- `input_path`: 单表模式输入文件
- `database_root`: 单库目录
- `tables_subdir`: 默认 `tables`
- `schema_filename`: 默认 `schema.json`
- `output_path`: 总结果 JSON 路径

### LLMRuntimeConfig

- `api_key`
- `base_url`
- `timeout_seconds`
- `max_retries`
- `labeling_model`
- `evidence_model`
- `writing_model`
- `validation_model`
- `null_generation_model`

### BaseParametersConfig

- `enable_profiler_alignment`
- `reference_document_path`
- `reference_guide_path`
- `document_length_tokens`
- `noise_level`
- `linguistic_complexity`
- `label_ratio`
- `if_primary_allowed`

### RuntimeConfig

- `sliding_window_blocks`
- `max_previous_context_chars`
- `max_validation_retries`
- `enable_soft_validation`
- `if_cached`

## 常用脚本

### 主流程

```bash
python -m data_construction.run_demo
```

### 直接校验已生成文档

```bash
python data_construction/src/agents/validator.py \
  --database-root <db_root> \
  --table-id <table_name> \
  --document-json <document_json>
```

### 清洗 docs 目录下的 markdown

```bash
python data_construction/clean.py
```

### OCR

```bash
python data_construction/ocr.py --help
```

### 预处理工具

可直接调用这些模块中的函数，或单独运行对应脚本：

- [src/utils/preprocess_spider.py](./src/utils/preprocess_spider.py)
- [src/utils/preprocess_bird.py](./src/utils/preprocess_bird.py)
- [src/utils/preprocess_bird_fast.py](./src/utils/preprocess_bird_fast.py)
- [src/utils/group_evidence.py](./src/utils/group_evidence.py)
- [src/utils/add_null_relation.py](./src/utils/add_null_relation.py)
