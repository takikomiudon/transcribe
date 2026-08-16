#!/usr/bin/env python3
"""Measure diagram-card quality from local JSON fixtures without API calls."""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


HTML_TAG = re.compile(r"<[^>]+>")
NUMBER = re.compile(r"\d+(?:[,.]\d+)*(?:%|％)?")


def _normalized_title(title: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", title)).casefold()


def _visible_text(value: str) -> str:
    return html.unescape(HTML_TAG.sub(" ", value))


def evaluate_fixture(path: Path) -> dict[str, int | float]:
    fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    segments = {segment["id"]: segment["raw_text"] for segment in fixture["segments"]}
    cards = fixture["cards"]

    titles = [_normalized_title(card["title"]) for card in cards]
    duplicate_title_count = len(titles) - len(set(titles))

    evidence_references = 0
    valid_evidence_references = 0
    numeric_claims = 0
    supported_numeric_claims = 0
    for card in cards:
        evidence_ids = card["evidence_segment_ids"]
        evidence_references += len(evidence_ids)
        evidence_text = " ".join(
            segments[segment_id]
            for segment_id in evidence_ids
            if segment_id in segments
        )
        valid_evidence_references += sum(
            segment_id in segments for segment_id in evidence_ids
        )
        card_text = f'{card["title"]} {_visible_text(card["html"])}'
        numbers = NUMBER.findall(card_text)
        numeric_claims += len(numbers)
        supported_numeric_claims += sum(number in evidence_text for number in numbers)

    evidence_validity = 0.0
    if evidence_references:
        evidence_validity = valid_evidence_references / evidence_references
    numeric_support = 1.0
    if numeric_claims:
        numeric_support = supported_numeric_claims / numeric_claims
    return {
        "card_count": len(cards),
        "duplicate_title_count": duplicate_title_count,
        "evidence_validity": evidence_validity,
        "numeric_support": numeric_support,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", nargs="+", type=Path)
    args = parser.parse_args()
    report = {str(path): evaluate_fixture(path) for path in args.fixtures}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
