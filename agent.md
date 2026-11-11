# Repository Guide for Future Agents

This file is an onboarding map, not a replacement for `AGENTS.md`. Read
`AGENTS.md` for the non-negotiable rules. Read this file first when you need to
know where a behaviour lives, how a message moves, and what can be measured
without inventing a number.

## What this repository is

`Stock market v3` is an unattended, paper-only trading framework with four
fixed modules and two support packages:

1. `module1/` - market data and the only broker-facing code.
2. `module2/` - the AI agent engine.
3. `module3/` - the paper order manager and position tracker.
4. `module4/` - the long archive, reporting backend and operator analytics.
5. `common/` - environment, trading window, holidays, logging and Telegram.
6. `runner/` - starts, watches, schedules and stops the services.

The browser client is `ui/`, built with Svelte 5 and Vite. It talks only to the
module HTTP surfaces; it never opens a data file directly.

Live broker execution does not exist. Module 1 can read the broker; Module 3
holds positions and settles them in paper. Do not add an execution adapter or
give Module 3 a broker credential.

## Local addresses

| Service | Default URL | Owner |
| --- | --- | --- |
| Operator site | `http://127.0.0.1:5200` | Vite |
| Module 1 | `http://127.0.0.1:5201` | market data |
| Module 2 | `http://127.0.0.1:5202` | agent control |
| Module 3 | `http://127.0.0.1:5203` | paper order book |
| Module 4 | `http://127.0.0.1:5204` | reporting |
| Runner control | `http://127.0.0.1:5205` | central process |

Vite proxies `/api`, `/agents`, `/orders`, `/report` and `/central` so the site
has one origin and the services do not need CORS.

## Start and verify

Unattended framework:

```powershell
python -m runner
python -m runner --check
```

Site:

```powershell
cd ui
npm run dev -- --host 127.0.0.1 --port 5200
npm run build
```

Python:

```powershell
uv run pytest
uv run ruff check .
```

If `uv` cannot resolve the local interpreter, use the checked-out environment:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

Browser smokes:

```powershell
cd ui
node tests/smoke.mjs
node tests/reporting-smoke.mjs
node tests/design-smoke.mjs
```

Do not leave Vite or module services running after a smoke run.

## The day

The exchange session and the framework's operator policy are different facts.

- Exchange session: 09:15-15:30 IST, owned by Module 1.
- Framework decisions: 09:30-14:30 IST.
- Final bar's decision grace: 90 seconds by default.
- Framework flat-by time: 14:50 IST.
- Runner starts Modules 3 and 2 only after Module 1, history, warm-up, feed and
  indicators are ready.
- Module 2 latest default stop: last decision moment plus
  `MODULE2_STOP_GRACE_SECONDS`, typically shortly after the final cycle.
- Module 3 stops later after archive sync and the settle/close path.
- Modules 1 and 4 keep running after the close.

The window is set in `common/window.py` and read by Modules 2 and 3 from the
same environment values. Holidays come from `data/holidays/nse.json`; the
runner gates the trading day on that file.

## Process lifecycle

The runner owns service trees and day transitions:

1. Start Module 1 and Module 4.
2. Wait for Module 1 readiness: session, history, warm-up, stream and computed
   indicators.
3. Start Module 3, then restart Module 2 fresh for the day.
4. At the Module 2 stop boundary, ask Module 4 to sync AI usage before stopping
   the agent processes.
5. At the Module 3 stop boundary, ask Module 4 to sync the order archive and
   S3 before stopping the book.
6. Keep the runner control surface alive for status, logs and restart buttons.

On Windows a Python service may be a process tree. Stop the tree, not only the
launcher process.

## Module 1 - market data

Primary files:

- `module1/service.py` - HTTP routes and service assembly.
- `module1/broker/session.py` - unattended Kite web session.
- `module1/broker/feed.py` - websocket tick decoder.
- `module1/candles.py` - minute buckets and gap-aware candles.
- `module1/indicators.py` - indicator catalogue, computation and field names.
- `module1/market.py` - live board, quotes, paths and subscriptions.
- `module1/analytics.py` - breadth, screening and derived market views.

Workflow:

1. Load the shared web-session cache from outside the repository.
2. Fetch instruments and construct the tradable universe.
3. Seed history, then extend it from the websocket during a session.
4. Build a minute from ticks; never stitch over a missing minute.
5. Close a candle only when the next minute's first tick establishes the
   boundary.
6. Compute indicators on closed candles only.
7. Serve boards, candles, indicators, benchmark and SSE updates.

Module 1 is the only place with broker credentials, broker clients or the
session cache. It also owns the benchmark because a benchmark is a market fact.

Important transport:

- HTTP JSON on `5201` for board, candles, indicators, readiness and benchmark.
- SSE for streamed board updates.
- WSS to the broker for prices. The Kite web session has no quote route.

Known broker facts are documented in `AGENTS.md` and
`module1/tests/data/README.md`. In particular, one-day history is capped at 360
candles, timestamps are IST, and cash minute data ends at 15:14.

## Module 2 - agent engine

Primary files:

- `module2/service.py` - control API, supervisor and SSE.
- `module2/worker.py` - one live agent process and the decision cycle.
- `module2/prompt.py` - stable system prompt and volatile cycle message.
- `module2/tools.py` - market, own-book and notebook tools.
- `module2/decision.py` - decision validation and sizing hand-off.
- `module2/orders.py` - best-effort POST to Module 3.
- `module2/schedule.py` - agent clock and decision boundaries.
- `module2/notes.py` - persistent notebook and end-of-day checkpoints.
- `module2/usage.py` - per-day and per-cycle token/timing aggregation.
- `module2/store.py` - definitions, activity, decisions, state and notes.

One cycle:

1. The agent process keeps its own clock. It wakes after its timeframe closes,
   plus `MODULE2_CANDLE_SETTLE_SECONDS` (five seconds by default).
2. It asks Module 1 whether the bar is finished. It polls
   `bar-status` every 0.4 seconds for up to 12 seconds by default and requires
   90% readiness.
3. It reads market state and its own orders from the module APIs.
4. It builds a cached system message plus one volatile user message containing
   the candle, book and notebook.
5. The model may call market tools in parallel. A tool-call round is gathered
   together, not run sequentially.
6. The model must submit one or more decisions and update the notebook.
7. Module 2 writes decisions first, then POSTs orders to Module 3. A POST
   failure is logged; Module 3 reconciles from Module 2's decision log.

One cycle may place several orders. A decision key names both the candle and
stock. Module 3's book is idempotent on `decision_id`.

### Agent activity records

Every agent has:

- `data/agents/<id>/definition.json`
- `data/agents/<id>/activity.jsonl`
- `data/agents/<id>/decisions.jsonl`
- `data/agents/<id>/state.json`
- `data/agents/<id>/notes.json`

`activity.jsonl` is the timing record. The key events are:

| Event | What it means |
| --- | --- |
| `agent_started` | process/model/window started |
| `cycle_started` | one closed-candle pass began |
| `model_reply` | one successful provider response; carries `latency_ms`, `tool_calls` and token fields |
| `tool_call` | model requested a non-decision tool |
| `tool_result` | completed tool; carries `ms` and the whole result |
| `decision` | validated decision written |
| `notes_updated` | notebook changed |
| `cycle_finished` | process-measured work duration for that cycle |
| `session_checkpointed` | end-of-day notebook hand-off saved |

`cycle_finished.duration_ms` is wall time from cycle start to outcome. It
includes model calls, tool rounds, order hand-off and notebook writes. It
excludes the sleep before the next candle. This is the preferred cycle-time
measurement.

Older logs may not have `cycle_finished`. Module 2's usage reader derives a
duration from the first and last timestamp carrying the same `cycle_id` and
labels it `derived_from_log`. It never presents that as measured work.

Model calls in one cycle are sequential. Tool calls within one round run in
parallel, so the sum of `tool_result.ms` is tool-thread work, not serial wall
time.

### Notebook memory

The notebook is the only reasoning memory that crosses cycles, restarts and
trading days. It is keyed, bounded and advisory:

- live notes are stored under `keys`;
- the model writes one key at a time with `write`, `append` or `delete`;
- the notebook is shown to the model at the start of every cycle;
- no sizing, risk or window rule reads it.

At the final decision cycle, the prompt asks for a whole-day lesson. After that
cycle, Module 2 writes a `session_checkpointed` event and stores a dated copy of
the notebook under `sessions` in `notes.json`.

The notebook now has two layers:

1. `keys` - live memory used by the model.
2. `sessions` - the last five end-of-day hand-offs, with saved time, cycle id,
   note count and text copies.

The next trading day's digest names the most recent earlier checkpoint. Editing
a note during the next day preserves the checkpoint history. Clearing the
live notebook also preserves checkpoint history.

### Agent usage API

`GET /v1/usage?day=YYYY-MM-DD` on Module 2 returns:

- per-agent token and count totals;
- `cycle_time_ms`, `model_time_ms`, `tool_time_ms` and `timed_cycles`;
- `cycle_rows`, one row per `cycle_id`, including wall time, model/tool work,
  tokens, decisions, notebook state and timing source.

The day boundary is IST. Token fields are only summed when the provider
actually reported them.

## Module 3 - order manager

Primary files:

- `module3/service.py` - HTTP API.
- `module3/tracker.py` - sweep, live-price fills and exits.
- `module3/orders.py` - order model and settlement.
- `module3/store.py` - daily book and append-only events.
- `module3/clients.py` - Module 1 price reads and Module 2 decision reads.

Workflow:

1. Receive or pull a decision.
2. Idempotently create one paper position on `decision_id`.
3. Wait for a live Module 1 price. Stored closes never fill an order.
4. Enter only if the plan still fits stop/target/drift limits.
5. Sweep prices every second; poll quiet names every 15 seconds.
6. Close on target, stop, timeout or session deadline.
7. Store gross rupees, capital, risk at actual entry and R multiple once.
8. Stamp `session_date` in IST at intake.

Module 3 has no broker code and cannot execute a real trade.

## Module 4 - reporting

Primary backend files:

- `module4/service.py` - HTTP API and background sync loops.
- `module4/ledger.py` - merge/sync order archive.
- `module4/analytics.py` - trading summaries and chart-ready aggregates.
- `module4/store.py` - local `history.json`.
- `module4/s3.py` - optional S3 mirror.
- `module4/usage.py` - per-day token/cycle mirror in `tokens.json`.
- `module4/benchmark.py` - benchmark closes.
- `module4/clients.py` - HTTP clients for Modules 2, 3 and 1.

Ownership rules:

- Orders are archived exactly as Module 3 described them, minus live fields.
- Module 4 never reads another module's storage.
- Orders merge by `order_id`; token days merge by IST day; cycles merge by
  `cycle_id`.
- S3 is a mirror, not the primary read path.
- Only the newest settled days need rereading.
- `history.json` is local-first and does not contain an account balance.

Sync rules:

- order archive loop: every 30 seconds by default while Module 3 is active;
- AI usage loop: every 30 seconds while Module 2 is active;
- benchmark loop: every 30 seconds while Module 1 is active;
- runner forces a final usage sync before Module 2 stops;
- runner forces a final archive/S3 sync before Module 3 stops.

### Reporting API

| Route | Answer |
| --- | --- |
| `/v1/analytics` | performance summary, curves, agents, days, R, exits, sides and symbols |
| `/v1/performance` | one scope's performance totals |
| `/v1/days` | one row per archived exchange day |
| `/v1/agents` | one row per agent with archived orders |
| `/v1/orders` | exact archived orders |
| `/v1/usage` | mirrored AI usage, including per-cycle rows |
| `/v1/benchmark` | daily benchmark closes and notional |
| `/v1/status` | archive, usage, benchmark and safety posture |
| `/v1/sync` | force order, usage and benchmark sync |
| `/v1/usage/sync` | force AI usage sync |

### Reporting UI

`ui/src/tabs/ReportingTab.svelte` is now a shell with a shared agent/day scope,
one polling loop and four views:

1. **Performance** - `reporting/PerformanceView.svelte`.
2. **AI performance** - `reporting/AiPerformanceView.svelte`.
3. **Order archive** - `reporting/OrderArchiveView.svelte`.
4. **Data coverage** - `reporting/CoverageView.svelte`.

`reporting/CycleScatter.svelte` draws per-cycle wall time or tokens.
`reporting/reporting.css` owns report layout. The existing chart components are
in `ui/src/reporting/`.

Literal meanings:

- gross P&L is before fees and taxes;
- drawdown is realised closed-P&L drawdown, not account equity;
- token totals are provider-reported observations;
- cycle wall time is process-measured only when `cycle_finished` exists;
- tool work may overlap and is not serial wall time;
- a missing observation remains missing.

## UI conventions

- Svelte 5 runes and Vite are the current stack.
- Colours, surfaces, borders, shadows and typography come from `ui/src/app.css`.
- Components must not hard-code colours.
- Preserve light and dark themes.
- Keep responsive page width intact. Tables may scroll inside their panel.
- Nav is horizontal on mobile; each view-control target should be at least
  44px where touch is likely.
- EventSource consumers rebuild their own stream after an error.

The Design tab and its measured SVG diagrams live under `ui/src/design/`.
Do not turn those into decorative illustrations: they document ownership,
message order, cadence and latency.

## Frequently needed invariants

- One broker-facing module only.
- No secret in this tree.
- Paper-only order manager.
- Unattended sign-in.
- One shared session cache/account.
- Closed candles only.
- Never claim freshness without a source and timestamp.
- Never stitch a candle across a missing minute.
- No hard-coded instrument token, universe membership or price.
- Tests with every behaviour change.
- Run tests, Ruff and the relevant browser smoke before calling UI work done.

