"""
One-way Telegram incident notification worker for ReRoute.
Segregated from simulation execution:
- Sends alerts only for Detected, verified Recovered, and Unresolved transitions.
- Every message includes "SIMULATED NETWORK".
- Non-blocking: bounded queue with a single daemon worker thread.
- Thread-safe deduplication by run ID + incident ID + event type.
- Discards queued stale alerts upon reset / epoch increment.
- Request timeouts and bounded error handling (respects 429 retry_after).
- Does not retry ambiguous timeouts to avoid duplicating alerts.
- Never logs or exposes bot tokens, URLs containing tokens, or raw exceptions containing secrets.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("reroute.telegram")


@dataclass
class IncidentAlertPayload:
    run_id: str
    incident_id: str
    event_type: str  # "DETECTED", "RECOVERED", "UNRESOLVED"
    epoch: int
    timestamp: float
    affected_component: Optional[str] = None
    evidence: Optional[List[str]] = None
    new_route: Optional[List[str]] = None
    recovery_duration_s: Optional[float] = None
    likely_cause: Optional[str] = None
    details: Optional[str] = None


class TelegramNotifier:
    """
    Background worker that transmits plain text incident notifications to Telegram.
    Always safely handles disabled states and network failures.
    """
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        api_base: str = "https://api.telegram.org",
        timeout_seconds: float = 4.0,
        max_queue_size: int = 50,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = timeout_seconds

        # Server-side environment lookup if not explicitly provided
        self._load_env_if_needed()

        env_enabled = os.environ.get("TELEGRAM_ENABLED", "").strip().lower() in ("1", "true", "yes")
        self.enabled = enabled if enabled is not None else env_enabled
        self.bot_token = (bot_token if bot_token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()

        self._queue: queue.Queue[Optional[IncidentAlertPayload]] = queue.Queue(maxsize=max_queue_size)
        self._sent_keys: Set[Tuple[str, str, str]] = set()
        self._lock = threading.Lock()
        self.current_epoch: int = 0
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._rate_limit_until: float = 0.0

        if self.enabled and self.bot_token and self.chat_id:
            self._start_worker()

    @staticmethod
    def _load_env_if_needed() -> None:
        """Loads root .env if variables not yet in os.environ."""
        needed = ["TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
        if any(k not in os.environ for k in needed):
            for candidate in [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env", Path(__file__).resolve().parent / ".env"]:
                if candidate.exists():
                    try:
                        with open(candidate, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k, v = line.split("=", 1)
                                    k, v = k.strip(), v.strip()
                                    if k not in os.environ:
                                        os.environ[k] = v
                    except Exception:
                        pass

    def _start_worker(self) -> None:
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop_event.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="TelegramNotifierWorker")
            self._worker_thread.start()

    def reset(self) -> None:
        """
        Advances epoch, discards queued alerts from previous runs, and clears deduplication history.
        Non-blocking and thread-safe.
        """
        with self._lock:
            self.current_epoch += 1
            self._rate_limit_until = 0.0
            self._sent_keys.clear()
            # Drain queue
            try:
                while True:
                    self._queue.get_nowait()
                    self._queue.task_done()
            except queue.Empty:
                pass

    def notify_incident(self, payload: IncidentAlertPayload) -> bool:
        """
        Enqueues an alert for asynchronous transmission.
        Returns True if enqueued, False if disabled, duplicate, or stale.
        Never blocks simulation execution.
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return False

        # Only notify allowed events
        if payload.event_type not in ("DETECTED", "RECOVERED", "UNRESOLVED"):
            return False

        with self._lock:
            if payload.epoch != self.current_epoch:
                return False

            dedup_key = (payload.run_id, payload.incident_id, payload.event_type)
            if dedup_key in self._sent_keys:
                return False

            self._sent_keys.add(dedup_key)

            # Ensure background thread is running
            self._start_worker()

            try:
                self._queue.put_nowait(payload)
                return True
            except queue.Full:
                logger.warning("Telegram alert queue is full; dropping notification")
                return False

    def _worker_loop(self) -> None:
        """Daemon worker loop pulling from bounded queue with interruptible rate limit cooldown."""
        while not self._stop_event.is_set():
            # Check rate-limit cooldown before dequeueing
            remaining_cooldown = 0.0
            with self._lock:
                now = time.time()
                if self._rate_limit_until > now:
                    remaining_cooldown = self._rate_limit_until - now

            if remaining_cooldown > 0:
                self._stop_event.wait(timeout=remaining_cooldown)
                if self._stop_event.is_set():
                    break

            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                self._queue.task_done()
                break

            with self._lock:
                if item.epoch != self.current_epoch:
                    self._queue.task_done()
                    continue

            self._send_payload(item)
            self._queue.task_done()

    def _format_message(self, p: IncidentAlertPayload) -> str:
        """Formats the plain text alert strictly following the format requirements."""
        lines = [
            "🚨 [SIMULATED NETWORK] Incident Alert",
            f"Event: {p.event_type}",
            f"Run ID: {p.run_id}",
            f"Incident ID: {p.incident_id}",
            f"Timestamp: {p.timestamp:.2f}s (simulated)",
        ]
        if p.affected_component:
            lines.append(f"Affected Component: {p.affected_component}")
        if p.likely_cause:
            lines.append(f"Likely Cause: {p.likely_cause}")
        if p.evidence:
            ev_str = "; ".join(p.evidence) if isinstance(p.evidence, list) else str(p.evidence)
            lines.append(f"Evidence: {ev_str}")
        if p.event_type == "RECOVERED":
            if p.new_route:
                lines.append(f"Active Bypass Route: {' -> '.join(p.new_route)}")
            if p.recovery_duration_s is not None:
                lines.append(f"Recovery Duration: {p.recovery_duration_s:.3f}s (simulated)")
        if p.details:
            lines.append(f"Details: {p.details}")

        return "\n".join(lines)

    def _send_payload(self, p: IncidentAlertPayload) -> bool:
        """Sends the message via Telegram Bot API sendMessage."""
        text = self._format_message(p)
        return self._send_raw_text(text)

    def _send_raw_text(self, text: str) -> bool:
        """Sends a plain text message via Telegram Bot API with redaction and bounded error handling."""
        if not self.bot_token or not self.chat_id:
            return False

        # Target endpoint URL
        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as e:
            # Handle rate-limiting (429) gracefully without crashing
            retry_after = 1
            try:
                err_body = e.read().decode("utf-8", errors="replace")
                data = json.loads(err_body)
                retry_after = int(data.get("parameters", {}).get("retry_after", 1))
            except Exception:
                pass

            if e.code == 429:
                logger.warning("Telegram rate-limited (429). Retry-after: %d seconds. Waiting interruptibly.", retry_after)
                with self._lock:
                    self._rate_limit_until = max(self._rate_limit_until, time.time() + retry_after)
                self._stop_event.wait(timeout=retry_after)
            else:
                # Never print the token or raw exception with token
                logger.warning("Telegram HTTP error %d: %s", e.code, e.reason)
        except urllib.error.URLError as e:
            # Ambiguous timeout or network failure: DO NOT retry blindly to avoid duplicate messages
            logger.warning("Telegram connection error: %s", self._sanitize_string(str(e.reason)))
        except Exception as e:
            logger.warning("Telegram delivery failed: %s", self._sanitize_string(str(e)))

        return False

    def _sanitize_string(self, text: str) -> str:
        """Redacts bot tokens from any exception or error string."""
        if not self.bot_token:
            return text
        # Redact the bot token
        sanitized = text.replace(self.bot_token, "[REDACTED_BOT_TOKEN]")
        # Redact token-like patterns (e.g. 123456:ABC-DEF...)
        sanitized = re.sub(r"\d{7,10}:[A-Za-z0-9_-]{30,}", "[REDACTED_BOT_TOKEN]", sanitized)
        return sanitized

    def shutdown(self, timeout: float = 1.0) -> None:
        """Stops the worker thread safely."""
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
