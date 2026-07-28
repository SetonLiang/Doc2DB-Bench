import requests
import time
import base64
from dotenv import dotenv_values
import os
from openai import OpenAI

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
config = dotenv_values(env_path)


def encode_image(image_path: str) -> str:
    """
    Encodes the image at the given path to a base64-encoded string.

    Args:
        image_path (str): The path to the image file.

    Returns:
        str: The base64-encoded image string.
    """
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    return base64_image

def get_answer(text, image=None, system_prompt=None, model='gpt-4o', temperature=0.0, max_tokens=8192, top_p=1.0):

    output = ''

    # api_url = config.get("DEEPWISDOM_URL")
    # api_key = config.get("DEEPWISDOM_KEY")
    api_url = config.get("GPT_URL_PRO")
    api_key = config.get("GPT_KEY_PRO")
    # api_url = config.get("GPT_URL_USTGZ")
    # api_key = config.get("GPT_KEY_USTGZ")

    if api_url and not api_url.endswith('/chat/completions'):
        api_url = api_url.rstrip('/') + '/chat/completions'

    print(api_url, api_key)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # 构建基础数据字典
    base_data = {
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'top_p': top_p
    }
    # Qwen3 等非流式调用：部分网关要求显式关闭 thinking，否则会 400
    if 'qwen3' in str(model).lower():
        base_data['enable_thinking'] = False

    if image is not None:
        content = [{'type': 'text','text': text}]
        if isinstance(image, list):
            for img_path in image:
                base64_image = encode_image(img_path)
                content.append({'type': 'image_url','image_url': {'url': f"data:image/jpeg;base64,{base64_image}"}})
        else:
            base64_image = encode_image(image)
            content.append({'type': 'image_url','image_url': {'url': f"data:image/jpeg;base64,{base64_image}"}})

        if system_prompt is not None:
            data = {
                **base_data,
                'messages': [
                    {"role": "system", "content": system_prompt},
                    {'role': 'user', 'content': content}
                ]
            }
        else:
            data = {
                **base_data,
                'messages': [
                    {'role': 'user', 'content': content}
                ]
            }
    else:
        if system_prompt is not None:
            data = {
                **base_data,
                'messages': [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ]
            }
        else:
            data = {
                **base_data,
                'messages': [{"role": "user", "content": text}]
            }
    # print(data)
    for i in range(3):
        try:
            response = requests.post(api_url, headers=headers, json=data)
            response = response.json()
            print(response)
            output = response['choices'][0]['message']['content']
            
            return output
        except:
            time.sleep(5)

    return output

if __name__ == '__main__':
    answer = get_answer("你是什么模型", model="gpt-5.4")
    print(answer)