"""Typed topics and evidence-backed knowledge units."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


SCHEMA_VERSION = 2
KnowledgeKind = Literal[
    "claim",
    "definition",
    "process",
    "comparison",
    "taxonomy",
    "timeline",
    "example",
    "caution",
    "question",
]
RelationKind = Literal[
    "supports",
    "contrasts_with",
    "example_of",
    "depends_on",
    "elaborates",
]
KnowledgeStatus = Literal["verified", "needs_review"]


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    summary: str
    segment_ids: list[str]
    parent_id: str | None = None
    order: int = 0


@dataclass(frozen=True)
class KnowledgeItem:
    label: str
    text: str


@dataclass(frozen=True)
class KnowledgeRelation:
    kind: RelationKind
    target_id: str


@dataclass(frozen=True)
class KnowledgeUnit:
    id: str
    topic_id: str
    kind: KnowledgeKind
    title: str
    summary: str
    items: list[KnowledgeItem]
    evidence_segment_ids: list[str]
    relations: list[KnowledgeRelation] = field(default_factory=list)
    status: KnowledgeStatus = "verified"


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def save_topics(path: Path, session_id: str, topics: list[Topic]) -> None:
    _save(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "items": [asdict(topic) for topic in topics],
        },
    )


def save_knowledge(
    path: Path,
    session_id: str,
    units: list[KnowledgeUnit],
    *,
    warnings: list[str],
    merge_log: list[str],
) -> None:
    _save(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "items": [asdict(unit) for unit in units],
            "warnings": warnings,
            "merge_log": merge_log,
        },
    )


def load_topics(path: Path) -> list[Topic]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported topic schema")
    return [Topic(**item) for item in value["items"]]


def load_knowledge(path: Path) -> list[KnowledgeUnit]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported knowledge schema")
    units: list[KnowledgeUnit] = []
    for item in value["items"]:
        item = dict(item)
        item["items"] = [KnowledgeItem(**child) for child in item["items"]]
        item["relations"] = [
            KnowledgeRelation(**relation) for relation in item["relations"]
        ]
        units.append(KnowledgeUnit(**item))
    return units
