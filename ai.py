"""Provider-specific text generation requests."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any


OPENAI_PROVIDER = "openai"
DEEPSEEK_PROVIDER = "deepseek"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"


@dataclass(frozen=True)
class AIModel:
    provider: str
    model: str
    label: str


DEFAULT_AI_MODEL = AIModel(
    provider=OPENAI_PROVIDER,
    model="gpt-5.6-luna",
    label="GPT-5.6 Luna",
)
DEEPSEEK_FLASH_MODEL = AIModel(
    provider=DEEPSEEK_PROVIDER,
    model="deepseek-v4-flash",
    label="DeepSeek V4 Flash",
)
MODEL_PRESETS = (DEFAULT_AI_MODEL, DEEPSEEK_FLASH_MODEL)


def model_from_values(provider: str, model: str) -> AIModel:
    for preset in MODEL_PRESETS:
        if preset.provider == provider and preset.model == model:
            return preset
    raise ValueError("利用できないAIモデルです。")


def available_models(deepseek_key: str) -> list[AIModel]:
    return [
        DEFAULT_AI_MODEL,
        *( [DEEPSEEK_FLASH_MODEL] if deepseek_key else []),
    ]


def api_key_for(model: AIModel, openai_key: str, deepseek_key: str) -> str:
    if model.provider == OPENAI_PROVIDER:
        return openai_key
    if model.provider == DEEPSEEK_PROVIDER:
        return deepseek_key
    raise ValueError("利用できないAIプロバイダです。")


def request(
    payload: dict[str, Any],
    api_key: str,
    timeout: float,
    model: AIModel = DEFAULT_AI_MODEL,
) -> dict[str, Any]:
    if model.provider == OPENAI_PROVIDER:
        request_payload = {**payload, "model": model.model}
        url = OPENAI_RESPONSES_URL
    elif model.provider == DEEPSEEK_PROVIDER:
        instructions = str(payload.get("instructions", ""))
        if "text" in payload and "json" not in instructions.lower():
            instructions = f"{instructions}\nJSON形式で出力してください。"
        request_payload = {
            "model": model.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": str(payload.get("input", ""))},
            ],
            "max_tokens": payload["max_output_tokens"],
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        if "text" in payload:
            request_payload["response_format"] = {"type": "json_object"}
        url = DEEPSEEK_CHAT_COMPLETIONS_URL
    else:
        raise ValueError("利用できないAIプロバイダです。")

    request_object = urllib.request.Request(
        url,
        data=json.dumps(request_payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request_object, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("AIから不正なJSONを受信しました。")
    return value


def response_text(response: dict[str, Any], model: AIModel = DEFAULT_AI_MODEL) -> str:
    if model.provider == OPENAI_PROVIDER:
        if response.get("status") not in (None, "completed"):
            raise ValueError("AI応答が完了しませんでした。")
        parts: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(str(content.get("text", "")))
        text = "".join(parts).strip()
    elif model.provider == DEEPSEEK_PROVIDER:
        choices = response.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        text = str(message.get("content") or "").strip()
    else:
        raise ValueError("利用できないAIプロバイダです。")
    if not text:
        raise ValueError("AI応答が空でした。")
    return text
