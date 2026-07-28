#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLaMA Factory API 客户端调用示例（函数式重构版）
"""

import requests
import json
from typing import List, Dict, Optional, Union, Generator


# 默认提示词模板
DEFAULT_PROMPT_TEMPLATE = """
Analyze the following sentence and determine which attributes from the list [\"goals\", \"shots\", \"fouls\", \"yellow_cards\", \"red_cards\", \"corner_kicks\", \"free_kicks\", \"offsides\"] are present. Note that goals and saved attempts and blocked attempts and missed attempts are considered shots. Handball and dangerous play are also considered foul. The second yellow card is also considered a red card. Penalty is also considered as free kicks. Please return only a JSON array containing the names of the attributes that appear. If no attributes are present, return an empty array []. Do not include any additional explanatory text.

Input: {input}
"""


def list_models(
    base_url: str = "http://localhost:8088", 
    api_key: Optional[str] = None
) -> Dict:
    """
    列出可用模型
    
    Args:
        base_url: API服务地址
        api_key: API密钥（可选）
        
    Returns:
        包含模型列表的字典
    """
    url = f"{base_url.rstrip('/')}/v1/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"API请求失败: {response.status_code} - {response.text}")
    return response.json()


def chat(
    prompt: Union[str, List[Dict[str, str]]],
    model: str = "ours-ft",
    base_url: str = "http://localhost:8088",
    api_key: Optional[str] = None,
    system_prompt: Optional[str] = None,
    stream: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 512,
    **kwargs
) -> Union[Dict, Generator[str, None, None]]:
    """
    发送聊天请求
    
    Args:
        prompt: 已经处理好的完整提示词（字符串）或消息列表
        model: 模型名称
        base_url: API服务地址
        api_key: API密钥
        system_prompt: 系统提示词
        stream: 是否使用流式输出
        temperature: 采样温度
        max_tokens: 最大生成token数
        **kwargs: 传递给 API 的其他可选参数
        
    Returns:
        如果是非流式，返回响应 JSON 字典；
        如果是流式，返回一个生成器，逐个 yield 生成的文本。
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 处理消息列表
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if isinstance(prompt, str):
        # 直接使用传入的 prompt
        messages.append({"role": "user", "content": prompt})
    else:
        # 如果已经是消息列表，直接追加
        messages.extend(prompt)

    data = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
        **kwargs
    }

    if stream:
        def stream_generator():
            response = requests.post(url, headers=headers, json=data, stream=True)
            if response.status_code != 200:
                raise Exception(f"API流式请求失败: {response.status_code} - {response.text}")
            
            for line in response.iter_lines():
                if line:
                    line_str = line.decode('utf-8')
                    if line_str.startswith('data: '):
                        data_str = line_str[6:]
                        if data_str == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                            if chunk.get('choices') and len(chunk['choices']) > 0:
                                delta = chunk['choices'][0].get('delta', {})
                                content_chunk = delta.get('content', '')
                                if content_chunk:
                                    yield content_chunk
                        except json.JSONDecodeError:
                            pass
        return stream_generator()
    else:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"API请求失败: {response.status_code} - {response.text}")
        return response.json()


if __name__ == "__main__":
    # 使用示例
    try:
        # 1. 列出模型
        print("--- 可用模型 ---")
        models = list_models()
        for m in models.get("data", []):
            print(f"- {m['id']}")
        
        # 2. 简单聊天（手动应用模板）
        print("\n--- 简单聊天 ---")
        test_input = "The game kicks off with the start of the first half. Player29(Away Team) commits a foul. Player2(Home Team) earns a free kick in his own half. Player29(Away Team) misses the header from the center of the box, sending it high and wide to the left after being assisted by Player20(Away Team) with a cross. "
        
        # 在输入函数前先处理好 prompt
        test_prompt = DEFAULT_PROMPT_TEMPLATE.format(input=test_input)
        
        result = chat(
            prompt=test_prompt
        )
        print(f"输出: {result['choices'][0]['message']['content']}")
        
        # 3. 流式聊天
        print("\n--- 流式聊天 ---")
        print("回答: ", end="", flush=True)
        for chunk in chat(prompt="讲个简短的故事", stream=True):
            print(chunk, end="", flush=True)
        print()

        # 4. 带 System Prompt
        print("\n--- 带 System Prompt ---")
        result = chat(
            prompt="你是谁？",
            system_prompt="你是一个名为'小白'的百度AI助手。"
        )
        print(f"输出: {result['choices'][0]['message']['content']}")

    except Exception as e:
        print(f"发生错误: {e}")
