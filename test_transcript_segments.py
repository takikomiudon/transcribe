"""Offline checks for immutable transcript evidence segments."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import transcript_segments as evidence


def test_batch_payload_parses_word_timestamps() -> None:
    transcript = evidence.batch_transcript_from_payload(
        {
            "text": "最初の説明です。 次の説明です。",
            "words": [
                {
                    "text": "最初の説明です。",
                    "start": 0.1,
                    "end": 1.2,
                    "type": "word",
                    "speaker_id": "speaker_0",
                    "logprob": -0.1,
                },
                {
                    "text": " ",
                    "start": 1.2,
                    "end": 1.3,
                    "type": "spacing",
                    "speaker_id": "speaker_0",
                },
                {
                    "text": "次の説明です。",
                    "start": 2.8,
                    "end": 3.7,
                    "type": "word",
                    "speaker_id": "speaker_0",
                },
            ],
        }
    )

    assert transcript.text == "最初の説明です。 次の説明です。"
    assert [word.id for word in transcript.words] == [
        "word-000001",
        "word-000002",
        "word-000003",
    ]
    assert transcript.words[0].start_ms == 100
    assert transcript.words[0].end_ms == 1_200
    assert transcript.words[0].logprob == -0.1
    assert transcript.words[1].kind == "spacing"


def test_segments_follow_sentence_gap_and_speaker_boundaries() -> None:
    transcript = evidence.batch_transcript_from_payload(
        {
            "text": "最初の説明です。次の説明です。別の話者です。",
            "words": [
                {
                    "text": "最初の説明です。",
                    "start": 0.0,
                    "end": 1.0,
                    "type": "word",
                    "speaker_id": "speaker_0",
                },
                {
                    "text": "次の説明です。",
                    "start": 2.5,
                    "end": 3.5,
                    "type": "word",
                    "speaker_id": "speaker_0",
                },
                {
                    "text": "別の話者です。",
                    "start": 3.6,
                    "end": 4.6,
                    "type": "word",
                    "speaker_id": "speaker_1",
                },
            ],
        }
    )

    segments = evidence.segment_transcript(transcript)

    assert [segment.id for segment in segments] == [
        "seg-0001",
        "seg-0002",
        "seg-0003",
    ]
    assert [segment.raw_text for segment in segments] == [
        "最初の説明です。",
        "次の説明です。",
        "別の話者です。",
    ]
    assert [segment.start_ms for segment in segments] == [0, 2_500, 3_600]
    assert [segment.speaker_id for segment in segments] == [
        "speaker_0",
        "speaker_0",
        "speaker_1",
    ]
    assert len({segment.id for segment in segments}) == len(segments)


def test_short_segment_keeps_a_different_speaker_boundary() -> None:
    transcript = evidence.batch_transcript_from_payload(
        {
            "text": "詳しい説明を続けます。はい。",
            "words": [
                {
                    "text": "詳しい説明を続けます。",
                    "start": 0.0,
                    "end": 1.0,
                    "type": "word",
                    "speaker_id": "speaker_0",
                },
                {
                    "text": "はい。",
                    "start": 1.1,
                    "end": 1.3,
                    "type": "word",
                    "speaker_id": "speaker_1",
                },
            ],
        }
    )

    segments = evidence.segment_transcript(transcript)

    assert [segment.raw_text for segment in segments] == [
        "詳しい説明を続けます。",
        "はい。",
    ]
    assert [segment.speaker_id for segment in segments] == [
        "speaker_0",
        "speaker_1",
    ]


def test_short_segment_without_end_keeps_previous_end() -> None:
    transcript = evidence.batch_transcript_from_payload(
        {
            "text": "詳しい説明を続けます。はい。",
            "words": [
                {
                    "text": "詳しい説明を続けます。",
                    "start": 0.0,
                    "end": 1.0,
                    "type": "word",
                    "speaker_id": "speaker_0",
                },
                {
                    "text": "はい。",
                    "start": 1.1,
                    "end": None,
                    "type": "word",
                    "speaker_id": "speaker_0",
                },
            ],
        }
    )

    segments = evidence.segment_transcript(transcript)

    assert len(segments) == 1
    assert segments[0].end_ms == 1_000


def test_plain_text_fallback_has_stable_untimed_evidence() -> None:
    transcript = evidence.batch_transcript_from_payload(
        {"text": "時刻のない最終文字起こしです。"}
    )

    segments = evidence.segment_transcript(transcript)

    assert transcript.words == []
    assert len(segments) == 1
    assert segments[0].id == "seg-0001"
    assert segments[0].raw_text == "時刻のない最終文字起こしです。"
    assert segments[0].start_ms is None
    assert segments[0].end_ms is None
    assert segments[0].word_ids == []


def test_batch_payload_rejects_decreasing_word_timestamps() -> None:
    with pytest.raises(ValueError, match="nondecreasing"):
        evidence.batch_transcript_from_payload(
            {
                "text": "順序が逆です。",
                "words": [
                    {
                        "text": "逆",
                        "start": 2.0,
                        "end": 2.5,
                        "type": "word",
                    },
                    {
                        "text": "順",
                        "start": 1.0,
                        "end": 1.5,
                        "type": "word",
                    },
                ],
            }
        )


def test_segment_json_roundtrip_and_source_reconstruction() -> None:
    segments = evidence.segment_transcript(
        evidence.batch_transcript_from_payload(
            {
                "text": "根拠その一。根拠その二。",
                "words": [
                    {
                        "text": "根拠その一。",
                        "start": 0.0,
                        "end": 1.0,
                        "type": "word",
                        "speaker_id": "speaker_0",
                    },
                    {
                        "text": "根拠その二。",
                        "start": 1.1,
                        "end": 2.0,
                        "type": "word",
                        "speaker_id": "speaker_0",
                    },
                ],
            }
        )
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "session-segments.json"
        evidence.save_segments(path, "session", segments)
        saved = json.loads(path.read_text(encoding="utf-8"))
        loaded = evidence.load_segments(path)

    assert saved["schema_version"] == 2
    assert saved["session_id"] == "session"
    assert saved["items"][0]["raw_text"] == "根拠その一。"
    assert loaded == segments
    assert evidence.reconstruct_source_text(
        ["seg-0001", "seg-0002"], loaded
    ) == "根拠その一。\n\n根拠その二。"
