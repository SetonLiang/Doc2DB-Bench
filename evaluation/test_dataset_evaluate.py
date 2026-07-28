#!/usr/bin/env python3
"""
Doc2DB 数据集评测脚本
用于测评系统在不同数据集上的性能表现
"""

import os
import sys
import json
import time
import glob
import shutil
from pandas.core.series import com
import requests
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
from openai import OpenAI

project_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, project_root)

from dotenv import dotenv_values
from utils import (
    _clean_response,
    load_expected_answer as util_load_expected_answer,
    convert_schema_format as util_convert_schema_format,
    load_result_from_output as util_load_result_from_output,
    merge_documents_tables as util_merge_documents_tables,
    load_best_snapshots_from_file as util_load_best_snapshots_from_file,
    extract_tables_from_full_result as util_extract_tables_from_full_result,
    save_output as util_save_output,
)
env_path = Path(__file__).parent.parent/ "llm" / ".env"
config = dotenv_values(env_path)
api_url = config.get("GPT_URL_PRO")
api_key = config.get("GPT_KEY_PRO")

client = OpenAI(
    api_key=api_key,
    base_url=api_url
)


logger = logging.getLogger(__name__)

BACKEND_URL = "http://localhost:5000"
DATASET_DIR = Path(__file__).parent.parent / "dataset/case/base_latest"
# OUTPUT_DIR = Path(__file__).parent.parent / "dataset/case/base_latest_output/docetl"
OUTPUT_DIR = Path(__file__).parent.parent / "dataset/case/base_latest_output/llm"
POLL_INTERVAL = 2
MAX_WAIT_TIME = 7200

DEFAULT_MODEL = "gpt-4o"
DEFAULT_RUN_NAME = None 


class Doc2DBEvaluator:
    """Doc2DB评估器"""
    
    def __init__(self, backend_url: str, dataset_dir: Path, output_dir: Path,
                 model: str = DEFAULT_MODEL, run_name: Optional[str] = None,
                 document_mode: str = 'single', enable_llm_eval: bool = True,
                 db_name_filter: Optional[str] = None, case_name_filter: Optional[str] = None,
                 eval_mode: str = 'pipeline', evaluate_entity_pk: bool = False):
        self.backend_url = backend_url
        self.dataset_dir = dataset_dir
        self.output_dir = output_dir
        self.model = model
        self.run_name = run_name
        self.document_mode = document_mode
        self.enable_llm_eval = enable_llm_eval
        self.db_name_filter = db_name_filter
        self.case_name_filter = case_name_filter
        self.eval_mode = eval_mode
        self.evaluate_entity_pk = evaluate_entity_pk
        self.results = []
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Output directory: {self.output_dir}")
        logger.info(f"Model: {model}")
        logger.info(f"Document mode: {document_mode}")
        logger.info(f"Eval mode: {eval_mode}")
        logger.info(
            "Evaluate Entity PK: meta-driven via case/meta.json if_primary_allowed "
            f"(fallback flag: {'include' if evaluate_entity_pk else 'skip'})"
        )
        logger.info(f"LLM evaluation: {'enabled' if enable_llm_eval else 'disabled (P/R/F1 only)'}")
        if run_name:
            logger.info(f"Run name: {run_name}")
        if db_name_filter or case_name_filter:
            fparts = []
            if db_name_filter:
                fparts.append(f"db_name={db_name_filter}")
            if case_name_filter:
                fparts.append(f"case_name={case_name_filter}")
            logger.info(f"Case filter: {', '.join(fparts)}")
    
    def _build_filename_prefix(self, db_name: str, case_name: str, model: str) -> str:
        """Build filename prefix: DB_case_model_mode"""
        safe_model = model.replace('/', '_').replace('\\', '_').replace('-', '_')
        mode = self.document_mode
        return f"{db_name}_{case_name}_{safe_model}_{mode}"

    def _get_evaluation_filename(self, name_prefix: str) -> str:
        """按评估模式生成评估文件名。"""
        if self.eval_mode == "oracle":
            return f"{name_prefix}_evaluate_oracle.json"
        if self.eval_mode == "pipeline-oracle":
            return f"{name_prefix}_evaluate_pipeline_oracle.json"
        return f"{name_prefix}_evaluate.json"
    
    def check_case_already_processed(self, case_info: Dict[str, Any]) -> bool:
        """Check if case has been processed (严格根据当前 model 判断)"""
        case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
        
        if not case_dir.exists():
            return False
        
        # 1. 检查标准格式的文件夹
        filename_prefix = self._build_filename_prefix(
            case_info['db_name'], 
            case_info['case_name'], 
            self.model
        )
        prefix_dir = case_dir / filename_prefix
        key_files = [
            "output_tables.json",
            "extracted_data.json",
            "extracted_data_pipeline_oracle.json",
            "oracle_relation.json",
        ]
        
        if prefix_dir.exists() and prefix_dir.is_dir():
            for f in key_files:
                if (prefix_dir / f).exists():
                    logger.info(f"   Found existing output: {filename_prefix}/{f}")
                    return True
        
        # 2. 如果标准格式不存在，必须通过 metadata.json 精确匹配 model
        for subdir in case_dir.iterdir():
            if not subdir.is_dir():
                continue
            metadata_file = subdir / "metadata.json"
            if metadata_file.exists():
                try:
                    with open(metadata_file, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                    # 必须精确等于当前 model！
                    if metadata.get('model') == self.model:
                        for f in key_files:
                            if (subdir / f).exists():
                                logger.info(f"   Found existing output via metadata: {subdir.name}/{f}")
                                return True
                except Exception:
                    continue
                    
        return False
    
    def check_case_already_evaluated(self, case_info: Dict[str, Any]) -> tuple[bool, Optional[Path], Optional[str]]:
        """检查案例是否已经评估过 (严格根据当前 model 判断)"""
        case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
        
        if not case_dir.exists():
            return False, None, None
        
        # 1. 首先尝试匹配标准格式的文件夹
        filename_prefix = self._build_filename_prefix(
            case_info['db_name'], 
            case_info['case_name'], 
            self.model
        )
        prefix_output_dir = case_dir / filename_prefix
        
        if prefix_output_dir.exists() and prefix_output_dir.is_dir():
            evaluation_file = prefix_output_dir / self._get_evaluation_filename(filename_prefix)
            if evaluation_file.exists():
                logger.info(f"   找到已有评估文件: {filename_prefix}/{evaluation_file.name}")
                return True, prefix_output_dir, filename_prefix
        
        # 2. 如果标准格式不存在，通过 metadata.json 精确查找
        for subdir in case_dir.iterdir():
            if not subdir.is_dir():
                continue
            
            metadata_file = subdir / "metadata.json"
            if metadata_file.exists():
                try:
                    with open(metadata_file, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                    if metadata.get('model') == self.model: # 精确匹配
                        evaluation_file = subdir / self._get_evaluation_filename(subdir.name)
                        if evaluation_file.exists():
                            logger.info(f"   找到已有评估文件: {subdir.name}/{evaluation_file.name}")
                            return True, subdir, subdir.name
                except Exception:
                    continue
        
        return False, None, None
        
    def check_backend_health(self) -> bool:
        """检查后端服务是否可用"""
        try:
            response = requests.get(f"{self.backend_url}/api/health", timeout=5)
            if response.status_code == 200:
                logger.info(f" 后端服务正常: {self.backend_url}")
                return True
            else:
                logger.error(f" 后端服务异常，状态码: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f" 无法连接到后端服务: {e}")
            return False
    
    def scan_datasets(self) -> List[Dict[str, Any]]:
        """扫描所有数据集和case"""
        cases = []
        
        if not self.dataset_dir.exists():
            logger.error(f" 数据集目录不存在: {self.dataset_dir}")
            return cases
        
        for db_dir in self.dataset_dir.iterdir():
            if not db_dir.is_dir():
                continue
            
            db_name = db_dir.name
            logger.info(f" 扫描数据库: {db_name}")
            
            for case_dir in db_dir.iterdir():
                if not case_dir.is_dir() or not case_dir.name.startswith("case"):
                    continue
                
                case_name = case_dir.name
                
                docs_dir = case_dir / "docs"
                schema_file = case_dir / "schema.json"
                answer_file = case_dir / "answer.json"
                answer_dir = case_dir / "answer"
                tables_dir = case_dir / "tables"
                
                if not docs_dir.exists():
                    logger.warning(f"️  {db_name}/{case_name}: docs目录不存在，跳过")
                    continue
                
                if not schema_file.exists():
                    logger.warning(f"️  {db_name}/{case_name}: schema.json不存在，跳过")
                    continue
                
                has_answer_file = answer_file.is_file()
                answer_json_files = sorted(answer_dir.glob("*.json")) if answer_dir.is_dir() else []
                table_json_files = (
                    sorted(
                        p
                        for p in tables_dir.glob("*.json")
                        if p.is_file() and p.name.lower() != "schema.json"
                    )
                    if tables_dir.is_dir()
                    else []
                )
                if has_answer_file:
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
                        f"️  {db_name}/{case_name}: 无标准答案（需要 answer.json、answer/*.json 或 tables/*.json），跳过"
                    )
                    continue
                
                doc_files = []
                for ext in ['*.txt', '*.pdf', '*.md', '*.docx', '*.doc']:
                    doc_files.extend(list(docs_dir.glob(ext)))
                
                if not doc_files:
                    logger.warning(f"️  {db_name}/{case_name}: docs目录为空，跳过")
                    continue
                
                cases.append({
                    'db_name': db_name,
                    'case_name': case_name,
                    'case_dir': case_dir,
                    'docs_dir': docs_dir,
                    'schema_file': schema_file,
                    'answer_file': answer_file if has_answer_file else None,
                    'answer_dir': answer_dir if answer_json_files else None,
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
                logger.info(f"   {db_name}/{case_name}: 发现 {len(doc_files)} 个文档文件，标准答案: {ans_desc}")
        
        logger.info(f"\n 扫描到 {len(cases)} 个有效测试案例")
        if self.db_name_filter:
            before = len(cases)
            cases = [c for c in cases if c['db_name'] == self.db_name_filter]
            logger.info(f"   按 --db-name 过滤: {before} -> {len(cases)}")
        if self.case_name_filter:
            before = len(cases)
            cases = [c for c in cases if c['case_name'] == self.case_name_filter]
            logger.info(f"   按 --case-name 过滤: {before} -> {len(cases)}")
        logger.info(f" 将处理 {len(cases)} 个案例\n")
        return cases

    def load_expected_answer(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """加载标准答案：支持 answer.json、answer/*.json、tables/*.json。"""
        src = case_info.get("answer_source")
        path = case_info.get("answer_path")
        if src == "tables" and path:
            tables_path = Path(path)
            expected: Dict[str, Any] = {}
            for table_file in sorted(tables_path.glob("*.json")):
                if table_file.name.lower() == "schema.json":
                    continue
                with open(table_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                    expected[table_file.stem] = payload["rows"]
                elif isinstance(payload, list):
                    expected[table_file.stem] = payload
                elif isinstance(payload, dict):
                    for k, v in payload.items():
                        if k in {"capability_assignments", "capability_assignments_source", "evidence"}:
                            continue
                        if isinstance(v, list):
                            expected[str(k)] = v
                        elif isinstance(v, dict) and isinstance(v.get("rows"), list):
                            expected[str(k)] = v["rows"]
                else:
                    logger.warning(f"  ⚠️ 跳过 tables 文件（不支持格式）: {table_file.name}")
            if expected:
                return expected
            raise ValueError(f"tables/ 目录无有效表数据: {tables_path}")
        return util_load_expected_answer(case_info)
    
    def upload_documents(self, doc_files: List[str]) -> List[str]:
        """注册文档文件路径到后端（不需要实际上传文件）"""
        uploaded_paths = []
        
        for doc_file in doc_files:
            try:
                # 直接传递文件路径，后端会读取文件
                response = requests.post(
                    f"{self.backend_url}/api/upload",
                    json={"filepath": doc_file},
                    timeout=60
                )
                
                if response.status_code == 200:
                    result = response.json()
                    uploaded_path = result.get('filepath', '')
                    uploaded_paths.append(uploaded_path)
                    logger.info(f"   注册成功: {os.path.basename(doc_file)}")
                elif response.status_code == 404:
                    logger.error(f"   文件不存在: {os.path.basename(doc_file)}")
                else:
                    logger.error(f"   注册失败: {os.path.basename(doc_file)} - {response.text}")
                    
            except Exception as e:
                logger.error(f"   注册异常: {os.path.basename(doc_file)} - {e}")
        
        return uploaded_paths
    
    def start_processing(self, files: List[str], schema: Dict[str, Any]) -> Optional[str]:
        """启动文档处理任务"""
        try:
            converted_schema = self._convert_schema_format(schema)
            
            table_name = ''
            tables_count = len(converted_schema.get('tables', []))
            if tables_count == 1:
                table_name = converted_schema['tables'][0]['name']
            elif tables_count > 1:
                table_name = ''
                logger.info(f"Schema contains {tables_count} tables, will process all tables")
            
            request_data = {
                'files': files,
                'db_schema': json.dumps(converted_schema),
                'table_name': table_name,
                'input_mode': 'schema',
                'schema_mode': 'json',
                'model': self.model,
                'nl_prompt': '',
                'processing_mode': 'serial',
                'max_concurrent_tables': 3,
                'document_mode': self.document_mode
            }
            
            response = requests.post(
                f"{self.backend_url}/api/doc2db/process",
                json=request_data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                task_id = result.get('task_id')
                logger.info(f"Processing task started: {task_id}")
                return task_id
            else:
                logger.error(f"Failed to start processing: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error starting processing: {e}")
            return None
    
    def _convert_schema_format(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        return util_convert_schema_format(schema, logger=logger)
    
    def wait_for_completion(self, task_id: str) -> Optional[Dict[str, Any]]:
        """等待任务完成并返回结果"""
        start_time = time.time()
        
        while True:
            try:
                elapsed = time.time() - start_time
                if elapsed > MAX_WAIT_TIME:
                    logger.error(f"   任务超时 ({MAX_WAIT_TIME}秒)")
                    return None
                
                response = requests.get(
                    f"{self.backend_url}/api/doc2db/status/{task_id}",
                    timeout=10
                )
                
                if response.status_code != 200:
                    logger.error(f"   获取状态失败: {response.text}")
                    time.sleep(POLL_INTERVAL)
                    continue
                
                status_data = response.json()
                status = status_data.get('status', 'unknown')
                current_step = status_data.get('current_step', '')
                
                if current_step:
                    logger.info(f"   处理中 [{int(elapsed)}s]: {current_step}")
                
                if status == 'completed':
                    logger.info(f"   任务完成 (耗时 {int(elapsed)}秒)")
                    return status_data
                elif status == 'failed':
                    error_msg = status_data.get('error', '未知错误')
                    logger.error(f"   任务失败: {error_msg}")
                    return None
                
                time.sleep(POLL_INTERVAL)
                
            except Exception as e:
                logger.error(f"   轮询状态异常: {e}")
                time.sleep(POLL_INTERVAL)
    
    def extract_result_data(self, status_data: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
        """从状态数据中提取结果"""
        try:
            result = status_data.get('result', {})
            result_context = status_data.get('result_context', {})
            
            tables = result.get('tables', {})
            if not tables and result_context:
                tables = result_context.get('tables', {})
            
            steps = status_data.get('steps', [])
            
            return {
                'tables': tables,
                'summary': result.get('summary', result_context.get('summary', {})),
                'full_result': result if result else result_context,
                'steps': steps,  # 保存所有处理步骤
                'task_id': task_id
            }
            
        except Exception as e:
            logger.error(f"   提取结果数据失败: {e}")
            return None
    
    def load_result_from_output(self, prefix_output_dir: Path, filename_prefix: str) -> Optional[Dict[str, Any]]:
        return util_load_result_from_output(
            prefix_output_dir=prefix_output_dir,
            eval_mode=self.eval_mode,
            logger=logger
        )
    
    def _merge_documents_tables(self, documents: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        return util_merge_documents_tables(documents, logger=logger)
    
    def _load_best_snapshots_from_file(self, snapshots_file: Path) -> Dict[str, List[Dict[str, Any]]]:
        return util_load_best_snapshots_from_file(snapshots_file, logger=logger)
    
    def _extract_tables_from_full_result(self, full_result: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        return util_extract_tables_from_full_result(full_result, logger=logger)
    
    def save_output(self, case_info: Dict[str, Any], result_data: Dict[str, Any]) -> tuple[Path, str]:
        filename_prefix = self._build_filename_prefix(
            case_info['db_name'],
            case_info['case_name'],
            self.model
        )
        return util_save_output(
            case_info=case_info,
            result_data=result_data,
            output_dir=self.output_dir,
            filename_prefix=filename_prefix,
            model=self.model,
            run_name=self.run_name,
            document_mode=self.document_mode,
            backend_output_root=Path(__file__).parent.parent / "backend" / "output",
            logger=logger
        )
    
    def get_value_type(self, value: Any) -> str:
        """判断值的类型"""
        if value is None or value == "":
            return "null"
        
        if isinstance(value, (int, float)):
            return "numeric"
        
        if isinstance(value, str):
            try:
                float(value.replace(',', ''))  # 处理千分位分隔符
                return "numeric"
            except ValueError:
                pass
            
            import re
            if re.match(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', value.strip()):
                return "date"
            
            if value.strip().lower() in ['true', 'false', 'yes', 'no', '是', '否']:
                return "boolean"
        
        if isinstance(value, bool):
            return "boolean"
        
        return "string"
    
    def normalize_numeric_value(self, value: Any) -> tuple[float, int]:
        """标准化数值，返回(数值, 小数位数)"""
        if value is None or value == "":
            return None, 0
        
        try:
            if isinstance(value, str):
                clean_value = value.replace(',', '').replace(' ', '').strip()
                num_value = float(clean_value)
            else:
                num_value = float(value)
            
            if isinstance(value, str) and '.' in value:
                decimal_places = len(value.split('.')[-1].rstrip('0'))
            elif isinstance(value, float) and not value.is_integer():
                decimal_places = len(str(value).split('.')[-1].rstrip('0'))
            else:
                decimal_places = 0
                
            return num_value, decimal_places
        except (ValueError, TypeError):
            return None, 0
    
    def normalize_string_value(self, value: Any) -> str:
        """标准化字符串值"""
        if value is None or value == "":
            return ""
        
        text = str(value).strip()
        import re
        text = re.sub(r'\s+', ' ', text)
        return text
    
    def _is_chinese_entity_match(self, str1: str, str2: str) -> bool:
        """Check if two strings match as Chinese entity abbreviations"""
        if not str1 or not str2:
            return False
        
        import re
        
        s1_no_space = re.sub(r'\s+', '', str1)
        s2_no_space = re.sub(r'\s+', '', str2)
        
        if len(s1_no_space) >= 4 and s1_no_space in s2_no_space:
            company_suffixes = ['股份有限公司', '有限公司', '公司', '集团', '股份', '(集团)', '（集团）']
            s2_core = s2_no_space
            for suffix in company_suffixes:
                s2_core = s2_core.replace(suffix, '')
            if s1_no_space in s2_core or s2_core in s1_no_space:
                return True
                
        if len(s2_no_space) >= 4 and s2_no_space in s1_no_space:
            company_suffixes = ['股份有限公司', '有限公司', '公司', '集团', '股份', '(集团)', '（集团）']
            s1_core = s1_no_space
            for suffix in company_suffixes:
                s1_core = s1_core.replace(suffix, '')
            if s2_no_space in s1_core or s1_core in s2_no_space:
                return True
        
        year_pattern = r'20\d{2}'
        quarter_patterns = {
            'Q1': ['q1', '一季度', '第一季度', '1季度'],
            'Q2': ['q2', '二季度', '第二季度', '2季度'],
            'Q3': ['q3', '三季度', '第三季度', '3季度'],
            'Q4': ['q4', '四季度', '第四季度', '4季度'],
        }
        
        year1_match = re.search(year_pattern, str1)
        year2_match = re.search(year_pattern, str2)
        
        if year1_match and year2_match:
            year1 = year1_match.group()
            year2 = year2_match.group()
            
            if year1 == year2:
                str1_lower = str1.lower()
                str2_lower = str2.lower()
                
                for quarter, patterns in quarter_patterns.items():
                    found_in_s1 = quarter.lower() in str1_lower or any(p in str1_lower for p in patterns)
                    found_in_s2 = quarter.lower() in str2_lower or any(p in str2_lower for p in patterns)
                    
                    if found_in_s1 and found_in_s2:
                        return True
        
        chinese_words_1 = set(re.findall(r'[\u4e00-\u9fa5]{3,}', s1_no_space))
        chinese_words_2 = set(re.findall(r'[\u4e00-\u9fa5]{3,}', s2_no_space))
        
        if chinese_words_1 and chinese_words_2:
            common_words = chinese_words_1 & chinese_words_2
            if common_words:
                min_words_count = min(len(chinese_words_1), len(chinese_words_2))
                if len(common_words) / min_words_count >= 0.5:
                    return True
        
        return False
    
    def calculate_string_similarity(self, str1: str, str2: str) -> float:
        """计算字符串相似度（使用编辑距离）"""
        if str1 == str2:
            return 1.0
        
        s1 = self.normalize_string_value(str1).lower()
        s2 = self.normalize_string_value(str2).lower()
        
        if s1 == s2:
            return 1.0
        
        if self._is_chinese_entity_match(s1, s2):
            return 0.95  # 给予高相似度但不是完全匹配
        
        def levenshtein_distance(a, b):
            if len(a) < len(b):
                return levenshtein_distance(b, a)
            
            if len(b) == 0:
                return len(a)
            
            previous_row = list(range(len(b) + 1))
            for i, c1 in enumerate(a):
                current_row = [i + 1]
                for j, c2 in enumerate(b):
                    insertions = previous_row[j + 1] + 1
                    deletions = current_row[j] + 1
                    substitutions = previous_row[j] + (c1 != c2)
                    current_row.append(min(insertions, deletions, substitutions))
                previous_row = current_row
            
            return previous_row[-1]
        
        max_len = max(len(s1), len(s2))
        if max_len == 0:
            return 1.0
        
        distance = levenshtein_distance(s1, s2)
        similarity = 1 - (distance / max_len)
        return max(0, similarity)
    
    def _is_descriptive_field_match(self, expected_str: str, generated_str: str) -> bool:
        """Check if descriptive fields match using keyword extraction"""
        if not expected_str or not generated_str:
            return False
        
        exp = expected_str.lower().strip()
        gen = generated_str.lower().strip()
        
        if len(gen) > len(exp) * 2:
            import re
            exp_clean = re.sub(r'[^\w]', '', exp)
            gen_clean = re.sub(r'[^\w]', '', gen)
            if exp_clean in gen_clean:
                return True
        
        if exp in gen or gen in exp:
            return True
        
        import re
        
        stop_words = {
            '的', '了', '和', '是', '在', '有', '与', '对', '及', '等', 
            '等等', '为', '以', '由', '被', '将', '可', '也', '都', '而',
            '本', '该', '此', '这', '那', '其', '之', '于', '从', '到',
            '课程', '本课程', '该课程', '学生', '设计', '提供',
            '介绍', '涵盖', '探讨', '包括', '教授'
        }
        
        def extract_keywords(text):
            keywords = set()
            
            chinese_phrases = re.findall(r'[\u4e00-\u9fa5]{2,6}', text)
            for phrase in chinese_phrases:
                if phrase not in stop_words:
                    keywords.add(phrase)
                    if len(phrase) > 2:
                        for i in range(len(phrase) - 1):
                            sub_phrase = phrase[i:i+2]
                            if sub_phrase not in stop_words:
                                keywords.add(sub_phrase)
            
            english_words = re.findall(r'[a-z0-9]+', text.lower())
            for word in english_words:
                if len(word) > 1 and word not in stop_words:
                    keywords.add(word)
            
            return keywords
        
        expected_keywords = extract_keywords(exp)
        generated_keywords = extract_keywords(gen)
        
        if not expected_keywords:
            return True
        
        matched_keywords = expected_keywords & generated_keywords
        keyword_match_ratio = len(matched_keywords) / len(expected_keywords)
        
        if keyword_match_ratio >= 0.3:
            return True
        
        core_concepts_matched = 0
        for exp_keyword in expected_keywords:
            for gen_keyword in generated_keywords:
                if exp_keyword in gen_keyword or gen_keyword in exp_keyword:
                    core_concepts_matched += 1
                    break
        
        partial_match_ratio = core_concepts_matched / len(expected_keywords) if expected_keywords else 0
        
        return partial_match_ratio >= 0.5
    
    # eval-mode: oracle的细粒度评估
    def _classify_tables_fine_grained(self, schema: Dict[str, Any]) -> Dict[str, str]:
        """
        基于有向无环图(DAG)动态规划，对表进行极其细粒度的拓扑分类
        分类标准：
        - Entity: 实体表（依赖深度为 0，无外键）
        - 1-hop (1-to-1): 一跳关系，指向 1 个实体表
        - 1-hop (1-to-X): 一跳关系，指向 X 个实体表 (如 1-to-2, 1-to-3)
        - X-hop: 多跳级联关系，最大依赖深度为 X (如 2-hop, 3-hop)
        """
        import networkx as nx
        G = nx.DiGraph()
        
        schema = self._normalize_schema_for_metrics(schema)
        tables = schema.get('tables', [])
        fk_counts = {} # 记录每个表的实际外键总数，用于精确判断 1-to-几
        
        # 1. 初始化节点和外键计数器
        for t in tables:
            if isinstance(t, dict) and 'name' in t:
                table_name = t['name'].lower()
                G.add_node(table_name)
                fk_counts[table_name] = 0
                
        # 2. 构建外键依赖边 (A 依赖 B，则添加边 A -> B)，同时统计外键数量
        for t in tables:
            if not isinstance(t, dict) or 'name' not in t:
                continue
            table_name = t['name'].lower()
            attrs = t.get('attributes', []) or t.get('fields', [])
            for attr in attrs:
                fk = attr.get('constraints', {}).get('foreign_key')
                if fk and isinstance(fk, dict) and 'table' in fk:
                    ref_table = fk['table'].lower()
                    if ref_table in G.nodes:
                        G.add_edge(table_name, ref_table)
                        fk_counts[table_name] += 1
                        
        # 3. 利用逆向拓扑排序和动态规划计算每个节点的最大依赖深度 (Hop数)
        depths = {}
        try:
            # 从图的最深层（叶子节点，即Entity）开始往回推
            for node in reversed(list(nx.topological_sort(G))):
                successors = list(G.successors(node))
                if not successors:
                    depths[node] = 0  # 没有依赖任何表，深度为 0
                else:
                    # 当前节点的深度 = 1 + 它依赖的最深那个表的深度
                    depths[node] = 1 + max(depths[neighbor] for neighbor in successors)
        except nx.NetworkXUnfeasible:
            logger.warning("⚠️ Schema 中检测到循环依赖，退回默认全表实体模式。")
            return {node: 'Entity' for node in G.nodes}

        # 4. 根据深度(Depth)和外键数量(FK count)进行绝对细粒度的分类打标
        fine_grained_classes = {}
        for node in G.nodes:
            depth = depths[node]
            fk_count = fk_counts[node]
            
            if depth == 0:
                fine_grained_classes[node] = 'Entity'
            elif depth == 1:
                # 深度为1说明只直接依赖基础实体表，根据外键数量精确划分 1-to-几
                if fk_count == 1:
                    fine_grained_classes[node] = '1-hop (1-to-1)'
                else:
                    fine_grained_classes[node] = f'1-hop (1-to-{fk_count})'
            else:
                # 深度 >= 2，说明它的外键指向了另一个带有外键的表，是多跳级联关系
                # 如果这个多跳表本身还有多个外键，你也可以选择拼上 fk_count，例如 f'{depth}-hop (1-to-{fk_count})'
                # 这里为了分类清晰，我们主打标注它的深度：
                fine_grained_classes[node] = f'{depth}-hop'
                
        return fine_grained_classes

    def calculate_oracle_fine_grained_metrics(self, cell_metrics: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        在 Oracle 模式下，按细粒度拓扑结构分类统计 P/R/F1
        """
        table_classes = self._classify_tables_fine_grained(schema)
        
        # 初始化分类统计池
        category_stats = {}
        for category in set(table_classes.values()):
            category_stats[category] = {
                'expected_cells': 0,
                'generated_cells': 0,
                'correct_cells': 0
            }
            
        # 遍历已计算出的各表级指标，对号入座
        table_metrics = cell_metrics.get('table_metrics', {})
        for table_name, metrics in table_metrics.items():
            category = table_classes.get(table_name.lower(), 'Entity')
            
            if category not in category_stats:
                category_stats[category] = {'expected_cells': 0, 'generated_cells': 0, 'correct_cells': 0}
                
            category_stats[category]['expected_cells'] += metrics['expected_cells']
            category_stats[category]['generated_cells'] += metrics['generated_cells']
            category_stats[category]['correct_cells'] += metrics['correct_cells']
            
        # 计算每一类的 P/R/F1
        oracle_results = {}
        for category, stats in category_stats.items():
            exp = stats['expected_cells']
            gen = stats['generated_cells']
            corr = stats['correct_cells']
            
            recall = (corr / exp * 100) if exp > 0 else 0
            precision = (corr / gen * 100) if gen > 0 else 0
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0
            
            oracle_results[category] = {
                'precision': precision,
                'recall': recall,
                'f1': f1,
                'expected_cells': exp,
                'correct_cells': corr
            }
            
        return oracle_results

    def _build_oracle_relation_table_metrics(self, cell_metrics: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        """Oracle模式下仅保留关系表指标，并为每个关系表添加hop类型。"""
        table_metrics = cell_metrics.get('table_metrics', {}) or {}
        table_types = self.get_table_types_from_schema(schema) if schema else {}
        table_hop_types = self._classify_tables_fine_grained(schema) if schema else {}

        relation_table_metrics = {}
        for table_name, metrics in table_metrics.items():
            table_name_lower = table_name.lower()
            schema_type = str(table_types.get(table_name_lower, '')).lower()
            hop_type = table_hop_types.get(table_name_lower, 'unknown')

            # 优先使用schema中的type判断；若缺失，退化为根据hop类别推断（Entity之外视为关系表）。
            is_relation = (schema_type == 'relation') or (not schema_type and hop_type != 'Entity')
            if not is_relation:
                continue

            table_metric_with_type = dict(metrics)
            table_metric_with_type['type'] = hop_type
            relation_table_metrics[table_name] = table_metric_with_type

        return relation_table_metrics

    def _build_evaluation_payload(
        self,
        cell_metrics: Dict[str, Any],
        structural_metrics: Dict[str, Any],
        llm_evaluation: Dict[str, Any],
        oracle_fine_grained_metrics: Dict[str, Any],
        timestamp_info: Dict[str, Any],
        schema: Dict[str, Any] = None,
        error_analysis: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """统一构建评估结果；oracle模式下仅输出关系表评估。"""
        if self.eval_mode == "oracle":
            evaluation = {
                "relation_table_metrics": self._build_oracle_relation_table_metrics(cell_metrics, schema),
                "relation_precision": structural_metrics.get('relation_precision', 0),
                "relation_recall": structural_metrics.get('relation_recall', 0),
                "relation_f1": structural_metrics.get('relation_f1', 0),
                "relation_expected_cells": structural_metrics.get('relation_expected_cells', 0),
                "relation_generated_cells": structural_metrics.get('relation_generated_cells', 0),
                "relation_correct_cells": structural_metrics.get('relation_correct_cells', 0),
                "oracle_fine_grained_metrics": oracle_fine_grained_metrics,
                "timestamp": timestamp_info
            }
            return evaluation

        evaluation = {
            "cell_recall": cell_metrics['cell_recall'],
            "cell_precision": cell_metrics['cell_precision'],
            "cell_f1": cell_metrics['cell_f1'],
            "total_expected_cells": cell_metrics['total_expected_cells'],
            "total_generated_cells": cell_metrics['total_generated_cells'],
            "total_correct_cells": cell_metrics['total_correct_cells'],
            "table_metrics": cell_metrics.get('table_metrics', {}),

            "table_recall": structural_metrics['table_recall'],
            "table_precision": structural_metrics['table_precision'],
            "field_recall": structural_metrics['field_recall'],
            "field_precision": structural_metrics['field_precision'],
            "expected_tables_count": structural_metrics['expected_tables_count'],
            "generated_tables_count": structural_metrics['generated_tables_count'],
            "correct_tables_count": structural_metrics['correct_tables_count'],
            "total_expected_fields": structural_metrics['total_expected_fields'],
            "total_generated_fields": structural_metrics['total_generated_fields'],
            "correct_fields": structural_metrics['correct_fields'],

            "entity_precision": structural_metrics.get('entity_precision', 0),
            "entity_recall": structural_metrics.get('entity_recall', 0),
            "entity_f1": structural_metrics.get('entity_f1', 0),
            "entity_expected_cells": structural_metrics.get('entity_expected_cells', 0),
            "entity_generated_cells": structural_metrics.get('entity_generated_cells', 0),
            "entity_correct_cells": structural_metrics.get('entity_correct_cells', 0),
            "relation_precision": structural_metrics.get('relation_precision', 0),
            "relation_recall": structural_metrics.get('relation_recall', 0),
            "relation_f1": structural_metrics.get('relation_f1', 0),
            "relation_expected_cells": structural_metrics.get('relation_expected_cells', 0),
            "relation_generated_cells": structural_metrics.get('relation_generated_cells', 0),
            "relation_correct_cells": structural_metrics.get('relation_correct_cells', 0),

            "llm_score": llm_evaluation.get('overall', 0),
            "llm_evaluation_details": llm_evaluation.get('evaluation_details', 'N/A'),
            "oracle_fine_grained_metrics": oracle_fine_grained_metrics,
            "timestamp": timestamp_info
        }
        if error_analysis is not None:
            evaluation["error_analysis"] = error_analysis

        if self.eval_mode != "oracle":
            evaluation["llm_raw_response"] = llm_evaluation.get('raw_response', '')
        return evaluation
        

    def compare_cell_values(self, expected: Any, generated: Any) -> dict:
        """精细化比较两个cell值"""
        result = {
            'match': False,
            'match_type': 'none',
            'similarity': 0.0,
            'expected_type': self.get_value_type(expected),
            'generated_type': self.get_value_type(generated),
            'details': {}
        }
        
        if result['expected_type'] == 'null' and result['generated_type'] == 'null':
            result['match'] = True
            result['match_type'] = 'exact'
            result['similarity'] = 1.0
            return result
        
        if result['expected_type'] == 'null' or result['generated_type'] == 'null':
            return result
        
        if result['expected_type'] == 'numeric' or result['generated_type'] == 'numeric':
            exp_val, exp_decimals = self.normalize_numeric_value(expected)
            gen_val, gen_decimals = self.normalize_numeric_value(generated)
            
            if exp_val is not None and gen_val is not None:
                if exp_val == gen_val:
                    result['match'] = True
                    result['match_type'] = 'exact_numeric'
                    result['similarity'] = 1.0
                    
                    if result['expected_type'] != result['generated_type']:
                        result['details']['storage_type_mismatch'] = True
                        result['details']['note'] = 'INTEGER vs VARCHAR - value matches but storage type differs'
                    
                    if exp_decimals == gen_decimals:
                        result['details']['decimal_precision_match'] = True
                    else:
                        result['details']['decimal_precision_match'] = False
                        result['details']['expected_decimals'] = exp_decimals
                        result['details']['generated_decimals'] = gen_decimals
                else:
                    if exp_val != 0:
                        relative_error = abs(exp_val - gen_val) / abs(exp_val)
                        result['similarity'] = max(0, 1 - relative_error)
                    
                result['details']['expected_value'] = exp_val
                result['details']['generated_value'] = gen_val
                return result
        
        
        elif result['expected_type'] == 'string' or result['generated_type'] == 'string':
            exp_str = self.normalize_string_value(expected)
            gen_str = self.normalize_string_value(generated)
            
            similarity = self.calculate_string_similarity(exp_str, gen_str)
            result['similarity'] = similarity
            
            import re
            has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', exp_str)) or bool(re.search(r'[\u4e00-\u9fa5]', gen_str))
            
            similarity_threshold = 0.85 if has_chinese else 0.9
            
            if similarity >= similarity_threshold:
                result['match'] = True
                result['match_type'] = 'string_similar'
                if has_chinese:
                    result['details']['chinese_match'] = True
            elif exp_str.lower() == gen_str.lower():
                result['match'] = True
                result['match_type'] = 'case_insensitive'
                result['similarity'] = 1.0
            elif self._is_descriptive_field_match(exp_str, gen_str):
                result['match'] = True
                result['match_type'] = 'keyword_match'
                result['similarity'] = 0.85
                result['details']['match_method'] = 'keyword_based'
            
            result['details']['expected_string'] = exp_str
            result['details']['generated_string'] = gen_str
        
        
        elif result['expected_type'] == 'date' or result['generated_type'] == 'date':
            exp_str = self.normalize_string_value(expected)
            gen_str = self.normalize_string_value(generated)
            
            import re
            exp_normalized = re.sub(r'[-/\s]+', '-', exp_str)
            gen_normalized = re.sub(r'[-/\s]+', '-', gen_str)
            
            if exp_normalized == gen_normalized:
                result['match'] = True
                result['match_type'] = 'date_normalized'
                result['similarity'] = 1.0
        
        
        elif result['expected_type'] == 'boolean' and result['generated_type'] == 'boolean':
            exp_bool = str(expected).lower() in ['true', 'yes', '是', '1']
            gen_bool = str(generated).lower() in ['true', 'yes', '是', '1']
            
            if exp_bool == gen_bool:
                result['match'] = True
                result['match_type'] = 'boolean'
                result['similarity'] = 1.0
        
        return result
    
    def _find_best_matching_row(self, expected_row: dict, generated_rows: list, used_indices: set) -> tuple:
        """Find best matching row based on content similarity"""
        if not isinstance(expected_row, dict) or not expected_row:
            return None, 0.0
        
        best_idx = None
        best_score = 0.0
        
        for idx, gen_row in enumerate(generated_rows):
            if idx in used_indices or not isinstance(gen_row, dict):
                continue
            
            row_score = self._calculate_row_similarity(expected_row, gen_row)
            
            if row_score > best_score:
                best_score = row_score
                best_idx = idx
        
        if best_score >= 0.3:
            return best_idx, best_score
        else:
            return None, 0.0
    
    def _calculate_row_similarity(self, row1: dict, row2: dict) -> float:
        """Calculate similarity between two rows"""
        if not row1 or not row2:
            return 0.0
        
        fields1 = {k.lower(): k for k in row1.keys()}
        fields2 = {k.lower(): k for k in row2.keys()}
        common_fields = set(fields1.keys()) & set(fields2.keys())
        
        if not common_fields:
            return 0.0
        
        field_scores = []
        for field_lower in common_fields:
            field1 = fields1[field_lower]
            field2 = fields2[field_lower]
            
            val1 = row1[field1]
            val2 = row2[field2]
            
            comparison = self.compare_cell_values(val1, val2)
            
            if comparison['match']:
                field_scores.append(1.0)
            else:
                field_scores.append(comparison['similarity'])
        
        return sum(field_scores) / len(field_scores) if field_scores else 0.0
    
    def _normalize_schema_for_metrics(self, schema: Any) -> Dict[str, Any]:
        """将多种顶层 schema 结构统一为含 ``tables`` 列表的字典（与 llm_evaluate_cascade.normalize_schema 一致）。

        根为 JSON **数组** ``[{ "name", "fields"|"attributes", ... }, ...]`` 时，规范为 ``{"tables": [...]}``，
        否则 ``get_table_types_from_schema`` / 主键提取无法识别，关系表会被当成 entity，relation 指标全为 0。
        """
        if isinstance(schema, list):
            tables = [t for t in schema if isinstance(t, dict) and t.get("name")]
            return {"tables": tables}
        if isinstance(schema, dict):
            return schema
        return {"tables": []}
    
    def _get_primary_key_fields_from_schema(self, schema: Dict[str, Any]) -> Dict[str, set]:
        """从schema中提取每个实体表的主键字段
        
        Args:
            schema: schema字典
            
        Returns:
            {table_name_lower: set(primary_key_field_names)} 的映射
        """
        primary_keys_by_table = {}
        
        try:
            schema = self._normalize_schema_for_metrics(schema)
            if 'tables' in schema and isinstance(schema['tables'], list):
                for table in schema['tables']:
                    if not isinstance(table, dict):
                        continue
                    
                    table_name = table.get('name', '')
                    if not table_name:
                        continue
                    
                    table_type = table.get('type', 'entity').lower()
                    # 只处理实体表主键，关系表不在这里做跳过规则
                    if table_type != 'entity':
                        continue
                    
                    table_name_lower = table_name.lower()
                    primary_keys = set()
                    
                    # 获取字段列表（兼容fields和attributes）
                    fields = table.get('attributes', []) or table.get('fields', [])
                    
                    for field in fields:
                        if not isinstance(field, dict):
                            continue
                        
                        field_name = field.get('name', '')
                        if not field_name:
                            continue
                        
                        # 检查是否是主键
                        constraints = field.get('constraints', {})
                        if isinstance(constraints, dict) and constraints.get('primary_key', False):
                            primary_keys.add(field_name.lower())
                    
                    if primary_keys:
                        primary_keys_by_table[table_name_lower] = primary_keys
                        logger.info(f"   实体表 {table_name} 的主键字段: {', '.join(sorted(primary_keys))}")
            
        except Exception as e:
            logger.warning(f"  ️  从schema提取主键字段失败: {e}")
        
        return primary_keys_by_table
    
    def _is_auto_generated_id_field(self, field_name: str, table_name: str = None, 
                                     primary_keys_by_table: Dict[str, set] = None) -> bool:
        """Check if field is a primary key ID field in an entity table
        
        Args:
            field_name: 字段名
            table_name: 表名（可选）
            primary_keys_by_table: 从schema提取的主键字段映射（可选）
        """
        field_lower = field_name.lower()
        
        # 如果提供了schema信息，使用schema中的主键信息
        if primary_keys_by_table and table_name:
            table_name_lower = table_name.lower()
            if table_name_lower in primary_keys_by_table:
                if field_lower in primary_keys_by_table[table_name_lower]:
                    return True
        return False

    def _get_entity_pk_allowed_from_meta(self, case_info: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
        """从 case/meta.json 读取 if_primary_allowed，用于决定实体表主键是否参与评估。"""
        if not case_info:
            return {}

        case_dir = case_info.get("case_dir")
        if not case_dir:
            return {}

        meta_path = Path(case_dir) / "meta.json"
        if not meta_path.exists():
            return {}

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as exc:
            logger.warning(f"  ️  读取 meta.json 失败，实体表主键按默认规则跳过: {exc}")
            return {}

        allowed = meta.get("if_primary_allowed", {})
        if not isinstance(allowed, dict):
            return {}

        return {
            str(table_name).lower(): bool(is_allowed)
            for table_name, is_allowed in allowed.items()
            if isinstance(table_name, str)
        }

    def _should_skip_entity_pk_field(
        self,
        field_name: str,
        table_name: str,
        primary_keys_by_table: Dict[str, set],
        entity_pk_allowed_by_table: Dict[str, bool],
    ) -> bool:
        """实体表主键是否跳过评估。

        默认跳过实体表主键；优先依据 meta.json 的 if_primary_allowed[table]：
        - true: 该实体表主键参与评估
        - false: 该实体表主键不参与评估
        若 meta.json 没有该表配置，才使用 --evaluate-entity-pk 作为兜底。
        """
        if not self._is_auto_generated_id_field(field_name, table_name, primary_keys_by_table):
            return False

        table_key = str(table_name).lower()
        if table_key in entity_pk_allowed_by_table:
            return not entity_pk_allowed_by_table[table_key]

        if self.evaluate_entity_pk:
            return False
        return True
        
    def analyze_detailed_errors(self, cell_metrics: Dict[str, Any], expected_data: Dict[str, Any]) -> Dict[str, Any]:
        error_analysis = {
            "summary": {
                "missing_attributes": 0,      # 期望有值，但生成为 None
                "value_hallucinations": 0,     # 期望为空，但生成了值
                "content_mismatches": 0,       # 两边都有值但不匹配
                "total_failed_cells": 0
            },
            "table_breakdown": {}
        }

        # 从 cell_metrics 获取详细比较列表
        detailed_comps = cell_metrics.get('detailed_comparisons', [])
        
        for comp in detailed_comps:
            # 跳过匹配正确的条目
            if comp.get('match') is True:
                continue

            # 1. 提取表名和行索引
            t_name = comp.get('table', 'unknown_table')
            row_idx = comp.get('row_index')
            
            # 2. 核心改进：无论 generated 是否为 None，都强制回溯 expected_data 提取背景
            row_context = "Context Missing"
            if isinstance(expected_data, dict) and t_name in expected_data:
                table_rows = expected_data[t_name]
                if row_idx is not None and row_idx < len(table_rows):
                    actual_row = table_rows[row_idx]
                    if isinstance(actual_row, dict):
                        # 选取前 3 个属性作为指纹，帮助你定位是哪一行丢了
                        row_context = {k: v for k, v in list(actual_row.items())[:3]}
                    else:
                        row_context = str(actual_row)[:50]

            # 3. 构造错误明细
            exp_val = comp.get('expected_value')
            gen_val = comp.get('generated_value')
            
            error_item = {
                "row_index": row_idx,
                "row_context": row_context,
                "field": comp.get('field'),
                "expected": exp_val,
                "generated": gen_val,
                "error_type": "unknown"
            }

            # 4. 判定错误分类 (针对你提供的 None 数据优化)
            if (exp_val is not None and exp_val != "") and (gen_val is None or gen_val == ""):
                error_item["error_type"] = "missing_attribute"
                error_analysis["summary"]["missing_attributes"] += 1
            elif (exp_val is None or exp_val == "") and (gen_val is not None and gen_val != ""):
                error_item["error_type"] = "hallucination"
                error_analysis["summary"]["value_hallucinations"] += 1
            else:
                error_item["error_type"] = "content_mismatch"
                error_analysis["summary"]["content_mismatches"] += 1

            error_analysis["summary"]["total_failed_cells"] += 1

            # 5. 填充 table_breakdown
            if t_name not in error_analysis["table_breakdown"]:
                error_analysis["table_breakdown"][t_name] = []
            
            error_analysis["table_breakdown"][t_name].append(error_item)

        return error_analysis
    def calculate_cell_metrics(self, generated: Dict[str, Any], expected: Dict[str, Any], 
                               schema: Dict[str, Any] = None,
                               case_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Calculate cell-level recall and precision metrics
        
        Args:
            generated: 生成的结果
            expected: 期望的结果
            schema: schema字典（可选，用于识别实体表的主键字段）
        """
        try:
            if schema is not None:
                schema = self._normalize_schema_for_metrics(schema)
            comparison_results = []
            table_stats = {}
            skipped_id_fields = set()
            
            # 从schema中提取实体表的主键字段
            primary_keys_by_table = {}
            if schema:
                primary_keys_by_table = self._get_primary_key_fields_from_schema(schema)
            entity_pk_allowed_by_table = self._get_entity_pk_allowed_from_meta(case_info)
            
            for table_name, expected_rows in expected.items():
                if not isinstance(expected_rows, list):
                    continue
                
                table_name_lower = table_name.lower()
                table_stats[table_name_lower] = {
                    'expected_cells': 0,
                    'generated_cells': 0,
                    'correct_cells': 0,
                    'exact_matches': 0,
                    'numeric_precision_errors': 0,
                    'string_similarity_matches': 0,
                    'type_mismatches': 0,
                    'expected_rows': len(expected_rows),
                    'generated_rows': 0,
                    'detailed_comparisons': []
                }
                
                generated_rows = None
                matched_gen_table_name = None
                for gen_table_name, gen_rows in generated.items():
                    gen_name_lower = gen_table_name.lower()
                    if gen_name_lower == table_name_lower:
                        generated_rows = gen_rows
                        matched_gen_table_name = gen_table_name
                        break
                    if gen_name_lower.rstrip('s') == table_name_lower.rstrip('s'):
                        generated_rows = gen_rows
                        matched_gen_table_name = gen_table_name
                        logger.info(f"  ️  Table name plural mismatch: expected '{table_name}' vs generated '{gen_table_name}'")
                        break
                
                if generated_rows is None:
                    logger.warning(f"   No matching generated table found: '{table_name}' (available: {list(generated.keys())})")
                    # 判断表类型
                    table_type = 'entity'
                    if schema and 'tables' in schema:
                        for t in schema.get('tables', []):
                            if isinstance(t, dict) and t.get('name', '').lower() == table_name_lower:
                                table_type = t.get('type', 'entity').lower()
                                break
                    
                    for row_idx, row in enumerate(expected_rows):
                        if isinstance(row, dict):
                            for attr, value in row.items():
                                # 跳过实体表的主键字段
                                if table_type == 'entity' and self._should_skip_entity_pk_field(
                                    attr,
                                    table_name,
                                    primary_keys_by_table,
                                    entity_pk_allowed_by_table
                                ):
                                    skipped_id_fields.add(f"{table_name}.{attr}")
                                    continue
                                if value is not None and value != "":
                                    table_stats[table_name_lower]['expected_cells'] += 1
                    continue
                
                table_stats[table_name_lower]['generated_rows'] = len(generated_rows)
                
                # 判断表类型（在循环外判断一次即可）
                table_type = 'entity'  # 默认
                if primary_keys_by_table:
                    # 如果表在primary_keys_by_table中，说明是实体表
                    if table_name_lower in primary_keys_by_table:
                        table_type = 'entity'
                    else:
                        # 检查schema中的表类型
                        if schema and 'tables' in schema:
                            for t in schema.get('tables', []):
                                if isinstance(t, dict) and t.get('name', '').lower() == table_name_lower:
                                    table_type = t.get('type', 'entity').lower()
                                    break
                
                # =================================================================
                # 🚀 改进的行匹配算法：全局最高分优先分配 (Global Maximum Assignment)
                # 解决因漏抽导致的“贪心抢占”与“级联错位”问题
                # =================================================================
                
                # 1. 计算所有可能的 (Expected_Row, Generated_Row) 的得分矩阵
                match_candidates = []
                for exp_idx, expected_row in enumerate(expected_rows):
                    if not isinstance(expected_row, dict):
                        continue
                    for gen_idx, gen_row in enumerate(generated_rows):
                        if not isinstance(gen_row, dict):
                            continue
                        
                        # 计算这对组合的相似度
                        score = self._calculate_row_similarity(expected_row, gen_row)
                        if score >= 0.3:  # 只保留及格的候选者
                            match_candidates.append((score, exp_idx, gen_idx))
                
                # 2. 按得分从高到低排序 (优先成全得分最高的“天作之合”)
                match_candidates.sort(key=lambda x: x[0], reverse=True)
                
                # 3. 执行分配
                used_expected_indices = set()
                used_generated_indices = set()
                final_matches = {}  # 记录 exp_idx -> gen_idx
                
                for score, exp_idx, gen_idx in match_candidates:
                    if exp_idx not in used_expected_indices and gen_idx not in used_generated_indices:
                        final_matches[exp_idx] = gen_idx
                        used_expected_indices.add(exp_idx)
                        used_generated_indices.add(gen_idx)

                # =================================================================
                # 4. 根据分配结果，执行后续的 Cell 级精细比对
                # =================================================================                
                for exp_row_idx, expected_row in enumerate(expected_rows):
                    if not isinstance(expected_row, dict):
                        continue
                        
                    # 从刚才的全局最优分配表中获取对应的生成行
                    matched_gen_idx = final_matches.get(exp_row_idx)
                    if matched_gen_idx is not None:
                        generated_row = generated_rows[matched_gen_idx]
                    else:
                        generated_row = {}  # 没有分配到，注定触发 missing_attribute
                    
                    all_fields = set()
                    if isinstance(expected_row, dict):
                        all_fields.update(expected_row.keys())
                    if isinstance(generated_row, dict):
                        all_fields.update(generated_row.keys())
                    
                    for field in all_fields:
                        field_lower = field.lower()
                        
                        # 检查是否是实体表的主键ID字段（只跳过实体表的主键，关系表的主键不跳过）
                        if table_type == 'entity' and self._should_skip_entity_pk_field(
                            field,
                            table_name,
                            primary_keys_by_table,
                            entity_pk_allowed_by_table
                        ):
                            skipped_id_fields.add(f"{table_name}.{field}")
                            continue
                        
                        expected_val = expected_row.get(field) if isinstance(expected_row, dict) else None
                        
                        generated_val = None
                        if isinstance(generated_row, dict):
                            for gen_field, gen_val in generated_row.items():
                                if gen_field.lower() == field_lower:
                                    generated_val = gen_val
                                    break
                        
                        if field in expected_row:
                            table_stats[table_name_lower]['expected_cells'] += 1
                        
                        if generated_row and field_lower in [f.lower() for f in generated_row.keys()]:
                            table_stats[table_name_lower]['generated_cells'] += 1
                        
                        if (expected_val is not None and expected_val != "") or (generated_val is not None and generated_val != ""):
                            comparison = self.compare_cell_values(expected_val, generated_val)
                            comparison['table'] = table_name_lower
                            comparison['row_index'] = exp_row_idx
                            comparison['field'] = field_lower
                            comparison['expected_value'] = expected_val
                            comparison['generated_value'] = generated_val
                            
                            comparison_results.append(comparison)
                            table_stats[table_name_lower]['detailed_comparisons'].append(comparison)
                            
                            if comparison['match']:
                                table_stats[table_name_lower]['correct_cells'] += 1
                                
                                if comparison['match_type'] == 'exact_numeric':
                                    table_stats[table_name_lower]['exact_matches'] += 1
                                    if not comparison['details'].get('decimal_precision_match', True):
                                        table_stats[table_name_lower]['numeric_precision_errors'] += 1
                                elif comparison['match_type'] in ['string_similar', 'case_insensitive', 'keyword_match']:
                                    table_stats[table_name_lower]['string_similarity_matches'] += 1
                                else:
                                    table_stats[table_name_lower]['exact_matches'] += 1
                            else:
                                if comparison['expected_type'] != comparison['generated_type']:
                                    is_storage_mismatch = comparison['details'].get('storage_type_mismatch', False)
                                    if not is_storage_mismatch:
                                        table_stats[table_name_lower]['type_mismatches'] += 1
                        elif (expected_val is None or expected_val == "") and (generated_val is None or generated_val == ""):
                            field_in_expected = field in expected_row
                            field_in_generated = generated_row and field_lower in [f.lower() for f in generated_row.keys()]
                            if field_in_expected and field_in_generated:
                                table_stats[table_name_lower]['correct_cells'] += 1
                
                for gen_idx, gen_row in enumerate(generated_rows):
                    if gen_idx not in used_generated_indices and isinstance(gen_row, dict):
                        for field in gen_row.keys():
                            # 跳过实体表的主键字段
                            if table_type == 'entity' and self._should_skip_entity_pk_field(
                                field,
                                table_name,
                                primary_keys_by_table,
                                entity_pk_allowed_by_table
                            ):
                                skipped_id_fields.add(f"{table_name}.{field}")
                                continue
                            table_stats[table_name_lower]['generated_cells'] += 1
            
            total_expected = sum(stats['expected_cells'] for stats in table_stats.values())
            total_generated = sum(stats['generated_cells'] for stats in table_stats.values())
            total_correct = sum(stats['correct_cells'] for stats in table_stats.values())
            total_exact = sum(stats['exact_matches'] for stats in table_stats.values())
            total_precision_errors = sum(stats['numeric_precision_errors'] for stats in table_stats.values())
            total_similarity_matches = sum(stats['string_similarity_matches'] for stats in table_stats.values())
            total_type_mismatches = sum(stats['type_mismatches'] for stats in table_stats.values())
            
            recall = (total_correct / total_expected * 100) if total_expected > 0 else 0
            precision = (total_correct / total_generated * 100) if total_generated > 0 else 0
            f1_score = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0
            
            if skipped_id_fields:
                logger.info(f"  ️  跳过 {len(skipped_id_fields)} 个实体表主键ID字段: {', '.join(sorted(skipped_id_fields))}")
            
            table_metrics = {}
            for table_name, stats in table_stats.items():
                table_recall = (stats['correct_cells'] / stats['expected_cells'] * 100) if stats['expected_cells'] > 0 else 0
                table_precision = (stats['correct_cells'] / stats['generated_cells'] * 100) if stats['generated_cells'] > 0 else 0
                table_f1 = (2 * table_precision * table_recall / (table_precision + table_recall)) if (table_precision + table_recall) > 0 else 0
                
                table_metrics[table_name] = {
                    'recall': table_recall,
                    'precision': table_precision,
                    'f1': table_f1,
                    'expected_cells': stats['expected_cells'],
                    'generated_cells': stats['generated_cells'],
                    'correct_cells': stats['correct_cells'],
                    'exact_matches': stats['exact_matches'],
                    'numeric_precision_errors': stats['numeric_precision_errors'],
                    'string_similarity_matches': stats['string_similarity_matches'],
                    'type_mismatches': stats['type_mismatches'],
                    'expected_rows': stats['expected_rows'],
                    'generated_rows': stats['generated_rows']
                }
            
            return {
                "cell_recall": recall,
                "cell_precision": precision,
                "cell_f1": f1_score,
                "total_expected_cells": total_expected,
                "total_generated_cells": total_generated,
                "total_correct_cells": total_correct,
                "total_exact_matches": total_exact,
                "total_numeric_precision_errors": total_precision_errors,
                "total_string_similarity_matches": total_similarity_matches,
                "total_type_mismatches": total_type_mismatches,
                "table_metrics": table_metrics,
                "detailed_comparisons": comparison_results,
                "total_expected_tables": len([t for t in table_stats.values() if t['expected_cells'] > 0]),
                "total_generated_tables": len([t for t in table_stats.values() if t['generated_cells'] > 0])
            }
            
        except Exception as e:
            logger.error(f"   计算cell指标失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "cell_recall": 0.0,
                "cell_precision": 0.0,
                "cell_f1": 0.0,
                "total_expected_cells": 0,
                "total_generated_cells": 0,
                "total_correct_cells": 0,
                "total_exact_matches": 0,
                "total_numeric_precision_errors": 0,
                "total_string_similarity_matches": 0,
                "total_type_mismatches": 0,
                "table_metrics": {},
                "detailed_comparisons": [],
                "total_expected_tables": 0,
                "total_generated_tables": 0
            }
    
    def get_table_types_from_schema(self, schema: Dict[str, Any]) -> Dict[str, str]:
        """从schema中提取每个表的类型（entity或relation）
        
        Args:
            schema: schema字典
            
        Returns:
            {table_name: table_type} 的映射，table_type为'entity'或'relation'
        """
        table_types = {}
        
        try:
            schema = self._normalize_schema_for_metrics(schema)
            if 'tables' in schema and isinstance(schema['tables'], list):
                for table in schema['tables']:
                    if isinstance(table, dict):
                        table_name = table.get('name', '')
                        table_type = table.get('type', 'entity')  # 默认为entity
                        if table_name:
                            table_types[table_name.lower()] = table_type
                
                if table_types:
                    entity_count = sum(1 for t in table_types.values() if t == 'entity')
                    relation_count = sum(1 for t in table_types.values() if t == 'relation')
                    logger.info(f"   Schema中的表类型: Entity={entity_count}, Relation={relation_count}")
                    for tname, ttype in table_types.items():
                        logger.info(f"     - {tname}: {ttype}")
        except Exception as e:
            logger.warning(f"  ️  解析schema表类型失败: {e}")
        
        return table_types
    
    def calculate_structural_metrics(self, generated: Dict[str, Any], expected: Dict[str, Any],
                                     schema: Dict[str, Any] = None,
                                     case_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Calculate structural-level metrics (table structure, field coverage, etc.)"""
        try:
            expected_tables = set(expected.keys())
            generated_tables = set(generated.keys())
            
            table_recall = len(expected_tables & generated_tables) / len(expected_tables) * 100 if expected_tables else 0
            table_precision = len(expected_tables & generated_tables) / len(generated_tables) * 100 if generated_tables else 0
            
            field_stats = {
                'total_expected_fields': 0,
                'total_generated_fields': 0,
                'correct_fields': 0
            }
            
            for table_name in expected_tables:
                if table_name in expected and isinstance(expected[table_name], list) and expected[table_name]:
                    expected_fields = set(expected[table_name][0].keys()) if expected[table_name] else set()
                    field_stats['total_expected_fields'] += len(expected_fields)
                    
                    if table_name in generated and isinstance(generated[table_name], list) and generated[table_name]:
                        generated_fields = set(generated[table_name][0].keys()) if generated[table_name] else set()
                        field_stats['total_generated_fields'] += len(generated_fields)
                        field_stats['correct_fields'] += len(expected_fields & generated_fields)
            
            field_recall = field_stats['correct_fields'] / field_stats['total_expected_fields'] * 100 if field_stats['total_expected_fields'] > 0 else 0
            field_precision = field_stats['correct_fields'] / field_stats['total_generated_fields'] * 100 if field_stats['total_generated_fields'] > 0 else 0
            
            entity_relation_stats = self.calculate_entity_relation_metrics(
                generated,
                expected,
                schema,
                case_info=case_info
            )
            
            return {
                'table_recall': table_recall,
                'table_precision': table_precision,
                'field_recall': field_recall,
                'field_precision': field_precision,
                'expected_tables_count': len(expected_tables),
                'generated_tables_count': len(generated_tables),
                'correct_tables_count': len(expected_tables & generated_tables),
                **field_stats,
                **entity_relation_stats
            }
            
        except Exception as e:
            logger.error(f"   计算结构指标失败: {e}")
            return {
                'table_recall': 0.0,
                'table_precision': 0.0,
                'field_recall': 0.0,
                'field_precision': 0.0,
                'expected_tables_count': 0,
                'generated_tables_count': 0,
                'correct_tables_count': 0,
                'total_expected_fields': 0,
                'total_generated_fields': 0,
                'correct_fields': 0,
                'entity_precision': 0.0,
                'entity_recall': 0.0,
                'entity_f1': 0.0,
                'relation_precision': 0.0,
                'relation_recall': 0.0,
                'relation_f1': 0.0
            }
    
    def calculate_entity_relation_metrics(self, generated: Dict[str, Any], expected: Dict[str, Any],
                                          schema: Dict[str, Any] = None,
                                          case_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Calculate cell-level P/R/F1 by table type (Entity/Relation) using Global Matching"""
        try:
            table_types = {}
            primary_keys_by_table = {}
            if schema:
                table_types = self.get_table_types_from_schema(schema)
                # 提取实体表的主键字段
                primary_keys_by_table = self._get_primary_key_fields_from_schema(schema)
            entity_pk_allowed_by_table = self._get_entity_pk_allowed_from_meta(case_info)
            
            entity_stats = {'expected_cells': 0, 'generated_cells': 0, 'correct_cells': 0}
            relation_stats = {'expected_cells': 0, 'generated_cells': 0, 'correct_cells': 0}
            skipped_id_fields = set()
            
            for table_name, expected_rows in expected.items():
                if not isinstance(expected_rows, list):
                    continue
                
                table_name_lower = table_name.lower()
                table_type = table_types.get(table_name_lower, 'entity')
                stats = entity_stats if table_type == 'entity' else relation_stats
                
                generated_rows = None
                for gen_table_name, gen_rows in generated.items():
                    gen_name_lower = gen_table_name.lower()
                    if gen_name_lower == table_name_lower or gen_name_lower.rstrip('s') == table_name_lower.rstrip('s'):
                        generated_rows = gen_rows
                        break
                
                if generated_rows is None:
                    for row in expected_rows:
                        if isinstance(row, dict):
                            for attr, value in row.items():
                                if table_type == 'entity' and self._should_skip_entity_pk_field(
                                    attr,
                                    table_name,
                                    primary_keys_by_table,
                                    entity_pk_allowed_by_table
                                ):
                                    skipped_id_fields.add(f"{table_name}.{attr}")
                                    continue
                                if value is not None and value != "":
                                    stats['expected_cells'] += 1
                    continue
                
                # =================================================================
                # 🚀 替换为：全局最高分优先分配 (Global Maximum Assignment)
                # =================================================================
                match_candidates = []
                for exp_idx, expected_row in enumerate(expected_rows):
                    if not isinstance(expected_row, dict):
                        continue
                    for gen_idx, gen_row in enumerate(generated_rows):
                        if not isinstance(gen_row, dict):
                            continue
                        score = self._calculate_row_similarity(expected_row, gen_row)
                        if score >= 0.3:
                            match_candidates.append((score, exp_idx, gen_idx))
                
                match_candidates.sort(key=lambda x: x[0], reverse=True)
                
                used_expected_indices = set()
                used_generated_indices = set()
                final_matches = {}
                
                for score, exp_idx, gen_idx in match_candidates:
                    if exp_idx not in used_expected_indices and gen_idx not in used_generated_indices:
                        final_matches[exp_idx] = gen_idx
                        used_expected_indices.add(exp_idx)
                        used_generated_indices.add(gen_idx)
                        
                # =================================================================
                # 执行 Cell 级清算
                # =================================================================
                for exp_row_idx, exp_row in enumerate(expected_rows):
                    if not isinstance(exp_row, dict):
                        continue
                    
                    # 从全局匹配字典中取值
                    matched_gen_idx = final_matches.get(exp_row_idx)
                    if matched_gen_idx is not None:
                        generated_row = generated_rows[matched_gen_idx]
                    else:
                        generated_row = {}
                    
                    all_fields = set()
                    if isinstance(exp_row, dict):
                        all_fields.update(exp_row.keys())
                    if isinstance(generated_row, dict):
                        all_fields.update(generated_row.keys())
                    
                    for field in all_fields:
                        field_lower = field.lower()
                        
                        # 检查是否是实体表的主键ID字段
                        if table_type == 'entity' and self._should_skip_entity_pk_field(
                            field,
                            table_name,
                            primary_keys_by_table,
                            entity_pk_allowed_by_table
                        ):
                            skipped_id_fields.add(f"{table_name}.{field}")
                            continue
                        
                        expected_val = exp_row.get(field) if isinstance(exp_row, dict) else None
                        generated_val = None
                        if isinstance(generated_row, dict):
                            for gen_field, gen_val in generated_row.items():
                                if gen_field.lower() == field_lower:
                                    generated_val = gen_val
                                    break
                        
                        if field in exp_row:
                            stats['expected_cells'] += 1
                        if generated_row and field_lower in [f.lower() for f in generated_row.keys()]:
                            stats['generated_cells'] += 1
                        
                        if (expected_val is not None and expected_val != "") or (generated_val is not None and generated_val != ""):
                            comparison = self.compare_cell_values(expected_val, generated_val)
                            if comparison['match']:
                                stats['correct_cells'] += 1
                        elif (expected_val is None or expected_val == "") and (generated_val is None or generated_val == ""):
                            field_in_expected = field in exp_row
                            field_in_generated = generated_row and field_lower in [f.lower() for f in generated_row.keys()]
                            if field_in_expected and field_in_generated:
                                stats['correct_cells'] += 1
                
                # 统计幻觉数据
                for gen_idx, gen_row in enumerate(generated_rows):
                    if gen_idx not in used_generated_indices and isinstance(gen_row, dict):
                        for field in gen_row.keys():
                            if table_type == 'entity' and self._should_skip_entity_pk_field(
                                field,
                                table_name,
                                primary_keys_by_table,
                                entity_pk_allowed_by_table
                            ):
                                skipped_id_fields.add(f"{table_name}.{field}")
                                continue
                            stats['generated_cells'] += 1
            
            # 计算最终分数
            entity_recall = (entity_stats['correct_cells'] / entity_stats['expected_cells'] * 100) if entity_stats['expected_cells'] > 0 else 0
            entity_precision = (entity_stats['correct_cells'] / entity_stats['generated_cells'] * 100) if entity_stats['generated_cells'] > 0 else 0
            entity_f1 = (2 * entity_precision * entity_recall / (entity_precision + entity_recall)) if (entity_precision + entity_recall) > 0 else 0
            
            relation_recall = (relation_stats['correct_cells'] / relation_stats['expected_cells'] * 100) if relation_stats['expected_cells'] > 0 else 0
            relation_precision = (relation_stats['correct_cells'] / relation_stats['generated_cells'] * 100) if relation_stats['generated_cells'] > 0 else 0
            relation_f1 = (2 * relation_precision * relation_recall / (relation_precision + relation_recall)) if (relation_precision + relation_recall) > 0 else 0
            
            logger.info(f"   Entity统计: expected={entity_stats['expected_cells']}, generated={entity_stats['generated_cells']}, correct={entity_stats['correct_cells']}")
            logger.info(f"   Relation统计: expected={relation_stats['expected_cells']}, generated={relation_stats['generated_cells']}, correct={relation_stats['correct_cells']}")
            
            return {
                'entity_precision': entity_precision,
                'entity_recall': entity_recall,
                'entity_f1': entity_f1,
                'entity_expected_cells': entity_stats['expected_cells'],
                'entity_generated_cells': entity_stats['generated_cells'],
                'entity_correct_cells': entity_stats['correct_cells'],
                'relation_precision': relation_precision,
                'relation_recall': relation_recall,
                'relation_f1': relation_f1,
                'relation_expected_cells': relation_stats['expected_cells'],
                'relation_generated_cells': relation_stats['generated_cells'],
                'relation_correct_cells': relation_stats['correct_cells']
            }
            
        except Exception as e:
            logger.error(f"   计算Entity/Relation指标失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                'entity_precision': 0.0, 'entity_recall': 0.0, 'entity_f1': 0.0,
                'entity_expected_cells': 0, 'entity_generated_cells': 0, 'entity_correct_cells': 0,
                'relation_precision': 0.0, 'relation_recall': 0.0, 'relation_f1': 0.0,
                'relation_expected_cells': 0, 'relation_generated_cells': 0, 'relation_correct_cells': 0
            }
    
    def evaluate_with_llm(self, generated: Dict[str, Any], expected: Dict[str, Any], 
                          case_info: Dict[str, Any]) -> Dict[str, Any]:
        """使用LLM进行精细化评估（只返回总分）"""
        try:
            schema = {}
            try:
                with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                    schema = json.load(f)
            except Exception as e:
                logger.warning(f"  ️  无法读取schema文件: {e}")
            
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


Please evaluate the quality of the generated results from the following dimensions:

- Whether the generated results are semantically consistent with the standard answer
- Whether numeric data is accurate (values must be exactly equal to be considered correct)
- Whether the decimal places of numeric values match the standard answer
- Whether string data is accurate (similarity needs to reach 90% or above)
- Whether there are hallucinations or erroneous information

- Whether the generated results contain all key information from the standard answer
- Whether there are important data omissions
- Whether the table structure and fields are complete
- Further explanations of these key points can be omitted, but the core content must be complete

- Whether the data format complies with the Schema definition
- Whether the data types are correct (numeric, string, date, etc.)
- Whether the formats are consistent (e.g., date format, number format, etc.)

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
            
            logger.info(f"   使用 gpt-4o 模型进行精细化评估")
            
            completion = client.chat.completions.create(
                model=DEFAULT_MODEL,
                temperature=0.0,
                messages=[
                    {"role": "user", "content": evaluation_prompt}
                ]
            )
            response = completion.choices[0].message.content

            response = _clean_response(response)
            
            if not response:
                raise Exception("LLM返回空响应")
            
            import re
            overall_score = 0
            evaluation_details = ""
            
            score_match = re.search(r'Rating:\s*\[\[(\d+(?:\.\d+)?)\]\]', response)
            if score_match:
                overall_score = float(score_match.group(1))
            else:
                score_match = re.search(r'"overall_score":\s*(\d+(?:\.\d+)?)', response)
                if score_match:
                    overall_score = float(score_match.group(1))
                else:
                    score_match = re.search(r'(\d+(?:\.\d+)?)\s*分', response)
                    if score_match:
                        overall_score = float(score_match.group(1))
            
            evidence_match = re.search(r'Evaluation evidence:\s*([^\n]+)', response)
            if evidence_match:
                evaluation_details = evidence_match.group(1).strip()
            else:
                evaluation_details = response[:500]  # 限制长度
            
            overall_score = max(0, min(100, overall_score))
            
            llm_evaluation = {
                "overall": overall_score,
                "evaluation_details": evaluation_details,
                "raw_response": response
            }
            
            logger.info(f"   LLM 精细化评估完成，总分: {overall_score:.1f}")
            return llm_evaluation
                
        except Exception as e:
            logger.warning(f"  ️  LLM评估失败，已跳过: {e}")
            return {
                "overall": 0.0,
                "evaluation_details": f"LLM评估已跳过: {str(e)}",
                "raw_response": ""
            }
    def check_case_already_evaluated(self, case_info: Dict[str, Any]) -> tuple[bool, Optional[Path], Optional[str]]:
        """检查案例是否已经评估过 (严格根据当前 model 判断，并且检测 llm_score)"""
        case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
        
        if not case_dir.exists():
            return False, None, None
            
        # ================== 【新增】验证评估是否有效的内部函数 ==================
        def _is_valid_evaluation(eval_file: Path) -> bool:
            if not eval_file.exists():
                return False
            try:
                with open(eval_file, 'r', encoding='utf-8') as f:
                    eval_data = json.load(f)
                # 核心逻辑：如果启用了 LLM 评分，非 oracle 模式下得分为 0，则判定为无效
                if self.enable_llm_eval and self.eval_mode != "oracle" and eval_data.get('llm_score', 0) == 0:
                    logger.info(f"   ⚠️ 发现已有评估文件 {eval_file.name}，但 llm_score 为 0，将强制重新评估")
                    return False
                return True
            except Exception:
                return False
        # ========================================================================
        
        # 1. 首先尝试匹配标准格式的文件夹
        filename_prefix = self._build_filename_prefix(
            case_info['db_name'], 
            case_info['case_name'], 
            self.model
        )
        prefix_output_dir = case_dir / filename_prefix
        
        if prefix_output_dir.exists() and prefix_output_dir.is_dir():
            evaluation_file = prefix_output_dir / self._get_evaluation_filename(filename_prefix)
            # 【修改】使用 _is_valid_evaluation 替代原来的 .exists()
            if _is_valid_evaluation(evaluation_file):
                logger.info(f"   找到已有评估文件: {filename_prefix}/{evaluation_file.name}")
                return True, prefix_output_dir, filename_prefix
        
        # 2. 如果标准格式不存在，通过 metadata.json 精确查找
        for subdir in case_dir.iterdir():
            if not subdir.is_dir():
                continue
            
            metadata_file = subdir / "metadata.json"
            if metadata_file.exists():
                try:
                    with open(metadata_file, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                    if metadata.get('model') == self.model: # 精确匹配
                        evaluation_file = subdir / self._get_evaluation_filename(subdir.name)
                        # 【修改】使用 _is_valid_evaluation 替代原来的 .exists()
                        if _is_valid_evaluation(evaluation_file):
                            logger.info(f"   找到已有评估文件: {subdir.name}/{evaluation_file.name}")
                            return True, subdir, subdir.name
                except Exception:
                    continue
        
        return False, None, None
    
    def process_case(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个测试案例"""
        case_id = f"{case_info['db_name']}/{case_info['case_name']}"
        logger.info(f"\n{'='*80}")
        logger.info(f" 开始处理: {case_id}")
        logger.info(f"{'='*80}")
        
        result = {
            'case_id': case_id,
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'model': self.model,
            'run_name': self.run_name,
            'document_mode': self.document_mode,  # 记录文档模式
            'status': 'failed',
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'duration': 0,
            'evaluation': None,
            'error': None
        }
        
        start_time = time.time()
        
        try:
            logger.info("读取schema...")
            with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                schema = json.load(f)
            
            logger.info("读取标准答案...")
            expected_answer = self.load_expected_answer(case_info)
            
            logger.info(f" 上传 {len(case_info['doc_files'])} 个文档...")
            uploaded_files = self.upload_documents(case_info['doc_files'])
            
            if not uploaded_files:
                raise Exception("文档上传失败")
            
            logger.info("启动处理任务...")
            task_id = self.start_processing(uploaded_files, schema)
            
            if not task_id:
                raise Exception("启动处理任务失败")
            
            result['task_id'] = task_id
            
            logger.info("等待处理完成...")
            status_data = self.wait_for_completion(task_id)
            
            if not status_data:
                raise Exception("处理任务失败或超时")
            
            logger.info("提取处理结果...")
            result_data = self.extract_result_data(status_data, task_id)
            
            if not result_data:
                raise Exception("提取结果失败")
            
            if result_data.get('steps'):
                logger.info(f"  ️  处理步骤数: {len(result_data['steps'])}")
                for step in result_data['steps']:
                    step_name = step.get('step', 'unknown')
                    step_status = step.get('status', 'unknown')
                    logger.info(f"    - {step_name}: {step_status}")
            
            logger.info("保存处理结果...")
            prefix_output_dir, filename_prefix = self.save_output(case_info, result_data)
            result['output_dir'] = str(prefix_output_dir)
            result['filename_prefix'] = filename_prefix
            
            # 检查是否已经评估过
            evaluation_file = prefix_output_dir / self._get_evaluation_filename(filename_prefix)
            if evaluation_file.exists():
                logger.info(f"️  案例已评估，加载已有评估结果: {evaluation_file.name}")
                try:
                    with open(evaluation_file, 'r', encoding='utf-8') as f:
                        existing_evaluation = json.load(f)

                    # ================== 【新增】0 分拦截逻辑 ==================
                    is_valid = True
                    if self.enable_llm_eval and self.eval_mode != "oracle" and existing_evaluation.get('llm_score', 0) == 0:
                        is_valid = False
                        logger.info(f"  ⚠️ 已有评估结果的 llm_score 为 0，将重新执行评估步骤")
                    
                    # 只有在有效时才直接返回，否则继续往下走重新评估的流程
                    if is_valid:
                        result['status'] = 'success'
                        result['duration'] = time.time() - start_time
                        result['end_time'] = datetime.now().isoformat()
                        result['evaluation'] = existing_evaluation
                        logger.info(f"\n {case_id} 处理成功（使用已有评估结果）！")
                        logger.info(f"️  耗时: {int(result['duration'])}秒")
                        logger.info(f" 评估结果:")
                        logger.info(f"   - Cell F1分数: {existing_evaluation.get('cell_f1', 0):.2f}")
                        logger.info(f"   - 表召回率: {existing_evaluation.get('table_recall', 0):.2f}%")
                        logger.info(f"   - LLM分数: {existing_evaluation.get('llm_score', 0):.1f}/100")
                        return result
                except Exception as e:
                    logger.warning(f"  ️  加载已有评估结果失败: {e}，将重新评估")
            
            logger.info("加载表数据用于评估...")
            output_result = self.load_result_from_output(prefix_output_dir, filename_prefix)
            
            if output_result and output_result.get('tables'):
                generated_tables = output_result['tables']
                logger.info(f"   成功加载 {len(generated_tables)} 个表用于评估")
            else:
                logger.warning(f"  ️  未能从输出目录加载表数据，使用原始结果")
                generated_tables = result_data.get('tables', {})
            
            logger.info("计算Cell级别的召回率和准确率...")
            cell_metrics = self.calculate_cell_metrics(generated_tables, expected_answer, schema, case_info=case_info)
            logger.info(f"   Cell召回率: {cell_metrics['cell_recall']:.2f}%")
            logger.info(f"   Cell准确率: {cell_metrics['cell_precision']:.2f}%")
            logger.info(f"   Cell F1分数: {cell_metrics['cell_f1']:.2f}")
            logger.info(f"  ️  正确Cells: {cell_metrics['total_correct_cells']}/{cell_metrics['total_expected_cells']}")
            
            logger.info("计算结构级别的指标...")
            structural_metrics = self.calculate_structural_metrics(
                generated_tables,
                expected_answer,
                schema,
                case_info=case_info
            )
            logger.info(f"   表召回率: {structural_metrics['table_recall']:.2f}%")
            logger.info(f"   表准确率: {structural_metrics['table_precision']:.2f}%")

            oracle_fine_grained_metrics = {}
            if self.eval_mode == "oracle":
                logger.info("计算 Oracle 细粒度拓扑指标...")
                oracle_fine_grained_metrics = self.calculate_oracle_fine_grained_metrics(cell_metrics, schema)
                for category, m in sorted(oracle_fine_grained_metrics.items()):
                    logger.info(
                        f"   - {category}: P={m['precision']:.2f}% R={m['recall']:.2f}% "
                        f"F1={m['f1']:.2f} (correct={m['correct_cells']}/{m['expected_cells']})"
                    )
            logger.info(f"   字段召回率: {structural_metrics['field_recall']:.2f}%")
            logger.info(f"   字段准确率: {structural_metrics['field_precision']:.2f}%")
            logger.info(f"   Entity F1: {structural_metrics.get('entity_f1', 0):.2f}")
            logger.info(f"   Relation F1: {structural_metrics.get('relation_f1', 0):.2f}")

            if self.enable_llm_eval and self.eval_mode != "oracle":
                logger.info("使用LLM评估结果...")
                llm_evaluation = self.evaluate_with_llm(
                    generated_tables, 
                    expected_answer, 
                    case_info
                )
            else:
                if self.eval_mode == "oracle":
                    logger.info("Oracle模式：跳过LLM评估")
                else:
                    logger.info("跳过LLM评估（仅计算P/R/F1）")
                llm_evaluation = {'overall': 0, 'evaluation_details': '', 'raw_response': ''}
            
            # 新增：执行错误深度分析
            error_analysis = self.analyze_detailed_errors(cell_metrics, expected_answer)

            result['status'] = 'success'
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()

            evaluation = self._build_evaluation_payload(
                cell_metrics=cell_metrics,
                structural_metrics=structural_metrics,
                llm_evaluation=llm_evaluation,
                oracle_fine_grained_metrics=oracle_fine_grained_metrics,
                timestamp_info={
                    "start_time": result['start_time'],
                    "end_time": result['end_time'],
                    "duration_seconds": result['duration']
                },
                schema=schema,
                error_analysis=error_analysis
            )
            result['evaluation'] = evaluation
            
            logger.info("保存评估结果...")
            evaluation_file = prefix_output_dir / self._get_evaluation_filename(filename_prefix)
            with open(evaluation_file, 'w', encoding='utf-8') as f:
                json.dump(evaluation, f, ensure_ascii=False, indent=2)
            logger.info(f"   评估结果已保存: {evaluation_file}")
            
            logger.info(f"\n {case_id} 处理成功！")
            logger.info(f"️  耗时: {int(result['duration'])}秒")
            logger.info(f" 详细评估结果:")
            if self.eval_mode == "oracle":
                logger.info(f"    Oracle关系表指标:")
                logger.info(f"      - Relation Precision: {evaluation.get('relation_precision', 0):.2f}%")
                logger.info(f"      - Relation Recall: {evaluation.get('relation_recall', 0):.2f}%")
                logger.info(f"      - Relation F1: {evaluation.get('relation_f1', 0):.2f}")
                logger.info(f"      - 关系表数量: {len(evaluation.get('relation_table_metrics', {}))}")
            else:
                logger.info(f"    Cell级别指标:")
                logger.info(f"      - 召回率: {evaluation['cell_recall']:.2f}%")
                logger.info(f"      - 准确率: {evaluation['cell_precision']:.2f}%")
                logger.info(f"      - F1分数: {evaluation['cell_f1']:.2f}")
                logger.info(f"      - 精确匹配: {evaluation.get('total_exact_matches', 0)}/{evaluation['total_expected_cells']}")
                logger.info(f"      - 数值精度错误: {evaluation.get('total_numeric_precision_errors', 0)}")
                logger.info(f"      - 字符串相似匹配: {evaluation.get('total_string_similarity_matches', 0)}")
                logger.info(f"      - 类型不匹配: {evaluation.get('total_type_mismatches', 0)}")
                logger.info(f"   ️  结构级别指标:")
                logger.info(f"      - 表召回率: {evaluation['table_recall']:.2f}%")
                logger.info(f"      - 表准确率: {evaluation['table_precision']:.2f}%")
                logger.info(f"      - 字段召回率: {evaluation['field_recall']:.2f}%")
                logger.info(f"      - 字段准确率: {evaluation['field_precision']:.2f}%")
                logger.info(f"    LLM精细化评分:")
                logger.info(f"      - 总分: {evaluation['llm_score']:.1f}/100")
            
            eval_details = evaluation.get('llm_evaluation_details', 'N/A')
            if len(eval_details) > 500:
                eval_details = eval_details[:500] + "..."
            logger.info(f" LLM评估详情: {eval_details}")
            
            if evaluation.get('table_metrics'):
                logger.info(f" 各表详细指标:")
                for table_name, metrics in evaluation['table_metrics'].items():
                    logger.info(f"   - {table_name}: 召回率={metrics['recall']:.1f}%, "
                              f"准确率={metrics['precision']:.1f}%, F1={metrics['f1']:.2f}")
                    logger.info(f"     行数: {metrics['generated_rows']}/{metrics['expected_rows']}, "
                              f"正确cells: {metrics['correct_cells']}/{metrics['expected_cells']}")
                    logger.info(f"     精确匹配: {metrics.get('exact_matches', 0)}, "
                              f"数值精度错误: {metrics.get('numeric_precision_errors', 0)}, "
                              f"类型不匹配: {metrics.get('type_mismatches', 0)}")
            
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            logger.error(f"\n {case_id} 处理失败: {e}")
        
        return result
    
    def scan_all_method_outputs(self) -> List[Dict[str, Any]]:
        """扫描所有方法的输出（包括系统方法和外部方法）
        
        Returns:
            List[Dict]: 每个字典包含：
                - db_name: 数据库名
                - case_name: 案例名
                - method_name: 方法名（如 gpt_4o_multi, langchain, langextract等）
                - output_dir: 输出目录路径
                - has_evaluation: 是否已有评估文件
        """
        all_outputs = []
        
        if not self.output_dir.exists():
            logger.error(f" 输出目录不存在: {self.output_dir}")
            return all_outputs
        
        for db_dir in self.output_dir.iterdir():
            if not db_dir.is_dir() or db_dir.name.startswith('evaluation_'):
                continue
            
            db_name = db_dir.name
            
            for case_dir in db_dir.iterdir():
                if not case_dir.is_dir():
                    continue
                
                case_name = case_dir.name
                
                for method_dir in case_dir.iterdir():
                    if not method_dir.is_dir():
                        continue
                    
                    method_name = method_dir.name
                    
                    has_data = (method_dir / "output_tables.json").exists() or \
                               (method_dir / "extracted_data.json").exists() or \
                               (method_dir / "extracted_data_pipeline_oracle.json").exists() or \
                               (method_dir / "oracle_relation.json").exists() or \
                               (method_dir / "extracted_table.json").exists()
                    
                    if not has_data:
                        continue
                    
                    evaluate_file = method_dir / self._get_evaluation_filename(method_name)
                    has_evaluation = evaluate_file.exists()
                    
                    if has_evaluation:
                        try:
                            with open(evaluate_file, 'r', encoding='utf-8') as f:
                                eval_data = json.load(f)
                            if self.enable_llm_eval and self.eval_mode != "oracle" and eval_data.get('llm_score', 0) == 0:
                                has_evaluation = False
                        except Exception:
                            has_evaluation = False
                            
                    all_outputs.append({
                        'db_name': db_name,
                        'case_name': case_name,
                        'method_name': method_name,
                        'output_dir': method_dir,
                        'has_evaluation': has_evaluation
                    })
        
        logger.info(f" 找到 {len(all_outputs)} 个方法输出")
        
        all_outputs.sort(key=lambda x: (x['db_name'], x['case_name'], x['method_name']))
        
        return all_outputs
    
    def evaluate_method_output(self, method_output: Dict[str, Any], case_info: Dict[str, Any]) -> Dict[str, Any]:
        """评估单个方法的输出
        
        Args:
            method_output: 方法输出信息（包含method_name, output_dir等）
            case_info: 案例信息（包含answer_file, schema_file等）
            
        Returns:
            评估结果字典
        """
        method_name = method_output['method_name']
        output_dir = method_output['output_dir']
        case_id = f"{method_output['db_name']}/{method_output['case_name']}/{method_name}"
        
        logger.info(f"\n 评估: {case_id}")
        
        result = {
            'case_id': case_id,
            'db_name': method_output['db_name'],
            'case_name': method_output['case_name'],
            'method_name': method_name,
            'status': 'failed',
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'duration': 0,
            'evaluation': None,
            'error': None
        }
        
        start_time = time.time()
        
        try:
            logger.info("读取标准答案和schema...")
            expected_answer = self.load_expected_answer(case_info)
            
            schema = {}
            try:
                with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                    schema = json.load(f)
            except Exception as e:
                logger.warning(f"  ️  无法读取schema文件: {e}")
            
            logger.info("加载输出结果...")
            output_result = self.load_result_from_output(output_dir, method_name)
            
            if not output_result or not output_result.get('tables'):
                raise Exception("未能加载有效的输出结果")
            
            generated_tables = output_result['tables']
            logger.info(f"   成功加载 {len(generated_tables)} 个表")
            
            logger.info("计算Cell级别的召回率和准确率...")
            cell_metrics = self.calculate_cell_metrics(generated_tables, expected_answer, schema, case_info=case_info)
            logger.info(f"   Cell召回率: {cell_metrics['cell_recall']:.2f}%")
            logger.info(f"   Cell准确率: {cell_metrics['cell_precision']:.2f}%")
            logger.info(f"   Cell F1分数: {cell_metrics['cell_f1']:.2f}")
            
            logger.info("计算结构级别的指标...")
            structural_metrics = self.calculate_structural_metrics(
                generated_tables,
                expected_answer,
                schema,
                case_info=case_info
            )
            logger.info(f"   表召回率: {structural_metrics['table_recall']:.2f}%")
            logger.info(f"   表准确率: {structural_metrics['table_precision']:.2f}%")

            oracle_fine_grained_metrics = {}
            if self.eval_mode == "oracle":
                logger.info("计算 Oracle 细粒度拓扑指标...")
                oracle_fine_grained_metrics = self.calculate_oracle_fine_grained_metrics(cell_metrics, schema)
                for category, m in sorted(oracle_fine_grained_metrics.items()):
                    logger.info(
                        f"   - {category}: P={m['precision']:.2f}% R={m['recall']:.2f}% "
                        f"F1={m['f1']:.2f} (correct={m['correct_cells']}/{m['expected_cells']})"
                    )
            
            if self.enable_llm_eval and self.eval_mode != "oracle":
                logger.info("使用LLM评估结果...")
                llm_evaluation = self.evaluate_with_llm(
                    generated_tables, 
                    expected_answer, 
                    case_info
                )
            else:
                if self.eval_mode == "oracle":
                    logger.info("Oracle模式：跳过LLM评估")
                else:
                    logger.info("跳过LLM评估（仅计算P/R/F1）")
                llm_evaluation = {'overall': 0, 'evaluation_details': '', 'raw_response': ''}
            
            result['status'] = 'success'
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()

            evaluation = self._build_evaluation_payload(
                cell_metrics=cell_metrics,
                structural_metrics=structural_metrics,
                llm_evaluation=llm_evaluation,
                oracle_fine_grained_metrics=oracle_fine_grained_metrics,
                timestamp_info={
                    "start_time": result['start_time'],
                    "end_time": result['end_time'],
                    "duration_seconds": result['duration']
                },
                schema=schema
            )
            result['evaluation'] = evaluation
            
            logger.info("保存评估结果...")
            evaluation_file = output_dir / self._get_evaluation_filename(method_name)
            with open(evaluation_file, 'w', encoding='utf-8') as f:
                json.dump(evaluation, f, ensure_ascii=False, indent=2)
            logger.info(f"   评估结果已保存: {evaluation_file}")
            
            logger.info(f" {case_id} 评估完成！")
            if self.eval_mode == "oracle":
                logger.info(
                    f"   Relation F1: {evaluation.get('relation_f1', 0):.2f} | "
                    f"关系表数: {len(evaluation.get('relation_table_metrics', {}))}"
                )
            else:
                logger.info(f"   Cell F1: {evaluation['cell_f1']:.2f} | LLM分数: {evaluation['llm_score']:.1f}/100")
            
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            logger.error(f" {case_id} 评估失败: {e}")
            import traceback
            traceback.print_exc()
        
        return result
    
    
    def evaluate_all_methods(self, skip_evaluated: bool = True):
        """评估所有方法的输出（包括外部方法）
        
        Args:
            skip_evaluated: 是否跳过已有评估文件的输出
        """
        logger.info("\n" + "="*80)
        logger.info("评估所有方法的输出结果")
        logger.info("="*80 + "\n")
        
        logger.info("扫描输出目录...")
        all_method_outputs = self.scan_all_method_outputs()
        if self.db_name_filter:
            before = len(all_method_outputs)
            all_method_outputs = [m for m in all_method_outputs if m['db_name'] == self.db_name_filter]
            logger.info(f"   按 --db-name 过滤方法输出: {before} -> {len(all_method_outputs)}")
        if self.case_name_filter:
            before = len(all_method_outputs)
            all_method_outputs = [m for m in all_method_outputs if m['case_name'] == self.case_name_filter]
            logger.info(f"   按 --case-name 过滤方法输出: {before} -> {len(all_method_outputs)}")
        
        if not all_method_outputs:
            logger.error("未找到任何方法输出")
            return
        
        if skip_evaluated:
            to_evaluate = [m for m in all_method_outputs if not m['has_evaluation']]
            skipped_count = len(all_method_outputs) - len(to_evaluate)
            logger.info(f"️  跳过 {skipped_count} 个已评估的输出")
        else:
            to_evaluate = all_method_outputs
            logger.info(f"️  重新评估所有输出")
        
        if not to_evaluate:
            logger.info("所有输出都已评估！")
            return
        
        logger.info(f" 待评估: {len(to_evaluate)} 个方法输出\n")
        
        cases = self.scan_datasets()
        case_info_map = {f"{c['db_name']}/{c['case_name']}": c for c in cases}
        
        for i, method_output in enumerate(to_evaluate, 1):
            logger.info(f"\n{'#'*80}")
            logger.info(f"# 进度: {i}/{len(to_evaluate)}")
            logger.info(f"{'#'*80}")
            
            case_key = f"{method_output['db_name']}/{method_output['case_name']}"
            case_info = case_info_map.get(case_key)
            
            if not case_info:
                logger.error(f" 未找到案例信息: {case_key}")
                continue
            
            result = self.evaluate_method_output(method_output, case_info)
            self.results.append(result)
            
            time.sleep(0.5)
        
        logger.info("\n" + "="*80)
        logger.info("生成统一评估报告...")
        logger.info("="*80 + "\n")
        
        
        self.print_unified_evaluation_summary()
        
        logger.info("\n" + "="*80)
        logger.info("评估完成！")
        logger.info("="*80 + "\n")
    
    def evaluate_existing_outputs(self):
        """评估已有的输出结果（不重新运行处理）"""
        logger.info("\n" + "="*80)
        logger.info("评估已有输出结果")
        logger.info("="*80 + "\n")
        
        logger.info("扫描数据集...")
        cases = self.scan_datasets()
        
        if not cases:
            logger.error("未找到有效的测试案例")
            return
        
        for i, case_info in enumerate(cases, 1):
            logger.info(f"\n{'#'*80}")
            logger.info(f"# 进度: {i}/{len(cases)}")
            logger.info(f"{'#'*80}")
            
            case_id = f"{case_info['db_name']}/{case_info['case_name']}"
            logger.info(f" 评估案例: {case_id}")
            
            if not self.check_case_already_processed(case_info):
                logger.warning(f"️  案例未处理，跳过: {case_id}")
                skipped_result = {
                    'case_id': case_id,
                    'db_name': case_info['db_name'],
                    'case_name': case_info['case_name'],
                    'model': self.model,
                    'run_name': self.run_name,
                    'document_mode': self.document_mode,
                    'status': 'no_output',
                    'message': '未找到输出文件'
                }
                self.results.append(skipped_result)
                continue
            
            # 检查是否已经评估过
            already_evaluated, eval_dir, eval_prefix = self.check_case_already_evaluated(case_info)
            if already_evaluated:
                logger.info(f"️  案例已评估，跳过: {case_id}")
                skipped_result = {
                    'case_id': case_id,
                    'db_name': case_info['db_name'],
                    'case_name': case_info['case_name'],
                    'model': self.model,
                    'run_name': self.run_name,
                    'document_mode': self.document_mode,
                    'status': 'skipped',
                    'message': '已有评估文件，跳过评估'
                }
                self.results.append(skipped_result)
                continue
            
            result = self.evaluate_case_output(case_info)
            self.results.append(result)
        
        logger.info("\n" + "="*80)
        logger.info("生成评估报告...")
        logger.info("="*80 + "\n")
        
        
        self.print_evaluation_summary()
        
        logger.info("\n" + "="*80)
        logger.info("Evaluation completed!")
        logger.info("="*80 + "\n")
    
    def evaluate_case_output(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """评估单个案例的已有输出"""
        case_id = f"{case_info['db_name']}/{case_info['case_name']}"
        logger.info(f"\n 评估案例: {case_id}")
        
        result = {
            'case_id': case_id,
            'db_name': case_info['db_name'],
            'case_name': case_info['case_name'],
            'model': self.model,
            'run_name': self.run_name,
            'document_mode': self.document_mode,
            'status': 'failed',
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'duration': 0,
            'evaluation': None,
            'error': None
        }
        
        start_time = time.time()
        
        # 检查是否已经评估过
        already_evaluated, eval_dir, eval_prefix = self.check_case_already_evaluated(case_info)
        if already_evaluated:
            logger.info(f"️  案例已评估，跳过: {case_id}")
            result['status'] = 'skipped'
            result['message'] = '已有评估文件，跳过评估'
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            return result
        
        try:
            logger.info("读取标准答案和schema...")
            expected_answer = self.load_expected_answer(case_info)
            
            schema = {}
            try:
                with open(case_info['schema_file'], 'r', encoding='utf-8') as f:
                    schema = json.load(f)
            except Exception as e:
                logger.warning(f"  ️  无法读取schema文件: {e}")
            
            case_dir = self.output_dir / case_info['db_name'] / case_info['case_name']
            
            # 首先尝试标准格式的文件夹
            filename_prefix = self._build_filename_prefix(
                case_info['db_name'], 
                case_info['case_name'], 
                self.model
            )
            prefix_output_dir = case_dir / filename_prefix
            
            # 检查标准格式目录是否存在且model匹配
            model_matched = False
            if prefix_output_dir.exists():
                metadata_file = prefix_output_dir / "metadata.json"
                if metadata_file.exists():
                    try:
                        with open(metadata_file, 'r', encoding='utf-8') as f:
                            metadata = json.load(f)
                        if metadata.get('model') == self.model:
                            model_matched = True
                            logger.info(f"找到匹配model的标准格式目录: {filename_prefix}")
                    except Exception as e:
                        logger.debug(f"读取metadata失败 {metadata_file}: {e}")
            
            # 如果标准格式不存在或未匹配，通过 metadata.json 精确查找
            if not model_matched:
                found_dir = None
                for subdir in case_dir.iterdir():
                    if not subdir.is_dir():
                        continue
                    
                    metadata_file = subdir / "metadata.json"
                    if metadata_file.exists():
                        try:
                            with open(metadata_file, 'r', encoding='utf-8') as f:
                                metadata = json.load(f)
                            if metadata.get('model') == self.model:  # 精确匹配
                                output_files = [
                                    subdir / "output_tables.json",
                                    subdir / "extracted_data.json",
                                    subdir / "extracted_data_pipeline_oracle.json",
                                    subdir / "oracle_relation.json",
                                    subdir / "extracted_table.json",
                                ]
                                if any(f.exists() for f in output_files):
                                    found_dir = subdir
                                    filename_prefix = subdir.name
                                    logger.info(f"找到匹配model的输出目录: {subdir.name} (model={self.model})")
                                    break
                        except Exception as e:
                            continue
                
                # ⚠️ 删除了 "safe_model.lower() in subdir.name.lower()" 的兜底代码
                if found_dir:
                    prefix_output_dir = found_dir
                else:
                    raise Exception(
                        f"未找到匹配model='{self.model}'的输出目录。\n"
                        f"请确保目录存在且 metadata.json 中的 model 值匹配。"
                    )
            
            logger.info("加载输出结果...")
            
            output_result = self.load_result_from_output(prefix_output_dir, filename_prefix)
            
            if not output_result or not output_result.get('tables'):
                raise Exception("未能加载有效的输出结果")
            
            generated_tables = output_result['tables']
            logger.info(f"   成功加载 {len(generated_tables)} 个表")
            
            logger.info("计算Cell级别的召回率和准确率...")
            cell_metrics = self.calculate_cell_metrics(generated_tables, expected_answer, schema, case_info=case_info)
            logger.info(f"   Cell召回率: {cell_metrics['cell_recall']:.2f}%")
            logger.info(f"   Cell准确率: {cell_metrics['cell_precision']:.2f}%")
            logger.info(f"   Cell F1分数: {cell_metrics['cell_f1']:.2f}")
            
            logger.info("计算结构级别的指标...")
            structural_metrics = self.calculate_structural_metrics(
                generated_tables,
                expected_answer,
                schema,
                case_info=case_info
            )
            logger.info(f"   表召回率: {structural_metrics['table_recall']:.2f}%")
            logger.info(f"   表准确率: {structural_metrics['table_precision']:.2f}%")
            logger.info(f"   字段召回率: {structural_metrics['field_recall']:.2f}%")
            logger.info(f"   字段准确率: {structural_metrics['field_precision']:.2f}%")

            oracle_fine_grained_metrics = {}
            if self.eval_mode == "oracle":
                logger.info("计算 Oracle 细粒度拓扑指标...")
                oracle_fine_grained_metrics = self.calculate_oracle_fine_grained_metrics(cell_metrics, schema)
                for category, m in sorted(oracle_fine_grained_metrics.items()):
                    logger.info(
                        f"   - {category}: P={m['precision']:.2f}% R={m['recall']:.2f}% "
                        f"F1={m['f1']:.2f} (correct={m['correct_cells']}/{m['expected_cells']})"
                    )
            
            if self.enable_llm_eval and self.eval_mode != "oracle":
                logger.info("使用LLM评估结果...")
                llm_evaluation = self.evaluate_with_llm(
                    generated_tables, 
                    expected_answer, 
                    case_info
                )
            else:
                if self.eval_mode == "oracle":
                    logger.info("Oracle模式：跳过LLM评估")
                else:
                    logger.info("跳过LLM评估（仅计算P/R/F1）")
                llm_evaluation = {'overall': 0, 'evaluation_details': '', 'raw_response': ''}
            
            error_analysis = self.analyze_detailed_errors(cell_metrics, expected_answer)

            result['status'] = 'success'
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()

            evaluation = self._build_evaluation_payload(
                cell_metrics=cell_metrics,
                structural_metrics=structural_metrics,
                llm_evaluation=llm_evaluation,
                oracle_fine_grained_metrics=oracle_fine_grained_metrics,
                timestamp_info={
                    "start_time": result['start_time'],
                    "end_time": result['end_time'],
                    "duration_seconds": result['duration']
                },
                schema=schema,
                error_analysis=error_analysis
            )
            result['evaluation'] = evaluation
            
            logger.info("保存评估结果...")
            evaluation_file = prefix_output_dir / self._get_evaluation_filename(filename_prefix)
            with open(evaluation_file, 'w', encoding='utf-8') as f:
                json.dump(evaluation, f, ensure_ascii=False, indent=2)
            logger.info(f"   评估结果已保存: {evaluation_file}")
            
            logger.info(f"\n {case_id} 评估完成！")
            logger.info(f"️  耗时: {int(result['duration'])}秒")
            logger.info(f" 评估结果:")
            if self.eval_mode == "oracle":
                logger.info(f"   - Relation F1分数: {evaluation.get('relation_f1', 0):.2f}")
                logger.info(f"   - 关系表数量: {len(evaluation.get('relation_table_metrics', {}))}")
            else:
                logger.info(f"   - Cell F1分数: {evaluation['cell_f1']:.2f}")
                logger.info(f"   - 表召回率: {evaluation['table_recall']:.2f}%")
                logger.info(f"   - LLM分数: {evaluation['llm_score']:.1f}/100")
            
        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            result['duration'] = time.time() - start_time
            result['end_time'] = datetime.now().isoformat()
            logger.error(f"\n {case_id} 评估失败: {e}")
        
        return result
    
    def generate_evaluation_standards(self) -> Dict[str, Any]:
        """生成详细的评估标准说明"""
        return {
            "evaluation_framework": {
                "name": "Doc2DB 多维度评估框架",
                "version": "2.0",
                "description": "基于Cell级别、结构级别和LLM评分的综合评估体系"
            },
            "metrics_definition": {
                "cell_level": {
                    "description": "Cell级别指标评估每个数据单元格的提取准确性",
                    "metrics": {
                        "cell_recall": {
                            "definition": "正确提取的cells数量 / 标准答案中的总cells数量",
                            "range": "0-100%",
                            "interpretation": {
                                "90-100%": "优秀 - 几乎完整提取所有信息",
                                "70-89%": "良好 - 提取了大部分重要信息",
                                "50-69%": "中等 - 提取了部分信息，存在遗漏",
                                "30-49%": "较差 - 大量信息遗漏",
                                "0-29%": "很差 - 严重信息缺失"
                            }
                        },
                        "cell_precision": {
                            "definition": "正确提取的cells数量 / 生成结果中的总cells数量",
                            "range": "0-100%",
                            "interpretation": {
                                "90-100%": "优秀 - 几乎没有错误或幻觉",
                                "70-89%": "良好 - 少量错误信息",
                                "50-69%": "中等 - 存在一定错误",
                                "30-49%": "较差 - 错误较多",
                                "0-29%": "很差 - 大量错误或幻觉"
                            }
                        },
                        "cell_f1": {
                            "definition": "召回率和准确率的调和平均数",
                            "formula": "2 * (precision * recall) / (precision + recall)",
                            "range": "0-100",
                            "interpretation": "综合评估提取质量，平衡完整性和准确性"
                        }
                    }
                },
                "structural_level": {
                    "description": "结构级别指标评估表结构和字段的识别准确性",
                    "metrics": {
                        "table_recall": "识别出的正确表数量 / 标准答案中的表数量",
                        "table_precision": "识别出的正确表数量 / 生成结果中的表数量",
                        "field_recall": "识别出的正确字段数量 / 标准答案中的字段数量",
                        "field_precision": "识别出的正确字段数量 / 生成结果中的字段数量"
                    }
                },
                "llm_evaluation": {
                    "description": "基于大语言模型的综合评估（0-100分）",
                    "evaluation_dimensions": {
                        "accuracy_and_hallucinations": {
                            "description": "准确性和幻觉",
                            "criteria": [
                                "生成结果与标准答案在语义上是否一致",
                                "数值型数据是否准确（数值必须完全相等才算正确）",
                                "数值的小数位数是否与标准答案一致",
                                "字符串型数据是否准确（相似度需要达到90%以上）",
                                "是否存在幻觉或错误信息"
                            ]
                        },
                        "completeness": {
                            "description": "完整性",
                            "criteria": [
                                "生成结果是否包含标准答案中的所有关键信息",
                                "是否有重要数据遗漏",
                                "表结构和字段是否完整",
                                "进一步说明这些关键点可以省略，但核心内容必须完整"
                            ]
                        },
                        "format_consistency": {
                            "description": "格式一致性",
                            "criteria": [
                                "数据格式是否符合Schema定义",
                                "数据类型是否正确（数值型、字符串型、日期型等）",
                                "格式是否统一（如日期格式、数字格式等）"
                            ]
                        }
                    },
                    "overall_score": {
                        "definition": "综合评分（总分）",
                        "range": "0-100分",
                        "calculation": "基于多维度的综合评估"
                    },
                    "key_requirements": [
                        "数值必须完全相等才算正确，包括小数位数",
                        "字符串相似度低于90%不算匹配",
                        "类型不匹配是严重错误",
                        "如果生成结果完全符合标准答案的所有关键点，总分应为满分100分"
                    ],
                    "output_format": "Rating: [[score]]，其中score是0-100之间的整数"
                }
            },
            "quality_thresholds": {
                "excellent": {
                    "cell_f1": ">= 90",
                    "table_recall": ">= 90%",
                    "field_recall": ">= 85%",
                    "llm_score": ">= 85",
                    "description": "优秀级别 - 可直接用于生产环境"
                },
                "good": {
                    "cell_f1": "70-89",
                    "table_recall": "70-89%",
                    "field_recall": "65-84%",
                    "llm_score": "70-84",
                    "description": "良好级别 - 经过少量调整可用于生产"
                },
                "acceptable": {
                    "cell_f1": "50-69",
                    "table_recall": "50-69%",
                    "field_recall": "45-64%",
                    "llm_score": "50-69",
                    "description": "可接受级别 - 需要进一步优化"
                },
                "poor": {
                    "cell_f1": "< 50",
                    "table_recall": "< 50%",
                    "field_recall": "< 45%",
                    "llm_score": "< 50",
                    "description": "较差级别 - 需要重大改进"
                }
            },
            "evaluation_process": {
                "steps": [
                    "1. 数据标准化：统一格式、大小写、日期等",
                    "2. Cell级别匹配：逐个比较数据单元格",
                    "3. 结构级别分析：评估表和字段的识别",
                    "4. LLM语义评估：深度理解和综合评分",
                    "5. 多维度综合：生成最终评估报告"
                ],
                "considerations": [
                    "忽略空值和null值的比较",
                    "数字格式标准化（如1.0 -> 1）",
                    "日期格式统一处理",
                    "大小写不敏感匹配",
                    "表名和字段名的灵活匹配"
                ]
            }
        }
    
    
    def print_unified_evaluation_summary(self):
        """打印统一的评估摘要（支持多个方法的对比）"""
        try:
            method_results = {}
            for result in self.results:
                if result['status'] != 'success' or not result.get('evaluation'):
                    continue
                
                method_name = result.get('method_name', 'unknown')
                if method_name not in method_results:
                    method_results[method_name] = []
                method_results[method_name].append(result)
            
            if not method_results:
                logger.warning("没有成功的评估结果")
                return
            
            logger.info("\n各方法平均指标对比:")
            logger.info("-" * 160)
            logger.info(f"{'方法名':30s} | {'测试案例':8s} | {'Cell召回':9s} | {'Cell准确':9s} | {'Cell F1':9s} | {'表召回':8s} | {'字段召回':9s} | {'LLM分数':9s}")
            logger.info("-" * 160)
            
            method_avg_data = []
            for method_name, method_res_list in sorted(method_results.items()):
                cell_recalls = [r['evaluation'].get('cell_recall', 0) for r in method_res_list]
                cell_precisions = [r['evaluation'].get('cell_precision', 0) for r in method_res_list]
                cell_f1s = [r['evaluation'].get('cell_f1', 0) for r in method_res_list]
                table_recalls = [r['evaluation'].get('table_recall', 0) for r in method_res_list]
                field_recalls = [r['evaluation'].get('field_recall', 0) for r in method_res_list]
                llm_scores = [r['evaluation'].get('llm_score', 0) for r in method_res_list]
                
                avg_cell_recall = sum(cell_recalls) / len(cell_recalls)
                avg_cell_precision = sum(cell_precisions) / len(cell_precisions)
                avg_cell_f1 = sum(cell_f1s) / len(cell_f1s)
                avg_table_recall = sum(table_recalls) / len(table_recalls)
                avg_field_recall = sum(field_recalls) / len(field_recalls)
                avg_llm_score = sum(llm_scores) / len(llm_scores)
                
                method_avg_data.append({
                    'method': method_name,
                    'cases': len(method_res_list),
                    'cell_recall': avg_cell_recall,
                    'cell_precision': avg_cell_precision,
                    'cell_f1': avg_cell_f1,
                    'table_recall': avg_table_recall,
                    'field_recall': avg_field_recall,
                    'llm_score': avg_llm_score
                })
                
                logger.info(
                    f"{method_name:30s} | {len(method_res_list):8d} | "
                    f"{avg_cell_recall:7.2f}% | {avg_cell_precision:7.2f}% | "
                    f"{avg_cell_f1:9.2f} | {avg_table_recall:6.1f}% | "
                    f"{avg_field_recall:7.1f}% | {avg_llm_score:7.1f}/100"
                )
            
            logger.info("-" * 160)
            
            if method_avg_data:
                best_by_f1 = max(method_avg_data, key=lambda x: x['cell_f1'])
                best_by_llm = max(method_avg_data, key=lambda x: x['llm_score'])
                
                logger.info(f"\n🏆 最佳方法:")
                logger.info(f"  - Cell F1最高: {best_by_f1['method']} (F1={best_by_f1['cell_f1']:.2f})")
                logger.info(f"  - LLM分数最高: {best_by_llm['method']} (分数={best_by_llm['llm_score']:.1f}/100)")
            
        except Exception as e:
            logger.error(f"打印统一评估摘要失败: {e}")
            import traceback
            traceback.print_exc()
    
    def save_evaluation_report(self, results: List[Dict[str, Any]]) -> Path:
        """生成并保存详细的评估报告"""
        try:
            standards = self.generate_evaluation_standards()
            
            total = len(results)
            success = len([r for r in results if r['status'] == 'success'])
            skipped = len([r for r in results if r['status'] == 'skipped'])
            failed = len([r for r in results if r['status'] == 'failed'])
            
            successful_results = [r for r in results if r['status'] == 'success' and r.get('evaluation')]
            
            if successful_results:
                avg_metrics = {}
                metric_keys = [
                    'cell_recall', 'cell_precision', 'cell_f1',
                    'table_recall', 'table_precision', 
                    'field_recall', 'field_precision',
                    'llm_score',
                    'total_exact_matches', 'total_numeric_precision_errors',
                    'total_string_similarity_matches', 'total_type_mismatches'
                ]
                
                for key in metric_keys:
                    values = [r['evaluation'][key] for r in successful_results if key in r['evaluation']]
                    avg_metrics[key] = sum(values) / len(values) if values else 0
            else:
                avg_metrics = {key: 0 for key in metric_keys}
            
            def get_quality_level(metrics):
                cell_f1 = metrics.get('cell_f1', 0)
                table_recall = metrics.get('table_recall', 0)
                field_recall = metrics.get('field_recall', 0)
                llm_score = metrics.get('llm_score', 0)
                
                if (cell_f1 >= 90 and table_recall >= 90 and 
                    field_recall >= 85 and llm_score >= 85):
                    return "excellent"
                elif (cell_f1 >= 70 and table_recall >= 70 and 
                      field_recall >= 65 and llm_score >= 70):
                    return "good"
                elif (cell_f1 >= 50 and table_recall >= 50 and 
                      field_recall >= 45 and llm_score >= 50):
                    return "acceptable"
                else:
                    return "poor"
            
            overall_quality = get_quality_level(avg_metrics)
            
            report = {
            "metadata": {
                "model": self.model,
                "run_name": self.run_name,
                "document_mode": self.document_mode,
                "timestamp": datetime.now().isoformat(),
                "total_cases": total,
                "evaluation_framework_version": "2.0"
            },
                "summary": {
                    "success_cases": success,
                    "skipped_cases": skipped,
                    "failed_cases": failed,
                    "success_rate": f"{success/total*100:.1f}%" if total > 0 else "0%",
                    "overall_quality_level": overall_quality
                },
                "average_metrics": avg_metrics,
                "evaluation_standards": standards,
                "detailed_results": results
            }
            
            safe_model = self.model.replace('/', '_').replace('\\', '_').replace('-', '_')
            mode = self.document_mode
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_filename = f"evaluation_report_{safe_model}_{mode}_{timestamp}.json"
            if self.run_name:
                safe_run_name = self.run_name.replace('/', '_').replace('\\', '_')
                report_filename = f"evaluation_report_{safe_model}_{mode}_{safe_run_name}_{timestamp}.json"
            
            report_file = self.output_dir / report_filename
            with open(report_file, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            
            logger.info(f"\n 详细评估报告已保存到: {report_file}")
            return report_file
            
        except Exception as e:
            logger.error(f"生成评估报告失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def print_evaluation_summary(self):
        """打印评估摘要"""
        try:
            total = len(self.results)
            success = len([r for r in self.results if r['status'] == 'success'])
            skipped = len([r for r in self.results if r['status'] == 'skipped'])
            failed = len([r for r in self.results if r['status'] == 'failed'])
            
            successful_results = [r for r in self.results if r['status'] == 'success' and r.get('evaluation')]
            
            if successful_results:
                cell_recalls = [r['evaluation'].get('cell_recall', 0) for r in successful_results]
                cell_precisions = [r['evaluation'].get('cell_precision', 0) for r in successful_results]
                cell_f1s = [r['evaluation'].get('cell_f1', 0) for r in successful_results]
                
                table_recalls = [r['evaluation'].get('table_recall', 0) for r in successful_results]
                table_precisions = [r['evaluation'].get('table_precision', 0) for r in successful_results]
                field_recalls = [r['evaluation'].get('field_recall', 0) for r in successful_results]
                field_precisions = [r['evaluation'].get('field_precision', 0) for r in successful_results]
                
                llm_scores = [r['evaluation'].get('llm_score', 0) for r in successful_results]
                
                avg_cell_recall = sum(cell_recalls) / len(cell_recalls)
                avg_cell_precision = sum(cell_precisions) / len(cell_precisions)
                avg_cell_f1 = sum(cell_f1s) / len(cell_f1s)
                avg_table_recall = sum(table_recalls) / len(table_recalls)
                avg_table_precision = sum(table_precisions) / len(table_precisions)
                avg_field_recall = sum(field_recalls) / len(field_recalls)
                avg_field_precision = sum(field_precisions) / len(field_precisions)
                avg_llm_score = sum(llm_scores) / len(llm_scores)
            else:
                avg_cell_recall = avg_cell_precision = avg_cell_f1 = 0
                avg_table_recall = avg_table_precision = 0
                avg_field_recall = avg_field_precision = 0
                avg_llm_score = 0
            
            logger.info("评估摘要:")
            logger.info(f"   配置信息:")
            logger.info(f"     模型: {self.model}")
            logger.info(f"     文档模式: {self.document_mode}")
            if self.run_name:
                logger.info(f"     运行名称: {self.run_name}")
            
            logger.info(f"\n  📈 执行统计:")
            logger.info(f"     总测试案例: {total}")
            logger.info(f"     成功: {success}")
            logger.info(f"     跳过: {skipped}")
            logger.info(f"     失败: {failed}")
            logger.info(f"     成功率: {success/total*100:.1f}%" if total > 0 else "0%")
            
            if successful_results:
                logger.info(f"\n   Cell级别平均指标:")
                logger.info(f"     召回率: {avg_cell_recall:.2f}%")
                logger.info(f"     准确率: {avg_cell_precision:.2f}%")
                logger.info(f"     F1分数: {avg_cell_f1:.2f}")
                
                logger.info(f"\n  ️  结构级别平均指标:")
                logger.info(f"     表召回率: {avg_table_recall:.2f}%")
                logger.info(f"     表准确率: {avg_table_precision:.2f}%")
                logger.info(f"     字段召回率: {avg_field_recall:.2f}%")
                logger.info(f"     字段准确率: {avg_field_precision:.2f}%")
                
                logger.info(f"\n   LLM精细化平均评分:")
                logger.info(f"     总分: {avg_llm_score:.1f}/100")
                
                if (avg_cell_f1 >= 90 and avg_table_recall >= 90 and 
                    avg_field_recall >= 85 and avg_llm_score >= 85):
                    quality_level = "🌟 优秀"
                    quality_desc = "可直接用于生产环境"
                elif (avg_cell_f1 >= 70 and avg_table_recall >= 70 and 
                      avg_field_recall >= 65 and avg_llm_score >= 70):
                    quality_level = "良好"
                    quality_desc = "经过少量调整可用于生产"
                elif (avg_cell_f1 >= 50 and avg_table_recall >= 50 and 
                      avg_field_recall >= 45 and avg_llm_score >= 50):
                    quality_level = "可接受"
                    quality_desc = "需要进一步优化"
                else:
                    quality_level = "较差"
                    quality_desc = "需要重大改进"
                
                logger.info(f"\n   综合质量评级: {quality_level}")
                logger.info(f"     {quality_desc}")
            
            logger.info(f"\n 各案例详细评分:")
            logger.info("-" * 140)
            logger.info(f"{'案例ID':25s} | {'Cell召回':8s} | {'Cell准确':8s} | {'Cell F1':8s} | {'表召回':7s} | {'字段召回':8s} | {'LLM分数':8s} | 状态")
            logger.info("-" * 140)
            
            for result in self.results:
                case_id = result['case_id']
                status = result['status']
                
                if status == 'success' and result.get('evaluation'):
                    eval_data = result['evaluation']
                    cell_recall = eval_data.get('cell_recall', 0)
                    cell_precision = eval_data.get('cell_precision', 0)
                    cell_f1 = eval_data.get('cell_f1', 0)
                    table_recall = eval_data.get('table_recall', 0)
                    field_recall = eval_data.get('field_recall', 0)
                    llm_score = eval_data.get('llm_score', 0)
                    
                    logger.info(f"{case_id:25s} | {cell_recall:6.2f}% | {cell_precision:6.2f}% | {cell_f1:8.2f} | {table_recall:5.1f}% | {field_recall:6.1f}% | {llm_score:6.1f}/100 |  成功")
                elif status == 'skipped':
                    logger.info(f"{case_id:25s} | {'SKIP':8s} | {'SKIP':8s} | {'SKIP':8s} | {'SKIP':7s} | {'SKIP':8s} | {'SKIP':8s} | ️  跳过")
                else:
                    error = result.get('error', 'Unknown error')[:30]
                    logger.info(f"{case_id:25s} | {'FAIL':8s} | {'FAIL':8s} | {'FAIL':8s} | {'FAIL':7s} | {'FAIL':8s} | {'FAIL':8s} |  失败: {error}")
            
            logger.info("-" * 140)
            
        except Exception as e:
            logger.error(f"打印评估摘要失败: {e}")
            import traceback
            traceback.print_exc()
    
    def run_evaluation(self):
        """运行完整的评估流程"""
        logger.info("\n" + "="*80)
        logger.info("Doc2DB 数据集评估开始")
        logger.info("="*80 + "\n")
                
        logger.info("扫描数据集...")
        cases = self.scan_datasets()
        
        if not cases:
            logger.error("未找到有效的测试案例")
            return
        
        for i, case_info in enumerate(cases, 1):
            logger.info(f"\n{'#'*80}")
            logger.info(f"# 进度: {i}/{len(cases)}")
            logger.info(f"{'#'*80}")
            
            case_id = f"{case_info['db_name']}/{case_info['case_name']}"
            logger.info(f" 检查案例: {case_id}")
            
            if self.check_case_already_processed(case_info):
                logger.info(f"️  案例已处理，跳过: {case_id}")
                skipped_result = {
                    'case_id': case_id,
                    'db_name': case_info['db_name'],
                    'case_name': case_info['case_name'],
                    'model': self.model,
                    'run_name': self.run_name,
                    'document_mode': self.document_mode,
                    'status': 'skipped',
                    'message': '已有输出文件，跳过处理'
                }
                self.results.append(skipped_result)
                continue
            
            result = self.process_case(case_info)
            self.results.append(result)
            
            time.sleep(1)
        
        logger.info("\n" + "="*80)
        logger.info("生成评估报告...")
        logger.info("="*80 + "\n")
        
        
        self.print_evaluation_summary()
        
        logger.info("\n" + "="*80)
        logger.info("评估完成！")
        logger.info("="*80 + "\n")
    
  
    
    def generate_evaluation_summary_excel(self, output_file: Optional[str] = None) -> Optional[Path]:
        """
        遍历所有的 {method_name}_evaluate*.json 文件（含 oracle / pipeline-oracle），汇总评估结果并生成Excel报告
        
        Args:
            output_file: 输出Excel文件路径（可选）。如果不指定，则自动生成在output_dir下
            
        Returns:
            生成的Excel文件路径，失败时返回None
        """
        try:
            import pandas as pd
        except ImportError:
            logger.error("需要安装 pandas 库: pip install pandas openpyxl")
            return None
        
        try:
            logger.info("\n" + "="*80)
            logger.info("生成评估汇总Excel报告")
            logger.info("="*80 + "\n")
            
            evaluate_files = list(self.output_dir.glob("**/*_evaluate*.json"))
            
            if not evaluate_files:
                logger.warning("未找到任何评估结果文件")
                return None
            
            logger.info(f" 找到 {len(evaluate_files)} 个评估结果文件")
            
            all_evaluations = []
            
            for eval_file in evaluate_files:
                try:
                    with open(eval_file, 'r', encoding='utf-8') as f:
                        evaluation = json.load(f)
                    
                    parts = eval_file.parts
                    
                    try:
                        output_dir_index = parts.index(self.output_dir.name)
                        db_name = parts[output_dir_index + 1]
                        case_name = parts[output_dir_index + 2]
                        method_name = parts[output_dir_index + 3]
                    except (ValueError, IndexError):
                        logger.warning(f"️  无法解析文件路径: {eval_file}")
                        db_name = eval_file.parent.parent.parent.name if len(eval_file.parts) > 3 else "Unknown"
                        case_name = eval_file.parent.parent.name if len(eval_file.parts) > 2 else "Unknown"
                        method_name = eval_file.parent.name if len(eval_file.parts) > 1 else "Unknown"
                    
                    record = {
                        'Database': db_name,
                        'Case': case_name,
                        'Method': method_name,
                        'Cell Recall (%)': evaluation.get('cell_recall', 0),
                        'Cell Precision (%)': evaluation.get('cell_precision', 0),
                        'Cell F1': evaluation.get('cell_f1', 0),
                        'Table Recall (%)': evaluation.get('table_recall', 0),
                        'Table Precision (%)': evaluation.get('table_precision', 0),
                        'Field Recall (%)': evaluation.get('field_recall', 0),
                        'Field Precision (%)': evaluation.get('field_precision', 0),
                        'LLM Score': evaluation.get('llm_score', 0),
                        'Expected Tables': evaluation.get('expected_tables_count', 0),
                        'Generated Tables': evaluation.get('generated_tables_count', 0),
                        'Correct Tables': evaluation.get('correct_tables_count', 0),
                        'Expected Cells': evaluation.get('total_expected_cells', 0),
                        'Generated Cells': evaluation.get('total_generated_cells', 0),
                        'Correct Cells': evaluation.get('total_correct_cells', 0),
                        'Exact Matches': evaluation.get('total_exact_matches', 0),
                        'Numeric Precision Errors': evaluation.get('total_numeric_precision_errors', 0),
                        'String Similarity Matches': evaluation.get('total_string_similarity_matches', 0),
                        'Type Mismatches': evaluation.get('total_type_mismatches', 0),
                    }
                    
                    all_evaluations.append(record)
                    
                except Exception as e:
                    logger.warning(f"️  读取评估文件失败 {eval_file}: {e}")
                    continue
            
            if not all_evaluations:
                logger.warning("未能成功读取任何评估数据")
                return None
            
            logger.info(f" 成功加载 {len(all_evaluations)} 条评估记录")
            
            df = pd.DataFrame(all_evaluations)
            
            df = df.sort_values(['Database', 'Case', 'Method'])
            
            if output_file is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = self.output_dir / f"evaluation_summary_{timestamp}.xlsx"
            else:
                output_file = Path(output_file)
            
            method_summary = df.groupby('Method').agg({
                'Cell Precision (%)': 'mean',
                'Cell Recall (%)': 'mean', 
                'Cell F1': 'mean',
                'LLM Score': 'mean',
                'Case': 'count'  # 统计每个Method的案例数
            }).round(2)
            
            method_summary.rename(columns={
                'Case': 'Test Cases',
                'Cell Precision (%)': 'Precision (%)',
                'Cell Recall (%)': 'Recall (%)',
                'Cell F1': 'F1',
                'LLM Score': 'LLM Score'
            }, inplace=True)
            method_summary = method_summary.reset_index()
            
            column_order = [
                'Method',
                'Test Cases', 
                'Precision (%)',
                'Recall (%)',
                'F1',
                'LLM Score'
            ]
            method_summary = method_summary[column_order]
            
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                method_summary.to_excel(writer, sheet_name='Method Summary', index=False)
                
                worksheet = writer.sheets['Method Summary']
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    adjusted_width = min(max_length + 2, 20)  # 限制最大宽度为20
                    worksheet.column_dimensions[column_letter].width = adjusted_width
        
            logger.info(f"\n Excel报告生成成功!")
            logger.info(f" 报告路径: {output_file}")
            logger.info(f"\n 报告内容:")
            logger.info(f"  - 方法汇总表: {len(method_summary)} 个方法的评估指标")
            logger.info(f"  - 基于 {len(all_evaluations)} 条评估记录生成")
            
            return output_file
            
        except Exception as e:
            logger.error(f" 生成Excel报告失败: {e}")
            import traceback
            traceback.print_exc()
            return None


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Doc2DB 数据集评测系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法:
  python test_dataset_evaluate.py
  
  python test_dataset_evaluate.py --model gpt-4o
  
  python test_dataset_evaluate.py --model gpt-4o --document-mode single
  
  python test_dataset_evaluate.py --model gpt-4o --run-name exp_001
  
  python test_dataset_evaluate.py --model gemini-2.5-flash --evaluate-only --document-mode multi --db-name customer_product
  
  python test_dataset_evaluate.py --evaluate-all-methods
  
  python test_dataset_evaluate.py --evaluate-all-methods --force-reevaluate
  
  python test_dataset_evaluate.py --model qwen-long --document-mode multi --run-name test_001
  
  python test_dataset_evaluate.py --model gpt-4o --document-mode multi --evaluate-only --run-name evaluation_001
  
  python test_dataset_evaluate.py --generate-excel
  
  python test_dataset_evaluate.py --generate-excel --excel-output ./my_report.xlsx

  # 只跑某个库 / 某个 case（与 --evaluate-only、完整跑流程均可组合）
  python test_dataset_evaluate.py --model qwen2.5-72b-instruct --db-name education --case-name case2 --evaluate-only --eval-mode pipeline-oracle 
  
  # Oracle 细粒度评估（输出 *_evaluate_oracle.json）
  python test_dataset_evaluate.py --model llama-3.1-70b-instruct --eval-mode pipeline-oracle --evaluate-only --db-name finance --case-name case10

  # Pipeline-Oracle：与 pipeline 相同流程与指标，优先读取 extracted_data_pipeline_oracle.json（输出 *_evaluate_pipeline_oracle.json）
  python test_dataset_evaluate.py --model qwen2.5-72b-instruct --eval-mode pipeline-oracle --evaluate-only --db-name education
  
        '''
    )
    
    parser.add_argument('--model', type=str, default=DEFAULT_MODEL,
                        help=f'使用的模型名称（默认: {DEFAULT_MODEL}）')
    parser.add_argument('--document-mode', type=str, default='multi', choices=['single', 'multi'],
                        help='文档处理模式：single（单文档模式）或 multi（多文档合并模式）（默认: multi）')
    parser.add_argument('--run-name', type=str, default=None,
                        help='运行名称（可选）')
    parser.add_argument('--backend-url', type=str, default=BACKEND_URL,
                        help=f'后端服务URL（默认: {BACKEND_URL}）')
    parser.add_argument('--evaluate-only', action='store_true',
                        help='仅评估模式：只对已有的输出结果进行评估，不重新运行处理')
    parser.add_argument('--evaluate-all-methods', action='store_true',
                        help='评估所有方法：评估所有方法的输出结果（包括外部方法如langchain、langextract等）')
    parser.add_argument('--force-reevaluate', action='store_true',
                        help='Force re-evaluation even if evaluation files exist')
    parser.add_argument('--generate-excel', action='store_true',
                        help='生成Excel汇总报告：遍历所有评估结果文件并生成Excel汇总报告')
    parser.add_argument('--excel-output', type=str,  default=None,
                        help='Excel输出文件路径（可选，默认自动生成在output目录）')
    parser.add_argument('--db-name', type=str, default=None,
                        help='只处理该数据库目录名（与 dataset/case/base 下文件夹名一致，默认处理全部）')
    parser.add_argument('--case-name', type=str, default=None,
                        help='只处理该 case 文件夹名（如 case1，默认处理全部）')
    parser.add_argument(
        '--eval-mode',
        type=str,
        default='pipeline',
        choices=['pipeline', 'oracle', 'pipeline-oracle'],
        help=(
            '评估模式：pipeline=常规评估（*_evaluate.json）；'
            'oracle=细粒度关系评估（*_evaluate_oracle.json）；'
            'pipeline-oracle=与 pipeline 相同，优先读取 extracted_data_pipeline_oracle.json（*_evaluate_pipeline_oracle.json）'
        ),
    )
    parser.add_argument('--evaluate-entity-pk', action='store_true',
                        help='是否在评估时将实体表的主键计入得分（默认: False，即自动跳过无业务含义的实体表主键）')
    
    args = parser.parse_args()
    
    print("\n" + "="*80)
    print("🌟 Doc2DB 数据集评测系统")
    print("="*80)
    
    if args.generate_excel:
        print("模式: 生成Excel汇总报告")
        print("="*80 + "\n")
        
        evaluator = Doc2DBEvaluator(
            backend_url=args.backend_url,
            dataset_dir=DATASET_DIR,
            output_dir=OUTPUT_DIR,
            model=args.model,
            run_name=args.run_name,
            document_mode=args.document_mode,
            eval_mode=args.eval_mode,
            evaluate_entity_pk=args.evaluate_entity_pk,
        )
        
        try:
            excel_file = evaluator.generate_evaluation_summary_excel(args.excel_output)
            if excel_file:
                print(f"\n Excel报告生成完成: {excel_file}")
            else:
                print("\nExcel报告生成失败")
        except KeyboardInterrupt:
            print("\n\n操作被用户中断")
        except Exception as e:
            print(f"\n\n 生成Excel报告出错: {e}")
            import traceback
            traceback.print_exc()
        
        return
    
    print(f" 模型: {args.model}")
    print(f" 文档模式: {args.document_mode}")
    print(f" 评估模式: {args.eval_mode}")
    if args.run_name:
        print(f"️  运行名称: {args.run_name}")
    if args.db_name or args.case_name:
        fparts = []
        if args.db_name:
            fparts.append(f"数据库={args.db_name}")
        if args.case_name:
            fparts.append(f"案例={args.case_name}")
        print(f"案例过滤: {', '.join(fparts)}")
    if args.evaluate_only:
        print("模式: 仅评估已有输出")
    else:
        print("模式: 完整处理+评估")
    print("="*80 + "\n")
    
    evaluator = Doc2DBEvaluator(
        backend_url=args.backend_url,
        dataset_dir=DATASET_DIR,
        output_dir=OUTPUT_DIR,
        model=args.model,
        run_name=args.run_name,
        document_mode=args.document_mode,  # 传递文档模式参数
        db_name_filter=args.db_name,
        case_name_filter=args.case_name,
        eval_mode=args.eval_mode,
        evaluate_entity_pk=args.evaluate_entity_pk,
        enable_llm_eval = True
    )
    
    try:
        if args.evaluate_all_methods:
            evaluator.evaluate_all_methods(skip_evaluated=not args.force_reevaluate)
        elif args.evaluate_only:
            evaluator.evaluate_existing_outputs()
        else:
            evaluator.run_evaluation()
    except KeyboardInterrupt:
        print("\n\n评估被用户中断")
    except Exception as e:
        print(f"\n\n 评估过程出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
