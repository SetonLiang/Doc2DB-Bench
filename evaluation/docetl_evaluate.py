#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DocETL 抽取能力评测脚本
使用 DocETL 进行结构化数据抽取
"""

import os
import sys
import json
import time
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional, Set

# 添加项目根目录到 Python 路径，以便导入 backend 模块
project_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, project_root)

# 加载环境变量 (从 llm/.env 文件)
from utils import token_length
from dotenv import dotenv_values
from utils import _clean_response
env_path = Path(__file__).parent.parent/ "llm" / ".env"
config = dotenv_values(env_path)
DEFAULT_API_URL = config.get("GPT_URL_PRO")
DEFAULT_API_KEY = config.get("GPT_KEY_PRO")

# print(DEFAULT_API_KEY, DEFAULT_API_URL)
# exit()
# DocETL imports
from docetl.api import (
    Pipeline, Dataset, MapOp, ReduceOp, UnnestOp,
    PipelineStep, PipelineOutput
)
DOCETL_AVAILABLE = True
# except ImportError:
#     print("⚠️  警告: docetl 库未安装，请运行 'pip install docetl'")
#     DOCETL_AVAILABLE = False



# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 配置参数
DATASET_DIR = Path(__file__).parent.parent / "dataset/case/base_latest"
OUTPUT_DIR = Path(__file__).parent.parent / "dataset/case/base_latest_output/docetl"
DEFAULT_MODEL = "gpt-4o"

class DocETLExtractorEvaluator:
    """DocETL 抽取能力评估器"""
    
    def __init__(self, model: str = DEFAULT_MODEL, dataset_dir: Path = DATASET_DIR,
                 output_dir: Path = OUTPUT_DIR, run_name: Optional[str] = None,
                 api_base: Optional[str] = DEFAULT_API_URL, api_key: Optional[str] = DEFAULT_API_KEY,
                 force_rerun: bool = False,
                 db_names: Optional[List[str]] = None,
                 case_names: Optional[List[str]] = None):
        
        if not DOCETL_AVAILABLE:
            raise ImportError("docetl 库未安装，请运行 'pip install docetl'")
        
        self.model = model
        self.dataset_dir = dataset_dir
        self.output_dir = output_dir
        self.run_name = run_name
        self.force_rerun = force_rerun
        self.results = []
        self.db_name_filter: Optional[Set[str]] = (
            {x.strip() for x in db_names if x and x.strip()} if db_names else None
        )
        self.case_name_filter: Optional[Set[str]] = (
            {x.strip() for x in case_names if x and x.strip()} if case_names else None
        )
        
        # API 配置
        if api_base is None:
            api_base = DEFAULT_API_URL
        if api_key is None:
            api_key = DEFAULT_API_KEY
        
        # 修复 URL：DocETL 内部使用 OpenAI 客户端，会自动添加 /chat/completions
        # 所以如果 URL 包含这个后缀，需要去掉以避免重复
        if api_base:
            original_url = api_base
            # 移除末尾的 /chat/completions 如果存在（支持带或不带末尾斜杠）
            api_base = api_base.rstrip('/')
            if api_base.endswith('/chat/completions'):
                api_base = api_base[:-len('/chat/completions')].rstrip('/')
                logger.info(f"🔧 已修复 API Base URL: {original_url} -> {api_base}")
            # 确保 URL 末尾没有斜杠，避免路径拼接问题
            api_base = api_base.rstrip('/')
        
        self.api_base = api_base
        self.api_key = api_key
        
        # 设置环境变量供 DocETL 使用
        # 注意：OpenAI 客户端使用 OPENAI_BASE_URL 而不是 OPENAI_API_BASE
        if self.api_key:
            os.environ["OPENAI_API_KEY"] = self.api_key
        if self.api_base:
            os.environ["OPENAI_BASE_URL"] = self.api_base
        # print(self.api_base, self.api_key)
        # exit()
        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 输出目录: {self.output_dir}")
        logger.info(f"🤖 模型: {model}")
        if api_base:
            logger.info(f"🌐 API Base: {api_base}")
        if api_key:
            logger.info(f"🔑 API Key: {api_key[:20]}..." if len(api_key) > 20 else "已设置")
        if run_name:
            logger.info(f"🏷️  运行名称: {run_name}")
        if self.db_name_filter:
            logger.info(f"🗂️  数据库过滤: {', '.join(sorted(self.db_name_filter))}")
        if self.case_name_filter:
            logger.info(f"🧪 Case过滤: {', '.join(sorted(self.case_name_filter))}")
        if force_rerun:
            logger.info(f"🔄 强制重新运行模式：将重新处理所有案例")
        else:
            logger.info(f"⏭️  跳过模式：已处理的案例将被跳过")
        
        # 验证环境变量设置
        logger.info(f"✅ DocETL 环境变量已配置:")
        logger.info(f"   OPENAI_API_KEY: {'已设置' if os.environ.get('OPENAI_API_KEY') else '未设置'}")
        logger.info(f"   OPENAI_BASE_URL: {os.environ.get('OPENAI_BASE_URL', '未设置')}")

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
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                with open(extracted_file, 'r', encoding='utf-8') as f:
                    extracted_data = json.load(f)
                
                if metadata.get('model') == self.model and metadata.get('method') == 'docetl':
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
            with open(method_dir / "metadata.json", 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            with open(method_dir / "extracted_data.json", 'r', encoding='utf-8') as f:
                extracted_data = json.load(f)
            
            expected_answer = self.load_expected_answer(case_info)
            
            cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
            llm_evaluation = self.evaluate_with_llm(extracted_data, expected_answer, case_info)
            
            evaluation = {
                "cell_recall": cell_metrics['cell_recall'],
                "cell_precision": cell_metrics['cell_precision'],
                "cell_f1": cell_metrics['cell_f1'],
                "total_expected_cells": cell_metrics['total_expected_cells'],
                "total_generated_cells": cell_metrics['total_generated_cells'],
                "total_correct_cells": cell_metrics['total_correct_cells'],
                "llm_score": llm_evaluation.get('overall', 0),
                "llm_completeness": llm_evaluation.get('completeness', 0),
                "llm_accuracy": llm_evaluation.get('accuracy', 0),
                "llm_consistency": llm_evaluation.get('consistency', 0),
                "llm_coverage": llm_evaluation.get('coverage', 0),
                "llm_comments": llm_evaluation.get('comments', 'N/A')
            }
            
            result = {
                'case_id': f"{case_info['db_name']}/{case_info['case_name']}",
                'db_name': case_info['db_name'],
                'case_name': case_info['case_name'],
                'model': self.model,
                'method': 'docetl',
                'run_name': self.run_name,
                'status': 'success',
                'start_time': metadata.get('timestamp'),
                'end_time': metadata.get('timestamp'),
                'duration': 0,
                'evaluation': evaluation,
                'output_dir': str(method_dir),
                'error': None,
                'from_cache': True
            }
            
            logger.info(f"  ✓ 已加载缓存结果: Cell召回={evaluation['cell_recall']:.2f}%, LLM分数={evaluation['llm_score']:.1f}")
            
            return result
            
        except Exception as e:
            logger.error(f"  ✗ 加载已有结果失败: {e}")
            return None
    
    def scan_datasets(self) -> List[Dict[str, Any]]:
        """扫描所有数据集和case"""
        cases = []
        
        if not self.dataset_dir.exists():
            logger.error(f"❌ 数据集目录不存在: {self.dataset_dir}")
            return cases
        
        for db_dir in self.dataset_dir.iterdir():
            if not db_dir.is_dir():
                continue
            
            db_name = db_dir.name
            if self.db_name_filter and db_name not in self.db_name_filter:
                continue
            logger.info(f"📁 扫描数据库: {db_name}")
            
            for case_dir in db_dir.iterdir():
                if not case_dir.is_dir() or not case_dir.name.startswith("case"):
                    continue
                
                case_name = case_dir.name
                if self.case_name_filter and case_name not in self.case_name_filter:
                    continue
                
                docs_dir = case_dir / "docs"
                schema_file = case_dir / "schema.json"
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
                    logger.warning(f"⚠️  {db_name}/{case_name}: schema.json不存在，跳过")
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
        """将 answer/ 下单个 json 转为 {表名: rows} 片段。"""
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
            logger.warning(
                f"  ⚠️  跳过 {jf.name}：tables 模式下未解析出 rows 或可合并的表数据"
            )
            return {}
        logger.warning(f"  ⚠️  跳过 {jf.name}：根类型需为 object 或 array")
        return {}

    def _merge_multi_table_json_dir(self, path: Path, mode: str) -> Dict[str, Any]:
        """合并目录下多个 *.json 为 expected_answer 字典。mode 为 "answer" 或 "tables"。"""
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
                    logger.warning(
                        f"  ⚠️  表 {k} 在多个文件中出现，后读入文件将覆盖先前内容: {jf.name}"
                    )
                merged[k] = v

        if not merged:
            label = "tables/" if mode == "tables" else "answer/"
            raise ValueError(f"{label} 目录无有效表数据: {path}")
        return merged

    def load_expected_answer(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """加载标准答案：支持 answer.json、answers/、answer/、tables/ 四种来源。"""
        src = case_info.get("answer_source", "file")
        path: Path = case_info.get("answer_path", case_info.get("answer_file"))
        if src == "file":
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
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
    
    def create_output_schema(self, schema: Dict[str, Any], table_name: str) -> Dict[str, str]:
        """从 schema 创建 DocETL 输出格式"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        
        output_schema = {}
        
        for attr in attributes:
            field_name = attr['name']
            field_type = attr.get('type', 'VARCHAR')
            
            if 'VARCHAR' in field_type or 'TEXT' in field_type or 'ENUM' in field_type or 'string' in field_type.lower():
                docetl_type = "string"
            elif 'INT' in field_type or 'integer' in field_type.lower():
                docetl_type = "int"
            elif 'DECIMAL' in field_type or 'FLOAT' in field_type or 'number' in field_type.lower():
                docetl_type = "float"
            elif 'BOOLEAN' in field_type or 'boolean' in field_type.lower():
                docetl_type = "boolean"
            elif 'DATE' in field_type or 'date' in field_type.lower():
                docetl_type = "string"
            else:
                docetl_type = "string"
            
            output_schema[field_name] = docetl_type
        
        logger.info(f"  ✓ 创建 DocETL 输出 schema: {table_name}")
        logger.info(f"    字段数量: {len(output_schema)}")
        
        return output_schema
    
    def build_extraction_prompt(self, schema: Dict[str, Any], table_name: str) -> str:
        """构建提取数据的 prompt"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        table_description = table_def.get('description', '')
        
        prompt_parts = [
            f"You are an expert in structured data extraction from documents.",
            f"",
            f"TASK: Extract ALL instances of {table_name} entities from the following text.",
            f"",
        ]
        
        if table_description:
            prompt_parts.append(f"Table Description: {table_description}")
            prompt_parts.append("")
        
        prompt_parts.append("Schema Definition:")
        for attr in attributes:
            attr_name = attr['name']
            attr_type = attr.get('type', 'VARCHAR')
            attr_desc = attr.get('description', '')
            constraints = attr.get('constraints', {})
            
            constraint_info = []
            if constraints.get('primary_key'):
                constraint_info.append('PRIMARY KEY')
            if constraints.get('foreign_key'):
                constraint_info.append('FOREIGN KEY')
            if constraints.get('unique'):
                constraint_info.append('UNIQUE')
            if not constraints.get('nullable', True):
                constraint_info.append('NOT NULL')
            
            constraint_str = f" [{', '.join(constraint_info)}]" if constraint_info else ""
            prompt_parts.append(f"  - {attr_name} ({attr_type}){constraint_str}: {attr_desc}")
        
        prompt_parts.extend([
            "",
            "IMPORTANT INSTRUCTIONS:",
            "1. Extract EVERY occurrence that matches the schema from the text",
            "2. Return a list of all extracted records",
            "3. Even if some fields are missing, extract the record with available fields",
            "4. Look for data in various formats (tables, lists, paragraphs, etc.)",
            "5. Extract exact values from the text without modification",
            "6. For missing fields, use null or omit them",
            "7. Pay special attention to field constraints",
            "",
            "Text to extract from:",
            "{{ input.content }}"
        ])
        
        return "\n".join(prompt_parts)
    
    def build_relation_extraction_prompt(self, schema: Dict[str, Any], table_name: str,
                                        reference_tables_data: Dict[str, List[Dict[str, Any]]]) -> str:
        """构建关系表提取的 prompt"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        table_description = table_def.get('description', '')
        relation_config = table_def.get('relation_extraction', {})
        
        prompt_parts = [
            "You are an expert in structured data extraction, specializing in relationship extraction.",
            "",
            f"TASK: Extract ALL relationship instances for the {table_name} table from the following text.",
            "",
        ]
        
        if table_description:
            prompt_parts.append(f"Table Description: {table_description}")
            prompt_parts.append("")
        
        if reference_tables_data:
            prompt_parts.append("【Reference Tables for Relationship Extraction】")
            
            extraction_hint = relation_config.get('extraction_hint', '')
            if extraction_hint:
                prompt_parts.append(f"Task: {extraction_hint}")
                prompt_parts.append("")
            
            for ref_table_name, table_data in reference_tables_data.items():
                if not table_data:
                    continue
                
                prompt_parts.append(f"{ref_table_name} Table:")
                fields = list(table_data[0].keys())
                
                header = "  | " + " | ".join(fields) + " |"
                separator = "  |" + "|".join(["-" * (len(f) + 2) for f in fields]) + "|"
                prompt_parts.append(header)
                prompt_parts.append(separator)
                
                max_rows = min(50, len(table_data))
                for row in table_data[:max_rows]:
                    values = [str(row.get(f, '')) for f in fields]
                    data_row = "  | " + " | ".join(values) + " |"
                    prompt_parts.append(data_row)
                
                if len(table_data) > max_rows:
                    prompt_parts.append(f"  ... (total {len(table_data)} rows)")
                
                prompt_parts.append("")
        
        prompt_parts.append("Schema Definition:")
        for attr in attributes:
            attr_name = attr['name']
            attr_type = attr.get('type', 'VARCHAR')
            attr_desc = attr.get('description', '')
            constraints = attr.get('constraints', {})
            
            constraint_info = []
            if constraints.get('primary_key'):
                constraint_info.append('PRIMARY KEY')
            if constraints.get('foreign_key'):
                constraint_info.append('FOREIGN KEY')
            if constraints.get('unique'):
                constraint_info.append('UNIQUE')
            if not constraints.get('nullable', True):
                constraint_info.append('NOT NULL')
            
            constraint_str = f" [{', '.join(constraint_info)}]" if constraint_info else ""
            prompt_parts.append(f"  - {attr_name} ({attr_type}){constraint_str}: {attr_desc}")
        
        prompt_parts.extend([
            "",
            "IMPORTANT INSTRUCTIONS FOR RELATIONSHIP EXTRACTION:",
            "1. Extract EVERY occurrence that matches the schema",
            "2. Use the reference tables to map entity names/descriptions to their correct IDs",
            "3. For foreign key fields, use the EXACT ID values from the reference tables, NOT the entity names",
            "4. Extract ONLY relationships explicitly mentioned in the document",
            "5. Each row represents ONE relationship between entities",
            "6. Return a list of all extracted relationship records",
            "",
            "Text to extract from:",
            "{{ input.content }}"
        ])
        
        return "\n".join(prompt_parts)
    
    def build_relation_extraction_prompt_inline(self, schema: Dict[str, Any], table_name: str,
                                               reference_tables_data: Dict[str, List[Dict[str, Any]]]) -> str:
        """构建关系表提取的 prompt（参考表数据嵌入，text使用模板占位符）"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        table_description = table_def.get('description', '')
        relation_config = table_def.get('relation_extraction', {})
        
        prompt_parts = [
            "You are an expert in structured data extraction, specializing in relationship extraction.",
            "",
            f"TASK: Extract ALL relationship instances for the {table_name} table from the following text.",
            "",
        ]
        
        if table_description:
            prompt_parts.append(f"Table Description: {table_description}")
            prompt_parts.append("")
        
        if reference_tables_data:
            prompt_parts.append("【Reference Tables for Relationship Extraction】")
            
            extraction_hint = relation_config.get('extraction_hint', '')
            if extraction_hint:
                prompt_parts.append(f"Task: {extraction_hint}")
                prompt_parts.append("")
            
            for ref_table_name, table_data in reference_tables_data.items():
                if not table_data:
                    continue
                
                prompt_parts.append(f"{ref_table_name} Table:")
                fields = list(table_data[0].keys())
                
                header = "  | " + " | ".join(fields) + " |"
                separator = "  |" + "|".join(["-" * (len(f) + 2) for f in fields]) + "|"
                prompt_parts.append(header)
                prompt_parts.append(separator)
                
                max_rows = min(50, len(table_data))
                for row in table_data[:max_rows]:
                    values = [str(row.get(f, '')) for f in fields]
                    data_row = "  | " + " | ".join(values) + " |"
                    prompt_parts.append(data_row)
                
                if len(table_data) > max_rows:
                    prompt_parts.append(f"  ... (total {len(table_data)} rows)")
                
                prompt_parts.append("")
            
            prompt_parts.extend([
                "IMPORTANT Instructions for Relationship Extraction:",
                "- Use the reference tables above to match entity names/descriptions to their IDs",
                "- For foreign key fields, MUST use the exact ID values from the reference tables",
                "- Extract ONLY the relationships that are explicitly mentioned in the document",
                ""
            ])
        
        prompt_parts.append("Schema Definition:")
        for attr in attributes:
            attr_name = attr['name']
            attr_type = attr.get('type', 'VARCHAR')
            attr_desc = attr.get('description', '')
            constraints = attr.get('constraints', {})
            
            constraint_info = []
            if constraints.get('primary_key'):
                constraint_info.append('PRIMARY KEY')
            if constraints.get('foreign_key'):
                constraint_info.append('FOREIGN KEY')
            if constraints.get('unique'):
                constraint_info.append('UNIQUE')
            if not constraints.get('nullable', True):
                constraint_info.append('NOT NULL')
            
            constraint_str = f" [{', '.join(constraint_info)}]" if constraint_info else ""
            prompt_parts.append(f"  - {attr_name} ({attr_type}){constraint_str}: {attr_desc}")
        
        prompt_parts.extend([
            "",
            "CRITICAL INSTRUCTIONS:",
            "1. Extract EVERY relationship occurrence from the text below",
            "2. Use EXACT ID values from reference tables - NOT names or descriptions  ",
            "3. Match text mentions to reference table entries to find correct IDs",
            "4. Each record = ONE relationship between entities",
            "5. Return a list of all extracted records",
            "",
            "Text to extract from:",
            "{{ input.content }}"  # 使用Jinja2模板占位符，消除警告
        ])
        
        return "\n".join(prompt_parts)

    def extract_with_docetl(self, text: str, schema: Dict[str, Any], 
                           table_name: str, temp_dir: Path,
                           reference_tables_data: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> tuple[Dict[str, List[Dict[str, Any]]], str]:
        """使用 DocETL 提取数据
        
        Args:
            text: 要提取的文本内容
            schema: 数据库schema
            table_name: 表名
            temp_dir: 临时目录
            reference_tables_data: 参考表数据（用于关系表提取）
        """
        
        try:
            output_schema = self.create_output_schema(schema, table_name)
            
            # 根据是否有参考表来构建不同的prompt
            if reference_tables_data:
                # 关系表提取：参考表数据嵌入到prompt，text使用模板占位符
                extraction_prompt = self.build_relation_extraction_prompt_inline(
                    schema, table_name, reference_tables_data
                )
                logger.info(f"  📝 使用关系表提取prompt（参考表已嵌入，text使用模板）")
            else:
                # 普通实体表提取
                extraction_prompt = self.build_extraction_prompt(schema, table_name)
            
            # 创建输入数据：所有情况都使用相同的格式
            input_file = temp_dir / f"input_{table_name}.json"
            input_data = [{"content": text, "table_name": table_name}]
            
            with open(input_file, 'w', encoding='utf-8') as f:
                json.dump(input_data, f, ensure_ascii=False, indent=2)
            
            output_file = temp_dir / f"output_{table_name}.json"
            intermediate_dir = temp_dir / "intermediate"
            intermediate_dir.mkdir(exist_ok=True)
            
            logger.info(f"  🤖 使用 DocETL ({self.model}) 提取表 {table_name}...")
            
            dataset = Dataset(type="file", path=str(input_file))
            
            # 构造正确的 schema 字符串格式（不带引号）
            schema_str = ", ".join([f"{k}: {v}" for k, v in output_schema.items()])
            
            map_op = MapOp(
                name=f"extract_{table_name}",
                type="map",
                prompt=extraction_prompt,
                output={"schema": {"extracted_rows": f"list[{{{schema_str}}}]"}},
                model=self.model,
                skip_on_error=False  # 改为 False 以便看到真实错误
            )
            
            unnest_op = UnnestOp(
                name=f"unnest_{table_name}",
                type="unnest",
                unnest_key="extracted_rows",
                keep_empty=False
            )
            
            step = PipelineStep(
                name="extraction_step",
                input="input_data",
                operations=[f"extract_{table_name}", f"unnest_{table_name}"]
            )
            
            pipeline_output = PipelineOutput(
                type="file",
                path=str(output_file),
                intermediate_dir=str(intermediate_dir)
            )
            
            pipeline = Pipeline(
                name=f"extract_{table_name}_pipeline",
                datasets={"input_data": dataset},
                operations=[map_op, unnest_op],
                steps=[step],
                output=pipeline_output,
                default_model=self.model
            )
            
            logger.info(f"  🚀 执行 DocETL pipeline...")
            cost = pipeline.run()
            logger.info(f"  ✓ Pipeline 执行完成，成本: ${cost:.4f}")
            
            if output_file.exists():
                with open(output_file, 'r', encoding='utf-8') as f:
                    output_data = json.load(f)
                
                rows = []
                for item in output_data:
                    # 🔧 修复：检查是否有 extracted_rows 包装层
                    if 'extracted_rows' in item and isinstance(item['extracted_rows'], dict):
                        # 如果 unnest 没有正常工作，手动展开 extracted_rows
                        row_data = item['extracted_rows']
                    else:
                        # 正常情况：过滤掉元数据字段
                        row_data = {k: v for k, v in item.items() 
                                   if k not in ['content', 'table_name', '_id']}
                    
                    if row_data:
                        rows.append(row_data)
                
                logger.info(f"  ✓ 成功提取 {len(rows)} 行数据")
                llm_response = f"DocETL Pipeline Execution\nCost: ${cost:.4f}\nExtracted {len(rows)} rows"
                
                return {table_name: rows}, llm_response
            else:
                logger.error(f"  ✗ 输出文件未生成: {output_file}")
                return {table_name: []}, "Output file not generated"
            
        except Exception as e:
            logger.error(f"  ✗ DocETL 提取失败: {e}")
            import traceback
            traceback.print_exc()
            return {table_name: []}, str(e)

    
    def _get_table_definition(self, schema: Dict[str, Any], table_name: str) -> Optional[Dict[str, Any]]:
        """获取表格定义"""
        tables = schema.get('tables', [])
        for table in tables:
            candidate_name = table.get('name') or table.get('table_name')
            if candidate_name == table_name:
                result = table.copy()
                if 'columns' in result and 'attributes' not in result:
                    result['attributes'] = result.pop('columns')
                if 'fields' in result and 'attributes' not in result:
                    result['attributes'] = result.pop('fields')
                return result
        
        if schema.get('table_name') == table_name and 'columns' in schema:
            return {'name': table_name, 'attributes': schema['columns']}
        
        if len(tables) == 1:
            result = tables[0].copy()
            if 'columns' in result and 'attributes' not in result:
                result['attributes'] = result.pop('columns')
            if 'fields' in result and 'attributes' not in result:
                result['attributes'] = result.pop('fields')
            return result
        
        return None

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
    
    def classify_tables_by_type(self, schema: Dict[str, Any]) -> Dict[str, List[str]]:
        """将表分类为实体表和关系表"""
        if not isinstance(schema, dict):
            return {'entity_tables': [], 'relation_tables': []}
        
        entity_tables = []
        relation_tables = []
        
        if 'tables' in schema and isinstance(schema['tables'], list):
            for table in schema['tables']:
                if not isinstance(table, dict):
                    continue
                
                table_name = table.get('name') or table.get('table_name')
                if not table_name:
                    continue
                table_type = table.get('type', 'entity').lower()
                
                relation_config = table.get('relation_extraction', {})
                has_relation_config = relation_config.get('enabled', False)
                
                if table_type in ['relation', 'relationship'] or has_relation_config:
                    relation_tables.append(table_name)
                else:
                    entity_tables.append(table_name)
        
        elif 'table_name' in schema or 'name' in schema:
            table_name = schema.get('name') or schema.get('table_name')
            table_type = schema.get('type', 'entity').lower()
            
            if table_type in ['relation', 'relationship']:
                relation_tables.append(table_name)
            else:
                entity_tables.append(table_name)
        
        return {'entity_tables': entity_tables, 'relation_tables': relation_tables}
    
    def _load_document_sources_for_relation(self, relation_config: Dict[str, Any], case_dir: Path) -> str:
        """从配置的document_sources加载关系表的专用文档"""
        document_sources = relation_config.get('document_sources', [])
        if not document_sources:
            logger.warning(f"  ⚠️ document_sources为空")
            return ""
        
        logger.info(f"  🎯 从document_sources加载 {len(document_sources)} 个文档")
        
        merged_content = []
        
        for doc_source in document_sources:
            try:
                doc_path = Path(doc_source)
                
                if not doc_path.is_absolute():
                    candidate1 = case_dir / doc_source
                    candidate2 = case_dir.parent.parent / doc_source
                    candidate3 = Path(__file__).parent.parent / doc_source
                    
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
        """加载关系抽取所需的参考表数据"""
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
                data_path = Path(data_source)
                if not data_path.is_absolute():
                    candidate1 = case_dir / data_source
                    candidate2 = case_dir.parent.parent / data_source
                    candidate3 = Path(__file__).parent.parent / data_source
                    
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
    
    def evaluate_with_llm(self, generated: Dict[str, Any], expected: Dict[str, Any], 
                          case_info: Dict[str, Any]) -> Dict[str, Any]:
        """使用LLM评估生成结果 (已禁用)"""
        logger.info(f"  ℹ️  LLM评估已禁用，返回默认值")
        return {
            "completeness": 0.0,
            "accuracy": 0.0,
            "consistency": 0.0,
            "coverage": 0.0,
            "overall": 0.0,
            "comments": "LLM评估已禁用"
        }
    
    def save_output(self, case_info: Dict[str, Any], extracted_data: Dict[str, Any], 
                    llm_response: str) -> Path:
        """保存处理结果"""
        method_dir = self._get_case_output_dir(case_info)
        method_dir.mkdir(parents=True, exist_ok=True)
        
        metadata = {
            'model': self.model,
            'run_name': self.run_name,
            'method': 'docetl',
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'timestamp': datetime.now().isoformat()
        }
        
        with open(method_dir / "metadata.json", 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        with open(method_dir / "extracted_data.json", 'w', encoding='utf-8') as f:
            json.dump(extracted_data, f, ensure_ascii=False, indent=2)
        
        with open(method_dir / "pipeline_log.txt", 'w', encoding='utf-8') as f:
            f.write(llm_response)
        
        logger.info(f"  ✓ 结果已保存到: {method_dir}")
        return method_dir
    
    def process_case(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个测试案例"""
        case_id = f"{case_info['db_name']}/{case_info['case_name']}"
        logger.info(f"\n{'='*80}")
        logger.info(f"🔍 开始处理: {case_id}")
        logger.info(f"{'='*80}")
        
        # 检查是否需要跳过已处理的案例
        if not self.force_rerun and self.is_case_processed(case_info):
            result = self.load_existing_result(case_info)
            if result:
                return result
        
        result = {
            'case_id': case_id,
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'model': self.model,
            'method': 'docetl',
            'run_name': self.run_name,
            'status': 'failed',
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'duration': 0,
            'evaluation': None,
            'error': None
        }
        
        start_time = time.time()
        
        try:
            logger.info("📋 读取schema...")
            with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                schema = self.normalize_schema(json.load(f))
            
            logger.info("📋 读取标准答案...")
            expected_answer = self.load_expected_answer(case_info)
            
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
            
            # 创建临时目录用于DocETL处理
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                
                # 对每张表分别提取数据
                extracted_data = {}
                all_responses = []
                
                # 阶段1：提取实体表
                if entity_tables:
                    logger.info(f"\n{'='*60}")
                    logger.info(f"📦 阶段1: 提取 {len(entity_tables)} 个实体表")
                    logger.info(f"{'='*60}")
                    
                    for idx, table_name in enumerate(entity_tables, 1):
                        logger.info(f"\n🔹 [{idx}/{len(entity_tables)}] 实体表: {table_name}")
                        
                        table_data, response = self.extract_with_docetl(
                            merged_content, schema, table_name, temp_path
                        )
                        
                        extracted_data.update(table_data)
                        all_responses.append(f"Table: {table_name}\n{response}\n")
                
                # 阶段2：提取关系表
                if relation_tables:
                    logger.info(f"\n{'='*60}")
                    logger.info(f"🔗 阶段2: 提取 {len(relation_tables)} 个关系表")
                    logger.info(f"{'='*60}")
                    
                    for idx, table_name in enumerate(relation_tables, 1):
                        logger.info(f"\n🔹 [{idx}/{len(relation_tables)}] 关系表: {table_name}")
                        
                        # 获取表定义
                        table_def = self._get_table_definition(schema, table_name)
                        if not table_def:
                            logger.error(f"  ❌ 未找到表定义: {table_name}")
                            continue
                        
                        # 加载参考表数据
                        reference_tables_data = self._load_reference_tables(table_def, case_info['case_dir'])
                        
                        # 加载关系表的专用文档
                        relation_config = table_def.get('relation_extraction', {})
                        if relation_config.get('document_sources'):
                            relation_doc_content = self._load_document_sources_for_relation(
                                relation_config, case_info['case_dir']
                            )
                            if relation_doc_content:
                                text_to_extract = relation_doc_content
                            else:
                                text_to_extract = merged_content
                        else:
                            text_to_extract = merged_content
                        
                        # 提取关系表数据，传入参考表数据
                        table_data, response = self.extract_with_docetl(
                            text_to_extract, schema, table_name, temp_path,
                            reference_tables_data=reference_tables_data
                        )
                        
                        extracted_data.update(table_data)
                        all_responses.append(f"Table: {table_name}\n{response}\n")
            
            # 保存输出
            combined_response = "\n".join(all_responses)
            output_dir = self.save_output(case_info, extracted_data, combined_response)
            
            # 计算评估指标
            logger.info("\n📊 计算评估指标...")
            cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
            llm_evaluation = self.evaluate_with_llm(extracted_data, expected_answer, case_info)
            
            evaluation = {
                "cell_recall": cell_metrics['cell_recall'],
                "cell_precision": cell_metrics['cell_precision'],
                "cell_f1": cell_metrics['cell_f1'],
                "total_expected_cells": cell_metrics['total_expected_cells'],
                "total_generated_cells": cell_metrics['total_generated_cells'],
                "total_correct_cells": cell_metrics['total_correct_cells'],
                "llm_score": llm_evaluation.get('overall', 0),
                "llm_completeness": llm_evaluation.get('completeness', 0),
                "llm_accuracy": llm_evaluation.get('accuracy', 0),
                "llm_consistency": llm_evaluation.get('consistency', 0),
                "llm_coverage": llm_evaluation.get('coverage', 0),
                "llm_comments": llm_evaluation.get('comments', 'N/A')
            }
            
            end_time = time.time()
            duration = end_time - start_time
            
            result.update({
                'status': 'success',
                'end_time': datetime.now().isoformat(),
                'duration': duration,
                'evaluation': evaluation,
                'output_dir': str(output_dir),
                'error': None
            })
            
            logger.info(f"\n✅ 案例处理成功")
            logger.info(f"  ⏱️  耗时: {duration:.1f}秒")
            logger.info(f"  📊 Cell召回率: {evaluation['cell_recall']:.2f}%")
            logger.info(f"  📊 Cell准确率: {evaluation['cell_precision']:.2f}%")
            logger.info(f"  📊 Cell F1: {evaluation['cell_f1']:.2f}")
            logger.info(f"  📊 LLM分数: {evaluation['llm_score']:.1f}/100")
            
        except Exception as e:
            end_time = time.time()
            duration = end_time - start_time
            
            result.update({
                'status': 'failed',
                'end_time': datetime.now().isoformat(),
                'duration': duration,
                'error': str(e)
            })
            
            logger.error(f"\n❌ 案例处理失败: {e}")
            import traceback
            traceback.print_exc()
        
        return result
    
    def run_evaluation(self):
        """运行完整的评估流程"""
        logger.info("\n" + "="*80)
        logger.info("🚀 DocETL 抽取能力评估开始")
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
        
        logger.info("\n" + "="*80)
        logger.info("🎉 评估完成！")
        logger.info("="*80 + "\n")


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='DocETL 抽取能力评测系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法:
  # 使用默认模型
  python docetl_evaluate.py
  
  # 指定模型
  python docetl_evaluate.py --model gpt-4o
  
  # 强制重新运行所有案例
  python docetl_evaluate.py --force-rerun

  # 仅跑某些数据库 / case（支持逗号分隔）
  python docetl_evaluate.py --db-name transportation --model gpt-5.4 --case-name case1 
  
  # 完整示例
  python docetl_evaluate.py --model gpt-4o --run-name baseline_test --force-rerun
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
    parser.add_argument('--force-rerun', action='store_true',
                        help='强制重新运行所有案例（默认: 跳过已处理的案例）')
    parser.add_argument('--db-name', type=str, default=None,
                        help='仅处理指定数据库（可逗号分隔，如 education,finance）')
    parser.add_argument('--case-name', type=str, default=None,
                        help='仅处理指定case（可逗号分隔，如 case1,case2）')
    
    args = parser.parse_args()
    
    print("\n" + "="*80)
    print("🌟 DocETL 抽取能力评测系统")
    print("="*80)
    print(f"🤖 模型: {args.model}")
    db_names = [x.strip() for x in args.db_name.split(',')] if args.db_name else None
    case_names = [x.strip() for x in args.case_name.split(',')] if args.case_name else None
    if args.run_name:
        print(f"🏷️  运行名称: {args.run_name}")
    if db_names:
        print(f"🗂️  数据库过滤: {', '.join(db_names)}")
    if case_names:
        print(f"🧪 Case过滤: {', '.join(case_names)}")
    if args.api_base:
        print(f"🌐 API Base: {args.api_base}")
    if args.force_rerun:
        print(f"🔄 模式: 强制重新运行")
    else:
        print(f"⏭️  模式: 跳过已处理案例")
    print("="*80 + "\n")
    
    evaluator = DocETLExtractorEvaluator(
        model=args.model,
        run_name=args.run_name,
        api_base=args.api_base,
        api_key=args.api_key,
        force_rerun=args.force_rerun,
        db_names=db_names,
        case_names=case_names
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
