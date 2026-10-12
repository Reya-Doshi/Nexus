"""
Unit and regression tests for TelegramNotifier (netadapt/telegram_notifier.py).
All external HTTP requests are strictly mocked with unittest.mock.
Verifies:
- Alert format: Plain text, includes "SIMULATED NETWORK", incident ID, affected component, evidence, and timestamps.
- Transitions: Alerts only for Detected, verified Recovered, and Unresolved.
- Recovery details: Includes active bypass route and simulated recovery duration. Never invents missing values.
- Deduplication: run_id + incident_id + event_type deduplication prevents duplicate alerts on repeated steps.
- Reset: Discards pending queue and invalidates old epoch alerts.
- Disabled mode: Completely inert when TELEGRAM_ENABLED is false/missing.
- Network tolerance: Request timeouts, 429 rate limit retry_after handling, no blind retries on timeouts.
- Simulation isolation: Simulation and controller continue normally even if notification fails or raises.
- Secrets hygiene: Never logs or reveals bot tokens.
"""

import json
import queue
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from netadapt.telegram_notifier import IncidentAlertPayload, TelegramNotifier
from netadapt.lab_session import LabSession
from netadapt.reroute_engine import IncidentState, ReRouteScenarioType


def test_telegram_notifier_disabled_mode():
    """Verify notifier is inert when enabled=False or credentials missing."""
    notifier = TelegramNotifier(enabled=False, bot_token="123456:FAKE", chat_id="99999")
    payload = IncidentAlertPayload(
        run_id="run-1",
        incident_id="INC-0001",
        event_type="DETECTED",
        epoch=0,
        timestamp=1.0,
        affected_component="R2-R4",
    )
    assert notifier.notify_incident(payload) is False

    # Missing credentials with enabled=True
    notifier_no_token = TelegramNotifier(enabled=True, bot_token="", chat_id="99999")
    assert notifier_no_token.notify_incident(payload) is False

    notifier_no_chat = TelegramNotifier(enabled=True, bot_token="123456:FAKE", chat_id="")
    assert notifier_no_chat.notify_incident(payload) is False


def test_telegram_notifier_message_formatting_and_required_tokens():
    """Verify plain text alert contents meet all prompt requirements."""
    notifier = TelegramNotifier(enabled=False, bot_token="123456:FAKE", chat_id="99999")

    # 1. Detected Alert
    detected_p = IncidentAlertPayload(
        run_id="run-100",
        incident_id="INC-0001",
        event_type="DETECTED",
        epoch=0,
        timestamp=2.5,
        affected_component="R2-R4",
        likely_cause="LINK_FAILURE",
        evidence=["Missed 2 heartbeats", "PDR drop to 0%"],
    )
    msg_det = notifier._format_message(detected_p)
    assert "SIMULATED NETWORK" in msg_det
    assert "INC-0001" in msg_det
    assert "run-100" in msg_det
    assert "DETECTED" in msg_det
    assert "R2-R4" in msg_det
    assert "LINK_FAILURE" in msg_det
    assert "Missed 2 heartbeats" in msg_det
    assert "2.50s (simulated)" in msg_det

    # 2. Recovered Alert
    recovered_p = IncidentAlertPayload(
        run_id="run-100",
        incident_id="INC-0001",
        event_type="RECOVERED",
        epoch=0,
        timestamp=4.8,
        affected_component="R2-R4",
        new_route=["Client", "R1", "R3", "R5", "R6", "Server"],
        recovery_duration_s=2.32,
    )
    msg_rec = notifier._format_message(recovered_p)
    assert "SIMULATED NETWORK" in msg_rec
    assert "RECOVERED" in msg_rec
    assert "Client -> R1 -> R3 -> R5 -> R6 -> Server" in msg_rec
    assert "2.320s (simulated)" in msg_rec

    # 3. Unresolved Alert
    unresolved_p = IncidentAlertPayload(
        run_id="run-100",
        incident_id="INC-0002",
        event_type="UNRESOLVED",
        epoch=0,
        timestamp=5.1,
        details="No alternate path exists meeting capacity. Packets dropping.",
    )
    msg_unres = notifier._format_message(unresolved_p)
    assert "SIMULATED NETWORK" in msg_unres
    assert "UNRESOLVED" in msg_unres
    assert "No alternate path exists" in msg_unres


def test_telegram_notifier_event_filtering():
    """Verify only DETECTED, RECOVERED, and UNRESOLVED transitions are accepted."""
    notifier = TelegramNotifier(enabled=True, bot_token="123456:FAKE", chat_id="99999")
    notifier.shutdown()  # Don't run background worker thread

    warning_p = IncidentAlertPayload(
        run_id="run-1", incident_id="INC-0001", event_type="WARNING", epoch=0, timestamp=1.0
    )
    assert notifier.notify_incident(warning_p) is False

    diagnosing_p = IncidentAlertPayload(
        run_id="run-1", incident_id="INC-0001", event_type="DIAGNOSING", epoch=0, timestamp=1.0
    )
    assert notifier.notify_incident(diagnosing_p) is False


def test_telegram_notifier_deduplication():
    """Verify deduplication by (run_id, incident_id, event_type) prevents duplicate sends."""
    notifier = TelegramNotifier(enabled=True, bot_token="123456:FAKE", chat_id="99999")
    notifier.shutdown()

    payload = IncidentAlertPayload(
        run_id="run-1",
        incident_id="INC-0001",
        event_type="DETECTED",
        epoch=0,
        timestamp=1.0,
        affected_component="R2-R4",
    )

    # First enqueue succeeds
    assert notifier.notify_incident(payload) is True
    # Immediate second enqueue with same run, incident, and event type must be rejected
    assert notifier.notify_incident(payload) is False

    # Different event type for same incident is allowed
    payload_rec = IncidentAlertPayload(
        run_id="run-1",
        incident_id="INC-0001",
        event_type="RECOVERED",
        epoch=0,
        timestamp=3.0,
    )
    assert notifier.notify_incident(payload_rec) is True

    # Same event type for different incident is allowed
    payload_inc2 = IncidentAlertPayload(
        run_id="run-1",
        incident_id="INC-0002",
        event_type="DETECTED",
        epoch=0,
        timestamp=5.0,
    )
    assert notifier.notify_incident(payload_inc2) is True


def test_telegram_notifier_reset_discards_queue_and_stale_epochs():
    """Verify reset() increments epoch and discards queued/in-flight items."""
    notifier = TelegramNotifier(enabled=True, bot_token="123456:FAKE", chat_id="99999")
    notifier.shutdown()

    payload = IncidentAlertPayload(
        run_id="run-1",
        incident_id="INC-0001",
        event_type="DETECTED",
        epoch=0,
        timestamp=1.0,
    )
    assert notifier.notify_incident(payload) is True
    assert notifier._queue.qsize() == 1

    # Reset advances epoch and empties queue
    notifier.reset()
    assert notifier.current_epoch == 1
    assert notifier._queue.qsize() == 0

    # Old epoch payload is discarded
    assert notifier.notify_incident(payload) is False


def test_telegram_notifier_send_raw_text_success_and_secrets_redaction():
    """Verify sendMessage sends plain text and redacts bot token on error."""
    token = "987654:SECRET_TOKEN_VALUE"
    notifier = TelegramNotifier(enabled=True, bot_token=token, chat_id="12345")
    notifier.shutdown()

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        success = notifier._send_raw_text("Hello SIMULATED NETWORK")
        assert success is True
        assert mock_urlopen.called
        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data.decode("utf-8"))
        assert data["chat_id"] == "12345"
        assert data["text"] == "Hello SIMULATED NETWORK"

    # Verify token redaction helper
    raw_error = f"HTTP 401: Unauthorized for url https://api.telegram.org/bot{token}/sendMessage"
    sanitized = notifier._sanitize_string(raw_error)
    assert token not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized


def test_telegram_notifier_rate_limit_and_timeout_no_blind_retry():
    """Verify rate limit (429) respects retry_after and timeouts do not blindly retry."""
    token = "123456:TOKEN"
    notifier = TelegramNotifier(enabled=True, bot_token=token, chat_id="12345")
    notifier.shutdown()

    # 1. 429 Rate limiting honors full retry_after via interruptible wait
    mock_err_429 = urllib.error.HTTPError(
        url="https://api.telegram.org",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"ok": false, "parameters": {"retry_after": 7}}'),
    )

    with patch("urllib.request.urlopen", side_effect=mock_err_429), patch.object(notifier._stop_event, "wait") as mock_wait:
        success = notifier._send_raw_text("test")
        assert success is False
        assert mock_wait.called
        assert mock_wait.call_args[1]["timeout"] == 7
        assert notifier._rate_limit_until > time.time()

    # Reset clears rate-limit cooldown and advances epoch
    notifier.reset()
    assert notifier._rate_limit_until == 0.0

    # 2. Timeout error does not crash or raise
    mock_timeout = urllib.error.URLError("timed out")
    with patch("urllib.request.urlopen", side_effect=mock_timeout):
        success = notifier._send_raw_text("test timeout")
        assert success is False


def test_telegram_notifier_simulation_continues_despite_network_failure():
    """Verify ReRouteController simulation runs smoothly even if Telegram throws errors."""
    session = LabSession()
    session.load_reroute_demo()
    ctrl = session.reroute_controller

    # Enable notifier with mock failing transport
    ctrl.notifier.enabled = True
    ctrl.notifier.bot_token = "123456:TEST_TOKEN"
    ctrl.notifier.chat_id = "123456"

    # Step simulation through fault injection with network raising exception
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network down")):
        ctrl.inject_scenario(ReRouteScenarioType.SUDDEN_LINK_FAILURE.value)
        for _ in range(6):
            res = session.reroute_step()
            assert "reroute" in res
            assert res["reroute"]["incident_state"] is not None

    # Clean shutdown
    ctrl.notifier.shutdown()
