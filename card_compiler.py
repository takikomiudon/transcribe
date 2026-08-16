"""Compile transcript evidence into typed topics and knowledge units."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ai
from card_models import (
    KnowledgeItem,
    KnowledgeRelation,
    KnowledgeUnit,
    Topic,
    save_knowledge,
    save_topics,
)
from transcript_segments import TranscriptSegment, reconstruct_source_text


COMPILER_TIMEOUT_SECONDS = 60
WINDOW_CHARACTER_LIMIT = 6_000
WINDOW_OVERLAP_SEGMENTS = 2
MIN_EVIDENCE_CHARACTERS = 20
KNOWLEDGE_KINDS = {
    "claim",
    "definition",
    "process",
    "comparison",
    "taxonomy",
    "timeline",
    "example",
    "caution",
    "question",
}
RELATION_KINDS = {
    "supports",
    "contrasts_with",
    "example_of",
    "depends_on",
    "elaborates",
}
STATUSES = {"verified", "needs_review"}
NUMBER = re.compile(r"\d+(?:[,.]\d+)*(?:%|％)?")
QUOTED = re.compile(r"[「『\"]([^」』\"]+)[」』\"]")

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "parent_title": {"type": "string"},
                },
                "required": [
                    "title",
                    "summary",
                    "segment_ids",
                    "parent_title",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}

KNOWLEDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(KNOWLEDGE_KINDS)},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["label", "text"],
                            "additionalProperties": False,
                        },
                    },
                    "evidence_segment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": sorted(RELATION_KINDS),
                                },
                                "target_id": {"type": "string"},
                            },
                            "required": ["kind", "target_id"],
                            "additionalProperties": False,
                        },
                    },
                    "status": {"type": "string", "enum": sorted(STATUSES)},
                },
                "required": [
                    "id",
                    "kind",
                    "title",
                    "summary",
                    "items",
                    "evidence_segment_ids",
                    "relations",
                    "status",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["units"],
    "additionalProperties": False,
}

CONSOLIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "merge_groups": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
            },
        }
    },
    "required": ["merge_groups"],
    "additionalProperties": False,
}

StructuredGenerator = Callable[
    [dict[str, Any], str, ai.AIModel], dict[str, Any]
]


@dataclass(frozen=True)
class CompilationResult:
    topics: list[Topic]
    units: list[KnowledgeUnit]
    warnings: list[str]
    merge_log: list[str]


def _base_payload(
    name: str,
    schema: dict[str, Any],
    instructions: str,
    input_value: dict[str, Any],
    model: ai.AIModel,
) -> dict[str, Any]:
    return {
        "model": model.model,
        "reasoning": {"effort": "none"},
        "instructions": instructions,
        "input": json.dumps(input_value, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": name,
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": 8_192,
        "store": False,
    }


def _segment_values(segments: list[TranscriptSegment]) -> list[dict[str, str]]:
    return [
        {"id": segment.id, "text": segment.normalized_text}
        for segment in segments
    ]


def outline_payload(
    segments: list[TranscriptSegment],
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
) -> dict[str, Any]:
    return _base_payload(
        "topic_outline",
        OUTLINE_SCHEMA,
        (
            "セグメントIDを保持して章・節を最大2階層で整理してください。"
            "ルート話題のparent_titleは空文字列にしてください。"
            "固定長ウィンドウ端を話題境界にしないでください。JSONだけを返してください。"
        ),
        {"segments": _segment_values(segments)},
        model,
    )


def knowledge_payload(
    topic: Topic,
    segments: list[TranscriptSegment],
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
) -> dict[str, Any]:
    return _base_payload(
        "knowledge_units",
        KNOWLEDGE_SCHEMA,
        (
            "話題から独立した主張・定義・手順・比較・分類・時系列・事例・注意・"
            "疑問を原子的に抽出してください。入力にない固有名詞を推測せず、"
            "各単位に実在するevidence_segment_idsを必ず付けてください。"
            "HTMLとsource_textは生成しないでください。JSONだけを返してください。"
        ),
        {
            "topic": {"title": topic.title, "summary": topic.summary},
            "segments": _segment_values(segments),
        },
        model,
    )


def consolidation_payload(
    units: list[KnowledgeUnit],
    candidate_pairs: list[tuple[str, str]],
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
) -> dict[str, Any]:
    return _base_payload(
        "knowledge_consolidation",
        CONSOLIDATION_SCHEMA,
        (
            "候補のうち中心主張が同じ言い換えだけを統合してください。"
            "反対の内容や単に関連する内容は統合しないでください。JSONだけを返してください。"
        ),
        {
            "units": [
                {
                    "id": unit.id,
                    "kind": unit.kind,
                    "title": unit.title,
                    "summary": unit.summary,
                }
                for unit in units
            ],
            "candidate_pairs": [list(pair) for pair in candidate_pairs],
        },
        model,
    )


def request_structured(
    payload: dict[str, Any],
    api_key: str,
    model: ai.AIModel,
) -> dict[str, Any]:
    response = ai.request(payload, api_key, COMPILER_TIMEOUT_SECONDS, model)
    value = json.loads(ai.strip_code_fence(ai.response_text(response, model)))
    if not isinstance(value, dict):
        raise ValueError("structured compiler response must be an object")
    return value


def segment_windows(
    segments: list[TranscriptSegment],
    character_limit: int = WINDOW_CHARACTER_LIMIT,
    overlap: int = WINDOW_OVERLAP_SEGMENTS,
) -> list[list[TranscriptSegment]]:
    windows: list[list[TranscriptSegment]] = []
    start = 0
    while start < len(segments):
        end = start
        characters = 0
        while end < len(segments):
            length = len(segments[end].normalized_text)
            if end > start and characters + length > character_limit:
                break
            characters += length
            end += 1
        windows.append(segments[start:end])
        if end == len(segments):
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return windows


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def _parse_topic_values(value: dict[str, Any]) -> list[dict[str, Any]]:
    raw_topics = value.get("topics")
    if not isinstance(raw_topics, list):
        raise ValueError("outline topics must be an array")
    topics: list[dict[str, Any]] = []
    for raw in raw_topics:
        if not isinstance(raw, dict):
            raise ValueError("outline topic must be an object")
        title = raw.get("title")
        summary = raw.get("summary")
        segment_ids = raw.get("segment_ids")
        parent_title = raw.get("parent_title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("outline topic title must not be empty")
        if not isinstance(summary, str):
            raise ValueError("outline topic summary must be a string")
        if not isinstance(segment_ids, list) or not all(
            isinstance(segment_id, str) for segment_id in segment_ids
        ):
            raise ValueError("outline segment_ids must be strings")
        if not isinstance(parent_title, str):
            raise ValueError("outline parent_title must be a string")
        topics.append(
            {
                "title": title.strip(),
                "summary": summary.strip(),
                "segment_ids": segment_ids,
                "parent_title": parent_title.strip(),
            }
        )
    return topics


def _fallback_topic(segments: list[TranscriptSegment]) -> list[Topic]:
    return [
        Topic(
            id="topic-0001",
            title="全体",
            summary="最終文字起こし全体",
            segment_ids=[segment.id for segment in segments],
        )
    ]


def _compile_outline(
    segments: list[TranscriptSegment],
    api_key: str,
    model: ai.AIModel,
    generator: StructuredGenerator,
    warnings: list[str],
) -> list[Topic]:
    raw_topics: list[dict[str, Any]] = []
    for window in segment_windows(segments):
        try:
            response = generator(outline_payload(window, model), api_key, model)
            raw_topics.extend(_parse_topic_values(response))
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            warnings.append(f"outline生成失敗: {error}")
    if not raw_topics:
        return _fallback_topic(segments)

    segment_order = {segment.id: index for index, segment in enumerate(segments)}
    drafts: dict[str, dict[str, Any]] = {}
    for raw in raw_topics:
        valid_ids = [
            segment_id
            for segment_id in raw["segment_ids"]
            if segment_id in segment_order
        ]
        valid_ids = list(dict.fromkeys(valid_ids))
        if not valid_ids:
            continue
        key = _normalized_title(raw["title"])
        if key in drafts:
            drafts[key]["segment_ids"] = list(
                dict.fromkeys([*drafts[key]["segment_ids"], *valid_ids])
            )
            continue
        drafts[key] = {
            "title": raw["title"],
            "summary": raw["summary"],
            "segment_ids": valid_ids,
            "parent_title": raw["parent_title"],
        }

    covered = {
        segment_id
        for draft in drafts.values()
        for segment_id in draft["segment_ids"]
    }
    uncovered = [segment.id for segment in segments if segment.id not in covered]
    if uncovered:
        uncategorized_key = _normalized_title("未分類")
        if uncategorized_key in drafts:
            drafts[uncategorized_key]["segment_ids"] = list(
                dict.fromkeys(
                    [*drafts[uncategorized_key]["segment_ids"], *uncovered]
                )
            )
        else:
            drafts[uncategorized_key] = {
                "title": "未分類",
                "summary": "アウトラインに含まれなかった発話",
                "segment_ids": uncovered,
                "parent_title": "",
            }
    ordered = sorted(
        drafts.values(),
        key=lambda draft: min(segment_order[id] for id in draft["segment_ids"]),
    )
    ids = {
        _normalized_title(draft["title"]): f"topic-{index:04d}"
        for index, draft in enumerate(ordered, start=1)
    }
    root_titles = {
        _normalized_title(draft["title"])
        for draft in ordered
        if not draft["parent_title"]
    }
    topics: list[Topic] = []
    for index, draft in enumerate(ordered, start=1):
        parent_id = None
        parent_key = _normalized_title(draft["parent_title"])
        if parent_key in ids and parent_key in root_titles:
            parent_id = ids[parent_key]
        topics.append(
            Topic(
                id=f"topic-{index:04d}",
                title=draft["title"],
                summary=draft["summary"],
                segment_ids=sorted(
                    draft["segment_ids"], key=segment_order.__getitem__
                ),
                parent_id=parent_id,
                order=index - 1,
            )
        )
    return topics


def _parse_units(
    value: dict[str, Any],
    topic: Topic,
    first_index: int,
) -> list[KnowledgeUnit]:
    raw_units = value.get("units")
    if not isinstance(raw_units, list):
        raise ValueError("knowledge units must be an array")
    local_ids: dict[str, str] = {}
    for offset, raw in enumerate(raw_units):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ValueError("knowledge unit id must be a string")
        local_id = raw["id"]
        if not local_id or local_id in local_ids:
            raise ValueError("knowledge unit ids must be unique")
        local_ids[local_id] = f"unit-{first_index + offset:04d}"

    units: list[KnowledgeUnit] = []
    for raw in raw_units:
        kind = raw.get("kind")
        title = raw.get("title")
        summary = raw.get("summary")
        status = raw.get("status")
        evidence_ids = raw.get("evidence_segment_ids")
        raw_items = raw.get("items")
        raw_relations = raw.get("relations")
        if kind not in KNOWLEDGE_KINDS:
            raise ValueError("unknown knowledge kind")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("knowledge title must not be empty")
        if not isinstance(summary, str):
            raise ValueError("knowledge summary must be a string")
        if status not in STATUSES:
            raise ValueError("unknown knowledge status")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError("knowledge evidence must not be empty")
        if not all(isinstance(id, str) for id in evidence_ids):
            raise ValueError("knowledge evidence ids must be strings")
        if not isinstance(raw_items, list) or not isinstance(raw_relations, list):
            raise ValueError("knowledge children must be arrays")
        items: list[KnowledgeItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("knowledge item must be an object")
            if not isinstance(raw_item.get("label"), str) or not isinstance(
                raw_item.get("text"), str
            ):
                raise ValueError("knowledge item fields must be strings")
            items.append(KnowledgeItem(raw_item["label"], raw_item["text"]))
        relations: list[KnowledgeRelation] = []
        for raw_relation in raw_relations:
            if not isinstance(raw_relation, dict):
                raise ValueError("knowledge relation must be an object")
            relation_kind = raw_relation.get("kind")
            target_id = raw_relation.get("target_id")
            if relation_kind not in RELATION_KINDS or target_id not in local_ids:
                raise ValueError("knowledge relation is invalid")
            relations.append(
                KnowledgeRelation(relation_kind, local_ids[target_id])
            )
        units.append(
            KnowledgeUnit(
                id=local_ids[raw["id"]],
                topic_id=topic.id,
                kind=kind,
                title=title.strip(),
                summary=summary.strip(),
                items=items,
                evidence_segment_ids=list(dict.fromkeys(evidence_ids)),
                relations=relations,
                status=status,
            )
        )
    return units


def _unit_detail(unit: KnowledgeUnit) -> str:
    return " ".join(
        [unit.title, unit.summary]
        + [f"{item.label} {item.text}" for item in unit.items]
    )


def _validate_units(
    units: list[KnowledgeUnit],
    segments: list[TranscriptSegment],
    warnings: list[str],
) -> list[KnowledgeUnit]:
    existing = {segment.id for segment in segments}
    valid: list[KnowledgeUnit] = []
    for unit in units:
        if any(id not in existing for id in unit.evidence_segment_ids):
            warnings.append(f"{unit.title}: 存在しない根拠ID")
            continue
        evidence = reconstruct_source_text(unit.evidence_segment_ids, segments)
        compact_evidence = re.sub(r"\s+", "", evidence)
        detail = _unit_detail(unit)
        compact_detail = re.sub(r"\s+", "", detail)
        if not compact_evidence:
            warnings.append(f"{unit.title}: 空の根拠")
            continue
        if (
            len(compact_evidence) < MIN_EVIDENCE_CHARACTERS
            and len(compact_evidence) * 2 < len(compact_detail)
        ):
            warnings.append(f"{unit.title}: 短すぎる根拠")
            continue
        status = unit.status
        unsupported_numbers = [
            number for number in NUMBER.findall(detail) if number not in evidence
        ]
        unsupported_quotes = [
            quote for quote in QUOTED.findall(detail) if quote not in evidence
        ]
        if unsupported_numbers or unsupported_quotes:
            status = "needs_review"
        valid.append(
            KnowledgeUnit(
                id=unit.id,
                topic_id=unit.topic_id,
                kind=unit.kind,
                title=unit.title,
                summary=unit.summary,
                items=unit.items,
                evidence_segment_ids=unit.evidence_segment_ids,
                relations=unit.relations,
                status=status,
            )
        )
    ids = {unit.id for unit in valid}
    return [
        KnowledgeUnit(
            id=unit.id,
            topic_id=unit.topic_id,
            kind=unit.kind,
            title=unit.title,
            summary=unit.summary,
            items=unit.items,
            evidence_segment_ids=unit.evidence_segment_ids,
            relations=[
                relation
                for relation in unit.relations
                if relation.target_id in ids and relation.target_id != unit.id
            ],
            status=unit.status,
        )
        for unit in valid
    ]


def _merge_group(group: list[KnowledgeUnit]) -> KnowledgeUnit:
    first = group[0]
    status = "verified"
    if any(unit.status == "needs_review" for unit in group):
        status = "needs_review"
    summary = max((unit.summary for unit in group), key=len)
    items = list(
        {
            (item.label, item.text): item
            for unit in group
            for item in unit.items
        }.values()
    )
    relations = list(
        {
            (relation.kind, relation.target_id): relation
            for unit in group
            for relation in unit.relations
        }.values()
    )
    return KnowledgeUnit(
        id=first.id,
        topic_id=first.topic_id,
        kind=first.kind,
        title=first.title,
        summary=summary,
        items=items,
        evidence_segment_ids=list(
            dict.fromkeys(
                id for unit in group for id in unit.evidence_segment_ids
            )
        ),
        relations=relations,
        status=status,
    )


def _merge_by_groups(
    units: list[KnowledgeUnit],
    groups: list[list[str]],
    merge_log: list[str],
    reason: str,
) -> list[KnowledgeUnit]:
    by_id = {unit.id: unit for unit in units}
    aliases = {unit.id: unit.id for unit in units}
    merged_ids: set[str] = set()
    replacements: dict[str, KnowledgeUnit] = {}
    for ids in groups:
        group = [by_id[id] for id in ids]
        merged = _merge_group(group)
        replacements[merged.id] = merged
        merged_ids.update(ids)
        for id in ids:
            aliases[id] = merged.id
        merge_log.append(f"{reason}: {','.join(ids)} -> {merged.id}")

    result: list[KnowledgeUnit] = []
    for unit in units:
        if unit.id in replacements:
            result.append(replacements[unit.id])
        elif unit.id not in merged_ids:
            result.append(unit)
    final_ids = {unit.id for unit in result}
    return [
        KnowledgeUnit(
            id=unit.id,
            topic_id=unit.topic_id,
            kind=unit.kind,
            title=unit.title,
            summary=unit.summary,
            items=unit.items,
            evidence_segment_ids=unit.evidence_segment_ids,
            relations=list(
                {
                    (relation.kind, aliases[relation.target_id]): KnowledgeRelation(
                        relation.kind, aliases[relation.target_id]
                    )
                    for relation in unit.relations
                    if aliases[relation.target_id] in final_ids
                    and aliases[relation.target_id] != unit.id
                }.values()
            ),
            status=unit.status,
        )
        for unit in result
    ]


def _merge_exact_titles(
    units: list[KnowledgeUnit], merge_log: list[str]
) -> list[KnowledgeUnit]:
    title_groups: dict[tuple[str, str, str], list[str]] = {}
    for unit in units:
        key = (unit.topic_id, unit.kind, _normalized_title(unit.title))
        title_groups.setdefault(key, []).append(unit.id)
    groups = [ids for ids in title_groups.values() if len(ids) > 1]
    return _merge_by_groups(units, groups, merge_log, "exact title")


def _trigrams(value: str) -> set[str]:
    normalized = _normalized_title(value)
    if len(normalized) < 3:
        return {normalized}
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _candidate_pairs(units: list[KnowledgeUnit]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(units):
        left_grams = _trigrams(left.title)
        for right in units[index + 1 :]:
            if left.kind != right.kind:
                continue
            right_grams = _trigrams(right.title)
            overlap = len(left_grams & right_grams) / len(left_grams | right_grams)
            # ponytail: character trigrams are the candidate filter; add embeddings
            # only if measured recall on local fixtures is insufficient.
            if overlap >= 0.55:
                pairs.append((left.id, right.id))
    return pairs


def _parse_merge_groups(
    value: dict[str, Any],
    units: list[KnowledgeUnit],
    candidates: list[tuple[str, str]],
) -> list[list[str]]:
    raw_groups = value.get("merge_groups")
    if not isinstance(raw_groups, list):
        raise ValueError("merge_groups must be an array")
    ids = {unit.id for unit in units}
    candidate_sets = {frozenset(pair) for pair in candidates}
    groups: list[list[str]] = []
    used: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, list) or len(raw_group) < 2:
            raise ValueError("merge group must contain at least two ids")
        if not all(isinstance(id, str) and id in ids for id in raw_group):
            raise ValueError("merge group contains an unknown id")
        group = list(dict.fromkeys(raw_group))
        if len(group) < 2 or any(id in used for id in group):
            raise ValueError("merge groups must be disjoint")
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if frozenset((left, right)) not in candidate_sets:
                    raise ValueError("merge group was not a candidate")
        groups.append(group)
        used.update(group)
    return groups


def compile_final_knowledge(
    segments: list[TranscriptSegment],
    api_key: str,
    model: ai.AIModel = ai.DEFAULT_AI_MODEL,
    *,
    generator: StructuredGenerator = request_structured,
) -> CompilationResult:
    warnings: list[str] = []
    merge_log: list[str] = []
    topics = _compile_outline(segments, api_key, model, generator, warnings)
    by_id = {segment.id: segment for segment in segments}
    units: list[KnowledgeUnit] = []
    next_index = 1
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                generator,
                knowledge_payload(
                    topic,
                    [by_id[id] for id in topic.segment_ids],
                    model,
                ),
                api_key,
                model,
            )
            for topic in topics
        ]
        for topic, future in zip(topics, futures, strict=True):
            try:
                extracted = _parse_units(future.result(), topic, next_index)
            except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                warnings.append(f"{topic.title}: knowledge抽出失敗: {error}")
                continue
            units.extend(extracted)
            next_index += len(extracted)

    units = _validate_units(units, segments, warnings)
    units = _merge_exact_titles(units, merge_log)
    candidates = _candidate_pairs(units)
    if candidates:
        try:
            response = generator(
                consolidation_payload(units, candidates, model), api_key, model
            )
            groups = _parse_merge_groups(response, units, candidates)
            units = _merge_by_groups(units, groups, merge_log, "semantic")
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            warnings.append(f"全体統合失敗: {error}")
    return CompilationResult(topics, units, warnings, merge_log)


def save_compilation(
    result: CompilationResult,
    session_id: str,
    outline_file: Path,
    knowledge_file: Path,
) -> None:
    save_topics(outline_file, session_id, result.topics)
    save_knowledge(
        knowledge_file,
        session_id,
        result.units,
        warnings=result.warnings,
        merge_log=result.merge_log,
    )


def outline_path(cards_path: Path) -> Path:
    return cards_path.with_name(f"{cards_path.stem}-outline.json")


def knowledge_path(cards_path: Path) -> Path:
    return cards_path.with_name(f"{cards_path.stem}-knowledge.json")
