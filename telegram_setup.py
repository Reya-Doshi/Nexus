#!/usr/bin/env python3
"""
Setup and diagnostic CLI helper for ReRoute Telegram notifications.

Usage:
    python telegram_setup.py --get-chat-id
        Queries Telegram Bot API getUpdates using TELEGRAM_BOT_TOKEN from .env.
        Lists available private chat IDs, usernames, and first/last names.
        Never prints message contents, secrets, or raw auth tokens.
        Explains webhook conflicts if present without deleting them.

    python telegram_setup.py --send-test
        Sends exactly one clearly labelled setup test alert to the configured
        TELEGRAM_CHAT_ID using TELEGRAM_BOT_TOKEN from .env.
        Includes "[SIMULATED NETWORK] Setup Verification Test".
        Exits with nonzero status on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_env() -> None:
    """Loads variables from root .env without overwriting existing environment."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for env_path in candidates:
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip()
                            if k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass


def sanitize_secrets(text: str, token: str) -> str:
    """Redacts bot token and token-like strings from text."""
    if token:
        text = text.replace(token, "[REDACTED_BOT_TOKEN]")
    text = re.sub(r"\d{7,10}:[A-Za-z0-9_-]{30,}", "[REDACTED_BOT_TOKEN]", text)
    return text


def get_webhook_info(token: str) -> Optional[Dict[str, Any]]:
    """Checks if a webhook is currently active for this bot."""
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ReRoute-Setup/1.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", {})
    except Exception:
        pass
    return None


def cmd_get_chat_id(token: str) -> int:
    """Lists private chat IDs from getUpdates."""
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is not configured in environment or .env.", file=sys.stderr)
        return 1

    # Check webhook conflict first
    wh_info = get_webhook_info(token)
    if wh_info and wh_info.get("url"):
        wh_url = wh_info.get("url")
        print("\n[!] Webhook Conflict Detected:", file=sys.stderr)
        print(f"    This bot currently has an active webhook configured: {wh_url}", file=sys.stderr)
        print("    Telegram Bot API getUpdates cannot be used while a webhook is active.", file=sys.stderr)
        print("    If you wish to use getUpdates, you can remove the webhook using Telegram API deleteWebhook.", file=sys.stderr)
        print("    (Note: This tool does not automatically delete your webhook to avoid unintended service interruption).\n", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ReRoute-Setup/1.0"})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"ERROR: Telegram API returned HTTP {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        safe_msg = sanitize_secrets(str(e), token)
        print(f"ERROR: Failed to connect to Telegram Bot API: {safe_msg}", file=sys.stderr)
        return 1

    if not data.get("ok"):
        desc = sanitize_secrets(data.get("description", "Unknown error"), token)
        print(f"ERROR: Telegram API error: {desc}", file=sys.stderr)
        return 1

    updates = data.get("result", [])
    if not updates:
        print("\nNo recent updates found for this bot.")
        print("To find your Chat ID:")
        print("1. Open Telegram and search for your bot username.")
        print("2. Click 'Start' or send any message (e.g. 'hello') to the bot.")
        print("3. Re-run: python telegram_setup.py --get-chat-id\n")
        return 0

    chats: Dict[str, Dict[str, Any]] = {}
    for update in updates:
        msg = update.get("message") or update.get("channel_post") or update.get("my_chat_member", {}).get("chat")
        if isinstance(msg, dict) and "chat" in msg:
            chat = msg["chat"]
            cid = str(chat.get("id"))
            chats[cid] = {
                "id": cid,
                "type": chat.get("type", "unknown"),
                "username": chat.get("username", "N/A"),
                "first_name": chat.get("first_name", ""),
                "last_name": chat.get("last_name", ""),
                "title": chat.get("title", ""),
            }
        elif isinstance(msg, dict) and "id" in msg:
            cid = str(msg.get("id"))
            chats[cid] = {
                "id": cid,
                "type": msg.get("type", "unknown"),
                "username": msg.get("username", "N/A"),
                "first_name": msg.get("first_name", ""),
                "last_name": msg.get("last_name", ""),
                "title": msg.get("title", ""),
            }

    print("\nAvailable Telegram Chats Found:")
    print("-" * 65)
    print(f"{'Chat ID':<15} | {'Type':<10} | {'Username':<18} | {'Name / Title'}")
    print("-" * 65)
    for c in chats.values():
        name = f"{c['first_name']} {c['last_name']}".strip() or c["title"] or "N/A"
        print(f"{c['id']:<15} | {c['type']:<10} | {c['username']:<18} | {name}")
    print("-" * 65)
    print("\nCopy your Chat ID and add it to your .env file:")
    print("TELEGRAM_CHAT_ID=<your_chat_id>\n")
    return 0


def cmd_send_test(token: str, chat_id: str) -> int:
    """Sends exactly one setup test alert to the configured chat."""
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is not configured.", file=sys.stderr)
        return 1
    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID is not configured.", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    test_message = (
        "🛠️ [SIMULATED NETWORK] Setup Verification Test\n"
        "Status: OK\n"
        "Notification System: ReRoute Autonomous Self-Healing Engine\n"
        "Integration: One-way Telegram Alerts Active"
    )

    payload = json.dumps({
        "chat_id": chat_id,
        "text": test_message,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "ReRoute-Setup/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                print(f"[✓] Setup verification test message sent successfully to Chat ID: {chat_id}")
                return 0
            else:
                desc = sanitize_secrets(data.get("description", "Failed to deliver message"), token)
                print(f"ERROR: Telegram API returned: {desc}", file=sys.stderr)
                return 1
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        safe_msg = sanitize_secrets(str(e), token)
        print(f"ERROR: Failed to send test alert: {safe_msg}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="ReRoute Telegram Notification Setup & Diagnostic Tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--get-chat-id", action="store_true", help="List available Telegram Chat IDs via getUpdates")
    group.add_argument("--send-test", action="store_true", help="Send a single setup test notification to configured chat")

    args = parser.parse_args()
    load_env()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if args.get_chat_id:
        sys.exit(cmd_get_chat_id(token))
    elif args.send_test:
        sys.exit(cmd_send_test(token, chat_id))


if __name__ == "__main__":
    main()
