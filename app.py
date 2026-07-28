import streamlit as st
import os,re
import sys
import tempfile
import shutil
from pathlib import Path
import json
import html
import markdown

# streamlit run app.py
# 设置页面配置
st.set_page_config(
    page_title="Doc2DB Generator",
    page_icon="📄",
    layout="wide"
)

# -----------------------------------------------------------------------------
# 路径配置与导入
# -----------------------------------------------------------------------------
# 获取当前文件所在目录 (projects/doc2db/)
PROJECT_ROOT = Path(__file__).parent.absolute()

# 添加项目根目录到 sys.path，支持 data_construction.* 包导入
BASELINE_DIR = PROJECT_ROOT
if str(BASELINE_DIR) not in sys.path:
    sys.path.insert(0, str(BASELINE_DIR))

# 添加 data_construction/src 到 sys.path，兼容 baseline 内部的绝对导入：
# from utils import ... / from llm.gpt import ...
DATA_CONSTRUCTION_SRC = PROJECT_ROOT / "data_construction" / "src"
if str(DATA_CONSTRUCTION_SRC) not in sys.path:
    sys.path.insert(0, str(DATA_CONSTRUCTION_SRC))

# 尝试导入所有策略的生成函数
try:
    # 注意：这些文件内部会处理 sys.path 以导入 utils 和 llm
    from data_construction.src.baseline.one_pass import generate_docs, validate_coverage
    from data_construction.src.baseline.rowbyrow import generate_docs_rowbyrow as generate_docs_horizontal
    from data_construction.src.baseline.colbycol import generate_docs_colbycol as generate_docs_vertical
    # 导入 CONFIG 用于处理关联表
    from data_construction.src.config import CONFIG
except ImportError as e:
    st.error(f"Error importing generation functions: {e}")
    st.error(f"Expected path: {BASELINE_DIR}")
    st.stop()

# -----------------------------------------------------------------------------
# 辅助函数
# -----------------------------------------------------------------------------

def _run_generation(db_path, schema_path, output_path, model, strategy, chunk_size=30, max_rows=None, recursive_level=100):
    """执行文档生成的辅助函数"""
    st.subheader("2. Generation Process")
    status_container = st.empty()
    
    try:
        # 重定向 stdout 以捕获 print 输出到 Streamlit 界面
        class StreamlitStdout:
            def __init__(self, container):
                self.container = container
                self.buffer = []
            
            def write(self, text):
                if text.strip():
                    self.buffer.append(text.strip())
                    # 显示最后几行日志
                    display_text = "\n".join(self.buffer[-5:])
                    self.container.text_area("Generation Log", display_text, height=150, disabled=True)
            
            def flush(self):
                pass
        
        original_stdout = sys.stdout
        stdout_capture = StreamlitStdout(status_container)
        sys.stdout = stdout_capture
        
        with st.spinner(f"Analyzing data and generating document using {strategy} strategy... This may take a few minutes."):
            # 根据策略调用相应的生成函数
            if strategy == "One Pass":
                generate_docs(
                    db_path, 
                    schema_path, 
                    output_path, 
                    model=model,
                    max_rows=max_rows,
                    recursive_level=recursive_level
                )
            elif strategy == "Horizontal":
                generate_docs_horizontal(
                    db_path,
                    schema_path,
                    output_path,
                    model=model,
                    chunk_size=chunk_size,
                    recursive_level=recursive_level
                )
            elif strategy == "Vertical":
                generate_docs_vertical(
                    db_path,
                    schema_path,
                    output_path,
                    model=model,
                    recursive_level=recursive_level
                )
            else:
                st.error(f"Unknown strategy: {strategy}")
                return
        
        # 恢复 stdout
        sys.stdout = original_stdout
        status_container.success("Generation Complete!")

        # 显示和下载结果
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                doc_content = f.read()
            
            st.subheader("3. Result")
            
            # 下载按钮
            output_filename = os.path.basename(output_path)
            st.download_button(
                label="📥 Download Markdown Document",
                data=doc_content,
                file_name=output_filename,
                mime="text/markdown"
            )
            
            # 预览
            with st.expander("Document Preview", expanded=True):
                st.markdown(doc_content)
        else:
            st.error("Output file was not created. Check logs for errors.")
            
    except Exception as e:
        sys.stdout = original_stdout if 'original_stdout' in locals() else sys.stdout
        st.error(f"An error occurred during generation: {e}")
        import traceback
        st.code(traceback.format_exc())

# -----------------------------------------------------------------------------
# 辅助函数：数据查看
# -----------------------------------------------------------------------------

def _view_database(db_path):
    """查看数据库内容"""
    if not db_path or not os.path.exists(db_path):
        st.warning("Database path not provided or does not exist.")
        return
    
    st.subheader("📊 Database Content")
    
    if os.path.isfile(db_path) and db_path.endswith(('.sqlite', '.db')):
        # SQLite 数据库
        st.info(f"SQLite Database: `{db_path}`")
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 获取所有表名
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            
            if tables:
                st.write(f"**Found {len(tables)} tables:**")
                table_names = [t[0] for t in tables]
                
                selected_table = st.selectbox("Select a table to view:", table_names)
                
                if selected_table:
                    # 获取表结构
                    cursor.execute(f"PRAGMA table_info({selected_table})")
                    columns = cursor.fetchall()
                    
                    st.write("**Table Schema:**")
                    col_info = []
                    for col in columns:
                        col_info.append(f"- `{col[1]}` ({col[2]})")
                    st.markdown("\n".join(col_info))
                    
                    # 获取数据（限制行数）
                    cursor.execute(f"SELECT * FROM {selected_table} LIMIT 100")
                    rows = cursor.fetchall()
                    
                    if rows:
                        st.write(f"**Data (showing first {len(rows)} rows):**")
                        import pandas as pd
                        df = pd.DataFrame(rows, columns=[col[1] for col in columns])
                        st.dataframe(df, use_container_width=True)
                    else:
                        st.info("Table is empty.")
            
            conn.close()
        except Exception as e:
            st.error(f"Error reading SQLite database: {e}")
    
    elif os.path.isdir(db_path):
        # JSON 文件夹
        st.info(f"JSON Directory: `{db_path}`")
        json_files = [f for f in os.listdir(db_path) if f.endswith('.json')]
        
        if json_files:
            st.write(f"**Found {len(json_files)} JSON files:**")
            selected_file = st.selectbox("Select a file to view:", sorted(json_files))
            
            if selected_file:
                file_path = os.path.join(db_path, selected_file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    st.write(f"**File: `{selected_file}`**")
                    
                    if isinstance(data, list):
                        st.write(f"**Records: {len(data)}**")
                        if len(data) > 0:
                            # 显示前几条记录
                            display_count = min(10, len(data))
                            st.write(f"**Preview (first {display_count} records):**")
                            import pandas as pd
                            df = pd.DataFrame(data[:display_count])
                            st.dataframe(df, use_container_width=True)
                            
                            if len(data) > display_count:
                                st.info(f"... and {len(data) - display_count} more records.")
                    else:
                        st.json(data)
                except Exception as e:
                    st.error(f"Error reading JSON file: {e}")
        else:
            st.warning("No JSON files found in the directory.")

def _view_schema(schema_path):
    """查看 Schema 内容"""
    if not schema_path or not os.path.exists(schema_path):
        st.warning("Schema path not provided or does not exist.")
        return
    
    st.subheader("📋 Schema Structure")
    st.info(f"Schema File: `{schema_path}`")
    
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_data = json.load(f)
        
        # 显示 Schema 信息
        if isinstance(schema_data, dict):
            if 'db_name' in schema_data:
                st.write(f"**Database Name:** {schema_data.get('db_name')}")
            if 'db_description' in schema_data:
                st.write(f"**Description:** {schema_data.get('db_description')}")
            
            tables = schema_data.get('tables', [])
        elif isinstance(schema_data, list):
            tables = schema_data
        else:
            tables = []
        
        if tables:
            st.write(f"**Tables: {len(tables)}**")
            
            selected_table_idx = st.selectbox(
                "Select a table to view details:",
                range(len(tables)),
                format_func=lambda x: tables[x].get('name') or tables[x].get('table_name', f'Table {x+1}')
            )
            
            if selected_table_idx is not None:
                table = tables[selected_table_idx]
                st.write("**Table Details:**")
                st.json(table)
        else:
            st.json(schema_data)
            
    except Exception as e:
        st.error(f"Error reading schema file: {e}")

def _get_db_id_from_path(path):
    """尝试从路径中解析 db_id"""
    norm_path = os.path.normpath(str(path))
    parts = norm_path.split(os.sep)
    for part in parts:
        if part in CONFIG:
            return part
    return None

def _get_primary_keys_from_schema(tables, table_name):
    """从 schema 中获取指定表的 primary key 列名列表"""
    for table in tables:
        t_name = table.get('table_name') or table.get('name')
        if t_name == table_name:
            columns = table.get('columns') or table.get('fields', [])
            primary_keys = []
            for c in columns:
                constraints = c.get('constraints', {})
                if isinstance(constraints, dict) and constraints.get('primary_key', False):
                    primary_keys.append(c.get('name', ''))
            return primary_keys
    return []

def _load_table_data(db_path, table_name, is_sqlite, conn=None):
    """读取特定表的数据"""
    try:
        import pandas as pd
        if is_sqlite:
            return pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn)
        else:
            # JSON 模式
            json_file = os.path.join(db_path, f"{table_name}.json")
            if not os.path.exists(json_file):
                json_file_nested = os.path.join(db_path, "tables", f"{table_name}.json")
                if os.path.exists(json_file_nested):
                    json_file = json_file_nested
            
            if os.path.exists(json_file):
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return pd.DataFrame(data)
            else:
                return None
    except Exception as e:
        print(f"Error reading table {table_name}: {e}")
        return None

def _extract_table_name_from_doc_filename(doc_filename):
    """
    从文档文件名中提取表名
    例如: document_gemini-2.5-pro_RA.md -> RA
          document_gpt-4o_student.md -> student
    """
    if not doc_filename:
        return None
    
    # 移除扩展名
    name_without_ext = os.path.splitext(doc_filename)[0]
    
    # 查找最后一个下划线后的部分（表名）
    # 格式通常是: document_model_tablename 或 document_tablename
    parts = name_without_ext.split('_')
    
    # 如果只有两部分（document_tablename），返回第二部分
    if len(parts) == 2:
        return parts[1]
    # 如果有三部分或更多（document_model_tablename），返回最后一部分
    elif len(parts) >= 3:
        return parts[-1]
    
    return None


def _build_single_table_source_data(db_path, table_name, schema_path=None):
    """
    构建单个表的源数据上下文字符串，用于验证覆盖度
    从database_path读取指定表的JSON文件
    如果是anchor table，会将外键id列替换为对应的anchor_cols（从CONFIG读取）
    
    Args:
        db_path: 数据库路径（文件夹路径，包含JSON文件）
        table_name: 表名（不包含.json扩展名）
        schema_path: Schema文件路径（可选，用于获取主键信息，anchor_cols从CONFIG读取）
    
    Returns:
        源数据字符串
    """
    if not os.path.exists(db_path):
        return None
    
    try:
        import pandas as pd
        
        # 构建JSON文件路径
        json_file = os.path.join(db_path, f"{table_name}.json")
        
        # 如果不在根目录，尝试在tables子目录中查找
        if not os.path.exists(json_file):
            json_file_nested = os.path.join(db_path, "tables", f"{table_name}.json")
            if os.path.exists(json_file_nested):
                json_file = json_file_nested
            else:
                return None
        
        # 读取JSON文件
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if not data:
            return f"Table: {table_name}\nData: (No records)"
        
        # 转换为DataFrame以便格式化
        df = pd.DataFrame(data)
        
        # === 特殊处理 Anchor Table：将外键id列替换为anchor_cols（从CONFIG读取） ===
        # 获取 DB Config（用于处理 anchor_table），直接从CONFIG读取
        db_id = _get_db_id_from_path(db_path) or (schema_path and _get_db_id_from_path(schema_path))
        db_config = CONFIG.get(db_id) if db_id else None
        
        if db_config:
            # 检查是否是anchor table（从CONFIG读取）
            anchor_table_config = db_config.get("anchor_table", [])
            if isinstance(anchor_table_config, str):
                anchor_table_config = [anchor_table_config]
            
            if table_name in anchor_table_config:
                print(f"!!! Processing Anchor Table for validation: {table_name} !!!")
                
                # 获取当前表的主键（如果提供了schema_path）
                current_table_pk = []
                if schema_path and os.path.exists(schema_path):
                    try:
                        with open(schema_path, 'r', encoding='utf-8') as f:
                            schema_data = json.load(f)
                        tables = schema_data if isinstance(schema_data, list) else schema_data.get('tables', [])
                        current_table_pk = _get_primary_keys_from_schema(tables, table_name)
                    except Exception as e:
                        print(f"Warning: Could not read schema for primary keys: {e}")
                
                # 从CONFIG中获取 anchor_cols 配置
                anchor_cols_config = db_config.get("anchor_cols", [])
                anchor_cols_dict = anchor_cols_config[0] if anchor_cols_config and isinstance(anchor_cols_config[0], dict) else {}
                
                # 获取 relation_pairs
                all_relation_pairs = db_config.get("relation_pairs", [])
                relation_pairs = []
                for pair in all_relation_pairs:
                    pair_name = pair.get("name", "")
                    if pair_name.startswith(f"{table_name}_") or pair_name == table_name:
                        relation_pairs.append(pair)
                
                # 处理每个关联表
                for pair in relation_pairs:
                    output_fields = pair.get("output_fields", [])
                    for related_table_name, related_key in output_fields:
                        # 如果相关表就是当前表，跳过
                        if related_table_name == table_name:
                            continue
                        
                        # 检查是否有该关联表的 anchor_cols 配置
                        if related_table_name in anchor_cols_dict:
                            cols_to_merge = anchor_cols_dict[related_table_name]
                            
                            # 加载关联表
                            related_json_file = os.path.join(db_path, f"{related_table_name}.json")
                            if not os.path.exists(related_json_file):
                                related_json_file = os.path.join(db_path, "tables", f"{related_table_name}.json")
                            
                            if os.path.exists(related_json_file):
                                with open(related_json_file, 'r', encoding='utf-8') as f:
                                    related_data = json.load(f)
                                
                                if related_data:
                                    related_df = pd.DataFrame(related_data)
                                    
                                    if related_key in df.columns and related_key in related_df.columns:
                                        # 准备要合并的列：anchor_cols 中指定的列
                                        rename_map = {}
                                        temp_cols = []
                                        
                                        for col_name in cols_to_merge:
                                            if col_name in related_df.columns:
                                                # 构造新名字以防冲突
                                                if related_table_name.lower() not in col_name.lower():
                                                    new_name = f"{related_table_name}_{col_name}"
                                                else:
                                                    new_name = col_name
                                                
                                                # 如果新名字已存在于 df，再加后缀
                                                if new_name in df.columns:
                                                    new_name = f"{new_name}_linked"
                                                
                                                rename_map[col_name] = new_name
                                                temp_cols.append(col_name)
                                        
                                        # 提取并重命名关联表的列
                                        if temp_cols:
                                            temp_related = related_df[[related_key] + temp_cols].rename(columns=rename_map)
                                            
                                            # Merge 关联表的列到当前表
                                            df = df.merge(temp_related, on=related_key, how="left")
                                            
                                            # 删除原来的外键列（id列），因为已经用anchor_cols替换了
                                            # 这是anchor table的特殊处理：将id替换为anchor_cols
                                            # 验证时不需要id列，直接删除外键列
                                            if related_key in df.columns:
                                                df = df.drop(columns=[related_key])
                                                print(f"  -> 已删除外键列 {related_key}，已用anchor_cols替换")
                
                # 删除当前表的主键列（验证时不需要id列）
                if current_table_pk:
                    pk_cols_to_remove = [pk for pk in current_table_pk if pk in df.columns]
                    if pk_cols_to_remove:
                        df = df.drop(columns=pk_cols_to_remove)
                        print(f"  -> 已删除当前表的主键列: {pk_cols_to_remove}")
        
        # 构建上下文
        full_context = []
        full_context.append(f"Table: {table_name}")
        full_context.append(f"Schema: {', '.join(df.columns.tolist())}")
        
        if df.empty:
            full_context.append("Data: (No records)")
        else:
            data_str = df.to_string(index=False)
            full_context.append(f"Data Rows:\n{data_str}")
        
        return "\n".join(full_context)
        
    except Exception as e:
        print(f"Error building single table source data: {e}")
        import traceback
        print(traceback.format_exc())
        return None


def _build_source_data(db_path, schema_path, max_rows=None):
    """
    构建源数据上下文字符串，用于验证覆盖度
    返回源数据字符串
    处理关联表时，会将关联实体表的列合并过来
    注意：包含所有数据，包括主键列
    """
    if not os.path.exists(db_path) or not os.path.exists(schema_path):
        return None
    
    try:
        import sqlite3
        import pandas as pd
        
        # 读取 schema
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_data = json.load(f)
        
        # 判断数据源类型
        is_sqlite = os.path.isfile(db_path) and db_path.endswith(('.sqlite', '.db'))
        
        conn = None
        if is_sqlite:
            conn = sqlite3.connect(db_path)
        
        # 兼容不同的 schema 格式
        if isinstance(schema_data, list):
            tables = schema_data
            db_name = 'Database'
            db_desc = ''
        else:
            db_name = schema_data.get('db_name') or schema_data.get('db_id', 'Database')
            db_desc = schema_data.get('db_description', '')
            tables = schema_data.get('tables', [])
        
        # 获取 DB Config（用于处理 anchor_table）
        db_id = _get_db_id_from_path(db_path) or _get_db_id_from_path(schema_path)
        db_config = CONFIG.get(db_id) if db_id else None
        
        full_context = []
        full_context.append(f"Database Name: {db_name}")
        if db_desc:
            full_context.append(f"Database Description: {db_desc}")
        full_context.append("")
        
        for table in tables:
            table_name = table.get('table_name') or table.get('name')
            if not table_name:
                continue
                
            table_desc = table.get('description', '')
            columns = table.get('columns') or table.get('fields', [])
            
            # 识别主键列（仅用于信息显示，不用于排除）
            primary_key_columns = []
            col_info_parts = []
            for c in columns:
                col_name = c.get('name', '')
                col_type = c.get('type', '')
                col_desc = c.get('description', '').strip()
                
                # 检查是否是主键（仅用于信息记录）
                constraints = c.get('constraints', {})
                if isinstance(constraints, dict) and constraints.get('primary_key', False):
                    primary_key_columns.append(col_name)
                
                # 构建列信息（包括主键列）
                if col_desc:
                    col_info_parts.append(f"{col_name} ({col_type}): {col_desc}")
                else:
                    col_info_parts.append(f"{col_name} ({col_type})")
            
            col_info = ", ".join(col_info_parts)
            
            full_context.append(f"Table: {table_name}")
            if table_desc:
                full_context.append(f"Description: {table_desc}")
            full_context.append(f"Schema: {col_info}")
            
            # 读取数据
            df = None
            try:
                df = _load_table_data(db_path, table_name, is_sqlite, conn)
                
                if df is not None and not df.empty:
                    # === 特殊处理 Anchor Table：合并关联表的列 ===
                    # 检查是否是anchor table（从CONFIG读取）
                    anchor_table_config = db_config.get("anchor_table", []) if db_config else []
                    if isinstance(anchor_table_config, str):
                        anchor_table_config = [anchor_table_config]
                    
                    if db_config and table_name in anchor_table_config:
                        relation_pairs = db_config.get("relation_pairs", [])
                        anchor_cols_config = db_config.get("anchor_cols", [])
                        
                        # 使用 anchor_cols 配置：合并指定的列
                        anchor_cols_dict = anchor_cols_config[0] if isinstance(anchor_cols_config, list) else anchor_cols_config
                        
                        for pair in relation_pairs:
                            output_fields = pair.get("output_fields", [])
                            for related_table_name, related_key in output_fields:
                                # 如果相关表就是当前表，跳过
                                if related_table_name == table_name:
                                    continue
                                
                                # 检查是否有该关联表的 anchor_cols 配置
                                if related_table_name in anchor_cols_dict:
                                    cols_to_merge = anchor_cols_dict[related_table_name]
                                    
                                    # 加载关联表
                                    related_df = _load_table_data(db_path, related_table_name, is_sqlite, conn)
                                    if related_df is not None and not related_df.empty:
                                        if related_key in df.columns and related_key in related_df.columns:
                                            # 准备要合并的列：anchor_cols 中指定的列（不包括外键列）
                                            rename_map = {}
                                            temp_cols = []
                                            
                                            for col_name in cols_to_merge:
                                                if col_name in related_df.columns:
                                                    # 构造新名字以防冲突
                                                    if related_table_name.lower() not in col_name.lower():
                                                        new_name = f"{related_table_name}_{col_name}"
                                                    else:
                                                        new_name = col_name
                                                    
                                                    # 如果新名字已存在于 df，再加后缀
                                                    if new_name in df.columns:
                                                        new_name = f"{new_name}_linked"
                                                    
                                                    rename_map[col_name] = new_name
                                                    temp_cols.append(col_name)
                                            
                                            # 提取并重命名关联表的列（包括外键列用于merge，但之后会删除）
                                            if temp_cols:
                                                temp_related = related_df[[related_key] + temp_cols].rename(columns=rename_map)
                                                
                                                # Merge 关联表的列到当前表
                                                df = df.merge(temp_related, on=related_key, how="left")
                                                
                                                # 验证时不需要id列，删除外键列
                                                if related_key in df.columns:
                                                    df = df.drop(columns=[related_key])
                    
                    # 验证时排除主键列（id列不需要）
                    primary_key_columns_to_remove = [pk for pk in primary_key_columns if pk in df.columns]
                    if primary_key_columns_to_remove:
                        df = df.drop(columns=primary_key_columns_to_remove)
                    
                    if max_rows is not None:
                        df = df.head(max_rows)
                    
                    if df.empty:
                        full_context.append("Data: (No records)")
                    else:
                        data_str = df.to_string(index=False)
                        full_context.append(f"Data Rows:\n{data_str}")
                else:
                    full_context.append("Data: (No records or not found)")
            except Exception as e:
                full_context.append(f"Data: (Error reading: {e})")
                continue
            
            full_context.append("\n" + "-"*30 + "\n")
        
        if conn:
            conn.close()
        
        return "\n".join(full_context)
    except Exception as e:
        # 返回 None，让调用者处理错误
        print(f"Error building source data: {e}")
        import traceback
        print(traceback.format_exc())
        return None
def _display_validation_results(is_covered, missing_entities, source_data):
    """显示验证结果的辅助函数"""
    st.divider()
    st.subheader("📊 Validation Results")
    
    # 显示验证结果
    if is_covered:
        st.success("✅ **验证通过！** 文档完全包含了数据库中的所有实体和属性。")
        st.balloons()
    else:
        st.error(f"❌ **验证未通过！** 文档缺少 {len(missing_entities)} 个实体/记录。")
        
        # 显示缺失的实体
        st.subheader("缺失的实体/记录详情：")
        if missing_entities:
            # 检查是否是错误信息
            if len(missing_entities) == 1 and missing_entities[0].startswith("Validation failed"):
                st.error(f"⚠️ {missing_entities[0]}")
            else:
                # 显示每个缺失的实体
                for i, missing in enumerate(missing_entities, 1):
                    # 尝试解析实体信息，如果是格式化的描述则美化显示
                    if "Table:" in missing or "Missing:" in missing:
                        st.markdown(f"**{i}. {missing}**")
                    else:
                        st.write(f"{i}. {missing}")
        else:
            st.warning("⚠️ 验证未通过，但未找到具体的缺失实体描述。这可能是验证过程中的错误。")
    
    # 显示统计信息
    col_stat1, col_stat2 = st.columns(2)
    with col_stat1:
        st.metric("验证状态", "✅ 完全覆盖" if is_covered else "❌ 未完全覆盖")
    with col_stat2:
        st.metric("缺失实体数量", len(missing_entities) if not is_covered else 0)
    
    # 可展开查看源数据摘要
    with st.expander("📋 View Source Data Summary"):
        source_preview = source_data[:2000] + "..." if len(source_data) > 2000 else source_data
        st.text_area("Source Data Preview", source_preview, height=300, disabled=True)
        st.caption(f"完整源数据长度: {len(source_data):,} 字符")
        
        st.divider()
        
        # 文档预览
        st.subheader("Document Preview")
        
        # 添加搜索功能
        search_term = st.text_input("🔍 Search in document", key="doc_search")
        
        if search_term:
            # 高亮搜索词
            highlighted_content = current_doc.replace(
                search_term, 
                f"<mark>{search_term}</mark>"
            )
            st.markdown(highlighted_content, unsafe_allow_html=True)
        else:
            st.markdown(current_doc)
        
        # 原始文本视图（可折叠）
        with st.expander("📝 View Raw Text"):
            st.text_area("Raw Document Content", current_doc, height=400, disabled=True)


# -----------------------------------------------------------------------------
# UI 布局
# -----------------------------------------------------------------------------

st.title("📄 Doc2DB Document Generator")
st.markdown("Upload your database and schema to generate comprehensive professional documents.")

# --- 侧边栏配置 ---
st.sidebar.header("Configuration")

# 模型选择
model_options = ["gpt-4o", "gemini-2.5-pro", "gpt-4o-mini"]
selected_model = st.sidebar.selectbox("Select Model", model_options, index=0)

# 策略选择
strategy_options = ["One Pass", "Horizontal", "Vertical"]
selected_strategy = st.sidebar.selectbox("Select Strategy", strategy_options, index=0)

# 策略说明（根据选择的策略动态显示）
strategy_descriptions = {
    "One Pass": "Generates the document in a single pass (with iterative refinement if coverage is low).",
    "Horizontal": "Constructing narratives using rows (tuples) as the primary semantic unit.",
    "Vertical": "A strategy based on attribute clustering, where entity attributes are vertically scattered across the document."
}
st.sidebar.info(f"**{selected_strategy} Strategy**:\n{strategy_descriptions.get(selected_strategy, 'No description available.')}")

# 参数设置
st.sidebar.subheader("Parameters")

# 温度设置
temperature = st.sidebar.slider(
    "Temperature",
    min_value=0.0,
    max_value=2.0,
    value=0.7,
    step=0.1,
    help="Controls randomness. Lower = more deterministic, Higher = more creative."
)

# 文档长度设置
doc_length_options = {
    "Short (~1000 words)": 1000,
    "Medium (~3000 words)": 3000,
    "Long (~5000 words)": 5000,
    "Very Long (~10000 words)": 10000
}
selected_length_label = st.sidebar.selectbox(
    "Document Length",
    options=list(doc_length_options.keys()),
    index=1,  # 默认 Medium
    help="Target word count for the generated document."
)
target_word_count = doc_length_options[selected_length_label]

# Recursive Level 设置（所有策略都支持）
recursive_level = st.sidebar.slider(
    "Recursive Level",
    min_value=0,
    max_value=100,
    value=100,
    step=25,
    help="Controls the complexity of language representation (0=Direct Mapping, 25=Descriptive Layering, 50=Logical Coupling, 75=Expert Embedding, 100=Expert Inference)."
)

# 迭代设置（仅对 One Pass 策略有效）
chunk_size = None  # 默认值
if selected_strategy == "One Pass":
    enable_iteration = st.sidebar.checkbox(
        "Enable Iterative Refinement",
        value=True,
        help="If enabled, the system will iteratively refine the document if coverage is incomplete."
    )
    max_iterations = st.sidebar.number_input(
        "Max Iterations",
        min_value=1,
        max_value=10,
        value=3,
        help="Maximum number of refinement iterations (only used if iterative refinement is enabled).",
        disabled=not enable_iteration
    )
    # One Pass 不使用 chunk_size
elif selected_strategy == "Horizontal":
    enable_iteration = False
    max_iterations = 1
    # Horizontal 使用 chunk_size
    chunk_size = st.sidebar.number_input(
        "Chunk Size (Rows)",
        min_value=10,
        max_value=1000,
        value=30,
        step=10,
        help="Maximum number of rows to process per chunk (for Horizontal strategy)."
    )
else:  # Vertical
    enable_iteration = False
    max_iterations = 1
    # Vertical 不使用 chunk_size

# 当前设置摘要
st.sidebar.subheader("Current Settings")
settings_summary = f"""
- **Model**: {selected_model}
- **Strategy**: {selected_strategy}
- **Temperature**: {temperature}
- **Target Length**: {selected_length_label}
- **Recursive Level**: {recursive_level}%
"""
if selected_strategy == "One Pass":
    settings_summary += f"- **Iteration**: {'Enabled' if enable_iteration else 'Disabled'}"
    if enable_iteration:
        settings_summary += f" (Max: {max_iterations})"
elif selected_strategy == "Horizontal":
    settings_summary += f"- **Chunk Size**: {chunk_size if chunk_size is not None else 30} rows"
st.sidebar.markdown(settings_summary)

# --- 创建选项卡 ---
tab1, tab2, tab3 = st.tabs(["📊 View Data", "🚀 Generate Document", "📄 View Document"])

# =============================================================================
# Tab 1: View Data
# =============================================================================
with tab1:
    st.header("📊 View Database and Schema")
    st.markdown("Load and inspect your database and schema files.")
    
    view_method = st.radio(
        "Choose input method:",
        ["File Upload", "Path Input"],
        horizontal=True,
        key="view_method"
    )
    
    col1, col2 = st.columns(2)
    
    if view_method == "File Upload":
        with col1:
            st.markdown("### Database")
            uploaded_db_files_view = st.file_uploader(
                "Upload Database File(s)", 
                type=["sqlite", "db", "json"], 
                accept_multiple_files=True,
                key="db_viewer_uploader"
            )
        
        with col2:
            st.markdown("### Schema")
            uploaded_schema_file_view = st.file_uploader(
                "Upload Schema File", 
                type=["json"], 
                key="schema_viewer_uploader"
            )
        
        if uploaded_db_files_view and uploaded_schema_file_view:
            # 保存到临时文件以便查看
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                
                # 处理 Database
                if any(f.name.endswith(('.sqlite', '.db')) for f in uploaded_db_files_view):
                    db_file = next(f for f in uploaded_db_files_view if f.name.endswith(('.sqlite', '.db')))
                    db_path_view = temp_path / db_file.name
                    with open(db_path_view, "wb") as f:
                        f.write(db_file.getbuffer())
                else:
                    db_path_view = temp_path / "tables"
                    os.makedirs(db_path_view, exist_ok=True)
                    for json_file in uploaded_db_files_view:
                        target = db_path_view / json_file.name
                        with open(target, "wb") as f:
                            f.write(json_file.getbuffer())
                
                # 处理 Schema
                schema_path_view = temp_path / uploaded_schema_file_view.name
                with open(schema_path_view, "wb") as f:
                    f.write(uploaded_schema_file_view.getbuffer())
                
                # 显示内容
                _view_database(str(db_path_view))
                st.divider()
                _view_schema(str(schema_path_view))
    
    else:  # Path Input
        with col1:
            st.markdown("### Database Path")
            db_path_view = st.text_input(
                "Database Path",
                value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/tables",
                key="db_view_path"
            )
        
        with col2:
            st.markdown("### Schema Path")
            schema_path_view = st.text_input(
                "Schema Path",
                value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/schema.json",
                key="schema_view_path"
            )
        
        if db_path_view and schema_path_view:
            if os.path.exists(db_path_view) and os.path.exists(schema_path_view):
                _view_database(db_path_view)
                st.divider()
                _view_schema(schema_path_view)
            else:
                st.warning("Please ensure both paths exist.")

# =============================================================================
# Tab 2: Generate Document
# =============================================================================
with tab2:
    st.header("🚀 Generate Document")
    st.markdown("Generate a comprehensive document from your database and schema.")
    
    # --- 主区域：数据输入方式选择 ---
    st.subheader("1. Input Method")
    input_method = st.radio(
        "Choose input method:",
        ["File Upload", "Path Input"],
        horizontal=True,
        help="File Upload: Upload files directly. Path Input: Enter file/folder paths on the server."
    )

    col1, col2 = st.columns(2)

    if input_method == "File Upload":
        with col1:
            st.markdown("### Database")
            st.markdown("Upload a SQLite file (`.sqlite`, `.db`) OR multiple JSON files (for table data).")
            uploaded_db_files = st.file_uploader(
                "Upload Database File(s)", 
                type=["sqlite", "db", "json"], 
                accept_multiple_files=True,
                key="db_uploader"
            )

        with col2:
            st.markdown("### Schema")
            st.markdown("Upload the Schema JSON file.")
            uploaded_schema_file = st.file_uploader(
                "Upload Schema File", 
                type=["json"], 
                key="schema_uploader"
            )
        
        # 文件上传模式的检查标志
        uploaded_db_files_check = uploaded_db_files is not None and len(uploaded_db_files) > 0
        uploaded_schema_file_check = uploaded_schema_file is not None
    
    else:  # Path Input
        with col1:
            st.markdown("### Database Path")
            st.markdown("Enter path to SQLite file OR folder containing JSON table files.")
            db_path_input = st.text_input(
                "Database Path",
                value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/tables",
                help="Path to SQLite file (.sqlite/.db) or folder containing .json table files"
            )
            
            # 检查路径并列出文件
            if db_path_input:
                if os.path.exists(db_path_input):
                    if os.path.isfile(db_path_input):
                        # 单个文件（SQLite）
                        if db_path_input.endswith(('.sqlite', '.db')):
                            st.success(f"✓ Found SQLite file: {db_path_input}")
                            uploaded_db_files_check = True
                        else:
                            st.error("File is not a SQLite file (.sqlite or .db)")
                            uploaded_db_files_check = False
                    else:
                        # 文件夹 - 列出所有 JSON 文件
                        json_files = [f for f in os.listdir(db_path_input) if f.endswith('.json')]
                        if json_files:
                            st.success(f"✓ Found {len(json_files)} JSON files in folder")
                            with st.expander(f"View {len(json_files)} JSON files"):
                                for f in sorted(json_files):
                                    st.text(f"  • {f}")
                            uploaded_db_files_check = True
                        else:
                            st.warning("No JSON files found in the folder.")
                            uploaded_db_files_check = False
                else:
                    st.error("Path does not exist.")
                    uploaded_db_files_check = False
            else:
                uploaded_db_files_check = False
        
        with col2:
            st.markdown("### Schema Path")
            st.markdown("Enter path to Schema JSON file.")
            schema_path_input = st.text_input(
                "Schema Path",
                value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/schema.json",
                help="Path to schema.json file"
            )
            
            # 检查 schema 路径
            if schema_path_input:
                if os.path.exists(schema_path_input) and os.path.isfile(schema_path_input):
                    if schema_path_input.endswith('.json'):
                        st.success(f"✓ Found schema file: {schema_path_input}")
                        uploaded_schema_file_check = True
                    else:
                        st.error("File is not a JSON file")
                        uploaded_schema_file_check = False
                else:
                    st.error("Schema file does not exist.")
                    uploaded_schema_file_check = False
            else:
                uploaded_schema_file_check = False
        
        # 为路径输入模式设置标志
        uploaded_db_files = db_path_input if uploaded_db_files_check else None
        uploaded_schema_file = schema_path_input if uploaded_schema_file_check else None

    # --- 输出路径设置 ---
    st.subheader("2. Output Path")
    output_path_input = st.text_input(
        "Output Document Path",
        value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/document.md",
        help="Path where the generated document will be saved (e.g., /path/to/document.md)",
        key="output_path_input"
    )
    
    # 验证输出路径
    if output_path_input:
        output_dir = os.path.dirname(output_path_input)
        if output_dir and not os.path.exists(output_dir):
            st.warning(f"Output directory does not exist: {output_dir}. It will be created automatically.")
        elif not output_path_input.endswith(('.md', '.txt', '.markdown')):
            st.warning("Output file should have a .md, .txt, or .markdown extension.")
    else:
        st.info("Please specify an output path for the generated document.")

    # -----------------------------------------------------------------------------
    # 处理逻辑
    # -----------------------------------------------------------------------------

    # 检查输入是否有效
    is_valid_input = False
    if input_method == "File Upload":
        is_valid_input = uploaded_db_files and uploaded_schema_file
    else:  # Path Input
        is_valid_input = uploaded_db_files_check and uploaded_schema_file_check
    
    # 检查输出路径是否有效
    is_output_valid = output_path_input and output_path_input.strip() != ""

    if is_valid_input and is_output_valid:
        generate_btn = st.button("🚀 Generate Document", type="primary", use_container_width=True)
        
        if generate_btn:
            # 确保输出目录存在
            output_dir = os.path.dirname(output_path_input)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            
            # 使用用户指定的输出路径
            output_path = output_path_input
            if input_method == "File Upload":
                # 文件上传模式：创建临时目录来处理文件
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)

                    # --- 1. 处理 Schema 文件 ---
                    schema_path = temp_path / uploaded_schema_file.name
                    with open(schema_path, "wb") as f:
                        f.write(uploaded_schema_file.getbuffer())
                        
                    # --- 2. 处理 Database 文件 ---
                    db_target_path = ""
                    
                    # 检查上传的是 SQLite 还是 JSON
                    is_sqlite = any(f.name.endswith(('.sqlite', '.db')) for f in uploaded_db_files)
                    
                    if is_sqlite:
                        # 如果包含 SQLite 文件，取第一个（通常只上传一个数据库文件）
                        db_file = next(f for f in uploaded_db_files if f.name.endswith(('.sqlite', '.db')))
                        db_target_path = temp_path / db_file.name
                        with open(db_target_path, "wb") as f:
                            f.write(db_file.getbuffer())
                        st.info(f"Using SQLite database: {db_file.name}")
                    else:
                        # 认为是 JSON 模式，将所有 JSON 文件保存到一个子目录
                        db_target_path = temp_path / "tables"
                        os.makedirs(db_target_path, exist_ok=True)
                        
                        for json_file in uploaded_db_files:
                            target = db_target_path / json_file.name
                            with open(target, "wb") as f:
                                f.write(json_file.getbuffer())
                        st.info(f"Using JSON directory with {len(uploaded_db_files)} files.")
                    
                    # 调用生成函数（使用用户指定的输出路径）
                    _run_generation(
                        str(db_target_path), 
                        str(schema_path), 
                        output_path, 
                        selected_model,
                        strategy=selected_strategy,
                        chunk_size=chunk_size if chunk_size is not None else 30,
                        max_rows=None,
                        recursive_level=recursive_level
                    )
            
            else:  # Path Input
                # 路径输入模式：直接使用路径
                db_path = uploaded_db_files  # 这是输入的路径字符串
                schema_path = uploaded_schema_file  # 这是输入的路径字符串
                
                # 调用生成函数（使用用户指定的输出路径）
                _run_generation(
                    db_path, 
                    schema_path, 
                    output_path, 
                    selected_model,
                    strategy=selected_strategy,
                    chunk_size=chunk_size if chunk_size is not None else 30,
                    max_rows=None,
                    recursive_level=recursive_level
                )

    else:
        # 提示信息
        if not is_valid_input and not is_output_valid:
            st.info("👈 Please provide your Database, Schema files, and Output path to start.")
        elif not is_valid_input:
            st.info("👈 Please provide your Database and Schema files to start.")
        elif not is_output_valid:
            st.info("👈 Please specify an output path for the generated document.")
        
        # 示例说明
        with st.expander("ℹ️ How to use"):
            st.markdown("""
            ### File Upload Mode:
            1. **Select Model**: Choose the LLM you want to use from the sidebar.
            2. **Upload Database**:
               - **SQLite**: Upload a single `.sqlite` or `.db` file.
               - **JSON**: Upload multiple `.json` files representing your tables.
            3. **Upload Schema**: Upload the `schema.json` file describing your database structure.
            4. **Generate**: Click the button and wait for the AI to draft your document.
            
            ### Path Input Mode:
            1. **Select Model**: Choose the LLM you want to use from the sidebar.
            2. **Database Path**: Enter the path to:
               - A SQLite file (`.sqlite` or `.db`), OR
               - A folder containing multiple `.json` table files (e.g., `/path/to/tables/`)
            3. **Schema Path**: Enter the path to your `schema.json` file.
            4. **Generate**: Click the button and wait for the AI to draft your document.
            
            **Note**: Path Input mode requires files to be accessible on the server where Streamlit is running.
            """)

# =============================================================================
# Tab 3: View Document
# =============================================================================
with tab3:
    st.header("📄 View Generated Document")
    st.markdown("Load and view an existing generated document.")
    
    # 初始化持久化状态
    if 'persisted_doc' not in st.session_state:
        st.session_state.persisted_doc = None
    if 'persisted_filename' not in st.session_state:
        st.session_state.persisted_filename = None

    doc_input_method = st.radio(
        "Choose input method:",
        ["File Upload", "Path Input"],
        horizontal=True,
        key="doc_view_method"
    )
    
    # --- 加载逻辑：统一写入 session_state ---
    if doc_input_method == "File Upload":
        uploaded_doc_file = st.file_uploader(
            "Upload Document File",
            type=["md", "txt", "markdown"],
            key="doc_uploader"
        )
        if uploaded_doc_file:
            try:
                # 读取并存入 session_state
                content = uploaded_doc_file.read().decode('utf-8')
                st.session_state.persisted_doc = content
                st.session_state.persisted_filename = uploaded_doc_file.name
                st.success(f"✓ Loaded document: {st.session_state.persisted_filename}")
            except Exception as e:
                st.error(f"Error reading document: {e}")
    
    else:  # Path Input
        doc_path_input = st.text_input(
            "Document Path",
            value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/document_gemini-2.5-pro.md",
            help="Path to the generated document file (.md, .txt, .markdown)",
            key="doc_path_input"
        )
        
        if doc_path_input and os.path.exists(doc_path_input):
            target_file = None
            
            # 情况 A: 输入的是文件夹
            if os.path.isdir(doc_path_input):
                text_extensions = ('.md', '.txt', '.markdown', '.mdx')
                all_files = [f for f in os.listdir(doc_path_input) if f.lower().endswith(text_extensions)]
                
                if all_files:
                    selected_file = st.selectbox("Select a file to view:", sorted(all_files), key="file_selector_in_folder")
                    target_file = os.path.join(doc_path_input, selected_file)
                else:
                    st.warning("No document files (.md, .txt) found in this folder.")
            
            # 情况 B: 输入的是单个文件
            elif os.path.isfile(doc_path_input):
                target_file = doc_path_input
            
            # 执行读取并存入 session_state
            if target_file:
                try:
                    with open(target_file, 'r', encoding='utf-8') as f:
                        st.session_state.persisted_doc = f.read()
                    st.session_state.persisted_filename = os.path.basename(target_file)
                except Exception as e:
                    st.error(f"Error reading {target_file}: {e}")
        elif doc_path_input:
            st.error("Path does not exist.")


    # --- 渲染逻辑：使用 session_state 驱动 ---
    if st.session_state.persisted_doc:
        current_doc = st.session_state.persisted_doc
        current_filename = st.session_state.persisted_filename

        # 1. 顶部基础信息
        col_i1, col_i2, col_i3 = st.columns(3)
        col_i1.metric("File", current_filename or "Unknown")
        col_i2.metric("Chars", f"{len(current_doc):,}")
        col_i3.metric("Est. Tokens", f"{len(current_doc) // 4:,}")

        st.divider()

        # --- 2. 自定义工具栏 (Toolbar) ---
        
        # CSS 魔法：这是布局对齐的关键
        st.markdown("""
            <style>
            /* 全局垂直居中，去除列之间的怪异空隙 */
            div[data-testid="column"] { 
                display: flex; 
                flex-direction: column; 
                justify-content: center;
            }
            
            /* 左侧：大标题样式 */
            .viewer-title {
                font-size: 1.5rem !important; /* 更大 */
                font-weight: 700 !important;   /* 更粗 */
                color: #0F1116;
                margin: 0;
                padding: 0;
                line-height: 1.2;
                white-space: nowrap;
            }

            /* 中间：搜索框高度强制对齐 */
            /* Streamlit 的 input 默认有 label 占位，这里强制移除 */
            .stTextInput {
                margin-bottom: 0px;
            }
            .stTextInput > div > div > input {
                height: 2.2rem; /* 标准高度 */
                padding-top: 0.4rem;
                padding-bottom: 0.4rem;
            }

            /* 右侧：紧凑按钮组 */
            /* 让按钮变小、变窄，像图标一样 */
            .stButton button { 
                width: 100%; 
                height: 2.2rem; /* 与搜索框高度一致 */
                padding: 0px !important; 
                border: 1px solid #d6d6d6;
                background-color: #ffffff;
                color: #555;
                font-size: 0.8rem; /* 箭头符号小一点 */
                line-height: 1;
                transition: all 0.2s;
            }
            .stButton button:hover {
                border-color: #ff4b4b;
                color: #ff4b4b;
                background-color: #fff5f5;
            }
            
            /* 计数器胶囊样式 */
            .match-counter {
                font-family: 'SF Mono', Consolas, monospace;
                font-size: 0.85rem;
                color: #444;
                background-color: #f0f2f6;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                text-align: center;
                height: 2.2rem; /* 高度对齐 */
                line-height: 2.1rem; /* 垂直居中文字 */
                white-space: nowrap;
                padding: 0 8px;
            }
            </style>
        """, unsafe_allow_html=True)

        if "search_idx" not in st.session_state:
            st.session_state.search_idx = 0

        # 布局比例优化：
        # 标题(3) | 搜索框(5) | 上(0.4) | 下(0.4) | 计数(1.2)
        # 这里的关键是 gap="small"，让右侧按钮紧贴在一起
        c_title, c_search, c_prev, c_next, c_stat = st.columns([3.5, 3, 0.4, 0.4, 1.2], gap="small")
        
        # A. 左侧：大标题
        with c_title:
            st.markdown('<div class="viewer-title">📄 Document Viewer</div>', unsafe_allow_html=True)
        
        # B. 中间：搜索框
        with c_search:
            # label_visibility="collapsed" 是消除上方空隙的关键
            search_query = st.text_input("Find", placeholder="🔍 Search text...", label_visibility="collapsed", key="v7_search")
        
        # --- 搜索核心逻辑 ---
        raw_html = markdown.markdown(current_doc, extensions=['tables', 'fenced_code', 'toc', 'sane_lists'])
        total_matches = 0
        final_html = raw_html
        
        if search_query:
            try:
                pattern = re.compile(f'(<[^>]+>)|({re.escape(search_query)})', re.IGNORECASE)
                valid_matches = [m for m in pattern.finditer(raw_html) if m.group(2)]
                total_matches = len(valid_matches)
                
                if total_matches > 0:
                    st.session_state.search_idx %= total_matches
                
                state_container = {"counter": 0}
                def replace_callback(match):
                    if match.group(1): return match.group(1)
                    if match.group(2):
                        text = match.group(2)
                        current_idx = state_container["counter"]
                        state_container["counter"] += 1
                        if current_idx == st.session_state.search_idx:
                            return f'<mark id="current-match" style="background-color:#ff9800; color:white; padding:0 2px; border-radius:2px; border:1px solid #e65100;">{text}</mark>'
                        else:
                            return f'<mark style="background-color:#fff59d; color:black; padding:0 2px; border-radius:2px;">{text}</mark>'
                    return match.group(0)

                if total_matches > 0:
                    final_html = pattern.sub(replace_callback, raw_html)
            except Exception as e:
                st.error(f"Regex error: {e}")

        # C. 右侧：紧凑按钮组 (放在计算逻辑之后)
        with c_prev:
            if st.button("▲", disabled=total_matches==0, key="btn_up_v7", help="Previous"):
                st.session_state.search_idx = (st.session_state.search_idx - 1) % total_matches
                st.rerun()
        with c_next:
            if st.button("▼", disabled=total_matches==0, key="btn_down_v7", help="Next"):
                st.session_state.search_idx = (st.session_state.search_idx + 1) % total_matches
                st.rerun()
        
        # D. 右侧：计数器
        with c_stat:
            if total_matches > 0:
                content = f"{st.session_state.search_idx + 1} / {total_matches}"
                color = "#333"
            else:
                content = "0 / 0"
                color = "#aaa"
            st.markdown(f'<div class="match-counter" style="color:{color};">{content}</div>', unsafe_allow_html=True)

        # --- E. 文档容器 ---
        # 增加 border 和上边距，与上方工具栏形成视觉关联
        with st.container(border=True):
            custom_css = """
            <style>
                body { margin: 0; padding: 0; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; color: #24292e; background-color: #fff; }
                .markdown-body {
                    box-sizing: border-box;
                    min-width: 200px;
                    max-width: 100%;
                    margin: 0 auto;
                    padding: 16px; /* 稍微减少内边距 */
                    height: 600px; 
                    overflow-y: auto; 
                    line-height: 1.6;
                    font-size: 15px;
                }
                /* 表格紧凑化 */
                table { border-collapse: collapse; width: 100%; margin-bottom: 12px; }
                table th, table td { padding: 6px 10px; border: 1px solid #e1e4e8; }
                table th { background-color: #f6f8fa; font-weight: 600; }
                /* 代码块 */
                pre { background-color: #f6f8fa; border-radius: 4px; padding: 12px; overflow: auto; margin-bottom: 12px; }
                code { font-family: SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace; font-size: 85%; }
                /* 滚动条 */
                ::-webkit-scrollbar { width: 8px; height: 8px; }
                ::-webkit-scrollbar-thumb { background-color: #c1c1c1; border-radius: 4px; }
                ::-webkit-scrollbar-track { background-color: #f1f1f1; }
            </style>
            """

            html_structure = f"""
            <!DOCTYPE html>
            <html>
            <head>{custom_css}</head>
            <body>
                <div class="markdown-body" id="scroll-container">
                    {final_html}
                </div>
                <script>
                    setTimeout(function() {{
                        const target = document.getElementById("current-match");
                        if (target) {{
                            target.scrollIntoView({{
                                behavior: "smooth",
                                block: "center",
                                inline: "nearest"
                            }});
                        }}
                    }}, 100);
                </script>
            </body>
            </html>
            """
            st.components.v1.html(html_structure, height=610, scrolling=False)

        st.download_button("📥 Download Document", data=current_doc, file_name="doc.md", use_container_width=True)
        
        
        # =====================================================================
        # 验证覆盖度功能
        # =====================================================================
        st.subheader("✅ Coverage Validation")
        st.markdown("Validate if the document fully covers all entities in the source database.")
        
        val_col1, val_col2 = st.columns(2)
        
        with val_col1:
            validation_db_path = st.text_input(
                "Database Path (for validation)",
                value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/tables",
                help="Path to SQLite file (.sqlite/.db) or folder containing .json table files",
                key="validation_db_path"
            )
        
        with val_col2:
            validation_schema_path = st.text_input(
                "Schema Path (for validation)",
                value="/data/liangzhuowen/projects/doc2db/benchmark/data_construction/output/D1_financial/phase_0/schema.json",
                help="Path to schema.json file (required for Full Database mode, optional for Single Table mode to enable anchor table processing)",
                key="validation_schema_path"
            )
        
        # 验证模式选择
        validation_mode = st.radio(
            "Validation Mode",
            options=["Single Table", "Full Database"],
            index=0,
            help="Single Table: Validate only the table mentioned in document filename. Full Database: Validate all tables.",
            key="validation_mode"
        )
        
        # 如果是Single Table模式，提示schema_path的作用
        # if validation_mode == "Single Table":
        #     st.info("💡 **Tip**: Providing Schema Path enables anchor table processing (replacing ID columns with anchor_cols).")
        
        # 验证模型选择
        validation_model = st.selectbox(
            "Validation Model",
            options=["gpt-4o", "gemini-2.5-pro", "gpt-4o-mini"],
            index=0,
            help="Model used for coverage validation (recommended: gpt-4o for accuracy)",
            key="validation_model"
        )
        
        # 如果是Single Table模式，显示提取的表名
        extracted_table_name = None
        if validation_mode == "Single Table":
            extracted_table_name = _extract_table_name_from_doc_filename(current_filename)
            if extracted_table_name:
                st.info(f"📋 Detected table name from filename: **{extracted_table_name}**")
            else:
                st.warning("⚠️ Could not extract table name from document filename. Please use Full Database mode or ensure filename format is: `document_model_tablename.md`")
        
        validate_btn = st.button("🔍 Validate Coverage", type="primary", use_container_width=True)
        
        # 执行验证
        if validate_btn:
            if validation_mode == "Single Table":
                # Single Table模式：只需要数据库路径，从文档文件名提取表名
                if not validation_db_path:
                    st.error("请提供数据库路径以进行验证。")
                elif not os.path.exists(validation_db_path):
                    st.error("数据库路径不存在，请检查路径是否正确。")
                elif not extracted_table_name:
                    st.error("无法从文档文件名中提取表名，请检查文件名格式或使用Full Database模式。")
                else:
                    with st.spinner(f"正在构建表 {extracted_table_name} 的源数据上下文并验证覆盖度...这可能需要几分钟时间。"):
                        try:
                            # 构建单个表的源数据（如果提供了schema_path，会进行anchor table特殊处理）
                            source_data = _build_single_table_source_data(
                                validation_db_path, 
                                extracted_table_name,
                                schema_path=validation_schema_path if validation_schema_path and os.path.exists(validation_schema_path) else None
                            )
                            
                            if source_data is None:
                                st.error(f"构建表 {extracted_table_name} 的源数据失败，请检查数据库路径和表名是否正确。")
                            else:
                                print(f"Source data (single table {extracted_table_name}): {source_data}")
                                # 执行验证
                                is_covered, missing_entities = validate_coverage(
                                    current_doc, 
                                    source_data, 
                                    model=validation_model
                                )
                                # 继续显示结果（下面的代码保持不变）
                                _display_validation_results(is_covered, missing_entities, source_data)
                        except Exception as e:
                            st.error(f"验证过程中发生错误: {e}")
                            import traceback
                            with st.expander("错误详情"):
                                st.code(traceback.format_exc())
            else:
                # Full Database模式：需要数据库路径和Schema路径
                if not validation_db_path or not validation_schema_path:
                    st.error("请提供数据库路径和 Schema 路径以进行验证。")
                elif not os.path.exists(validation_db_path) or not os.path.exists(validation_schema_path):
                    st.error("数据库路径或 Schema 路径不存在，请检查路径是否正确。")
                else:
                    with st.spinner("正在构建源数据上下文并验证覆盖度...这可能需要几分钟时间。"):
                        try:
                            # 构建源数据
                            source_data = _build_source_data(validation_db_path, validation_schema_path)
                            
                            if source_data is None:
                                st.error("构建源数据失败，请检查数据库和 Schema 文件是否正确。")
                            else:
                                print(f"Source data (full database): {source_data}")
                                # 执行验证
                                is_covered, missing_entities = validate_coverage(
                                    current_doc, 
                                    source_data, 
                                    model=validation_model
                                )
                                # 继续显示结果（下面的代码保持不变）
                                _display_validation_results(is_covered, missing_entities, source_data)
                        except Exception as e:
                            st.error(f"验证过程中发生错误: {e}")
                            import traceback
                            with st.expander("错误详情"):
                                st.code(traceback.format_exc())
                        