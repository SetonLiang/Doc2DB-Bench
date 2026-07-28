#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 抽取能力评测脚本
直接测试单个LLM的文档抽取能力，不经过完整的Doc2DB pipeline
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
from openai import OpenAI
import networkx as nx
# 添加项目根目录到 Python 路径，以便导入 backend 模块
project_root = os.path.dirname(os.path.dirname(__file__))  # 从 evaluate 回到项目根目录
sys.path.insert(0, project_root)

# 加载环境变量 (从 llm/.env 文件)
from utils import token_length
from dotenv import dotenv_values
from utils import _clean_response
env_path = Path(__file__).parent.parent/ "llm" / ".env"
config = dotenv_values(env_path)
DEFAULT_API_URL = config.get("GPT_URL_PRO")
DEFAULT_API_KEY = config.get("GPT_KEY_PRO")
DEEPSEEK_API_URL = config.get("GPT_URL_DS") or DEFAULT_API_URL
DEEPSEEK_API_KEY = config.get("GPT_KEY_DS") or DEFAULT_API_KEY

_client_cache: Dict[str, OpenAI] = {}


def _is_deepseek_model(model_name: Optional[str]) -> bool:
    return str(model_name or "").strip().lower().startswith("deepseek-v")


def _is_dashscope_model(model_name: Optional[str]) -> bool:
    normalized = str(model_name or "").strip().lower()
    return normalized.startswith("qwen") or normalized.startswith("glm")


def _get_client_for_model(model_name: Optional[str]) -> OpenAI:
    if _is_deepseek_model(model_name):
        cache_key = "deepseek"
        api_url = DEEPSEEK_API_URL
        api_key = DEEPSEEK_API_KEY
        # api_url = DEFAULT_API_URL
        # api_key = DEFAULT_API_KEY
    else:
        cache_key = "default"
        api_url = DEFAULT_API_URL
        api_key = DEFAULT_API_KEY

    if cache_key not in _client_cache:
        _client_cache[cache_key] = OpenAI(api_key=api_key, base_url=api_url)
    return _client_cache[cache_key]

# 导入 LLM 调用接口
from llm.ours import get_answer as ours_get_answer
from llm.dashscope import get_answer as dashscope_get_answer

# 导入 BAML 客户端（用于评估）
# from backend.baml_client.sync_client import b as baml_client
# from backend.baml_client.types import EvaluationResult
# from backend.baml_src.client_selector import get_client_name_for_model

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
OUTPUT_DIR = Path(__file__).parent.parent / "dataset/case/base_latest_output/llm"
DEFAULT_MODEL = "gpt-4o" #claude-3-5-sonnet-20240620, gpt-4o, qwen-turbo,qwen2.5-14b-instruct
DEFAULT_CHUNK_SIZE = 500000  # 默认chunk大小（字符数）1000000
# DEFAULT_CHUNK_SIZE = 10000000  # 默认chunk大小（字符数）
DEFAULT_CHUNK_OVERLAP = 500  # chunk之间的重叠部分（字符数）
MODEL_MAX_CHARS_BY_MODEL = {
    # 按模型限制单次读取的最大字符数；超过后会继续切分剩余文本
    "gpt-4o": 500000,
    "qwen2.5-14b-ins": 500000,
}
OPEN_SOURCE_MODEL_PREFIXES = (
    "llama-3.1-8b-ins",
    # "qwen2.5-",
)

# 写入 prompt.txt 时用占位符代替全文，避免单文件体积过大；内容与运行时传入 LLM 的文档一致。
PROMPT_ARCHIVE_DOCUMENT_PLACEHOLDER = (
    "...（此处为运行时合并后的文档全文，或与 relation_extraction.document_sources "
    "一致；归档为节省篇幅省略）...\n"
)


class LLMExtractorEvaluator:
    """LLM抽取能力评估器"""
    
    def __init__(self, model: str = DEFAULT_MODEL, dataset_dir: Path = DATASET_DIR,
                 output_dir: Path = OUTPUT_DIR, run_name: Optional[str] = None,
                 chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
                 force_rerun: bool = False, eval_mode: str = "oracle",
                 db_name_filter: Optional[str] = None, case_name_filter: Optional[str] = None,
                 ours_port: Optional[int] = None, single_mode: bool = False):
        self.model = model
        self.dataset_dir = dataset_dir
        self.output_dir = output_dir
        self.run_name = run_name
        self.db_name_filter = db_name_filter
        self.case_name_filter = case_name_filter
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.model_max_chunk_size = self._get_model_max_chunk_size(model)
        self.effective_chunk_size = min(self.chunk_size, self.model_max_chunk_size)
        self.force_rerun = force_rerun
        self.eval_mode = eval_mode
        self.ours_port = ours_port
        self.single_mode = single_mode
        self.results = []
        
        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 输出目录: {self.output_dir}")
        logger.info(f"🤖 模型: {model}")
        eval_mode_desc_map = {
            "oracle": "Oracle（真值反馈）",
            "pipeline": "Pipeline（端到端）",
            "pipeline-oracle": "Pipeline-Oracle（实体预测 + 关系GT参考抽取）",
        }
        eval_mode_desc = eval_mode_desc_map.get(eval_mode, f"Unknown({eval_mode})")
        logger.info(f"🎯 评测模式: {eval_mode_desc}")
        logger.info(f"⚡ Single模式 (实体表一次性抽取): {'启用' if single_mode else '关闭'}")
        if run_name:
            logger.info(f"🏷️  运行名称: {run_name}")
        logger.info(
            f"📏 Chunk大小配置: {chunk_size} 字符, 模型上限: {self.model_max_chunk_size} 字符, "
            f"实际分块大小: {self.effective_chunk_size} 字符, 重叠: {chunk_overlap} 字符"
        )
        if force_rerun:
            logger.info(f"🔄 强制重新运行模式：将重新处理所有案例")
        else:
            logger.info(f"⏭️  跳过模式：已处理的案例将被跳过")
        if db_name_filter or case_name_filter:
            parts = []
            if db_name_filter:
                parts.append(f"数据库={db_name_filter}")
            if case_name_filter:
                parts.append(f"案例={case_name_filter}")
            logger.info(f"🔎 案例过滤: {', '.join(parts)}")
        if self.ours_port is not None:
            logger.info(f"🔌 开源模型端口(ours): {self.ours_port}")

    def _get_model_max_chunk_size(self, model: str) -> int:
        """按模型名称返回单次最大字符数，未配置则退回 DEFAULT_CHUNK_SIZE。"""
        model_name = str(model or "").lower()
        for model_prefix, max_chars in MODEL_MAX_CHARS_BY_MODEL.items():
            if model_name.startswith(model_prefix.lower()):
                return max_chars
        return DEFAULT_CHUNK_SIZE

    def _use_ours_backend(self, model_name: Optional[str] = None) -> bool:
        """指定开源模型走 llm/ours.py，其余模型走默认 OpenAI 兼容接口。"""
        target = str(model_name or self.model or "").strip().lower()
        return any(target.startswith(prefix) for prefix in OPEN_SOURCE_MODEL_PREFIXES)

    def _call_llm(self, user_prompt: str, system_prompt: Optional[str] = None, model_name: Optional[str] = None) -> str:
        """统一 LLM 调用入口。"""
        target_model = model_name or self.model

        if _is_dashscope_model(target_model):
            return dashscope_get_answer(
                user_prompt,
                system_prompt=system_prompt,
                model=target_model,
            )

        if self._use_ours_backend(target_model):
            return ours_get_answer(
                user_prompt,
                system_prompt=system_prompt,
                model=target_model,
                port=self.ours_port,
            )

        messages = (
            [{"role": "user", "content": user_prompt}]
            if system_prompt is None
            else [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        create_kwargs: Dict[str, Any] = {
            "model": target_model,
            "temperature": 0.0,
            "messages": messages,
        }
        # 部分兼容网关（如 vip.yi-zhan.top 上的 Qwen3）：非流式调用必须显式关闭 thinking。
        # extra_body 会与请求 JSON 根字段合并（与 curl 顶层 enable_thinking 等价）。
        if "qwen3" in str(target_model).lower():
            create_kwargs["stream"] = False
            create_kwargs["extra_body"] = {"enable_thinking": False}

        completion = _get_client_for_model(target_model).chat.completions.create(**create_kwargs)
        return completion.choices[0].message.content
    
    def scan_datasets(self) -> List[Dict[str, Any]]:
        """扫描所有数据集和case"""
        cases = []
        
        if not self.dataset_dir.exists():
            logger.error(f"❌ 数据集目录不存在: {self.dataset_dir}")
            return cases
        

        # 遍历所有数据库目录
        for db_dir in self.dataset_dir.iterdir():
            print(db_dir)
            if not db_dir.is_dir():
                continue
            
            db_name = db_dir.name
            logger.info(f"📁 扫描数据库: {db_name}")
            
            # 遍历该数据库下的所有case
            for case_dir in db_dir.iterdir():
                if not case_dir.is_dir() or not case_dir.name.startswith("case"):
                    continue
                
                case_name = case_dir.name
                
                # 检查必要文件是否存在
                docs_dir = case_dir / "docs"
                schema_file = case_dir / "schema.json"
                answer_file = case_dir / "answer.json"
                answer_dir = case_dir / "answer"
                answer_json_files = sorted(answer_dir.glob("*.json")) if answer_dir.is_dir() else []
                tables_dir = case_dir / "tables"
                meta_file = case_dir / "meta.json"
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
                
                # 标准答案：answer.json > answer/*.json > tables/*.json（后者常为 data_construction 导出）
                if answer_file.is_file():
                    answer_source = "file"
                    answer_path = answer_file
                elif answer_json_files:
                    answer_source = "folder"
                    answer_path = answer_dir
                elif table_json_files:
                    answer_source = "tables"
                    answer_path = tables_dir
                else:
                    logger.warning(
                        f"⚠️  {db_name}/{case_name}: 无标准答案（需要 answer.json、answer/*.json 或 tables/*.json），跳过"
                    )
                    continue
                
                # 获取所有文档文件
                doc_files = []
                for ext in ['*.txt', '*.pdf', '*.md', '*.docx', '*.doc']:
                    doc_files.extend(list(docs_dir.glob(ext)))
                print(doc_files)
                
                if not doc_files:
                    logger.warning(f"⚠️  {db_name}/{case_name}: docs目录为空，跳过")
                    continue
                
                cases.append({
                    'db_name': db_name,
                    'case_name': case_name,
                    'case_dir': case_dir,
                    'docs_dir': docs_dir,
                    'schema_file': schema_file,
                    'meta_file': meta_file,
                    'answer_source': answer_source,
                    'answer_path': answer_path,
                    'doc_files': [str(f) for f in doc_files]
                })
                
                if answer_source == "file":
                    ans_desc = "answer.json"
                elif answer_source == "folder":
                    ans_desc = f"answer/（{len(answer_json_files)} 个 json）"
                else:
                    ans_desc = f"tables/（{len(table_json_files)} 个 json）"
                logger.info(f"  ✓ {db_name}/{case_name}: 发现 {len(doc_files)} 个文档文件，标准答案: {ans_desc}")
        
        logger.info(f"\n📊 扫描到 {len(cases)} 个有效测试案例")

        if self.db_name_filter:
            before = len(cases)
            cases = [c for c in cases if c['db_name'] == self.db_name_filter]
            logger.info(f"   按 --db-name 过滤: {before} -> {len(cases)}")
        if self.case_name_filter:
            before = len(cases)
            cases = [c for c in cases if c['case_name'] == self.case_name_filter]
            logger.info(f"   按 --case-name 过滤: {before} -> {len(cases)}")

        logger.info(f"📊 将运行 {len(cases)} 个案例\n")
        return cases
    
    def read_document_content(self, file_path: str) -> str:
        """读取文档内容（支持txt文件）"""
        try:
            # 目前只支持txt文件，后续可扩展支持其他格式
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            logger.info(f"  ✓ 读取文档: {os.path.basename(file_path)} (长度: {token_length(content)} tokens)")
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
                # 添加文档分隔符
                merged_content.append(f"=== Document {i}: {os.path.basename(doc_file)} ===\n")
                merged_content.append(content)
                merged_content.append(f"\n=== End of Document {i} ===\n\n")
        
        final_content = "\n".join(merged_content)
        logger.info(f"  ✓ 合并完成，总长度: {token_length(final_content)} tokens")
        return final_content
    
    def _answer_file_to_table_pieces(self, jf: Path, data: Any) -> Dict[str, Any]:
        """将 answer/ 下单个 json 转为 {表名: rows} 片段（与旧逻辑一致）。"""
        if isinstance(data, dict):
            return dict(data)
        if isinstance(data, list):
            return {jf.stem: data}
        logger.warning(f"  ⚠️  跳过 {jf.name}：根类型需为 object 或 array")
        return {}

    def _tables_file_to_table_pieces(self, jf: Path, data: Any) -> Dict[str, Any]:
        """将 tables/ 下单个 json 转为 {表名: rows}。

        支持：
        - 根为数组：整文件视为单表，键为文件名（不含 .json）
        - 根为对象且含 rows 数组：``{{\"rows\": [...]}}``
        - 根为对象且多键：每个值为行列表，或 ``{{\"rows\": [...]}}`` 嵌套（忽略 capability_assignments 等元数据键）
        """
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
        """合并目录下多个 *.json 为一张 expected_answer 字典。mode 为 \"answer\" 或 \"tables\"。"""
        merged: Dict[str, Any] = {}
        for jf in sorted(path.glob("*.json")):
            if mode == "tables" and jf.name.lower() == "schema.json":
                continue
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
            if mode == "tables":
                pieces = self._tables_file_to_table_pieces(jf, data)
            else:
                pieces = self._answer_file_to_table_pieces(jf, data)
            for k, v in pieces.items():
                if k in merged:
                    logger.warning(
                        f"  ⚠️  表 {k} 在多个文件中出现，后读入的文件覆盖先前内容: {jf.name}"
                    )
                merged[k] = v
        if not merged:
            label = "tables/" if mode == "tables" else "answer/"
            raise ValueError(f"{label} 目录无有效表数据: {path}")
        return merged

    def load_expected_answer(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """加载标准答案：支持 answer.json 单文件，或 answer/、tables/ 下多个表 json。

        优先级（扫描阶段已定）：answer.json > answer/*.json > tables/*.json

        answer/ 约定：
        - 每个 *.json 若为对象 ``{\"TableName\": [rows], ...}``，则合并所有键；
        - 若为数组 ``[...]``，则以文件名为表名（不含 .json）作为键。

        tables/ 约定：
        - 根为数组：键为文件名 stem；
        - 根为对象且含 ``rows``：键为文件名 stem；
        - 根为对象且多表：非元数据键下列式或 ``rows`` 子对象；
        - ``schema.json``（大小写不敏感）忽略。
        """
        src = case_info.get("answer_source", "file")
        path: Path = case_info["answer_path"]
        if src == "file":
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        if src == "tables":
            return self._merge_multi_table_json_dir(path, "tables")
        return self._merge_multi_table_json_dir(path, "answer")

    def load_if_primary_allowed(self, case_info: Dict[str, Any]) -> Dict[str, bool]:
        """读取 case 下 meta.json 的 if_primary_allowed，返回小写表名映射。"""
        meta_path = case_info.get("meta_file")
        if not meta_path:
            return {}
        meta_path = Path(meta_path)
        if not meta_path.is_file():
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                return {}
            raw = meta.get("if_primary_allowed", {})
            if not isinstance(raw, dict):
                return {}
            normalized: Dict[str, bool] = {}
            for table_name, flag in raw.items():
                if isinstance(table_name, str):
                    normalized[table_name.lower()] = bool(flag)
            return normalized
        except Exception as e:
            logger.warning(f"  ⚠️  读取 meta.json(if_primary_allowed) 失败，忽略该配置: {e}")
            return {}

    def _normalize_meta_type_tokens(self, raw_type: Any) -> List[str]:
        """将 meta.type 的值统一为小写 token 列表，兼容字符串/列表/字符串化列表。"""
        if isinstance(raw_type, list):
            vals = raw_type
        else:
            text = str(raw_type).strip()
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text.replace("'", '"'))
                    vals = parsed if isinstance(parsed, list) else [text]
                except Exception:
                    vals = [text]
            else:
                vals = [text]
        return [str(v).strip().lower() for v in vals if str(v).strip()]

    def load_multihop_relation_flags(self, case_info: Dict[str, Any]) -> Dict[str, bool]:
        """读取 case 下 meta.json 的 type 字段，标记哪些表属于 multi-hop 关系表。"""
        meta_path = case_info.get("meta_file")
        if not meta_path:
            return {}
        meta_path = Path(meta_path)
        if not meta_path.is_file():
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                return {}
            raw_type_map = meta.get("type", {})
            if not isinstance(raw_type_map, dict):
                return {}

            flags: Dict[str, bool] = {}
            for table_name, raw_type in raw_type_map.items():
                if not isinstance(table_name, str):
                    continue
                tokens = self._normalize_meta_type_tokens(raw_type)
                is_multihop = any(
                    t in {"2-hop", "multi-hop", "multihop", "2hop", "hop2"}
                    for t in tokens
                )
                flags[table_name.lower()] = is_multihop
            return flags
        except Exception as e:
            logger.warning(f"  ⚠️  读取 meta.json(type) 失败，忽略 multi-hop 跳过配置: {e}")
            return {}
    
    def normalize_schema(self, raw: Any) -> Dict[str, Any]:
        """将多种 schema.json 顶层结构统一为内部使用的 dict（含 ``tables`` 列表）。

        支持：
        - 标准对象 ``{"tables": [{ "name", "fields"|"attributes", ... }, ...]}``
        - 根为 JSON **数组** ``[{ "name", "type", "fields", ... }, ...]``（如 customer_product/case1/schema.json）
        - 单表 ``{"table_name", "columns"}`` 保持原样返回

        表级仍可使用 ``fields``；`_get_table_definition` / `get_extraction_order` 已兼容 ``fields`` 与 ``attributes``。
        """
        if isinstance(raw, list):
            tables = [t for t in raw if isinstance(t, dict) and t.get("name")]
            if not tables:
                logger.warning("  ⚠️  schema 根为数组但未找到带 name 的表定义")
            else:
                logger.info(f"  📋 schema 根为数组格式，已规范为 tables（共 {len(tables)} 张表）")
            return {"tables": tables}
        if isinstance(raw, dict):
            if "tables" in raw or ("table_name" in raw and "columns" in raw):
                return raw
            logger.warning("  ⚠️  schema 对象缺少 tables，且非单表 table_name/columns 格式")
            return raw
        logger.warning(f"  ⚠️  无法识别的 schema 类型: {type(raw)}")
        return {"tables": []}
    
    def _result_data_filename(self) -> str:
        """按评测模式区分结果文件名。"""
        if self.eval_mode == "oracle":
            return "oracle_relation.json"
        if self.eval_mode == "pipeline-oracle":
            return "extracted_data_pipeline_oracle.json"
        return "extracted_data.json"
    
    def _eval_mode_metadata_slot(self) -> str:
        """metadata.json 中嵌套字段名：oracle / pipeline / pipeline_oracle。"""
        if self.eval_mode == "oracle":
            return "oracle"
        if self.eval_mode == "pipeline-oracle":
            return "pipeline_oracle"
        return "pipeline"
    
    def is_case_processed(self, case_info: Dict[str, Any]) -> bool:
        """检查某个case是否已经处理过
        
        检查逻辑：
        1. 首先检查当前模型是否已处理过（精确匹配）
        2. 如果当前模型未处理，检查case目录下是否存在任何已处理的文件夹（基于case文件名）
        """
        case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
        
        # 1. 检查当前模型是否已处理过
        safe_model = self.model.replace('/', '_').replace('\\', '_').replace('-', '_')
        folder_name = f"{case_info['db_name']}_{case_info['case_name']}_{safe_model}_only"
        
        model_dir = case_dir / folder_name
        metadata_file = model_dir / "metadata.json"
        data_file = model_dir / self._result_data_filename()
        
        if metadata_file.exists() and data_file.exists():
            try:
                # 验证文件内容是否有效
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                with open(data_file, 'r', encoding='utf-8') as f:
                    extracted_data = json.load(f)
                
                # 检查是否是相同的模型
                if metadata.get('model') == self.model:
                    logger.info(f"  ⏭️  案例已处理过（当前模型），跳过: {case_info['db_name']}/{case_info['case_name']}")
                    return True
            except Exception as e:
                logger.warning(f"  ⚠️  读取已有结果失败，将重新处理: {e}")
        
        # 2. 检查case目录下是否存在任何已处理的文件夹（基于case文件名）
        # case_name 可能包含模型信息（如 case1_gemini2.5pro），如果已经存在处理结果，也跳过
        if case_dir.exists():
            # 提取 case 的基本部分（如 case1_gemini2.5pro -> case1）
            case_base = case_info['case_name'].split('_')[0] if '_' in case_info['case_name'] else case_info['case_name']
            
            # 查找所有符合 {db_name}_{case_base}_*_only 模式的文件夹
            pattern_prefix = f"{case_info['db_name']}_{case_base}_"
            pattern_suffix = "_only"
            
            for item in case_dir.iterdir():
                if item.is_dir():
                    folder_name_check = item.name
                    # 检查是否符合模式：以 pattern_prefix 开头，以 pattern_suffix 结尾
                    if folder_name_check.startswith(pattern_prefix) and folder_name_check.endswith(pattern_suffix):
                        # 检查关键文件是否存在（与当前 eval_mode 对应的数据文件）
                        check_metadata = item / "metadata.json"
                        check_data = item / self._result_data_filename()
                        
                        if check_metadata.exists() and check_data.exists():
                            try:
                                # 验证文件内容是否有效
                                with open(check_metadata, 'r', encoding='utf-8') as f:
                                    check_metadata_data = json.load(f)
                                with open(check_data, 'r', encoding='utf-8') as f:
                                    check_extracted_data = json.load(f)
                                
                                # 验证 db_name 和 case_name 是否匹配（case_name 可能被截断，所以只检查基本部分）
                                existing_db_name = check_metadata_data.get('db_name', '')
                                existing_case_name = check_metadata_data.get('case_name', '')
                                existing_case_base = existing_case_name.split('_')[0] if '_' in existing_case_name else existing_case_name
                                existing_model = check_metadata_data.get('model', 'unknown')
                                
                                # 只有当 db_name、case_base 和 model 都匹配时才跳过
                                if (existing_db_name == case_info['db_name'] and 
                                    existing_case_base == case_base and
                                    existing_model == self.model):
                                    logger.info(f"  ⏭️  案例已处理过（模型: {existing_model}），跳过: {case_info['db_name']}/{case_info['case_name']}")
                                    return True
                            except Exception as e:
                                # 文件损坏，继续检查其他文件夹
                                logger.debug(f"  ⚠️  检查文件夹 {folder_name_check} 时出错: {e}")
                                continue
        
        return False
    
    def load_existing_result(self, case_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """加载已处理案例的结果
        
        查找逻辑：
        1. 首先尝试使用完整的 case_name 查找
        2. 如果失败，尝试使用 case_base（case1）查找匹配的文件夹
        """
        case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
        
        # 提取 case 的基本部分（如 case1_gemini2.5pro -> case1）
        case_base = case_info['case_name'].split('_')[0] if '_' in case_info['case_name'] else case_info['case_name']
        
        # 清理模型名中的特殊字符
        safe_model = self.model.replace('/', '_').replace('\\', '_').replace('-', '_')
        
        # 1. 首先尝试使用完整的 case_name
        folder_name_full = f"{case_info['db_name']}_{case_info['case_name']}_{safe_model}_only"
        model_dir_full = case_dir / folder_name_full
        
        # 2. 如果完整路径不存在，尝试查找基于 case_base 的文件夹
        data_filename = self._result_data_filename()
        model_dir = None
        if (model_dir_full / "metadata.json").exists() and (model_dir_full / data_filename).exists():
            model_dir = model_dir_full
        else:
            # 查找所有符合模式的文件夹
            pattern_prefix = f"{case_info['db_name']}_{case_base}_"
            pattern_suffix = "_only"
            
            if case_dir.exists():
                for item in case_dir.iterdir():
                    if item.is_dir():
                        folder_name_check = item.name
                        if folder_name_check.startswith(pattern_prefix) and folder_name_check.endswith(pattern_suffix):
                            check_metadata = item / "metadata.json"
                            check_data = item / data_filename
                            
                            if check_metadata.exists() and check_data.exists():
                                try:
                                    # 验证 metadata 是否匹配
                                    with open(check_metadata, 'r', encoding='utf-8') as f:
                                        check_metadata_data = json.load(f)
                                    
                                    existing_db_name = check_metadata_data.get('db_name', '')
                                    existing_case_name = check_metadata_data.get('case_name', '')
                                    existing_case_base = existing_case_name.split('_')[0] if '_' in existing_case_name else existing_case_name
                                    
                                    # 检查是否匹配当前模型和 case
                                    if (existing_db_name == case_info['db_name'] and 
                                        existing_case_base == case_base and
                                        check_metadata_data.get('model') == self.model):
                                        model_dir = item
                                        logger.info(f"  ✓ 找到匹配的结果文件夹: {folder_name_check}")
                                        break
                                except Exception as e:
                                    logger.debug(f"  ⚠️  检查文件夹 {folder_name_check} 时出错: {e}")
                                    continue
        
        if model_dir is None:
            logger.error(f"  ✗ 未找到匹配的结果文件夹")
            return None
        
        try:
            # 读取元数据
            with open(model_dir / "metadata.json", 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            # 读取提取的数据（与当前 eval_mode 对应的文件名）
            with open(model_dir / data_filename, 'r', encoding='utf-8') as f:
                extracted_data = json.load(f)
            
            # 读取标准答案
            expected_answer = self.load_expected_answer(case_info)
            
            # 重新计算评估指标
            cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
            # llm_evaluation = self.evaluate_with_llm(extracted_data, expected_answer, case_info)
            
            result = {
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
            
            return result
            
        except Exception as e:
            logger.error(f"  ✗ 加载已有结果失败: {e}")
            return None
    
    def split_text_into_chunks(self, text: str) -> List[str]:
        """将文本分割成多个chunks（基于字符数，但统计token数）"""
        text_char_len = len(text)
        text_token_len = token_length(text)
        effective_chunk_size = self.effective_chunk_size
        effective_overlap = min(self.chunk_overlap, max(0, effective_chunk_size - 1))
        
        # 用字符长度判断是否需要分chunk
        if text_char_len <= effective_chunk_size:
            logger.info(f"  ℹ️  文档长度 {text_char_len} 字符 ({text_token_len} tokens)，无需分chunk")
            return [text]
        
        chunks = []
        start = 0
        
        while start < text_char_len:
            end = start + effective_chunk_size
            
            # 如果不是最后一个chunk，尝试在合适的位置断开（如段落、句子）
            if end < text_char_len:
                # 优先在段落边界断开
                paragraph_break = text.rfind('\n\n', start, end)
                if paragraph_break > start + effective_chunk_size // 2:
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
                    if sentence_break > start + effective_chunk_size // 2:
                        end = sentence_break + 1
            
            chunk = text[start:end]
            chunks.append(chunk)
            
            # 下一个chunk的起始位置，考虑重叠
            start = end - effective_overlap if end < text_char_len else end
        
        # 统计token数用于日志
        chunk_token_lengths = [token_length(chunk) for chunk in chunks]
        avg_token_len = sum(chunk_token_lengths) / len(chunk_token_lengths) if chunks else 0
        
        logger.info(
            f"  ✂️  文档已分割成 {len(chunks)} 个chunks（每个约 {effective_chunk_size} 字符，平均 {avg_token_len:.0f} tokens）"
        )
        for i, chunk in enumerate(chunks, 1):
            chunk_char_len = len(chunk)
            chunk_token_len = token_length(chunk)
            logger.info(f"     Chunk {i}: {chunk_char_len} 字符 ({chunk_token_len} tokens)")
        
        return chunks
    
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
    
    def _is_value_empty(self, value: Any) -> bool:
        """判断值是否为空（None、空字符串、'--'等）"""
        if value is None:
            return True
        normalized = self.normalize_value(value)
        return normalized == "" or normalized == "--" or normalized == "null" or normalized == "none"
    
    def _can_merge_rows(self, row1: Dict[str, Any], row2: Dict[str, Any], primary_key: str = None) -> bool:
        """判断两条记录是否可以合并
        
        规则：
        1. 如果指定了主键，主键值必须相同
        2. 对于每个字段，如果两个记录都有非空值且值不同，则不能合并
        3. 如果字段值互补（一个为空，另一个有值），则可以合并
        
        Args:
            row1: 第一条记录
            row2: 第二条记录
            primary_key: 主键字段名（可选，如果提供则必须相同才能合并）
        
        Returns:
            True表示可以合并，False表示不能合并
        """
        # 如果指定了主键，检查主键是否相同
        if primary_key:
            pk1 = self.normalize_value(row1.get(primary_key))
            pk2 = self.normalize_value(row2.get(primary_key))
            if pk1 != pk2 or not pk1:  # 主键不同或主键为空，不能合并
                return False
        
        # 获取所有字段名
        all_keys = set(row1.keys()) | set(row2.keys())
        
        # 检查每个字段
        for key in all_keys:
            val1 = row1.get(key)
            val2 = row2.get(key)
            
            is_empty1 = self._is_value_empty(val1)
            is_empty2 = self._is_value_empty(val2)
            
            # 如果两个值都不为空，检查是否相同
            if not is_empty1 and not is_empty2:
                norm_val1 = self.normalize_value(val1)
                norm_val2 = self.normalize_value(val2)
                if norm_val1 != norm_val2:
                    # 同一个字段有两个不同的非空值，不能合并
                    return False
        
        # 所有字段都兼容（互补或相同），可以合并
        return True
    
    def _merge_two_rows(self, row1: Dict[str, Any], row2: Dict[str, Any]) -> Dict[str, Any]:
        """合并两条记录，优先使用非空值
        
        Args:
            row1: 第一条记录
            row2: 第二条记录
        
        Returns:
            合并后的记录
        """
        merged = {}
        all_keys = set(row1.keys()) | set(row2.keys())
        
        for key in all_keys:
            val1 = row1.get(key)
            val2 = row2.get(key)
            
            is_empty1 = self._is_value_empty(val1)
            is_empty2 = self._is_value_empty(val2)
            
            if not is_empty1 and not is_empty2:
                # 两个值都不为空，优先使用第一个（或可以比较选择更好的）
                merged[key] = val1
            elif not is_empty1:
                # val1不为空，val2为空
                merged[key] = val1
            elif not is_empty2:
                # val2不为空，val1为空
                merged[key] = val2
            else:
                # 两个都为空
                merged[key] = val1 if val1 is not None else val2
        
        return merged
    
    def merge_extraction_results(self, all_results: List[Dict[str, List[Dict[str, Any]]]], 
                                 primary_keys: Dict[str, str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """合并多个chunk的提取结果，智能合并互补的记录
        
        Args:
            all_results: 所有chunk的提取结果列表
            primary_keys: 每个表的主键字段名字典，格式为 {table_name: primary_key_field}
        
        Returns:
            合并后的结果字典
        """
        if primary_keys is None:
            primary_keys = {}
        
        # 收集所有表的所有行
        table_rows = {}
        
        for result in all_results:
            for table_name, rows in result.items():
                if table_name not in table_rows:
                    table_rows[table_name] = []
                table_rows[table_name].extend(rows)
        
        # 对每个表进行智能合并
        merged_result = {}
        for table_name, rows in table_rows.items():
            if not rows:
                merged_result[table_name] = []
                continue
            
            # 获取该表的主键字段名（如果有）
            pk_field = primary_keys.get(table_name)
            
            # 如果指定了主键，按主键分组
            if pk_field:
                # 按主键分组
                pk_groups = {}
                for row in rows:
                    pk_value = self.normalize_value(row.get(pk_field))
                    if pk_value:
                        if pk_value not in pk_groups:
                            pk_groups[pk_value] = []
                        pk_groups[pk_value].append(row)
                    else:
                        # 主键为空的记录，单独处理
                        if None not in pk_groups:
                            pk_groups[None] = []
                        pk_groups[None].append(row)
                
                # 对每个主键组内的记录进行合并
                merged_rows = []
                for pk_value, group_rows in pk_groups.items():
                    if pk_value is None:
                        # 主键为空的记录，使用简单去重
                        seen = set()
                        for row in group_rows:
                            sig = self._create_row_signature(row)
                            if sig not in seen:
                                seen.add(sig)
                                merged_rows.append(row)
                    else:
                        # 有主键的记录，尝试合并互补的记录
                        # 使用贪心算法：不断尝试合并，直到没有更多可以合并的记录
                        current_group = group_rows.copy()
                        merged_group = []
                        
                        while current_group:
                            base_row = current_group.pop(0)
                            merged_row = base_row.copy()
                            changed = True
                            
                            # 不断尝试与剩余记录合并，直到没有更多可以合并的
                            while changed:
                                changed = False
                                to_remove = []
                                
                                for i, other_row in enumerate(current_group):
                                    if self._can_merge_rows(merged_row, other_row, pk_field):
                                        merged_row = self._merge_two_rows(merged_row, other_row)
                                        to_remove.append(i)
                                        changed = True
                                
                                # 移除已合并的记录（从后往前删除，避免索引问题）
                                for i in reversed(to_remove):
                                    current_group.pop(i)
                            
                            merged_group.append(merged_row)
                        
                        merged_rows.extend(merged_group)
                
                merged_result[table_name] = merged_rows
                logger.info(f"  🔗 表 {table_name}: {len(rows)} 行 -> 智能合并后 {len(merged_rows)} 行")
            else:
                # 没有主键，使用简单去重
                seen_records = set()
                unique_rows = []
                
                for row in rows:
                    row_signature = self._create_row_signature(row)
                    if row_signature not in seen_records:
                        seen_records.add(row_signature)
                        unique_rows.append(row)
                
                merged_result[table_name] = unique_rows
                logger.info(f"  🔗 表 {table_name}: {len(rows)} 行 -> 去重后 {len(unique_rows)} 行（无主键，使用简单去重）")
        
        return merged_result
    
    def build_extraction_prompt(self, schema: Dict[str, Any], table_name: str, 
                                merged_content: str, nl_prompt: str = "") -> tuple[str, str]:
        """构建抽取提示词（参考base.py的实现）"""
        
        # 获取表定义
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        
        # 构建详细的schema信息
        schema_info = self._build_detailed_schema_prompt(table_def, nl_prompt)
        
        # 系统提示词
        system_prompt = """You are a professional data extraction expert. 
Your role is to read documents carefully and extract structured data with high accuracy and recall. 
Always follow the user instructions strictly and produce only the requested output format.

This is a COLD START extraction - extract all relevant data from scratch."""
        
        # 用户提示词
        user_prompt = f"""

Your task: Extract structured data from the following document according to the schema.

{schema_info}


Extraction Requirements:
1. Carefully read the document content and identify all relevant data records
2. For each record, extract all available attribute values according to the schema constraints
3. If an attribute value is not found, set it to null
4. Maintain data accuracy and completeness
5. Pay strict attention to data types, formats, and domain constraints defined in the schema
6. Follow all validation rules and constraints specified in the schema
7. Extract as many relevant records as possible for high recall
8. Ensure extracted values comply with the schema requirements

CRITICAL: You MUST respond with a Markdown table format marked with <TABLE BEGIN> and <TABLE END> tags.

Format Example:
<TABLE BEGIN>
| {attributes[0]['name'] if attributes else 'field1'} | {attributes[1]['name'] if len(attributes) > 1 else 'field2'} | {attributes[2]['name'] if len(attributes) > 2 else 'field3'} |
|---|---|---|
| extracted_value1 | extracted_value2 | extracted_value3 |
| extracted_value4 | extracted_value5 | extracted_value6 |
<TABLE END>

IMPORTANT NOTES:
- Use ONLY the column names from the schema: {[attr['name'] for attr in attributes]}
- Each row should represent one data record (e.g., one quarter's data for one company)
- Create separate rows for different quarters/time periods
- Always include all schema fields as columns, even if the value is null
- Use simple text values in table cells, no complex structures
- Follow standard Markdown table format with proper alignment
- Include the header separator row with dashes

Additional Instructions: {nl_prompt if nl_prompt else 'No special requirements'}

Document Content:
{merged_content}

Please return the Markdown table result in the format specified in the system instructions. Remember to respond with the complete table wrapped in <TABLE BEGIN> and <TABLE END> tags."""
        
        return system_prompt, user_prompt
    

    # === [NEW] Multi-Entity Prompt Generator ===
    def build_multi_extraction_prompt(self, schema: Dict[str, Any], table_names: List[str], merged_content: str, nl_prompt: str = "") -> tuple[str, str]:
        schema_parts = []
        format_examples = []
        
        for t_name in table_names:
            t_def = self._get_table_definition(schema, t_name)
            if not t_def:
                continue
            attributes = t_def.get('attributes', [])
            schema_parts.append(self._build_detailed_schema_prompt(t_def, ""))
            
            headers = [attr['name'] for attr in attributes]
            fmt = (
                f"<TABLE BEGIN: {t_name}>\n"
                f"| {' | '.join(headers)} |\n"
                f"|{'|'.join(['---'] * len(headers))}|\n"
                f"| {' | '.join(['val'] * len(headers))} |\n"
                f"<TABLE END>"
            )
            format_examples.append(fmt)

        combined_schema = "\n\n".join(schema_parts)
        combined_examples = "\n\n".join(format_examples)

        system_prompt = """You are a professional data extraction expert.
Your role is to read documents carefully and extract structured data for MULTIPLE tables with high accuracy and recall.
Always follow the user instructions strictly and produce ONLY the requested output format.

This is a COLD START extraction - extract all relevant data from scratch."""

        user_prompt = f"""
Your task: Extract structured data from the following document into MULTIPLE tables according to their schemas.

{combined_schema}

Extraction Requirements:
1. Carefully read the document content.
2. For EACH table schema provided, extract all relevant records.
3. If an attribute value is not found, set it to null.
4. Maintain data accuracy and completeness. Follow all constraints.

CRITICAL: You MUST respond with Markdown tables. Each table MUST be wrapped in its own <TABLE BEGIN: table_name> and <TABLE END> tags.

Format Example:
{combined_examples}

IMPORTANT NOTES:
- Use EXACTLY the column names defined in each schema.
- Each row represents one data record.
- Always include all fields as columns, even if null.

Additional Instructions: {nl_prompt if nl_prompt else 'No special requirements'}

Document Content:
{merged_content}

Please return the Markdown tables in the format specified. Remember to wrap EVERY table in its specific <TABLE BEGIN: table_name> tag."""

        return system_prompt, user_prompt


    def _get_table_definition(self, schema: Dict[str, Any], table_name: str) -> Optional[Dict[str, Any]]:
        """获取表格定义（兼容多种schema格式）"""
        
        # 格式1: 标准格式 {tables: [{name: '', attributes: [...]}]}
        tables = schema.get('tables', [])
        for table in tables:
            if table.get('name') == table_name:
                result = table.copy()
                # 兼容 fields 字段名，统一转换为 attributes
                if 'fields' in result and 'attributes' not in result:
                    result['attributes'] = result.pop('fields')
                return result
        
        # 格式2: 前端表单格式 {table_name: '', columns: [...]}
        if schema.get('table_name') == table_name and 'columns' in schema:
            return {
                'name': table_name,
                'attributes': schema['columns']
            }
    
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

    def _prompt_kind_cn_for_table(self, schema: Dict[str, Any], table_name: str) -> str:
        """按 schema 分类返回用于 prompt 归档的中文类型标签"""
        classified = self.classify_tables_by_type(schema)
        if table_name in classified.get("relation_tables", []):
            return "关系表"
        return "实体表"

    def _snapshot_prompt_for_relation_call(
        self,
        schema: Dict[str, Any],
        table_name: str,
        reference_tables_data: Dict[str, List[Dict[str, Any]]],
    ) -> tuple[str, str]:
        """与单次 LLM 调用相同的模板（归档用占位文档），不含 chunk 后缀说明。"""
        if reference_tables_data:
            return self.build_relation_extraction_prompt(
                schema,
                table_name,
                PROMPT_ARCHIVE_DOCUMENT_PLACEHOLDER,
                reference_tables_data,
                nl_prompt="",
            )
        return self.build_extraction_prompt(
            schema,
            table_name,
            PROMPT_ARCHIVE_DOCUMENT_PLACEHOLDER,
            nl_prompt="",
        )
    
    def get_extraction_order(self, schema: Dict[str, Any]) -> List[str]:
        """构建表依赖图并进行拓扑排序，解决多元级联关系。

        根据 schema 中的外键定义构建有向图，被引用的表排在前面。
        没有外键依赖的表（纯实体表）会自动排在最前面。
        """
        G = nx.DiGraph()

        tables = schema.get('tables', [])
        if not tables and 'table_name' in schema:
            tables = [{'name': schema['table_name'], 'attributes': schema.get('columns', [])}]

        name_map: Dict[str, str] = {}
        for table in tables:
            if isinstance(table, dict) and 'name' in table:
                key = table['name'].lower()
                name_map[key] = table['name']
                G.add_node(key)

        for table in tables:
            if not isinstance(table, dict) or 'name' not in table:
                continue
            table_name = table['name'].lower()
            attributes = table.get('attributes', table.get('fields', []))
            for attr in attributes:
                fk = attr.get('constraints', {}).get('foreign_key')
                if fk and isinstance(fk, dict) and 'table' in fk:
                    ref_table = fk['table'].lower()
                    if ref_table in G.nodes:
                        G.add_edge(ref_table, table_name)

        try:
            sorted_nodes = list(nx.topological_sort(G))
            extraction_order = [name_map[node] for node in sorted_nodes if node in name_map]
            logger.info(f"  依赖图拓扑抽取顺序: {' -> '.join(extraction_order)}")
            return extraction_order
        except nx.NetworkXUnfeasible:
            logger.error("  Schema 中存在循环依赖！退回按原始顺序抽取。")
            return [t['name'] for t in tables if isinstance(t, dict) and 'name' in t]

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
                    logger.info(f"  ✅ 成功加载文档: {doc_path.name} ({token_length(content)} tokens)")
                
            except Exception as e:
                logger.error(f"  ❌ 加载文档失败 {doc_source}: {e}")
                continue
        
        if merged_content:
            result = "\n".join(merged_content)
            logger.info(f"  ✅ 从document_sources加载了 {len(document_sources)} 个文档，总计 {token_length(result)} tokens")
            return result
        else:
            logger.warning(f"  ⚠️ document_sources配置的文档都加载失败")
            return ""
    
    def _load_reference_tables(self, table_def: Dict[str, Any], expected_answer: Dict[str, List]) -> Dict[str, List[Dict[str, Any]]]:
        """加载关系抽取所需的参考表数据。
        
        逻辑：扫描 table_def 里的外键(foreign_key)，并从 expected_answer 中找到对应的实体表。
        支持大小写自动对齐。
        """
        reference_tables = {}
        
        # 1. 扫描 attributes 提取所有的外键依赖表
        required_tables = set()
        for attr in table_def.get('attributes', []):
            fk = attr.get('constraints', {}).get('foreign_key')
            if fk and isinstance(fk, dict):
                ref_table_name = fk.get('table')
                if ref_table_name:
                    required_tables.add(ref_table_name)
        
        if not required_tables:
            return reference_tables

        # 2. 为了解决大小写问题，建立一个“小写 -> 原始Key”的映射表
        # 例如：{'student': 'Student', 'course': 'Course'}
        answer_keys_map = {k.lower(): k for k in expected_answer.keys()}

        for target_table in required_tables:
            target_lower = target_table.lower()
            
            # 3. 匹配表名（不区分大小写）
            if target_lower in answer_keys_map:
                actual_key = answer_keys_map[target_lower]
                table_data = expected_answer[actual_key]
                
                if isinstance(table_data, list):
                    # 记录找到的表（保存到结果时可以使用原始定义的 target_table 名字）
                    reference_tables[target_table] = table_data
                    logger.info(f"  ✅ 已从 answer.json 匹配并加载参考表: [{actual_key}] (映射自外键定义: {target_table})，共 {len(table_data)} 行")
                else:
                    logger.warning(f"  ⚠️ answer.json 中的 {actual_key} 不是列表格式")
            else:
                # 如果在内存中没找到，保留原有的磁盘搜索逻辑作为兜底（可选）
                logger.warning(f"  ⚠️ 外键依赖表 '{target_table}' 未在 expected_answer 中找到 (现有Key: {list(expected_answer.keys())})")
                
        return reference_tables

    def _load_reference_tables_from_extracted(self, table_def: Dict[str, Any], 
                                               extracted_data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
        """从模型已抽取的全局数据池中加载参考表（Pipeline模式专用）。

        与 _load_reference_tables 逻辑类似，但数据来源是 extracted_data 而非 expected_answer。
        """
        reference_tables = {}

        required_tables = set()
        for attr in table_def.get('attributes', []):
            fk = attr.get('constraints', {}).get('foreign_key')
            if fk and isinstance(fk, dict):
                ref_table_name = fk.get('table')
                if ref_table_name:
                    required_tables.add(ref_table_name)

        if not required_tables:
            return reference_tables

        extracted_keys_map = {k.lower(): k for k in extracted_data.keys()}

        for target_table in required_tables:
            target_lower = target_table.lower()
            if target_lower in extracted_keys_map:
                actual_key = extracted_keys_map[target_lower]
                table_data = extracted_data[actual_key]
                if isinstance(table_data, list):
                    reference_tables[target_table] = table_data
                    logger.info(f"  ✅ [Pipeline] 已从模型抽取数据加载参考表: [{actual_key}] (映射自外键定义: {target_table})，共 {len(table_data)} 行")
                else:
                    logger.warning(f"  ⚠️ 模型抽取数据中的 {actual_key} 不是列表格式")
            else:
                logger.warning(f"  ⚠️ [Pipeline] 外键依赖表 '{target_table}' 未在已抽取数据中找到 (现有Key: {list(extracted_data.keys())})")

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
    
    def build_relation_extraction_prompt(self, schema: Dict[str, Any], table_name: str, 
                                         merged_content: str, reference_tables_data: Dict[str, List[Dict[str, Any]]],
                                         nl_prompt: str = "") -> tuple[str, str]:
        """构建关系抽取的提示词（包含参考表数据）"""
        
        table_def = self._get_table_definition(schema, table_name)
        if not table_def:
            raise ValueError(f"未找到表格定义: {table_name}")
        
        attributes = table_def.get('attributes', [])
        relation_config = table_def.get('relation_extraction', {})
        
        schema_info = self._build_detailed_schema_prompt(table_def, nl_prompt)
        
        # 构建参考表prompt
        reference_tables_prompt = self._build_reference_tables_prompt(reference_tables_data, relation_config)
        
        system_prompt = """You are a professional data extraction expert specializing in relationship extraction.
Your role is to read documents carefully and extract relationships between entities with high accuracy and recall.
You must use the reference tables provided to correctly map entity names to their IDs.
Always follow the user instructions strictly and produce only the requested output format.

This is a RELATIONSHIP EXTRACTION task - extract relationships between entities using the reference tables."""
        
        user_prompt = f"""
Your task: Extract relationship data from the following document according to the schema.

{schema_info}

{reference_tables_prompt}

Extraction Requirements:
1. Carefully read the document content and identify all relationships between entities
2. Use the reference tables to map entity names/descriptions to their correct IDs
3. For each relationship, extract all required foreign key fields
4. Only extract relationships that are explicitly mentioned in the document
5. For foreign key fields, use the EXACT ID values from the reference tables

CRITICAL: You MUST respond with a Markdown table format marked with <TABLE BEGIN> and <TABLE END> tags.

Format Example:
<TABLE BEGIN>
| {attributes[0]['name'] if attributes else 'field1'} | {attributes[1]['name'] if len(attributes) > 1 else 'field2'} |
|---|---|
| id_value1 | id_value2 |
| id_value3 | id_value4 |
<TABLE END>

IMPORTANT NOTES:
- Use ONLY the column names from the schema: {[attr['name'] for attr in attributes]}
- Each row represents ONE relationship between entities
- Use the ID values from the reference tables, NOT the entity names
- Follow standard Markdown table format with proper alignment

Additional Instructions: {nl_prompt if nl_prompt else 'No special requirements'}

Document Content:
{merged_content}

Please return the Markdown table result. Remember to use ID values from reference tables for foreign key fields."""
        
        return system_prompt, user_prompt
    
    def _get_primary_key_from_table_def(self, table_def: Dict[str, Any]) -> Optional[str]:
        """从表定义中获取主键字段名"""
        attributes = table_def.get('attributes', [])
        for attr in attributes:
            constraints = attr.get('constraints', {})
            if isinstance(constraints, dict) and constraints.get('primary_key', False):
                return attr.get('name')
        return None

    def _get_key_fields_from_table_def(self, table_def: Dict[str, Any]) -> set[str]:
        """获取表中所有关键字段（主键 + 外键）"""
        key_fields = set()
        if not table_def:
            return key_fields
        for attr in table_def.get("attributes", []):
            name = attr.get("name")
            if not name:
                continue
            constraints = attr.get("constraints", {})
            if not isinstance(constraints, dict):
                continue
            if constraints.get("primary_key", False) or constraints.get("foreign_key"):
                key_fields.add(name)
        return key_fields

    def _get_foreign_key_fields_in_order(self, table_def: Dict[str, Any]) -> List[str]:
        """按 schema 属性顺序返回外键字段名列表。"""
        fk_fields: List[str] = []
        if not table_def:
            return fk_fields
        for attr in table_def.get("attributes", []):
            name = attr.get("name")
            if not name:
                continue
            constraints = attr.get("constraints", {})
            if not isinstance(constraints, dict):
                continue
            if constraints.get("foreign_key"):
                fk_fields.append(name)
        return fk_fields

    def _find_expected_table_rows(self, expected_answer: Dict[str, Any], table_name: str) -> List[Dict[str, Any]]:
        """在 expected_answer 中按表名（大小写不敏感）查找对应表数据"""
        if not isinstance(expected_answer, dict):
            return []
        key_map = {k.lower(): k for k in expected_answer.keys()}
        matched = key_map.get(table_name.lower())
        if not matched:
            return []
        rows = expected_answer.get(matched, [])
        if isinstance(rows, list):
            return rows
        return []

    def _align_rows_with_gt_keys(
        self,
        table_name: str,
        table_def: Dict[str, Any],
        extracted_rows: List[Dict[str, Any]],
        expected_answer: Dict[str, Any],
        allow_primary_key_backfill: bool = True,
        skip_key_fields: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """基于 GT 行匹配并回填主外键，解决文档无ID时的后续关系抽取问题。"""
        if not extracted_rows or not table_def:
            return extracted_rows

        gt_rows = self._find_expected_table_rows(expected_answer, table_name)
        if not gt_rows:
            logger.warning(f"  ⚠️  [Pipeline补键] GT 中未找到表 {table_name}，跳过补键")
            return extracted_rows

        key_fields = self._get_key_fields_from_table_def(table_def)
        if not key_fields:
            return extracted_rows
        primary_key_field = self._get_primary_key_from_table_def(table_def)
        fill_key_fields = set(key_fields)
        if not allow_primary_key_backfill and primary_key_field:
            fill_key_fields.discard(primary_key_field)
        if skip_key_fields:
            fill_key_fields -= set(skip_key_fields)
        if not fill_key_fields:
            logger.info(
                f"  ℹ️  [Pipeline补键] 表 {table_name}: 可回填关键字段为空（主键禁补或字段被跳过），跳过补键"
            )
            return extracted_rows

        # 用于匹配的字段：排除主外键，仅用业务字段做相似匹配
        candidate_fields = []
        for attr in table_def.get("attributes", []):
            name = attr.get("name")
            if name and name not in key_fields:
                candidate_fields.append(name)

        used_gt_idx = set()
        replaced_count = 0
        aligned_rows: List[Dict[str, Any]] = []

        for row in extracted_rows:
            if not isinstance(row, dict):
                aligned_rows.append(row)
                continue

            best_idx = -1
            best_score = -1
            best_overlap = 0

            for idx, gt_row in enumerate(gt_rows):
                if idx in used_gt_idx or not isinstance(gt_row, dict):
                    continue

                overlap = 0
                score = 0
                for field in candidate_fields:
                    row_val = row.get(field)
                    gt_val = gt_row.get(field)
                    row_norm = self.normalize_value(row_val)
                    gt_norm = self.normalize_value(gt_val)
                    if row_norm and gt_norm:
                        overlap += 1
                        if row_norm == gt_norm:
                            score += 1

                if overlap == 0:
                    continue
                if score > best_score or (score == best_score and overlap > best_overlap):
                    best_idx = idx
                    best_score = score
                    best_overlap = overlap

            # 没有任何可用匹配时，保留原行
            if best_idx < 0 or best_score <= 0:
                aligned_rows.append(row)
                continue

            used_gt_idx.add(best_idx)
            gt_row = gt_rows[best_idx]
            updated = row.copy()
            changed = False

            # 仅回填关键字段（主键/外键，可按配置关闭主键回填）
            for key_field in fill_key_fields:
                gt_val = gt_row.get(key_field)
                if gt_val is None:
                    continue
                if updated.get(key_field) != gt_val:
                    updated[key_field] = gt_val
                    changed = True

            if changed:
                replaced_count += 1
            aligned_rows.append(updated)

        if replaced_count > 0:
            logger.info(
                f"  🔧 [Pipeline补键] 表 {table_name}: 已为 {replaced_count}/{len(extracted_rows)} 行回填关键键（基于GT匹配）"
            )
        else:
            logger.info(f"  ℹ️  [Pipeline补键] 表 {table_name}: 未发生主外键回填")
        return aligned_rows
    
    # === [NEW] Extract Multi Entity Tables ===
    def _extract_multi_entity_tables(self, schema: Dict[str, Any], table_names: List[str], 
                                     merged_content: str) -> tuple[Dict[str, List[Dict]], List[str], Dict[str, Any]]:
        """在 single_mode 下，一次性抽取多个实体表"""
        all_results = []
        all_responses = []
        
        chunks = self.split_text_into_chunks(merged_content)
        sys_prompt, user_prompt = "", ""

        for i, chunk in enumerate(chunks, 1):
            logger.info(f"  📄 [Single Mode] 处理 Chunk {i}/{len(chunks)}...")
            
            chunk_info = f" (Chunk {i}/{len(chunks)})" if len(chunks) > 1 else ""
            sys_prompt, user_prompt = self.build_multi_extraction_prompt(
                schema, table_names, chunk, nl_prompt=""
            )
            
            logger.info(f"  🤖 调用LLM ({self.model}){chunk_info}...")
            llm_response = self._call_llm(
                user_prompt=user_prompt,
                system_prompt=sys_prompt,
                model_name=self.model,
            )

            llm_response = _clean_response(llm_response)
            
            if not llm_response:
                logger.warning(f"  ⚠️  Chunk {i} LLM返回空响应")
                continue
            
            logger.info(f"  ✓ LLM响应长度: {token_length(llm_response)} tokens")
            
            # 使用针对多表解析的专属方法
            chunk_result = self.parse_multi_llm_response(llm_response, table_names, schema)
            all_results.append(chunk_result)
            all_responses.append(f"=== Multi-Entity Tables {table_names} - Chunk {i} ===\n{llm_response}\n")
            
            if i < len(chunks):
                time.sleep(0.5)

        final_table_data = {}
        pks_map = {}
        for t_name in table_names:
            t_def = self._get_table_definition(schema, t_name)
            pk = self._get_primary_key_from_table_def(t_def)
            if pk:
                pks_map[t_name] = pk
                
        # 全局合并
        if all_results:
            merged_data = self.merge_extraction_results(all_results, primary_keys=pks_map)
            for t_name in table_names:
                final_table_data[t_name] = merged_data.get(t_name, [])
        else:
            for t_name in table_names:
                final_table_data[t_name] = []

        prompt_section = {
            "table_name": ", ".join(table_names),
            "kind_cn": "多实体表一次性抽取",
            "note": "启用 single 模式，将所有实体表合并至一次 Prompt 中进行抽取",
            "system_prompt": sys_prompt,
            "user_prompt": user_prompt
        }

        return final_table_data, all_responses, prompt_section

    # === [NEW] Parse Multi Table Response ===
    def parse_multi_llm_response(self, llm_response: str, table_names: List[str], schema: Dict) -> Dict[str, List[Dict]]:
        """解析包含多个 Markdown table 的 LLM 输出"""
        results = {t: [] for t in table_names}
        import re

        # 1. 优先使用带名字的 TABLE BEGIN 匹配
        pattern_strict = r'<TABLE BEGIN:\s*([^>]+)>(.*?)<TABLE END>'
        matches = re.findall(pattern_strict, llm_response, re.DOTALL | re.IGNORECASE)

        if matches:
            for t_name_raw, content in matches:
                matched_name = next((t for t in table_names if t.lower() == t_name_raw.strip().lower()), None)
                if matched_name:
                    t_def = self._get_table_definition(schema, matched_name)
                    # 复用单表解析逻辑
                    mock_resp = f"<TABLE BEGIN>\n{content.strip()}\n<TABLE END>"
                    parsed = self.parse_llm_response(mock_resp, t_def)
                    results[matched_name].extend(parsed.get(matched_name, []))
            return results

        # 2. 如果不带名字，匹配普通的 TABLE BEGIN
        pattern_fallback = r'<TABLE BEGIN>(.*?)<TABLE END>'
        matches_fb = re.findall(pattern_fallback, llm_response, re.DOTALL | re.IGNORECASE)

        tables_content = []
        if matches_fb:
            tables_content = matches_fb
        else:
            # 3. 终极兜底：直接提取所有 markdown 表格
            lines = llm_response.split('\n')
            curr_table = []
            for line in lines:
                if line.strip().startswith('|'):
                    curr_table.append(line)
                elif curr_table:
                    tables_content.append("\n".join(curr_table))
                    curr_table = []
            if curr_table:
                tables_content.append("\n".join(curr_table))

        # 根据表头匹配是哪张表
        for content in tables_content:
            lines = [line.strip() for line in content.strip().split('\n') if line.strip()]
            if len(lines) < 3: 
                continue
            
            header_line = lines[0]
            headers = set([h.strip().lower() for h in header_line.split('|') if h.strip()])

            best_match = None
            best_overlap = 0
            for t_name in table_names:
                t_def = self._get_table_definition(schema, t_name)
                t_headers = set([a['name'].lower() for a in t_def.get('attributes', [])])
                overlap = len(headers & t_headers)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = t_name

            if best_match and best_overlap > 0:
                t_def = self._get_table_definition(schema, best_match)
                mock_resp = f"<TABLE BEGIN>\n{content.strip()}\n<TABLE END>"
                parsed = self.parse_llm_response(mock_resp, t_def)
                results[best_match].extend(parsed.get(best_match, []))

        return results


    def _extract_entity_table(self, schema: Dict[str, Any], table_name: str, 
                               merged_content: str) -> tuple[Dict[str, List[Dict]], List[str]]:
        """提取实体表数据
        
        Args:
            schema: 完整schema
            table_name: 表名
            merged_content: 合并后的文档内容
            
        Returns:
            (提取结果字典, 响应列表)
        """
        all_results = []
        all_responses = []
        table_def = self._get_table_definition(schema, table_name)
        
        # 获取主键字段名
        primary_key = self._get_primary_key_from_table_def(table_def) if table_def else None
        
        # 分割文本成chunks
        chunks = self.split_text_into_chunks(merged_content)
        
        for i, chunk in enumerate(chunks, 1):
            logger.info(f"  📄 处理 Chunk {i}/{len(chunks)}...")
            
            chunk_info = f" (Chunk {i}/{len(chunks)})" if len(chunks) > 1 else ""
            system_prompt, user_prompt = self.build_extraction_prompt(
                schema, table_name, chunk, nl_prompt=""
            )
            # print(user_prompt)
            # print("-----")
            
            logger.info(f"  🤖 调用LLM ({self.model}){chunk_info}...")
            llm_response = self._call_llm(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                model_name=self.model,
            )

            llm_response = _clean_response(llm_response)
            
            print(llm_response)
            if not llm_response:
                logger.warning(f"  ⚠️  Chunk {i} LLM返回空响应")
                continue
            
            logger.info(f"  ✓ LLM响应长度: {token_length(llm_response)} tokens")
            
            chunk_result = self.parse_llm_response(llm_response, table_def)
            all_results.append(chunk_result)
            all_responses.append(f"=== Entity Table {table_name} - Chunk {i} Response ===\n{llm_response}\n")
            
            if i < len(chunks):
                time.sleep(0.5)
        
        # 合并结果
        if not all_results:
            logger.warning(f"  ⚠️  表 {table_name} 未成功提取任何数据")
            return {table_name: []}, all_responses
        
        logger.info(f"  📊 合并 {len(all_results)} 个chunk的结果...")
        # 传递主键信息用于智能合并
        primary_keys = {table_name: primary_key} if primary_key else {}
        table_data = self.merge_extraction_results(all_results, primary_keys=primary_keys)
        
        table_rows = sum(len(rows) for rows in table_data.values())
        logger.info(f"  ✅ 实体表 {table_name} 提取完成: {table_rows} 行数据")
        
        return table_data, all_responses
    
    def _extract_relation_table(self, schema: Dict[str, Any], table_name: str, merged_content: str, 
                                extracted_entity_data: Dict[str, List[Dict]], expected_answer: Dict[str, List[Dict]],
                                 case_dir: Path) -> tuple[Dict[str, List[Dict]], List[str], Dict[str, Any]]:
        """提取关系表数据（带参考表）；实体表走同一路径时无参考表则使用冷启动实体 prompt。
        
        Args:
            schema: 完整schema
            table_name: 表名
            merged_content: 合并后的文档内容
            extracted_entity_data: 已提取的实体表数据
            case_dir: 案例目录
            
        Returns:
            (提取结果字典, 响应列表, 用于 prompt.txt 归档的单表 prompt 描述)
        """
        all_results = []
        all_responses = []
        table_def = self._get_table_definition(schema, table_name)
        
        # 获取主键字段名
        primary_key = self._get_primary_key_from_table_def(table_def) if table_def else None
        
        # 🎯 如果配置了document_sources，直接读取指定文档
        relation_config = table_def.get('relation_extraction', {}) if table_def else {}
        if relation_config.get('enabled') and relation_config.get('document_sources'):
            logger.info(f"  🎯 检测到document_sources配置，将直接读取指定文档")
            merged_content = self._load_document_sources_for_relation(relation_config, case_dir)
        
        # 1. 根据 eval_mode 加载参考表数据
        reference_tables_data = {}

        if self.eval_mode in ("oracle", "pipeline-oracle"):
            # Oracle 模式：所有前置上下文强制来自 expected_answer（GT）
            mode_tag = "Oracle" if self.eval_mode == "oracle" else "Pipeline-Oracle"
            logger.info(f"  🎯 [{mode_tag}模式] 从 expected_answer 加载参考表（GT）")
            reference_tables_data = self._load_reference_tables(table_def, expected_answer)
            if not reference_tables_data:
                logger.warning(f"  ⚠️  {mode_tag}模式下未从 expected_answer 中找到参考表")
        else:
            # Pipeline 模式：所有前置上下文 100% 来自全局数据池 extracted_entity_data
            logger.info(f"  🔗 [Pipeline模式] 从模型已抽取数据池加载参考表")
            if extracted_entity_data:
                reference_tables_data = self._load_reference_tables_from_extracted(
                    table_def, extracted_entity_data
                )
            if not reference_tables_data:
                logger.warning(f"  ⚠️  Pipeline模式下未从已抽取数据中找到参考表")
        
        if reference_tables_data:
            logger.info(f"  ✅ 已加载 {len(reference_tables_data)} 个参考表")
        else:
            logger.warning(f"  ⚠️  未找到参考表数据，将进行无参考的关系提取")

        arch_sys, arch_user = self._snapshot_prompt_for_relation_call(
            schema, table_name, reference_tables_data
        )
        kind_cn = self._prompt_kind_cn_for_table(schema, table_name)
        if reference_tables_data:
            note = (
                f"单次 LLM 调用模板（归档中文档已用占位符）；"
                f"含参考表 {len(reference_tables_data)} 张，使用关系抽取 system/user。"
            )
        else:
            note = (
                "单次 LLM 调用模板（归档中文档已用占位符）；"
                "无参考表时与「冷启动实体抽取」同款结构（列为此表 schema）。"
            )
        prompt_section: Dict[str, Any] = {
            "table_name": table_name,
            "kind_cn": kind_cn,
            "note": note,
            "system_prompt": arch_sys,
            "user_prompt": arch_user,
        }
        
        # 2. 分割文本成chunks
        chunks = self.split_text_into_chunks(merged_content)
        
        for i, chunk in enumerate(chunks, 1):
            logger.info(f"  📄 处理 Chunk {i}/{len(chunks)}...")
            
            chunk_info = f" (Chunk {i}/{len(chunks)})" if len(chunks) > 1 else ""
            
            # 使用关系提取专用的prompt
            if reference_tables_data:
                system_prompt, user_prompt = self.build_relation_extraction_prompt(
                    schema, table_name, chunk, reference_tables_data, nl_prompt=""
                )

                print(user_prompt[:5000])
                print("-----")
                # exit()
            else:
                # 没有参考数据时使用普通提取
                system_prompt, user_prompt = self.build_extraction_prompt(
                    schema, table_name, chunk, nl_prompt=""
                )

            logger.info(f"  🤖 调用LLM ({self.model}){chunk_info}...")
            llm_response = self._call_llm(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                model_name=self.model,
            )

            llm_response = _clean_response(llm_response)
            
            if not llm_response:
                logger.warning(f"  ⚠️  Chunk {i} LLM返回空响应")
                continue
            
            logger.info(f"  ✓ LLM响应长度: {token_length(llm_response)} tokens")
            
            chunk_result = self.parse_llm_response(llm_response, table_def)
            all_results.append(chunk_result)
            all_responses.append(f"=== Relation Table {table_name} - Chunk {i} Response ===\n{llm_response}\n")
            
            if i < len(chunks):
                time.sleep(0.5)
        
        # 合并结果
        if not all_results:
            logger.warning(f"  ⚠️  表 {table_name} 未成功提取任何数据")
            return {table_name: []}, all_responses, prompt_section
        
        logger.info(f"  📊 合并 {len(all_results)} 个chunk的结果...")
        # 传递主键信息用于智能合并
        primary_keys = {table_name: primary_key} if primary_key else {}
        table_data = self.merge_extraction_results(all_results, primary_keys=primary_keys)
        
        table_rows = sum(len(rows) for rows in table_data.values())
        logger.info(f"  ✅ 关系表 {table_name} 提取完成: {table_rows} 行数据")
        
        return table_data, all_responses, prompt_section
    
    def _build_detailed_schema_prompt(self, table_def: Dict[str, Any], nl_prompt: str) -> str:
        """构建详细的schema信息作为prompt的核心部分"""
        table_name = table_def.get('name', 'data_table')
        attributes = table_def.get('attributes', [])
        
        schema_parts = [
            f"=== TABLE SCHEMA DEFINITION ===",
            f"Table Name: {table_name}",
            f"Fields ({len(attributes)} total):"
        ]
        
        # 构建每个字段的详细信息
        for i, attr in enumerate(attributes):
            field_info = [f"\n{i+1}. Field: {attr['name']}"]
            
            # 数据类型
            if 'type' in attr:
                field_info.append(f"   - Type: {attr['type']}")
            
            # 描述信息
            if 'description' in attr:
                field_info.append(f"   - Description: {attr['description']}")
            
            # 约束信息 - 整合顶层和 constraints 字典中的约束
            constraints = []
            
            # 从 constraints 字典中读取约束
            constraint_dict = attr.get('constraints', {})
            if constraint_dict.get('primary_key', False):
                constraints.append("PRIMARY KEY")
            if constraint_dict.get('foreign_key', False):
                constraints.append("FOREIGN KEY")
            if constraint_dict.get('unique', False):
                constraints.append("UNIQUE")
            if not constraint_dict.get('nullable', True):
                constraints.append("NOT NULL (required)")
            
            # 从顶层读取约束（向后兼容）
            if attr.get('required', False) and "NOT NULL" not in ' '.join(constraints):
                constraints.append("REQUIRED (must not be null)")
            if 'domain' in attr:
                constraints.append(f"Domain values: {attr['domain']}")
            if 'enum' in attr:
                constraints.append(f"Enum values: {attr['enum']}")
            enum_in_constraints = constraint_dict.get('enum')
            if enum_in_constraints is not None:
                constraints.append(f"Enum values (constraints): {enum_in_constraints}")
            if 'format' in attr:
                constraints.append(f"Format pattern: {attr['format']}")
            if 'pattern' in attr:
                constraints.append(f"Regex pattern: {attr['pattern']}")
            pattern_in_constraints = constraint_dict.get('pattern')
            if pattern_in_constraints:
                constraints.append(f"Regex pattern (constraints): {pattern_in_constraints}")
            if 'min' in attr:
                constraints.append(f"Minimum value: {attr['min']}")
            if 'max' in attr:
                constraints.append(f"Maximum value: {attr['max']}")
            if attr.get('unique', False) and "UNIQUE" not in ' '.join(constraints):
                constraints.append("Must be unique")
            
            if constraints:
                field_info.append(f"   - Constraints: {' | '.join(constraints)}")
            
            schema_parts.extend(field_info)
        
        # 添加业务需求
        if nl_prompt:
            schema_parts.extend([
                f"\n=== BUSINESS REQUIREMENTS ===",
                f"Additional Requirements: {nl_prompt}",
                f"Please ensure the extracted data aligns with these business requirements while strictly following the schema definition."
            ])
        
        schema_parts.append(f"\n=== CRITICAL SCHEMA COMPLIANCE ===")
        schema_parts.append(f"- ALL extracted values MUST match the specified data types")
        schema_parts.append(f"- REQUIRED fields cannot be empty or null")
        schema_parts.append(f"- Values must comply with domain constraints if specified")
        schema_parts.append(f"- Follow format patterns exactly as defined")
        schema_parts.append(f"- Respect min/max value constraints")
        
        return "\n".join(schema_parts)
    
    def parse_llm_response(self, llm_response: str, table_def: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        """解析LLM响应，提取表格数据"""
        
        table_name = table_def.get('name', 'data_table')
        attributes = table_def.get('attributes', [])
        
        try:
            # 查找 <TABLE BEGIN> 和 <TABLE END> 之间的内容
            import re
            table_pattern = r'<TABLE BEGIN>(.*?)<TABLE END>'
            matches = re.findall(table_pattern, llm_response, re.DOTALL)
            
            if not matches:
                logger.warning("  ⚠️  未找到表格标记，尝试查找Markdown表格...")
                # 尝试直接查找Markdown表格
                lines = llm_response.split('\n')
                table_lines = [line for line in lines if line.strip().startswith('|')]
                if len(table_lines) >= 3:  # 至少要有表头、分隔符、数据行
                    table_content = '\n'.join(table_lines)
                else:
                    raise ValueError("未找到有效的表格内容")
            else:
                table_content = matches[0].strip()
            
            # 解析Markdown表格
            lines = [line.strip() for line in table_content.split('\n') if line.strip()]
            
            if len(lines) < 3:
                raise ValueError(f"表格行数不足: {len(lines)}")
            
            # 解析表头
            header_line = lines[0]
            headers = [h.strip() for h in header_line.split('|') if h.strip()]
            
            # 跳过分隔符行（第二行）
            
            # 解析数据行
            rows = []
            for line in lines[2:]:
                if not line.strip() or line.startswith('---'):
                    continue
                
                cells = [c.strip() for c in line.split('|') if c.strip()]
                
                # 确保单元格数量匹配表头
                while len(cells) < len(headers):
                    cells.append('')
                
                # 构建行字典
                row_dict = {}
                for i, header in enumerate(headers):
                    if i < len(cells):
                        cell_value = cells[i].strip()
                        # 处理null值
                        if cell_value.lower() in ['null', 'none', 'n/a', '']:
                            row_dict[header] = None
                        else:
                            row_dict[header] = cell_value
                    else:
                        row_dict[header] = None
                
                if any(v is not None for v in row_dict.values()):
                    rows.append(row_dict)
            
            logger.info(f"  ✓ 解析完成，提取 {len(rows)} 行数据")
            return {table_name: rows}
            
        except Exception as e:
            logger.error(f"  ✗ 解析LLM响应失败: {e}")
            logger.debug(f"LLM响应内容:\n{llm_response[:500]}...")
            return {table_name: []}
    
    def normalize_value(self, value: Any) -> str:
        """标准化单元格值，用于比较"""
        if value is None:
            return ""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)):
            return str(value)
        return str(value).strip().lower()
    
    def calculate_cell_metrics(self, generated: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
        """计算cell级别的召回率和准确率（改进版：使用list而非set，正确处理重复和空值）"""
        try:
            # 🔧 改进：使用list保留所有cells（包括重复的和空值）
            expected_cells = []
            generated_cells = []
            
            # 遍历所有期望的表
            for table_name, expected_rows in expected.items():
                if not isinstance(expected_rows, list):
                    continue
                
                # 标准化表名（不区分大小写）
                table_name_lower = table_name.lower()
                
                # 构建期望的cell列表: (table, attribute, value)
                for row_idx, row in enumerate(expected_rows):
                    if isinstance(row, dict):
                        for attr, value in row.items():
                            attr_lower = attr.lower()
                            norm_value = self.normalize_value(value)
                            # 🔧 使用append而不是add，保留所有cell（包括重复的）
                            expected_cells.append((table_name_lower, attr_lower, norm_value))
            
            # 遍历所有生成的表
            for table_name, generated_rows in generated.items():
                if not isinstance(generated_rows, list):
                    continue
                
                table_name_lower = table_name.lower()
                
                # 构建生成的cell列表
                for row_idx, row in enumerate(generated_rows):
                    if isinstance(row, dict):
                        for attr, value in row.items():
                            attr_lower = attr.lower()
                            norm_value = self.normalize_value(value)
                            # 🔧 使用append而不是add，保留所有cell
                            generated_cells.append((table_name_lower, attr_lower, norm_value))
            
            # 🔧 改进：使用"消耗"策略匹配cells
            # 复制expected_cells用于消耗匹配
            expected_cells_remaining = expected_cells.copy()
            correct_count = 0
            
            for gen_cell in generated_cells:
                # 在剩余的expected_cells中查找匹配项
                if gen_cell in expected_cells_remaining:
                    # 找到匹配，计数并从剩余列表中删除（消耗掉）
                    expected_cells_remaining.remove(gen_cell)
                    correct_count += 1
            
            # 计算指标
            total_expected = len(expected_cells)
            total_generated = len(generated_cells)
            total_correct = correct_count
            
            recall = (total_correct / total_expected * 100) if total_expected > 0 else 0
            precision = (total_correct / total_generated * 100) if total_generated > 0 else 0
            f1_score = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0
            
            logger.info(f"  📊 Cell指标详情: 期望{total_expected}个cell, 生成{total_generated}个cell, 正确{total_correct}个cell")
            
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
        """使用LLM进行精细化评估"""
        try:
            # 读取schema文件
            schema = {}
            try:
                with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                    schema = self.normalize_schema(json.load(f))
            except Exception as e:
                logger.warning(f"  ⚠️  无法读取schema文件: {e}")
            
            # 构建精细化评估提示
            evaluation_prompt = f"""
You are a database evaluation expert. Please evaluate the quality of database tables extracted from documents.

Database Name: {case_info['db_name']}
Test Case: {case_info['case_name']}

[Database Schema]:
{json.dumps(schema, ensure_ascii=False, indent=2)}

[Generated Results]:
{json.dumps(generated, ensure_ascii=False, indent=2)}

[Standard Answer]:
{json.dumps(expected, ensure_ascii=False, indent=2)}

## Evaluation Dimensions and Standards

Please evaluate the quality of the generated results from the following dimensions:

### 1. Accuracy and Hallucinations
- Whether the generated results are semantically consistent with the standard answer
- Whether numeric data is accurate (values must be exactly equal to be considered correct)
- Whether the decimal places of numeric values match the standard answer
- Whether string data is accurate (similarity needs to reach 90% or above)
- Whether there are hallucinations or erroneous information

### 2. Completeness
- Whether the generated results contain all key information from the standard answer
- Whether there are important data omissions
- Whether the table structure and fields are complete
- Further explanations of these key points can be omitted, but the core content must be complete

### 3. Format Consistency
- Whether the data format complies with the Schema definition
- Whether the data types are correct (numeric, string, date, etc.)
- Whether the formats are consistent (e.g., date format, number format, etc.)

## Evaluation Requirements
1. Numeric values must be exactly equal to be considered correct, including decimal places
2. String similarity below 90% is not considered a match
3. Type mismatches are serious errors
4. If the generated results fully comply with all key points of the standard answer, the total score should be 100 points
5. Please note: The standard answer may be considered the correct answer to the question

The assistant will receive a total score from 0 to 100 points, with higher scores indicating better overall performance. Please note that if the assistant's answer fully complies with the above standards and matches the standard answer, the total score should be 100 points.

Please first provide a comprehensive evaluation explanation (no more than 100 words), avoiding any potential bias. Then output the score in strict format.

Please output in the following format:
<Start Output>
Evaluation evidence: Your evaluation explanation (no more than 100 words)
Rating: [[score]]
<End Output>

Where score is an integer between 0-100.

Now begin the evaluation:
"""
            
            logger.info(f"  📊 使用 {self.model} 模型进行精细化评估")
            
            response = self._call_llm(
                user_prompt=evaluation_prompt,
                model_name=self.model,
            )

            response = _clean_response(response)
            
            if not response:
                raise Exception("LLM返回空响应")
            
            # 解析响应
            import re
            overall_score = 0
            evaluation_details = ""
            
            # 尝试提取 Rating: [[score]] 格式
            score_match = re.search(r'Rating:\s*\[\[(\d+(?:\.\d+)?)\]\]', response)
            if score_match:
                overall_score = float(score_match.group(1))
            else:
                # 尝试其他可能的格式
                score_match = re.search(r'"overall_score":\s*(\d+(?:\.\d+)?)', response)
                if score_match:
                    overall_score = float(score_match.group(1))
                else:
                    # 尝试提取数字+分
                    score_match = re.search(r'(\d+(?:\.\d+)?)\s*分', response)
                    if score_match:
                        overall_score = float(score_match.group(1))
            
            # 提取评估说明
            evidence_match = re.search(r'Evaluation evidence:\s*([^\n]+)', response)
            if evidence_match:
                evaluation_details = evidence_match.group(1).strip()
            else:
                # 如果没有找到，使用整个响应的前500字符
                evaluation_details = response[:500]
            
            # 确保分数在合理范围内
            overall_score = max(0, min(100, overall_score))
            
            llm_evaluation = {
                "overall": overall_score,
                "completeness": overall_score,  # 使用总分作为各维度分数
                "accuracy": overall_score,
                "consistency": overall_score,
                "coverage": overall_score,
                "comments": evaluation_details
            }
            
            logger.info(f"  ✓ LLM 精细化评估完成，总分: {overall_score:.1f}")
            return llm_evaluation
                
        except Exception as e:
            logger.warning(f"  ⚠️  LLM评估失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "completeness": 0.0,
                "accuracy": 0.0,
                "consistency": 0.0,
                "coverage": 0.0,
                "overall": 0.0,
                "comments": f"LLM评估失败: {str(e)}"
            }

    def _format_prompt_txt_for_disk(self, prompt_info: Dict[str, Any]) -> str:
        """将多表 prompt 段或旧版扁平结构格式化为 prompt.txt 全文。"""
        sections = prompt_info.get("sections")
        if isinstance(sections, list) and sections:
            blocks: List[str] = []
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                name = sec.get("table_name", "?")
                kind = sec.get("kind_cn", "")
                note = sec.get("note", "")
                blocks.append("=" * 80)
                blocks.append(f"表名: {name} ｜ Schema 分类: {kind}")
                if note:
                    blocks.append(f"说明: {note}")
                blocks.append("=" * 80)
                blocks.append("")
                blocks.append("=== SYSTEM PROMPT ===")
                blocks.append("")
                blocks.append(sec.get("system_prompt", ""))
                blocks.append("")
                blocks.append("=== USER PROMPT ===")
                blocks.append("")
                blocks.append(sec.get("user_prompt", ""))
                blocks.append("")
            return "\n".join(blocks).rstrip() + "\n"
        sp = prompt_info.get("system_prompt", "")
        up = prompt_info.get("user_prompt", "")
        out = (
            "=== SYSTEM PROMPT ===\n\n"
            + sp
            + "\n\n=== USER PROMPT ===\n\n"
            + up
        )
        if not out.endswith("\n"):
            out += "\n"
        return out

    def save_output(self, case_info: Dict[str, Any], extracted_data: Dict[str, Any], 
                    llm_response: str, prompt_info: Dict[str, Any], extraction_time: str = None) -> Path:
        """保存处理结果到输出目录
        
        Args:
            case_info: 案例信息
            extracted_data: 抽取的数据
            llm_response: LLM原始响应
            prompt_info: Prompt信息。支持：
                - {'sections': [ {table_name, kind_cn, note?, system_prompt, user_prompt}, ... ]}
                - 兼容旧版扁平结构 {'system_prompt', 'user_prompt'}
            extraction_time: 抽取开始时间（ISO格式字符串），如果为None则使用当前时间
        """
        # 创建案例目录：output_dir / db_name / case_name
        case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
        case_dir.mkdir(parents=True, exist_ok=True)
        
        # 构建文件夹名 - 清理模型名中的特殊字符
        safe_model = self.model.replace('/', '_').replace('\\', '_').replace('-', '_')
        folder_name = f"{case_info['db_name']}_{case_info['case_name']}_{safe_model}_only"
        
        # 创建模型专用文件夹
        model_dir = case_dir / folder_name
        model_dir.mkdir(parents=True, exist_ok=True)
        
        out_name = self._result_data_filename()
        slot = self._eval_mode_metadata_slot()
        metadata_path = model_dir / "metadata.json"
        
        metadata: Dict[str, Any] = {}
        if metadata_path.exists():
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                if not isinstance(metadata, dict):
                    metadata = {}
            except Exception as e:
                logger.warning(f"  ⚠️  读取已有 metadata.json 失败，将覆盖写入: {e}")
                metadata = {}
        
        # 迁移旧版扁平字段 → oracle / pipeline / pipeline_oracle 嵌套
        if metadata.get('eval_mode') and metadata.get('result_data_file'):
            if metadata['eval_mode'] == 'oracle':
                leg_slot = 'oracle'
            elif metadata['eval_mode'] == 'pipeline-oracle':
                leg_slot = 'pipeline_oracle'
            else:
                leg_slot = 'pipeline'
            if leg_slot not in metadata or not isinstance(metadata.get(leg_slot), dict):
                metadata[leg_slot] = {
                    'time': metadata.get('extraction_time'),
                    'result_data_file': metadata['result_data_file'],
                }
        for k in ('eval_mode', 'result_data_file', 'extraction_time'):
            metadata.pop(k, None)
        
        metadata['model'] = self.model
        metadata['run_name'] = self.run_name
        metadata['method'] = 'llm_only'
        metadata['db_name'] = case_info['db_name']
        metadata['case_name'] = case_info['case_name']
        metadata[slot] = {
            'time': extraction_time,
            'result_data_file': out_name,
        }
        
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        # 保存抽取结果：按 eval_mode 写入对应文件名（oracle / pipeline / pipeline-oracle）
        with open(model_dir / out_name, 'w', encoding='utf-8') as f:
            json.dump(extracted_data, f, ensure_ascii=False, indent=2)
        
        # 保存LLM原始响应
        with open(model_dir / "llm_response.txt", 'w', encoding='utf-8') as f:
            f.write(llm_response)
        
        # 保存prompt信息（每张表一段：实体表 / 关系表）
        with open(model_dir / "prompt.txt", 'w', encoding='utf-8') as f:
            f.write(self._format_prompt_txt_for_disk(prompt_info))
        
        logger.info(f"  ✓ 结果已保存到: {model_dir}（数据文件: {out_name}）")
        return model_dir
    
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
            # 1. 读取schema
            logger.info("📋 读取schema...")
            with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                schema = self.normalize_schema(json.load(f))
            
            # 2. 读取标准答案
            logger.info("📋 读取标准答案...")
            expected_answer = self.load_expected_answer(case_info)
            if_primary_allowed = self.load_if_primary_allowed(case_info)
            multihop_relation_flags = self.load_multihop_relation_flags(case_info)
            
            # 3. 合并文档内容
            logger.info(f"📚 合并 {len(case_info['doc_files'])} 个文档...")
            merged_content = self.merge_documents(case_info['doc_files'])
            
            if not merged_content:
                raise Exception("文档内容为空")
            
            # 4. 根据外键构建依赖图并进行拓扑排序
            extraction_order = self.get_extraction_order(schema)
            total_tables = len(extraction_order)
            entity_tables = {
                table.lower()
                for table in self.classify_tables_by_type(schema).get("entity_tables", [])
                if isinstance(table, str)
            }
            logger.info(f"  需要提取 {total_tables} 张表")
            
            # 5. 按拓扑顺序流式提取（已抽取的表自动作为后续表的上下文）
            extracted_data: Dict[str, List[Dict[str, Any]]] = {}
            all_combined_responses: List[str] = []
            all_prompt_sections: List[Dict[str, Any]] = []
            
            eval_mode_desc_map = {
                "oracle": "Oracle（真值反馈）",
                "pipeline": "Pipeline（端到端）",
                "pipeline-oracle": "Pipeline-Oracle（实体预测 + 关系GT参考抽取）",
            }
            eval_mode_desc = eval_mode_desc_map.get(self.eval_mode, f"Unknown({self.eval_mode})")
            logger.info(f"\n{'='*60}")
            logger.info(f"  开始按拓扑顺序提取 {total_tables} 个表 | 模式: {eval_mode_desc}")
            logger.info(f"{'='*60}")
            
            # Pipeline 模式下，不将 expected_answer 传给抽取方法，防止任何 GT 泄漏
            answer_for_extraction = expected_answer if self.eval_mode in ("oracle", "pipeline-oracle") else {}
            

            # ====== 新增: Single Mode 实体表聚合抽取 ======
            processed_count = 0
            if self.single_mode and self.eval_mode != "oracle":
                entity_table_names = [t for t in extraction_order if t.lower() in entity_tables]
                relation_table_names = [t for t in extraction_order if t.lower() not in entity_tables]

                if entity_table_names:
                    logger.info(f"\n  🚀 [Single Mode] 一次性抽取 {len(entity_table_names)} 个实体表: {', '.join(entity_table_names)}")
                    multi_results, multi_responses, multi_prompt_section = self._extract_multi_entity_tables(
                        schema, entity_table_names, merged_content
                    )
                    extracted_data.update(multi_results)
                    all_combined_responses.extend(multi_responses)
                    all_prompt_sections.append(multi_prompt_section)
                    processed_count += len(entity_table_names)

                tables_to_process = relation_table_names
            else:
                tables_to_process = extraction_order
            # ============================================

            for table_name in tables_to_process:
                processed_count += 1
                logger.info(f"\n  [{processed_count}/{total_tables}] 抽取表: {table_name}")
            # for table_idx, table_name in enumerate(extraction_order, 1):
                # logger.info(f"\n  [{table_idx}/{total_tables}] 抽取表: {table_name}")

                # Oracle 模式优化：跳过纯实体表（无外键依赖），直接用 GT 数据
                if self.eval_mode == "oracle":
                    table_def = self._get_table_definition(schema, table_name)
                    has_fk = False
                    if table_def:
                        for attr in table_def.get('attributes', []):
                            fk = attr.get('constraints', {}).get('foreign_key')
                            if fk and isinstance(fk, dict) and fk.get('table'):
                                has_fk = True
                                break
                    
                    if not has_fk:
                        # 纯实体表：直接从 GT 注入，无需 LLM 抽取
                        answer_keys_map = {k.lower(): k for k in expected_answer.keys()}
                        gt_key = answer_keys_map.get(table_name.lower())
                        if gt_key and isinstance(expected_answer[gt_key], list):
                            extracted_data[table_name] = expected_answer[gt_key]
                            logger.info(f"  ⏭️  [Oracle] 跳过实体表 {table_name}，直接使用 GT 数据（{len(expected_answer[gt_key])} 行）")
                            all_combined_responses.append(f"=== Entity Table {table_name} - Skipped (Oracle GT) ===\n")
                            skip_sys, skip_user = self.build_extraction_prompt(
                                schema,
                                table_name,
                                PROMPT_ARCHIVE_DOCUMENT_PLACEHOLDER,
                                nl_prompt="",
                            )
                            all_prompt_sections.append({
                                "table_name": table_name,
                                "kind_cn": self._prompt_kind_cn_for_table(schema, table_name),
                                "note": (
                                    "Oracle：该表 schema 中无外键，未调用 LLM，结果来自 GT；"
                                    "以下为若对该表走 LLM 抽取时使用的实体表 system/user 模板。"
                                ),
                                "system_prompt": skip_sys,
                                "user_prompt": skip_user,
                            })
                            continue
                        else:
                            logger.warning(f"  ⚠️  [Oracle] 实体表 {table_name} 在 GT 中未找到，退回 LLM 抽取")
                
                table_results, table_responses, prompt_section = self._extract_relation_table(
                    schema,
                    table_name,
                    merged_content,
                    extracted_data,
                    answer_for_extraction,
                    case_info['case_dir']
                )
                all_prompt_sections.append(prompt_section)

                # Pipeline 模式：文档常无ID，抽取后与GT按业务字段匹配，回填主外键再进入后续表抽取
                if self.eval_mode == "pipeline" and table_name in table_results:
                    table_def = self._get_table_definition(schema, table_name)
                    allow_primary_key_backfill = True
                    skip_key_fields: set[str] = set()

                    # 实体表且 meta.json 标注 if_primary_allowed=true 时，不回填主键ID（避免覆盖模型可直接抽取到的主键）
                    if table_name.lower() in entity_tables and if_primary_allowed.get(table_name.lower()) is True:
                        allow_primary_key_backfill = False
                        logger.info(
                            f"  ℹ️  [Pipeline补键] 表 {table_name}: meta.if_primary_allowed=true，跳过主键ID回填"
                        )

                    # multi-hop 关系表：只跳过“最后一个外键字段”的GT回填，其他键仍允许回填
                    if (
                        multihop_relation_flags.get(table_name.lower(), False)
                        and table_name.lower() not in entity_tables
                    ):
                        fk_fields = self._get_foreign_key_fields_in_order(table_def)
                        if fk_fields:
                            last_fk = fk_fields[-1]
                            skip_key_fields.add(last_fk)
                            logger.info(
                                f"  ℹ️  [Pipeline补键] 表 {table_name}: multi-hop 关系表，跳过最后外键字段回填 -> {last_fk}"
                            )

                    table_results[table_name] = self._align_rows_with_gt_keys(
                        table_name=table_name,
                        table_def=table_def,
                        extracted_rows=table_results[table_name],
                        expected_answer=expected_answer,
                        allow_primary_key_backfill=allow_primary_key_backfill,
                        skip_key_fields=skip_key_fields,
                    )
                
                extracted_data.update(table_results)
                all_combined_responses.extend(table_responses)
                
                # if table_idx < total_tables:
                #     time.sleep(1)
            
            combined_response = "\n".join(all_combined_responses)
            
            logger.info(f"\n{'='*60}")
            total_rows = sum(len(rows) for rows in extracted_data.values())
            logger.info(f"  完成所有表的提取，共 {len(extracted_data)} 张表，总计 {total_rows} 行数据")
            logger.info(f"  抽取顺序: {' -> '.join(extraction_order)}")
            logger.info(f"{'='*60}")
            
            extraction_duration = time.time() - start_time
            
            logger.info("  保存处理结果...")
            output_dir = self.save_output(
                case_info, 
                extracted_data, 
                combined_response,
                {"sections": all_prompt_sections},
                extraction_time=extraction_duration
            )
            result['output_dir'] = str(output_dir)
            
            # 10. 计算Cell级别的指标
            # logger.info("📊 计算Cell级别的召回率和准确率...")
            # cell_metrics = self.calculate_cell_metrics(extracted_data, expected_answer)
            # logger.info(f"  ✓ Cell召回率: {cell_metrics['cell_recall']:.2f}%")
            # logger.info(f"  ✓ Cell准确率: {cell_metrics['cell_precision']:.2f}%")
            # logger.info(f"  ✓ Cell F1分数: {cell_metrics['cell_f1']:.2f}")
            
            # # 10. 使用LLM评估结果
            # logger.info("🎯 使用LLM评估结果...")
            # llm_evaluation = self.evaluate_with_llm(
            #     extracted_data, 
            #     expected_answer, 
            #     case_info
            # )
            
            # # 11. 整合评估结果
            # evaluation = {
            #     # Cell级别指标
            #     "cell_recall": cell_metrics['cell_recall'],
            #     "cell_precision": cell_metrics['cell_precision'],
            #     "cell_f1": cell_metrics['cell_f1'],
            #     "total_expected_cells": cell_metrics['total_expected_cells'],
            #     "total_generated_cells": cell_metrics['total_generated_cells'],
            #     "total_correct_cells": cell_metrics['total_correct_cells'],
                
            #     # LLM评分
            #     "llm_score": llm_evaluation.get('overall', 0),
            #     "llm_completeness": llm_evaluation.get('completeness', 0),
            #     "llm_accuracy": llm_evaluation.get('accuracy', 0),
            #     "llm_consistency": llm_evaluation.get('consistency', 0),
            #     "llm_coverage": llm_evaluation.get('coverage', 0),
            #     "llm_comments": llm_evaluation.get('comments', 'N/A')
            # }
            # result['evaluation'] = evaluation
            
            # 11.1 保存评估结果
            # logger.info("💾 保存评估结果...")
            # with open(output_dir / "evaluation.json", 'w', encoding='utf-8') as f:
            #     json.dump(evaluation, f, ensure_ascii=False, indent=2)
            # logger.info(f"  ✓ 评估结果已保存到: {output_dir / 'evaluation.json'}")
            
            # 12. 更新状态
            result['status'] = 'success'
            result['duration'] = extraction_duration
            result['end_time'] = datetime.now().isoformat()
            
            logger.info(f"\n✅ {case_id} 处理成功！")
            logger.info(f"⏱️  耗时: {int(result['duration'])}秒")
            # logger.info(f"📊 评估结果:")
            # logger.info(f"   - Cell召回率: {evaluation['cell_recall']:.2f}%")
            # logger.info(f"   - Cell准确率: {evaluation['cell_precision']:.2f}%")
            # logger.info(f"   - LLM分数: {evaluation['llm_score']:.1f}/100")
            # logger.info(f"💬 评语: {evaluation['llm_comments']}")
            
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
        logger.info("🚀 LLM 抽取能力评估开始")
        logger.info("="*80 + "\n")
        
        # 1. 扫描数据集
        logger.info("📁 扫描数据集...")
        cases = self.scan_datasets()

        # print(cases)
        # exit()

        if not cases:
            logger.error("❌ 未找到有效的测试案例")
            return
        
        # 2. 逐个处理测试案例
        for i, case_info in enumerate(cases, 1):
            logger.info(f"\n{'#'*80}")
            logger.info(f"# 进度: {i}/{len(cases)}")
            logger.info(f"{'#'*80}")
            
            result = self.process_case(case_info)
            self.results.append(result)
            
            # 简短延迟，避免请求过快
            time.sleep(1)


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='LLM 抽取能力评测系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法:
  # 使用默认模型，Oracle模式（会跳过已处理的案例）
  python llm_evaluate_cascade.py --eval-mode oracle
  
  # Pipeline（端到端）模式
  python llm_evaluate_cascade.py --eval-mode pipeline

  # Pipeline-Oracle 模式（实体走预测，关系直接使用GT）
  python llm_evaluate_cascade.py --eval-mode pipeline-oracle
  
  # 指定模型
  python llm_evaluate_cascade.py --model claude-sonnet-4-20250514 --eval-mode oracle --db-name customer_product
  
  # 强制重新运行所有案例
  python llm_evaluate_cascade.py --force-rerun --eval-mode pipeline
  
  # 指定运行名称（用于多次实验）
  python llm_evaluate_cascade.py --model gpt-4o --run-name exp_001 --eval-mode oracle
  
  # 指定chunk大小（处理长文档）
  python llm_evaluate_cascade.py --chunk-size 15000 --chunk-overlap 800

  # 只跑某个数据库下的某个 case（可与其它参数组合）
  python llm_evaluate_cascade.py --db-name medical --model glm-5.1 --eval-mode pipeline-oracle --case-name case2 
  
  # 完整示例
  python llm_evaluate_cascade.py --model qwen-long --run-name test_001 --chunk-size 20000 --force-rerun --eval-mode pipeline

  # 指定 ours 后端端口（仅对开源模型路由生效）
  python llm_evaluate_cascade.py --db-name education --model qwen2.5-72b-instruct --eval-mode pipeline --ours-port 8888 

评测模式说明:
  oracle   - 真值反馈模式：关系表的前置上下文来自 expected_answer（GT），
             衡量模型在完美前置条件下的理论关系抽取上限。
  pipeline - 端到端模式：关系表的前置上下文 100%% 来自模型自己抽取的数据，
             衡量误差累积（Error Propagation）对系统的真实影响。
  pipeline-oracle - 混合模式：实体表走模型抽取；关系表继续调用LLM，但关系抽取参考表
                    来自 expected_answer（GT 实体表），用于隔离实体误差传播。
        '''
    )
    
    parser.add_argument('--model', type=str, default=DEFAULT_MODEL,
                        help=f'使用的模型名称（默认: {DEFAULT_MODEL}）')
    parser.add_argument('--run-name', type=str, default=None,
                        help='运行名称，用于区分不同的实验（可选）')
    parser.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f'文档分chunk的大小（字符数，默认: {DEFAULT_CHUNK_SIZE}）')
    parser.add_argument('--chunk-overlap', type=int, default=DEFAULT_CHUNK_OVERLAP,
                        help=f'chunk之间的重叠部分（字符数，默认: {DEFAULT_CHUNK_OVERLAP}）')
    parser.add_argument('--force-rerun', action='store_true',
                        help='强制重新运行所有案例，即使已经处理过（默认: False，会跳过已处理的案例）')
    parser.add_argument('--eval-mode', type=str, default='pipeline', choices=['oracle', 'pipeline', 'pipeline-oracle'],
                        help='评测模式: oracle=真值反馈, pipeline=端到端, pipeline-oracle=实体预测+关系真值（默认: pipeline）')
    parser.add_argument('--db-name', type=str, default=None,
                        help='只处理该数据库目录名（与 dataset 下文件夹名一致，默认处理全部）')
    parser.add_argument('--case-name', type=str, default=None,
                        help='只处理该 case 文件夹名（如 case1，默认处理全部）')
    parser.add_argument('--ours-port', type=int, default=None,
                        help='开源模型(ours后端)端口号；仅当模型走 llm/ours.py 时生效')
    parser.add_argument('--single', action='store_true', help='在 Pipeline 模式下，将所有实体表打包在一次 Prompt 中提取')
    
    args = parser.parse_args()
    
    eval_mode_desc_map = {
        "oracle": "Oracle（真值反馈）",
        "pipeline": "Pipeline（端到端）",
        "pipeline-oracle": "Pipeline-Oracle（实体预测 + 关系GT参考抽取）",
    }
    eval_mode_desc = eval_mode_desc_map.get(args.eval_mode, args.eval_mode)
    
    print("\n" + "="*80)
    print("🌟 LLM 抽取能力评测系统")
    print("="*80)
    print(f"🤖 模型: {args.model}")
    print(f"🎯 评测模式: {eval_mode_desc}")
    print(f"⚡ Single模式: {'启用' if args.single else '关闭'}")
    if args.eval_mode == "oracle":
        print(f"   → 关系表的前置上下文来自 expected_answer（GT），测量理论抽取上限")
    elif args.eval_mode == "pipeline-oracle":
        print(f"   → 实体表来自模型预测；关系表仍调用LLM，但参考表来自 expected_answer（GT）")
    else:
        print(f"   → 关系表的前置上下文 100% 来自模型自己的抽取结果，测量误差累积")
    if args.run_name:
        print(f"🏷️  运行名称: {args.run_name}")
    print(f"📏 Chunk大小: {args.chunk_size} 字符, 重叠: {args.chunk_overlap} 字符")
    if args.force_rerun:
        print(f"🔄 模式: 强制重新运行所有案例")
    else:
        print(f"⏭️  模式: 跳过已处理的案例")
    if args.db_name or args.case_name:
        fparts = []
        if args.db_name:
            fparts.append(f"数据库={args.db_name}")
        if args.case_name:
            fparts.append(f"案例={args.case_name}")
        print(f"🔎 案例过滤: {', '.join(fparts)}")
    if args.ours_port is not None:
        print(f"🔌 ours端口: {args.ours_port}")
    print("="*80 + "\n")
    
    # 创建评估器
    evaluator = LLMExtractorEvaluator(
        model=args.model,
        run_name=args.run_name,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        force_rerun=args.force_rerun,
        eval_mode=args.eval_mode,
        db_name_filter=args.db_name,
        case_name_filter=args.case_name,
        ours_port=args.ours_port,
        single_mode=args.single
    )
    
    # # 运行评估
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

