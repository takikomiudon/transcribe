"""Offline checks for provider-specific AI requests."""

from __future__ import annotations

import json
from unittest.mock import patch

import ai


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
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


def test_unknown_model_is_rejected() -> None:
    try:
        ai.model_from_values("deepseek", "unknown")
    except ValueError as error:
        assert str(error) == "利用できないAIモデルです。"
    else:
        raise AssertionError("unknown model was accepted")
