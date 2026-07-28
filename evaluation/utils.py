import re
import os
import json
import pickle
import random
import logging
from pathlib import Path
from typing import TypedDict, List, Dict, Any, Optional
import tiktoken
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")

def token_length(text):
    return len(encoding.encode(text, disallowed_special=()))

def read_json(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def read_pkl(path):
    """
    读取pickle文件
    
    Args:
        path: pickle文件路径
    
    Returns:
        pickle文件中存储的数据
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "rb") as f:
        return pickle.load(f)

       
def save_to_json(data, json_file):
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def save_pkl(data, path):
    """
    保存数据到pickle文件
    
    Args:
        data: 要保存的数据
        path: pickle文件保存路径
    """
    # 创建目录（如果不存在）
    os.makedirs(os.path.dirname(path), exist_ok=True)
    
    with open(path, "wb") as f:
        pickle.dump(data, f)

def create_folder(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)

def extract_id_text(input_json_path, output_json_path):
    """
    读取json文件，提取每个样本的id和text字段，构造新的json文件
    输出格式: {"id1": "text1", "id2": "text2", ...}
    
    Args:
        input_json_path: 输入的json文件路径
        output_json_path: 输出的json文件路径
    
    Returns:
        Dict with id as key and text as value
    """
    # 读取原始json文件
    data = read_json(input_json_path)
    
    # 创建输出目录
    create_folder(os.path.dirname(output_json_path))
    
    # 检查数据格式
    result = {}
    
    if isinstance(data, list):
        # 如果数据是列表
        for item in data:
            if isinstance(item, dict) and 'id' in item and 'text' in item:
                result[str(item['id'])] = item['text']
            else:
                print(f"Warning: 跳过不包含id或text字段的项: {item}")
    elif isinstance(data, dict):
        # 如果数据是单个字典
        if 'id' in data and 'text' in data:
            result[str(data['id'])] = data['text']
        else:
            raise ValueError("字典数据中缺少id或text字段")
    else:
        raise ValueError(f"不支持的数据格式: {type(data)}")
    
    # 保存到新的json文件
    save_to_json(result, output_json_path)
    
    print(f"成功处理 {len(result)} 个样本")
    print(f"结果已保存到: {output_json_path}")
    
    return result

def parse_answer_r1(answer):
    cleaned_answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip()
    return cleaned_answer

def _clean_response(text):
    """Clean LLM response by removing code blocks."""
    if not text:
        return text
    if '```json' in text:
        m = re.search(r'```json\s*\n(.*?)```', text, re.DOTALL)
        return m.group(1).strip() if m else text
    elif '```python' in text:
        m = re.search(r'```python\s*\n(.*?)```', text, re.DOTALL)
        return m.group(1).strip() if m else text
    elif '```plaintext' in text:
        m = re.search(r'```plaintext\s*\n(.*?)```', text, re.DOTALL)
        return m.group(1).strip() if m else text
    elif '```' in text:
        m = re.search(r'```\s*\n(.*?)```', text, re.DOTALL)
        return m.group(1).strip() if m else text
    return text


def load_expected_answer(case_info: Dict[str, Any]) -> Dict[str, Any]:
    """加载标准答案：优先 answer.json；否则读取 answer/*.json 并按表名聚合。"""
    answer_file = case_info.get("answer_file")
    if answer_file:
        answer_path = Path(answer_file)
        if answer_path.exists():
            with open(answer_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and isinstance(payload.get("tables"), dict):
                return payload["tables"]
            if isinstance(payload, dict):
                return payload
            raise ValueError(f"answer.json 格式错误: {answer_path}")

    answer_dir = case_info.get("answer_dir")
    if answer_dir:
        answer_path = Path(answer_dir)
        expected: Dict[str, Any] = {}
        for table_file in sorted(answer_path.glob("*.json")):
            with open(table_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                expected[table_file.stem] = payload["rows"]
            elif isinstance(payload, list):
                expected[table_file.stem] = payload
            elif isinstance(payload, dict):
                if len(payload) == 1:
                    key = next(iter(payload.keys()))
                    value = payload[key]
                    if isinstance(value, list):
                        expected[str(key)] = value
                        continue
                raise ValueError(f"answer 目录内文件格式错误: {table_file}")
            else:
                raise ValueError(f"answer 目录内文件格式错误: {table_file}")
        if expected:
            return expected

    raise FileNotFoundError("未找到可用标准答案（answer.json 或 answer/*.json）")


def convert_schema_format(schema: Dict[str, Any], logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """Convert schema format: 'fields' to 'attributes'."""
    if not isinstance(schema, dict):
        return schema

    converted = schema.copy()
    if "tables" in converted:
        new_tables = []
        for table in converted["tables"]:
            new_table = table.copy()
            if "fields" in new_table:
                new_table["attributes"] = new_table.pop("fields")
            new_tables.append(new_table)
        converted["tables"] = new_tables

    if logger:
        logger.info("  ️  Schema format converted (fields -> attributes)")
    return converted


def merge_documents_tables(
    documents: List[Dict[str, Any]],
    logger: Optional[logging.Logger] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """合并 documents 数组中的表数据。"""
    merged_tables = {}
    try:
        for doc_idx, document in enumerate(documents):
            if not isinstance(document, dict):
                if logger:
                    logger.warning(f"  ️  document[{doc_idx}] 不是字典类型，跳过")
                continue

            for table_name, table_data in document.items():
                if table_name in ["metadata", "version", "generator", "merged_files"]:
                    continue
                if not isinstance(table_data, list):
                    if logger:
                        logger.warning(f"  ️  表 {table_name} 在 document[{doc_idx}] 中不是数组类型，跳过")
                    continue
                merged_tables.setdefault(table_name, []).extend(table_data)

        for table_name, rows in merged_tables.items():
            if not rows:
                continue
            seen = set()
            unique_rows = []
            for row in rows:
                row_str = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if row_str not in seen:
                    seen.add(row_str)
                    unique_rows.append(row)
            merged_tables[table_name] = unique_rows
            if logger:
                logger.info(f"    - 表 {table_name}: 合并后共 {len(unique_rows)} 行（去重前 {len(rows)} 行）")
        return merged_tables
    except Exception as e:
        if logger:
            logger.error(f"   合并 documents 表数据失败: {e}")
        return {}


def extract_tables_from_full_result(
    full_result: Dict[str, Any],
    logger: Optional[logging.Logger] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """从 full_result 中提取表数据（支持多表）。"""
    tables = {}
    try:
        if "tables" in full_result and isinstance(full_result["tables"], dict):
            tables = full_result["tables"]
            if tables:
                if logger:
                    logger.info(f"   从 full_result.tables 获取 {len(tables)} 个表")
                return tables

        if "snapshot" in full_result and "rows" in full_result["snapshot"]:
            table_name = full_result["snapshot"].get("table", "unknown")
            if table_name == "unknown" and full_result["snapshot"]["rows"]:
                first_tuple_id = full_result["snapshot"]["rows"][0].get("tuple_id", "")
                if first_tuple_id.startswith("t-"):
                    parts = first_tuple_id.split("-")
                    if len(parts) >= 2:
                        table_name = parts[1]

            rows = []
            for row in full_result["snapshot"]["rows"]:
                row_dict = {}
                if "cells" in row:
                    for attr_name, cell_data in row["cells"].items():
                        if isinstance(cell_data, dict):
                            if "best" in cell_data and isinstance(cell_data["best"], dict):
                                row_dict[attr_name] = cell_data["best"].get("value", "")
                            elif "value" in cell_data:
                                row_dict[attr_name] = cell_data["value"]
                            else:
                                row_dict[attr_name] = ""
                        else:
                            row_dict[attr_name] = cell_data
                if row_dict:
                    rows.append(row_dict)
            if rows:
                tables[table_name] = rows
                if logger:
                    logger.info(f"  ️  从 full_result.snapshot 提取表 {table_name}: {len(rows)} 行（仅单表）")

        elif "table_data" in full_result and "rows" in full_result["table_data"]:
            table_name = full_result.get("summary", {}).get("table_name", "unknown")
            rows = []
            for row in full_result["table_data"]["rows"]:
                row_dict = {}
                for attr_name, cell_data in row.items():
                    if isinstance(cell_data, dict) and "value" in cell_data:
                        row_dict[attr_name] = cell_data["value"]
                    else:
                        row_dict[attr_name] = cell_data
                if row_dict:
                    rows.append(row_dict)
            if rows:
                tables[table_name] = rows
                if logger:
                    logger.info(f"  ️  从 full_result.table_data 提取表 {table_name}: {len(rows)} 行（仅单表）")
    except Exception as e:
        if logger:
            logger.error(f"   从 full_result 提取表数据失败: {e}")
    return tables


def load_best_snapshots_from_file(
    snapshots_file: Path,
    logger: Optional[logging.Logger] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """从 snapshots.jsonl 中挑选最完整快照并转为表数据。"""
    try:
        snapshots_by_table = {}
        with open(snapshots_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshot_dict = json.loads(line)
                    table_name = snapshot_dict.get("table")
                    stage = snapshot_dict.get("processing_stage", "")
                    rows_count = snapshot_dict.get("rows_count", 0)
                    if table_name:
                        snapshots_by_table.setdefault(table_name, []).append({
                            "dict": snapshot_dict,
                            "stage": stage,
                            "rows_count": rows_count
                        })
                except Exception:
                    continue

        tables_data = {}
        for table_name, snapshots_list in snapshots_by_table.items():
            snapshots_list = [
                s for s in snapshots_list
                if "Batch验证" not in s.get("dict", {}).get("stage_description", "")
            ]
            final_snapshots = [s for s in snapshots_list if s["stage"] == "final"]
            verification_snapshots = [s for s in snapshots_list if s["stage"] == "verification"]
            fixing_snapshots = [s for s in snapshots_list if s["stage"] == "fixing"]
            extraction_snapshots = [s for s in snapshots_list if s["stage"] == "extraction"]

            chosen_snapshot = None
            if final_snapshots:
                chosen_snapshot = final_snapshots[-1]
            else:
                all_candidates = []
                if verification_snapshots:
                    all_candidates.extend([(s, "verification") for s in verification_snapshots])
                if fixing_snapshots:
                    all_candidates.extend([(s, "fixing") for s in fixing_snapshots])
                if extraction_snapshots:
                    all_candidates.extend([(s, "extraction") for s in extraction_snapshots])
                if all_candidates:
                    chosen_snapshot, _ = max(all_candidates, key=lambda x: x[0]["rows_count"])

            if not chosen_snapshot:
                continue
            snapshot_dict = chosen_snapshot["dict"]
            rows = snapshot_dict.get("rows", [])
            table_rows = []
            for row in rows:
                row_dict = {}
                cells = row.get("cells", {})
                for attr_name, cell_data in cells.items():
                    if isinstance(cell_data, dict):
                        if "value" in cell_data:
                            row_dict[attr_name] = cell_data["value"]
                        elif "best" in cell_data and isinstance(cell_data["best"], dict):
                            row_dict[attr_name] = cell_data["best"].get("value", "")
                        else:
                            row_dict[attr_name] = ""
                    else:
                        row_dict[attr_name] = cell_data
                if row_dict:
                    table_rows.append(row_dict)
            if table_rows:
                tables_data[table_name] = table_rows
        return tables_data
    except Exception as e:
        if logger:
            logger.error(f"   从snapshots.jsonl读取快照失败: {e}")
        return {}


def load_result_from_output(
    prefix_output_dir: Path,
    eval_mode: str = "pipeline",
    logger: Optional[logging.Logger] = None
) -> Optional[Dict[str, Any]]:
    """从输出目录加载最终结果（支持多种输出格式）。"""
    try:
        if eval_mode == "oracle":
            oracle_relation_file = prefix_output_dir / "oracle_relation.json"
            if oracle_relation_file.exists():
                if logger:
                    logger.info("Reading from oracle_relation.json (oracle mode)...")
                with open(oracle_relation_file, "r", encoding="utf-8") as f:
                    tables = json.load(f)
                if not tables:
                    if logger:
                        logger.warning("oracle_relation.json is empty")
                    return None
                if logger:
                    logger.info(f"Loaded {len(tables)} tables from oracle_relation.json")
                return {"tables": tables}

        if eval_mode == "pipeline-oracle":
            pipeline_oracle_file = prefix_output_dir / "extracted_data_pipeline_oracle.json"
            if pipeline_oracle_file.exists():
                if logger:
                    logger.info(
                        "Reading from extracted_data_pipeline_oracle.json (pipeline-oracle mode)..."
                    )
                with open(pipeline_oracle_file, "r", encoding="utf-8") as f:
                    tables = json.load(f)
                if not tables:
                    if logger:
                        logger.warning("extracted_data_pipeline_oracle.json is empty")
                    return None
                if logger:
                    logger.info(
                        f"Loaded {len(tables)} tables from extracted_data_pipeline_oracle.json"
                    )
                return {"tables": tables}

        output_tables_file = prefix_output_dir / "output_tables.json"
        if output_tables_file.exists():
            if logger:
                logger.info("Reading from output_tables.json...")
            with open(output_tables_file, "r", encoding="utf-8") as f:
                tables = json.load(f)
            if not tables:
                if logger:
                    logger.warning("output_tables.json is empty")
                return None
            if logger:
                logger.info(f"Loaded {len(tables)} tables from output_tables.json")
            return {"tables": tables}

        extracted_data_file = prefix_output_dir / "extracted_data.json"
        if extracted_data_file.exists():
            if logger:
                logger.info("Reading from extracted_data.json...")
            with open(extracted_data_file, "r", encoding="utf-8") as f:
                tables = json.load(f)
            if not tables:
                if logger:
                    logger.warning("extracted_data.json is empty")
                return None
            if logger:
                logger.info(f"Loaded {len(tables)} tables from extracted_data.json")
            return {"tables": tables}

        oracle_relation_file = prefix_output_dir / "oracle_relation.json"
        if oracle_relation_file.exists():
            if logger:
                logger.info("Reading from oracle_relation.json...")
            with open(oracle_relation_file, "r", encoding="utf-8") as f:
                tables = json.load(f)
            if not tables:
                if logger:
                    logger.warning("oracle_relation.json is empty")
                return None
            if logger:
                logger.info(f"Loaded {len(tables)} tables from oracle_relation.json")
            return {"tables": tables}

        extracted_table_file = prefix_output_dir / "extracted_table.json"
        if extracted_table_file.exists():
            if logger:
                logger.info("Reading from extracted_table.json...")
            with open(extracted_table_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if "documents" in data and isinstance(data["documents"], list):
                merged_tables = merge_documents_tables(data["documents"], logger=logger)
                if not merged_tables:
                    if logger:
                        logger.warning("No valid table data in extracted_table.json")
                    return None
                if logger:
                    logger.info(f"Loaded and merged {len(merged_tables)} tables from extracted_table.json (documents format)")
                return {"tables": merged_tables}

            if isinstance(data, dict):
                tables = {}
                metadata_fields = {"metadata", "version", "generator", "merged_files", "tables", "relations"}
                for key, value in data.items():
                    if key in metadata_fields:
                        continue
                    if isinstance(value, list):
                        tables[key] = value
                if not tables:
                    if logger:
                        logger.warning("No valid table data in extracted_table.json")
                    return None
                if logger:
                    logger.info(f"Loaded {len(tables)} tables from extracted_table.json (direct dict format)")
                return {"tables": tables}

            if logger:
                logger.warning("Unsupported format in extracted_table.json")
            return None

        if logger:
            logger.warning(
                "Not found: output_tables.json, extracted_data.json, "
                "extracted_data_pipeline_oracle.json, oracle_relation.json "
                f"or extracted_table.json in {prefix_output_dir}"
            )
        return None
    except Exception as e:
        if logger:
            logger.error(f"Failed to load output result: {e}")
        return None


def save_output(
    case_info: Dict[str, Any],
    result_data: Dict[str, Any],
    output_dir: Path,
    filename_prefix: str,
    model: str,
    run_name: Optional[str],
    document_mode: str,
    backend_output_root: Path,
    logger: Optional[logging.Logger] = None
) -> tuple[Path, str]:
    """保存处理结果到输出目录。"""
    case_dir = output_dir / case_info["db_name"] / case_info["case_name"]
    case_dir.mkdir(parents=True, exist_ok=True)

    prefix_output_dir = case_dir / filename_prefix
    prefix_output_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "model": model,
        "run_name": run_name,
        "document_mode": document_mode,
        "db_name": case_info["db_name"],
        "case_name": case_info["case_name"],
        "filename_prefix": filename_prefix,
        "task_id": result_data.get("task_id", "")
    }
    metadata_file = prefix_output_dir / "metadata.json"
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    task_id = result_data.get("task_id")
    tables_data = {}
    if task_id:
        backend_result_file = backend_output_root / task_id / "result.json"
        if backend_result_file.exists():
            if logger:
                logger.info("  ️  从后端输出目录读取完整的 result.json")
            with open(backend_result_file, "r", encoding="utf-8") as f:
                backend_result = json.load(f)
            tables_data = backend_result.get("tables", {})
            if logger:
                logger.info(f"   从后端 result.json 读取 {len(tables_data)} 个表")
        else:
            if logger:
                logger.warning(f"  ️  后端输出目录不存在: {backend_output_root / task_id}")

    if not tables_data:
        if logger:
            logger.info("  ️  从 full_result 提取表数据")
        tables_data = extract_tables_from_full_result(result_data["full_result"], logger=logger)

    tables_file = prefix_output_dir / "output_tables.json"
    with open(tables_file, "w", encoding="utf-8") as f:
        json.dump(tables_data, f, ensure_ascii=False, indent=2)
    if logger:
        logger.info(f"   保存output_tables.json: {len(tables_data)}个表")

    steps_file = prefix_output_dir / "processing_steps.json"
    with open(steps_file, "w", encoding="utf-8") as f:
        json.dump(result_data["steps"], f, ensure_ascii=False, indent=2)

    if logger:
        logger.info(f"   结果已保存到: {prefix_output_dir}")
        logger.info(f"   文件夹: {filename_prefix}")
    return prefix_output_dir, filename_prefix



def convert_table_string_to_dict(table_string: str) -> Dict[str, Dict[str, int]]:
    """
    Convert table string format to dictionary.
    
    Args:
        table_string: String in format "Team,Goals,Shots,...<NEWLINE>Away Team,1,8,...<NEWLINE>Home Team,1,13,..."
    
    Returns:
        Dictionary with team names as keys and attribute dictionaries as values.
        Example: {
            "Away Team": {"Team_name": "Away Team", "Goals": 1, "Shots": 8, ...},
            "Home Team": {"Team_name": "Home Team", "Goals": 1, "Shots": 13, ...}
        }
    """
    lines = table_string.strip().split("<NEWLINE>")
    
    if len(lines) < 2:
        return {}
    
    # Parse header
    headers = [h.strip() for h in lines[0].split(",")]
    
    # Parse data rows
    result = {}
    for line in lines[1:]:
        values = [v.strip() for v in line.split(",")]
        if len(values) != len(headers):
            continue
        
        team_name = values[0]
        team_stats = {"Team_name": team_name}  # 添加 Team_name 字段，与 calculate_team_statistics 格式一致
        
        for i in range(1, len(headers)):
            attr_name = headers[i]
            try:
                attr_value = int(values[i])
            except ValueError:
                attr_value = values[i]  # Keep as string if not convertible to int
            
            team_stats[attr_name] = attr_value
        
        result[team_name] = team_stats
    
    return result


def convert_dataset_to_json(input_file: str, output_file: str):
    """
    Convert dataset from table string format to standard JSON format.
    
    Args:
        input_file: Path to input JSON file with table strings
        output_file: Path to output JSON file with converted dictionaries
    """
    # Read input file
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    
    # Convert each record
    converted_data = []
    for i, record in enumerate(data):
        converted_record = {
            "id": record.get("id"),
            "text": record.get("text"),
            "team_statistics": convert_table_string_to_dict(record.get("table", ""))
        }
        converted_data.append(converted_record)
        
    # Save output file
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(converted_data, f, ensure_ascii=False, indent=2)
    
    print(f"Successfully converted {len(converted_data)} records to {output_file}")
    
    return converted_data


def extract_text_to_txt(input_json_path: str, output_dir: str):
    """
    读取JSON文件，将每个样本的text字段写入单独的txt文件，文件名为对应的id。
    
    Args:
        input_json_path: 输入的JSON文件路径
        output_dir: 输出目录路径，txt文件将保存在此目录下
    
    Returns:
        int: 成功处理的样本数量
    """
    # 读取JSON文件
    data = read_json(input_json_path)
    
    # 创建输出目录
    create_folder(output_dir)
    
    # 处理数据
    count = 0
    
    if isinstance(data, list):
        # 如果数据是列表
        for item in data:
            if isinstance(item, dict) and 'id' in item and 'text' in item:
                sample_id = str(item['id'])
                text_content = item['text']
                
                # 构建输出文件路径
                output_file = os.path.join(output_dir, f"{sample_id}.txt")
                
                # 写入txt文件
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(text_content)
                
                count += 1
            else:
                print(f"Warning: 跳过不包含id或text字段的项: {item}")
    elif isinstance(data, dict):
        # 如果数据是单个字典
        if 'id' in data and 'text' in data:
            sample_id = str(data['id'])
            text_content = data['text']
            
            # 构建输出文件路径
            output_file = os.path.join(output_dir, f"{sample_id}.txt")
            
            # 写入txt文件
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(text_content)
            
            count += 1
        else:
            raise ValueError("字典数据中缺少id或text字段")
    else:
        raise ValueError(f"不支持的数据格式: {type(data)}")
    
    print(f"成功处理 {count} 个样本")
    print(f"结果已保存到: {output_dir}")
    
    return count


def calculate_cost(usage: Dict[str, int], model_name: str) -> float:
    """
    计算API调用成本
    
    Args:
        usage: token usage dict {'prompt_tokens': int, 'completion_tokens': int, ...}
        model_name: model name string
        
    Returns:
        cost in USD
    """
    # 简化的费率表 (单位: 美元/1k tokens)
    # 请根据实际情况更新
    pricing = {
        "gpt-4o": {
            "input_price": 0.0025,
            "output_price": 0.01
        },
        "gpt-4o-mini": {
            "input_price": 0.00015,
            "output_price": 0.0006
        },
        "gpt-3.5-turbo": {
            "input_price": 0.0005,
            "output_price": 0.0015
        },
        "gemini-2.0-flash": {
            "input_price": 0.0018,
            "output_price": 0.0072
        },
        "gemini-2.5-flash": {
            "input_price": 0.0003,
            "output_price": 0.00255
        }
    }
    
    # 处理模型名称别名或版本后缀
    pricing_model = model_name
    if "gpt-4o" in model_name and "mini" not in model_name:
        pricing_model = "gpt-4o"
    elif "gpt-4o" in model_name and "mini" in model_name:
        pricing_model = "gpt-4o-mini"
        
    if pricing_model not in pricing:
        return 0.0

    p_tokens = usage.get('prompt_tokens', 0)
    c_tokens = usage.get('completion_tokens', 0)
    
    rates = pricing[pricing_model]
    
    input_cost = (p_tokens / 1000) * rates['input_price']
    output_cost = (c_tokens / 1000) * rates['output_price']
    
    return input_cost + output_cost


def split_train_valid(train_json_path: str, valid_json_path: str, valid_ratio: float = None, valid_size: int = None, seed: int = None):
    """
    从train.json中随机选择一定比例或固定数量的数据作为验证集
    
    Args:
        train_json_path: 训练集JSON文件路径
        valid_json_path: 验证集JSON文件保存路径
        valid_ratio: 验证集比例（如果提供了valid_size则忽略此参数）
        valid_size: 验证集固定数量（优先使用此参数）
        seed: 随机种子，用于结果可复现
    
    Returns:
        tuple: (train_data, valid_data) 训练集和验证集数据列表
    """
    # 设置随机种子
    if seed is not None:
        random.seed(seed)
    
    # 读取训练数据
    train_data = read_json(train_json_path)
    
    if not isinstance(train_data, list):
        raise ValueError("训练数据必须是列表格式")
    
    total_samples = len(train_data)
    
    # 确定验证集大小：优先使用valid_size，否则使用valid_ratio
    if valid_size is not None:
        # 使用固定数量
        valid_size = min(valid_size, total_samples)  # 不能超过总样本数
        if valid_size <= 0:
            raise ValueError("valid_size必须大于0")
    elif valid_ratio is not None:
        # 使用比例
        valid_size = max(1, int(total_samples * valid_ratio))  # 至少选择1个样本
    else:
        # 默认使用1%
        valid_size = max(1, int(total_samples * 0.01))
    
    # 随机选择验证集
    valid_data = random.sample(train_data, valid_size)
    
    # 获取验证集的id集合（用于快速查找）
    valid_ids = {item['id'] for item in valid_data}
    
    # 剩余数据作为训练集
    train_data_new = [item for item in train_data if item['id'] not in valid_ids]
    
    # 创建输出目录
    create_folder(os.path.dirname(valid_json_path))
    
    # 保存验证集
    save_to_json(valid_data, valid_json_path)
    
    print(f"总样本数: {total_samples}")
    print(f"验证集样本数: {len(valid_data)} ({len(valid_data)/total_samples*100:.2f}%)")
    print(f"训练集样本数: {len(train_data_new)} ({len(train_data_new)/total_samples*100:.2f}%)")
    print(f"验证集已保存到: {valid_json_path}")
    
    return train_data_new, valid_data

import random

def shuffle_inplace(lst):
    random.shuffle(lst)
    return lst

if __name__ == "__main__":
    # extract_id_text("/data/liangzhuowen/dataset/sports/LiveSum/data/test.json", "/data/liangzhuowen/projects/doc2db/Experiment/datasets/livesum/processed/text.json")
    # extract_text_to_txt("/data/liangzhuowen/dataset/sports/LiveSum/data/test.json", "/data/liangzhuowen/dataset/sports/LiveSum/data/test/")


    # 使用固定数量采样50条
    split_train_valid("/data/liangzhuowen/projects/doc2db/Experiment/datasets/rotowire/valid_with_schema.json", "/data/liangzhuowen/projects/doc2db/Experiment/datasets/rotowire/valid_50_with_schema2.json", valid_size=50, seed=42)
    
    # 或者使用比例采样（如果注释掉上面一行，取消注释下面一行）
    # split_train_valid("/data/liangzhuowen/dataset/sports/LiveSum/data/train.json", "/data/liangzhuowen/projects/doc2db/Experiment/datasets/livesum/valid2.json", valid_ratio=0.05, seed=42)