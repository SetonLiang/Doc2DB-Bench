#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LangChain 抽取能力评测脚本
使用 LangChain 的 PydanticOutputParser 进行结构化数据抽取
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional, Type

# 添加项目根目录到 Python 路径，以便导入 backend 模块
project_root = os.path.dirname(os.path.dirname(__file__))  # 从 evaluate 回到项目根目录
sys.path.insert(0, project_root)

# 加载环境变量
from utils import token_length
from dotenv import dotenv_values
from utils import _clean_response
env_path = Path(__file__).parent.parent/ "llm" / ".env"
config = dotenv_values(env_path)
DEFAULT_API_URL = config.get("GPT_URL_PRO")
DEFAULT_API_KEY = config.get("GPT_KEY_PRO")


# LangChain imports
from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI
from langchain.output_parsers import OutputFixingParser
from pydantic import BaseModel, Field, create_model

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 配置参数
# 现在所有文件都在evaluate文件夹里，需要指向上一级目录
DATASET_DIR = Path(__file__).parent.parent / "dataset/case/base_latest"
OUTPUT_DIR = Path(__file__).parent.parent / "dataset/case/base_latest_output/langchain"
DEFAULT_MODEL = "gpt-4o"
DEFAULT_CHUNK_SIZE = 100000  # 默认chunk大小（字符数）
DEFAULT_CHUNK_OVERLAP = 500  # chunk之间的重叠部分


class LangChainExtractorEvaluator:
    """LangChain 抽取能力评估器"""
    
    def __init__(self, model: str = DEFAULT_MODEL, dataset_dir: Path = DATASET_DIR,
                 output_dir: Path = OUTPUT_DIR, run_name: Optional[str] = None,
                 api_base: Optional[str] = None, api_key: Optional[str] = None,
                 chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
                 force_rerun: bool = False, data_name: Optional[str] = None,
                 case_name: Optional[str] = None):
        self.model = model
        self.dataset_dir = dataset_dir
        self.output_dir = output_dir
        self.run_name = run_name
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.force_rerun = force_rerun
        self.data_name_filter = data_name.strip() if isinstance(data_name, str) and data_name.strip() else None
        self.case_name_filter = case_name.strip() if isinstance(case_name, str) and case_name.strip() else None
        self.results = []

        # API 配置
        if api_base is None:
            api_base = DEFAULT_API_URL
        if api_key is None:
            api_key = DEFAULT_API_KEY

        # 修复 URL：与 langextract_evaluate.py 保持一致，去掉可能重复的 /chat/completions
        if api_base:
            original_url = api_base
            api_base = api_base.rstrip('/')
            if api_base.endswith('/chat/completions'):
                api_base = api_base[:-len('/chat/completions')].rstrip('/')
                logger.info(f"🔧 已修复 API Base URL: {original_url} -> {api_base}")
            api_base = api_base.rstrip('/')

        self.api_base = api_base
        self.api_key = api_key

        # 配置 LangChain LLM
        llm_kwargs = {
            "model": model,
            "temperature": 0,
        }
        if self.api_base:
            llm_kwargs["base_url"] = self.api_base
        if self.api_key:
            llm_kwargs["api_key"] = self.api_key

        self.llm = ChatOpenAI(**llm_kwargs)
        
        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 输出目录: {self.output_dir}")
        logger.info(f"🤖 模型: {model}")
        if self.api_base:
            logger.info(f"🌐 API Base: {self.api_base}")
        if run_name:
            logger.info(f"🏷️  运行名称: {run_name}")
        logger.info(f"📏 Chunk大小: {chunk_size} 字符, 重叠: {chunk_overlap} 字符")
        if force_rerun:
            logger.info(f"🔄 强制重新运行模式：将重新处理所有案例")
        else:
            logger.info(f"⏭️  跳过模式：已处理的案例将被跳过")
        if self.data_name_filter:
            logger.info(f"🗂️  数据库过滤: {self.data_name_filter}")
        if self.case_name_filter:
            logger.info(f"🧩 案例过滤: {self.case_name_filter}")

    def _get_case_output_dir(self, case_info: Dict[str, Any]) -> Path:
        """返回案例输出目录: OUTPUT_DIR / db_name / case_name / db_case_model"""
        return (
            self.output_dir
            / case_info['db_name']
            / case_info['case_name']
            / f"{case_info['db_name']}_{case_info['case_name']}_{self.model}"
        )
    
    def is_case_processed(self, case_info: Dict[str, Any]) -> bool:
        """检查某个case是否已经处理过"""
        method_dir = self._get_case_output_dir(case_info)
        metadata_file = method_dir / "metadata.json"
        extracted_file = method_dir / "extracted_data.json"
        
        if metadata_file.exists() and extracted_file.exists():
            try:
                # 验证文件内容是否有效
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                with open(extracted_file, 'r', encoding='utf-8') as f:
                    extracted_data = json.load(f)
                
                # 检查是否是相同的模型和方法
                if metadata.get('model') == self.model and metadata.get('method') == 'langchain':
                    logger.info(f"  ⏭️  案例已处理过，跳过: {case_info['db_name']}/{case_info['case_name']}")
                    return True
            except Exception as e:
                logger.warning(f"  ⚠️  读取已有结果失败，将重新处理: {e}")
                return False
        
        return False
    
    def load_existing_result(self, case_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """加载已处理案例的结果"""
        method_dir = self._get_case_output_dir(case_info)
        
        try:
            # 读取元数据
            with open(method_dir / "metadata.json", 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            # 读取提取的数据
            with open(method_dir / "extracted_data.json", 'r', encoding='utf-8') as f:
                extracted_data = json.load(f)
            
            result = {
                'case_id': f"{case_info['db_name']}/{case_info['case_name']}",
                'db_name': case_info['db_name'],
                'case_name': case_info['case_name'],
                'model': self.model,
                'method': 'langchain',
                'run_name': self.run_name,
                'status': 'skipped',
                'start_time': metadata.get('timestamp') or datetime.now().isoformat(),
                'end_time': metadata.get('timestamp') or datetime.now().isoformat(),
                'duration': 0,
                'evaluation': None,
                'output_dir': str(method_dir),
                'error': None,
                'from_cache': True,
                'note': '已跳过，使用已有结果文件'
            }

            try:
                expected_answer = self.load_expected_answer(case_info)
                cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
                result['evaluation'] = {
                    "cell_recall": cell_metrics['cell_recall'],
                    "cell_precision": cell_metrics['cell_precision'],
                    "cell_f1": cell_metrics['cell_f1'],
                    "total_expected_cells": cell_metrics['total_expected_cells'],
                    "total_generated_cells": cell_metrics['total_generated_cells'],
                    "total_correct_cells": cell_metrics['total_correct_cells'],
                }
                logger.info("  ✓ 已加载缓存结果并重新计算评估指标")
            except Exception as eval_error:
                logger.warning(f"  ⚠️  缓存结果评估失败，返回抽取结果: {eval_error}")
            
            return result
            
        except Exception as e:
            logger.error(f"  ✗ 加载已有结果失败: {e}")
            return None
    
    def create_pydantic_model_from_schema(self, schema: Dict[str, Any], table_name: str) -> Type[BaseModel]:
        """从 schema 动态创建 Pydantic 模型（参考 ref_langchain.py）"""
        
        # 获取表定义
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        
        # 构建字段定义
        field_definitions = {}
        
        for attr in attributes:
            field_name = attr['name']
            field_type = attr.get('type', 'VARCHAR')
            field_desc = attr.get('description', '')
            
            # 映射数据库类型到 Python 类型
            if 'VARCHAR' in field_type or 'TEXT' in field_type or 'ENUM' in field_type or 'string' in field_type.lower():
                python_type = str
            elif 'INT' in field_type or 'integer' in field_type.lower():
                python_type = int
            elif 'DECIMAL' in field_type or 'FLOAT' in field_type or 'number' in field_type.lower():
                python_type = float
            elif 'BOOLEAN' in field_type or 'boolean' in field_type.lower():
                python_type = bool
            elif 'DATE' in field_type or 'date' in field_type.lower():
                python_type = str
            else:
                python_type = str
            
            # 如果字段不是必需的，使用 Optional
            if not attr.get('required', False):
                python_type = Optional[python_type]
            
            # 创建字段
            field_definitions[field_name] = (python_type, Field(description=field_desc, default=None))
        
        # 动态创建 Pydantic 模型
        model_class = create_model(
            table_name,
            **field_definitions
        )
        
        logger.info(f"  ✓ 创建 Pydantic 模型: {table_name}")
        logger.info(f"    字段数量: {len(field_definitions)}")
        
        return model_class
    
    def create_list_model_from_schema(self, schema: Dict[str, Any], table_name: str) -> Type[BaseModel]:
        """创建包含多行数据的列表模型"""
        
        # 先创建单行模型
        row_model = self.create_pydantic_model_from_schema(schema, table_name)
        
        # 创建列表模型
        list_model = create_model(
            f"{table_name}List",
            rows=(List[row_model], Field(description=f"List of {table_name} records"))
        )
        
        return list_model
    
    def scan_datasets(self) -> List[Dict[str, Any]]:
        """扫描所有数据集和case"""
        cases = []
        
        if not self.dataset_dir.exists():
            logger.error(f"❌ 数据集目录不存在: {self.dataset_dir}")
            return cases
        
        # 遍历所有数据库目录
        for db_dir in self.dataset_dir.iterdir():
            if not db_dir.is_dir():
                continue
            
            db_name = db_dir.name
            if self.data_name_filter and db_name != self.data_name_filter:
                continue
            logger.info(f"📁 扫描数据库: {db_name}")
            
            # 遍历该数据库下的所有case
            for case_dir in db_dir.iterdir():
                if not case_dir.is_dir() or not case_dir.name.startswith("case"):
                    continue
                
                case_name = case_dir.name
                if self.case_name_filter and case_name != self.case_name_filter:
                    continue
                
                # 检查必要文件是否存在
                docs_dir = case_dir / "docs"
                schema_file = case_dir / "schema.json"
                if not schema_file.exists():
                    tables_schema_file = case_dir / "tables" / "schema.json"
                    if tables_schema_file.exists():
                        schema_file = tables_schema_file
                answer_file = case_dir / "answer.json"
                answers_dir = case_dir / "answers"
                answers_json_files = sorted(answers_dir.glob("*.json")) if answers_dir.is_dir() else []
                answer_dir = case_dir / "answer"
                answer_json_files = sorted(answer_dir.glob("*.json")) if answer_dir.is_dir() else []
                tables_dir = case_dir / "tables"
                table_json_files = (
                    sorted(
                        p
                        for p in tables_dir.glob("*.json")
                        if p.is_file() and p.name.lower() != "schema.json"
                    )
                    if tables_dir.is_dir()
                    else []
                )
                
                if not docs_dir.exists():
                    logger.warning(f"⚠️  {db_name}/{case_name}: docs目录不存在，跳过")
                    continue
                
                if not schema_file.exists():
                    logger.warning(f"⚠️  {db_name}/{case_name}: schema.json不存在（含 tables/schema.json），跳过")
                    continue
                
                # 标准答案优先级：answer.json > answers/*.json > answer/*.json > tables/*.json
                if answer_file.is_file():
                    answer_source = "file"
                    answer_path = answer_file
                elif answers_json_files:
                    answer_source = "answers"
                    answer_path = answers_dir
                elif answer_json_files:
                    answer_source = "folder"
                    answer_path = answer_dir
                elif table_json_files:
                    answer_source = "tables"
                    answer_path = tables_dir
                else:
                    logger.warning(
                        f"⚠️  {db_name}/{case_name}: 无标准答案（需要 answer.json、answers/*.json、answer/*.json 或 tables/*.json），跳过"
                    )
                    continue
                
                # 获取所有文档文件
                doc_files = []
                for ext in ['*.txt', '*.pdf', '*.md', '*.docx', '*.doc']:
                    doc_files.extend(list(docs_dir.glob(ext)))
                
                if not doc_files:
                    logger.warning(f"⚠️  {db_name}/{case_name}: docs目录为空，跳过")
                    continue
                
                cases.append({
                    'db_name': db_name,
                    'case_name': case_name,
                    'case_dir': case_dir,
                    'docs_dir': docs_dir,
                    'schema_file': schema_file,
                    'answer_file': answer_file,  # 兼容旧逻辑
                    'answer_source': answer_source,
                    'answer_path': answer_path,
                    'doc_files': [str(f) for f in doc_files]
                })
                
                if answer_source == "file":
                    ans_desc = "answer.json"
                elif answer_source == "answers":
                    ans_desc = f"answers/（{len(answers_json_files)} 个 json）"
                elif answer_source == "folder":
                    ans_desc = f"answer/（{len(answer_json_files)} 个 json）"
                else:
                    ans_desc = f"tables/（{len(table_json_files)} 个 json）"
                logger.info(f"  ✓ {db_name}/{case_name}: 发现 {len(doc_files)} 个文档文件，标准答案: {ans_desc}")
        
        logger.info(f"\n📊 总共发现 {len(cases)} 个有效测试案例\n")
        return cases

    def _answer_file_to_table_pieces(self, jf: Path, data: Any) -> Dict[str, Any]:
        """将 answer/ 或 answers/ 下单个 json 转为 {表名: rows} 片段。"""
        if isinstance(data, dict):
            return dict(data)
        if isinstance(data, list):
            return {jf.stem: data}
        logger.warning(f"  ⚠️  跳过 {jf.name}：根类型需为 object 或 array")
        return {}

    def _tables_file_to_table_pieces(self, jf: Path, data: Any) -> Dict[str, Any]:
        """将 tables/ 下单个 json 转为 {表名: rows}。"""
        stem = jf.stem
        skip_keys = frozenset({"capability_assignments", "capability_assignments_source", "evidence"})
        if isinstance(data, list):
            return {stem: data}
        if isinstance(data, dict):
            rows = data.get("rows")
            if isinstance(rows, list):
                return {stem: rows}
            out: Dict[str, Any] = {}
            for k, v in data.items():
                if k in skip_keys:
                    continue
                if isinstance(v, list):
                    out[k] = v
                elif isinstance(v, dict) and isinstance(v.get("rows"), list):
                    out[k] = v["rows"]
            if out:
                return out
            logger.warning(f"  ⚠️  跳过 {jf.name}：tables 模式下未解析出 rows 或可合并的表数据")
            return {}
        logger.warning(f"  ⚠️  跳过 {jf.name}：根类型需为 object 或 array")
        return {}

    def _merge_multi_table_json_dir(self, path: Path, mode: str) -> Dict[str, Any]:
        """合并目录下多个 *.json 为 expected_answer 字典。mode 为 answer 或 tables。"""
        merged: Dict[str, Any] = {}
        for jf in sorted(path.glob("*.json")):
            if mode == "tables" and jf.name.lower() == "schema.json":
                continue
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
            pieces = (
                self._tables_file_to_table_pieces(jf, data)
                if mode == "tables"
                else self._answer_file_to_table_pieces(jf, data)
            )
            for k, v in pieces.items():
                if k in merged:
                    logger.warning(f"  ⚠️  表 {k} 在多个文件中出现，后读入文件将覆盖先前内容: {jf.name}")
                merged[k] = v

        if not merged:
            label = "tables/" if mode == "tables" else "answer(s)/"
            raise ValueError(f"{label} 目录无有效表数据: {path}")
        return merged

    def load_expected_answer(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """加载标准答案：支持 answer.json、answers/、answer/、tables/。"""
        src = case_info.get("answer_source", "file")
        path: Path = case_info.get("answer_path", case_info.get("answer_file"))
        if src == "file":
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and isinstance(payload.get("tables"), dict):
                return payload["tables"]
            if isinstance(payload, dict):
                return payload
            raise ValueError(f"answer.json 格式错误（应为对象）: {path}")
        if src == "tables":
            return self._merge_multi_table_json_dir(path, "tables")
        return self._merge_multi_table_json_dir(path, "answer")

    def _normalize_schema_table(self, table: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """将单个表定义标准化为 {name, attributes, ...} 结构。"""
        if not isinstance(table, dict):
            return None
        normalized = dict(table)
        if "name" not in normalized and normalized.get("table_name"):
            normalized["name"] = normalized["table_name"]
        if "attributes" not in normalized:
            if isinstance(normalized.get("columns"), list):
                normalized["attributes"] = normalized["columns"]
            elif isinstance(normalized.get("fields"), list):
                normalized["attributes"] = normalized["fields"]
        if not normalized.get("name"):
            return None
        return normalized

    def normalize_schema(self, raw: Any) -> Dict[str, Any]:
        """将多种 schema 顶层结构统一为内部可消费的格式。"""
        if isinstance(raw, list):
            tables = []
            for t in raw:
                norm = self._normalize_schema_table(t) if isinstance(t, dict) else None
                if norm:
                    tables.append(norm)
            if not tables:
                logger.warning("  ⚠️  schema 根为数组但未找到带 name/table_name 的表定义")
            else:
                logger.info(f"  📋 schema 根为数组格式，已规范为 tables（共 {len(tables)} 张表）")
            return {"tables": tables}
        if isinstance(raw, dict):
            if "tables" in raw and isinstance(raw.get("tables"), list):
                tables = []
                for t in raw.get("tables", []):
                    norm = self._normalize_schema_table(t) if isinstance(t, dict) else None
                    if norm:
                        tables.append(norm)
                normalized_raw = dict(raw)
                normalized_raw["tables"] = tables
                return normalized_raw
            if "table_name" in raw and ("columns" in raw or "fields" in raw or "attributes" in raw):
                single_table = self._normalize_schema_table(raw)
                if single_table:
                    return {"tables": [single_table]}
                return raw
            logger.warning("  ⚠️  schema 对象缺少 tables，且非单表 table_name/columns(fields) 格式")
            return raw
        logger.warning(f"  ⚠️  无法识别的 schema 类型: {type(raw)}")
        return {"tables": []}
    
    def read_document_content(self, file_path: str) -> str:
        """读取文档内容"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"  ✓ 读取文档: {os.path.basename(file_path)} (长度: {len(content)} 字符)")
            return content
        except Exception as e:
            logger.error(f"  ✗ 读取文档失败: {os.path.basename(file_path)} - {e}")
            return ""
    
    def merge_documents(self, doc_files: List[str]) -> str:
        """合并多个文档的内容"""
        merged_content = []
        
        for i, doc_file in enumerate(doc_files, 1):
            content = self.read_document_content(doc_file)
            if content:
                merged_content.append(f"=== Document {i}: {os.path.basename(doc_file)} ===\n")
                merged_content.append(content)
                merged_content.append(f"\n=== End of Document {i} ===\n\n")
        
        final_content = "\n".join(merged_content)
        logger.info(f"  ✓ 合并完成，总长度: {len(final_content)} 字符")
        return final_content
    
    def split_text_into_chunks(self, text: str) -> List[str]:
        """将文本分割成多个chunks"""
        if len(text) <= self.chunk_size:
            logger.info(f"  ℹ️  文档长度 {len(text)} 字符，无需分chunk")
            return [text]
        
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + self.chunk_size
            
            # 如果不是最后一个chunk，尝试在合适的位置断开（如段落、句子）
            if end < len(text):
                # 优先在段落边界断开
                paragraph_break = text.rfind('\n\n', start, end)
                if paragraph_break > start + self.chunk_size // 2:
                    end = paragraph_break + 2
                else:
                    # 其次在句子边界断开
                    sentence_break = max(
                        text.rfind('。', start, end),
                        text.rfind('！', start, end),
                        text.rfind('？', start, end),
                        text.rfind('.', start, end),
                        text.rfind('!', start, end),
                        text.rfind('?', start, end)
                    )
                    if sentence_break > start + self.chunk_size // 2:
                        end = sentence_break + 1
            
            chunk = text[start:end]
            chunks.append(chunk)
            
            # 下一个chunk的起始位置，考虑重叠
            start = end - self.chunk_overlap if end < len(text) else end
        
        logger.info(f"  ✂️  文档已分割成 {len(chunks)} 个chunks，每个约 {self.chunk_size} 字符")
        for i, chunk in enumerate(chunks, 1):
            logger.info(f"     Chunk {i}: {len(chunk)} 字符")
        
        return chunks
    
    def extract_single_chunk(self, text: str, schema: Dict[str, Any], 
                            table_name: str, chunk_id: int = 0) -> tuple[List[Dict[str, Any]], str]:
        """从单个chunk中提取数据"""
        
        try:
            # 创建 Pydantic 列表模型
            list_model = self.create_list_model_from_schema(schema, table_name)
            
            # 创建 parser
            parser = PydanticOutputParser(pydantic_object=list_model)
            fixing_parser = OutputFixingParser.from_llm(parser=parser, llm=self.llm)
            
            # 获取当前表的定义和描述
            table_def = self._get_table_definition(schema, table_name)
            table_description = table_def.get('description', '') if table_def else ''
            
            # 构建表结构说明
            schema_context = self._build_schema_context(schema, table_name)
            
            # 构建 prompt
            chunk_info = f" (Chunk {chunk_id})" if chunk_id > 0 else ""
            prompt = f"""
Please extract structured data from the following text according to the schema for table "{table_name}".

{schema_context}

Extract ALL relevant records for the "{table_name}" table from the document. Each record should be a separate item in the list.

Text content{chunk_info}:
{text}

{parser.get_format_instructions()}

IMPORTANT: 
- Return a JSON object with a "rows" field containing a list of all extracted records for "{table_name}".
- Extract EVERY occurrence of relevant data for this table, even if mentioned multiple times.
- For foreign key fields, use the exact values that would match the referenced table.
"""
            
            logger.info(f"  🤖 调用 LangChain ({self.model}){chunk_info}...")
            
            # 调用 LLM
            response = self.llm.invoke(prompt)
            llm_response = response.content
            
            logger.info(f"  ✓ LLM响应长度: {len(llm_response)} 字符")
            
            # 解析响应
            try:
                result = fixing_parser.parse(llm_response)
                
                # 转换为标准格式
                rows = []
                if hasattr(result, 'rows'):
                    for row in result.rows:
                        row_dict = {}
                        if hasattr(row, 'model_dump'):
                            row_dict = row.model_dump()
                        elif hasattr(row, 'dict'):
                            row_dict = row.dict()
                        else:
                            row_dict = dict(row)
                        rows.append(row_dict)
                
                logger.info(f"  ✓ 成功提取 {len(rows)} 行数据{chunk_info}")
                
                return rows, llm_response
                
            except Exception as parse_error:
                logger.error(f"  ✗ 解析失败{chunk_info}: {parse_error}")
                logger.debug(f"LLM响应: {llm_response[:500]}...")
                return [], llm_response
        
        except Exception as e:
            logger.error(f"  ✗ LangChain 提取失败{chunk_info}: {e}")
            import traceback
            traceback.print_exc()
            return [], str(e)
    
    def merge_extraction_results(self, all_rows: List[List[Dict[str, Any]]], 
                                 table_name: str) -> Dict[str, List[Dict[str, Any]]]:
        """合并多个chunk的提取结果，去除重复记录"""
        
        merged_rows = []
        seen_records = set()
        
        for chunk_rows in all_rows:
            for row in chunk_rows:
                # 创建行的唯一标识符（基于所有字段值）
                row_signature = self._create_row_signature(row)
                
                if row_signature not in seen_records:
                    seen_records.add(row_signature)
                    merged_rows.append(row)
        
        logger.info(f"  🔗 合并结果: 总共 {sum(len(r) for r in all_rows)} 行 -> 去重后 {len(merged_rows)} 行")
        
        return {table_name: merged_rows}
    
    def _create_row_signature(self, row: Dict[str, Any]) -> str:
        """创建行的唯一签名用于去重"""
        # 将所有字段值标准化后连接成字符串
        values = []
        for key in sorted(row.keys()):
            value = row[key]
            normalized = self.normalize_value(value)
            if normalized:  # 只包含非空值
                values.append(f"{key}:{normalized}")
        return "||".join(values)
    
    def extract_with_langchain(self, text: str, schema: Dict[str, Any], 
                               table_name: str) -> tuple[Dict[str, List[Dict[str, Any]]], str]:
        """使用 LangChain 提取数据（支持分chunk处理）"""
        
        try:
            # 分割文本成chunks
            chunks = self.split_text_into_chunks(text)
            
            all_rows = []
            all_responses = []
            
            # 处理每个chunk
            for i, chunk in enumerate(chunks, 1):
                logger.info(f"\n  📄 处理 Chunk {i}/{len(chunks)}...")
                
                rows, llm_response = self.extract_single_chunk(
                    chunk, schema, table_name, chunk_id=i if len(chunks) > 1 else 0
                )
                
                all_rows.append(rows)
                all_responses.append(f"=== Chunk {i} Response ===\n{llm_response}\n")
                
                # 短暂延迟，避免API限流
                if i < len(chunks):
                    time.sleep(0.5)
            
            # 合并所有chunk的结果
            merged_result = self.merge_extraction_results(all_rows, table_name)
            
            # 合并所有响应
            combined_response = "\n".join(all_responses)
            
            logger.info(f"  ✅ 完成所有chunks的提取，最终结果: {len(merged_result[table_name])} 行数据")
            
            return merged_result, combined_response
            
        except Exception as e:
            logger.error(f"  ✗ LangChain 提取失败: {e}")
            import traceback
            traceback.print_exc()
            return {table_name: []}, str(e)
    
    def extract_relation_single_chunk(self, text: str, schema: Dict[str, Any], 
                                      table_name: str, reference_tables_data: Dict[str, List[Dict[str, Any]]],
                                      chunk_id: int = 0) -> tuple[List[Dict[str, Any]], str]:
        """从单个chunk中提取关系表数据（带参考表）"""
        
        try:
            # 创建 Pydantic 列表模型
            list_model = self.create_list_model_from_schema(schema, table_name)
            
            # 创建 parser
            parser = PydanticOutputParser(pydantic_object=list_model)
            fixing_parser = OutputFixingParser.from_llm(parser=parser, llm=self.llm)
            
            # 获取当前表的定义和描述
            table_def = self._get_table_definition(schema, table_name)
            relation_config = table_def.get('relation_extraction', {}) if table_def else {}
            
            # 构建表结构说明
            schema_context = self._build_schema_context(schema, table_name)
            
            # 构建参考表prompt
            reference_tables_prompt = self._build_reference_tables_prompt(reference_tables_data, relation_config)
            
            # 构建 prompt
            chunk_info = f" (Chunk {chunk_id})" if chunk_id > 0 else ""
            prompt = f"""
You are an expert in structured data extraction, specializing in relationship extraction.

{schema_context}

{reference_tables_prompt}

Your task: Extract relationship data from the following text for the "{table_name}" table.

Text content{chunk_info}:
{text}

{parser.get_format_instructions()}

IMPORTANT INSTRUCTIONS FOR RELATIONSHIP EXTRACTION:
- Return a JSON object with a "rows" field containing a list of all extracted relationship records.
- Use the reference tables above to map entity names/descriptions to their correct IDs.
- For foreign key fields, use the EXACT ID values from the reference tables, NOT the entity names.
- Extract ONLY the relationships that are explicitly mentioned in the document.
- Each row represents ONE relationship between entities.
"""
            
            logger.info(f"  🤖 调用 LangChain ({self.model}){chunk_info} [关系抽取]...")
            
            # 调用 LLM
            response = self.llm.invoke(prompt)
            llm_response = response.content
            
            logger.info(f"  ✓ LLM响应长度: {len(llm_response)} 字符")
            
            # 解析响应
            try:
                result = fixing_parser.parse(llm_response)
                
                # 转换为标准格式
                rows = []
                if hasattr(result, 'rows'):
                    for row in result.rows:
                        row_dict = {}
                        if hasattr(row, 'model_dump'):
                            row_dict = row.model_dump()
                        elif hasattr(row, 'dict'):
                            row_dict = row.dict()
                        else:
                            row_dict = dict(row)
                        rows.append(row_dict)
                
                logger.info(f"  ✓ 成功提取 {len(rows)} 行关系数据{chunk_info}")
                
                return rows, llm_response
                
            except Exception as parse_error:
                logger.error(f"  ✗ 解析失败{chunk_info}: {parse_error}")
                logger.debug(f"LLM响应: {llm_response[:500]}...")
                return [], llm_response
        
        except Exception as e:
            logger.error(f"  ✗ LangChain 关系提取失败{chunk_info}: {e}")
            import traceback
            traceback.print_exc()
            return [], str(e)
    
    def extract_relation_with_langchain(self, text: str, schema: Dict[str, Any], 
                                        table_name: str, reference_tables_data: Dict[str, List[Dict[str, Any]]],
                                        extracted_entity_data: Dict[str, List[Dict]] = None,
                                        case_dir: Path = None) -> tuple[Dict[str, List[Dict[str, Any]]], str]:
        """使用 LangChain 提取关系表数据（带参考表，支持分chunk处理）"""
        
        try:
            table_def = self._get_table_definition(schema, table_name)
            
            # 🎯 如果配置了document_sources，直接读取指定文档
            relation_config = table_def.get('relation_extraction', {}) if table_def else {}
            if relation_config.get('enabled') and relation_config.get('document_sources') and case_dir:
                logger.info(f"  🎯 检测到document_sources配置，将直接读取指定文档")
                text = self._load_document_sources_for_relation(relation_config, case_dir)
                if not text:
                    logger.warning(f"  ⚠️ document_sources加载失败，使用传入的text")
            
            # 1. 如果没有提供参考表数据，尝试加载
            if not reference_tables_data and case_dir:
                reference_tables_data = self._load_reference_tables(table_def, case_dir)
            
            # 2. 如果仍然没有参考数据，尝试使用已提取的实体数据
            if not reference_tables_data and extracted_entity_data:
                logger.info(f"  📋 使用已提取的实体表数据作为参考...")
                attributes = table_def.get('attributes', []) if table_def else []
                for attr in attributes:
                    constraints = attr.get('constraints', {})
                    if constraints.get('foreign_key'):
                        field_name = attr.get('name', '')
                        if field_name.endswith('ID') or field_name.endswith('_id'):
                            ref_table = field_name[:-2] if field_name.endswith('ID') else field_name[:-3]
                            for entity_name, entity_data in extracted_entity_data.items():
                                if entity_name.lower() == ref_table.lower():
                                    reference_tables_data[entity_name] = entity_data
                                    logger.info(f"    ✓ 使用已提取的 {entity_name} 作为参考")
            
            if reference_tables_data:
                logger.info(f"  ✅ 已加载 {len(reference_tables_data)} 个参考表")
            else:
                logger.warning(f"  ⚠️  未找到参考表数据，将进行无参考的关系提取")
                # 没有参考数据时使用普通提取
                return self.extract_with_langchain(text, schema, table_name)
            
            # 分割文本成chunks
            chunks = self.split_text_into_chunks(text)
            
            all_rows = []
            all_responses = []
            
            # 处理每个chunk
            for i, chunk in enumerate(chunks, 1):
                logger.info(f"\n  📄 处理 Chunk {i}/{len(chunks)} [关系抽取]...")
                
                rows, llm_response = self.extract_relation_single_chunk(
                    chunk, schema, table_name, reference_tables_data,
                    chunk_id=i if len(chunks) > 1 else 0
                )
                
                all_rows.append(rows)
                all_responses.append(f"=== Relation Chunk {i} Response ===\n{llm_response}\n")
                
                # 短暂延迟，避免API限流
                if i < len(chunks):
                    time.sleep(0.5)
            
            # 合并所有chunk的结果
            merged_result = self.merge_extraction_results(all_rows, table_name)
            
            # 合并所有响应
            combined_response = "\n".join(all_responses)
            
            logger.info(f"  ✅ 完成所有chunks的关系提取，最终结果: {len(merged_result[table_name])} 行数据")
            
            return merged_result, combined_response
            
        except Exception as e:
            logger.error(f"  ✗ LangChain 关系提取失败: {e}")
            import traceback
            traceback.print_exc()
            return {table_name: []}, str(e)
    
    def _get_table_definition(self, schema: Dict[str, Any], table_name: str) -> Optional[Dict[str, Any]]:
        """获取表格定义"""
        
        tables = schema.get('tables', [])
        for table in tables:
            if table.get('name') == table_name:
                result = table.copy()
                if 'fields' in result and 'attributes' not in result:
                    result['attributes'] = result.pop('fields')
                return result
        
        if schema.get('table_name') == table_name and 'columns' in schema:
            return {
                'name': table_name,
                'attributes': schema['columns']
            }
        
        if len(tables) == 1:
            result = tables[0].copy()
            if 'fields' in result and 'attributes' not in result:
                result['attributes'] = result.pop('fields')
            return result
        
        return None
    
    def classify_tables_by_type(self, schema: Dict[str, Any]) -> Dict[str, List[str]]:
        """将表分类为实体表和关系表
        
        根据 schema 中表的 "type" 字段来分类：
        - type: "entity" → 实体表
        - type: "relation" / "relationship" → 关系表
        - 如果没有 type 字段，默认为实体表
        
        Returns:
            {'entity_tables': [...], 'relation_tables': [...]}
        """
        if not isinstance(schema, dict):
            return {'entity_tables': [], 'relation_tables': []}
        
        entity_tables = []
        relation_tables = []
        
        if 'tables' in schema and isinstance(schema['tables'], list):
            for table in schema['tables']:
                if not isinstance(table, dict) or 'name' not in table:
                    continue
                
                table_name = table['name']
                table_type = table.get('type', 'entity').lower()
                
                # 检查是否有 relation_extraction 配置
                relation_config = table.get('relation_extraction', {})
                has_relation_config = relation_config.get('enabled', False)
                
                if table_type in ['relation', 'relationship'] or has_relation_config:
                    relation_tables.append(table_name)
                else:
                    entity_tables.append(table_name)
        
        elif 'table_name' in schema:
            table_name = schema['table_name']
            table_type = schema.get('type', 'entity').lower()
            
            if table_type in ['relation', 'relationship']:
                relation_tables.append(table_name)
            else:
                entity_tables.append(table_name)
        
        return {
            'entity_tables': entity_tables,
            'relation_tables': relation_tables
        }
    
    def _load_document_sources_for_relation(self, relation_config: Dict[str, Any], case_dir: Path) -> str:
        """从配置的document_sources加载关系表的专用文档
        
        Args:
            relation_config: relation_extraction配置
            case_dir: 案例目录（用于解析相对路径）
            
        Returns:
            文档内容（从document_sources加载）
        """
        document_sources = relation_config.get('document_sources', [])
        if not document_sources:
            logger.warning(f"  ⚠️ document_sources为空")
            return ""
        
        logger.info(f"  🎯 从document_sources加载 {len(document_sources)} 个文档")
        
        merged_content = []
        
        for doc_source in document_sources:
            try:
                # 解析文档路径
                doc_path = Path(doc_source)
                
                if not doc_path.is_absolute():
                    # 尝试多种路径策略
                    candidate1 = case_dir / doc_source
                    candidate2 = case_dir.parent.parent / doc_source  # dataset/xxx/case1 -> dataset
                    candidate3 = Path(__file__).parent.parent / doc_source  # 从evaluate目录
                    
                    if candidate1.exists():
                        doc_path = candidate1
                    elif candidate2.exists():
                        doc_path = candidate2
                    elif candidate3.exists():
                        doc_path = candidate3
                    else:
                        doc_path = Path(__file__).parent.parent / doc_source
                
                if not doc_path.exists():
                    logger.error(f"  ❌ 文档源不存在: {doc_path}")
                    continue
                
                with open(doc_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                if content:
                    merged_content.append(f"=== Document: {doc_path.name} ===\n")
                    merged_content.append(content)
                    merged_content.append(f"\n=== End of {doc_path.name} ===\n\n")
                    logger.info(f"  ✅ 成功加载文档: {doc_path.name} ({len(content)} 字符)")
                
            except Exception as e:
                logger.error(f"  ❌ 加载文档失败 {doc_source}: {e}")
                continue
        
        if merged_content:
            result = "\n".join(merged_content)
            logger.info(f"  ✅ 从document_sources加载了 {len(document_sources)} 个文档，总计 {len(result)} 字符")
            return result
        else:
            logger.warning(f"  ⚠️ document_sources配置的文档都加载失败")
            return ""
    
    def _load_reference_tables(self, table_def: Dict[str, Any], case_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
        """加载关系抽取所需的参考表数据
        
        Args:
            table_def: 表定义（包含 relation_extraction 配置）
            case_dir: 案例目录（用于解析相对路径）
            
        Returns:
            字典，key为表名，value为该表的数据列表
        """
        reference_tables = {}
        
        relation_config = table_def.get('relation_extraction', {})
        if not relation_config.get('enabled', False):
            return reference_tables
        
        reference_configs = relation_config.get('reference_tables', [])
        
        for ref_config in reference_configs:
            table_name = ref_config.get('table')
            data_source = ref_config.get('data_source')
            key_fields = ref_config.get('key_fields', [])
            
            if not table_name or not data_source:
                logger.warning(f"⚠️ 参考表配置不完整，跳过: {ref_config}")
                continue
            
            try:
                # 解析数据源路径
                data_path = Path(data_source)
                if not data_path.is_absolute():
                    # 尝试多种路径策略
                    candidate1 = case_dir / data_source
                    candidate2 = case_dir.parent.parent / data_source  # dataset/xxx/case1 -> dataset
                    candidate3 = Path(__file__).parent.parent / data_source  # 从evaluate目录
                    
                    if candidate1.exists():
                        data_path = candidate1
                    elif candidate2.exists():
                        data_path = candidate2
                    elif candidate3.exists():
                        data_path = candidate3
                    else:
                        data_path = Path(__file__).parent.parent / data_source
                
                if not data_path.exists():
                    logger.warning(f"⚠️ 参考表数据文件不存在: {data_path}")
                    continue
                
                with open(data_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if table_name in data:
                    table_data = data[table_name]
                    
                    # 如果指定了 key_fields，只保留这些字段
                    if key_fields:
                        filtered_data = []
                        for row in table_data:
                            filtered_row = {k: v for k, v in row.items() if k in key_fields}
                            filtered_data.append(filtered_row)
                        reference_tables[table_name] = filtered_data
                    else:
                        reference_tables[table_name] = table_data
                    
                    logger.info(f"  ✅ 加载参考表 {table_name}: {len(table_data)} 行")
                else:
                    logger.warning(f"  ⚠️ 数据文件中未找到表 {table_name}")
                    
            except Exception as e:
                logger.error(f"  ❌ 加载参考表 {table_name} 失败: {e}")
                continue
        
        return reference_tables
    
    def _build_reference_tables_prompt(self, reference_tables_data: Dict[str, List[Dict[str, Any]]], 
                                       relation_config: Dict[str, Any]) -> str:
        """构建包含参考表数据的prompt部分
        
        Args:
            reference_tables_data: 参考表数据字典
            relation_config: relation_extraction配置
            
        Returns:
            格式化的prompt字符串
        """
        if not reference_tables_data:
            return ""
        
        prompt_parts = ["\n【Reference Tables for Relationship Extraction】"]
        
        extraction_hint = relation_config.get('extraction_hint', '')
        if extraction_hint:
            prompt_parts.append(f"\nTask: {extraction_hint}\n")
        
        for table_name, table_data in reference_tables_data.items():
            if not table_data:
                continue
            
            prompt_parts.append(f"\n{table_name} Table:")
            
            if table_data:
                fields = list(table_data[0].keys())
                
                # 表头
                header = "  | " + " | ".join(fields) + " |"
                separator = "  |" + "|".join(["-" * (len(f) + 2) for f in fields]) + "|"
                prompt_parts.append(header)
                prompt_parts.append(separator)
                
                # 数据行（限制显示前50行）
                max_rows = min(50, len(table_data))
                for row in table_data[:max_rows]:
                    values = [str(row.get(f, '')) for f in fields]
                    data_row = "  | " + " | ".join(values) + " |"
                    prompt_parts.append(data_row)
                
                if len(table_data) > max_rows:
                    prompt_parts.append(f"  ... (total {len(table_data)} rows)")
            
            prompt_parts.append("")
        
        prompt_parts.append("IMPORTANT Instructions for Relationship Extraction:")
        prompt_parts.append("- Use the reference tables above to match entity names/descriptions to their IDs")
        prompt_parts.append("- For foreign key fields, MUST use the exact ID values from the reference tables")
        prompt_parts.append("- Extract ONLY the relationships that are explicitly mentioned in the document")
        prompt_parts.append("")
        
        return "\n".join(prompt_parts)
    
    def _build_schema_context(self, schema: Dict[str, Any], current_table: str) -> str:
        """构建表结构上下文说明"""
        
        context_parts = []
        
        # 获取所有表
        tables = schema.get('tables', [])
        
        if len(tables) > 1:
            # 多表场景：提供所有表的概览
            context_parts.append("Database Schema Overview:")
            context_parts.append(f"This database contains {len(tables)} tables:")
            
            for table in tables:
                table_name = table.get('name', 'unknown')
                table_type = table.get('type', 'entity')
                fields = table.get('fields', table.get('attributes', []))
                
                # 找出主键和外键字段
                pk_fields = [f['name'] for f in fields if f.get('constraints', {}).get('primary_key', False)]
                fk_fields = [f['name'] for f in fields if f.get('constraints', {}).get('foreign_key', False)]
                
                context_parts.append(f"  - {table_name} ({table_type}): {len(fields)} fields")
                if pk_fields:
                    context_parts.append(f"    Primary Key: {', '.join(pk_fields)}")
                if fk_fields:
                    context_parts.append(f"    Foreign Keys: {', '.join(fk_fields)}")
            
            context_parts.append("")
            
            # 提供关系信息
            relations = schema.get('relations', [])
            if relations:
                context_parts.append("Table Relations:")
                for rel in relations:
                    rel_type = rel.get('type', 'unknown')
                    from_table = rel.get('from', {}).get('table', 'unknown')
                    from_field = rel.get('from', {}).get('field', 'unknown')
                    to_table = rel.get('to', {}).get('table', 'unknown')
                    to_field = rel.get('to', {}).get('field', 'unknown')
                    context_parts.append(f"  - {from_table}.{from_field} -> {to_table}.{to_field} ({rel_type})")
                context_parts.append("")
        
        # 当前表的详细信息
        table_def = self._get_table_definition(schema, current_table)
        if table_def:
            context_parts.append(f"Target Table: {current_table}")
            table_type = table_def.get('type', 'entity')
            table_desc = table_def.get('description', '')
            
            if table_type:
                context_parts.append(f"Type: {table_type}")
            if table_desc:
                context_parts.append(f"Description: {table_desc}")
            
            fields = table_def.get('attributes', table_def.get('fields', []))
            if fields:
                context_parts.append(f"Fields ({len(fields)}):")
                for field in fields:
                    field_name = field.get('name', 'unknown')
                    field_type = field.get('type', 'VARCHAR')
                    field_desc = field.get('description', '')
                    constraints = field.get('constraints', {})
                    
                    constraint_info = []
                    if constraints.get('primary_key'):
                        constraint_info.append('PK')
                    if constraints.get('foreign_key'):
                        constraint_info.append('FK')
                    if not constraints.get('nullable', True):
                        constraint_info.append('NOT NULL')
                    
                    constraint_str = f" [{', '.join(constraint_info)}]" if constraint_info else ""
                    context_parts.append(f"  - {field_name} ({field_type}){constraint_str}: {field_desc}")
        
        return "\n".join(context_parts)
    
    def normalize_value(self, value: Any) -> str:
        """标准化单元格值"""
        if value is None:
            return ""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)):
            return str(value)
        return str(value).strip().lower()
    
    def calculate_cell_metrics(self, generated: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
        """计算cell级别的召回率和准确率"""
        try:
            expected_cells = set()
            generated_cells = set()
            correct_cells = set()
            
            for table_name, expected_rows in expected.items():
                if not isinstance(expected_rows, list):
                    continue
                
                table_name_lower = table_name.lower()
                
                for row_idx, row in enumerate(expected_rows):
                    if isinstance(row, dict):
                        for attr, value in row.items():
                            attr_lower = attr.lower()
                            norm_value = self.normalize_value(value)
                            expected_cells.add((table_name_lower, attr_lower, norm_value))
            
            for table_name, generated_rows in generated.items():
                if not isinstance(generated_rows, list):
                    continue
                
                table_name_lower = table_name.lower()
                
                for row_idx, row in enumerate(generated_rows):
                    if isinstance(row, dict):
                        for attr, value in row.items():
                            attr_lower = attr.lower()
                            norm_value = self.normalize_value(value)
                            cell_key = (table_name_lower, attr_lower, norm_value)
                            generated_cells.add(cell_key)
                            
                            if cell_key in expected_cells:
                                correct_cells.add(cell_key)
            
            total_expected = len(expected_cells)
            total_generated = len(generated_cells)
            total_correct = len(correct_cells)
            
            recall = (total_correct / total_expected * 100) if total_expected > 0 else 0
            precision = (total_correct / total_generated * 100) if total_generated > 0 else 0
            f1_score = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0
            
            return {
                "cell_recall": recall,
                "cell_precision": precision,
                "cell_f1": f1_score,
                "total_expected_cells": total_expected,
                "total_generated_cells": total_generated,
                "total_correct_cells": total_correct
            }
            
        except Exception as e:
            logger.error(f"  ✗ 计算cell指标失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "cell_recall": 0.0,
                "cell_precision": 0.0,
                "cell_f1": 0.0,
                "total_expected_cells": 0,
                "total_generated_cells": 0,
                "total_correct_cells": 0
            }
    
    def save_output(self, case_info: Dict[str, Any], extracted_data: Dict[str, Any], 
                    llm_response: str) -> Path:
        """保存处理结果"""
        method_dir = self._get_case_output_dir(case_info)
        method_dir.mkdir(parents=True, exist_ok=True)
        
        metadata = {
            'model': self.model,
            'run_name': self.run_name,
            'method': 'langchain',
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'timestamp': datetime.now().isoformat()
        }
        
        with open(method_dir / "metadata.json", 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        with open(method_dir / "extracted_data.json", 'w', encoding='utf-8') as f:
            json.dump(extracted_data, f, ensure_ascii=False, indent=2)
        
        with open(method_dir / "llm_response.txt", 'w', encoding='utf-8') as f:
            f.write(llm_response)
        
        logger.info(f"  ✓ 结果已保存到: {method_dir}")
        return method_dir
    
    def process_case(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个测试案例"""
        case_id = f"{case_info['db_name']}/{case_info['case_name']}"
        logger.info(f"\n{'='*80}")
        logger.info(f"🔍 开始处理: {case_id}")
        logger.info(f"{'='*80}")
        
        # 检查是否已经处理过（如果不是强制重新运行模式）
        if not self.force_rerun and self.is_case_processed(case_info):
            existing_result = self.load_existing_result(case_info)
            if existing_result:
                logger.info(f"✅ 使用缓存结果: {case_id}")
                return existing_result
            else:
                logger.warning(f"⚠️  缓存结果加载失败，将重新处理: {case_id}")
        
        result = {
            'case_id': case_id,
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'model': self.model,
            'method': 'langchain',
            'run_name': self.run_name,
            'status': 'failed',
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'duration': 0,
            'evaluation': None,
            'error': None,
            'from_cache': False
        }
        
        start_time = time.time()
        
        try:
            logger.info("📋 读取schema...")
            with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                schema = self.normalize_schema(json.load(f))
            
            logger.info(f"📚 合并 {len(case_info['doc_files'])} 个文档...")
            merged_content = self.merge_documents(case_info['doc_files'])
            
            if not merged_content:
                raise Exception("文档内容为空")
            
            # 分类表为实体表和关系表
            table_classification = self.classify_tables_by_type(schema)
            entity_tables = table_classification['entity_tables']
            relation_tables = table_classification['relation_tables']
            
            all_table_names = entity_tables + relation_tables
            logger.info(f"📊 需要提取 {len(all_table_names)} 张表")
            logger.info(f"   📦 实体表 ({len(entity_tables)}): {', '.join(entity_tables) if entity_tables else '无'}")
            logger.info(f"   🔗 关系表 ({len(relation_tables)}): {', '.join(relation_tables) if relation_tables else '无'}")
            
            # 对每张表分别提取数据（先实体表，再关系表）
            extracted_data = {}
            all_llm_responses = []
            table_idx = 0
            total_tables = len(all_table_names)
            
            # 阶段1：提取实体表
            if entity_tables:
                logger.info(f"\n{'='*60}")
                logger.info(f"📦 阶段1: 提取 {len(entity_tables)} 个实体表")
                logger.info(f"{'='*60}")
                
                for table_name in entity_tables:
                    table_idx += 1
                    logger.info(f"\n🔹 [{table_idx}/{total_tables}] 实体表: {table_name}")
                    
                    table_data, llm_response = self.extract_with_langchain(
                        merged_content, schema, table_name
                    )
                    
                    # 合并到总结果中
                    extracted_data.update(table_data)
                    all_llm_responses.append(f"=== Entity Table: {table_name} ===\n{llm_response}\n")
                    
                    # 短暂延迟，避免API限流
                    if table_idx < total_tables:
                        time.sleep(1)
            
            # 阶段2：提取关系表（使用已提取的实体数据或参考表数据）
            if relation_tables:
                logger.info(f"\n{'='*60}")
                logger.info(f"🔗 阶段2: 提取 {len(relation_tables)} 个关系表")
                logger.info(f"{'='*60}")
                
                for table_name in relation_tables:
                    table_idx += 1
                    logger.info(f"\n🔹 [{table_idx}/{total_tables}] 关系表: {table_name}")
                    
                    table_def = self._get_table_definition(schema, table_name)
                    reference_tables_data = self._load_reference_tables(table_def, case_info['case_dir']) if table_def else {}
                    
                    table_data, llm_response = self.extract_relation_with_langchain(
                        merged_content, schema, table_name, reference_tables_data,
                        extracted_entity_data=extracted_data,
                        case_dir=case_info['case_dir']
                    )
                    
                    # 合并到总结果中
                    extracted_data.update(table_data)
                    all_llm_responses.append(f"=== Relation Table: {table_name} ===\n{llm_response}\n")
                    
                    # 短暂延迟，避免API限流
                    if table_idx < total_tables:
                        time.sleep(1)
            
            # 合并所有表的LLM响应
            llm_response = "\n".join(all_llm_responses)
            
            logger.info(f"\n{'='*60}")
            total_rows = sum(len(rows) for rows in extracted_data.values())
            logger.info(f"✅ 完成所有表的提取，共 {len(extracted_data)} 张表，总计 {total_rows} 行数据")
            logger.info(f"   📦 实体表: {', '.join(entity_tables) if entity_tables else '无'}")
            logger.info(f"   🔗 关系表: {', '.join(relation_tables) if relation_tables else '无'}")
            logger.info(f"{'='*60}")
            
            logger.info("💾 保存处理结果...")
            output_dir = self.save_output(case_info, extracted_data, llm_response)
            result['output_dir'] = str(output_dir)
            
            result['status'] = 'success'
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            
            logger.info(f"\n✅ {case_id} 处理成功！")
            logger.info(f"⏱️  耗时: {int(result['duration'])}秒")
            logger.info("📊 已跳过评估，仅保留抽取结果")
            
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            logger.error(f"\n❌ {case_id} 处理失败: {e}")
            import traceback
            traceback.print_exc()
        
        return result
    
    def run_evaluation(self):
        """运行完整的评估流程"""
        logger.info("\n" + "="*80)
        logger.info("🚀 LangChain 抽取能力评估开始")
        logger.info("="*80 + "\n")
        
        logger.info("📁 扫描数据集...")
        cases = self.scan_datasets()
        
        if not cases:
            logger.error("❌ 未找到有效的测试案例")
            return
        
        for i, case_info in enumerate(cases, 1):
            logger.info(f"\n{'#'*80}")
            logger.info(f"# 进度: {i}/{len(cases)}")
            logger.info(f"{'#'*80}")
            
            result = self.process_case(case_info)
            self.results.append(result)
            
            time.sleep(1)
        
    
   


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='LangChain 抽取能力评测系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法:
  # 使用默认模型（会跳过已处理的案例）
  python langchain_evaluate.py
  
  # 指定模型
  python langchain_evaluate.py --model gpt-4o

  # 只跑指定数据库
  python langchain_evaluate.py --data-name education

  # 只跑指定数据库下的指定 case
  python langchain_evaluate.py --data-name education --case-name case2 --model gpt-5.4
  
  # 强制重新运行所有案例
  python langchain_evaluate.py --force-rerun
  
  # 指定 API 配置
  python langchain_evaluate.py --model gpt-4o --api-base https://api.openai.com/v1
  
  # 指定chunk大小（处理长文档）
  python langchain_evaluate.py --chunk-size 10000 --chunk-overlap 1000
  
  # 完整示例
  python langchain_evaluate.py --model gpt-4o --run-name baseline_test --chunk-size 20000 --force-rerun
        '''
    )
    
    parser.add_argument('--model', type=str, default=DEFAULT_MODEL,
                        help=f'使用的模型名称（默认: {DEFAULT_MODEL}）')
    parser.add_argument('--run-name', type=str, default=None,
                        help='运行名称，用于区分不同的实验（可选）')
    parser.add_argument('--api-base', type=str, default=None,
                        help='API Base URL（可选，默认从环境变量读取）')
    parser.add_argument('--api-key', type=str, default=None,
                        help='API Key（可选，默认从环境变量读取）')
    parser.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f'文档分chunk的大小（字符数，默认: {DEFAULT_CHUNK_SIZE}）')
    parser.add_argument('--chunk-overlap', type=int, default=DEFAULT_CHUNK_OVERLAP,
                        help=f'chunk之间的重叠部分（字符数，默认: {DEFAULT_CHUNK_OVERLAP}）')
    parser.add_argument('--force-rerun', action='store_true',
                        help='强制重新运行所有案例，即使已经处理过（默认: False，会跳过已处理的案例）')
    parser.add_argument('--data-name', '--db-name', dest='data_name', type=str, default=None,
                        help='仅处理指定数据库（例如: education）')
    parser.add_argument('--case-name', type=str, default=None,
                        help='仅处理指定案例（例如: case2）')
    
    args = parser.parse_args()
    
    print("\n" + "="*80)
    print("🌟 LangChain 抽取能力评测系统")
    print("="*80)
    print(f"🤖 模型: {args.model}")
    if args.run_name:
        print(f"🏷️  运行名称: {args.run_name}")
    if args.api_base:
        print(f"🌐 API Base: {args.api_base}")
    print(f"📏 Chunk大小: {args.chunk_size} 字符, 重叠: {args.chunk_overlap} 字符")
    if args.force_rerun:
        print(f"🔄 模式: 强制重新运行所有案例")
    else:
        print(f"⏭️  模式: 跳过已处理的案例")
    if args.data_name:
        print(f"🗂️  数据库过滤: {args.data_name}")
    if args.case_name:
        print(f"🧩 案例过滤: {args.case_name}")
    print("="*80 + "\n")
    
    evaluator = LangChainExtractorEvaluator(
        model=args.model,
        run_name=args.run_name,
        api_base=args.api_base,
        api_key=args.api_key,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        force_rerun=args.force_rerun,
        data_name=args.data_name,
        case_name=args.case_name
    )
    
    try:
        evaluator.run_evaluation()
    except KeyboardInterrupt:
        print("\n\n⚠️  评估被用户中断")
    except Exception as e:
        print(f"\n\n❌ 评估过程出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
