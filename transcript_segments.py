"""Immutable word timestamps and deterministic transcript evidence segments."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
SILENCE_BOUNDARY_MS = 1_200
MAX_SEGMENT_CHARACTERS = 400
MIN_SEGMENT_CHARACTERS = 6
SENTENCE_END = re.compile(r"[。！？!?]$")


@dataclass(frozen=True)
class TranscriptWord:
    id: str
    text: str
    start_ms: int | None
    end_ms: int | None
    speaker_id: str | None = None
    logprob: float | None = None
    kind: str = "word"


@dataclass(frozen=True)
class TranscriptSegment:
    id: str
    raw_text: str
    normalized_text: str
    start_ms: int | None
    end_ms: int | None
    word_ids: list[str]
    speaker_id: str | None = None
    uncertain_spans: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BatchTranscript:
    text: str
    words: list[TranscriptWord]


def _milliseconds(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("word timestamp must be numeric")
    return round(value * 1_000)


def batch_transcript_from_payload(payload: object) -> BatchTranscript:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("batch transcript must contain text")
    raw_words: object = []
    if "words" in payload:
        raw_words = payload["words"]
    if not isinstance(raw_words, list):
        raise ValueError("batch transcript words must be an array")

    words: list[TranscriptWord] = []
    previous_start_ms: int | None = None
    for index, raw_word in enumerate(raw_words, start=1):
        if not isinstance(raw_word, dict):
            raise ValueError("batch transcript word must be an object")
        if not isinstance(raw_word.get("text"), str):
            raise ValueError("batch transcript word must contain text")
        if not isinstance(raw_word.get("type"), str):
            raise ValueError("batch transcript word must contain type")
        speaker_id = None
        if "speaker_id" in raw_word:
            speaker_id = raw_word["speaker_id"]
        if speaker_id is not None and not isinstance(speaker_id, str):
            raise ValueError("word speaker_id must be a string")
        logprob = None
        if "logprob" in raw_word:
            logprob = raw_word["logprob"]
        if logprob is not None and not isinstance(logprob, (int, float)):
            raise ValueError("word logprob must be numeric")
        start = None
        if "start" in raw_word:
            start = raw_word["start"]
        end = None
        if "end" in raw_word:
            end = raw_word["end"]
        word = TranscriptWord(
            id=f"word-{index:06d}",
            text=raw_word["text"],
            start_ms=_milliseconds(start),
            end_ms=_milliseconds(end),
            speaker_id=speaker_id,
            logprob=logprob,
            kind=raw_word["type"],
        )
        if (
            previous_start_ms is not None
            and word.start_ms is not None
            and word.start_ms < previous_start_ms
        ):
            raise ValueError("word timestamps must be nondecreasing")
        if word.start_ms is not None:
            previous_start_ms = word.start_ms
        words.append(word)
    return BatchTranscript(text=payload["text"].strip(), words=words)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _build_segment(index: int, words: list[TranscriptWord]) -> TranscriptSegment:
    timed = [word for word in words if word.start_ms is not None]
    ended = [word for word in words if word.end_ms is not None]
    speakers = {
        word.speaker_id
        for word in words
        if word.kind != "spacing" and word.speaker_id is not None
    }
    start_ms = None
    if timed:
        start_ms = timed[0].start_ms
    end_ms = None
    if ended:
        end_ms = ended[-1].end_ms
    speaker_id = None
    if len(speakers) == 1:
        speaker_id = next(iter(speakers))
    raw_text = "".join(word.text for word in words).strip()
    return TranscriptSegment(
        id=f"seg-{index:04d}",
        raw_text=raw_text,
        normalized_text=_normalize_text(raw_text),
        start_ms=start_ms,
        end_ms=end_ms,
        word_ids=[word.id for word in words],
        speaker_id=speaker_id,
    )


def _merge_short_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    merged: list[TranscriptSegment] = []
    for segment in segments:
        if len(segment.normalized_text) >= MIN_SEGMENT_CHARACTERS or not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        if (
            previous.speaker_id is not None
            and segment.speaker_id is not None
            and previous.speaker_id != segment.speaker_id
        ):
            merged.append(segment)
            continue
        end_ms = previous.end_ms
        if segment.end_ms is not None:
            end_ms = segment.end_ms
        merged[-1] = TranscriptSegment(
            id=previous.id,
            raw_text=previous.raw_text + segment.raw_text,
            normalized_text=_normalize_text(
                previous.normalized_text + segment.normalized_text
            ),
            start_ms=previous.start_ms,
            end_ms=end_ms,
            word_ids=[*previous.word_ids, *segment.word_ids],
            speaker_id=previous.speaker_id,
            uncertain_spans=[*previous.uncertain_spans, *segment.uncertain_spans],
        )
    return [
        TranscriptSegment(
            id=f"seg-{index:04d}",
            raw_text=segment.raw_text,
            normalized_text=segment.normalized_text,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            word_ids=segment.word_ids,
            speaker_id=segment.speaker_id,
            uncertain_spans=segment.uncertain_spans,
        )
        for index, segment in enumerate(merged, start=1)
    ]


def segment_transcript(transcript: BatchTranscript) -> list[TranscriptSegment]:
    if not transcript.words:
        text = transcript.text.strip()
        if not text:
            return []
        return [
            TranscriptSegment(
                id="seg-0001",
                raw_text=text,
                normalized_text=_normalize_text(text),
                start_ms=None,
                end_ms=None,
                word_ids=[],
            )
        ]

    groups: list[list[TranscriptWord]] = []
    current: list[TranscriptWord] = []
    current_characters = 0
    previous_word: TranscriptWord | None = None
    pending_sentence_boundary = False
    for word in transcript.words:
        boundary = pending_sentence_boundary and word.kind != "spacing"
        if previous_word is not None and current:
            if (
                previous_word.end_ms is not None
                and word.start_ms is not None
                and word.start_ms - previous_word.end_ms >= SILENCE_BOUNDARY_MS
            ):
                boundary = True
            if (
                word.kind != "spacing"
                and previous_word.speaker_id is not None
                and word.speaker_id is not None
                and word.speaker_id != previous_word.speaker_id
            ):
                boundary = True
            if current_characters + len(word.text) > MAX_SEGMENT_CHARACTERS:
                boundary = True
        if boundary:
            groups.append(current)
            current = []
            current_characters = 0
            pending_sentence_boundary = False
        current.append(word)
        current_characters += len(word.text)
        if word.kind != "spacing":
            previous_word = word
            pending_sentence_boundary = SENTENCE_END.search(word.text) is not None
        if "\n" in word.text:
            pending_sentence_boundary = True
    if current:
        groups.append(current)

    segments = [_build_segment(index, group) for index, group in enumerate(groups, 1)]
    return _merge_short_segments(segments)


def segments_path(transcript_path: Path) -> Path:
    stem = transcript_path.stem.removesuffix("-final")
    return transcript_path.with_name(f"{stem}-segments.json")


def save_segments(
    path: Path,
    session_id: str,
    segments: list[TranscriptSegment],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "session_id": session_id,
                    "items": [asdict(segment) for segment in segments],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_segments(path: Path) -> list[TranscriptSegment]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported transcript segment schema")
    return [TranscriptSegment(**item) for item in value["items"]]


def reconstruct_source_text(
    segment_ids: list[str],
    segments: list[TranscriptSegment],
) -> str:
    by_id = {segment.id: segment for segment in segments}
    return "\n\n".join(by_id[segment_id].raw_text for segment_id in segment_ids)
