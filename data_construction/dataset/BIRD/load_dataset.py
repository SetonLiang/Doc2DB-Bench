import sqlite3
import pandas as pd
import os
import shutil

def read_sqlite(db_path, query):
    """
    读取 sqlite 数据库并执行查询
    :param db_path: sqlite 文件路径，如 'data.db'
    :param query: SQL 查询语句
    :return: 查询结果（list of tuples）
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(query)
    results = cursor.fetchall()
    
    conn.close()
    return results


def read_sqlite_with_columns(db_path, query):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(query)
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    
    conn.close()
    return columns, rows



def read_sqlite_as_dict(db_path, query):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    conn.close()
    return [dict(row) for row in rows]



def read_sqlite_to_df(db_path, query):
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def list_sqlite_tables(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table';"
    )
    tables = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    return tables


def move_folders_by_name(source_path, target_path, folder_names):
    """
    将源路径中指定名称的文件夹复制到目标路径
    
    :param source_path: 源文件夹路径
    :param target_path: 目标文件夹路径
    :param folder_names: 要复制的文件夹名列表
    :return: 成功复制的文件夹列表和失败的文件夹列表
    """
    copied_folders = []
    failed_folders = []
    
    # 确保目标路径存在
    if not os.path.exists(target_path):
        os.makedirs(target_path)
        print(f"创建目标路径: {target_path}")
    
    # 检查源路径是否存在
    if not os.path.exists(source_path):
        print(f"错误: 源路径不存在: {source_path}")
        return copied_folders, failed_folders
    
    # 遍历源路径中的所有项目
    for item in os.listdir(source_path):
        item_path = os.path.join(source_path, item)
        
        # 只处理文件夹
        if os.path.isdir(item_path) and item in folder_names:
            target_item_path = os.path.join(target_path, item)
            
            try:
                # 如果目标位置已存在同名文件夹，可以选择跳过或覆盖
                if os.path.exists(target_item_path):
                    print(f"警告: 目标位置已存在文件夹 '{item}'，跳过复制")
                    failed_folders.append(item)
                    continue
                
                # 复制文件夹
                shutil.copytree(item_path, target_item_path)
                copied_folders.append(item)
                print(f"成功复制文件夹: {item}")
                
            except Exception as e:
                print(f"复制文件夹 '{item}' 时出错: {str(e)}")
                failed_folders.append(item)
    
    print(f"\n复制完成: 成功 {len(copied_folders)} 个，失败 {len(failed_folders)} 个")
    return copied_folders, failed_folders

db_name = "dataset/BIRD/ours/raw/airline/airline.sqlite"
df = list_sqlite_tables(db_name)
print(df)

rows = read_sqlite(db_name, 'SELECT * FROM "Airlines"')
print(rows[0])
print(len(rows))
cols, rows = read_sqlite_with_columns(
    db_name,
    "SELECT * FROM Airlines"
)
print(cols)
# data = read_sqlite_as_dict(
#     "example.db",
#     "SELECT * FROM users"
# )


# source_path = "dataset/BIRD/dev/dev_databases"
# target_path = "dataset/BIRD/ours"

# folder_list = ['financial', 'formula_1', 'cs_semester', 'sales', 'social_media', 'university', 'retails', 'soccer_2016', 'professional_basketball', 'moive', 'regional_sales', 'airline', 'authors', 'beer_factory', 'cars', 'chicago_crime', 'law_episode', 'food_inspection']
# print(len(folder_list))
# copied, failed = move_folders_by_name(source_path, target_path, folder_list)
# print(f"成功复制: {copied}")
# print(f"失败: {failed}")
