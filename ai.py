"""Provider-specific text generation requests."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
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
DEEPSEEK_PEAK_HOURS_UTC = ((1, 4), (6, 10))


def is_deepseek_peak_hour(now: datetime | None = None) -> bool:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return any(start <= current.hour < end for start, end in DEEPSEEK_PEAK_HOURS_UTC)


def effective_model(model: AIModel, now: datetime | None = None) -> AIModel:
    if model.provider == DEEPSEEK_PROVIDER and is_deepseek_peak_hour(now):
        return DEFAULT_AI_MODEL
    return model


def model_from_values(provider: str, model: str) -> AIModel:
    for preset in MODEL_PRESETS:
        if preset.provider == provider and preset.model == model:
            return preset
    raise ValueError("利用できないAIモデルです。")


def available_models(deepseek_key: str) -> list[AIModel]:
    models = [DEFAULT_AI_MODEL]
    if deepseek_key:
        models.append(DEEPSEEK_FLASH_MODEL)
    return models


def api_key_for(model: AIModel, openai_key: str, deepseek_key: str) -> str:
    if model.provider == OPENAI_PROVIDER:
        key = openai_key
    elif model.provider == DEEPSEEK_PROVIDER:
        key = deepseek_key
    else:
        raise ValueError("利用できないAIプロバイダです。")
    if not key:
        if model.provider == DEEPSEEK_PROVIDER:
            provider = "DeepSeek"
        else:
            provider = "OpenAI"
        raise ValueError(f"{provider} APIキーが設定されていません。")
    return key


def strip_code_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


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
    if not isinstance(response, dict):
        raise ValueError("AIから不正な応答を受信しました。")
    if model.provider == OPENAI_PROVIDER:
        if response.get("status") not in (None, "completed"):
            raise ValueError("AI応答が完了しませんでした。")
        parts: list[str] = []
        output = response.get("output", [])
        if not isinstance(output, list):
            raise ValueError("AIから不正な応答を受信しました。")
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("AIから不正な応答を受信しました。")
            if item.get("type") != "message":
                continue
            contents = item.get("content", [])
            if not isinstance(contents, list):
                raise ValueError("AIから不正な応答を受信しました。")
            for content in contents:
                if not isinstance(content, dict):
                    raise ValueError("AIから不正な応答を受信しました。")
                if content.get("type") == "refusal":
                    raise ValueError("AI応答が拒否されました。")
                if content.get("type") == "output_text":
                    part = content.get("text", "")
                    if not isinstance(part, str):
                        raise ValueError("AIから不正な応答を受信しました。")
                    parts.append(part)
        text = "".join(parts).strip()
    elif model.provider == DEEPSEEK_PROVIDER:
        choices = response.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError("AIから不正な応答を受信しました。")
        choice: dict[str, Any] = {}
        if choices:
            if not isinstance(choices[0], dict):
                raise ValueError("AIから不正な応答を受信しました。")
            choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ValueError("AIから不正な応答を受信しました。")
        if finish_reason == "length":
            raise ValueError("AI応答がmax_tokensで打ち切られました。")
        if finish_reason == "content_filter":
            raise ValueError("AI応答がコンテンツフィルタで遮断されました。")
        if finish_reason == "insufficient_system_resource":
            raise ValueError("AIサーバーが混雑しているため応答を取得できませんでした。")
        message = choice.get("message", {})
        if not isinstance(message, dict):
            raise ValueError("AIから不正な応答を受信しました。")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("AIから不正な応答を受信しました。")
        text = ""
        if content is not None:
            text = content.strip()
    else:
        raise ValueError("利用できないAIプロバイダです。")
    if not text:
        raise ValueError("AI応答が空でした。")
    return text
