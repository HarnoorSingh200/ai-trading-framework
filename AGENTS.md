# Workspace Instructions - v3

## What this is

`v3` is a rebuild of the trading framework, one module at a time, with the user
watching each step. `v1` and `v2` are history: read them for what they learned,
never copy their structure wholesale.

The four-module shape is fixed:

1. **Market data** - `module1/`. Built.
2. **Agent engine** - `module2/`. Built.
3. **Order manager** - `module3/`. Built.
4. **Reporting** - `module4/`. Built.

Two support packages sit beside them and are not modules: `common/` (secret
reading, logging, the trading window, the Telegram notifier) and `runner/` (the
supervisor that starts and stops the four services, watches them and keeps the
day's clock). Neither holds market data, a position, or a decision.

## Non-negotiables

- **Module boundaries hold.** Module 1 owns everything that talks to the
  broker. No other module holds a credential, opens a broker connection, or
  reads another module's storage.
- **No secret ever enters this tree.** Not in source, docs, logs, tests,
  screenshots or configuration. Secrets come from the environment or from the
  file named by `FRAMEWORK_SECRETS_FILE`, which lives outside the repository.
  The broker session cache belongs outside the tree too.
- **Live trading stays impossible.** Nothing in v3 may place a real order. Do
  not add a live adapter, and do not soften the paper-only posture, until the
  user asks for it in a separate, explicit instruction.
- **The order manager holds positions; it does not execute trades.** Module 3
  has no broker credential and no adapter to add one to, and "executed" means
  the framework took the position itself, at the first live price Module 1
  published for that stock. Any change that gives Module 3 a route to a broker
  is a change to this rule, not an implementation detail.
- **Sign-in stays unattended.** The web-session flow exists so the framework
  never asks a person for anything at start-up. Do not introduce a prompt, a
  consent step, or a daily manual task. Never replace it with the Kite Connect
  consent flow, whose token dies every morning.
- **One cache, one account.** The session cache format is shared with the
  existing tooling on purpose. Two independent sign-ins for the same user
  invalidate each other.

## Working method

- Build vertical slices that actually run, and prove them against the real
  broker where the broker is involved.
- **Add a test with every behaviour change.** For Module 1 that means unit
  tests, and a real run for anything that only the broker can confirm.
- **An unattended run is `python -m runner`.** It is the central process: it
  starts Module 1 and Module 4 together (reporting reads a local archive, not
  market data, so it must not wait for the warm-up), waits until the session,
  history, warm-up, feed *and the computed indicators* are actually ready, and
  only then starts Modules 3 and 2 for the trading day (stopping them on the
  day's clock). It keeps running after the close and serves the Control tab's
  status, logs and restart buttons on `FRAMEWORK_CONTROL_PORT` (5205 by
  default). `python -m runner --check` prints the resolved configuration and
  sends one real Telegram message.
- **Never claim freshness you do not have.** If a price is stored rather than
  live, say so, and say how old it is. A number without a source and a
  timestamp is a bug in this project.
- **Real data over convenient data.** Do not hard-code instrument tokens,
  index membership, or prices; fetch them and record when they were fetched.
- Run `uv run pytest` and `uv run ruff check .` before calling anything done,
  and the browser smoke test for UI changes.
- Record what a change taught you in the code where the next person will need
  it - especially anything about the broker's real behaviour, which is
  documented nowhere and contradicted by the obvious reading of its own wire
  format.

## Facts worth not re-learning

- **The trading window is operator policy, not the exchange's calendar.** By
  default the framework decides 09:30-14:30 IST and is flat by 14:50 IST;
  Module 1 still owns the real session hours (09:15-15:30, weekends). The three
  moments live in `common/window.py` and are read by Module 2 and Module 3 from
  the same environment variables, so the two can never disagree about whose
  turn it is.
- **Exchange holidays are one local JSON list, and the runner owns the gate.**
  Module 1 seeds it from `module1/data/nse_holidays.json` and refreshes
  `data/holidays/nse.json` from NSE's holiday master (day check, not per
  request). The runner reads that file directly - it does not ask Module 1 -
  and starts Modules 2 and 3 only on a weekday without a holiday and before
  14:30 IST. Module 1 keeps fetching market data on a holiday. A holiday must
  never start the trading day, and a year the list does not cover must say so
  rather than pass as "no holiday".
- **Closed hours are a state, not a failure.** Module 1 and Module 4 run for
  the life of the central process - last available data and the long record are
  useful after the close - while Modules 3 and 2 exist only for the trading day
  and are reported as *resting*, with the reason, outside it. `python -m runner`
  does not exit at the close and does not refuse to start outside the window;
  the Control tab shows the same day state it acts on.
- **Module 2 is started fresh each morning.** Its agents finish for the day on
  their own clock, so a process left running overnight has a dead fleet by
  morning. The central process restarts Module 2 at the day's lead time (five
  minutes before the first decision, on a day the runner's own local holiday
  check says has a session) rather than trusting whatever was left running.
- **A fresh agent waits for the next boundary, not the candle it just missed.**
  A restart at 11:03 with a 15-minute agent must not book a trade on the 10:45
  bar: `run()` sleeps to the next decision boundary before its first look
  (unless the day has no decision left, in which case it says so and stops).
  The simulation caught this - the first version decided a candle that had
  closed 43 seconds before the agent process existed.
- **Latency is part of the log, not of memory.** Every `model_reply` event
  carries `latency_ms`, `tool_calls` and the token counts, and every
  `tool_result` carries `ms`. Do not drop them: `logs/latency_report.py` reads
  the two to separate model time, tool time (and its parallelism) and
  orchestration overhead, and a stage that is not recorded cannot be analysed.
- **An agent is turned off, never deleted, for production.** `enabled: false`
  in its definition keeps the agent, its notes and its whole history, and takes
  it out of the fleet: nothing auto-starts it, a manual Start is refused with
  `AGENT_DISABLED`, and a direct `--agent` launch does nothing. The status
  shows the switch, so "not running" is never mistaken for "crashed".
- **A benchmark is a market fact, so Module 1 serves it; Module 4 mirrors it.**
  Module 1 resolves an INDICES row from the same instrument dump and fetches
  the index's daily closes over the same session (indices are not in the
  tradable universe, so the EQ-only map deliberately does not carry them).
  Module 4 keeps them in `data/history/benchmark.json` and draws them beside
  the agents' realised P&L on the same notional, so a rupee result is read
  against what the market did. Like the token record it is last-known-wins.
- **Token usage is Module 2's log, mirrored by HTTP.** Module 2 sums the
  `model_reply` events it already writes and serves a day's totals on
  `/v1/usage`; Module 4 polls that route and keeps `data/history/tokens.json`,
  never reading another module's files. The runner forces a final read into
  Module 4 *before* it stops Module 2 after the last decision, because the logs
  are the only copy and a missed final read cannot be recovered. The record is
  monotonic within a day: a merge keeps the larger observation, so a restart
  that reports less never walks a total backwards.
- **A cycle's work time is measured where the work happens.** Module 2 writes
  `cycle_finished` with the process-measured wall duration after the model,
  tool, hand-off and notebook work, before it sleeps for the next candle. Its
  usage route mirrors that row with the cycle's tokens and tool/model timing.
  Older logs without the event may show a duration derived from their first and
  last timestamps; never call that measured.
- **A wake-up waits for the boundary, plus a settle.** An agent that starts
  mid-session waits for the *next* candle of its own timeframe; it reads that
  candle five seconds after the boundary, because Module 1 only closes a minute
  when the first tick of the next one arrives. `MODULE2_CANDLE_SETTLE_SECONDS`
  is that buffer, and `MODULE2_BAR_TIMEOUT_SECONDS` is still the ceiling on
  waiting for the book to say the bar is finished.
- **The last order of the day is capped, not refused by luck.** The candle that
  closes at 14:30 is decided in the grace after the close, and its holding time
  is counted from the candle so it may use the whole twenty minutes to 14:50.
  Module 3 stores `session_close_at` on the order and caps the deadline there,
  and expires an order that arrives after the close instead of entering it.
- **A notification is never a dependency.** Every message goes through the
  queue in `common/telegram.py`: callers return immediately, a failed send is
  logged and counted rather than raised, and a repeated key is suppressed for
  its dedupe window so a reconnect loop cannot become a hundred messages. Never
  put the bot token in this tree; it belongs in `FRAMEWORK_SECRETS_FILE`.
- **One rotating log per process, in IST.** Every entry point calls
  `common.logging.setup_logging(<service>)` before it does anything else, which
  is why there is no `logging.basicConfig` anywhere any more. A cloud machine
  has no console to read, so the file under `logs/` is the record.
- **A refused `EventSource` never reconnects.** When the site is open before
  the framework, the Vite proxy answers 500 for the module streams, and the
  browser fails that connection permanently - it retries a *dropped* stream,
  not a refused one. Every tab that streams therefore rebuilds its own
  `EventSource` on error, and the market tab polls the board on a slow
  heartbeat because a closed market pushes nothing at all. The first screen
  after a cold start must recover without a manual reload.
- **A `.cmd` file must keep Windows line endings.** `apply_patch` writes LF
  endings, and cmd.exe loses `goto`/`call` labels in an LF-only batch file: the
  window then closes before printing anything, which looks exactly like "I
  double-clicked and nothing happened". After editing any `.cmd` in this tree,
  convert it back to CRLF. The launcher's logic lives in
  `start-framework.ps1` for this reason; `start-module-1.cmd` is only a stub.
- **A service is not always one process.** In this environment
  `.venv\Scripts\python.exe` is a launcher that spawns the real interpreter, so
  `python -m module1` is two processes; Module 2 also starts one process per
  agent on purpose. Stopping a service therefore has to stop its *tree* - the
  runner and Module 2 both use `taskkill /T /F` on Windows and a process group
  on POSIX - or the child keeps the port, the book, or the position.
- **History seeds, the stream extends.** During a session the historical API
  must not be touched for candles. If a change makes a live session poll
  history per minute, it is wrong however convenient it looks.
- **One budget, two lanes.** The rate limit is the account's, not a
  connection's. Background work yields to anything a person is waiting on.
- **Never stitch over a hole.** A minute with no tick is recorded as missing,
  and a bar short of minutes is marked short. A plausible-looking bar built
  from three ticks is worse than an obviously incomplete one.
- **Probe the tail once per day, not once per symbol.** A cap-hit day is
  memoised: 92 of 200 warm-up requests were one question asked fifty times.
- **One depth per cached series.** Two callers wanting different depths cannot
  share a cache keyed only by symbol: whichever asks first would shrink the
  answer for the other. The daily series is fetched at one fixed depth.
- **Theme every colour through a token.** No component may hard-code a colour,
  a shadow or a surface: both palettes live in `app.css`, and anything that
  bypasses them breaks dark mode silently.
- **Indicators never run on a forming candle.** A value that changes while you
  look at it is worse than one a bar old. Every value must carry the bar it
  belongs to.
- **A fill happens at the first live price Module 1 publishes.** That price
  becomes the order's entry and everything downstream is computed from it; the
  price the plan was built on is kept beside it, never conflated with it. The
  quantity stays Module 2's, so the money a finished order risked is the order
  manager's number, not the sizing arithmetic's.
- **A plan that no longer fits the market is not entered.** Strictly between the
  stop and the target, and no further than the tolerated drift from the price
  the plan was built on. Past either, the order expires with the numbers in its
  note; entering anyway would be a trade nobody decided on.
- **A finished order settles its own P&L.** Gross rupees, the capital used, the
  risk the plan carried at the entry that was actually got, and the R-multiple
  that result represents are computed once, at the close, and stored with the
  order. Do not leave a reader to recompute a settlement.
- **An order belongs to the exchange day it was taken on.** `session_date` is
  stamped at intake, in IST. Grouping by UTC day would split the Indian
  afternoon - 15:00 IST is 09:30 UTC - and every daily total would be wrong.
  Module 3's book is the day; Module 4 keeps the history across days.
- **One description of an order lives in one module.** Module 4 stores each
  order exactly as Module 3 describes it, minus the fields that move while it is
  open. Do not re-cut the shape on the way across, and do not archive a price:
  a price is a reading, not part of what happened, and keeping it would rewrite
  the archive on every sync.
- **Module 4 is local-first and S3-mirrored.** The dashboard reads
  `data/history/history.json`; the same archive object lives under the
  configured S3 prefix (currently `stock market v3/history.json`). Startup
  merges local and remote by `order_id`, uploads after a successful sync, and
  the runner refuses to power off if the final S3 upload fails. AWS credentials
  stay on the host, never in this tree.
- **The scheduled deployment is `trading-framework-v3-2` on OCI.** It is a
  stopped Ubuntu 24.04 VM in `remote-dev` / `ap-hyderabad-1`, started weekdays
  at 09:10 IST by Resource Scheduler. The scheduler principal needs its own
  IAM policy; a 15:00 IST OCI stop schedule is the hard backstop because the
  guest poweroff path can fail to transition the instance. It runs the runner
  under `trading-framework.service` and the operator site under
  `trading-site.service` (Vite dev server on loopback 5200, reached through an
  SSH tunnel); cloud-init installs Node and npm, and `bootstrap.sh` installs
  the site's dependencies on update. The runbook is `deploy/oracle-plan.md`.
- **A finished day never changes.** Every order in it is closed by the time the
  session ends, so Module 4 reads a completed day once and reads it for good.
  Only the newest couple are read again. Anything that makes a sync walk the
  whole history is a change to that rule, not an optimisation.
- **Pruning is a request, never a policy.** `POST /v1/orders/prune` is the one
  operation in Module 3 that destroys anything, and nothing calls it on a timer.
  It refuses to touch an open order and refuses to guess at an undated one, and
  it cannot know whether Module 4 has archived the orders first - so the honest
  sequence is sync the archive, check it, then prune. Do not make it automatic.
- **A new field on a stored record needs a backfill.** `session_date` was added
  to orders after orders had already been written, and a book whose older rows
  carry no day reports nothing for them: the daily index comes back empty and
  the archive has nothing to file. Derive from a timestamp the record already
  has - never from today, which would file an old trade under this morning.
- **When one price clears both the target and the stop, the stop is the
  outcome.** A snapshot feed has no ordering inside the gap between two
  observations. Booking the profitable side would be the most flattering
  mistake the order manager could make; book the worse one and flag it.
- **A stored price never fills an order.** `session_close` and
  `previous_close` are prices nobody could have traded at. An order waits for a
  live tick, and the wait itself has a limit.
- **The order book is idempotent on `decision_id`.** That is the only reason
  Module 2's push and Module 3's pull can run at the same time, and the only
  reason a retry after a timeout is harmless.
- **A watched stock that never trades produces no stream frames.** The price
  subscription is not a heartbeat, so the poll under it is not optional.
- **A tool result is recorded whole.** The activity log is the record of what
  the model was actually told; a result cut at a fixed length reads as a tool
  that is broken rather than a log that is short. Anything that must be cut
  carries a flag saying so, and the site folds long results rather than dropping
  them.
- **A cycle that runs out of rounds still answers.** The last round says so, so
  the model submits an order or an explicit ignore instead of spending its whole
  budget exploring and producing nothing. Standing aside deliberately is a
  decision; silence is not.
- **Reasoning that must survive lives in the notebook, and nowhere else.** An
  agent's cycles are separate conversations, so the only memory it carries is
  `data/agents/<id>/notes.json`, written by the `notes` tool (keyed, bounded,
  one lesson per key) and shown to it at the top of every cycle. After its
  decisions and before it sleeps the update is mandatory; a cycle that decides
  without one is logged as `notes_missing`, and the decision still stands. The
  notebook is advisory - no sizing, risk or window rule may read it - the agent
  id comes from the definition and never from an argument, and the `notes` tool
  is exempt from the tool-call budget like `submit_decision`.
- **The final cycle is the notebook's end-of-day hand-off.** It is told to write
  a whole-day lesson, and Module 2 then saves a dated snapshot in the notebook's
  bounded `sessions` history. The next trading day's prompt names the newest
  earlier checkpoint. The live `keys` remain editable memory; checkpointing
  must never erase them, and editing or clearing today's live notes must not
  rewrite yesterday's evidence.
- **The agent keeps its own clock; Module 1 owns the calendar.** Never make an
  agent's wake-up depend on being told that a bar closed - the notification's
  latency and a settle period on top of it are delays that have nothing to do
  with the work. Compute the boundary from the session open, then ask whether
  the bar is *finished* and wait for that fact rather than for a number of
  seconds somebody guessed.
- **One candle, one pass - and as many answers as it deserves.** A cycle may
  place several orders. Do not reintroduce a single-decision loop, and do not
  let two decisions from one bar share a `decision_key`: it names the stock.
- **A round's tool calls run together.** A model that asks about three stocks in
  one turn wants all three; running them in sequence pays three times for one
  answer. Anything they share - the tool budget, the bundle cache, the activity
  log - has to be safe to touch from several threads.
- **A missing answer is not a negative answer.** A wake-up that does not say
  what the session is doing must never be read as a closed market, or every
  order placed on a live tape is refused by a rule the market has not broken.
  The bar-close announcement carries the session for exactly this reason, and
  anything that consumes it must ask rather than assume.
- **An agent may read its own orders and nobody else's.** The agent id comes
  from the definition the worker is running, never from a tool argument, so a
  model cannot name its way into another agent's book.
- **Visibility is not a risk rule.** An agent can see what it already holds, and
  nothing in the framework stops it opening a second position in the same stock.
  Do not let a prompt that says "check your orders" read as an enforced limit -
  if that limit is wanted, it belongs in an explicit rule with a refusal.
- **A read path never computes.** Aggregation and indicators are rebuilt by
  background work keyed on the book's version counter; a request serves
  whatever is current. Making a request pay for the universe is how this
  service spent 97% of a core.
- **Null means "not enough history", never zero and never a guess.** Keep the
  warm-up rule; it is the only reason a downstream number is trustworthy.
- **Field names live in one place.** `FIELDS` in `module1/indicators.py` is the
  catalogue the UI builds columns from *and* the vocabulary a screen filters on,
  so a value the engine computes but nobody lists cannot be asked for by name -
  and it will be asked for, because the model sees it in a stock's data. Two
  tests now fail if the two drift apart in either direction. A field that should
  not be a column carries `surface="inspector"` rather than being left out.
- **The UI never hard-codes a field name.** Columns, labels, decimals and
  groups all come from `/v1/market/indicators/catalogue`, so adding a field is
  a backend change only.
- A Kite web session has **no quote route**. Live prices come only from the
  websocket at `wss://ws.zerodha.com` with `api_key=kite3`.
- A historical request returns **at most 360 candles per calendar day**, taking
  the *earliest* of the range it was given. Since a session is 375 minutes, the
  tail is missing from a one-day request - and the follow-up must name a
  **time of day**, because a date-only bound sends the same date and gets the
  same answer back.
- Historical `from`/`to` bounds are read as **IST**. Format is
  `YYYY-MM-DD HH:MM:SS`; ISO's `T` is rejected, and sending a UTC time where an
  IST one was meant asks for the wrong window without any error.
- A daily candle is stamped at midnight IST and only exists once the session
  has closed.
- The broker's one-minute history for the cash segment **ends at 15:14**, so
  the last fifteen minutes of a session are not available as minute candles.
- The websocket's declared packet size is not the stride between packets. See
  `module1/tests/data/README.md`.
