"""
Tests for fixing rows in parallel.

A file whose rows each need the same small fix — six identical CTA repairs, say
— used to cost six round trips one after another. Rows share nothing, so they
now go at once. What must survive that: violations on ONE row still run in
order (each rewrites what the next reads), and the record of the run reads the
same however the threads were scheduled.

The fake client here answers from the content of the request rather than a
queue, because under parallelism the order of calls is not fixed.
"""

from __future__ import annotations

import json
import threading
import time

from src.agents import CriticAgent, FixerAgent, TriageAgent
from src.orchestrator import Orchestrator
from src.state import CallMetric, CsvRow, FixOutcome, PipelineState, Platform

CTA = "IG: instagram.com/alex_y_yarvinen"
BAD_CTA = "[IG](https://instagram.com/alex_y_yarvinen)"


class ConcurrentFake:
    """Answers by request content, and records how many calls overlapped."""

    def __init__(self, latency: float = 0.05) -> None:
        self.latency = latency
        self.metrics: list[CallMetric] = []
        self.prompts: list[str] = []
        self.peak_concurrency = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def complete(self, *, model: str, system: str, user: str) -> str:
        with self._lock:
            self.prompts.append(user)
            self._in_flight += 1
            self.peak_concurrency = max(self.peak_concurrency, self._in_flight)
        try:
            time.sleep(self.latency)          # stand in for a round trip
            return self._answer(user)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _answer(self, user: str) -> str:
        payload = json.loads(user)
        if "violations" in payload:           # triage
            return json.dumps({
                "fix_order": [
                    {"rule_id": v["rule_id"], "row_index": v["row_index"]}
                    for v in payload["violations"]
                ],
                "human_only": [],
            })
        # fixer: repair the CTA, leaving the rest of the text alone
        text = payload["row"]["text"]
        return json.dumps({
            "action": "edit_text",
            "new_text": text.replace(BAD_CTA, CTA),
            "reason": "flat URL CTA",
        })


def orchestrator(fake: ConcurrentFake, max_parallel_rows: int) -> Orchestrator:
    return Orchestrator(
        triage=TriageAgent(fake),
        fixer=FixerAgent(fake),
        critic=CriticAgent(fake),
        max_parallel_rows=max_parallel_rows,
    )


def facebook_rows(count: int) -> list[CsvRow]:
    """`count` rows, each with the one CTA violation."""
    return [
        CsvRow(
            row_index=i,
            platform=Platform.FACEBOOK,
            date="2026/06/24 18:00",
            text=f"Post number {i}. {BAD_CTA}",
            label="promo",
        )
        for i in range(count)
    ]


def test_rows_are_fixed_at_the_same_time():
    fake = ConcurrentFake(latency=0.05)
    state = PipelineState(rows=facebook_rows(6))

    state = orchestrator(fake, max_parallel_rows=6).run(state)

    assert fake.peak_concurrency > 1, "rows were still fixed one at a time"
    assert [a.outcome for a in state.attempts] == [FixOutcome.FIXED] * 6


def test_parallel_fixing_is_faster_than_one_at_a_time():
    rows, latency = 6, 0.05

    slow = ConcurrentFake(latency)
    orchestrator(slow, max_parallel_rows=1).run(PipelineState(rows=facebook_rows(rows)))

    quick = ConcurrentFake(latency)
    started = time.perf_counter()
    orchestrator(quick, max_parallel_rows=rows).run(PipelineState(rows=facebook_rows(rows)))
    parallel = time.perf_counter() - started

    assert slow.peak_concurrency == 1
    # Six fixes plus one triage call; in parallel that is roughly two rounds.
    assert parallel < rows * latency


def test_the_cap_is_respected():
    fake = ConcurrentFake(latency=0.05)
    orchestrator(fake, max_parallel_rows=2).run(PipelineState(rows=facebook_rows(8)))
    assert fake.peak_concurrency <= 2


def test_two_violations_on_one_row_stay_in_order():
    """The second fix has to read what the first one wrote, so a row is never
    split across threads."""
    long_tail = "word " * 70
    row = CsvRow(
        row_index=0,
        platform=Platform.TWITTER,
        date="2026/06/24 18:00",
        text=f"{long_tail}{BAD_CTA}",
        label="promo",
    )
    assert len(row.text) > 280               # twitter_length AND cta_format

    class Sequential(ConcurrentFake):
        def _answer(self, user: str) -> str:
            payload = json.loads(user)
            if "violations" in payload:
                return super()._answer(user)
            rule = payload["violation"]["rule_id"]
            text = payload["row"]["text"]
            if rule == "twitter_length":
                return json.dumps({
                    "action": "edit_text",
                    "new_text": f"Trimmed. {BAD_CTA}",
                    "reason": "under 280",
                })
            return json.dumps({
                "action": "edit_text",
                "new_text": text.replace(BAD_CTA, CTA),
                "reason": "flat URL CTA",
            })

    fake = Sequential(latency=0.0)
    state = orchestrator(fake, max_parallel_rows=4).run(PipelineState(rows=[row]))

    assert fake.peak_concurrency == 1, "one row must not be fixed in parallel"
    # The CTA fix saw the trimmed text, not the original.
    assert state.rows[0].text == f"Trimmed. {CTA}"


def test_the_record_of_the_run_is_ordered_by_row_not_by_luck():
    """Threads finish in whatever order they like; the report must not."""
    fake = ConcurrentFake(latency=0.02)
    state = orchestrator(fake, max_parallel_rows=6).run(PipelineState(rows=facebook_rows(6)))

    assert [a.row_index for a in state.attempts] == list(range(6))
    fixed = [e for e in state.trace if e.detail.startswith("Fix ACCEPTED")]
    assert [e.detail.split("@row")[1].split()[0] for e in fixed] == [str(i) for i in range(6)]


def test_every_row_is_committed():
    fake = ConcurrentFake(latency=0.01)
    state = orchestrator(fake, max_parallel_rows=6).run(PipelineState(rows=facebook_rows(6)))

    assert all(BAD_CTA not in r.text for r in state.rows)
    assert all(CTA in r.text for r in state.rows)
    assert state.final_violations == []
