import time
import base64
from dotenv import dotenv_values
import os
from anthropic import Anthropic

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


def get_answer(text, image=None, system_prompt=None, model='claude-3-5-sonnet-20241022', temperature=0.0, max_tokens=10240, top_p=1.0):
    """
    Get answer from Claude API using Anthropic official SDK.
    
    Args:
        text (str): The text prompt/question
        image (str or list, optional): Path(s) to image file(s) to include
        system_prompt (str, optional): System prompt/instructions
        model (str): Claude model name (default: 'claude-3-5-sonnet-20241022')
        temperature (float): Sampling temperature (0.0-1.0)
        max_tokens (int): Maximum tokens in response
        top_p (float): Nucleus sampling parameter
    
    Returns:
        str: The response text from Claude
    """
    output = ''
    
    # Get API key from environment
    api_key = config.get("GPT_KEY") or config.get("CLAUDE_API_KEY")
    
    if not api_key:
        raise ValueError("GPT_KEY or CLAUDE_API_KEY not found in .env file")
    
    # Get base URL - prioritize ANTHROPIC_URL, fallback to GPT_URL if it's Anthropic-compatible
    # Note: If GPT_URL points to an OpenAI-compatible proxy, it won't work with Anthropic SDK
    base_url = config.get("ANTHROPIC_URL")
    
    # Initialize Anthropic client
    if base_url:
        # Clean up base_url - remove trailing slashes and API path suffixes
        base_url = base_url.rstrip('/')
        # Remove /v1/messages or /v1/chat/completions if present
        if '/v1/' in base_url:
            base_url = base_url.rsplit('/v1/', 1)[0]
        client = Anthropic(api_key=api_key, base_url=base_url)
    else:
        # Use default Anthropic API endpoint: https://api.anthropic.com
        client = Anthropic(api_key=api_key)
    
    # Build messages list
    messages = []
    
    # Handle image input
    if image is not None:
        content = [{'type': 'text', 'text': text}]
        
        if isinstance(image, list):
            for img_path in image:
                base64_image = encode_image(img_path)
                # Claude uses different image format than OpenAI
                content.append({
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': 'image/jpeg',  # Adjust based on image type
                        'data': base64_image
                    }
                })
        else:
            base64_image = encode_image(image)
            content.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/jpeg',
                    'data': base64_image
                }
            })
        
        messages.append({'role': 'user', 'content': content})
    else:
        messages.append({'role': 'user', 'content': text})
    
    # Retry logic (up to 3 times)
    for i in range(3):
        try:
            # Prepare request parameters
            request_params = {
                'model': model,
                'max_tokens': max_tokens,
                'temperature': temperature,
                'top_p': top_p,
                'messages': messages
            }
            
            # Add system prompt if provided
            if system_prompt is not None:
                request_params['system'] = system_prompt
            
            # Make API call
            response = client.messages.create(**request_params)
            
            # Extract text from response
            # Claude returns a list of content blocks, we need to extract text from them
            output = ''
            for content_block in response.content:
                if content_block.type == 'text':
                    output += content_block.text
            
            return output
            
        except Exception as e:
            error_msg = str(e)
            print(f"Error calling Claude API (attempt {i+1}/3): {e}")
            
            # Provide helpful error message for common issues
            if "404" in error_msg or "Invalid URL" in error_msg:
                print("⚠️  Hint: The API endpoint may not support Anthropic API format.")
                print("   If using a proxy server, ensure it supports Anthropic's /v1/messages endpoint.")
                print("   Or set ANTHROPIC_URL in .env to point to an Anthropic-compatible endpoint.")
            
            if i < 2:  # Don't sleep on last attempt
                time.sleep(5)
            else:
                raise
    
    return output


if __name__ == '__main__':
    # Test the function
    answer = get_answer("你好，你是什么模型？", model="claude-sonnet-4-20250514")
    print(answer)
