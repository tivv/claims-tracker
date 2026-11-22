import json
from datetime import date
from unittest.mock import MagicMock

from app import extraction


def _fake_client(payload: dict):
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=json.dumps(payload))]
    client = MagicMock()
    client.messages.create.return_value = msg
    return client


def _clear_all_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(extraction.credentials, "get_anthropic_key", lambda: None)
    monkeypatch.setattr(extraction.credentials, "get_gemini_key", lambda: None)


def test_not_configured_without_api_key(monkeypatch):
    _clear_all_keys(monkeypatch)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is False


def test_uses_keychain_key_when_present(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setattr(extraction.credentials, "get_anthropic_key", lambda: "sk-kc-key")
    captured = {}

    def fake_anthropic(*args, **kwargs):
        captured["api_key"] = kwargs.get("api_key")
        msg = MagicMock()
        msg.content = [MagicMock(type="text", text=json.dumps({
            "member_name": "Jordan Rivera", "provider_name": "Bluebird Speech",
            "first_service_date": "2026-05-06", "amount_billed": "$570.00",
        }))]
        client = MagicMock()
        client.messages.create.return_value = msg
        return client

    monkeypatch.setattr(extraction.anthropic, "Anthropic", fake_anthropic)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert result.amount_billed_cents == 57000
    assert captured["api_key"] == "sk-kc-key"


def test_falls_back_to_env_when_keychain_empty(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-key")
    captured = {}

    def fake_anthropic(*args, **kwargs):
        captured["api_key"] = kwargs.get("api_key")
        msg = MagicMock()
        msg.content = [MagicMock(type="text", text=json.dumps({
            "member_name": "", "provider_name": "", "first_service_date": "", "amount_billed": "",
        }))]
        client = MagicMock()
        client.messages.create.return_value = msg
        return client

    monkeypatch.setattr(extraction.anthropic, "Anthropic", fake_anthropic)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert captured["api_key"] == "sk-env-key"


def test_success_maps_fields(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = _fake_client({
        "member_name": "Jordan Rivera",
        "provider_name": "Bluebird Speech",
        "first_service_date": "2026-05-06",
        "amount_billed": "$570.00",
    })
    monkeypatch.setattr(extraction.anthropic, "Anthropic", lambda *a, **k: client)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert result.error is None
    assert result.member_name == "Jordan Rivera"
    assert result.provider_name == "Bluebird Speech"
    assert result.first_service_date == date(2026, 5, 6)
    assert result.amount_billed_cents == 57000


def test_blank_fields_become_none(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = _fake_client({
        "member_name": "",
        "provider_name": "",
        "first_service_date": "",
        "amount_billed": "",
    })
    monkeypatch.setattr(extraction.anthropic, "Anthropic", lambda *a, **k: client)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert result.member_name is None
    assert result.amount_billed_cents is None
    assert result.first_service_date is None


def test_malformed_values_degrade_to_none(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = _fake_client({
        "member_name": "Jordan Rivera",
        "provider_name": "Bluebird Speech",
        "first_service_date": "not-a-date",
        "amount_billed": "free of charge",
    })
    monkeypatch.setattr(extraction.anthropic, "Anthropic", lambda *a, **k: client)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert result.error is None
    assert result.first_service_date is None
    assert result.amount_billed_cents is None
    assert result.member_name == "Jordan Rivera"


def test_api_error_is_captured(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(extraction.anthropic, "Anthropic", lambda *a, **k: client)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert "boom" in result.error
    assert result.member_name is None


def test_gemini_extraction_success(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setattr(extraction.credentials, "get_gemini_key", lambda: "AIza-gemini-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["api_key"] = (headers or {}).get("x-goog-api-key")
        resp = MagicMock()
        resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"member_name": "Jordan Rivera", "provider_name": "Bluebird Speech", "first_service_date": "2026-05-06", "amount_billed": "$570.00"}'
                    }]
                }
            }]
        }
        return resp

    monkeypatch.setattr(extraction.requests, "post", fake_post)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert result.error is None
    assert captured["api_key"] == "AIza-gemini-key"
    assert result.member_name == "Jordan Rivera"
    assert result.provider_name == "Bluebird Speech"
    assert result.first_service_date == date(2026, 5, 6)
    assert result.amount_billed_cents == 57000


def test_gemini_env_fallback(monkeypatch):
    _clear_all_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["api_key"] = (headers or {}).get("x-goog-api-key")
        resp = MagicMock()
        resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"member_name": "", "provider_name": "", "first_service_date": "", "amount_billed": ""}'
                    }]
                }
            }]
        }
        return resp

    monkeypatch.setattr(extraction.requests, "post", fake_post)
    result = extraction.extract_submission_fields(b"%PDF-1.4 fake")
    assert result.configured is True
    assert captured["api_key"] == "AIza-env-key"
