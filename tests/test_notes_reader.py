"""The notes reader only adds holds, reads only what changed, retries once,
and fails towards HOLD. No test here calls the real API."""

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import anthropic
import pytest

from chaain.connectors.stock_system.generator import CATALOGUE, SaleGenerator
from chaain.connectors.warehouse.mock import MockWarehouse
from chaain.core.config import load_rules, load_settings
from chaain.core.decision import Route
from chaain.llm import LLMError, provider_from_config
from chaain.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from chaain.llm.fake import FakeProvider
from chaain.workflows.order_push.gate import GateRules, Reference, decide
from chaain.workflows.order_push.notes_reader import (
    FLAGS, PROMPT_VERSION, SCHEMA, NotesReader, changed_text,
)

RULES = GateRules.from_config(load_rules("demo"))


def answer(raised=(), confidence=0.95, unsure=(), explanation="Nothing to check."):
    """A well-formed model answer with the given flags raised or unsure."""
    return {
        "flags": {
            name: {
                "raised": name in raised,
                "confidence": 0.5 if name in unsure else confidence,
                "evidence": "before 10am" if name in raised else "",
            }
            for name in FLAGS
        },
        "explanation": explanation,
    }


@pytest.fixture
def gen():
    return SaleGenerator(seed=31)


@pytest.fixture
def ref(gen):
    return Reference(
        warehouse_stock={code: 1000 for code, *_ in CATALOGUE},
        known_customers=frozenset(c.id for c in gen.customers),
    )


def reader(provider):
    return NotesReader(provider, sleep=lambda s: None)


def gate(sale, ref, notes):
    problems = MockWarehouse(today=date(2026, 11, 4)).prepare(sale).problems
    return decide(sale, ref, RULES, problems, notes)


def clean_standing_note(sale):
    """Give the sale a standing comment the fake treats as clean."""
    return replace(sale, standing_note="Deliver to side entrance. 09:00-13:00")


# ---------- reading only what changed ----------

def test_unchanged_note_needs_no_ai_call_for_the_order(gen):
    sale = gen.make()
    fake = FakeProvider(answer=lambda text: answer())
    result = reader(fake).read(sale)
    assert result.flags == () and result.error is None
    assert all("standing comment" in user for _, user in fake.calls)  # only the standing read


def test_only_the_added_text_is_sent(gen):
    sale = gen.make()
    sale = replace(sale, note=f"{sale.standing_note} THIS WEEK deliver Tuesday instead")
    text = changed_text(sale)
    assert "THIS WEEK deliver Tuesday instead" in text and sale.standing_note not in text


def test_a_rewritten_note_and_the_memo_are_sent_whole(gen):
    sale = gen.make("time_in_instructions", memo="Collect empties")
    text = changed_text(sale)
    assert "Deliver before 10am" in text and "Order memo:\nCollect empties" in text


# ---------- what the gate does with the answer ----------

def test_raised_flag_holds_the_order_with_the_evidence_and_source(gen, ref):
    sale = gen.make("time_in_instructions")
    fake = FakeProvider(answer=lambda t: answer(raised=("time_in_instructions",)) if "Order note" in t else answer())
    decision = gate(sale, ref, reader(fake).read(sale))
    assert decision.route is Route.HOLD
    assert '"before 10am"' in decision.reasons[0] and PROMPT_VERSION in decision.reasons[0]


def test_unsure_flag_holds_even_when_not_raised(gen, ref):
    sale = gen.make("no_window")
    fake = FakeProvider(answer=lambda t: answer(unsure=("needs_human",)) if "Order note" in t else answer())
    decision = gate(sale, ref, reader(fake).read(sale))
    assert decision.route is Route.HOLD and "unsure about needs human (50%)" in decision.reasons[0]


def test_confident_clean_answer_lets_the_order_through(gen, ref):
    sale = gen.make("no_window")
    decision = gate(sale, ref, reader(FakeProvider(answer=lambda t: answer())).read(sale))
    assert decision.route is Route.AUTO


def test_standing_problem_is_reported_once_not_held(gen, ref):
    sales = [gen.make("standing_time_in_instructions") for _ in range(3)]
    sales = [replace(s, customer_id=sales[0].customer_id) for s in sales]
    fake = FakeProvider(answer=lambda t: answer(raised=("time_in_instructions",)))
    notes_reader = reader(fake)

    decisions = [gate(s, ref, notes_reader.read(s)) for s in sales]

    assert all(d.route is Route.AUTO for d in decisions)
    assert all("Fix the customer record" in d.reports[0] for d in decisions)
    assert len(fake.calls) == 1  # the standing comment was read once and cached


# ---------- failure handling ----------

def test_one_failure_is_retried_after_a_pause(gen, ref):
    sale = clean_standing_note(gen.make("no_window"))
    pauses = []
    fake = FakeProvider(script=[answer(), LLMError("timed out")], answer=lambda t: answer())
    result = NotesReader(fake, retry_pause_seconds=2, sleep=pauses.append).read(sale)
    assert result.error is None and pauses == [2] and len(fake.calls) == 3


def test_two_failures_hold_the_order(gen, ref):
    sale = clean_standing_note(gen.make("no_window"))
    fake = FakeProvider(script=[answer(), LLMError("timed out"), LLMError("timed out")])
    decision = gate(sale, ref, reader(fake).read(sale))
    assert decision.route is Route.HOLD and "Notes reader unavailable (timed out)" in decision.reasons[0]


@pytest.mark.parametrize("broken", [
    {"explanation": "x"},                                           # no flags
    {**answer(), "flags": {**answer()["flags"], "invoice_only": {"raised": "yes", "confidence": 0.9, "evidence": ""}}},
    {**answer(), "flags": {**answer()["flags"], "needs_human": {"raised": False, "confidence": 7, "evidence": ""}}},
])
def test_malformed_answers_count_as_failures(gen, ref, broken):
    sale = clean_standing_note(gen.make("no_window"))
    fake = FakeProvider(script=[answer(), broken, broken])
    decision = gate(sale, ref, reader(fake).read(sale))
    assert decision.route is Route.HOLD and "unavailable" in decision.reasons[0]


def test_standing_read_failure_is_reported_and_retried_next_order(gen, ref):
    sale = gen.make()
    fake = FakeProvider(script=[LLMError("down"), LLMError("down")], answer=lambda t: answer())
    notes_reader = reader(fake)
    first = gate(sale, ref, notes_reader.read(sale))
    assert first.route is Route.AUTO and "Couldn't check" in first.reports[0]
    second = gate(sale, ref, notes_reader.read(sale))
    assert second.reports == ()


# ---------- the Anthropic provider, against a stub client (no network) ----------

class StubClient:
    def __init__(self, response=None, error=None):
        self.requests = []
        self.response, self.error = response, error
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def reply(data, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(data))])


def test_request_uses_structured_output_effort_and_fallback():
    client = StubClient(reply(answer()))
    provider = AnthropicProvider("claude-opus-5-5", effort="low", client=client)

    assert provider.complete_json("system", "user", SCHEMA) == answer()
    request = client.requests[0]
    assert request["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
    assert "thinking" not in request  # always on for this model; effort is the control


def test_no_fallback_for_models_without_it():
    client = StubClient(reply(answer()))
    AnthropicProvider("claude-haiku-5-5", client=client).complete_json("s", "u", SCHEMA)
    assert "fallbacks" not in client.requests[0]


@pytest.mark.parametrize("response, error, message", [
    (reply({}, stop_reason="refusal"), None, "declined"),
    (reply({}, stop_reason="max_tokens"), None, "cut off"),
    (SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")]), None, "valid JSON"),
    (None, anthropic.APIConnectionError(request=None), "couldn't connect"),
])
def test_every_failure_becomes_one_error_type(response, error, message):
    provider = AnthropicProvider("claude-opus-5-5", client=StubClient(response, error))
    with pytest.raises(LLMError, match=message):
        provider.complete_json("s", "u", SCHEMA)


def test_model_comes_from_the_environment_or_config():
    llm = load_settings("demo")["llm"]
    assert provider_from_config(llm, env={"ANTHROPIC_API_KEY": "x"}).model == "claude-opus-5-5"
    chosen = provider_from_config(llm, env={"ANTHROPIC_API_KEY": "x", "ANTHROPIC_MODEL": "claude-sonnet-5-5"})
    assert chosen.model == "claude-sonnet-5-5"
