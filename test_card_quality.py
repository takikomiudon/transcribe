"""API-free regression checks for diagram-card quality fixtures."""

from pathlib import Path

from evaluate_card_quality import evaluate_fixture


FIXTURE_DIR = Path("tests/fixtures/card_quality")


def test_all_quality_regressions_have_small_synthetic_fixtures() -> None:
    assert {path.stem for path in FIXTURE_DIR.glob("*.json")} == {
        "asr_uncertain_name",
        "duplicate_title",
        "parallel_as_flow",
        "short_tail_evidence",
        "split_central_claim",
    }


def test_evaluator_exposes_current_duplicate_and_evidence_failures() -> None:
    duplicate = evaluate_fixture(FIXTURE_DIR / "duplicate_title.json")
    short_tail = evaluate_fixture(FIXTURE_DIR / "short_tail_evidence.json")

    assert duplicate == {
        "card_count": 2,
        "duplicate_title_count": 1,
        "evidence_validity": 1.0,
        "numeric_support": 1.0,
    }
    assert short_tail == {
        "card_count": 1,
        "duplicate_title_count": 0,
        "evidence_validity": 1.0,
        "numeric_support": 0.0,
    }
