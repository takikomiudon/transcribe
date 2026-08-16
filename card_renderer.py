"""Deterministically render typed knowledge with existing card components."""

from __future__ import annotations

import html

from card_models import KnowledgeItem, KnowledgeUnit, Topic
from cards import Card, sanitize_card_html
from transcript_segments import TranscriptSegment, reconstruct_source_text


def _item(item: KnowledgeItem, class_name: str) -> str:
    return (
        f'<div class="{class_name}"><strong>{html.escape(item.label)}</strong>'
        f"<p>{html.escape(item.text)}</p></div>"
    )


def _component(unit: KnowledgeUnit) -> str:
    if unit.kind == "process" and len(unit.items) >= 3:
        return "flow"
    if unit.kind == "comparison":
        return "compare"
    if unit.kind == "taxonomy":
        return "tree"
    if unit.kind == "timeline":
        return "timeline"
    if unit.kind == "definition":
        return "keyvalue"
    return "callout"


def _render_component(unit: KnowledgeUnit, component: str) -> str:
    summary = html.escape(unit.summary)
    summary_body = ""
    if summary:
        summary_body = f"<p>{summary}</p>"
    if component in {"flow", "compare", "tree", "timeline"} and not unit.items:
        return summary_body
    if component == "flow":
        children: list[str] = []
        for index, item in enumerate(unit.items):
            if index:
                children.append('<span class="flow-arrow">→</span>')
            children.append(_item(item, "flow-step"))
        return summary_body + f'<div class="flow">{"".join(children)}</div>'
    if component == "compare":
        children = "".join(_item(item, "compare-item") for item in unit.items)
        return summary_body + f'<div class="compare">{children}</div>'
    if component == "tree":
        children = "".join(_item(item, "tree-branch") for item in unit.items)
        return summary_body + f'<div class="tree">{children}</div>'
    if component == "timeline":
        children = "".join(_item(item, "timeline-item") for item in unit.items)
        return summary_body + f'<div class="timeline">{children}</div>'
    if component == "keyvalue":
        children = "".join(
            '<div class="keyvalue-item">'
            f'<span class="key">{html.escape(item.label)}</span>'
            f'<span class="value">{html.escape(item.text)}</span></div>'
            for item in unit.items
        )
        if not children:
            children = (
                '<div class="keyvalue-item"><span class="key">定義</span>'
                f'<span class="value">{summary}</span></div>'
            )
        return f'<div class="keyvalue">{children}</div>'
    body = f"<p>{summary}</p>"
    if unit.items:
        body += "<ul>" + "".join(
            f"<li><strong>{html.escape(item.label)}</strong> "
            f"{html.escape(item.text)}</li>"
            for item in unit.items
        ) + "</ul>"
    return f'<div class="callout">{body}</div>'


def _render_card_body(
    unit: KnowledgeUnit,
    examples: list[KnowledgeUnit],
    component: str,
) -> str:
    parts = ['<div class="card-body">', _render_component(unit, component)]
    for example in examples:
        parts.append(
            '<div><span class="label">具体例</span>'
            f"<p>{html.escape(example.summary)}</p></div>"
        )
    if unit.status == "needs_review" or any(
        example.status == "needs_review" for example in examples
    ):
        parts.append(
            '<p class="muted"><strong>要確認</strong> '
            "文字起こし上の表記を確認してください。</p>"
        )
    parts.append("</div>")
    return sanitize_card_html("".join(parts))


def render_cards(
    topics: list[Topic],
    units: list[KnowledgeUnit],
    segments: list[TranscriptSegment],
) -> list[Card]:
    by_id = {unit.id: unit for unit in units}
    examples: dict[str, list[KnowledgeUnit]] = {}
    attached: set[str] = set()
    for unit in units:
        if unit.kind != "example":
            continue
        for relation in unit.relations:
            if relation.kind == "example_of" and relation.target_id in by_id:
                examples.setdefault(relation.target_id, []).append(unit)
                attached.add(unit.id)
                break

    topic_order = {topic.id: topic.order for topic in topics}
    unit_order = {unit.id: index for index, unit in enumerate(units)}
    ordered_units = sorted(
        units,
        key=lambda unit: (topic_order[unit.topic_id], unit_order[unit.id]),
    )
    rendered: list[Card] = []
    for unit in ordered_units:
        if unit.id in attached:
            continue
        child_units = examples.get(unit.id, [])
        card_units = [unit, *child_units]
        evidence_ids = list(
            dict.fromkeys(
                id
                for card_unit in card_units
                for id in card_unit.evidence_segment_ids
            )
        )
        component = _component(unit)
        status = "done"
        if any(card_unit.status == "needs_review" for card_unit in card_units):
            status = "needs_review"
        rendered.append(
            Card(
                id=unit.id,
                title=unit.title,
                html=_render_card_body(unit, child_units, component),
                source_text=reconstruct_source_text(evidence_ids, segments),
                created_at=0,
                status=status,
                topic_id=unit.topic_id,
                unit_ids=[card_unit.id for card_unit in card_units],
                evidence_segment_ids=evidence_ids,
                component=component,
                summary=unit.summary,
            )
        )
    return rendered
