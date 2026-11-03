"""The notes eval scores the way the gate routes, and never needs the API to
re-score. No test here calls the real API."""

import json
from datetime import datetime, timezone

import pytest

from chaain.llm import LLMError
from chaain.llm.fake import FakeProvider
from chaain.workflows.order_push.notes_eval import (
    CASES, Case, collect, latest, load_cases, reaches_person, report, save, score,
)
from chaain.workflows.order_push.notes_reader import FLAGS, NotesReader


def case(id, must=(), may=(), path="order"):
    return Case(id=id, note=f"note {id}", path=path, must=frozenset(must), may=frozenset(may))


def answer(c, run=1, raised=(), confidence=0.95, unsure=()):
    return {
        "case": c.id, "run": run, "note": c.note,
        "flags": {n: {"raised": n in raised, "confidence": 0.5 if n in unsure else confidence} for n in FLAGS},
        "explanation": "",
    }


def results(*answers, runs=1):
    return {"prompt_version": "notes-v1", "source": "notes-v1 via fake/fake-model",
            "created": "2026-11-03T12:00:00+00:00", "runs": runs, "answers": list(answers)}


# ---------- the eval set ----------

def test_the_eval_set_loads_and_has_twenty_cases():
    cases = load_cases(CASES)
    assert len(cases) == 20
    assert any(c.path == "standing" for c in cases)
    assert sum(1 for c in cases if not c.needs_person) >= 4  # enough false-positive tests


def test_an_unknown_flag_in_the_eval_set_is_an_error(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text("cases:\n  - {id: a, note: x, must: [reserve_stock]}\n")
    with pytest.raises(ValueError, match="unknown flags"):
        load_cases(bad)


def test_a_flag_cant_be_both_must_and_may(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text("cases:\n  - {id: a, note: x, must: [needs_human], may: [needs_human]}\n")
    with pytest.raises(ValueError, match="both"):
        load_cases(bad)


# ---------- reaching a person, as the gate decides it ----------

def test_a_raised_flag_reaches_a_person():
    c = case("a", must=["time_in_instructions"])
    assert reaches_person(c, answer(c, raised=["time_in_instructions"]), 0.8)


def test_an_unsure_order_note_holds_but_an_unsure_standing_comment_does_not():
    order, standing = case("o"), case("s", path="standing")
    assert reaches_person(order, answer(order, unsure=["needs_human"]), 0.8)
    assert not reaches_person(standing, answer(standing, unsure=["needs_human"]), 0.8)


def test_a_failed_read_reaches_a_person():
    c = case("a")
    assert reaches_person(c, {"case": "a", "run": 1, "note": c.note, "error": "timeout"}, 0.8)


# ---------- scoring ----------

def test_a_missed_flag_is_a_miss_and_a_send():
    c = case("asap", must=["time_in_instructions"])
    s = score(results(answer(c)), [c], 0.8)
    assert s.missed == [("asap", 1)] and s.route_recall == 0
    assert s.flags["time_in_instructions"].fn == 1


def test_a_may_flag_is_not_scored_but_any_other_extra_is_a_false_positive():
    c = case("asap", must=["time_in_instructions"], may=["needs_human"])
    s = score(results(answer(c, raised=["time_in_instructions", "needs_human", "invoice_only"])), [c], 0.8)
    assert s.route_recall == 1
    assert s.flags["time_in_instructions"].tp == 1
    assert s.flags["needs_human"].fp == 0
    assert s.flags["invoice_only"].fp == 1


def test_holding_a_safe_note_is_a_wrong_hold():
    c = case("safe")
    s = score(results(answer(c, unsure=["needs_human"])), [c], 0.8)
    assert s.wrong_holds == [("safe", 1)] and s.unsure_only == [("safe", 1)]


def test_a_note_that_holds_on_one_run_and_not_another_is_a_flip():
    c = case("asap", must=["time_in_instructions"])
    s = score(results(answer(c, 1, raised=["time_in_instructions"]), answer(c, 2), runs=2), [c], 0.8)
    assert s.flips == ["asap"] and s.missed == [("asap", 2)]


def test_the_suggested_threshold_is_the_lowest_with_no_misses():
    """Lower thresholds hold less, so the lowest one that misses nothing
    gives the fewest wrong holds."""
    risky, safe = case("risky", must=["needs_human"]), case("safe")
    s = score(results(answer(risky, confidence=0.85), answer(safe, confidence=0.88)), [risky, safe], 0.8)
    assert s.sweep[0.85] == (1, 0) and s.sweep[0.9] == (0, 1)
    assert s.suggested_threshold == 0.9


def test_holding_for_the_wrong_flag_still_holds_but_is_a_flag_miss():
    c = case("x", must=["invoice_only"])
    s = score(results(answer(c, raised=["needs_human"], confidence=0.99)), [c], 0.8)
    assert s.missed == [] and s.flags["invoice_only"].fn == 1


def test_no_threshold_is_suggested_when_a_confident_miss_slips_through():
    c = case("x", must=["invoice_only"])
    s = score(results(answer(c, confidence=0.99)), [c], 0.8)
    assert s.suggested_threshold is None


def test_results_for_a_note_that_has_since_changed_are_refused():
    c = case("a")
    stale = answer(c)
    stale["note"] = "the old wording"
    with pytest.raises(ValueError, match="changed since the run"):
        score(results(stale), [c], 0.8)


# ---------- running and saving ----------

def well_formed(raised=()):
    return {"flags": {n: {"raised": n in raised, "confidence": 0.9, "evidence": ""} for n in FLAGS},
            "explanation": "Check the time."}


def test_collect_frames_each_note_as_the_gate_does_and_records_failures():
    order, standing = case("o", must=["needs_human"]), case("s", path="standing")
    fake = FakeProvider(script=[well_formed(["needs_human"]), LLMError("down"), LLMError("down again")])
    out = collect(NotesReader(fake, sleep=lambda s: None), [order, standing], runs=1,
                  now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    assert fake.calls[0][1].startswith("Order note (changed for this order):\nnote o")
    assert fake.calls[1][1].startswith("Customer's standing comment")
    assert out["answers"][0]["flags"]["needs_human"]["raised"] is True
    assert out["answers"][1]["error"] == "down again"  # after the one retry
    assert out["source"] == "notes-v1 via fake/fake-model"


def test_saved_results_can_be_rescored_without_the_api(tmp_path):
    c = case("o", must=["needs_human"])
    fake = FakeProvider(answer=lambda text: well_formed(["needs_human"]))
    out = collect(NotesReader(fake, sleep=lambda s: None), [c], runs=3,
                  now=datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    path = save(out, tmp_path)
    assert path.name == "20261103-120000-notes-v1-fake-model.json"
    assert latest(tmp_path) == path
    s = score(json.loads(path.read_text()), [c], 0.8)
    assert s.route_recall == 1 and s.answers == 3 and s.flips == []


def test_a_run_where_every_read_failed_reports_nothing_measured():
    """Failed reads hold, so an all-failed run would otherwise look perfect."""
    c = case("a", must=["needs_human"])
    failed = results({"case": "a", "run": 1, "note": c.note, "error": "credentials rejected"})
    text = report(score(failed, [c], 0.8), failed)
    assert "nothing was measured" in text and "credentials rejected" in text
    assert "Route recall" not in text and "Lowest threshold" not in text
