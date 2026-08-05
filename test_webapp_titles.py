"""Offline checks for automatic web-app session titles."""

from __future__ import annotations

from unittest.mock import patch

import transcribe
from webapp.titles import generate_title, title_payload


def test_title_payload() -> None:
    text = "あ" * 2_100
    payload = title_payload(text)

    assert payload == {
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


def test_generate_title_uses_responses_helper() -> None:
    with patch(
        "webapp.titles.transcribe.request_response_text",
        return_value=" 文字起こしWeb会議 ",
    ) as request:
        assert generate_title("会議本文", "test-key") == "文字起こしWeb会議"

    request.assert_called_once_with(title_payload("会議本文"), "test-key", 10)


def test_generate_title_returns_none_on_failure_or_empty_input() -> None:
    with patch(
        "webapp.titles.transcribe.request_response_text",
        side_effect=TimeoutError,
    ):
        assert generate_title("会議本文", "test-key") is None
    assert generate_title("   ", "test-key") is None


def test_generate_title_limits_response_to_twenty_characters() -> None:
    with patch(
        "webapp.titles.transcribe.request_response_text",
        return_value="あ" * 21,
    ):
        assert generate_title("会議本文", "test-key") == "あ" * 20


def main() -> None:
    test_title_payload()
    test_generate_title_uses_responses_helper()
    test_generate_title_returns_none_on_failure_or_empty_input()
    test_generate_title_limits_response_to_twenty_characters()
    print("ok")


if __name__ == "__main__":
    main()
