# Simulated-session testing and latency analysis

**Subject:** Trading Framework v3 - the agent engine (Module 2), the notebook
feature, and the whole decision-to-archive chain.
**Date:** 2026-10-11 (Saturday evening; the real market closed, which is the
point of the exercise).
**Method:** fake market + scripted model, real Modules 2, 3 and 4.
**Raw artefacts:** `logs/sim/<stamp>/report.md`, `report.json`,
`artifacts/model_requests.jsonl`, `artifacts/m1_requests.jsonl`, and each
module's own logs, for every run listed in Appendix A.

---

## 1. What was asked, and what this proves

The operator asked for thorough testing of the notebook feature *and everything
else*, with fake data, because the market is closed at night - and for a proper
latency analysis of each stage of a cycle.

What is proven here:

* the **notebook** reaches the model on every call, is written on every
  deciding cycle, survives restarts, and its failure path (`notes_missing`)
  leaves the decision and the order intact;
* a full cycle runs the **production code path**: Module 2's control service
  starts the agent as its own child process, the real prompt is built, the real
  tools call a real (fake) Module 1 over HTTP, the decision is recorded, handed
  to a real Module 3, filled at a live price, closed when the market moves, and
  archived by a real Module 4;
* **where the time goes**, stage by stage, measured rather than assumed - and
  the measured case for batching tool calls instead of spawning more agents.

## 2. How it was tested

| piece | file | what it is |
|---|---|---|
| fake market | `logs/fake_module1.py` | Module 1's HTTP contract with synthetic, deterministic prices; a session clock that says "open"; a new closed candle every minute; settable per-request latency |
| scripted model | `logs/fake_model.py` | OpenAI-compatible `/chat/completions`; plays a believable cycle (fields+screen -> 3 stocks + 3 candle series in one round -> decision -> notebook write); logs whether the digest and the reminder were in the prompt |
| session harness | `logs/sim_session.py` | starts the fakes and the **real** Modules 3, 4 and Module 2 (autostart), waits for N cycles, stops everything, writes the report |
| analysis | `logs/latency_report.py` | per-cycle stage latency, tool parallelism, fake-market per-route latency, the chain, and the pass/fail checks |

Reproduce any run:

```powershell
cd v3
uv run python logs/sim_session.py --cycles 6 --model-latency-ms 350 --m1-latency-ms 40
uv run python logs/latency_report.py logs/sim/<stamp>     # re-print from the raw logs
```

The failure path is a first-class run, not a unit test: add
`--expect-missing-notes` and the scripted model refuses the notebook, so the
framework must decide anyway, record `notes_missing`, and keep the order.

Environment for the runs below: Windows, the project virtualenv, everything on
127.0.0.1, one-minute candles (`MODULE2_CANDLE_SETTLE_SECONDS=1`), the operator
window opened to the whole day so the fake exchange counts as a session.

## 3. Results at a glance

| run (stamp) | purpose | cycles | model / market latency | checks |
|---|---|---|---|---|
| 20261011-171039 | shakedown - found the scripted-model loop | 2 | 300 / 40 ms | 6 of 9 (failures were the test's, not the framework's) |
| 20261011-171256 | shakedown after the script fix | 1 | 300 / 40 ms | 9 of 9 |
| 20261011-171426 | first full run | 6 | 350 / 40 ms | 9 of 9 |
| 20261011-172344 | full chain; one order closed | 4 | 350 / 40 ms | 9 of 9 |
| 20261011-173343 | final happy run | 3 | 350 / 40 ms | 9 of 9 |
| 20261011-172722 | slow market | 3 | 350 / 200 ms | 9 of 9 |
| 20261011-173621 | model refuses the notebook | 2 | 250 / 40 ms | 9 of 9 (the degraded path, as configured) |

Superseded runs are kept in Appendix A for completeness: 20261011-172032 (slow
market, before the fake market measured whole requests) and 20261011-173102
(refusal, before the reminder counter was made exact).

Every check in the nine-point list passed on all final runs: cycles run, every
cycle decided, the notebook update happened, the digest reached the model, the
reminder fired when needed, no failure events, orders reached Module 3, Module 4
archived them, and the notebook persisted.

## 4. Where the time goes

Primary run: **20261011-171426**, six cycles, simulated model latency 350 ms
(±40), fake market 40 ms per request. All times in milliseconds.

| stage | n | p50 | p90 | max | what it is |
|---|---|---|---|---|---|
| wake | 6 | 1172 | 1181 | 1187 | bar close -> first model call (1 s settle + bar-ready wait) |
| model | 6 | 1324 | 1428 | 1475 | sum of the cycle's model calls (3-4 calls) |
| tools (wall) | 6 | 135 | 147 | 158 | 10 tool calls in 3-4 parallel rounds |
| overhead | 6 | 120 | 128 | 144 | prompt assembly, book read, decision record, notebook write |
| **cycle** | 6 | **1567** | 1676 | 1777 | wake is excluded; this is first call -> cycle end |
| sleep | 5 | 58384 | 58654 | 58706 | cycle end -> next boundary (one-minute candles) |

Read it as a budget for a one-minute agent: about **1.6 s of work in a 60 s
cycle**, of which the model is ~1.3 s, the tools ~0.14 s and the framework's own
orchestration ~0.12 s. A 15-minute agent has the same ~1.6 s of work and
~13.5 minutes of sleep.

The wake stage is a fact, not a sleep: the agent wakes at the boundary, waits
for the settle buffer, asks Module 1 whether the bar is finished, and starts the
moment it is - 1.17 s end to end here (1 s of that is the configured settle).

### Tool calls run in parallel - measured

Ten tool calls per cycle, asked for as two multi-call rounds. The report sums
each call's own duration ("serial") and takes the round's slowest call as the
round's wall time ("parallel"):

| run | market latency | tool time, one after another | tool time, run together | speed-up |
|---|---|---|---|---|
| 20261011-172344 | 40 ms | 0.43-0.53 s | 0.14-0.15 s | ~3.4x |
| 20261011-172722 | 200 ms | 1.79-1.92 s | 0.45-0.48 s | ~3.9x |

Slowing the fake market five-fold grew the serial tool work five-fold, but the
round's wall time only tripled - because a round's calls overlap. This is the
measured answer to "should agents spawn helpers to go faster": batching the
calls they already make is free and already implemented; spawning agents
multiplies model calls, which is the dominant cost.

### The fake market, as the tools see it

Configured latency vs observed per route (p50):

| route | 40 ms configured | 200 ms configured |
|---|---|---|
| `/v1/market/bar-status` | 55 ms | 212 ms |
| `/v1/market/screen` | 50 ms | 214 ms |
| `/v1/market/symbol/{symbol}` | 52 ms | 224 ms |
| `/v1/market/candles` | 45 ms | 220 ms |
| `/v1/market/prices` | 48 ms | 205 ms |
| `/v1/market/status` | 49 ms | 212 ms |

The configured delay lands where the tool instrumentation says it should, which
is what makes the tool-stage numbers trustworthy. Module 3's own watcher also
polls the prices of everything it holds every two seconds (one request for the
whole set), so the market sees more traffic than the agent's own calls - but
those polls run in Module 3's process and did not slow a cycle.

### Tokens, per run

| run | cycles | model calls | tool calls | prompt tokens | completion tokens |
|---|---|---|---|---|---|
| 20261011-171426 | 6 | 21 | 60 | 56 052 | 900 |
| 20261011-172344 | 4 | 14 | 40 | 37 158 | 600 |
| 20261011-173343 | 3 | 11 | 30 | 29 720 | 460 |

About 9-10k prompt tokens per cycle in the simulated cycles - dominated by the
tool results (three stock bundles and three candle series), which is why the
prompt keeps the *system* half byte-identical and lets the provider's prompt
cache work on it.

### For contrast: a real recorded session

From `data/agents/nifty-intraday/activity.jsonl` (real Module 1, real model,
5-minute candles, 33 cycles on 2026-09-30 to 10-05): median cycle **67 s**,
p90 92 s, max 205 s, median 15 tool calls. The framework's own share measured
above (~0.25 s) is noise next to a real model; the reason to keep cycles small
is the model's thinking time, and the reason to keep tool calls batched is the
market round trips behind them.

## 5. The notebook, end to end

| evidence | value (final happy run) |
|---|---|
| model calls carrying the notebook digest | 11 of 11 |
| deciding cycles that wrote a note | 3 of 3 (`notes_missing`: 0) |
| notebook at rest | 2 keys, 3 edits, 112 characters |
| reminder-driven calls (the mandatory nudge) | 2 - one per odd cycle |

The scripted model deliberately uses different paths: on even cycles it submits
the decision and the notebook write in the same round (the cycle ends
immediately, no extra model call); on odd cycles it decides alone and writes
only after the worker's mandatory reminder. Every third cycle writes a keyed
lesson; every sixth deletes one, so `write`, `append` and `delete` are all
exercised by an ordinary run.

The failure path was run on purpose (**20261011-173621**): the model answered
the reminder with prose, twice per cycle, and refused to write. The framework
behaved exactly as designed - 2 of 2 cycles recorded `notes_missing`, 0 writes,
no notebook file content, and both orders were still recorded, filled and
archived. A missing note never removes a decision; it is recorded as a silence
the operator can see.

## 6. The chain behind Module 2

| run | decisions | orders in Module 3 | filled | statuses | archived in Module 4 |
|---|---|---|---|---|---|
| 20261011-171426 | 6 | 6 | 6 | 6 open | 6 |
| 20261011-172344 | 4 | 4 | 4 | 1 closed, 3 open | 4 |
| 20261011-173343 | 3 | 3 | 3 | 3 open | 3 |
| 20261011-173621 (refusal) | 2 | 2 | 2 | 2 open | 2 |

The 4-cycle run is the important one: the synthetic price walked far enough for
one order to reach its exit on its own, so the run covers decision -> fill at
the first live price -> target/stop/timeout exit -> settled P&L -> daily book ->
long archive. Nothing in that path was simulated except the price series.

## 7. Defects found by this testing, and fixed

1. **A fresh agent decided a candle that closed before it existed.** The first
   shakedown (20261011-171039) shows a cycle waking **43.7 s** after its bar
   closed, because a restarted agent read the most recently closed bar instead
   of waiting for the next boundary - for a 15-minute agent that would be up to
   ~14 minutes of stale tape, and it contradicts the documented behaviour (and
   the README). `AgentWorker.run()` now waits for the next decision boundary
   before its first look, unless the day has no decision left, in which case it
   says so and stops. Three tests cover it; the next run's wake stage is 1.16 s
   (the configured settle) instead of 43.7 s.
2. **Latency was invisible in the logs.** `model_reply` now records
   `latency_ms`, the tool-call count and token usage; `tool_result` records its
   own `ms`. Without them no stage analysis is possible; with them the report
   above is a read of the log, not a reconstruction.
3. **The empty-notebook digest collided with the mandatory reminder's wording**
   ("Before you sleep..."), which inflated the scripted model's reminder count
   and made a check lie. The digest was reworded and the checker now matches the
   reminder by its opening words only.
4. **The report counted edits on surviving keys**, so a legitimate `delete`
   made a correct run look wrong. It now counts write events per cycle, which is
   what the mandatory rule is about.
5. **The fake market logged handler time, not request time**, reporting 0.3 ms
   for a request that took 40 ms. The middleware now measures the whole request,
   which is why section 4's market table adds up.

## 8. Caveats - what this does *not* prove

* The model is **scripted**, not an LLM: this proves the framework's plumbing,
  its timing and its audit trail, not the quality of a model's reasoning. The
  first real-model session after the weekend is the first test of that.
* One-minute candles were used to fit several cycles into minutes; real agents
  run 5-15 minute timeframes where sleep dominates, as the real-session numbers
  above show.
* All processes are on loopback; a cloud machine's HTTP and provider latency
  will differ. The instrumented events (`model_reply`, `tool_result`) travel
  with every real run, so the same report can be produced from production logs
  unchanged.
* The refusal path was exercised with a scripted refusal. A real model that
  forgets the notebook will produce the same `notes_missing` event; that has
  not happened in production yet because the feature is new.

## 9. Verification of the surrounding work

* `uv run pytest` - **589 passed** (211 in Module 2, including the notebook,
  the mandatory-loop tests and the three new fresh-start tests).
* `uv run ruff check .` - clean.
* `npm run build` in `ui/` - clean.
* `node tests/smoke.mjs` - the full Chromium walk (six tabs, market table,
  inspector, Control tab, log viewer) against the real framework: no problems,
  no unexpected console errors.

---

# Appendix A - every run, in full

Each entry is the `report.md` the harness wrote for that run, unchanged except
for heading depth. `logs/sim/<stamp>/report.json` holds the same data in
machine-readable form, and `logs/sim/` also keeps the raw request logs
(`artifacts/model_requests.jsonl`, `artifacts/m1_requests.jsonl`) and each
module's own log. **`logs/` is not tracked by git**, so this document is the
durable copy of all of it.

<!-- RUN-REPORTS -->

## Run 20261011-171039

*Shakedown: the scripted model looped on the notebook instead of deciding. Kept because it is the run that found the loop - the failures are the test script's, not the framework's.*

### Simulated session report

- cycles: 1 · timeframe 1m · model latency 300.0ms · market latency 40.0ms
- started: 2026-10-11T17:10:39.038935+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 2 of 1 requested |
| every_cycle_decided | FAIL | 0 decisions over 2 cycles |
| notebook_update_mandatory | PASS | 10 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 12 of 12 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 8 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | FAIL | 0 orders, 0 filled, statuses {} |
| module4_archived | FAIL | 0 orders in the archive |
| notebook_persisted | PASS | 1 keys, 10 edits, 229 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 2 | 22423.0 | 1160.0 | 43686.0 | 44846.0 |
| model_ms | 2 | 1697.9 | 912.2 | 2483.6 | 3395.8 |
| tool_wall_ms | 2 | 96.2 | 48.2 | 144.1 | 192.3 |
| overhead_ms | 2 | 32.5 | 32.3 | 32.6 | 64.9 |
| cycle_ms | 2 | 1826.5 | 993.0 | 2660.0 | 3653.0 |
| sleep_ms | 1 | 14814.0 | 14814.0 | 14814.0 | 14814.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:09 | 43686.0 | 2483.6 (8) | 8, 144.1 | 32.3 | 2660.0 | 14814.0 | 7 |
| 17:10 | 1160.0 | 912.2 (3) | 3, 48.2 | 32.6 | 993.0 | None | 3 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 2 | 0.4 | 0.2 | 0.6 |
| /v1/market/status | 4 | 0.3 | 0.3 | 0.7 |
| /v1/market/symbol/SIM03 | 1 | 0.9 | 0.9 | 0.9 |

#### The chain behind Module 2

- 0 decisions recorded · 0 orders in Module 3 (0 filled) · 0 archived in Module 4
- notebook: 1 keys, 10 edits, 229 characters
- model: 11 calls · 14022 prompt tokens · 308 completion tokens
- tool calls: 11 · model kinds: {'forced-note': 11, 'decide': 1}

---

## Run 20261011-171256

*Shakedown after the script fix: one cycle, all green.*

### Simulated session report

- cycles: 1 · timeframe 1m · model latency 300.0ms · market latency 40.0ms
- started: 2026-10-11T17:12:56.543362+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 1 of 1 requested |
| every_cycle_decided | PASS | 1 decisions over 1 cycles |
| notebook_update_mandatory | PASS | 1 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 4 of 4 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 4 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 1 orders, 1 filled, statuses {'open': 1} |
| module4_archived | PASS | 1 orders in the archive |
| notebook_persisted | PASS | 1 keys, 1 edits, 22 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 1 | 1174.0 | 1174.0 | 1174.0 | 1174.0 |
| model_ms | 1 | 1215.4 | 1215.4 | 1215.4 | 1215.4 |
| tool_wall_ms | 1 | 150.1 | 150.1 | 150.1 | 150.1 |
| overhead_ms | 1 | 128.5 | 128.5 | 128.5 | 128.5 |
| cycle_ms | 1 | 1494.0 | 1494.0 | 1494.0 | 1494.0 |
| sleep_ms | 0 | None | None | None | None |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:13 | 1174.0 | 1215.4 (4) | 10, 150.1 | 128.5 | 1494.0 | None | 1 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 1 | 0.2 | 0.2 | 0.2 |
| /v1/market/candles | 3 | 2.2 | 2.2 | 2.7 |
| /v1/market/fields | 1 | 0.2 | 0.2 | 0.2 |
| /v1/market/prices | 9 | 0.3 | 0.4 | 0.9 |
| /v1/market/screen | 1 | 0.6 | 0.6 | 0.6 |
| /v1/market/status | 4 | 0.6 | 0.8 | 0.9 |
| /v1/market/symbol/SIM07 | 1 | 4.1 | 4.1 | 4.1 |
| /v1/market/symbol/SIM24 | 1 | 1.4 | 1.4 | 1.4 |
| /v1/market/symbol/SIM35 | 1 | 1.0 | 1.0 | 1.0 |

#### The chain behind Module 2

- 1 decisions recorded · 1 orders in Module 3 (1 filled) · 1 archived in Module 4
- notebook: 1 keys, 1 edits, 22 characters
- model: 4 calls · 11232 prompt tokens · 160 completion tokens
- tool calls: 10 · model kinds: {'open': 1, 'inspect': 1, 'decide': 1, 'forced-note': 1}

---

## Run 20261011-171426

*First full run (6 cycles). The market-request table below predates the fake market's whole-request timing fix; the framework stages are valid.*

### Simulated session report

- cycles: 6 · timeframe 1m · model latency 350.0ms · market latency 40.0ms
- started: 2026-10-11T17:14:26.348193+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 6 of 6 requested |
| every_cycle_decided | PASS | 6 decisions over 6 cycles |
| notebook_update_mandatory | PASS | 6 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 21 of 21 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 6 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 6 orders, 6 filled, statuses {'open': 6} |
| module4_archived | PASS | 6 orders in the archive |
| notebook_persisted | PASS | 6 writes over 6 cycles; 1 keys left, 4 edits on them, 91 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 6 | 1172.0 | 1181.0 | 1187.0 | 7042.0 |
| model_ms | 6 | 1323.6 | 1428.2 | 1474.7 | 7736.1 |
| tool_wall_ms | 6 | 135.3 | 147.0 | 157.9 | 819.1 |
| overhead_ms | 6 | 120.2 | 128.2 | 144.4 | 690.8 |
| cycle_ms | 6 | 1567.0 | 1676.0 | 1777.0 | 9246.0 |
| sleep_ms | 5 | 58384.0 | 58654.0 | 58706.0 | 292282.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:14 | 1160.0 | 1428.2 (4) | 10, 112.0 | 86.8 | 1627.0 | 58384.0 | 1 |
| 17:15 | 1171.0 | 1097.5 (3) | 10, 137.1 | 113.4 | 1348.0 | 58654.0 | 1 |
| 17:16 | 1173.0 | 1474.7 (4) | 10, 157.9 | 144.4 | 1777.0 | 58220.0 | 1 |
| 17:17 | 1170.0 | 1088.5 (3) | 10, 131.6 | 90.9 | 1311.0 | 58706.0 | 1 |
| 17:18 | 1187.0 | 1414.3 (4) | 10, 133.5 | 128.2 | 1676.0 | 58318.0 | 1 |
| 17:19 | 1181.0 | 1232.9 (3) | 10, 147.0 | 127.1 | 1507.0 | None | 1 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 6 | 0.6 | 0.9 | 0.9 |
| /v1/market/candles | 18 | 0.9 | 3.3 | 6.0 |
| /v1/market/fields | 6 | 0.3 | 0.3 | 0.8 |
| /v1/market/prices | 160 | 0.3 | 0.9 | 1.2 |
| /v1/market/screen | 6 | 0.7 | 1.3 | 1.5 |
| /v1/market/status | 14 | 0.3 | 0.5 | 0.9 |
| /v1/market/symbol/SIM01 | 2 | 0.7 | 0.4 | 0.9 |
| /v1/market/symbol/SIM06 | 1 | 6.0 | 6.0 | 6.0 |
| /v1/market/symbol/SIM11 | 1 | 0.6 | 0.6 | 0.6 |
| /v1/market/symbol/SIM12 | 1 | 1.9 | 1.9 | 1.9 |
| /v1/market/symbol/SIM17 | 1 | 0.3 | 0.3 | 0.3 |
| /v1/market/symbol/SIM18 | 1 | 1.1 | 1.1 | 1.1 |
| /v1/market/symbol/SIM23 | 2 | 2.8 | 1.4 | 4.2 |
| /v1/market/symbol/SIM28 | 1 | 0.6 | 0.6 | 0.6 |
| /v1/market/symbol/SIM29 | 1 | 0.8 | 0.8 | 0.8 |
| /v1/market/symbol/SIM34 | 2 | 0.9 | 0.8 | 1.0 |
| /v1/market/symbol/SIM35 | 1 | 1.1 | 1.1 | 1.1 |
| /v1/market/symbol/SIM40 | 1 | 1.7 | 1.7 | 1.7 |
| /v1/market/symbol/SIM45 | 2 | 0.8 | 0.6 | 0.9 |
| /v1/market/symbol/SIM46 | 1 | 1.0 | 1.0 | 1.0 |

#### The chain behind Module 2

- 6 decisions recorded · 6 orders in Module 3 (6 filled) · 6 archived in Module 4
- notebook: 1 keys, 4 edits, 91 characters
- model: 21 calls · 56052 prompt tokens · 900 completion tokens
- tool calls: 60 · model kinds: {'open': 6, 'inspect': 6, 'decide': 3, 'forced-note': 3, 'decide-and-note': 3}

---

## Run 20261011-172032

*Slow-market run before the fake market measured whole requests; superseded by 20261011-172722.*

### Simulated session report

- cycles: 3 · timeframe 1m · model latency 350.0ms · market latency 200.0ms
- started: 2026-10-11T17:20:32.373861+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 3 of 3 requested |
| every_cycle_decided | PASS | 3 decisions over 3 cycles |
| notebook_update_mandatory | PASS | 3 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 11 of 11 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 5 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 3 orders, 3 filled, statuses {'open': 3} |
| module4_archived | PASS | 3 orders in the archive |
| notebook_persisted | PASS | 3 writes over 3 cycles; 2 keys left, 3 edits on them, 112 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 3 | 1673.0 | 1673.0 | 1688.0 | 5021.0 |
| model_ms | 3 | 1445.0 | 1445.0 | 1446.1 | 3989.6 |
| tool_wall_ms | 3 | 454.4 | 454.4 | 462.4 | 1362.0 |
| overhead_ms | 3 | 315.8 | 315.8 | 316.5 | 932.4 |
| cycle_ms | 3 | 2206.0 | 2206.0 | 2217.0 | 6284.0 |
| sleep_ms | 2 | 57947.0 | 57768.0 | 58126.0 | 115894.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:20 | 1688.0 | 1446.1 (4) | 10, 454.4 | 316.5 | 2217.0 | 57768.0 | 1 |
| 17:21 | 1673.0 | 1098.5 (3) | 10, 462.4 | 300.1 | 1861.0 | 58126.0 | 1 |
| 17:22 | 1660.0 | 1445.0 (4) | 10, 445.2 | 315.8 | 2206.0 | None | 1 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 3 | 0.9 | 0.9 | 0.9 |
| /v1/market/candles | 9 | 0.5 | 1.1 | 1.1 |
| /v1/market/fields | 3 | 0.7 | 0.7 | 3.1 |
| /v1/market/prices | 64 | 0.9 | 1.0 | 1.4 |
| /v1/market/screen | 3 | 0.7 | 0.7 | 3.6 |
| /v1/market/status | 8 | 0.8 | 1.0 | 1.0 |
| /v1/market/symbol/SIM05 | 1 | 0.5 | 0.5 | 0.5 |
| /v1/market/symbol/SIM11 | 1 | 0.3 | 0.3 | 0.3 |
| /v1/market/symbol/SIM16 | 1 | 0.6 | 0.6 | 0.6 |
| /v1/market/symbol/SIM22 | 1 | 0.2 | 0.2 | 0.2 |
| /v1/market/symbol/SIM33 | 2 | 0.6 | 0.5 | 0.7 |
| /v1/market/symbol/SIM39 | 1 | 0.7 | 0.7 | 0.7 |
| /v1/market/symbol/SIM44 | 1 | 0.6 | 0.6 | 0.6 |
| /v1/market/symbol/SIM50 | 1 | 0.8 | 0.8 | 0.8 |

#### The chain behind Module 2

- 3 decisions recorded · 3 orders in Module 3 (3 filled) · 3 archived in Module 4
- notebook: 2 keys, 3 edits, 112 characters
- model: 11 calls · 29863 prompt tokens · 460 completion tokens
- tool calls: 30 · model kinds: {'open': 3, 'inspect': 3, 'decide': 2, 'forced-note': 2, 'decide-and-note': 1}

---

## Run 20261011-172344

*Full chain, 4 cycles; one order reached its exit and closed.*

### Simulated session report

- cycles: 4 · timeframe 1m · model latency 350.0ms · market latency 40.0ms
- started: 2026-10-11T17:23:44.283357+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 4 of 4 requested |
| every_cycle_decided | PASS | 4 decisions over 4 cycles |
| notebook_update_mandatory | PASS | 4 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 14 of 14 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 5 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 4 orders, 4 filled, statuses {'closed': 1, 'open': 3} |
| module4_archived | PASS | 4 orders in the archive |
| notebook_persisted | PASS | 4 writes over 4 cycles; 2 keys left, 4 edits on them, 135 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 4 | 1184.0 | 1189.0 | 1200.0 | 4732.0 |
| model_ms | 4 | 1255.9 | 1423.1 | 1483.6 | 5054.8 |
| tool_wall_ms | 4 | 137.6 | 139.8 | 151.2 | 561.4 |
| overhead_ms | 4 | 134.0 | 135.6 | 141.6 | 538.8 |
| cycle_ms | 4 | 1530.5 | 1700.0 | 1764.0 | 6155.0 |
| sleep_ms | 3 | 58311.0 | 58311.0 | 58664.0 | 175196.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:23 | 1179.0 | 1483.6 (4) | 10, 151.2 | 129.2 | 1764.0 | 58221.0 | 1 |
| 17:24 | 1164.0 | 1088.8 (3) | 10, 139.8 | 132.4 | 1361.0 | 58664.0 | 1 |
| 17:25 | 1189.0 | 1423.1 (4) | 10, 135.3 | 141.6 | 1700.0 | 58311.0 | 1 |
| 17:26 | 1200.0 | 1059.3 (3) | 10, 135.1 | 135.6 | 1330.0 | None | 1 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 4 | 55.2 | 55.5 | 56.7 |
| /v1/market/candles | 12 | 44.8 | 56.7 | 60.8 |
| /v1/market/fields | 4 | 54.6 | 55.1 | 56.5 |
| /v1/market/prices | 98 | 47.8 | 49.7 | 51.3 |
| /v1/market/prices/stream | 4 | 46.8 | 47.6 | 48.8 |
| /v1/market/screen | 4 | 49.6 | 50.5 | 51.2 |
| /v1/market/status | 10 | 49.2 | 54.6 | 57.1 |
| /v1/market/symbol/{symbol} | 12 | 52.4 | 62.7 | 65.0 |

#### The chain behind Module 2

- 4 decisions recorded · 4 orders in Module 3 (4 filled) · 4 archived in Module 4
- notebook: 2 keys, 4 edits, 135 characters
- model: 14 calls · 37158 prompt tokens · 600 completion tokens
- tool calls: 40 · model kinds: {'open': 4, 'inspect': 4, 'decide': 2, 'forced-note': 2, 'decide-and-note': 2}

---

## Run 20261011-172722

*Slow market (200 ms per request): the parallelism comparison in section 4 of the report.*

### Simulated session report

- cycles: 3 · timeframe 1m · model latency 350.0ms · market latency 200.0ms
- started: 2026-10-11T17:27:22.132463+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 3 of 3 requested |
| every_cycle_decided | PASS | 3 decisions over 3 cycles |
| notebook_update_mandatory | PASS | 3 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 11 of 11 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 5 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 3 orders, 3 filled, statuses {'open': 3} |
| module4_archived | PASS | 3 orders in the archive |
| notebook_persisted | PASS | 3 writes over 3 cycles; 2 keys left, 3 edits on them, 112 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 3 | 1660.0 | 1660.0 | 1684.0 | 4970.0 |
| model_ms | 3 | 1462.6 | 1462.6 | 1740.6 | 4331.1 |
| tool_wall_ms | 3 | 479.4 | 479.4 | 480.4 | 1409.5 |
| overhead_ms | 3 | 323.7 | 323.7 | 326.0 | 895.4 |
| cycle_ms | 3 | 2268.0 | 2268.0 | 2436.0 | 6636.0 |
| sleep_ms | 2 | 57883.0 | 57756.0 | 58010.0 | 115766.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:27 | 1660.0 | 1462.6 (4) | 10, 479.4 | 326.0 | 2268.0 | 57756.0 | 1 |
| 17:28 | 1684.0 | 1127.9 (3) | 10, 480.4 | 323.7 | 1932.0 | 58010.0 | 1 |
| 17:29 | 1626.0 | 1740.6 (4) | 10, 449.7 | 245.7 | 2436.0 | None | 1 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 3 | 212.3 | 212.3 | 214.9 |
| /v1/market/candles | 9 | 219.9 | 241.2 | 242.6 |
| /v1/market/fields | 3 | 216.3 | 216.3 | 228.7 |
| /v1/market/prices | 64 | 205.2 | 219.2 | 219.9 |
| /v1/market/prices/stream | 3 | 207.4 | 207.4 | 217.9 |
| /v1/market/screen | 3 | 213.6 | 213.6 | 216.4 |
| /v1/market/status | 8 | 211.8 | 218.6 | 218.8 |
| /v1/market/symbol/{symbol} | 9 | 224.4 | 237.6 | 239.5 |

#### The chain behind Module 2

- 3 decisions recorded · 3 orders in Module 3 (3 filled) · 3 archived in Module 4
- notebook: 2 keys, 3 edits, 112 characters
- model: 11 calls · 29767 prompt tokens · 460 completion tokens
- tool calls: 30 · model kinds: {'open': 3, 'inspect': 3, 'decide': 2, 'forced-note': 2, 'decide-and-note': 1}

---

## Run 20261011-173102

*First refusal run. The reminder counter was made exact afterwards, so its 'reminder' figure is inflated; the notebook and order results stand.*

### Simulated session report

- cycles: 2 · timeframe 1m · model latency 250.0ms · market latency 40.0ms
- started: 2026-10-11T17:31:02.174724+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 2 of 2 requested |
| every_cycle_decided | PASS | 2 decisions over 2 cycles |
| notebook_update_mandatory | PASS | the notebook was refused on purpose: 2 of 2 deciding cycles recorded notes_missing, 0 writes |
| digest_reached_the_model | PASS | 10 of 10 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 10 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 2 orders, 2 filled, statuses {'open': 2} |
| module4_archived | PASS | 2 orders in the archive |
| notebook_persisted | PASS | 0 keys left (expected none, the model refused) |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 2 | 1158.5 | 1156.0 | 1161.0 | 2317.0 |
| model_ms | 2 | 1383.8 | 1361.7 | 1405.8 | 2767.5 |
| tool_wall_ms | 2 | 114.9 | 98.8 | 131.1 | 229.9 |
| overhead_ms | 2 | 104.8 | 104.4 | 105.2 | 209.6 |
| cycle_ms | 2 | 1603.5 | 1598.0 | 1609.0 | 3207.0 |
| sleep_ms | 1 | 58396.0 | 58396.0 | 58396.0 | 58396.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:31 | 1156.0 | 1405.8 (5) | 9, 98.8 | 104.4 | 1609.0 | 58396.0 | 0 |
| 17:32 | 1161.0 | 1361.7 (5) | 9, 131.1 | 105.2 | 1598.0 | None | 0 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 2 | 45.5 | 45.0 | 45.9 |
| /v1/market/candles | 6 | 47.5 | 57.1 | 58.3 |
| /v1/market/fields | 2 | 44.3 | 43.3 | 45.3 |
| /v1/market/prices | 39 | 47.4 | 49.0 | 53.4 |
| /v1/market/prices/stream | 2 | 47.8 | 45.7 | 49.9 |
| /v1/market/screen | 2 | 46.3 | 45.6 | 47.0 |
| /v1/market/status | 6 | 46.7 | 47.7 | 52.7 |
| /v1/market/symbol/{symbol} | 6 | 52.7 | 59.1 | 61.6 |

#### The chain behind Module 2

- 2 decisions recorded · 2 orders in Module 3 (2 filled) · 2 archived in Module 4
- notebook: 0 keys, 0 edits, 0 characters
- model: 10 calls · 31156 prompt tokens · 344 completion tokens
- tool calls: 18 · model kinds: {'open': 2, 'inspect': 2, 'decide': 2, 'forgot-note': 4}

---

## Run 20261011-173343

*Final happy run (3 cycles).*

### Simulated session report

- cycles: 3 · timeframe 1m · model latency 350.0ms · market latency 40.0ms
- started: 2026-10-11T17:33:43.309861+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 3 of 3 requested |
| every_cycle_decided | PASS | 3 decisions over 3 cycles |
| notebook_update_mandatory | PASS | 3 notebook writes, 0 cycles missing one |
| digest_reached_the_model | PASS | 11 of 11 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 2 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 3 orders, 3 filled, statuses {'open': 3} |
| module4_archived | PASS | 3 orders in the archive |
| notebook_persisted | PASS | 3 writes over 3 cycles; 2 keys left, 3 edits on them, 112 characters |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 3 | 1181.0 | 1181.0 | 1191.0 | 3547.0 |
| model_ms | 3 | 1396.6 | 1396.6 | 1416.9 | 3815.1 |
| tool_wall_ms | 3 | 133.6 | 133.6 | 133.7 | 383.6 |
| overhead_ms | 3 | 124.8 | 124.8 | 136.8 | 377.3 |
| cycle_ms | 3 | 1646.0 | 1646.0 | 1670.0 | 4576.0 |
| sleep_ms | 2 | 58530.0 | 58314.0 | 58746.0 | 117060.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:33 | 1191.0 | 1416.9 (4) | 10, 116.3 | 136.8 | 1670.0 | 58314.0 | 1 |
| 17:34 | 1175.0 | 1001.6 (3) | 10, 133.6 | 124.8 | 1260.0 | 58746.0 | 1 |
| 17:35 | 1181.0 | 1396.6 (4) | 10, 133.7 | 115.7 | 1646.0 | None | 1 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 3 | 56.8 | 56.8 | 59.9 |
| /v1/market/candles | 9 | 53.4 | 56.8 | 57.0 |
| /v1/market/fields | 3 | 44.0 | 44.0 | 44.7 |
| /v1/market/prices | 68 | 48.0 | 49.8 | 57.0 |
| /v1/market/prices/stream | 3 | 44.4 | 44.4 | 47.2 |
| /v1/market/screen | 3 | 49.9 | 49.9 | 51.6 |
| /v1/market/status | 8 | 47.1 | 53.5 | 56.4 |
| /v1/market/symbol/{symbol} | 9 | 57.5 | 58.0 | 61.4 |

#### The chain behind Module 2

- 3 decisions recorded · 3 orders in Module 3 (3 filled) · 3 archived in Module 4
- notebook: 2 keys, 3 edits, 112 characters
- model: 11 calls · 29720 prompt tokens · 460 completion tokens
- tool calls: 30 · model kinds: {'open': 3, 'inspect': 3, 'decide': 2, 'forced-note': 2, 'decide-and-note': 1}

---

## Run 20261011-173621

*Final refusal run: the scripted model refuses the notebook on purpose, and the framework records the silence without losing the orders.*

### Simulated session report

- cycles: 2 · timeframe 1m · model latency 250.0ms · market latency 40.0ms
- started: 2026-10-11T17:36:21.359234+05:30 · host: logs/sim

#### Checks

| check | result | detail |
|---|---|---|
| cycles_run | PASS | 2 of 2 requested |
| every_cycle_decided | PASS | 2 decisions over 2 cycles |
| notebook_update_mandatory | PASS | the notebook was refused on purpose: 2 of 2 deciding cycles recorded notes_missing, 0 writes |
| digest_reached_the_model | PASS | 10 of 10 model calls carried the notebook |
| reminder_fired_when_needed | PASS | 4 model calls arrived in answer to the mandatory reminder |
| no_failures | PASS | 0 failure events |
| orders_reached_module3 | PASS | 2 orders, 2 filled, statuses {'open': 2} |
| module4_archived | PASS | 2 orders in the archive |
| notebook_persisted | PASS | 0 keys left (expected none, the model refused) |

#### Stage latency (ms)

| stage | n | p50 | p90 | max | sum |
|---|---|---|---|---|---|
| wake_ms | 2 | 1170.0 | 1169.0 | 1171.0 | 2340.0 |
| model_ms | 2 | 1356.2 | 1307.2 | 1405.2 | 2712.4 |
| tool_wall_ms | 2 | 146.5 | 132.3 | 160.7 | 293.0 |
| overhead_ms | 2 | 136.8 | 120.5 | 153.1 | 273.6 |
| cycle_ms | 2 | 1639.5 | 1560.0 | 1719.0 | 3279.0 |
| sleep_ms | 1 | 58442.0 | 58442.0 | 58442.0 | 58442.0 |

#### Per cycle

| bar (IST) | wake | model (calls) | tools (calls, wall) | overhead | cycle | sleep | notes |
|---|---|---|---|---|---|---|---|
| 17:36 | 1169.0 | 1307.2 (5) | 9, 132.3 | 120.5 | 1560.0 | 58442.0 | 0 |
| 17:37 | 1171.0 | 1405.2 (5) | 9, 160.7 | 153.1 | 1719.0 | None | 0 |

#### Module 1 requests (fake market)

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| /v1/market/bar-status | 2 | 51.1 | 46.6 | 55.6 |
| /v1/market/candles | 6 | 65.6 | 75.2 | 76.8 |
| /v1/market/fields | 2 | 50.9 | 49.8 | 52.0 |
| /v1/market/prices | 39 | 47.5 | 49.4 | 52.7 |
| /v1/market/prices/stream | 1 | 45.4 | 45.4 | 45.4 |
| /v1/market/screen | 2 | 50.4 | 49.5 | 51.3 |
| /v1/market/status | 6 | 47.7 | 50.7 | 53.3 |
| /v1/market/symbol/{symbol} | 6 | 63.7 | 69.8 | 71.0 |

#### The chain behind Module 2

- 2 decisions recorded · 2 orders in Module 3 (2 filled) · 2 archived in Module 4
- notebook: 0 keys, 0 edits, 0 characters
- model: 10 calls · 31089 prompt tokens · 344 completion tokens
- tool calls: 18 · model kinds: {'open': 2, 'inspect': 2, 'decide': 2, 'forgot-note': 4}
