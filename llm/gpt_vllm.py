import requests
import time
from openai import OpenAI
from dotenv import dotenv_values
import httpx
import json
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
config = dotenv_values(env_path)

    
def get_answer(text, system_prompt=None, model='gpt-4'):
    output = ''
    api_key = config.get("GPT_KEY")
    base_url = config.get("GPT_URL")
    print(api_key, base_url)
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=httpx.Client(trust_env=False),
    )

    if system_prompt is not None:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            stream=False
        )

    else:
        response = client.chat.completions.create(
            model=model,
            temperature=0.6,
            max_tokens=4096,
            messages=[
                {"role": "user", "content": text},
            ],
            stream=False
        )
    print(response)

    for i in range(3):
        try:
            if hasattr(response, "choices"):
                output = response.choices[0].message.content
            elif isinstance(response, dict):
                output = response["choices"][0]["message"]["content"]
            elif isinstance(response, str):
                parsed = json.loads(response)
                output = parsed["choices"][0]["message"]["content"]
            else:
                raise TypeError(f"Unexpected response type: {type(response).__name__}")

            return output
        except Exception:
            time.sleep(5)

if __name__ == '__main__':
    answer = get_answer("你是什么模型", model="gpt-5.5")
    print(answer)
