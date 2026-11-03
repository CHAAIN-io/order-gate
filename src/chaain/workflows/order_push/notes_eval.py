"""The notes reader's eval: how often a note that needs a person gets one.

Two steps, kept apart so the numbers can be reproduced without the API:
- run:   ask the configured model about every case in evals/notes/cases.yaml,
         several times, and save every answer to evals/notes/results/.
- score: score a saved file against the cases. No API call.

The headline is route recall: of the notes that should reach a person, how
many did. A miss there is an order sent straight to picking. Per-flag
precision and recall sit underneath. A flag listed in a case's `may` is a
fair reading and isn't scored either way.

"Reaches a person" follows the gate: on an order note, any raised flag, any
unraised flag below the confidence threshold, or a failed read means HOLD.
On a standing comment, a raised flag or a failed read means a "fix the
customer record" report. Confidence doesn't count there, since standing
comments never hold.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from chaain.llm import LLMError
from chaain.workflows.order_push.notes_reader import (
    FLAGS, ORDER_NOTE, PROMPT_VERSION, STANDING_NOTE, NotesReader,
)

EVALS = Path(__file__).resolve().parents[4] / "evals" / "notes"
CASES = EVALS / "cases.yaml"
RESULTS = EVALS / "results"
THRESHOLDS = (0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
PATHS = ("order", "standing")


@dataclass(frozen=True)
class Case:
    id: str
    note: str
    path: str
    must: frozenset[str]
    may: frozenset[str]
    why: str = ""

    @property
    def needs_person(self) -> bool:
        return bool(self.must)


def load_cases(path: Path = CASES) -> list[Case]:
    """Load and check the eval set. A typo in a flag name would silently
    change the scores, so it's an error."""
    with open(path) as f:
        raw = yaml.safe_load(f)["cases"]
    cases, seen = [], set()
    for c in raw:
        case = Case(
            id=c["id"], note=c["note"], path=c.get("path", "order"),
            must=frozenset(c.get("must") or ()), may=frozenset(c.get("may") or ()), why=c.get("why", ""),
        )
        unknown = (case.must | case.may) - set(FLAGS)
        if unknown:
            raise ValueError(f"{case.id}: unknown flags {sorted(unknown)}")
        if case.must & case.may:
            raise ValueError(f"{case.id}: a flag can't be both must and may: {sorted(case.must & case.may)}")
        if case.path not in PATHS:
            raise ValueError(f"{case.id}: path must be one of {PATHS}")
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases


# ---------- run: ask the model ----------

def collect(reader: NotesReader, cases: list[Case], runs: int, now: datetime | None = None) -> dict:
    """Ask about every case `runs` times, framed exactly as the gate frames it.
    A failed read is recorded, not raised: it's an outcome like any other."""
    answers = []
    for run in range(1, runs + 1):
        for case in cases:
            text = (ORDER_NOTE if case.path == "order" else STANDING_NOTE).format(case.note.strip())
            entry = {"case": case.id, "run": run, "note": case.note}
            try:
                flags = reader.ask(text)
                entry["flags"] = {f.name: {"raised": f.raised, "confidence": f.confidence} for f in flags}
                entry["explanation"] = flags[0].explanation if flags else ""
            except LLMError as e:
                entry["error"] = str(e)
            answers.append(entry)
    return {
        "prompt_version": PROMPT_VERSION,
        "source": reader.source(),
        "created": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "runs": runs,
        "answers": answers,
    }


def save(results: dict, folder: Path = RESULTS) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(results["created"]).strftime("%Y%m%d-%H%M%S")
    model = results["source"].rsplit("/", 1)[-1]
    path = folder / f"{stamp}-{results['prompt_version']}-{model}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    return path


# ---------- score: no API ----------

def reaches_person(case: Case, answer: dict, threshold: float) -> bool:
    if "error" in answer:
        return True
    flags = answer["flags"].values()
    if any(f["raised"] for f in flags):
        return True
    return case.path == "order" and any(f["confidence"] < threshold for f in flags)


@dataclass
class FlagCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class Score:
    threshold: float
    answers: int = 0
    errors: int = 0
    should_reach: int = 0  # answers whose case needs a person
    reached: int = 0  # ... of which reached one
    missed: list[tuple[str, int]] = field(default_factory=list)  # (case, run): sent when it shouldn't be
    wrong_holds: list[tuple[str, int]] = field(default_factory=list)  # (case, run): held when it needn't be
    unsure_only: list[tuple[str, int]] = field(default_factory=list)  # held only by a low confidence
    flips: list[str] = field(default_factory=list)  # cases whose outcome changed between runs
    flags: dict[str, FlagCounts] = field(default_factory=lambda: {name: FlagCounts() for name in FLAGS})
    sweep: dict[float, tuple[int, int]] = field(default_factory=dict)  # threshold -> (missed, wrong holds)

    @property
    def route_recall(self) -> float | None:
        return self.reached / self.should_reach if self.should_reach else None

    @property
    def suggested_threshold(self) -> float | None:
        """The lowest threshold with no misses: the fewest wrong holds while
        missing nothing. None if no threshold gets to zero misses, which
        means the prompt needs work, not the threshold."""
        clean = [t for t, (missed, _) in sorted(self.sweep.items()) if missed == 0]
        return clean[0] if clean else None


def score(results: dict, cases: list[Case], threshold: float, thresholds=THRESHOLDS) -> Score:
    by_id = {c.id: c for c in cases}
    stale = sorted({a["case"] for a in results["answers"] if a["case"] in by_id and a["note"] != by_id[a["case"]].note})
    if stale:
        raise ValueError(f"these notes changed since the run, so run again: {stale}")
    answers = [(by_id[a["case"]], a) for a in results["answers"] if a["case"] in by_id]

    s = Score(threshold=threshold)
    outcomes: dict[str, set[bool]] = {}
    for case, a in answers:
        s.answers += 1
        reached = reaches_person(case, a, threshold)
        outcomes.setdefault(case.id, set()).add(reached)
        key = (case.id, a["run"])
        if case.needs_person:
            s.should_reach += 1
            s.reached += reached
            if not reached:
                s.missed.append(key)
        elif reached:
            s.wrong_holds.append(key)
        if "error" in a:
            s.errors += 1
            continue
        raised = {name for name, f in a["flags"].items() if f["raised"]}
        if reached and not raised:
            s.unsure_only.append(key)
        for name in FLAGS:
            counts = s.flags[name]
            if name in case.must:
                if name in raised:
                    counts.tp += 1
                else:
                    counts.fn += 1
            elif name in raised and name not in case.may:
                counts.fp += 1
    s.flips = [cid for cid, seen in outcomes.items() if len(seen) > 1]
    for t in thresholds:
        missed = sum(1 for c, a in answers if c.needs_person and not reaches_person(c, a, t))
        wrong = sum(1 for c, a in answers if not c.needs_person and reaches_person(c, a, t))
        s.sweep[t] = (missed, wrong)
    return s


def report(s: Score, results: dict) -> str:
    def pct(x: float | None) -> str:
        return "  n/a" if x is None else f"{x:5.0%}"

    runs = results["runs"]
    lines = [
        f"Notes reader eval: {results['source']}, {runs} run(s), {results['created']}",
        f"Threshold {s.threshold:.2f}. {s.answers} answers, {s.errors} failed reads.",
    ]
    if s.errors:
        reasons = sorted({a["error"] for a in results["answers"] if "error" in a})
        lines.append(f"WARNING: failed reads hold, so they inflate recall. Errors: {'; '.join(reasons)}")
    if s.errors == s.answers:
        lines.append("Every read failed: nothing was measured. Fix the cause and run again.")
        return "\n".join(lines)
    lines += [
        "",
        f"Route recall: {pct(s.route_recall)}  ({s.reached} of {s.should_reach} reached a person)",
        f"Missed (sent, should have held): {len(s.missed)}  {_keys(s.missed)}",
        f"Wrong holds (held, needn't be):  {len(s.wrong_holds)}  {_keys(s.wrong_holds)}",
        f"Held only by low confidence:     {len(s.unsure_only)}  {_keys(s.unsure_only)}",
    ]
    if runs > 1:
        lines.append(f"Changed outcome between runs:    {len(s.flips)}  {', '.join(s.flips)}")
    lines += ["", f"{'Flag':<24} {'Prec':>5} {'Recall':>6}   tp  fp  fn"]
    for name, c in s.flags.items():
        lines.append(f"{name:<24} {pct(c.precision)} {pct(c.recall):>6}  {c.tp:>3} {c.fp:>3} {c.fn:>3}")
    lines += ["", "Threshold sweep     missed  wrong holds"]
    for t, (missed, wrong) in s.sweep.items():
        mark = "  <- current" if t == s.threshold else ""
        lines.append(f"  {t:.2f}           {missed:>6}  {wrong:>11}{mark}")
    suggested = s.suggested_threshold
    lines.append("")
    if suggested is None:
        lines.append("No threshold gets to zero misses: change the prompt, not the threshold.")
    elif len(set(s.sweep.values())) == 1:
        lines.append("The threshold makes no difference on this run: keep the current one.")
    else:
        lines.append(f"Lowest threshold with no misses: {suggested:.2f}")
    return "\n".join(lines)


def _keys(keys: list[tuple[str, int]]) -> str:
    return ", ".join(f"{cid}#{run}" for cid, run in keys)


def latest(folder: Path = RESULTS) -> Path:
    files = sorted(folder.glob("*.json"))
    if not files:
        raise SystemExit(f"no results in {folder}: run `make eval` first")
    return files[-1]


def main(argv: list[str] | None = None) -> None:
    from chaain.core.config import load_rules, load_settings
    from chaain.llm import provider_from_config

    parser = argparse.ArgumentParser(prog="notes_eval", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="ask the model about every case, save and score")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--model", help="try another model on the same set")
    sc = sub.add_parser("score", help="score a saved results file (no API call)")
    sc.add_argument("file", nargs="?", type=Path, help="default: the latest in evals/notes/results/")
    args = parser.parse_args(argv)

    cases = load_cases()
    threshold = load_rules()["notes"]["confidence_threshold"]
    if args.command == "run":
        llm = load_settings()["llm"]
        env = dict(os.environ)
        if args.model:
            env[llm.get("model_env", "ANTHROPIC_MODEL")] = args.model
        reader = NotesReader(provider_from_config(llm, env), llm.get("retry_pause_seconds", 2))
        results = collect(reader, cases, args.runs)
        path = save(results)
        print(f"Saved {path.relative_to(EVALS.parents[1])}\n")
    else:
        path = args.file or latest()
        results = json.loads(path.read_text())
    print(report(score(results, cases, threshold), results))


if __name__ == "__main__":
    main()
