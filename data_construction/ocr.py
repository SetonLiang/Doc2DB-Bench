import os
import sys
import argparse
import tempfile
import shutil
import random
import ast
from pathlib import Path
from PIL import Image
import asyncio

# 添加 DeepSeek OCR 路径
DEEPSEEK_OCR_DIR = Path(__file__).parent / "OCR" / "DeepSeek-OCR" / "DeepSeek-OCR-master" / "DeepSeek-OCR-vllm"
sys.path.insert(0, str(DEEPSEEK_OCR_DIR))

# PaddleOCR 路径
PADDLEOCR_DIR = Path(__file__).parent / "OCR"

# 全局模型缓存
_model_cache = {
    'paddle_ocr': {
        'model': None,
        'processor': None,
        'model_path': None,
        'device': None
    },
    'deepseek_ocr': {
        'engine': None,
        'processor': None
    }
}


def _get_paddle_ocr_model(model_path: str = None, task: str = "table"):
    """
    获取或加载 PaddleOCR 模型（带缓存）
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor
    
    if model_path is None:
        model_path = "OCR/models/PaddleOCR-VL"
    
    cache = _model_cache['paddle_ocr']
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    # 如果模型已加载且路径相同，直接返回
    if cache['model'] is not None and cache['model_path'] == model_path and cache['device'] == device:
        return cache['model'], cache['processor'], device
    
    # 加载模型
    print(f"Loading PaddleOCR model from {model_path}...")
    model = AutoModelForCausalLM.from_pretrained(
        model_path, trust_remote_code=True, torch_dtype=torch.bfloat16
    ).to(device).eval()
    processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
    
    # 缓存模型
    cache['model'] = model
    cache['processor'] = processor
    cache['model_path'] = model_path
    cache['device'] = device
    
    print("PaddleOCR model loaded and cached.")
    return model, processor, device


def deepseek_ocr_pdf(
    pdf_path: str,
    output_path: str = None,
    prompt: str = None,
    random_seed: int | None = None,
    pages: list[int] | tuple[int, int] | None = None,
) -> str:
    """
    使用 DeepSeek OCR (run_dpsk_ocr_pdf) 将 PDF 转换为 markdown

    Args:
        pdf_path: 输入 PDF 路径
        output_path: 输出目录或 .mmd 文件路径（可选）
        prompt: OCR prompt（可选）
        random_seed: PDF 抽样随机种子（可选）
        pages: 指定页区间 [start, end)，例如 [0, 5] 表示第 0-4 页（可选）

    Returns:
        markdown 文本内容
    """
    from run_dpsk_ocr_pdf import run_ocr_pdf
    sampled_pdf_path, tmp_pdf_dir, sampled_range = _prepare_pdf_for_ocr(
        pdf_path, max_pages=15, random_seed=random_seed, pages=pages
    )
    try:
        if sampled_range is not None:
            start_page, end_page = sampled_range
            if pages is not None:
                print(
                    f"Using user-specified pages [{pages[0]}, {pages[1]}) -> "
                    f"{start_page}-{end_page} for OCR."
                )
            else:
                print(
                    f"PDF has more than 5 pages, selected consecutive pages with most text: "
                    f"{start_page}-{end_page} for OCR (seed={random_seed})."
                )
        return run_ocr_pdf(pdf_path=sampled_pdf_path, output_path=output_path, prompt=prompt)
    finally:
        if tmp_pdf_dir is not None:
            tmp_pdf_dir.cleanup()


def _prepare_pdf_for_ocr(
    pdf_path: str,
    max_pages: int = 5,
    random_seed: int | None = None,
    pages: list[int] | tuple[int, int] | None = None,
) -> tuple[str, tempfile.TemporaryDirectory | None, tuple[int, int] | None]:
    """
    若 pages 给定，则按 [start, end) 指定页区间抽取；
    否则当 PDF 页数超过 max_pages 时，选择文本总量最多的连续 max_pages 页。

    Returns:
        (pdf_for_ocr_path, temp_dir_handler, sampled_range_1based)
    """
    import importlib

    try:
        pdf_module = importlib.import_module("pypdf")
    except ImportError:
        pdf_module = importlib.import_module("PyPDF2")

    PdfReader = getattr(pdf_module, "PdfReader")
    PdfWriter = getattr(pdf_module, "PdfWriter")

    try:
        reader = PdfReader(pdf_path)
    except Exception as exc:
        # Some encrypted PDFs require optional crypto dependencies (e.g. PyCryptodome).
        # Gracefully fall back to the original PDF so OCR can still proceed.
        print(
            f"Warning: failed to inspect PDF for page sampling ({exc}). "
            "Falling back to full PDF."
        )
        return pdf_path, None, None
    total_pages = len(reader.pages)
    if pages is not None:
        if len(pages) != 2:
            raise ValueError(f"Invalid pages value: {pages}. Expected [start, end).")
        start_idx, end_exclusive = int(pages[0]), int(pages[1])
        if start_idx < 0 or end_exclusive <= start_idx or end_exclusive > total_pages:
            raise ValueError(
                f"Invalid pages range: [{start_idx}, {end_exclusive}), total pages: {total_pages}."
            )
        end_idx = end_exclusive - 1
    else:
        if total_pages <= max_pages:
            return pdf_path, None, None

        text_lengths: list[int] = []
        for page_idx in range(total_pages):
            try:
                page_text = reader.pages[page_idx].extract_text() or ""
            except Exception:
                page_text = ""
            text_lengths.append(len(page_text.strip()))

        best_start_idx = 0
        best_score = -1
        current_score = sum(text_lengths[:max_pages])
        best_score = current_score

        for start_idx in range(1, total_pages - max_pages + 1):
            current_score += text_lengths[start_idx + max_pages - 1] - text_lengths[start_idx - 1]
            if current_score > best_score:
                best_score = current_score
                best_start_idx = start_idx
            elif current_score == best_score and random_seed is not None:
                # 有种子时，平分窗口可复现地随机打破平局。
                tie_rng = random.Random(f"{random_seed}-{start_idx}")
                if tie_rng.random() < 0.5:
                    best_start_idx = start_idx

        start_idx = best_start_idx
        end_idx = start_idx + max_pages - 1
    tmp_dir = tempfile.TemporaryDirectory()
    sampled_pdf_path = str(Path(tmp_dir.name) / "sampled_pages.pdf")

    writer = PdfWriter()
    for page_idx in range(start_idx, end_idx + 1):
        writer.add_page(reader.pages[page_idx])

    with open(sampled_pdf_path, "wb") as f:
        writer.write(f)

    # sampled_range 使用 1-based 页码，便于日志查看
    sampled_range = (start_idx + 1, end_idx + 1)
    return sampled_pdf_path, tmp_dir, sampled_range


def deepseek_ocr(image_path: str, output_path: str = None, prompt: str = None) -> str:
    """
    使用 DeepSeek OCR 将图片转换为 markdown
    
    Args:
        image_path: 输入图片路径
        output_path: 输出 markdown 文件路径（可选）
        prompt: OCR prompt（可选，默认使用文档转换 prompt）
        
    Returns:
        markdown 文本内容
    """
    # 导入 DeepSeek OCR 相关模块（使用 sys.path 添加的路径直接导入）
    import config as deepseek_config
    from run_dpsk_ocr_image import load_image, stream_generate, re_match, process_image_with_refs
    from deepseek_ocr import DeepseekOCRForCausalLM
    from process.image_process import DeepseekOCRProcessor
    import re
    
    # 设置输出路径
    if output_path is None:
        output_dir = tempfile.mkdtemp()
        output_mmd_path = os.path.join(output_dir, "result.mmd")
    else:
        output_dir = os.path.dirname(output_path) if os.path.dirname(output_path) else "."
        output_mmd_path = output_path
        os.makedirs(output_dir, exist_ok=True)
    
    # 临时修改 config 中的 OUTPUT_PATH
    original_output_path = deepseek_config.OUTPUT_PATH
    deepseek_config.OUTPUT_PATH = output_dir
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(f'{output_dir}/images', exist_ok=True)
    
    # 加载图片
    image = load_image(image_path)
    if image is None:
        raise ValueError(f"Failed to load image: {image_path}")
    image = image.convert('RGB')
    
    # 设置 prompt
    if prompt is None:
        default_prompt = deepseek_config.PROMPT
        prompt = default_prompt if '<image>' in default_prompt else '<image>\n<|grounding|>Convert the document to markdown.'
    
    # 缓存 processor（避免每次都创建新实例）
    cache = _model_cache['deepseek_ocr']
    if cache['processor'] is None:
        cache['processor'] = DeepseekOCRProcessor()
    
    processor = cache['processor']
    
    # 处理图片特征
    if '<image>' in prompt:
        image_features = processor.tokenize_with_images(
            images=[image], bos=True, eos=True, cropping=deepseek_config.CROP_MODE
        )
    else:
        image_features = ''
    
    # 运行 OCR
    result_out = asyncio.run(stream_generate(image_features, prompt))
    
    # 保存原始输出
    with open(f'{output_dir}/result_ori.mmd', 'w', encoding='utf-8') as f:
        f.write(result_out)
    
    # 处理引用和图片
    matches_ref, matches_images, matches_other = re_match(result_out)
    
    outputs = result_out
    # 替换图片引用
    for idx, a_match_image in enumerate(matches_images):
        outputs = outputs.replace(a_match_image, f'![](images/' + str(idx) + '.jpg)\n')
    
    # 替换其他标记
    for idx, a_match_other in enumerate(matches_other):
        outputs = outputs.replace(a_match_other, '').replace('\\coloneqq', ':=').replace('\\eqqcolon', '=:')
    
    # 保存最终 markdown
    with open(output_mmd_path, 'w', encoding='utf-8') as f:
        f.write(outputs)
    
    # 恢复原始 OUTPUT_PATH
    deepseek_config.OUTPUT_PATH = original_output_path
    
    # 读取结果
    with open(output_mmd_path, 'r', encoding='utf-8') as f:
        markdown_content = f.read()
    
    # 清理临时目录（如果使用的是临时目录）
    if output_path is None:
        try:
            shutil.rmtree(output_dir)
        except:
            pass
    
    return markdown_content


def paddle_ocr(image_path: str, output_path: str = None, task: str = "table", model_path: str = None) -> str:
    """
    使用 PaddleOCR-VL 将图片转换为 markdown
    
    Args:
        image_path: 输入图片路径
        output_path: 输出 markdown 文件路径（可选）
        task: 任务类型，可选 'ocr', 'table', 'chart', 'formula'，默认 'table'
        model_path: 模型路径（可选）
        
    Returns:
        markdown 文本内容（HTML table 格式）
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor
    
    # 设置默认模型路径
    if model_path is None:
        model_path = "OCR/models/PaddleOCR-VL"
    
    # 设置输出路径
    if output_path is None:
        output_dir = tempfile.mkdtemp()
        output_mmd_path = os.path.join(output_dir, "result.mmd")
    else:
        output_dir = os.path.dirname(output_path) if os.path.dirname(output_path) else "."
        output_mmd_path = output_path
        os.makedirs(output_dir, exist_ok=True)
    
    # 导入 paddleocr 模块
    paddleocr_module_path = PADDLEOCR_DIR / "paddleocr.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("paddleocr_module", paddleocr_module_path)
    paddleocr_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(paddleocr_module)
    parse_to_html_table = paddleocr_module.parse_to_html_table
    PROMPTS = paddleocr_module.PROMPTS
    
    # 加载图片
    image = Image.open(image_path).convert("RGB")
    
    # 使用缓存的模型（如果已加载）
    model, processor, DEVICE = _get_paddle_ocr_model(model_path, task)
    
    # 构建消息
    messages = [
        {"role": "user",         
         "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": PROMPTS[task]},
            ]
        }
    ]
    
    # 处理输入
    inputs = processor.apply_chat_template(
        messages, 
        tokenize=True, 
        add_generation_prompt=True, 	
        return_dict=True,
        return_tensors="pt"
    ).to(DEVICE)
    
    # 生成输出
    outputs = model.generate(**inputs, max_new_tokens=1024)
    outputs = processor.batch_decode(outputs, skip_special_tokens=True)[0]
    
    # 移除 prompt 前缀
    prompt_text = PROMPTS[task]
    if outputs.startswith(prompt_text):
        outputs = outputs[len(prompt_text):].strip()
    
    # 解析为 HTML table（markdown）
    html_table = parse_to_html_table(outputs)
    
    # 保存结果
    with open(output_mmd_path, "w", encoding="utf-8") as f:
        f.write(html_table)
    
    # 读取结果
    with open(output_mmd_path, "r", encoding="utf-8") as f:
        markdown_content = f.read()
    
    # 清理临时目录（如果使用的是临时目录）
    if output_path is None:
        try:
            shutil.rmtree(output_dir)
        except:
            pass
    
    return markdown_content


def ocr_image(image_path: str, ocr_type: str = "deepseek_ocr", output_path: str = None, **kwargs) -> str:
    """
    统一的 OCR 接口，将图片或 PDF 转换为 markdown

    Args:
        image_path: 输入图片路径或 PDF 路径
        ocr_type: OCR 类型，'deepseek_ocr' 或 'paddle_ocr'（PDF 仅支持 deepseek_ocr）
        output_path: 输出 markdown 文件路径（可选）
        **kwargs: 其他参数
            - 对于 deepseek_ocr: prompt (str), random_seed (int), pages ([start, end))
            - 对于 paddle_ocr: task (str), model_path (str)

    Returns:
        markdown 文本内容
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"File not found: {image_path}")

    is_pdf = str(image_path).lower().endswith(".pdf")
    if is_pdf:
        if ocr_type != "deepseek_ocr":
            print("Warning: PDF only supports deepseek_ocr, using run_dpsk_ocr_pdf.")
        prompt = kwargs.get("prompt", None)
        random_seed = kwargs.get("random_seed", None)
        pages = kwargs.get("pages", None)
        return deepseek_ocr_pdf(
            image_path,
            output_path,
            prompt,
            random_seed=random_seed,
            pages=pages,
        )

    if ocr_type == "deepseek_ocr":
        prompt = kwargs.get("prompt", None)
        return deepseek_ocr(image_path, output_path, prompt)
    elif ocr_type == "paddle_ocr":
        task = kwargs.get("task", "table")
        model_path = kwargs.get("model_path", None)
        return paddle_ocr(image_path, output_path, task, model_path)
    else:
        raise ValueError(f"Unknown OCR type: {ocr_type}. Choose 'deepseek_ocr' or 'paddle_ocr'")


def markdown_to_csv(markdown_content: str) -> str:
    """
    将 Markdown 表格转换为 CSV 格式
    
    Args:
        markdown_content: Markdown 表格内容
        
    Returns:
        CSV 格式的字符串
    """
    import csv
    import io
    import re
    
    lines = markdown_content.strip().split('\n')
    csv_rows = []
    
    for line in lines:
        line = line.strip()
        # 跳过空行
        if not line:
            continue
        
        # 跳过分隔线：检查整行是否只包含 |、空格、-、: 字符（markdown 表格分隔线）
        if re.match(r'^[\s\|:\-]+$', line) and '|' in line and ('-' in line or ':' in line):
            continue
        
        # 只处理以 | 开头的行（markdown 表格行）
        if not line.startswith('|'):
            continue
        
        # 提取单元格内容
        # 分割 |，移除首尾空元素
        parts = line.split('|')
        cells = [part.strip() for part in parts[1:-1] if part.strip() or part == '']
        
        # 处理空单元格
        cells = [cell if cell else '' for cell in cells]
        
        if cells:
            csv_rows.append(cells)
    
    if not csv_rows:
        return ""
    
    # 转换为 CSV 字符串
    output = io.StringIO()
    writer = csv.writer(output)
    for row in csv_rows:
        writer.writerow(row)
    
    return output.getvalue()


def html_table_to_csv(html_content: str) -> str:
    """
    将 HTML 表格转换为 CSV 格式（使用 convert_md.py 的函数先转为 markdown）
    
    Args:
        html_content: HTML 表格内容
        
    Returns:
        CSV 格式的字符串
    """
    # 导入 convert_md.py 的函数
    convert_md_path = Path(__file__).parent / "OCR" / "DeepSeek-OCR" / "convert_md.py"
    if not convert_md_path.exists():
        # 尝试另一个可能的路径
        convert_md_path = Path(__file__).parent / "OCR" / "DeepSeek-OCR-master" / "DeepSeek-OCR-vllm" / "convert_md.py"
    
    import importlib.util
    spec = importlib.util.spec_from_file_location("convert_md", convert_md_path)
    convert_md_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(convert_md_module)
    
    # 先转换为 markdown
    markdown_content = convert_md_module.html_table_to_markdown(html_content)
    
    # 再转换为 CSV
    return markdown_to_csv(markdown_content)


def process_dataset(
    jsonl_file: str,
    image_base_dir: str,
    output_base_dir: str,
    ocr_type: str = "deepseek_ocr",
    start_idx: int = None,
    end_idx: int = None,
    **kwargs
):
    """
    批量处理数据集，将 JSONL 中的每个样本的图片转换为 markdown 和 CSV
    
    Args:
        jsonl_file: JSONL 文件路径
        image_base_dir: 图片基础目录（用于查找图片文件）
        output_base_dir: 输出基础目录
        ocr_type: OCR 类型，'deepseek_ocr' 或 'paddle_ocr'
        start_idx: 开始索引（包含），None 表示从开头开始
        end_idx: 结束索引（不包含），None 表示处理到结尾
        **kwargs: 其他 OCR 参数（prompt, task, model_path 等）
        
    输出目录结构:
        output_base_dir/
            {original_data_index}/
                {table_name}.mmd
                {table_name}.csv
                ...
    """
    import json
    import re
    from pathlib import Path
    
    # 创建输出基础目录
    os.makedirs(output_base_dir, exist_ok=True)
    
    # 加载 JSONL 数据
    print(f"Loading JSONL file: {jsonl_file}")
    samples = []
    with open(jsonl_file, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    
    total_samples_count = len(samples)
    print(f"Found {total_samples_count} samples")
    
    # 保存原始的索引值用于显示
    original_start_idx = start_idx
    original_end_idx = end_idx
    
    # 应用索引范围
    if start_idx is not None or end_idx is not None:
        actual_start_idx = start_idx if start_idx is not None else 0
        actual_end_idx = end_idx if end_idx is not None else total_samples_count
        samples = samples[actual_start_idx:actual_end_idx]
        print(f"Processing samples from index {actual_start_idx} to {actual_end_idx} (selected: {len(samples)} samples)")
    else:
        actual_start_idx = 0
        actual_end_idx = total_samples_count
    
    # 建立图片索引（递归查找）
    print(f"Indexing images from: {image_base_dir}")
    image_map = {}
    for root, _, files in os.walk(image_base_dir):
        for file in files:
            if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                image_map[file] = os.path.join(root, file)
    
    print(f"Found {len(image_map)} images")
    
    # 预加载模型（如果使用 PaddleOCR）
    if ocr_type == "paddle_ocr":
        task = kwargs.get("task", "table")
        model_path = kwargs.get("model_path", None)
        print(f"Pre-loading {ocr_type} model...")
        _get_paddle_ocr_model(model_path, task)
        print("Model pre-loaded successfully.")
    elif ocr_type == "deepseek_ocr":
        # DeepSeek OCR 的引擎会在第一次调用时创建，这里只预加载 processor
        print(f"Pre-loading {ocr_type} processor...")
        import config as deepseek_config
        from process.image_process import DeepseekOCRProcessor
        cache = _model_cache['deepseek_ocr']
        if cache['processor'] is None:
            cache['processor'] = DeepseekOCRProcessor()
        print("Processor pre-loaded successfully.")
    
    # 处理每个样本
    total_images = 0
    processed_images = 0
    failed_images = 0
    
    for idx, sample in enumerate(samples):
        original_data_index = sample.get('original_data_index')
        table_image_ids = sample.get('table_image_ids', [])
        table_names = sample.get('table_names', '')
        
        if original_data_index is None:
            print(f"Warning: Sample {idx} has no original_data_index, skipping...")
            continue
        
        # 解析 table_names（可能是字符串格式，如 "['table1', 'table2']"）
        try:
            if isinstance(table_names, str):
                # 尝试解析字符串格式的表名列表
                import ast
                table_names_list = ast.literal_eval(table_names)
            elif isinstance(table_names, list):
                table_names_list = table_names
            else:
                table_names_list = [str(table_names)]
        except:
            # 如果解析失败，使用原始字符串
            table_names_list = [table_names] if table_names else ['table']
        
        # 创建样本输出目录
        sample_output_dir = os.path.join(output_base_dir, str(original_data_index))
        os.makedirs(sample_output_dir, exist_ok=True)
        
        print(f"\nProcessing sample {idx+1}/{len(samples)} (original_data_index: {original_data_index})")
        print(f"  Table names: {table_names_list}")
        print(f"  Found {len(table_image_ids)} images")
        
        # 处理每个图片，使用对应的 table_name
        for img_idx, image_id in enumerate(table_image_ids):
            total_images += 1
            
            # 查找图片路径
            image_path = image_map.get(image_id)
            if not image_path or not os.path.exists(image_path):
                print(f"  Warning: Image not found: {image_id}")
                failed_images += 1
                continue
            
            # 使用 table_name 命名文件（如果图片数多于表名数，使用索引）
            if img_idx < len(table_names_list):
                table_name = table_names_list[img_idx]
            else:
                table_name = table_names_list[-1] if table_names_list else 'table'
            
            # 清理表名（移除特殊字符，用于文件名）
            safe_table_name = re.sub(r'[^\w\s-]', '', str(table_name)).strip().replace(' ', '_')
            if not safe_table_name:
                safe_table_name = f"table_{img_idx}"
            
            # 生成输出文件路径
            mmd_output_path = os.path.join(sample_output_dir, f"{safe_table_name}.mmd")
            csv_output_path = os.path.join(sample_output_dir, f"{safe_table_name}.csv")
            
            # 如果文件已存在，跳过
            if os.path.exists(mmd_output_path) and os.path.exists(csv_output_path):
                print(f"  Skipping {image_id} -> {safe_table_name} (already exists)")
                continue
            
            try:
                print(f"  Processing {image_id} -> {safe_table_name}...")
                # 调用 OCR
                markdown_content = ocr_image(
                    image_path=image_path,
                    ocr_type=ocr_type,
                    output_path=mmd_output_path,
                    **kwargs
                )
                
                # 转换为 CSV
                try:
                    # 如果输出是 HTML 表格格式，先转换为 markdown 再转 CSV
                    if '<table>' in markdown_content:
                        csv_content = html_table_to_csv(markdown_content)
                    else:
                        # 如果已经是 markdown 格式，直接转换
                        csv_content = markdown_to_csv(markdown_content)
                    
                    # 保存 CSV
                    with open(csv_output_path, 'w', encoding='utf-8') as f:
                        f.write(csv_content)
                    print(f"  ✓ Saved MMD to: {mmd_output_path}")
                    print(f"  ✓ Saved CSV to: {csv_output_path}")
                except Exception as e:
                    print(f"  ⚠ Warning: Failed to convert to CSV: {e}")
                    print(f"  ✓ Saved MMD to: {mmd_output_path}")
                
                processed_images += 1
                
            except Exception as e:
                print(f"  ✗ Error processing {image_id}: {e}")
                failed_images += 1
                import traceback
                traceback.print_exc()
                continue
    
    print("\n" + "="*80)
    print("Processing Summary:")
    print("="*80)
    print(f"Total samples in file: {total_samples_count}")
    if original_start_idx is not None or original_end_idx is not None:
        print(f"Processed samples range: [{actual_start_idx}:{actual_end_idx}]")
    print(f"Processed samples: {len(samples)}")
    print(f"Total images: {total_images}")
    print(f"Successfully processed: {processed_images}")
    print(f"Failed: {failed_images}")
    print(f"Output directory: {output_base_dir}")


def _parse_pages_arg(pages_arg: str | None) -> list[int] | None:
    if pages_arg is None:
        return None
    try:
        parsed = ast.literal_eval(pages_arg)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"Invalid --pages value: {pages_arg}. Use format like [0,5].") from exc

    if not isinstance(parsed, (list, tuple)) or len(parsed) != 2:
        raise ValueError(f"Invalid --pages value: {pages_arg}. Use format like [0,5].")

    start, end = parsed
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError(f"Invalid --pages value: {pages_arg}. start/end must be integers.")
    return [start, end]


if __name__ == "__main__":
    # python ocr.py dataset \
    # dataset/Table/MTabVQA/data/MTabVQA-Query/VQA.jsonl \
    # --image_base_dir dataset/Table/MTabVQA/data/MTabVQA-Query/table_images \
    # --output_base_dir ./ocr_results \
    # --ocr_type deepseek_ocr \
    # --task ocr
    # --start_idx 0
    # --end_idx 100


    # python ocr.py single \
    # data_construction/dataset/template/culture/book/1/1.pdf \
    # --output data_construction/dataset/template/culture/book/1/template.mmd \
    # --ocr_type deepseek_ocr \
    # --task ocr 
    # --pages [15,25] 
    
    parser = argparse.ArgumentParser(description="OCR tool to convert images to markdown")
    
    # 添加子命令支持
    subparsers = parser.add_subparsers(dest='mode', help='Operation mode')
    
    # 单图片/PDF 处理模式
    single_parser = subparsers.add_parser('single', help='Process a single image or PDF')
    single_parser.add_argument("image_path", type=str, help="Path to input image or PDF")
    single_parser.add_argument("--ocr_type", type=str, choices=["deepseek_ocr", "paddle_ocr"], 
                               default="deepseek_ocr", help="OCR engine to use")
    single_parser.add_argument("-o", "--output", type=str, default=None, 
                               help="Output markdown file path (optional)")
    single_parser.add_argument("--prompt", type=str, default=None,
                               help="OCR prompt for deepseek_ocr (optional)")
    single_parser.add_argument("--task", type=str, choices=["ocr", "table", "chart", "formula"],
                               default="table", help="Task type for paddle_ocr")
    single_parser.add_argument("--model_path", type=str, default=None,
                               help="Model path for paddle_ocr (optional)")
    single_parser.add_argument("--random_seed", type=int, default=None,
                               help="Random seed for PDF page sampling (deepseek_ocr only)")
    single_parser.add_argument("--pages", type=str, default=None,
                               help="PDF pages range [start,end), e.g. [0,5] (deepseek_ocr only)")
    
    # 数据集处理模式
    dataset_parser = subparsers.add_parser('dataset', help='Process a dataset JSONL file')
    dataset_parser.add_argument("jsonl_file", type=str, help="Path to JSONL file")
    dataset_parser.add_argument("--image_base_dir", type=str, required=True,
                               help="Base directory containing images")
    dataset_parser.add_argument("--output_base_dir", type=str, required=True,
                               help="Base directory for output markdown files")
    dataset_parser.add_argument("--ocr_type", type=str, choices=["deepseek_ocr", "paddle_ocr"], 
                               default="deepseek_ocr", help="OCR engine to use")
    dataset_parser.add_argument("--prompt", type=str, default=None,
                               help="OCR prompt for deepseek_ocr (optional)")
    dataset_parser.add_argument("--task", type=str, choices=["ocr", "table", "chart", "formula"],
                               default="table", help="Task type for paddle_ocr")
    dataset_parser.add_argument("--model_path", type=str, default=None,
                               help="Model path for paddle_ocr (optional)")
    dataset_parser.add_argument("--start_idx", type=int, default=None,
                               help="Start index (inclusive) for processing samples")
    dataset_parser.add_argument("--end_idx", type=int, default=None,
                               help="End index (exclusive) for processing samples")
    dataset_parser.add_argument("--random_seed", type=int, default=None,
                               help="Random seed for PDF page sampling (deepseek_ocr only)")
    dataset_parser.add_argument("--pages", type=str, default=None,
                               help="PDF pages range [start,end), e.g. [0,5] (deepseek_ocr only)")
    
    args = parser.parse_args()
    
    # 如果没有指定模式，默认使用单图片模式（向后兼容）
    if args.mode is None:
        args.mode = 'single'
        # 重新解析，将 image_path 作为位置参数
        parser = argparse.ArgumentParser(description="OCR tool to convert images/PDF to markdown")
        parser.add_argument("image_path", type=str, help="Path to input image or PDF")
        parser.add_argument("--ocr_type", type=str, choices=["deepseek_ocr", "paddle_ocr"], 
                           default="deepseek_ocr", help="OCR engine to use")
        parser.add_argument("-o", "--output", type=str, default=None, 
                           help="Output markdown file path (optional)")
        parser.add_argument("--prompt", type=str, default=None,
                           help="OCR prompt for deepseek_ocr (optional)")
        parser.add_argument("--task", type=str, choices=["ocr", "table", "chart", "formula"],
                           default="table", help="Task type for paddle_ocr")
        parser.add_argument("--model_path", type=str, default=None,
                           help="Model path for paddle_ocr (optional)")
        parser.add_argument("--random_seed", type=int, default=None,
                           help="Random seed for PDF page sampling (deepseek_ocr only)")
        parser.add_argument("--pages", type=str, default=None,
                           help="PDF pages range [start,end), e.g. [0,5] (deepseek_ocr only)")
        args = parser.parse_args()
        args.mode = 'single'
    
    try:
        if args.mode == 'single':
            kwargs = {}
            if args.ocr_type == "deepseek_ocr":
                if args.prompt:
                    kwargs["prompt"] = args.prompt
                if args.random_seed is not None:
                    kwargs["random_seed"] = args.random_seed
                pages = _parse_pages_arg(args.pages)
                if pages is not None:
                    kwargs["pages"] = pages
            elif args.ocr_type == "paddle_ocr":
                kwargs["task"] = args.task
                if args.model_path:
                    kwargs["model_path"] = args.model_path
            
            result = ocr_image(
                args.image_path,
                ocr_type=args.ocr_type,
                output_path=args.output,
                **kwargs
            )
            
            if args.output:
                print(f"Markdown saved to: {args.output}")
            else:
                print("\n" + "="*80)
                print("OCR Result (Markdown):")
                print("="*80)
                print(result)
        
        elif args.mode == 'dataset':
            kwargs = {}
            if args.ocr_type == "deepseek_ocr":
                if args.prompt:
                    kwargs["prompt"] = args.prompt
                if args.random_seed is not None:
                    kwargs["random_seed"] = args.random_seed
                pages = _parse_pages_arg(args.pages)
                if pages is not None:
                    kwargs["pages"] = pages
            elif args.ocr_type == "paddle_ocr":
                kwargs["task"] = args.task
                if args.model_path:
                    kwargs["model_path"] = args.model_path
            
            process_dataset(
                jsonl_file=args.jsonl_file,
                image_base_dir=args.image_base_dir,
                output_base_dir=args.output_base_dir,
                ocr_type=args.ocr_type,
                start_idx=args.start_idx,
                end_idx=args.end_idx,
                **kwargs
            )
            
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
