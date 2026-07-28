# utils.py
from typing import Dict, Any, List

def infer_relation_type_from_schema(
    src_table: str, 
    src_col: str, 
    dst_table: str, 
    dst_col: str, 
    schema: Dict[str, Any]
) -> str:
    """
    底层推断逻辑：根据主外键关系判断是 one2one, one2many, many2many 等。
    """
    src_pks = schema.get("primary_keys", {}).get(src_table, [])
    dst_pks = schema.get("primary_keys", {}).get(dst_table, [])
    
    is_src_pk = src_col in src_pks
    is_dst_pk = dst_col in dst_pks
    
    if is_src_pk and is_dst_pk:
        return "one2one"
    elif not is_src_pk and is_dst_pk:
        return "many2one" # 或者统一叫 one2many
    elif is_src_pk and not is_dst_pk:
        return "one2many"
    else:
        return "many2many"

def infer_fk_relation_types_from_config(
    db_config: Dict[str, Any], 
    schema: Dict[str, Any]
) -> Dict[str, str]:
    """
    根据 config.py 中的 fk_relations 结合 schema 信息，推断每条关系的关系类型。
    """
    relation_types: Dict[str, str] = {}
    
    fk_relations: List[str] = db_config.get("fk_relations", [])
    if not isinstance(fk_relations, list):
        return relation_types
        
    tables = set(schema.get("tables", []))
    columns_map = schema.get("columns", {})
    primary_keys = schema.get("primary_keys", {})
    
    table_lookup = {t.lower(): t for t in tables}
    
    def _norm_table(name: str) -> str:
        return table_lookup.get((name or "").strip().lower(), (name or "").strip())

    for rel_str in fk_relations:
        text = str(rel_str).strip()
        if not text:
            continue
            
        parts = [p.strip() for p in text.replace("→", "->").split("->")]
        if len(parts) != 2:
            continue
            
        src_table, dst_table = _norm_table(parts[0]), _norm_table(parts[1])
        
        if src_table not in tables or dst_table not in tables:
            continue
            
        src_cols = {c["name"] for c in columns_map.get(src_table, [])}
        dst_cols = {c["name"] for c in columns_map.get(dst_table, [])}
        
        candidate_col = ""
        for pk in primary_keys.get(dst_table, []):
            if pk in src_cols and pk in dst_cols:
                candidate_col = pk
                break
                
        if not candidate_col:
            common_cols = sorted(src_cols.intersection(dst_cols))
            if common_cols:
                candidate_col = common_cols[0]
                
        if not candidate_col:
            relation_types[f"{src_table}->{dst_table}"] = "unknown"
            continue
            
        try:
            rel_type = infer_relation_type_from_schema(
                src_table=src_table,
                src_col=candidate_col,
                dst_table=dst_table,
                dst_col=candidate_col,
                schema=schema
            )
            relation_types[f"{src_table}->{dst_table}"] = rel_type
        except Exception as e:
            print(f"Warning: 推断 {src_table}->{dst_table} 时出错: {e}")
            relation_types[f"{src_table}->{dst_table}"] = "error"

    return relation_types