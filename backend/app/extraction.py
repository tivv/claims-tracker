import base64
import json
import os
from typing import Optional

import anthropic
import requests

from app import credentials
from app.ingest import _parse_date, _parse_money
from app.schemas import ExtractionResult

_MODEL = "claude-sonnet-4-6"
_GEMINI_MODEL = "gemini-flash-latest"

_SCHEMA = {
    "type": "object",
    "properties": {
        "member_name": {"type": "string", "description": "Patient or member full name; empty string if not found"},
        "provider_name": {"type": "string", "description": "Provider, practice, or facility name; empty string if not found"},
        "first_service_date": {"type": "string", "description": "Earliest date of service if several appear, ISO YYYY-MM-DD preferred; empty string if not found"},
        "amount_billed": {"type": "string", "description": "Total amount billed including the dollar sign, e.g. \"$570.00\"; empty string if not found"},
    },
    "required": ["member_name", "provider_name", "first_service_date", "amount_billed"],
    "additionalProperties": False,
}

_GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "member_name": {"type": "STRING", "description": "Patient or member full name; empty string if not found"},
        "provider_name": {"type": "STRING", "description": "Provider, practice, or facility name; empty string if not found"},
        "first_service_date": {"type": "STRING", "description": "Earliest date of service if several appear, ISO YYYY-MM-DD preferred; empty string if not found"},
        "amount_billed": {"type": "STRING", "description": "Total amount billed including the dollar sign, e.g. \"$570.00\"; empty string if not found"},
    },
    "required": ["member_name", "provider_name", "first_service_date", "amount_billed"],
}

_PROMPT = (
    "This is a medical claim or superbill PDF. Extract the member name, the provider "
    "name, the earliest date of service (if several are listed), and the total amount "
    "billed, and return them in the required JSON format. If a field cannot be "
    "determined, use an empty string."
)


def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    return s or None


def _extract_with_anthropic(pdf_bytes: bytes, key: str) -> dict:
    client = anthropic.Anthropic(api_key=key)
    b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    message = client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), "")
    return json.loads(text)


def _extract_with_gemini(pdf_bytes: bytes, key: str) -> dict:
    model = os.environ.get("GEMINI_MODEL", _GEMINI_MODEL)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    resp = requests.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "application/pdf", "data": b64}},
                    {"text": _PROMPT},
                ],
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _GEMINI_SCHEMA,
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = next((p["text"] for p in parts if "text" in p), "")
    return json.loads(text)


def extract_submission_fields(pdf_bytes: bytes) -> ExtractionResult:
    """Send a claim PDF to Gemini or Claude and extract submission fields.

    Returns configured=False when neither GEMINI_API_KEY nor ANTHROPIC_API_KEY
    is set, and configured=True with an error string when the call or parse
    fails. Never raises — callers fall back to manual entry.
    """
    gemini_key = (
        credentials.get_gemini_key()
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    anthropic_key = credentials.get_anthropic_key() or os.environ.get("ANTHROPIC_API_KEY")
    if not gemini_key and not anthropic_key:
        return ExtractionResult(configured=False)

    try:
        if gemini_key:
            data = _extract_with_gemini(pdf_bytes, gemini_key)
        else:
            data = _extract_with_anthropic(pdf_bytes, anthropic_key)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001 — degrade gracefully to manual entry
        return ExtractionResult(configured=True, error=str(e))

    billed_raw = _clean(data.get("amount_billed"))
    date_raw = _clean(data.get("first_service_date"))
    try:
        service_date = _parse_date(date_raw) if date_raw else None
    except ValueError:
        service_date = None

    try:
        amount_cents = _parse_money(billed_raw) if billed_raw else None
    except (ValueError, AttributeError):
        amount_cents = None

    return ExtractionResult(
        configured=True,
        member_name=_clean(data.get("member_name")),
        provider_name=_clean(data.get("provider_name")),
        first_service_date=service_date,
        amount_billed_cents=amount_cents,
    )
