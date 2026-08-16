"""API-free checks for the structured final-card compiler."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from threading import Barrier
from typing import Any

import ai
import card_compiler
import card_models
from transcript_segments import TranscriptSegment


def segment(id: str, text: str, start_ms: int) -> TranscriptSegment:
    return TranscriptSegment(
        id=id,
        raw_text=text,
        normalized_text=text,
        start_ms=start_ms,
        end_ms=start_ms + 1_000,
        word_ids=[f"word-{id}"],
    )


def test_llm_schemas_never_request_html_or_source_text() -> None:
    segments = [segment("seg-0001", "十分に長い根拠となる説明です。", 0)]
    topic = card_models.Topic(
        id="topic-0001",
        title="全体",
        summary="全体の概要",
        segment_ids=["seg-0001"],
    )
    payloads = [
        card_compiler.outline_payload(segments),
        card_compiler.knowledge_payload(topic, segments),
    ]

    for payload in payloads:
        output_format = payload["text"]["format"]
        schema_text = json.dumps(output_format["schema"])
        assert output_format["strict"] is True
        assert "html" not in schema_text
        assert "source_text" not in schema_text


def test_compiler_validates_evidence_and_merges_exact_titles() -> None:
    segments = [
        segment(
            "seg-0001",
            "小さく試すと失敗の費用を抑え、早く学習できます。",
            0,
        ),
        segment(
            "seg-0002",
            "別の場面でも小さな試行は判断材料を増やします。",
            1_100,
        ),
        segment("seg-0003", "にたどり着いていきます。", 2_200),
    ]

    def generate(
        payload: dict[str, Any], _: str, __: ai.AIModel
    ) -> dict[str, Any]:
        name = payload["text"]["format"]["name"]
        if name == "topic_outline":
            return {
                "topics": [
                    {
                        "title": "試行の価値",
                        "summary": "小さな試行の効果",
                        "segment_ids": [
                            "seg-0001",
                            "seg-0002",
                            "seg-0003",
                        ],
                        "parent_title": "",
                    }
                ]
            }
        assert name == "knowledge_units"
        return {
            "units": [
                {
                    "id": "local-1",
                    "kind": "claim",
                    "title": "小さく試す価値",
                    "summary": "小さく試すと失敗の費用を抑えられる。",
                    "items": [],
                    "evidence_segment_ids": ["seg-0001"],
                    "relations": [],
                    "status": "verified",
                },
                {
                    "id": "local-2",
                    "kind": "claim",
                    "title": " 小さく 試す価値 ",
                    "summary": "小さな試行は判断材料を増やす。",
                    "items": [],
                    "evidence_segment_ids": ["seg-0002"],
                    "relations": [],
                    "status": "verified",
                },
                {
                    "id": "local-3",
                    "kind": "claim",
                    "title": "存在しない根拠",
                    "summary": "存在しない根拠に依存する。",
                    "items": [],
                    "evidence_segment_ids": ["seg-9999"],
                    "relations": [],
                    "status": "verified",
                },
                {
                    "id": "local-4",
                    "kind": "process",
                    "title": "判断材料を増やす3段階",
                    "summary": "調査、試作、検証の3段階で判断材料を増やす。",
                    "items": [
                        {"label": "1", "text": "調査"},
                        {"label": "2", "text": "試作"},
                        {"label": "3", "text": "検証"},
                    ],
                    "evidence_segment_ids": ["seg-0003"],
                    "relations": [],
                    "status": "verified",
                },
                {
                    "id": "local-5",
                    "kind": "definition",
                    "title": "クバネティスという用語",
                    "summary": "ASR上の表記を未確認として扱う。",
                    "items": [],
                    "evidence_segment_ids": ["seg-0001", "seg-0003"],
                    "relations": [],
                    "status": "needs_review",
                },
            ]
        }

    result = card_compiler.compile_final_knowledge(
        segments,
        "api-key",
        generator=generate,
    )

    assert len(result.topics) == 1
    assert [unit.title.strip() for unit in result.units] == [
        "小さく試す価値",
        "クバネティスという用語",
    ]
    assert result.units[0].evidence_segment_ids == ["seg-0001", "seg-0002"]
    assert result.units[1].evidence_segment_ids == ["seg-0001", "seg-0003"]
    assert result.units[1].status == "needs_review"
    assert len(result.merge_log) == 1
    assert any("存在しない根拠" in warning for warning in result.warnings)
    assert any("短すぎる根拠" in warning for warning in result.warnings)


def test_outline_and_topic_failures_fall_back_without_losing_segments() -> None:
    segments = [
        segment("seg-0001", "最初の十分に長い説明があります。", 0),
        segment("seg-0002", "次の十分に長い説明もあります。", 1_100),
    ]

    def fail(_: dict[str, Any], __: str, ___: ai.AIModel) -> dict[str, Any]:
        raise RuntimeError("offline")

    result = card_compiler.compile_final_knowledge(
        segments,
        "api-key",
        generator=fail,
    )

    assert len(result.topics) == 1
    assert result.topics[0].title == "全体"
    assert result.topics[0].segment_ids == ["seg-0001", "seg-0002"]
    assert result.units == []
    assert len(result.warnings) == 2


def test_uncategorized_outline_topic_gains_uncovered_segments() -> None:
    segments = [
        segment("seg-0001", "未分類として抽出された十分に長い説明です。", 0),
        segment("seg-0002", "アウトラインから漏れた十分に長い説明です。", 1_100),
    ]

    def generate(
        payload: dict[str, Any], _: str, __: ai.AIModel
    ) -> dict[str, Any]:
        if payload["text"]["format"]["name"] == "topic_outline":
            return {
                "topics": [
                    {
                        "title": "未分類",
                        "summary": "モデルが付けた話題",
                        "segment_ids": ["seg-0001"],
                        "parent_title": "",
                    }
                ]
            }
        return {"units": []}

    result = card_compiler.compile_final_knowledge(
        segments, "api-key", generator=generate
    )

    assert len(result.topics) == 1
    assert result.topics[0].title == "未分類"
    assert result.topics[0].summary == "モデルが付けた話題"
    assert result.topics[0].segment_ids == ["seg-0001", "seg-0002"]


def test_topic_knowledge_extraction_runs_in_parallel() -> None:
    segments = [
        segment("seg-0001", "第一の話題について十分に長い根拠を説明します。", 0),
        segment("seg-0002", "第二の話題について十分に長い根拠を説明します。", 1_100),
    ]
    knowledge_calls = Barrier(2)

    def generate(
        payload: dict[str, Any], _: str, __: ai.AIModel
    ) -> dict[str, Any]:
        name = payload["text"]["format"]["name"]
        if name == "topic_outline":
            return {
                "topics": [
                    {
                        "title": "第一の話題",
                        "summary": "第一の概要",
                        "segment_ids": ["seg-0001"],
                        "parent_title": "",
                    },
                    {
                        "title": "第二の話題",
                        "summary": "第二の概要",
                        "segment_ids": ["seg-0002"],
                        "parent_title": "",
                    },
                ]
            }
        assert name == "knowledge_units"
        knowledge_calls.wait(timeout=1)
        input_value = json.loads(payload["input"])
        segment_id = input_value["segments"][0]["id"]
        kind = "claim"
        if segment_id == "seg-0002":
            kind = "definition"
        return {
            "units": [
                {
                    "id": "local-1",
                    "kind": kind,
                    "title": f"{segment_id}の知識",
                    "summary": input_value["segments"][0]["text"],
                    "items": [],
                    "evidence_segment_ids": [segment_id],
                    "relations": [],
                    "status": "verified",
                }
            ]
        }

    result = card_compiler.compile_final_knowledge(
        segments,
        "api-key",
        generator=generate,
    )

    assert [unit.id for unit in result.units] == ["unit-0001", "unit-0002"]
    assert [unit.topic_id for unit in result.units] == [
        "topic-0001",
        "topic-0002",
    ]


def test_structured_artifacts_roundtrip_with_schema_version() -> None:
    topic = card_models.Topic(
        id="topic-0001",
        title="全体",
        summary="概要",
        segment_ids=["seg-0001"],
    )
    unit = card_models.KnowledgeUnit(
        id="unit-0001",
        topic_id=topic.id,
        kind="claim",
        title="根拠付き主張",
        summary="根拠に基づく主張です。",
        items=[],
        evidence_segment_ids=["seg-0001"],
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        outline = root / "session-outline.json"
        knowledge = root / "session-knowledge.json"
        card_models.save_topics(outline, "session", [topic])
        card_models.save_knowledge(
            knowledge,
            "session",
            [unit],
            warnings=["warning"],
            merge_log=["merge"],
        )

        outline_value = json.loads(outline.read_text(encoding="utf-8"))
        knowledge_value = json.loads(knowledge.read_text(encoding="utf-8"))
        loaded_topics = card_models.load_topics(outline)
        loaded_units = card_models.load_knowledge(knowledge)

    assert outline_value["schema_version"] == 2
    assert knowledge_value["schema_version"] == 2
    assert knowledge_value["warnings"] == ["warning"]
    assert knowledge_value["merge_log"] == ["merge"]
    assert loaded_topics == [topic]
    assert loaded_units == [unit]
