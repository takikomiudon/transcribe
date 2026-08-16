"""Network-free checks for deterministic knowledge-card rendering."""

from __future__ import annotations

import card_models
import card_renderer
from transcript_segments import TranscriptSegment


def segment(id: str, text: str) -> TranscriptSegment:
    return TranscriptSegment(id, text, text, 0, 1_000, [f"word-{id}"])


def unit(
    id: str,
    kind: card_models.KnowledgeKind,
    title: str,
    items: list[card_models.KnowledgeItem],
    *,
    status: card_models.KnowledgeStatus = "verified",
    relations: list[card_models.KnowledgeRelation] = [],
) -> card_models.KnowledgeUnit:
    return card_models.KnowledgeUnit(
        id=id,
        topic_id="topic-0001",
        kind=kind,
        title=title,
        summary=f"{title}の要約 <unsafe>",
        items=items,
        evidence_segment_ids=["seg-0001"],
        relations=relations,
        status=status,
    )


def test_kind_to_component_mapping_and_html_are_deterministic() -> None:
    topic = card_models.Topic(
        "topic-0001", "話題", "概要", ["seg-0001"]
    )
    items = [
        card_models.KnowledgeItem("一", "説明A"),
        card_models.KnowledgeItem("二", "説明B"),
        card_models.KnowledgeItem("三", "説明C"),
    ]
    units = [
        unit("unit-0001", "process", "手順", items),
        unit("unit-0002", "comparison", "比較", items[:2]),
        unit("unit-0003", "taxonomy", "分類", items),
        unit("unit-0004", "timeline", "時系列", items),
        unit("unit-0005", "definition", "定義", items[:2]),
        unit("unit-0006", "claim", "主張", []),
        unit("unit-0007", "caution", "注意", []),
        unit("unit-0008", "question", "疑問", []),
    ]

    first = card_renderer.render_cards([topic], units, [segment("seg-0001", "根拠")])
    second = card_renderer.render_cards([topic], units, [segment("seg-0001", "根拠")])

    assert [card.component for card in first] == [
        "flow",
        "compare",
        "tree",
        "timeline",
        "keyvalue",
        "callout",
        "callout",
        "callout",
    ]
    assert [card.html for card in first] == [card.html for card in second]
    assert all(card.created_at == 0 for card in first)
    assert any("&lt;unsafe&gt;" in card.html for card in first)
    assert all("<unsafe>" not in card.html for card in first)
    assert "<h3>" not in first[0].html
    assert 'class="flow"' in first[0].html


def test_examples_fold_into_parent_and_source_comes_from_segments() -> None:
    topic = card_models.Topic(
        "topic-0001", "話題", "概要", ["seg-0001", "seg-0002"]
    )
    parent = card_models.KnowledgeUnit(
        "unit-0001",
        topic.id,
        "claim",
        "根拠付き主張",
        "中心主張",
        [],
        ["seg-0001"],
    )
    example = card_models.KnowledgeUnit(
        "unit-0002",
        topic.id,
        "example",
        "具体例",
        "補足となる具体例",
        [],
        ["seg-0002"],
        [card_models.KnowledgeRelation("example_of", parent.id)],
        "needs_review",
    )

    cards = card_renderer.render_cards(
        [topic],
        [parent, example],
        [segment("seg-0001", "離れた根拠一。"), segment("seg-0002", "離れた根拠二。")],
    )

    assert len(cards) == 1
    assert cards[0].unit_ids == ["unit-0001", "unit-0002"]
    assert cards[0].evidence_segment_ids == ["seg-0001", "seg-0002"]
    assert cards[0].source_text == "離れた根拠一。\n\n離れた根拠二。"
    assert "具体例" in cards[0].html
    assert "要確認" in cards[0].html


def test_parallel_taxonomy_never_renders_as_flow() -> None:
    topic = card_models.Topic(
        "topic-0001", "選択肢", "概要", ["seg-0001"]
    )
    taxonomy = unit(
        "unit-0001",
        "taxonomy",
        "3つの選択肢",
        [
            card_models.KnowledgeItem("自社開発", "内製する"),
            card_models.KnowledgeItem("外部委託", "委託する"),
            card_models.KnowledgeItem("既製品", "購入する"),
        ],
    )

    card = card_renderer.render_cards(
        [topic], [taxonomy], [segment("seg-0001", "3つは並列です。")]
    )[0]

    assert card.component == "tree"
    assert 'class="tree"' in card.html
    assert 'class="flow"' not in card.html
