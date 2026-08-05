"""Automatic Japanese titles for completed transcription sessions."""

from __future__ import annotations

from typing import Any

import transcribe


TITLE_TIMEOUT_SECONDS = 10


def title_payload(text: str) -> dict[str, Any]:
    return {
        "model": transcribe.TRANSLATION_MODEL,
        "reasoning": {"effort": "none"},
        "instructions": (
            "この文字起こしの内容を表す簡潔な日本語タイトルを最大20文字で"
            "出力してください。タイトルのみを出力し、括弧や引用符を付けないで"
            "ください。"
        ),
        "input": text[:2_000],
        "max_output_tokens": 200,
        "store": False,
    }


def generate_title(text: str, api_key: str) -> str | None:
    if not text.strip():
        return None
    try:
        title = transcribe.request_response_text(
            title_payload(text), api_key, TITLE_TIMEOUT_SECONDS
        ).strip()
    except Exception:
        return None
    return title[:20] or None
