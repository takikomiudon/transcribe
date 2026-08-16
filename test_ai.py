"""Offline checks for provider-specific AI requests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import ai
import pytest


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_default_model_is_openai_luna() -> None:
    assert ai.DEFAULT_AI_MODEL == ai.AIModel(
        provider="openai",
        model="gpt-5.6-luna",
        label="GPT-5.6 Luna",
    )
    assert ai.model_from_values("openai", "gpt-5.6-luna") == ai.DEFAULT_AI_MODEL


def test_deepseek_is_available_only_with_a_key() -> None:
    assert [model.model for model in ai.available_models("")] == ["gpt-5.6-luna"]
    assert [model.model for model in ai.available_models("deepseek-key")] == [
        "gpt-5.6-luna",
        "deepseek-v4-flash",
    ]


def test_deepseek_peak_hours_use_utc_half_open_boundaries() -> None:
    def at(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 8, 5, hour, minute, tzinfo=timezone.utc)

    assert not ai.is_deepseek_peak_hour(at(0, 59))
    assert ai.is_deepseek_peak_hour(at(1, 0))
    assert ai.is_deepseek_peak_hour(at(3, 59))
    assert not ai.is_deepseek_peak_hour(at(4, 0))
    assert not ai.is_deepseek_peak_hour(at(5, 59))
    assert ai.is_deepseek_peak_hour(at(6, 0))
    assert ai.is_deepseek_peak_hour(at(9, 59))
    assert not ai.is_deepseek_peak_hour(at(10, 0))


def test_effective_model_only_replaces_deepseek_during_peak_hours() -> None:
    peak = datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc)
    regular = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)

    assert ai.effective_model(ai.DEEPSEEK_FLASH_MODEL, peak) == ai.DEFAULT_AI_MODEL
    assert ai.effective_model(ai.DEEPSEEK_FLASH_MODEL, regular) == ai.DEEPSEEK_FLASH_MODEL
    assert ai.effective_model(ai.DEFAULT_AI_MODEL, peak) == ai.DEFAULT_AI_MODEL


def test_deepseek_request_uses_chat_completions_and_json_output() -> None:
    response = FakeResponse(
        {
            "choices": [{"message": {"content": '{"value":"ok"}'}}],
            "usage": {"total_tokens": 7},
        }
    )
    payload = {
        "model": "gpt-5.6-luna",
        "instructions": "Return JSON.",
        "input": "source",
        "max_output_tokens": 32,
        "text": {"format": {"type": "json_schema"}},
    }
    model = ai.model_from_values("deepseek", "deepseek-v4-flash")

    with patch("ai.urllib.request.urlopen", return_value=response) as urlopen:
        result = ai.request(payload, "deepseek-key", 10, model)

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == "Bearer deepseek-key"
    body = json.loads(request.data)
    assert body == {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "source"},
        ],
        "max_tokens": 32,
        "stream": False,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    assert ai.response_text(result, model) == '{"value":"ok"}'


def test_deepseek_response_text_reports_finish_reason() -> None:
    for finish_reason, message in (
        ("length", "AI応答がmax_tokensで打ち切られました。"),
        ("content_filter", "AI応答がコンテンツフィルタで遮断されました。"),
        (
            "insufficient_system_resource",
            "AIサーバーが混雑しているため応答を取得できませんでした。",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            ai.response_text(
                {
                    "choices": [
                        {
                            "finish_reason": finish_reason,
                            "message": {"content": '{"value":"ok"}'},
                        }
                    ]
                },
                ai.DEEPSEEK_FLASH_MODEL,
            )


def test_deepseek_response_text_rejects_null_content() -> None:
    with pytest.raises(ValueError, match="AI応答が空でした。"):
        ai.response_text(
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": None}}
                ]
            },
            ai.DEEPSEEK_FLASH_MODEL,
        )


@pytest.mark.parametrize(
    ("response", "model"),
    [
        ({"choices": [None]}, ai.DEEPSEEK_FLASH_MODEL),
        ({"choices": ["invalid"]}, ai.DEEPSEEK_FLASH_MODEL),
        ({"choices": [{"message": None}]}, ai.DEEPSEEK_FLASH_MODEL),
        ({"output": ["invalid"]}, ai.DEFAULT_AI_MODEL),
        (
            {"output": [{"type": "message", "content": None}]},
            ai.DEFAULT_AI_MODEL,
        ),
    ],
)
def test_response_text_rejects_malformed_shapes(
    response: dict[str, object], model: ai.AIModel
) -> None:
    with pytest.raises(ValueError, match="AIから不正な応答を受信しました。"):
        ai.response_text(response, model)


def test_request_rejects_non_object_response() -> None:
    with (
        patch("ai.urllib.request.urlopen", return_value=FakeResponse([])),
        pytest.raises(ValueError, match="AIから不正なJSONを受信しました。"),
    ):
        ai.request({"input": "test"}, "openai-key", 10)


def test_strip_code_fence() -> None:
    assert ai.strip_code_fence('```json\n{"value":"ok"}\n```') == '{"value":"ok"}'
    assert ai.strip_code_fence('```\n{"value":"ok"}\n```') == '{"value":"ok"}'
    assert ai.strip_code_fence('{"value":"ok"}') == '{"value":"ok"}'


def test_unknown_model_is_rejected() -> None:
    try:
        ai.model_from_values("deepseek", "unknown")
    except ValueError as error:
        assert str(error) == "利用できないAIモデルです。"
    else:
        raise AssertionError("unknown model was accepted")
