import os
import time
from typing import Dict, List, Optional

from dotenv import dotenv_values
from openai import OpenAI

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
config = dotenv_values(env_path)


def _build_messages(text: str, system_prompt: Optional[str] = None) -> List[Dict[str, str]]:
    if system_prompt:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
    return [{"role": "user", "content": text}]


def get_answer(
    text: str,
    system_prompt: Optional[str] = None,
    model: str = "qwen-max",
    temperature: float = 0.0,
    max_tokens: int = 8192,
    top_p: float = 1.0,
    retries: int = 3,
) -> str:
    """
    使用 OpenAI 兼容方式调用 DashScope。

    依赖:
        pip install openai

    .env:
        DASHSCOPE_URL=your_base_url
        DASHSCOPE_API=your_api_key
    """
    base_url = config.get("GPT_URL")
    api_key = config.get("GPT_KEY")

    if not base_url:
        raise ValueError("未在 llm/.env 中找到 GPT_URL")
    if not api_key:
        raise ValueError("未在 llm/.env 中找到 GPT_KEY")

    client = OpenAI(api_key=api_key, base_url=base_url)
    messages = _build_messages(text, system_prompt=system_prompt)
    last_error: Optional[str] = None

    for attempt in range(retries):
        try:
            create_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
            }
            if "qwen3" in str(model).lower():
                create_kwargs["stream"] = False
                create_kwargs["extra_body"] = {"enable_thinking": False}

            response = client.chat.completions.create(**create_kwargs)
            content = response.choices[0].message.content
            if content:
                return content
            last_error = "empty response content"
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries - 1:
                time.sleep(3)

    raise RuntimeError(f"DashScope 调用失败: {last_error or 'empty response'}")


if __name__ == "__main__":
    answer = get_answer("你是什么模型？", model="glm-5.1")
    print(answer)
