#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LangExtract 抽取能力评测脚本
使用 LangExtract 库进行结构化数据抽取
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional

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


# LangExtract imports
try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    print("⚠️  警告: langextract 库未安装，请运行 'pip install langextract'")
    LANGEXTRACT_AVAILABLE = False

# 导入 BAML 客户端（用于评估）


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
OUTPUT_DIR = Path(__file__).parent.parent / "dataset/case/base_latest_output/langextract"
DEFAULT_MODEL = "gpt-4o"


class LangExtractEvaluator:
    """LangExtract 抽取能力评估器"""
    
    def __init__(self, model: str = DEFAULT_MODEL, dataset_dir: Path = DATASET_DIR,
                 output_dir: Path = OUTPUT_DIR, run_name: Optional[str] = None,
                 api_base: Optional[str] = None, api_key: Optional[str] = None,
                 extraction_passes: int = 1, max_workers: int = 10,
                 data_name: Optional[str] = None, case_name: Optional[str] = None):
        
        if not LANGEXTRACT_AVAILABLE:
            raise ImportError("langextract 库未安装，请运行 'pip install langextract'")
        
        self.model = model
        self.dataset_dir = dataset_dir
        self.output_dir = output_dir
        self.run_name = run_name
        self.results = []
        
        # LangExtract 配置
        self.extraction_passes = extraction_passes
        self.max_workers = max_workers
        self.data_name_filter = data_name.strip() if isinstance(data_name, str) and data_name.strip() else None
        self.case_name_filter = case_name.strip() if isinstance(case_name, str) and case_name.strip() else None
        
        # API 配置
        if api_base is None:
            api_base = DEFAULT_API_URL
        if api_key is None:
            api_key = DEFAULT_API_KEY
        
        # 修复 URL：LangExtract 的 OpenAI provider 会自动添加 /chat/completions
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
        
        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 输出目录: {self.output_dir}")
        logger.info(f"🤖 模型: {model}")
        logger.info(f"🔄 抽取轮数: {extraction_passes}")
        if self.data_name_filter:
            logger.info(f"🗂️  数据库过滤: {self.data_name_filter}")
        if self.case_name_filter:
            logger.info(f"🧪 案例过滤: {self.case_name_filter}")
        if api_base:
            logger.info(f"🌐 API Base: {api_base}")
        if run_name:
            logger.info(f"🏷️  运行名称: {run_name}")

    def _get_case_output_dir(self, case_info: Dict[str, Any]) -> Path:
        """返回案例输出目录: OUTPUT_DIR / db_name / case_name / db_case_model"""
        return (
            self.output_dir
            / case_info['db_name']
            / case_info['case_name']
            / f"{case_info['db_name']}_{case_info['case_name']}_{self.model}"
        )
    
    def create_schema_description(self, schema: Dict[str, Any], table_name: str) -> str:
        """从 schema 创建描述文本"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        
        description_parts = [
            f"Extract structured data for table: {table_name}",
            "",
            "Schema Definition:",
        ]
        
        for attr in attributes:
            attr_desc = f"  - {attr['name']}"
            if 'type' in attr:
                attr_desc += f" ({attr['type']})"
            if 'description' in attr:
                attr_desc += f": {attr['description']}"
            
            # 添加约束信息
            constraint_dict = attr.get('constraints', {})
            constraints = []
            if constraint_dict.get('primary_key', False):
                constraints.append("PRIMARY KEY")
            if constraint_dict.get('foreign_key', False):
                constraints.append("FOREIGN KEY")
            if constraint_dict.get('unique', False):
                constraints.append("UNIQUE")
            if not constraint_dict.get('nullable', True):
                constraints.append("NOT NULL")
            
            if constraints:
                attr_desc += f" [{', '.join(constraints)}]"
            
            description_parts.append(attr_desc)
        
        description_parts.extend([
            "",
            "Extract ALL relevant records from the document.",
            "Each record should contain values for the specified attributes."
        ])
        
        return "\n".join(description_parts)
    
    def create_extraction_schema(self, schema: Dict[str, Any], table_name: str) -> Dict[str, Any]:
        """创建 LangExtract 使用的 schema 格式"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        
        # 将 schema 转换为 LangExtract 期望的格式
        extraction_schema = {
            "table_name": table_name,
            "attributes": []
        }
        
        for attr in attributes:
            attr_info = {
                "name": attr['name'],
                "type": attr.get('type', 'string')
            }
            if 'description' in attr:
                attr_info['description'] = attr['description']
            extraction_schema["attributes"].append(attr_info)
        
        return extraction_schema
    
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
            if self.data_name_filter and db_name != self.data_name_filter:
                continue
            logger.info(f"📁 扫描数据库: {db_name}")
            
            for case_dir in db_dir.iterdir():
                if not case_dir.is_dir() or not case_dir.name.startswith("case"):
                    continue
                
                case_name = case_dir.name
                if self.case_name_filter and case_name != self.case_name_filter:
                    continue
                
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
    
    def merge_documents(self, doc_files: List[str], max_length: int = 100000) -> str:
        """合并多个文档的内容，并限制总长度"""
        merged_content = []
        current_length = 0
        MAX_DOC_LENGTH = max_length  # 限制为约10万字符，避免 LangExtract 处理超长文档失败
        
        for i, doc_file in enumerate(doc_files, 1):
            content = self.read_document_content(doc_file)
            if content:
                # 检查是否会超过限制
                doc_header = f"=== Document {i}: {os.path.basename(doc_file)} ===\n"
                doc_footer = f"\n=== End of Document {i} ===\n\n"
                doc_total = len(doc_header) + len(content) + len(doc_footer)
                
                if current_length + doc_total > MAX_DOC_LENGTH:
                    # 如果添加完整文档会超过限制，则截断
                    remaining = MAX_DOC_LENGTH - current_length - len(doc_header) - len(doc_footer)
                    if remaining > 1000:  # 至少保留1000字符才有意义
                        merged_content.append(doc_header)
                        merged_content.append(content[:remaining] + "\n... [文档被截断] ...")
                        merged_content.append(doc_footer)
                        current_length = MAX_DOC_LENGTH
                        logger.warning(f"  ⚠️  文档 {os.path.basename(doc_file)} 被截断以避免超长")
                    logger.warning(f"  ⚠️  达到长度限制 ({MAX_DOC_LENGTH} 字符)，停止合并后续文档")
                    break
                else:
                    merged_content.append(doc_header)
                    merged_content.append(content)
                    merged_content.append(doc_footer)
                    current_length += doc_total
        
        final_content = "\n".join(merged_content)
        logger.info(f"  ✓ 合并完成，总长度: {len(final_content)} 字符")
        if len(final_content) >= MAX_DOC_LENGTH * 0.95:
            logger.warning(f"  ⚠️  文档接近或达到长度限制，可能影响提取完整性")
        return final_content
    
    def create_example_data(self, schema: Dict[str, Any], table_name: str) -> List[Any]:
        """创建示例数据用于 LangExtract"""
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            return []
        
        attributes = table_def.get('attributes', [])
        if not attributes:
            return []
        
        # 创建更真实的示例数据，根据字段类型和名称生成
        example_values = {}
        example_text_parts = []
        
        for attr in attributes:
            attr_name = attr['name']
            attr_type = attr.get('type', 'string').lower()
            attr_desc = attr.get('description', '')
            
            # 根据字段名称和类型生成更真实的示例值
            if 'name' in attr_name.lower() and 'company' in attr_name.lower():
                example_val = "Acme Corporation"
            elif 'name' in attr_name.lower():
                example_val = "John Smith"
            elif 'phone' in attr_name.lower() or 'telephone' in attr_name.lower():
                example_val = "(555) 123-4567"
            elif 'email' in attr_name.lower():
                example_val = "example@company.com"
            elif 'address' in attr_name.lower() or 'location' in attr_name.lower():
                example_val = "123 Main Street, New York, NY 10001"
            elif 'date' in attr_name.lower() or 'time' in attr_name.lower():
                example_val = "2024-01-15"
            elif 'year' in attr_name.lower():
                example_val = "2024"
            elif 'amount' in attr_name.lower() or 'price' in attr_name.lower() or 'revenue' in attr_name.lower():
                example_val = "1000000"
            elif 'int' in attr_type or 'number' in attr_type or 'decimal' in attr_type:
                example_val = "12345"
            elif 'bool' in attr_type:
                example_val = "true"
            else:
                example_val = f"Sample {attr_name}"
            
            example_values[attr_name] = example_val
            example_text_parts.append(f"{attr_name}: {example_val}")
        
        # 构建更自然的示例文本（只使用前几个字段）
        example_text = "Example: " + ", ".join(example_text_parts[:min(4, len(example_text_parts))])
        
        try:
            # 创建 Extraction 对象
            extraction = lx.data.Extraction(
                extraction_class=table_name,
                extraction_text=example_text.strip(),
                attributes=example_values
            )
            
            # 创建 ExampleData 对象
            example = lx.data.ExampleData(
                text=example_text.strip(),
                extractions=[extraction]
            )
            return [example]
        except Exception as e:
            logger.warning(f"  ⚠️  创建示例数据失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def extract_with_langextract(self, text: str, schema: Dict[str, Any], 
                                 table_name: str) -> tuple[Dict[str, List[Dict[str, Any]]], Any]:
        """使用 LangExtract 提取数据，带有容错机制"""
        
        try:
            # 创建 schema 描述
            schema_description = self.create_schema_description(schema, table_name)
            extraction_schema = self.create_extraction_schema(schema, table_name)
            
            # 构建 prompt
            prompt_description = f"""
You are an expert in structured data extraction from documents.

TASK: Extract ALL instances of {table_name} entities from the input text.

SCHEMA DEFINITION:
{schema_description}

IMPORTANT INSTRUCTIONS:
1. Extract EVERY occurrence that matches the schema
2. Even if some fields are missing, extract the record with available fields
3. Look for data in various formats (tables, lists, paragraphs, etc.)
4. Field values can be:
   - Explicitly stated (e.g., "Company Name: XYZ Corp")
   - Embedded in sentences (e.g., "XYZ Corp is located at...")
   - In structured formats (tables, forms)
5. Extract the exact values from the text without modification
6. If a field value is not found, you can omit it or set it to null
7. Pay special attention to field constraints (PRIMARY KEY, FOREIGN KEY, NOT NULL, UNIQUE)
   - PRIMARY KEY fields are critical identifiers and must be extracted
   - FOREIGN KEY fields should match values from related tables
   - NOT NULL fields are required and should not be empty
   - UNIQUE fields should have distinct values across records

OUTPUT FORMAT: Return structured records matching the schema with all available fields.
"""
            
            logger.info(f"  🤖 调用 LangExtract ({self.model})...")
            logger.info(f"  🔄 抽取轮数: {self.extraction_passes}")
            
            # 创建示例数据 (LangExtract 要求必须提供)
            examples = self.create_example_data(schema, table_name)
            if not examples:
                logger.warning("  ⚠️  未能创建示例数据，使用通用示例")
                # 创建一个最简单的通用示例
                try:
                    generic_extraction = lx.data.Extraction(
                        extraction_class=table_name,
                        extraction_text="Example entity",
                        attributes={"field": "value"}
                    )
                    examples = [lx.data.ExampleData(
                        text="Example entity with field value",
                        extractions=[generic_extraction]
                    )]
                except Exception as e:
                    logger.error(f"  ✗ 无法创建示例数据: {e}")
                    # LangExtract 要求必须有 examples，如果无法创建则返回空结果
                    return {table_name: []}, None
            
            logger.info(f"  📝 使用 {len(examples)} 个示例")
            
            # 配置 LangExtract 参数
            extract_params = {
                "text_or_documents": text,
                "prompt_description": prompt_description,
                "examples": examples,  # LangExtract 要求必须提供 examples
                "model_id": self.model,
                "language_model_type": lx.inference.OpenAILanguageModel,
                "extraction_passes": self.extraction_passes,
                "max_workers": self.max_workers,
                "fence_output": True,  # 需要保持为 True 以正确解析
                "use_schema_constraints": False  # 保持为 False 以提高灵活性
            }
            
            # 添加 API 配置
            if self.api_base or self.api_key:
                extract_params["language_model_params"] = {}
                if self.api_base:
                    extract_params["language_model_params"]["base_url"] = self.api_base
                if self.api_key:
                    extract_params["language_model_params"]["api_key"] = self.api_key
            
            # 调用 LangExtract，使用 try-except 包裹以捕获部分失败
            result = None
            try:
                result = lx.extract(**extract_params)
                logger.info(f"  ✓ LangExtract 完成，提取 {len(result.extractions)} 个实体")
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"  ⚠️  LangExtract 部分失败: {error_msg[:200]}")
                
                # 即使失败，尝试获取部分结果
                # 有些情况下 LangExtract 会在内部处理部分 batch 后失败
                # 我们尝试返回已处理的数据
                if 'result' in locals() and result is not None:
                    logger.info(f"  ℹ️  尽管有错误，仍成功提取了 {len(result.extractions)} 个实体")
                else:
                    logger.error(f"  ✗ LangExtract 完全失败，无法获取任何提取结果")
                    # 返回空结果而不是抛出异常
                    return {table_name: []}, None
            
            # 转换结果为标准格式（带容错机制）
            rows = []
            failed_extractions = 0
            
            # 按照 extraction_class 分组（如果有的话）
            # 否则将所有提取结果合并为行
            extraction_map = {}
            
            for idx, extraction in enumerate(result.extractions):
                try:
                    extraction_class = extraction.extraction_class or "entity"
                    extraction_text = extraction.extraction_text
                    
                    # 构建行数据
                    row_data = {}
                    
                    # 如果有 attributes，添加它们
                    if hasattr(extraction, 'attributes') and extraction.attributes:
                        for key, value in extraction.attributes.items():
                            try:
                                # 清理和标准化值
                                if value is None or value == "None":
                                    row_data[key] = None
                                elif isinstance(value, str):
                                    # 检查是否是字符串化的字典
                                    cleaned_value = value.strip()
                                    if cleaned_value.startswith('{') and cleaned_value.endswith('}'):
                                        # 尝试解析字符串化的字典
                                        try:
                                            import ast
                                            parsed = ast.literal_eval(cleaned_value)
                                            if isinstance(parsed, dict):
                                                # 如果解析成功，合并字典内容而不是保存字符串
                                                for inner_key, inner_value in parsed.items():
                                                    if inner_value is not None and inner_value != "None" and str(inner_value).strip():
                                                        row_data[inner_key] = inner_value if not (isinstance(inner_value, str) and not inner_value.strip()) else None
                                            else:
                                                row_data[key] = cleaned_value if cleaned_value else None
                                        except (ValueError, SyntaxError):
                                            # 解析失败，当作普通字符串
                                            row_data[key] = cleaned_value if cleaned_value else None
                                    else:
                                        row_data[key] = cleaned_value if cleaned_value else None
                                elif isinstance(value, (dict, list)):
                                    # 如果是字典或列表，直接保存
                                    row_data[key] = value
                                else:
                                    # 其他类型，转换为字符串
                                    row_data[key] = str(value) if value is not None else None
                            except Exception as attr_error:
                                # 单个属性处理失败，记录并继续
                                logger.debug(f"  ⚠️  处理属性 {key} 失败: {attr_error}")
                                continue
                    
                    # 如果没有足够的 attributes，尝试使用 extraction_text 作为主字段
                    if not row_data:
                        # 使用第一个 schema 字段作为主字段
                        table_def = self._get_table_definition(schema, table_name)
                        attributes = table_def.get('attributes', [])
                        if attributes:
                            first_attr = attributes[0]['name']
                            row_data[first_attr] = extraction_text
                    
                    # 过滤掉完全为空的记录
                    if row_data:
                        # 检查是否所有值都是空的
                        has_non_empty = False
                        for value in row_data.values():
                            if value is not None and str(value).strip():
                                has_non_empty = True
                                break
                        
                        if has_non_empty:
                            rows.append(row_data)
                
                except Exception as extraction_error:
                    # 单个提取结果处理失败，记录并继续处理下一个
                    failed_extractions += 1
                    logger.debug(f"  ⚠️  处理提取结果 #{idx} 失败: {extraction_error}")
                    continue
            
            if failed_extractions > 0:
                logger.warning(f"  ⚠️  有 {failed_extractions} 个提取结果处理失败，已跳过")
            
            logger.info(f"  ✓ 转换为 {len(rows)} 行数据")
            
            return {table_name: rows}, result
            
        except Exception as e:
            logger.error(f"  ✗ LangExtract 提取过程出现异常: {e}")
            import traceback
            traceback.print_exc()
            
            # 尝试检查是否有部分结果可以返回
            if 'result' in locals() and result is not None and hasattr(result, 'extractions'):
                logger.warning(f"  ⚠️  尽管出现异常，仍尝试返回部分提取的 {len(result.extractions)} 个实体")
                try:
                    # 尝试转换已有的结果
                    rows = []
                    for extraction in result.extractions:
                        try:
                            if hasattr(extraction, 'attributes') and extraction.attributes:
                                row_data = {}
                                for key, value in extraction.attributes.items():
                                    if value and str(value).strip():
                                        row_data[key] = value
                                if row_data:
                                    rows.append(row_data)
                        except:
                            continue
                    
                    if rows:
                        logger.info(f"  ✓ 成功恢复 {len(rows)} 行部分数据")
                        return {table_name: rows}, result
                except:
                    pass
            
            # 如果无法恢复任何数据，返回空结果
            logger.warning(f"  ⚠️  返回空结果")
            return {table_name: []}, None
    
    def extract_relation_with_langextract(self, text: str, schema: Dict[str, Any], 
                                          table_name: str, reference_tables_data: Dict[str, List[Dict[str, Any]]],
                                          extracted_entity_data: Dict[str, List[Dict]] = None,
                                          case_dir: Path = None) -> tuple[Dict[str, List[Dict[str, Any]]], Any]:
        """使用 LangExtract 提取关系表数据（带参考表），带有容错机制"""
        
        try:
            table_def = self._get_table_definition(schema, table_name)
            if not table_def:
                raise ValueError(f"未找到表格定义: {table_name}")
            
            relation_config = table_def.get('relation_extraction', {})
            
            # 🎯 如果配置了document_sources，直接读取指定文档
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
                attributes = table_def.get('attributes', [])
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
                return self.extract_with_langextract(text, schema, table_name)
            
            # 创建 schema 描述
            schema_description = self.create_schema_description(schema, table_name)
            
            # 构建参考表prompt
            reference_tables_prompt = self._build_reference_tables_prompt(reference_tables_data, relation_config)
            
            # 构建 prompt
            prompt_description = f"""
You are an expert in structured data extraction, specializing in relationship extraction.

TASK: Extract ALL relationship instances for the {table_name} table from the input text.

SCHEMA DEFINITION:
{schema_description}

{reference_tables_prompt}

IMPORTANT INSTRUCTIONS FOR RELATIONSHIP EXTRACTION:
1. Extract EVERY occurrence that matches the schema
2. Use the reference tables to map entity names/descriptions to their correct IDs
3. For foreign key fields, use the EXACT ID values from the reference tables, NOT the entity names
4. Extract ONLY the relationships that are explicitly mentioned in the document
5. Each row represents ONE relationship between entities
6. Look for data in various formats (tables, lists, paragraphs, etc.)
7. Even if some fields are missing, extract the record with available fields

OUTPUT FORMAT: Return structured records matching the schema with all available fields.
"""
            
            logger.info(f"  🤖 调用 LangExtract ({self.model}) [关系抽取]...")
            logger.info(f"  🔄 抽取轮数: {self.extraction_passes}")
            
            # 创建示例数据 (LangExtract 要求必须提供)
            examples = self.create_example_data(schema, table_name)
            if not examples:
                logger.warning("  ⚠️  未能创建示例数据，使用通用示例")
                try:
                    generic_extraction = lx.data.Extraction(
                        extraction_class=table_name,
                        extraction_text="Example relationship",
                        attributes={"field": "value"}
                    )
                    examples = [lx.data.ExampleData(
                        text="Example relationship with field value",
                        extractions=[generic_extraction]
                    )]
                except Exception as e:
                    logger.error(f"  ✗ 无法创建示例数据: {e}")
                    return {table_name: []}, None
            
            logger.info(f"  📝 使用 {len(examples)} 个示例")
            
            # 配置 LangExtract 参数
            extract_params = {
                "text_or_documents": text,
                "prompt_description": prompt_description,
                "examples": examples,
                "model_id": self.model,
                "language_model_type": lx.inference.OpenAILanguageModel,
                "extraction_passes": self.extraction_passes,
                "max_workers": self.max_workers,
                "fence_output": True,
                "use_schema_constraints": False
            }
            
            # 添加 API 配置
            if self.api_base or self.api_key:
                extract_params["language_model_params"] = {}
                if self.api_base:
                    extract_params["language_model_params"]["base_url"] = self.api_base
                if self.api_key:
                    extract_params["language_model_params"]["api_key"] = self.api_key
            
            # 调用 LangExtract
            result = None
            try:
                result = lx.extract(**extract_params)
                logger.info(f"  ✓ LangExtract 关系抽取完成，提取 {len(result.extractions)} 个关系")
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"  ⚠️  LangExtract 关系抽取部分失败: {error_msg[:200]}")
                
                if 'result' in locals() and result is not None:
                    logger.info(f"  ℹ️  尽管有错误，仍成功提取了 {len(result.extractions)} 个关系")
                else:
                    logger.error(f"  ✗ LangExtract 关系抽取完全失败，无法获取任何提取结果")
                    return {table_name: []}, None
            
            # 转换结果为标准格式（带容错机制）
            rows = []
            failed_extractions = 0
            
            for idx, extraction in enumerate(result.extractions):
                try:
                    row_data = {}
                    
                    if hasattr(extraction, 'attributes') and extraction.attributes:
                        for key, value in extraction.attributes.items():
                            try:
                                if value is None or value == "None":
                                    row_data[key] = None
                                elif isinstance(value, str):
                                    cleaned_value = value.strip()
                                    if cleaned_value.startswith('{') and cleaned_value.endswith('}'):
                                        try:
                                            import ast
                                            parsed = ast.literal_eval(cleaned_value)
                                            if isinstance(parsed, dict):
                                                for inner_key, inner_value in parsed.items():
                                                    if inner_value is not None and inner_value != "None" and str(inner_value).strip():
                                                        row_data[inner_key] = inner_value if not (isinstance(inner_value, str) and not inner_value.strip()) else None
                                            else:
                                                row_data[key] = cleaned_value if cleaned_value else None
                                        except (ValueError, SyntaxError):
                                            row_data[key] = cleaned_value if cleaned_value else None
                                    else:
                                        row_data[key] = cleaned_value if cleaned_value else None
                                elif isinstance(value, (dict, list)):
                                    row_data[key] = value
                                else:
                                    row_data[key] = str(value) if value is not None else None
                            except Exception as attr_error:
                                logger.debug(f"  ⚠️  处理属性 {key} 失败: {attr_error}")
                                continue
                    
                    if not row_data:
                        attributes = table_def.get('attributes', [])
                        if attributes:
                            first_attr = attributes[0]['name']
                            row_data[first_attr] = extraction.extraction_text
                    
                    if row_data:
                        has_non_empty = False
                        for value in row_data.values():
                            if value is not None and str(value).strip():
                                has_non_empty = True
                                break
                        
                        if has_non_empty:
                            rows.append(row_data)
                
                except Exception as extraction_error:
                    failed_extractions += 1
                    logger.debug(f"  ⚠️  处理关系提取结果 #{idx} 失败: {extraction_error}")
                    continue
            
            if failed_extractions > 0:
                logger.warning(f"  ⚠️  有 {failed_extractions} 个关系提取结果处理失败，已跳过")
            
            logger.info(f"  ✓ 转换为 {len(rows)} 行关系数据")
            
            return {table_name: rows}, result
            
        except Exception as e:
            logger.error(f"  ✗ LangExtract 关系提取过程出现异常: {e}")
            import traceback
            traceback.print_exc()
            
            if 'result' in locals() and result is not None and hasattr(result, 'extractions'):
                logger.warning(f"  ⚠️  尽管出现异常，仍尝试返回部分提取的 {len(result.extractions)} 个关系")
                try:
                    rows = []
                    for extraction in result.extractions:
                        try:
                            if hasattr(extraction, 'attributes') and extraction.attributes:
                                row_data = {}
                                for key, value in extraction.attributes.items():
                                    if value and str(value).strip():
                                        row_data[key] = value
                                if row_data:
                                    rows.append(row_data)
                        except:
                            continue
                    
                    if rows:
                        logger.info(f"  ✓ 成功恢复 {len(rows)} 行部分关系数据")
                        return {table_name: rows}, result
                except:
                    pass
            
            logger.warning(f"  ⚠️  返回空结果")
            return {table_name: []}, None
    
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
        
        if schema.get('table_name') == table_name and ('columns' in schema or 'fields' in schema or 'attributes' in schema):
            return {
                'name': table_name,
                'attributes': schema.get('attributes') or schema.get('columns') or schema.get('fields') or []
            }
        
        if len(tables) == 1:
            result = tables[0].copy()
            if 'columns' in result and 'attributes' not in result:
                result['attributes'] = result.pop('columns')
            if 'fields' in result and 'attributes' not in result:
                result['attributes'] = result.pop('fields')
            return result
        
        return None

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
                if not isinstance(table, dict):
                    continue
                
                table_name = table.get('name') or table.get('table_name')
                if not table_name:
                    continue
                table_type = table.get('type', 'entity').lower()
                
                # 检查是否有 relation_extraction 配置
                relation_config = table.get('relation_extraction', {})
                has_relation_config = relation_config.get('enabled', False)
                
                if table_type in ['relation', 'relationship'] or has_relation_config:
                    relation_tables.append(table_name)
                else:
                    entity_tables.append(table_name)
        
        elif 'table_name' in schema or 'name' in schema:
            table_name = schema.get('name') or schema.get('table_name')
            if not table_name:
                return {'entity_tables': [], 'relation_tables': []}
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
    
    # def evaluate_with_llm(self, generated: Dict[str, Any], expected: Dict[str, Any], 
    #                       case_info: Dict[str, Any]) -> Dict[str, Any]:
    #     """使用LLM评估生成结果"""
    #     try:
    #         client_name = get_client_name_for_model(self.model)
    #         baml_options = {
    #             "client_registry": {
    #                 "DefaultClient": client_name,
    #             }
    #         }
            
    #         logger.info(f"  📊 使用 {self.model} 模型进行评估")
            
    #         baml_result = baml_client.EvaluateExtraction(
    #             db_name=case_info['db_name'],
    #             case_name=case_info['case_name'],
    #             generated_result=json.dumps(generated, ensure_ascii=False, indent=2),
    #             expected_answer=json.dumps(expected, ensure_ascii=False, indent=2),
    #             baml_options=baml_options
    #         )
            
    #         llm_evaluation = {
    #             "completeness": baml_result.completeness,
    #             "accuracy": baml_result.accuracy,
    #             "consistency": baml_result.consistency,
    #             "coverage": baml_result.coverage,
    #             "overall": baml_result.overall,
    #             "comments": baml_result.comments
    #         }
            
    #         logger.info(f"  ✓ LLM 评估完成，分数: {llm_evaluation['overall']}")
    #         return llm_evaluation
    #             
    #     except Exception as e:
    #         logger.warning(f"  ⚠️  LLM评估失败: {e}")
    #         return {
    #             "completeness": 0.0,
    #             "accuracy": 0.0,
    #             "consistency": 0.0,
    #             "coverage": 0.0,
    #             "overall": 0.0,
    #             "comments": f"LLM评估失败: {str(e)}"
    #         }
    
    def save_output(self, case_info: Dict[str, Any], extracted_data: Dict[str, Any], 
                    langextract_result: Any) -> Path:
        """保存处理结果"""
        method_dir = self._get_case_output_dir(case_info)
        method_dir.mkdir(parents=True, exist_ok=True)
        
        # 保存元数据
        metadata = {
            'model': self.model,
            'run_name': self.run_name,
            'method': 'langextract',
            'extraction_passes': self.extraction_passes,
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'timestamp': datetime.now().isoformat()
        }
        
        with open(method_dir / "metadata.json", 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        # 保存抽取结果
        with open(method_dir / "extracted_data.json", 'w', encoding='utf-8') as f:
            json.dump(extracted_data, f, ensure_ascii=False, indent=2)
        
        # 保存 LangExtract 原始结果
        if langextract_result:
            try:
                extractions_data = []
                for extraction in langextract_result.extractions:
                    extraction_dict = {
                        'extraction_class': extraction.extraction_class,
                        'extraction_text': extraction.extraction_text,
                        'attributes': extraction.attributes if hasattr(extraction, 'attributes') else {}
                    }
                    if hasattr(extraction, 'char_interval') and extraction.char_interval:
                        extraction_dict['char_interval'] = {
                            'start_pos': extraction.char_interval.start_pos,
                            'end_pos': extraction.char_interval.end_pos
                        }
                    extractions_data.append(extraction_dict)
                
                langextract_data = {
                    'total_extractions': len(extractions_data),
                    'extractions': extractions_data
                }
                
                with open(method_dir / "langextract_result.json", 'w', encoding='utf-8') as f:
                    json.dump(langextract_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"  ⚠️  处理 LangExtract 原始结果失败: {e}")
        
        logger.info(f"  ✓ 结果已保存到: {method_dir}")
        return method_dir
    
    def process_case(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个测试案例"""
        case_id = f"{case_info['db_name']}/{case_info['case_name']}"
        logger.info(f"\n{'='*80}")
        logger.info(f"🔍 开始处理: {case_id}")
        logger.info(f"{'='*80}")
        
        # 检查输出文件是否已存在
        method_dir = self._get_case_output_dir(case_info)
        metadata_file = method_dir / "metadata.json"
        extracted_file = method_dir / "extracted_data.json"
        
        if metadata_file.exists() and extracted_file.exists():
            logger.info(f"⏭️  跳过: 结果文件已存在 - {method_dir}")
            
            # 尝试从已有文件中读取评估结果
            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                with open(extracted_file, 'r', encoding='utf-8') as f:
                    extracted_data = json.load(f)
                
                # 如果文件中有评估数据，则读取并返回
                result = {
                    'case_id': case_id,
                    'db_name': case_info['db_name'],
                    'case_name': case_info['case_name'],
                    'model': self.model,
                    'method': 'langextract',
                    'run_name': self.run_name,
                    'status': 'skipped',
                    'start_time': datetime.now().isoformat(),
                    'end_time': datetime.now().isoformat(),
                    'duration': 0,
                    'output_dir': str(method_dir),
                    'evaluation': None,
                    'error': None,
                    'note': '已跳过，使用已有结果文件'
                }
                
                # 尝试计算评估指标（如果有标准答案）
                try:
                    expected_answer = self.load_expected_answer(case_info)
                    
                    # 计算cell指标
                    cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
                    
                    # 使用LLM评估
                    # llm_evaluation = self.evaluate_with_llm(extracted_data, expected_answer, case_info)
                    
                    evaluation = {
                        "cell_recall": cell_metrics['cell_recall'],
                        "cell_precision": cell_metrics['cell_precision'],
                        "cell_f1": cell_metrics['cell_f1'],
                        "total_expected_cells": cell_metrics['total_expected_cells'],
                        "total_generated_cells": cell_metrics['total_generated_cells'],
                        "total_correct_cells": cell_metrics['total_correct_cells'],
                        # "llm_score": llm_evaluation.get('overall', 0),
                        # "llm_completeness": llm_evaluation.get('completeness', 0),
                        # "llm_accuracy": llm_evaluation.get('accuracy', 0),
                        # "llm_consistency": llm_evaluation.get('consistency', 0),
                        # "llm_coverage": llm_evaluation.get('coverage', 0),
                        # "llm_comments": llm_evaluation.get('comments', 'N/A')
                    }
                    result['evaluation'] = evaluation
                    
                    logger.info(f"  ✓ 从已有文件读取结果并重新评估")
                    logger.info(f"  📊 Cell召回率: {evaluation['cell_recall']:.2f}%")
                    logger.info(f"  📊 Cell准确率: {evaluation['cell_precision']:.2f}%")
                    # logger.info(f"  📊 LLM分数: {evaluation['llm_score']:.1f}/100")
                    
                except Exception as e:
                    logger.warning(f"  ⚠️  无法重新评估已有结果: {e}")
                
                return result
                
            except Exception as e:
                logger.warning(f"  ⚠️  读取已有文件失败: {e}，将重新处理")
        
        result = {
            'case_id': case_id,
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'model': self.model,
            'method': 'langextract',
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
            
            # 对每张表分别提取数据（先实体表，再关系表）
            extracted_data = {}
            all_langextract_results = []
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
                    
                    logger.info(f"🎯 使用 LangExtract 提取表 {table_name} 的数据...")
                    table_data, langextract_result = self.extract_with_langextract(
                        merged_content, schema, table_name
                    )
                    
                    # 合并到总结果中
                    extracted_data.update(table_data)
                    if langextract_result:
                        all_langextract_results.append({
                            'table_name': table_name,
                            'table_type': 'entity',
                            'result': langextract_result
                        })
                    
                    table_rows = sum(len(rows) for rows in table_data.values())
                    logger.info(f"  ✅ 实体表 {table_name} 提取完成: {table_rows} 行数据")
                    
                    # 表之间短暂延迟
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
                    
                    logger.info(f"🎯 使用 LangExtract 提取关系表 {table_name} 的数据...")
                    table_data, langextract_result = self.extract_relation_with_langextract(
                        merged_content, schema, table_name, reference_tables_data,
                        extracted_entity_data=extracted_data,
                        case_dir=case_info['case_dir']
                    )
                    
                    # 合并到总结果中
                    extracted_data.update(table_data)
                    if langextract_result:
                        all_langextract_results.append({
                            'table_name': table_name,
                            'table_type': 'relation',
                            'result': langextract_result
                        })
                    
                    table_rows = sum(len(rows) for rows in table_data.values())
                    logger.info(f"  ✅ 关系表 {table_name} 提取完成: {table_rows} 行数据")
                    
                    # 表之间短暂延迟
                    if table_idx < total_tables:
                        time.sleep(1)
            
            logger.info(f"\n{'='*60}")
            total_rows = sum(len(rows) for rows in extracted_data.values())
            logger.info(f"✅ 完成所有表的提取，共 {len(extracted_data)} 张表，总计 {total_rows} 行数据")
            logger.info(f"   📦 实体表: {', '.join(entity_tables) if entity_tables else '无'}")
            logger.info(f"   🔗 关系表: {', '.join(relation_tables) if relation_tables else '无'}")
            logger.info(f"{'='*60}")
            
            # 合并所有表的 LangExtract 结果（用于保存）
            langextract_result = all_langextract_results[0]['result'] if all_langextract_results else None
            
            logger.info("💾 保存处理结果...")
            output_dir = self.save_output(case_info, extracted_data, langextract_result)
            result['output_dir'] = str(output_dir)
            
            logger.info("📊 计算Cell级别的指标...")
            cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
            logger.info(f"  ✓ Cell召回率: {cell_metrics['cell_recall']:.2f}%")
            logger.info(f"  ✓ Cell准确率: {cell_metrics['cell_precision']:.2f}%")
            logger.info(f"  ✓ Cell F1分数: {cell_metrics['cell_f1']:.2f}")
            
            logger.info("🎯 使用LLM评估结果...")
            # llm_evaluation = self.evaluate_with_llm(extracted_data, expected_answer, case_info)
            
            evaluation = {
                "cell_recall": cell_metrics['cell_recall'],
                "cell_precision": cell_metrics['cell_precision'],
                "cell_f1": cell_metrics['cell_f1'],
                "total_expected_cells": cell_metrics['total_expected_cells'],
                "total_generated_cells": cell_metrics['total_generated_cells'],
                "total_correct_cells": cell_metrics['total_correct_cells'],
                # "llm_score": llm_evaluation.get('overall', 0),
                # "llm_completeness": llm_evaluation.get('completeness', 0),
                # "llm_accuracy": llm_evaluation.get('accuracy', 0),
                # "llm_consistency": llm_evaluation.get('consistency', 0),
                # "llm_coverage": llm_evaluation.get('coverage', 0),
                # "llm_comments": llm_evaluation.get('comments', 'N/A')
            }
            result['evaluation'] = evaluation
            
            result['status'] = 'success'
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            
            logger.info(f"\n✅ {case_id} 处理成功！")
            logger.info(f"⏱️  耗时: {int(result['duration'])}秒")
            logger.info(f"📊 评估结果:")
            logger.info(f"   - Cell召回率: {evaluation['cell_recall']:.2f}%")
            logger.info(f"   - Cell准确率: {evaluation['cell_precision']:.2f}%")
            # logger.info(f"   - LLM分数: {evaluation['llm_score']:.1f}/100")
            
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
        logger.info("🚀 LangExtract 抽取能力评估开始")
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
        
    #     logger.info("\n" + "="*80)
    #     logger.info("📊 生成评估报告...")
    #     logger.info("="*80 + "\n")
        
    #     self.generate_report()
        
    #     logger.info("\n" + "="*80)
    #     logger.info("🎉 评估完成！")
    #     logger.info("="*80 + "\n")
    
    # def generate_report(self):
    #     """生成评估报告"""
    #     total = len(self.results)
    #     success = len([r for r in self.results if r['status'] == 'success'])
    #     failed = len([r for r in self.results if r['status'] == 'failed'])
    #     skipped = len([r for r in self.results if r['status'] == 'skipped'])
        
    #     cell_recalls = []
    #     cell_precisions = []
    #     cell_f1s = []
    #     llm_scores = []
        
    #     for r in self.results:
    #         if r.get('evaluation'):
    #             eval_data = r['evaluation']
    #             if 'cell_recall' in eval_data:
    #                 cell_recalls.append(eval_data['cell_recall'])
    #             if 'cell_precision' in eval_data:
    #                 cell_precisions.append(eval_data['cell_precision'])
    #             if 'cell_f1' in eval_data:
    #                 cell_f1s.append(eval_data['cell_f1'])
    #             if 'llm_score' in eval_data:
    #                 llm_scores.append(eval_data['llm_score'])
        
    #     avg_cell_recall = sum(cell_recalls) / len(cell_recalls) if cell_recalls else 0
    #     avg_cell_precision = sum(cell_precisions) / len(cell_precisions) if cell_precisions else 0
    #     avg_cell_f1 = sum(cell_f1s) / len(cell_f1s) if cell_f1s else 0
    #     avg_llm_score = sum(llm_scores) / len(llm_scores) if llm_scores else 0
        
    #     report = {
    #         'model': self.model,
    #         'method': 'langextract',
    #         'extraction_passes': self.extraction_passes,
    #         'run_name': self.run_name,
    #         'summary': {
    #             'total_cases': total,
    #             'success_cases': success,
    #             'failed_cases': failed,
    #             'skipped_cases': skipped,
    #             'success_rate': f"{success/total*100:.1f}%" if total > 0 else "0%",
    #             'avg_cell_recall': f"{avg_cell_recall:.2f}%",
    #             'avg_cell_precision': f"{avg_cell_precision:.2f}%",
    #             'avg_cell_f1': f"{avg_cell_f1:.2f}",
    #             'avg_llm_score': f"{avg_llm_score:.1f}/100"
    #         },
    #         'results': self.results,
    #         'timestamp': datetime.now().isoformat()
    #     }
        
    #     safe_model = self.model.replace('/', '_').replace('\\', '_').replace('-', '_')
    #     report_filename = f"langextract_evaluation_report_{safe_model}.json"
    #     if self.run_name:
    #         safe_run_name = self.run_name.replace('/', '_').replace('\\', '_')
    #         report_filename = f"langextract_evaluation_report_{safe_model}_{safe_run_name}.json"
        
    #     report_file = self.output_dir / report_filename
    #     with open(report_file, 'w', encoding='utf-8') as f:
    #         json.dump(report, f, ensure_ascii=False, indent=2)
        
    #     logger.info("📊 评估摘要:")
    #     logger.info(f"  方法: LangExtract")
    #     logger.info(f"  模型: {self.model}")
    #     logger.info(f"  抽取轮数: {self.extraction_passes}")
    #     if self.run_name:
    #         logger.info(f"  运行名称: {self.run_name}")
    #     logger.info(f"  总测试案例: {total}")
    #     logger.info(f"  成功: {success}")
    #     logger.info(f"  跳过: {skipped}")
    #     logger.info(f"  失败: {failed}")
    #     logger.info(f"  成功率: {report['summary']['success_rate']}")
    #     logger.info(f"\n📈 平均指标:")
    #     logger.info(f"  Cell召回率: {report['summary']['avg_cell_recall']}")
    #     logger.info(f"  Cell准确率: {report['summary']['avg_cell_precision']}")
    #     logger.info(f"  Cell F1分数: {report['summary']['avg_cell_f1']}")
    #     logger.info(f"  LLM分数: {report['summary']['avg_llm_score']}")
    #     logger.info(f"\n📄 详细报告已保存到: {report_file}")
        
    #     logger.info("\n📊 各案例详细评分:")
    #     logger.info("-" * 120)
    #     logger.info(f"{'案例ID':30s} | {'召回率':8s} | {'准确率':8s} | {'F1分数':8s} | {'LLM分数':8s} | 评语")
    #     logger.info("-" * 120)
    #     for result in self.results:
    #         case_id = result['case_id']
    #         status = result['status']
            
    #         if status in ['success', 'skipped'] and result.get('evaluation'):
    #             eval_data = result['evaluation']
    #             recall = eval_data.get('cell_recall', 0)
    #             precision = eval_data.get('cell_precision', 0)
    #             f1 = eval_data.get('cell_f1', 0)
    #             llm_score = eval_data.get('llm_score', 0)
    #             comment = eval_data.get('llm_comments', 'N/A')[:50]
    #             status_prefix = "⏭️ " if status == 'skipped' else ""
    #             logger.info(f"{status_prefix}{case_id:28s} | {recall:6.2f}% | {precision:6.2f}% | {f1:8.2f} | {llm_score:6.1f}/100 | {comment}")
    #         else:
    #             error = result.get('error', 'Unknown error')[:50]
    #             logger.info(f"{case_id:30s} | {'FAILED':8s} | {'FAILED':8s} | {'FAILED':8s} | {'FAILED':8s} | {error}")
    #     logger.info("-" * 120)


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='LangExtract 抽取能力评测系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法:
  # 使用默认模型
  python langextract_evaluate.py
  
  # 指定模型
  python langextract_evaluate.py --model gpt-4o
  
  # 指定抽取轮数（默认为1）
  python langextract_evaluate.py --model gpt-5.4 --extraction-passes 1
  
  # 仅跑指定 data/case
  python langextract_evaluate.py --data-name medical --model gpt-5.4 --case-name case2 
  
  # 完整示例
  python langextract_evaluate.py --model gpt-4o --extraction-passes 1 --run-name baseline_test
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
    parser.add_argument('--extraction-passes', type=int, default=1,
                        help='LangExtract 抽取轮数（默认: 1）')
    parser.add_argument('--max-workers', type=int, default=10,
                        help='最大并发工作线程数（默认: 10）')
    parser.add_argument('--data-name', type=str, default=None,
                        help='仅评测指定数据库目录（如 education）')
    parser.add_argument('--case-name', type=str, default=None,
                        help='仅评测指定案例目录（如 case2）')
    
    args = parser.parse_args()
    
    print("\n" + "="*80)
    print("🌟 LangExtract 抽取能力评测系统")
    print("="*80)
    print(f"🤖 模型: {args.model}")
    print(f"🔄 抽取轮数: {args.extraction_passes}")
    if args.data_name:
        print(f"🗂️  数据库过滤: {args.data_name}")
    if args.case_name:
        print(f"🧪 案例过滤: {args.case_name}")
    if args.run_name:
        print(f"🏷️  运行名称: {args.run_name}")
    if args.api_base:
        print(f"🌐 API Base: {args.api_base}")
    print("="*80 + "\n")
    
    evaluator = LangExtractEvaluator(
        model=args.model,
        run_name=args.run_name,
        api_base=args.api_base,
        api_key=args.api_key,
        extraction_passes=args.extraction_passes,
        max_workers=args.max_workers,
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
