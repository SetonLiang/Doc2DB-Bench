import sqlite3
import os
import json
import random
import csv
import io
import argparse
import importlib.util
import re
from pathlib import Path
from typing import Dict, List, Optional, Any


def _repo_root() -> Path:
    """data_construction 项目根（src/utils/preprocess_bird.py -> parents[2]）。"""
    return Path(__file__).resolve().parents[2]


def _bird_dataset_roots() -> List[Path]:
    """
    可能放置 BIRD 数据集的根目录（含 train/dev）。
    兼容：本仓库 dataset/BIRD；以及同级 benchmark/dataset/BIRD。
    """
    roots: List[Path] = []
    here = _repo_root()
    for rel in ("dataset/BIRD", "dataset/bird"):
        p = here / rel
        if p.is_dir():
            roots.append(p)
    bench = here.parent / "benchmark" / "dataset" / "BIRD"
    if bench.is_dir() and bench not in roots:
        roots.append(bench)
    return roots


def _infer_bird_split_from_path(db_path: Optional[str]) -> Optional[str]:
    """
    从 sqlite 路径判断属于 train 还是 dev。
    识别目录名 train_databases / train_database（以及 dev 对应形式）。
    返回 'train' | 'dev' | None（无法判断时由调用方兜底）。
    """
    if not db_path:
        return None
    parts = [p.lower() for p in Path(db_path).resolve().parts]
    train_markers = ("train_databases", "train_database")
    dev_markers = ("dev_databases", "dev_database")
    if any(m in parts for m in train_markers):
        return "train"
    if any(m in parts for m in dev_markers):
        return "dev"
    return None


def _resolve_bird_dev_config_path() -> Optional[Path]:
    """定位 BIRD dev config.py（兼容多根目录、BIRD/bird 目录名）。"""
    for bird_root in _bird_dataset_roots():
        path = bird_root / "dev" / "config.py"
        if path.exists():
            return path
    return None


def _load_config_module(config_path: Path) -> Dict[str, Any]:
    """从指定 config.py 加载 CONFIG 字典。"""
    try:
        spec = importlib.util.spec_from_file_location("config_module", str(config_path))
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = getattr(module, "CONFIG", {})
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def _resolve_bird_tables_json_path(db_path: Optional[str] = None) -> Optional[Path]:
    """
    定位 BIRD 表结构定义 JSON：dev 用 dev_tables.json，train 用 train_tables.json。
    路径在 train_databases / train_database 下时只加载 train_tables.json；
    在 dev_databases / dev_database 下时只加载 dev_tables.json。
    若无法从路径判断 split，则优先 dev，再尝试 train。
    """
    split = _infer_bird_split_from_path(db_path)
    if split == "train":
        order = [("train", "train_tables.json")]
    elif split == "dev":
        order = [("dev", "dev_tables.json")]
    else:
        order = [("dev", "dev_tables.json"), ("train", "train_tables.json")]

    for subdir, filename in order:
        for bird_root in _bird_dataset_roots():
            candidate = bird_root / subdir / filename
            if candidate.exists():
                return candidate
    return None


def _db_id_candidates(db_id: str) -> List[str]:
    """归一化可匹配的 db_id 候选（兼容 bird_dev/bird_train 前缀与 _mvdb 后缀）。"""
    candidates = [db_id]
    m = re.match(r"bird_dev_\d+_(.+?)(?:_mvdb)?$", db_id or "")
    if m:
        candidates.append(m.group(1))
    m_tr = re.match(r"bird_train_\d+_(.+?)(?:_mvdb)?$", db_id or "")
    if m_tr:
        candidates.append(m_tr.group(1))
    if db_id.endswith("_mvdb"):
        candidates.append(db_id[:-5])
    return [c for c in candidates if c]


def _json_tables_has_db(tables_path: Path, candidates_lower: set[str]) -> bool:
    """判断 *_tables.json 中是否包含目标 db_id。"""
    try:
        payload = json.loads(tables_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, dict):
            continue
        item_db_id = str(item.get("db_id", "")).strip().lower()
        if item_db_id and item_db_id in candidates_lower:
            return True
    return False


def _infer_bird_split(db_id: str, db_path: Optional[str]) -> str:
    """
    推断 split：
    1) 优先根据 db_path 判定；
    2) 若路径无法判定，则根据 db_id 在 train/dev tables 中的归属判定；
    3) 最后兜底 dev。
    """
    by_path = _infer_bird_split_from_path(db_path)
    if by_path:
        return by_path

    candidates_lower = {c.lower() for c in _db_id_candidates(db_id)}
    # 路径不可判定时，优先按 train 命中（避免 train 数据落到 dev 路径）
    for split, filename in (("train", "train_tables.json"), ("dev", "dev_tables.json")):
        for bird_root in _bird_dataset_roots():
            candidate = bird_root / split / filename
            if candidate.exists() and _json_tables_has_db(candidate, candidates_lower):
                return split
    return "dev"


def _load_bird_tables_entry(db_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    从 dataset/BIRD/{dev|train}/ 下加载指定 db 的 schema 定义（dev_tables.json / train_tables.json）。
    当 db_path 位于 train_databases / train_database 下时，仅从 train_tables.json 解析；
    dev 路径同理使用 dev_tables.json。
    支持 db_id 为真实库名，或 bird_dev_xxx_yyy / bird_train_xxx_yyy 等形式。
    """
    tables_path = _resolve_bird_tables_json_path(db_path)
    if tables_path is None:
        return {}
    try:
        payload = json.loads(tables_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, list):
        return {}

    candidates_lower = {c.lower() for c in _db_id_candidates(db_id)}

    for item in payload:
        if not isinstance(item, dict):
            continue
        item_db_id = str(item.get("db_id", "")).strip()
        if not item_db_id:
            continue
        if item_db_id.lower() in candidates_lower:
            return item
    return {}


def _apply_dev_tables_keys_to_schema(
    schema: Dict[str, Any],
    dev_entry: Dict[str, Any],
) -> None:
    """
    用 dev_tables.json 的主外键定义覆盖 schema['primary_keys'] / schema['foreign_keys']。
    仅覆盖能在当前 SQLite schema 中匹配到的列名。
    """
    if not isinstance(dev_entry, dict):
        return
    table_names_original = dev_entry.get("table_names_original", [])
    column_names_original = dev_entry.get("column_names_original", [])
    primary_keys = dev_entry.get("primary_keys", [])
    foreign_keys = dev_entry.get("foreign_keys", [])
    if not isinstance(table_names_original, list) or not isinstance(column_names_original, list):
        return

    sqlite_tables = set(schema.get("tables", []))
    table_lookup = {t.lower(): t for t in sqlite_tables}

    index_to_col: Dict[int, tuple[str, str]] = {}
    for idx, item in enumerate(column_names_original):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        table_idx = item[0]
        col_name = str(item[1]).strip()
        if not isinstance(table_idx, int) or table_idx < 0:
            continue
        if table_idx >= len(table_names_original):
            continue
        raw_table_name = str(table_names_original[table_idx]).strip()
        table_name = table_lookup.get(raw_table_name.lower(), raw_table_name)
        if not table_name or table_name not in sqlite_tables or not col_name:
            continue
        index_to_col[idx] = (table_name, col_name)

    columns_by_table = {
        table: {col["name"] for col in schema.get("columns", {}).get(table, [])}
        for table in sqlite_tables
    }

    def _normalize_column_name(table: str, col: str) -> Optional[str]:
        if col in columns_by_table.get(table, set()):
            return col
        lower_map = {c.lower(): c for c in columns_by_table.get(table, set())}
        return lower_map.get(col.lower())

    new_pk_map: Dict[str, List[str]] = {t: [] for t in sqlite_tables}
    new_fk_map: Dict[str, Dict[str, tuple[str, str]]] = {t: {} for t in sqlite_tables}

    def _iter_pk_indexes(pk_item: Any) -> List[int]:
        if isinstance(pk_item, int):
            return [pk_item]
        if isinstance(pk_item, (list, tuple)):
            return [x for x in pk_item if isinstance(x, int)]
        return []

    if isinstance(primary_keys, list):
        for pk_item in primary_keys:
            for pk_idx in _iter_pk_indexes(pk_item):
                info = index_to_col.get(pk_idx)
                if info is None:
                    continue
                table_name, col_name_raw = info
                col_name = _normalize_column_name(table_name, col_name_raw)
                if not col_name:
                    continue
                if col_name not in new_pk_map[table_name]:
                    new_pk_map[table_name].append(col_name)

    if isinstance(foreign_keys, list):
        for fk_item in foreign_keys:
            if not isinstance(fk_item, (list, tuple)) or len(fk_item) < 2:
                continue
            src_idx, dst_idx = fk_item[0], fk_item[1]
            if not isinstance(src_idx, int) or not isinstance(dst_idx, int):
                continue
            src_info = index_to_col.get(src_idx)
            dst_info = index_to_col.get(dst_idx)
            if src_info is None or dst_info is None:
                continue
            src_table, src_col_raw = src_info
            dst_table, dst_col_raw = dst_info
            src_col = _normalize_column_name(src_table, src_col_raw)
            dst_col = _normalize_column_name(dst_table, dst_col_raw)
            if not src_col or not dst_col:
                continue
            new_fk_map[src_table][src_col] = (dst_table, dst_col)

    for table in sqlite_tables:
        if new_pk_map[table]:
            schema["primary_keys"][table] = new_pk_map[table]
        if new_fk_map[table]:
            schema["foreign_keys"][table] = new_fk_map[table]


def _match_db_config_entry(config: Dict[str, Any], db_id: str) -> Dict[str, Any]:
    """按 db_id 匹配 CONFIG 项（嵌套格式：{db_id: {...}}）。"""
    if not isinstance(config, dict):
        return {}
    matched_db_id = None
    for key in config:
        if key in db_id or db_id.endswith(key):
            matched_db_id = key
            break
    if matched_db_id is None:
        matched_db_id = db_id
    entry = config.get(matched_db_id, {})
    return entry if isinstance(entry, dict) else {}


def _get_dataset_config_entry(config: Dict[str, Any], db_id: str) -> Dict[str, Any]:
    """
    解析 CONFIG：支持
    - 平铺单库：顶层含 db_path / anchor_table 等；
    - 嵌套多库：{db_name: {...}}（兼容 dataset/BIRD/dev/config.py）。
    """
    if not isinstance(config, dict) or not config:
        return {}
    flat_markers = (
        "db_path",
        "anchor_table",
        "fk_relations",
        "relation_pairs",
        "anchor_cols",
        "cluster_cols",
    )
    if any(k in config for k in flat_markers):
        return config
    return _match_db_config_entry(config, db_id)


def extract_schema(
    cursor: sqlite3.Cursor,
    db_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict:
    """提取数据库 schema：所有表（排除 sqlite 内部表）；优先使用 dev/train 的 *_tables.json 覆盖 PK/FK。"""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    all_tables = [row[0] for row in cursor.fetchall()]

    schema = {
        'tables': all_tables,
        'columns': {},
        'primary_keys': {},
        'foreign_keys': {}
    }

    for table in schema['tables']:
        cursor.execute(f'PRAGMA table_info("{table}")')
        columns = cursor.fetchall()
        schema['columns'][table] = [
            {'name': col[1], 'type': col[2], 'notnull': col[3], 'pk': col[5]}
            for col in columns
        ]
        schema['primary_keys'][table] = [
            col['name'] for col in schema['columns'][table] if col['pk'] > 0
        ]

    for table in schema['tables']:
        cursor.execute(f'PRAGMA foreign_key_list("{table}")')
        fks = cursor.fetchall()
        fk_dict = {}
        for fk in fks:
            from_col = fk[3]
            to_table = fk[2]
            to_col = fk[4]
            if to_col is None or to_col == '':
                to_table_pks = schema['primary_keys'].get(to_table, [])
                to_col = to_table_pks[0] if to_table_pks else None
            if to_col:
                fk_dict[from_col] = (to_table, to_col)
        schema['foreign_keys'][table] = fk_dict

    # 对 BIRD 数据集：若能在 dev_tables.json / train_tables.json 找到该库定义，则覆盖主外键关系。
    if db_id:
        tables_entry = _load_bird_tables_entry(db_id, db_path=db_path)
        if tables_entry:
            _apply_dev_tables_keys_to_schema(schema, tables_entry)

    return schema


def _infer_dataset_db_name(db_path: str, db_id: Optional[str] = None) -> str:
    """推断 dataset/BIRD/{train|dev}_databases 下的数据库目录名。"""
    # 优先从 db_path 中解析 .../{train|dev}_databases/<db_name>/...（含 *_database 变体）
    parts = Path(db_path).resolve().parts
    parts_lower = [p.lower() for p in parts]
    for marker in ("train_databases", "train_database", "dev_databases", "dev_database"):
        if marker in parts_lower:
            idx = parts_lower.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]

    # 再从 db_id 解析，如 bird_dev_128_financial 或 bird_dev_128_financial_mvdb
    if db_id:
        m = re.match(r"bird_dev_\d+_(.+?)(?:_mvdb)?$", db_id)
        if m:
            return m.group(1)
        if db_id.endswith("_mvdb"):
            return db_id[:-5]
        return db_id

    # 最后兜底：使用 sqlite 文件名
    stem = Path(db_path).stem
    if stem.endswith("_mvdb"):
        stem = stem[:-5]
    return stem


def _normalize_description_block(text: str) -> str:
    """将 CSV 字段内多行文本（含 Commonsense / Normal range 等）压成可读的单段描述。"""
    if not text or not str(text).strip():
        return ""
    lines = [ln.strip() for ln in str(text).splitlines()]
    parts = [ln for ln in lines if ln]
    return " ".join(parts)


def _merge_csv_column_description(row: Dict[str, str]) -> str:
    """
    合并 BIRD database_description 行：column_description、value_description（可含换行与 range）、data_format。
    """
    col_part = _normalize_description_block((row.get("column_description") or "").strip())
    val_part = _normalize_description_block((row.get("value_description") or "").strip())
    fmt = (row.get("data_format") or "").strip()

    pieces: List[str] = []
    if col_part:
        pieces.append(col_part)
    if val_part:
        # 避免 column 与 value 完全相同时的重复
        if val_part != col_part:
            pieces.append(val_part)
        elif not col_part:
            pieces.append(val_part)

    merged = " ".join(pieces).strip()
    if fmt and fmt.lower() not in ("none", "null"):
        low = merged.lower()
        if fmt.lower() not in low and not any(
            token in low for token in ("integer", "real", "text", "date", "time", "bool")
        ):
            merged = f"{merged} (data format: {fmt})".strip() if merged else f"data format: {fmt}"
    return merged


def _parse_database_description_csv(content: str) -> List[Dict[str, str]]:
    """
    解析 database_description 下的 CSV。必须对全文使用 csv.DictReader，以正确处理
    value_description 中带引号、换行、逗号的内容；不得在首个「commonsense evidence:」处截断，
    否则会在首条数据行中间切开文件，导致整表解析失败。
    """
    rows: List[Dict[str, str]] = []
    # 兼容极少数在 CSV 正文后追加无引号说明段的旧文件：先尝试全文解析，失败再回退截断。
    for attempt in ("full", "truncated"):
        buf = content
        if attempt == "truncated":
            evidence_start = content.lower().find("commonsense evidence:")
            if evidence_start == -1:
                continue
            buf = content[:evidence_start].strip()

        try:
            reader = csv.DictReader(io.StringIO(buf))
            if not reader.fieldnames:
                continue
            parsed: List[Dict[str, str]] = []
            for row in reader:
                if not row:
                    continue
                parsed.append({k: (v if v is not None else "") for k, v in row.items()})
            if parsed:
                rows = parsed
                break
        except Exception:
            continue
    return rows


def load_table_descriptions(db_path: str, table_names: List[str], db_id: Optional[str] = None) -> Dict[str, Dict]:
    """读取表描述信息，优先 db_path 同级目录，失败则回溯 dataset 目录。"""
    descriptions = {}
    db_dir = Path(db_path).resolve().parent
    inferred_db_name = _infer_dataset_db_name(db_path, db_id)

    candidate_desc_dirs: List[Path] = [db_dir / "database_description"]
    for bird_root in _bird_dataset_roots():
        candidate_desc_dirs.extend(
            [
                bird_root / "dev" / "dev_databases" / inferred_db_name / "database_description",
                bird_root / "dev" / "dev_database" / inferred_db_name / "database_description",
                bird_root / "train" / "train_databases" / inferred_db_name / "database_description",
                bird_root / "train" / "train_database" / inferred_db_name / "database_description",
                bird_root / "ours" / inferred_db_name / "database_description",
            ]
        )

    desc_dir = None
    for candidate in candidate_desc_dirs:
        if candidate.exists():
            desc_dir = candidate
            break

    if desc_dir is None:
        return descriptions

    for table_name in table_names:
        csv_file = desc_dir / f"{table_name}.csv"
        if not csv_file.exists():
            continue

        table_desc = {"columns": {}}
        try:
            with open(csv_file, "r", encoding="utf-8-sig") as f:
                content = f.read()
            data_rows = _parse_database_description_csv(content)
            for row in data_rows:
                original_col = (
                    row.get("original_column_name", "")
                    or row.get("\ufefforiginal_column_name", "")
                ).strip()
                col_name = (row.get("column_name", "") or "").strip()
                col_desc = _merge_csv_column_description(row)
                if not col_desc:
                    col_desc = col_name
                if col_desc:
                    if original_col:
                        table_desc["columns"][original_col] = col_desc
                    if col_name:
                        table_desc["columns"][col_name] = col_desc
            descriptions[table_name] = table_desc
        except Exception:
            continue

    return descriptions


def load_anchor_table(db_id: str) -> List[str]:
    """
    从 dataset/bird/dev/config.py 加载 anchor_table。
    返回 anchor_table 列表，若无法加载则返回 []。
    """
    config_path = _resolve_bird_dev_config_path()
    if config_path is None:
        return []
    config = _load_config_module(config_path)
    entry = _get_dataset_config_entry(config, db_id)
    anchor = entry.get("anchor_table", [])
    if isinstance(anchor, str):
        return [anchor]
    if isinstance(anchor, list):
        return list(anchor)
    return []


def load_db_config_entry(db_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载指定数据库配置项。
    优先读取 db_path 同级目录 config.py；其次回退到 dataset/BIRD/dev/config.py。
    """
    candidate_paths: List[Path] = []
    if db_path:
        candidate_paths.append(Path(db_path).resolve().parent / "config.py")
    bird_dev_config = _resolve_bird_dev_config_path()
    if bird_dev_config is not None:
        candidate_paths.append(bird_dev_config)

    for path in candidate_paths:
        if not path.exists():
            continue
        config = _load_config_module(path)
        entry = _get_dataset_config_entry(config, db_id)
        if entry:
            return entry
    return {}


def _build_config_relations(
    db_config: Dict[str, Any],
    schema: Dict[str, Any],
) -> List[tuple[str, str, str, str]]:
    """
    从 config 里构建关系: (src_table, src_col, dst_table, dst_col)。
    优先 relation_pairs，其次 fk_relations。
    """
    relations: List[tuple[str, str, str, str]] = []
    tables = set(schema.get("tables", []))
    columns = schema.get("columns", {})
    primary_keys = schema.get("primary_keys", {})
    table_lookup = {t.lower(): t for t in tables}

    def _norm_table(name: str) -> str:
        return table_lookup.get((name or "").strip().lower(), (name or "").strip())

    relation_pairs = db_config.get("relation_pairs", [])
    if isinstance(relation_pairs, list):
        for pair in relation_pairs:
            if not isinstance(pair, dict):
                continue
            name = str(pair.get("name", "")).strip()
            output_fields = pair.get("output_fields", [])
            if not name or not isinstance(output_fields, list):
                continue
            for item in output_fields:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                dst_table_raw = str(item[0]).strip()
                dst_table = _norm_table(dst_table_raw)
                dst_col = str(item[1]).strip()
                lower_name = name.lower()
                lower_suffix = f"_{dst_table_raw.lower()}"
                src_table = name[: -len(lower_suffix)] if lower_name.endswith(lower_suffix) else ""
                src_table = _norm_table(src_table)
                if not src_table or src_table not in tables or dst_table not in tables:
                    continue
                src_cols = {c["name"] for c in columns.get(src_table, [])}
                dst_cols = {c["name"] for c in columns.get(dst_table, [])}
                if dst_col in src_cols and dst_col in dst_cols:
                    relations.append((src_table, dst_col, dst_table, dst_col))

    fk_relations = db_config.get("fk_relations", [])
    if isinstance(fk_relations, list):
        for rel in fk_relations:
            text = str(rel).strip()
            if not text:
                continue
            parts = [p.strip() for p in text.replace("→", "->").split("->")]
            if len(parts) != 2:
                continue
            src_table, dst_table = _norm_table(parts[0]), _norm_table(parts[1])
            if src_table not in tables or dst_table not in tables:
                continue
            src_cols = {c["name"] for c in columns.get(src_table, [])}
            dst_cols = {c["name"] for c in columns.get(dst_table, [])}
            candidate_col = ""
            for pk in primary_keys.get(dst_table, []):
                if pk in src_cols and pk in dst_cols:
                    candidate_col = pk
                    break
            if not candidate_col:
                common_cols = sorted(src_cols.intersection(dst_cols))
                if common_cols:
                    candidate_col = common_cols[0]
            if candidate_col:
                relations.append((src_table, candidate_col, dst_table, candidate_col))

    # 去重
    uniq = []
    seen = set()
    for r in relations:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def _enforce_anchor_cascade(
    cursor: sqlite3.Cursor,
    schema: Dict[str, Any],
    all_tables: Dict[str, List[Dict[str, Any]]],
    db_config: Dict[str, Any],
) -> None:
    """
    基于 config 关系补齐实体表记录，确保 anchor 表所需 ID 在实体表中可找到。
    若目标实体表本身不存在该 ID，则剔除关系表中的悬空外键记录。
    """
    relations = _build_config_relations(db_config, schema)
    if not relations:
        return

    anchor_set = set(db_config.get("anchor_table", []) or [])
    if anchor_set:
        relations = [r for r in relations if r[0] in anchor_set]
    if not relations:
        return

    # 关系表外键列必须非 NULL：先清理，再做级联补齐
    for src_table, src_col, _, _ in relations:
        src_rows = all_tables.get(src_table, [])
        if not src_rows:
            continue
        all_tables[src_table] = [r for r in src_rows if r.get(src_col) is not None]

    # 用主键去重，若无主键则按完整行去重
    def row_key(table: str, row: Dict[str, Any]) -> tuple:
        pks = schema.get("primary_keys", {}).get(table, [])
        if pks:
            return tuple(row.get(pk) for pk in pks)
        cols = [c["name"] for c in schema.get("columns", {}).get(table, [])]
        return tuple(row.get(c) for c in cols)

    changed = True
    while changed:
        changed = False
        for src_table, src_col, dst_table, dst_col in relations:
            src_rows = all_tables.get(src_table, [])
            dst_rows = all_tables.get(dst_table, [])
            if not src_rows:
                continue

            need_vals = {r.get(src_col) for r in src_rows if r.get(src_col) is not None}
            have_vals = {r.get(dst_col) for r in dst_rows if r.get(dst_col) is not None}
            missing_vals = [v for v in need_vals if v not in have_vals]
            if not missing_vals:
                continue

            # SQLite 单次 IN 最多 999 个变量，分批查询
            SQLITE_MAX_VARS = 999
            fetched = []
            for i in range(0, len(missing_vals), SQLITE_MAX_VARS):
                batch = missing_vals[i : i + SQLITE_MAX_VARS]
                placeholders = ",".join("?" for _ in batch)
                cursor.execute(
                    f'SELECT * FROM "{dst_table}" WHERE "{dst_col}" IN ({placeholders})',
                    batch,
                )
                fetched.extend(cursor.fetchall())
            existing_keys = {row_key(dst_table, r) for r in dst_rows}
            for row in fetched:
                new_row = {col["name"]: row[col["name"]] for col in schema["columns"][dst_table]}
                k = row_key(dst_table, new_row)
                if k in existing_keys:
                    continue
                dst_rows.append(new_row)
                existing_keys.add(k)
                changed = True
            all_tables[dst_table] = dst_rows

            # 强一致约束：关系表外键必须能在目标实体表命中，否则丢弃该关系记录
            valid_vals = {r.get(dst_col) for r in dst_rows if r.get(dst_col) is not None}
            filtered_src_rows = [r for r in src_rows if r.get(src_col) in valid_vals]
            if len(filtered_src_rows) != len(src_rows):
                all_tables[src_table] = filtered_src_rows
                changed = True


def convert_schema_to_output_format(
    schema: Dict,
    table_descriptions: Optional[Dict[str, Dict]] = None,
    anchor_table: Optional[List[str]] = None,
) -> List[Dict]:
    """将内部 schema 格式转换为输出格式。若提供 anchor_table，则表中在 anchor_table 的 type 为 relation，否则为 entity。"""
    table_descriptions = table_descriptions or {}
    anchor_set = set(anchor_table) if anchor_table else set()
    output_schema = []

    def _normalize_name(name: str) -> str:
        return "".join(ch for ch in (name or "").lower() if ch.isalnum())

    for table_name in schema['tables']:
        table_col_descriptions = table_descriptions.get(table_name, {}).get('columns', {})
        normalized_desc_map = {_normalize_name(k): v for k, v in table_col_descriptions.items() if k}
        table_type = "relation" if table_name in anchor_set else "entity"
        table_def = {
            "id": str(len(output_schema) + 1),
            "name": table_name,
            "type": table_type,
            "fields": []
        }
        for col in schema['columns'][table_name]:
            field_def = {
                "name": col['name'],
                "type": col['type'],
                "constraints": {"nullable": not col['notnull']}
            }
            # 优先精确匹配，再用标准化名称匹配，确保描述尽可能写入 schema
            col_name = col['name']
            desc = ""
            if col_name in table_col_descriptions:
                desc = (table_col_descriptions.get(col_name) or "").strip()
            if not desc:
                desc = (normalized_desc_map.get(_normalize_name(col_name), "") or "").strip()
            if desc:
                field_def["description"] = desc
            if col['pk'] > 0:
                field_def["constraints"]["primary_key"] = True
            fk_columns = schema['foreign_keys'].get(table_name, {})
            if col['name'] in fk_columns:
                ref_table, ref_col = fk_columns[col['name']]
                field_def["constraints"]["foreign_key"] = {"table": ref_table, "column": ref_col}
            table_def["fields"].append(field_def)
        output_schema.append(table_def)

    return output_schema


def _build_fk_relations_for_config(schema: Dict[str, Any], anchor_table: List[str]) -> List[str]:
    relations: List[str] = []
    for src in anchor_table:
        fk_map = schema.get("foreign_keys", {}).get(src, {})
        for _, (dst_table, _) in fk_map.items():
            relations.append(f"{src}→{dst_table}")
    deduped: List[str] = []
    seen = set()
    for rel in relations:
        if rel not in seen:
            seen.add(rel)
            deduped.append(rel)
    return deduped


def _build_relation_pairs_for_config(schema: Dict[str, Any], anchor_table: List[str]) -> List[Dict[str, Any]]:
    pairs: List[Dict[str, Any]] = []
    for src in anchor_table:
        fk_map = schema.get("foreign_keys", {}).get(src, {})
        if not fk_map:
            continue
        ordered_targets: List[tuple[str, str]] = []
        seen_tables = set()
        for _, (dst_table, dst_col) in fk_map.items():
            if dst_table in seen_tables:
                continue
            seen_tables.add(dst_table)
            ordered_targets.append((dst_table.lower(), dst_col))
        for dst_table, dst_col in ordered_targets:
            pairs.append(
                {
                    "name": f"{src.lower()}_{dst_table}",
                    "output_fields": [(dst_table, dst_col)],
                }
            )
    return pairs


def _pick_anchor_columns(columns: List[str]) -> List[str]:
    if not columns:
        return []
    preferred_tokens = (
        "name",
        "title",
        "email",
        "phone",
        "address",
        "type",
        "status",
        "description",
        "gender",
        "grade",
        "cost",
        "date",
        "start",
        "end",
    )
    low_priority_tokens = ("id", "code", "key", "num")
    preferred = [col for col in columns if any(token in col.lower() for token in preferred_tokens)]
    if preferred:
        return preferred[:2]
    filtered = [col for col in columns if not any(token in col.lower() for token in low_priority_tokens)]
    if filtered:
        return filtered[:2]
    return columns[:2]


def _build_anchor_cols_for_config(
    schema: Dict[str, Any],
    relation_tables: Optional[List[str]] = None,
) -> List[Dict[str, List[str]]]:
    anchor_map: Dict[str, List[str]] = {}
    relation_set = set(relation_tables or [])
    columns_map = schema.get("columns", {})
    pk_map = schema.get("primary_keys", {})
    fk_map = schema.get("foreign_keys", {})
    for table in schema.get("tables", []):
        # # anchor_cols 仅保留实体表字段，不包含关系表。
        # if table in relation_set:
        #     continue
        cols = [c.get("name") for c in columns_map.get(table, []) if c.get("name")]
        pk_cols = set(pk_map.get(table, []))
        fk_cols = set((fk_map.get(table, {}) or {}).keys())
        candidates = [c for c in cols if c not in pk_cols and c not in fk_cols]
        # ----
        if table in relation_set and not candidates:
            # 关系表常为纯外键/联合主键：若无“属性列”，退化为非主键列（含外键）再选
            candidates = [c for c in cols if c not in pk_cols]
        if table in relation_set and not candidates:
            candidates = cols
        # ------
        picked = _pick_anchor_columns(candidates)
        if picked:
            anchor_map[table.lower()] = picked
    return [anchor_map]


def _build_generated_db_config(
    schema: Dict[str, Any],
    db_id: str,
    db_path: str,
    existing_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    existing_entry = existing_entry or {}
    sqlite_name = Path(db_path).name
    db_name = _infer_dataset_db_name(db_path, db_id=db_id)

    anchor_table = [
        table
        for table in schema.get("tables", [])
        if schema.get("foreign_keys", {}).get(table)
    ]
    fk_relations = _build_fk_relations_for_config(schema, anchor_table)
    relation_pairs = _build_relation_pairs_for_config(schema, anchor_table)
    anchor_cols = _build_anchor_cols_for_config(schema, relation_tables=anchor_table)

    split = _infer_bird_split(db_id=db_id, db_path=db_path)
    if split == "train":
        rel_db_path = f"train/train_databases/{db_name}/{sqlite_name}"
    else:
        rel_db_path = f"dev/dev_databases/{db_name}/{sqlite_name}"

    generated = {
        "db_path": rel_db_path,
        "anchor_table": anchor_table,
        "fk_relations": fk_relations,
        "relation_pairs": relation_pairs,
        "anchor_cols": anchor_cols,
        "cluster_cols": existing_entry.get("cluster_cols", []),
    }
    # 保留已有的额外配置字段（如 num_docs/domain/characteristic），避免覆盖业务参数。
    for key, value in existing_entry.items():
        if key not in generated:
            generated[key] = value
    return generated


def _save_generated_config_py(
    output_dir: str,
    config_entry: Dict[str, Any],
) -> None:
    def _format_python_literal(value: Any, indent: int = 0) -> str:
        pad = " " * indent
        next_pad = " " * (indent + 4)
        if isinstance(value, dict):
            if not value:
                return "{}"
            lines = ["{"]
            for k, v in value.items():
                lines.append(f"{next_pad}{_format_python_literal(k)}: {_format_python_literal(v, indent + 4)},")
            lines.append(f"{pad}}}")
            return "\n".join(lines)
        if isinstance(value, list):
            if not value:
                return "[]"
            lines = ["["]
            for item in value:
                lines.append(f"{next_pad}{_format_python_literal(item, indent + 4)},")
            lines.append(f"{pad}]")
            return "\n".join(lines)
        if isinstance(value, tuple):
            if not value:
                return "()"
            lines = ["("]
            for item in value:
                lines.append(f"{next_pad}{_format_python_literal(item, indent + 4)},")
            lines.append(f"{pad})")
            return "\n".join(lines)
        return repr(value)

    config_path = Path(output_dir) / "config.py"
    rendered = "CONFIG = " + _format_python_literal(config_entry, indent=0) + "\n"
    config_path.write_text(rendered, encoding="utf-8")
    print("✓ 已保存 config.py")


def _sample_tables_from_generated_files(
    all_tables: Dict[str, List[Dict[str, Any]]],
    output_dir: str,
    max_records_per_table: Optional[int],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    基于已生成的 schema.json + config.py 采样：
    1) 实体表先采样；
    2) 关系表按 schema.json 的 FK 约束过滤后采样，确保引用可落到实体采样结果。
    """
    if max_records_per_table is None:
        return {t: list(rows) for t, rows in all_tables.items()}

    schema_path = Path(output_dir) / "schema.json"
    config_path = Path(output_dir) / "config.py"
    if not schema_path.exists():
        print("⚠ 未找到 schema.json，回退为直接按表采样")
        return {
            table_name: random.sample(rows, max_records_per_table)
            if len(rows) > max_records_per_table else list(rows)
            for table_name, rows in all_tables.items()
        }

    try:
        schema_output = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        schema_output = []

    generated_config: Dict[str, Any] = {}
    if config_path.exists():
        generated_config = _load_config_module(config_path)

    # schema.json 中 type=relation 的表优先；若缺失，则用 config.py 的 anchor_table 兜底。
    relation_tables = {
        str(t.get("name", "")).strip()
        for t in (schema_output if isinstance(schema_output, list) else [])
        if isinstance(t, dict) and str(t.get("type", "")).strip().lower() == "relation"
    }
    if not relation_tables:
        anchor = generated_config.get("anchor_table", [])
        if isinstance(anchor, str):
            relation_tables = {anchor}
        elif isinstance(anchor, list):
            relation_tables = {str(x).strip() for x in anchor if str(x).strip()}

    fk_map: Dict[str, Dict[str, tuple[str, str]]] = {}
    pk_map: Dict[str, List[str]] = {}
    if isinstance(schema_output, list):
        for table_def in schema_output:
            if not isinstance(table_def, dict):
                continue
            table_name = str(table_def.get("name", "")).strip()
            if not table_name:
                continue
            table_fk: Dict[str, tuple[str, str]] = {}
            table_pk: List[str] = []
            for field in table_def.get("fields", []):
                if not isinstance(field, dict):
                    continue
                field_name = str(field.get("name", "")).strip()
                constraints = field.get("constraints", {})
                if not field_name or not isinstance(constraints, dict):
                    continue
                if constraints.get("primary_key") is True:
                    table_pk.append(field_name)
                fk = constraints.get("foreign_key")
                if not isinstance(fk, dict):
                    continue
                ref_table = str(fk.get("table", "")).strip()
                ref_col = str(fk.get("column", "")).strip()
                if ref_table and ref_col:
                    table_fk[field_name] = (ref_table, ref_col)
            if table_fk:
                fk_map[table_name] = table_fk
            pk_map[table_name] = table_pk

    result: Dict[str, List[Dict[str, Any]]] = {}

    def _row_identity(table: str, row: Dict[str, Any]) -> tuple[Any, ...]:
        pks = pk_map.get(table, [])
        if pks:
            return tuple(row.get(pk) for pk in pks)
        # 无主键时退化为整行去重
        return (json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),)

    def _merge_rows(table: str, base_rows: List[Dict[str, Any]], extra_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = list(base_rows)
        seen = {_row_identity(table, r) for r in merged}
        for row in extra_rows:
            key = _row_identity(table, row)
            if key in seen:
                continue
            merged.append(row)
            seen.add(key)
        return merged

    # 第一步：实体表先采样
    for table_name, rows in all_tables.items():
        if table_name in relation_tables:
            continue
        if len(rows) <= max_records_per_table:
            result[table_name] = list(rows)
        else:
            result[table_name] = random.sample(rows, max_records_per_table)

    # 第二步：关系表按 FK 可达性过滤后再采样
    for table_name, rows in all_tables.items():
        if table_name not in relation_tables:
            continue
        table_fk = fk_map.get(table_name, {})
        if not table_fk:
            valid_rows = list(rows)
        else:
            valid_rows = []
            for row in rows:
                row_ok = True
                for src_col, (dst_table, dst_col) in table_fk.items():
                    fk_val = row.get(src_col)
                    if fk_val is None:
                        row_ok = False
                        break
                    dst_rows = result.get(dst_table, all_tables.get(dst_table, []))
                    valid_vals = {r.get(dst_col) for r in dst_rows}
                    if fk_val not in valid_vals:
                        row_ok = False
                        break
                if row_ok:
                    valid_rows.append(row)

            # 兜底策略：
            # 如果“实体先采样 + FK 严格过滤”导致关系表为空，
            # 改为“关系表先采样，再反向级联回补实体表”。
            if not valid_rows and rows:
                print(f"⚠ 关系表 '{table_name}' 严格 FK 过滤后为空，改用关系优先采样并反向级联实体")
                if len(rows) <= max_records_per_table:
                    relation_seed = list(rows)
                else:
                    relation_seed = random.sample(rows, max_records_per_table)

                # 先基于 relation_seed 回补实体表
                for src_col, (dst_table, dst_col) in table_fk.items():
                    need_vals = {
                        r.get(src_col)
                        for r in relation_seed
                        if r.get(src_col) is not None
                    }
                    if not need_vals:
                        continue
                    dst_all_rows = all_tables.get(dst_table, [])
                    matched_rows = [r for r in dst_all_rows if r.get(dst_col) in need_vals]
                    if matched_rows:
                        current_dst = result.get(dst_table, [])
                        result[dst_table] = _merge_rows(dst_table, current_dst, matched_rows)

                # 用回补后的实体集重新校验 relation_seed 的 FK 一致性
                repaired_rows: List[Dict[str, Any]] = []
                for row in relation_seed:
                    row_ok = True
                    for src_col, (dst_table, dst_col) in table_fk.items():
                        fk_val = row.get(src_col)
                        if fk_val is None:
                            row_ok = False
                            break
                        dst_rows = result.get(dst_table, all_tables.get(dst_table, []))
                        valid_vals = {r.get(dst_col) for r in dst_rows}
                        if fk_val not in valid_vals:
                            row_ok = False
                            break
                    if row_ok:
                        repaired_rows.append(row)
                valid_rows = repaired_rows if repaired_rows else relation_seed
        if len(valid_rows) <= max_records_per_table:
            result[table_name] = valid_rows
        else:
            result[table_name] = random.sample(valid_rows, max_records_per_table)

    # 第三步：若 schema/config 中缺失某些表定义，兜底按表采样
    for table_name, rows in all_tables.items():
        if table_name in result:
            continue
        if len(rows) <= max_records_per_table:
            result[table_name] = list(rows)
        else:
            result[table_name] = random.sample(rows, max_records_per_table)
    return result


def extract_table_data(
    cursor: sqlite3.Cursor,
    table_name: str,
    schema: Dict,
    max_records: Optional[int] = None
) -> List[Dict]:
    """提取表数据，max_records 为 None 时读取全部。主键为 NULL 的记录会被过滤。"""
    try:
        cursor.execute(f'SELECT * FROM "{table_name}"')
        rows = cursor.fetchall()
        result = []
        for row in rows:
            record = {}
            for col in schema['columns'][table_name]:
                col_name = col['name']
                if col_name in row.keys():
                    record[col_name] = row[col_name]
            result.append(record)
        pk_cols = schema.get('primary_keys', {}).get(table_name, []) or []
        if pk_cols:
            # 仅过滤 SQL NULL（Python None），不把空字符串当作 NULL。
            result = [
                r for r in result
                if all(r.get(pk) is not None for pk in pk_cols)
            ]

        if max_records is not None and len(result) > max_records:
            result = random.sample(result, max_records)
        return result
    except sqlite3.Error as e:
        print(f"提取表 {table_name} 数据时出错: {e}")
        return []

# def _sample_tables_with_fk_guarantee(
#     all_tables: Dict[str, List[Dict[str, Any]]],
#     schema: Dict[str, Any],
#     max_records: int,
#     anchor_set: set,
# ) -> Dict[str, List[Dict[str, Any]]]:
#     result: Dict[str, List[Dict[str, Any]]] = {}

#     # 第一步：实体表先采样
#     for table_name, rows in all_tables.items():
#         if table_name in anchor_set:
#             continue
#         if len(rows) <= max_records:
#             result[table_name] = list(rows)
#         else:
#             result[table_name] = random.sample(rows, max_records)
#         print(f"  - '{table_name}'（实体）: {len(result[table_name])} 条")

#     # 第二步：关系表按实体表结果过滤，再采样
#     for table_name, rows in all_tables.items():
#         if table_name not in anchor_set:
#             continue
#         fk_map = schema.get("foreign_keys", {}).get(table_name, {})

#         # 过滤：所有外键都能在对应实体表中找到
#         def _row_fk_valid(row: Dict[str, Any]) -> bool:
#             for fk_col, (dst_table, dst_col) in fk_map.items():
#                 fk_val = row.get(fk_col)
#                 if fk_val is None:
#                     return False
#                 dst_rows = result.get(dst_table, all_tables.get(dst_table, []))
#                 valid_vals = {r.get(dst_col) for r in dst_rows}
#                 if fk_val not in valid_vals:
#                     return False
#             return True

#         valid_rows = [r for r in rows if _row_fk_valid(r)]
#         if len(valid_rows) <= max_records:
#             result[table_name] = valid_rows
#         else:
#             result[table_name] = random.sample(valid_rows, max_records)
#         print(f"  - '{table_name}'（关系）: {len(result[table_name])} 条（有效行={len(valid_rows)}）")

#     return result

# def preprocess_database(
#     db_path: str,
#     output_dir: str,
#     db_id: Optional[str] = None,
#     max_records_per_table: Optional[int] = 100
# ) -> Optional[Dict]:
#     if not os.path.exists(db_path):
#         print(f"❌ 错误: 数据库文件不存在: {db_path}")
#         return None

#     if db_id is None:
#         db_id = Path(db_path).stem

#     random.seed(42)
#     conn = sqlite3.connect(db_path)
#     conn.row_factory = sqlite3.Row
#     cursor = conn.cursor()

#     try:
#         schema = extract_schema(cursor, db_id=db_id, db_path=db_path)
#         print(f"✓ Schema 提取完成，共 {len(schema['tables'])} 个表")

#         db_config = load_db_config_entry(db_id, db_path=db_path)
#         table_descriptions = load_table_descriptions(db_path, schema['tables'], db_id=db_id)

#         # 第一步：所有表全量加载
#         all_tables: Dict[str, List[Dict[str, Any]]] = {}
#         for table_name in schema['tables']:
#             print(f"  - 正在提取 '{table_name}'...", end=' ')
#             table_data = extract_table_data(cursor, table_name, schema, max_records=None)
#             all_tables[table_name] = table_data
#             print(f"✓ {len(table_data)} 条记录（全量）")

#         # 第二步：级联补齐（确保关系表外键在实体表中存在）
#         _enforce_anchor_cascade(
#             cursor=cursor,
#             schema=schema,
#             all_tables=all_tables,
#             db_config=db_config,
#         )

#         # 第三步：实体表先采样，关系表按实体表结果过滤后采样
#         if max_records_per_table is not None:
#             anchor_set = set(db_config.get("anchor_table", []) or [])
#             all_tables = _sample_tables_with_fk_guarantee(
#                 all_tables=all_tables,
#                 schema=schema,
#                 max_records=max_records_per_table,
#                 anchor_set=anchor_set,
#             )

#         conn.close()
#         return {
#             'db_id': db_id,
#             'db_path': db_path,
#             'schema': schema,
#             'tables': all_tables,
#             'table_descriptions': table_descriptions,
#             'db_config': db_config,
#         }
#     except Exception as e:
#         print(f"❌ 预处理失败: {e}")
#         import traceback
#         traceback.print_exc()
#         conn.close()
#         return None

def preprocess_database(
    db_path: str,
    output_dir: str,
    db_id: Optional[str] = None,
    max_records_per_table: Optional[int] = 100
) -> Optional[Dict]:
    """
    预处理 SQLite 数据库：提取 schema 和表数据，保存到 output_dir。

    Args:
        db_path: 数据库文件路径
        output_dir: 输出目录
        db_id: 数据库 ID，不指定则从文件名推断
        max_records_per_table: 每表最大记录数，None 表示不限制
    """
    if not os.path.exists(db_path):
        print(f"❌ 错误: 数据库文件不存在: {db_path}")
        return None

    if db_id is None:
        db_id = Path(db_path).stem

    random.seed(42)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        schema = extract_schema(cursor, db_id=db_id, db_path=db_path)
        print(f"✓ Schema 提取完成，共 {len(schema['tables'])} 个表")

        db_config = load_db_config_entry(db_id, db_path=db_path)
        table_descriptions = load_table_descriptions(db_path, schema['tables'], db_id=db_id)

        all_tables: Dict[str, List[Dict[str, Any]]] = {}
        for table_name in schema['tables']:
            print(f"  - 正在提取 '{table_name}'...", end=' ')
            # 先全量提取；采样在 save_preprocessed_data 中基于已生成 config.py/schema.json 完成。
            table_data = extract_table_data(cursor, table_name, schema, max_records=None)
            all_tables[table_name] = table_data
            print(f"✓ {len(table_data)} 条记录（全量）")

        _enforce_anchor_cascade(
            cursor=cursor,
            schema=schema,
            all_tables=all_tables,
            db_config=db_config,
        )

        conn.close()
        return {
            'db_id': db_id,
            'db_path': db_path,
            'schema': schema,
            'tables': all_tables,
            'table_descriptions': table_descriptions,
            'db_config': db_config,
            'max_records_per_table': max_records_per_table,
        }
    except Exception as e:
        print(f"❌ 预处理失败: {e}")
        import traceback
        traceback.print_exc()
        conn.close()
        return None


def save_preprocessed_data(data: Dict, output_dir: str) -> None:
    """保存预处理的数据库数据"""
    os.makedirs(output_dir, exist_ok=True)
    tables_dir = os.path.join(output_dir, 'tables')
    os.makedirs(tables_dir, exist_ok=True)

    table_descriptions = data.get('table_descriptions', {})
    db_id = data.get('db_id', '')
    db_path = data.get('db_path', '')
    existing_config_entry = data.get('db_config', {}) if isinstance(data.get('db_config', {}), dict) else {}
    generated_config_entry = _build_generated_db_config(
        schema=data['schema'],
        db_id=db_id,
        db_path=db_path,
        existing_entry=existing_config_entry,
    ) if db_id and db_path else {}
    anchor_table = generated_config_entry.get("anchor_table", []) if generated_config_entry else []
    schema_output = convert_schema_to_output_format(data['schema'], table_descriptions, anchor_table)
    with open(os.path.join(output_dir, 'schema.json'), 'w', encoding='utf-8') as f:
        json.dump(schema_output, f, indent=2, ensure_ascii=False)
    print(f"✓ 已保存 schema.json")
    if db_id and generated_config_entry:
        _save_generated_config_py(output_dir, generated_config_entry)

    sampled_tables = _sample_tables_from_generated_files(
        all_tables=data['tables'],
        output_dir=output_dir,
        max_records_per_table=data.get('max_records_per_table'),
    )
    for table_name, records in sampled_tables.items():
        table_file = os.path.join(tables_dir, f'{table_name}.json')
        with open(table_file, 'w', encoding='utf-8') as f:
            f.write('[\n')
            for i, record in enumerate(records):
                record_json = json.dumps(record, ensure_ascii=False)
                f.write(f'{record_json},\n' if i < len(records) - 1 else f'{record_json}\n')
            f.write(']\n')
        print(f"  - {table_name}.json ({len(records)} 条记录)")

    stats = {
        'db_id': data['db_id'],
        'num_tables': len(sampled_tables),
        'tables': {
            t: {
                'num_records': len(r),
                'primary_keys': data['schema']['primary_keys'].get(t, []),
                'foreign_keys': data['schema']['foreign_keys'].get(t, {})
            }
            for t, r in sampled_tables.items()
        },
        'total_records': sum(len(r) for r in sampled_tables.values())
    }
    with open(os.path.join(output_dir, 'stats.json'), 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print(f"✓ 已保存 stats.json")
    print(f"\n所有文件已保存到: {output_dir}")


def main():
    # python3 preprocess.py outputs/bird_dev_1471_debit_card_specializing/step1_mvdb/bird_dev_1471_debit_card_specializing_mvdb.sqlite -o outputs/bird_dev_1471_debit_card_specializing/step1_mvdb -n 100
    parser = argparse.ArgumentParser(description="预处理 SQLite 数据库")
    parser.add_argument("db_path", help="数据库文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出目录")
    parser.add_argument("--db-id", help="数据库 ID，不指定则从文件名推断")
    parser.add_argument("-n", "--max-records", type=int, default=100,
                        help="每表最大记录数，0 表示不限制")
    args = parser.parse_args()

    max_records = None if args.max_records == 0 else args.max_records
    data = preprocess_database(
        db_path=args.db_path,
        output_dir=args.output,
        db_id=args.db_id,
        max_records_per_table=max_records
    )
    if data:
        save_preprocessed_data(data, args.output)
    else:
        exit(1)


if __name__ == "__main__":
    main()
