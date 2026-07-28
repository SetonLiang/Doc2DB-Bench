#!/usr/bin/env python3
"""
清理 markdown 文件中的 <> 标识（如 <r6_c2>, </r6_c2> 等），输出到新文件夹。
"""

import argparse
import re
from pathlib import Path


def remove_tags(text: str) -> str:
    """移除文本中类似 <r6_c2>, </r2,7_c2> 的标签。"""
    # 匹配 <r数字_c数字> 或 <r数字,数字,..._c数字> 或 </...> 格式的标签
    # 支持单行 r6_c2 和多行 r2,7_c2
    pattern = r'</?r[\d,]+_c\d+>'
    return re.sub(pattern, '', text)


def clean_markdown_file(input_path: Path, output_path: Path) -> None:
    """清理单个 markdown 文件并写入输出路径。"""
    content = input_path.read_text(encoding='utf-8')
    cleaned = remove_tags(content)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(cleaned, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='移除 markdown 文件中的 <> 标识')
    parser.add_argument(
        'input_dir',
        type=Path,
        nargs='?',
        default=Path(__file__).parent / 'dataset/BIRD/ours/processed/shipping/docs',
        help='输入文件夹路径（包含 markdown 文件）',
    )
    parser.add_argument(
        '-o', '--output',
        type=Path,
        default=None,
        help='输出文件夹路径（默认为 input_dir 同级下的 docs_clean）',
    )
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output.resolve() if args.output else input_dir.parent / 'docs_clean'

    if not input_dir.exists():
        print(f'错误: 输入文件夹不存在: {input_dir}')
        return 1

    md_files = list(input_dir.glob('*.md'))
    if not md_files:
        print(f'警告: 在 {input_dir} 中未找到 .md 文件')

    for md_file in md_files:
        output_path = output_dir / md_file.name
        clean_markdown_file(md_file, output_path)
        print(f'已处理: {md_file.name} -> {output_path}')

    print(f'\n完成，共处理 {len(md_files)} 个文件，输出到: {output_dir}')
    return 0


if __name__ == '__main__':
    exit(main())
