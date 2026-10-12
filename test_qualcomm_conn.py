"""
Local verification script for Qualcomm AI Inference Suite (Cirrascale).
Reads credentials from local .env without printing secrets to terminal or logs.
"""
import os
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

def load_env():
    if not ENV_PATH.exists():
        print("[!] .env file not found. Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)
    env_vars = {}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip().strip("'\"")
    return env_vars

def main():
    env = load_env()
    endpoint = env.get("QUALCOMM_AI_ENDPOINT", "https://aisuite.cirrascale.com/apis/v2").rstrip("/")
    model = env.get("QUALCOMM_AI_MODEL", "Llama-3.1-8B")
    key = env.get("QUALCOMM_AI_KEY", "")

    if not key or key == "your_cirrascale_api_key_here":
        print("[!] Please set your actual QUALCOMM_AI_KEY in .env before testing.")
        sys.exit(1)

    print(f"[*] Testing connection to Cirrascale Qualcomm Cloud AI 100 Suite...")
    print(f"    Endpoint: {endpoint}")
    print(f"    Selected Model: {model}")

    # Step 1: Query models list
    models_url = f"{endpoint}/models"
    req = urllib.request.Request(
        models_url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            print("[+] Authentication SUCCESS! Connected to Cirrascale Qualcomm Cloud AI 100.")
            llm_models = data.get("llm", [])
            print(f"[*] Available LLM Models on Qualcomm Cloud AI 100:")
            for m in llm_models:
                marker = " (active)" if m == model else ""
                print(f"    - {m}{marker}")
    except Exception as e:
        print(f"[-] Could not list models: {e}")

    # Step 2: Test inference call
    print(f"[*] Testing live inference with '{model}'...")
    chat_url = f"{endpoint}/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Respond with: QUALCOMM_CLOUD_AI_100_ONLINE"}],
        "max_tokens": 15
    }).encode()
    chat_req = urllib.request.Request(
        chat_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(chat_req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            reply = data["choices"][0]["message"]["content"].strip()
            print(f"[+] Inference verified! Response from Qualcomm hardware: '{reply}'")
    except Exception as e:
        print(f"[-] Chat completion test error: {e}")

if __name__ == "__main__":
    main()
