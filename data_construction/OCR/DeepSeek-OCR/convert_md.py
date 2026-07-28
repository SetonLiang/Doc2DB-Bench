#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将HTML表格格式转换为Markdown表格格式
"""

import re
import argparse
from pathlib import Path
from typing import List, Optional


def html_table_to_markdown(html_content: str) -> str:
    """
    将HTML表格转换为Markdown表格
    
    Args:
        html_content: 包含HTML表格的字符串
        
    Returns:
        Markdown格式的表格字符串
    """
    # 提取表格内容（移除可能的其他标记）
    table_match = re.search(r'<table>(.*?)</table>', html_content, re.DOTALL)
    if not table_match:
        # 如果没有找到table标签，尝试直接解析整个内容
        table_content = html_content.strip()
        if not table_content.startswith('<table>'):
            return html_content  # 如果不是表格，直接返回原内容
    else:
        table_content = table_match.group(1)
    
    # 提取所有行
    rows = []
    tr_pattern = r'<tr>(.*?)</tr>'
    tr_matches = re.findall(tr_pattern, table_content, re.DOTALL)
    
    for tr_content in tr_matches:
        # 提取单元格
        td_pattern = r'<td>(.*?)</td>'
        cells = re.findall(td_pattern, tr_content, re.DOTALL)
        # 清理单元格内容（移除HTML实体和多余空白）
        cleaned_cells = [cell.strip().replace('&nbsp;', ' ') for cell in cells]
        rows.append(cleaned_cells)
    
    if not rows:
        return html_content  # 如果没有找到行，返回原内容
    
    # 转换为Markdown表格
    markdown_lines = []
    
    # 表头
    if rows:
        header = rows[0]
        markdown_lines.append('| ' + ' | '.join(header) + ' |')
        # 分隔线
        markdown_lines.append('| ' + ' | '.join(['---'] * len(header)) + ' |')
        # 数据行
        for row in rows[1:]:
            # 确保每行的列数与表头一致
            while len(row) < len(header):
                row.append('')
            markdown_lines.append('| ' + ' | '.join(row[:len(header)]) + ' |')
    
    return '\n'.join(markdown_lines)


def convert_file(input_file: str, output_file: Optional[str] = None) -> None:
    """
    转换文件格式
    
    Args:
        input_file: 输入的.mmd文件路径
        output_file: 输出的.md文件路径（如果为None，则自动生成）
    """
    input_path = Path(input_file)
    
    if not input_path.exists():
        raise FileNotFoundError(f"文件不存在: {input_file}")
    
    # 读取输入文件
    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 转换为Markdown
    markdown_content = html_table_to_markdown(content)
    
    # 确定输出文件路径
    if output_file is None:
        output_path = input_path.with_suffix('.md')
    else:
        output_path = Path(output_file)
    
    # 写入输出文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)
    
    print(f"转换完成: {input_file} -> {output_path}")
    print(f"共 {len(markdown_content.split(chr(10)))} 行")


def main():
    parser = argparse.ArgumentParser(description='将HTML表格格式转换为Markdown表格格式')
    parser.add_argument(
        'input',
        type=str,
        help='输入的.mmd文件路径'
    )
    parser.add_argument(
        '-o', '--output',
        type=str,
        default=None,
        help='输出的.md文件路径（默认：输入文件名.md）'
    )
    
    args = parser.parse_args()
    
    try:
        convert_file(args.input, args.output)
    except Exception as e:
        print(f"错误: {e}")
        return 1
    
    return 0


if __name__ == '__main__':
    exit(main())
