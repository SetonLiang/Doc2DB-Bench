import os
import re
os.environ["CUDA_VISIBLE_DEVICES"] = '4,5'

from PIL import Image
import torch
from transformers import AutoModelForCausalLM, AutoProcessor

# ---- Settings ----
model_path = "OCR/models/PaddleOCR-VL"
image_path = "OCR/DeepSeek-OCR/input/imgs/126olympics_full.png"
task = "ocr" # Options: 'ocr' | 'table' | 'chart' | 'formula'
output_mmd_path = "OCR/output/result.mmd"
# ------------------

def parse_to_html_table(text: str) -> str:
    """
    Parses the specialized PaddleOCR-VL output markers into an HTML table.
    Markers: <fcel> (field cell), <ecel> (empty cell), <nl> (new line)
    """
    lines = text.split('<nl>')
    rows_html = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Extract items using regex to find content between <fcel> or just <ecel>
        # Logic: 
        # 1. Replace <ecel> with <fcel>EMPTY_MARKER to unify splitting
        # 2. Split by <fcel>
        processed_line = line.replace('<ecel>', '<fcel> ') # Use a space for empty
        parts = processed_line.split('<fcel>')
        
        # First part before the first <fcel> is usually empty or noise
        cells = [p.strip() for p in parts if p.strip() or p == ' ']
        # Convert ' ' back to empty string
        cells = ["" if c == ' ' else c for c in cells]
        
        if cells:
            row_html = "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
            rows_html.append(row_html)
            
    return "<table>" + "".join(rows_html) + "</table>"

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

PROMPTS = {
    "ocr": "OCR:",
    "table": "Table Recognition:",
    "formula": "Formula Recognition:",
    "chart": "Chart Recognition:",
}

image = Image.open(image_path).convert("RGB")

model = AutoModelForCausalLM.from_pretrained(
    model_path, trust_remote_code=True, torch_dtype=torch.bfloat16
).to(DEVICE).eval()
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

messages = [
    {"role": "user",         
     "content": [
            {"type": "image", "image": image},
            {"type": "text", "text": PROMPTS[task]},
        ]
    }
]
inputs = processor.apply_chat_template(
    messages, 
    tokenize=True, 
    add_generation_prompt=True, 	
    return_dict=True,
    return_tensors="pt"
).to(DEVICE)

outputs = model.generate(**inputs, max_new_tokens=1024)
outputs = processor.batch_decode(outputs, skip_special_tokens=True)[0]

# Remove the prompt prefix if it exists in the output
prompt_text = PROMPTS[task]
if outputs.startswith(prompt_text):
    outputs = outputs[len(prompt_text):].strip()

print("Original output:", outputs)

# Parse to MMD (HTML Table)
html_table = parse_to_html_table(outputs)

# Save to .mmd file
with open(output_mmd_path, "w", encoding="utf-8") as f:
    f.write(html_table)

print(f"\nParsed MMD Table saved to: {output_mmd_path}")
