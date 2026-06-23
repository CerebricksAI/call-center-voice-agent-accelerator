import os

import httpx
from dotenv import load_dotenv

load_dotenv()

endpoint = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").rstrip("/")
key = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
model = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip()
headers = {"api-key": key, "Content-Type": "application/json"}
payload = {
    "model": model,
    "messages": [
        {
            "role": "user",
            "content": 'Return JSON only: {"insights":[{"key":"test","value":"hello","confidence":0.9}]}',
        }
    ],
    "max_tokens": 80,
    "temperature": 0.1,
    "response_format": {"type": "json_object"},
}
urls = [
    f"{endpoint}/openai/v1/chat/completions",
    f"{endpoint}/openai/deployments/{model}/chat/completions?api-version=2024-10-21",
    f"{endpoint}/models/chat/completions?api-version=2024-05-01-preview",
]
for url in urls:
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=30)
        print("URL:", url.replace(endpoint, ""))
        print("STATUS:", r.status_code)
        print("BODY:", r.text[:220])
        print("---")
    except Exception as exc:
        print("ERR", url, exc)
