export const MODULES = [
  {
    number: "01",
    id: "design-market",
    name: "Market data",
    role: "Broker boundary",
    title: "One connection to the broker becomes clean, timestamped market facts.",
    summary:
      "Module 1 signs in with the existing web session, resolves the universe, loads history and subscribes to the live tape. It closes candles when the next minute begins, computes indicators only from closed bars, and publishes HTTP snapshots plus server-sent events.",
    sequence: {
      id: "market-sequence",
      title: "Market data message flow",
      description:
        "The broker supplies history and live ticks; Module 1 builds closed candles and indicators and publishes them to local consumers.",
      actors: [
        { label: "Broker", meta: "Kite web APIs + WSS" },
        { label: "Market data", meta: "Module 1 · :5201" },
        { label: "Candle book", meta: "1m → all timeframes" },
        { label: "Indicators", meta: "closed bars only" },
        { label: "Consumers", meta: "site, M2, M3, M4" },
      ],
      messages: [
        { from: 1, to: 0, label: "session + instruments", kind: "call" },
        { from: 0, to: 1, label: "historical candles", kind: "return" },
        { from: 1, to: 0, label: "subscribe token set", kind: "async" },
        { from: 0, to: 1, label: "binary price packets", kind: "async" },
        { from: 1, to: 2, label: "accepted tick → minute bucket", kind: "call" },
        { from: 2, to: 3, label: "closed bar only", kind: "accent" },
        { from: 3, to: 1, label: "values + bar_open_time", kind: "return" },
        { from: 1, to: 4, label: "HTTP snapshot · board / prices", kind: "call" },
        { from: 1, to: 4, label: "SSE board / prices / bar_closed", kind: "async" },
      ],
      frame: {
        operator: "LOOP",
        label: "while the market moves",
        first: 2,
        last: 6,
      },
    },
    contract: [
      {
        direction: "Broker → Module 1",
        message: "Web session, instruments, historical candles, binary WebSocket ticks",
        transport: "HTTPS + WSS · shared session cache",
        timing: "Tick freshness 120s · WSS reconnect 2s × attempt, capped 60s",
      },
      {
        direction: "Module 1 → local API",
        message: "Board, prices, screening, candle bundles, indicators, benchmark",
        transport: "Loopback HTTP · JSON",
        timing: "Requests read cache; board reads never compute indicators",
      },
      {
        direction: "Module 1 → subscribers",
        message: "`board`, `prices`, `bar_closed` events; 15s keepalive",
        transport: "Server-sent events",
        timing: "Board min interval 250ms · prices min interval 250ms",
      },
      {
        direction: "Background workers",
        message: "Daily history refresh, gap repair, benchmark, holiday list",
        transport: "Broker / NSE HTTP, inside Module 1",
        timing: "History 60s check · repair 300s · benchmark 900s · holidays 24h",
      },
    ],
    signals: [
      ["Bar close", "First accepted tick of the next minute closes the previous one."],
      ["Freshness", "A tick is `live` for 120s; after that the stored-close source is shown."],
      ["Measured", "`exchange_time`, `received_at`, `price_at`, `price_age_seconds`."],
    ],
    latencyNote:
      "The broker’s transport delay is not stored as a request-duration metric. The record keeps the exchange timestamp and local receipt time, then exposes the age of the price that was actually served.",
  },
  {
    number: "02",
    id: "design-agents",
    name: "Agent engine",
    role: "Decision loop",
    title: "Each agent wakes on its own clock, gathers in parallel, then commits a sized plan.",
    summary:
      "A fresh agent waits for the next boundary of its own timeframe, adds a five-second settle, and asks Module 1 whether the bar is actually finished. It reads its own book and notes, then runs model/tool rounds. One candle can produce several decisions, but it is attempted only once.",
    sequence: {
      id: "agent-sequence",
      title: "Agent cycle message flow",
      description:
        "An agent wakes, waits for a closed bar, reads its own position context, alternates model and parallel tool rounds, and hands sized decisions to Module 3.",
      actors: [
        { label: "Agent clock", meta: "own timeframe" },
        { label: "Agent", meta: "one process" },
        { label: "Module 1", meta: "market facts" },
        { label: "Model", meta: "Chat or Responses" },
        { label: "Module 3", meta: "own order book" },
      ],
      messages: [
        { from: 0, to: 1, label: "boundary + 5s settle", kind: "async" },
        { from: 1, to: 2, label: "GET /bar-status", kind: "call" },
        { from: 2, to: 1, label: "ready ≥ 90% · 12s cap", kind: "return" },
        { from: 1, to: 4, label: "GET own holdings + day result", kind: "call" },
        { from: 4, to: 1, label: "positions + realised P&L", kind: "return" },
        { from: 1, to: 3, label: "prompt + tool schemas", kind: "call" },
        { from: 3, to: 1, label: "tool calls + latency_ms", kind: "return" },
        { from: 1, to: 2, label: "parallel market tool round", kind: "call" },
        { from: 2, to: 1, label: "results · each carries ms", kind: "return" },
        { from: 1, to: 4, label: "POST sized decisions · 10s", kind: "accent" },
        { from: 1, to: 3, label: "next round / notebook update", kind: "call" },
        { from: 3, to: 1, label: "answer or final tool call", kind: "return" },
      ],
      frame: {
        operator: "LOOP",
        label: "poll 0.4s",
        first: 1,
        last: 2,
      },
    },
    contract: [
      {
        direction: "Clock → agent",
        message: "Wake at the next closed candle, never the candle just missed",
        transport: "In-process schedule",
        timing: "Boundary + 5s settle · ready poll 0.4s · 12s ceiling",
      },
      {
        direction: "Agent → Module 1",
        message: "`status`, `bar-status`, `fields`, `screen`, `stock`, `candles`",
        transport: "Loopback HTTP · JSON",
        timing: "HTTP timeout 30s; one round fans out up to 8 calls",
      },
      {
        direction: "Agent ↔ model",
        message: "Stable prompt, notebook digest, clock, candle and tool schemas",
        transport: "Responses or Chat Completions",
        timing: "Default cycle timeout 120s · one retry after 5s",
      },
      {
        direction: "Agent → Module 3",
        message: "Own order summary; sized decision POSTs",
        transport: "Loopback HTTP · JSON",
        timing: "Book read / decision handoff timeout 10s · idempotent by decision_id",
      },
    ],
    signals: [
      ["Measured per call", "`model_reply.latency_ms`; every tool result carries `ms`."],
      ["Parallel round", "Tool wall time is the slowest call in the round, not the sum."],
      ["Measured per cycle", "`cycle_finished.duration_ms` covers work after the wake and before the sleep."],
      ["Derived stages", "`wake_ms`, `model_ms`, `tool_wall_ms`, `overhead_ms` and `sleep_ms` break that cycle apart."],
    ],
    latencyNote:
      "The activity log is the source of truth. Cycle wall time is measured by the agent process. A round’s tool calls overlap, so adding their `ms` values would overstate elapsed time; the latency report uses the maximum within each round and labels older cycles without cycle_finished as derived.",
  },
  {
    number: "03",
    id: "design-orders",
    name: "Order manager",
    role: "Paper positions",
    title: "A decision waits for a live tick, then becomes a position with a clock and three exits.",
    summary:
      "Module 3 owns the only position book. It records a decision, waits for the first fresh live price from Module 1, and enters only if the plan still brackets the market. A stream watches held symbols, a 15-second poll covers quiet names, and a one-second sweep enforces time.",
    sequence: {
      id: "order-sequence",
      title: "Order tracking message flow",
      description:
        "Module 2 submits a decision, Module 3 records it, waits for a live Module 1 price, opens the paper position, and persists every transition for reporting.",
      actors: [
        { label: "Module 2", meta: "decisions" },
        { label: "Order manager", meta: "Module 3 · :5203" },
        { label: "Module 1", meta: "prices" },
        { label: "Order ledger", meta: "JSON + JSONL" },
        { label: "Reporting", meta: "pull by day" },
      ],
      messages: [
        { from: 0, to: 1, label: "POST /v1/orders · 10s", kind: "call" },
        { from: 1, to: 3, label: "order_received · pending", kind: "call" },
        { from: 1, to: 2, label: "GET /prices?symbols=", kind: "call" },
        { from: 2, to: 1, label: "live price + age", kind: "return" },
        { from: 1, to: 3, label: "open at first fitting tick", kind: "accent" },
        { from: 2, to: 1, label: "SSE watched-price frames", kind: "async" },
        { from: 1, to: 3, label: "target / stop / timeout", kind: "call" },
        { from: 4, to: 1, label: "GET days + orders", kind: "call" },
        { from: 1, to: 4, label: "durable order rows", kind: "return" },
      ],
    },
    contract: [
      {
        direction: "Module 2 → Module 3",
        message: "Sized plan with decision_id, levels, holding time and reason",
        transport: "Loopback HTTP POST",
        timing: "10s timeout · retry-safe because `decision_id` is idempotent",
      },
      {
        direction: "Module 3 ↔ Module 1",
        message: "Named price snapshot, then SSE frames for held symbols only",
        transport: "HTTP + server-sent events",
        timing: "SSE first frame per symbol · poll 15s · reconnect 2s × attempt, max 60s",
      },
      {
        direction: "Module 3 → ledger",
        message: "Checkpoint plus append-only `order_received`, fill and exit events",
        transport: "Atomic `orders.json` + `events.jsonl`",
        timing: "Clock sweep 1s · no event-store polling",
      },
      {
        direction: "Module 3 ↔ Module 2",
        message: "Pull recent decisions if the direct handoff failed",
        transport: "HTTP pull from agent decision log",
        timing: "Every 30s · max age 120m",
      },
    ],
    signals: [
      ["Price gate", "Only `source=live` within 120s may fill or exit an order."],
      ["Plan gate", "Fill must be strictly inside stop/target and within 1% drift."],
      ["Stored timing", "`received_at`, `filled_at`, `deadline`, `ended_at`, `price_age_seconds`."],
    ],
    latencyNote:
      "Order timing is stored as event timestamps, not an aggregate request-latency field. The 15-second quote poll is a quiet-name safety net; the stream normally supplies the first price immediately.",
  },
  {
    number: "04",
    id: "design-reporting",
    name: "Reporting",
    role: "Durable record",
    title: "Three APIs feed one archive; finished days stop changing.",
    summary:
      "Module 4 pulls the daily book from Module 3, benchmark closes from Module 1 and token plus per-cycle timing from Module 2. It merges orders by order_id, usage by IST day and cycles by cycle_id; only the newest two days need rereading.",
    sequence: {
      id: "reporting-sequence",
      title: "Reporting synchronization message flow",
      description:
        "Module 4 periodically pulls orders, benchmark and usage data, merges them into the archive, and mirrors the archive to S3.",
      actors: [
        { label: "Module 3", meta: "daily book" },
        { label: "Module 1", meta: "benchmark" },
        { label: "Module 2", meta: "token usage" },
        { label: "Reporting", meta: "Module 4 · :5204" },
        { label: "Archive", meta: "local + S3" },
      ],
      messages: [
        { from: 3, to: 0, label: "GET day index + wanted rows", kind: "call" },
        { from: 0, to: 3, label: "full order shape by day", kind: "return" },
        { from: 3, to: 1, label: "GET /v1/market/benchmark", kind: "call" },
        { from: 1, to: 3, label: "NIFTY daily closes", kind: "return" },
        { from: 3, to: 2, label: "GET /v1/usage?day=", kind: "call" },
        { from: 2, to: 3, label: "tokens + cycle rows", kind: "return" },
        { from: 3, to: 4, label: "merge + write history.json", kind: "accent" },
        { from: 3, to: 4, label: "mirror archive to S3", kind: "async" },
      ],
      frame: {
        operator: "LOOP",
        label: "every 30s",
        first: 0,
        last: 7,
      },
    },
    contract: [
      {
        direction: "Module 4 → Module 3",
        message: "Day index, then complete order rows for selected exchange days",
        transport: "Loopback HTTP · JSON",
        timing: "Every 30s · newest 2 days reread · HTTP timeout 30s",
      },
      {
        direction: "Module 4 → Module 1",
        message: "NIFTY 50 daily closes for a same-notional comparison",
        transport: "Loopback HTTP · JSON",
        timing: "Every 30s · Module 1 caches the broker read for 900s",
      },
      {
        direction: "Module 4 → Module 2",
        message: "Per-day totals and per-cycle tokens, wall time, model time and tool work",
        transport: "Loopback HTTP · JSON",
        timing: "Every 30s while M2 answers; final forced read before M2 stops",
      },
      {
        direction: "Module 4 → storage",
        message: "Order archive, benchmark closes, token usage",
        transport: "Atomic local JSON + S3 archive",
        timing: "Upload after a successful sync; final upload checked before shutdown",
      },
    ],
    signals: [
      ["Finished means stable", "Days older than the newest two are read once and kept."],
      ["Identity", "Orders merge by `order_id`; newer open-order fields are removed."],
      ["Measured timestamp", "The archive records `synced_at`; request duration is not persisted."],
    ],
    latencyNote:
      "Reporting exposes when data was last synchronized, not how long each HTTP request took. A failed pull leaves the previous local value in place rather than replacing it with an estimate.",
  },
];

export const TRANSPORT_RULES = [
  {
    title: "Loopback HTTP",
    detail: "Normal request/response between services. Each caller has its own timeout.",
  },
  {
    title: "Server-sent events",
    detail: "One-way updates: `board`, `prices`, `bar_closed`, order and agent activity feeds.",
  },
  {
    title: "Best-effort push",
    detail: "A decision is logged before its POST to Module 3; reconciliation recovers a missed handoff.",
  },
  {
    title: "No file crossing",
    detail: "One module may not open another module’s storage. Reading happens through HTTP.",
  },
];

export const LATENCY_METRICS = [
  {
    code: "duration_ms",
    source: "Module 2 · cycle_finished",
    meaning: "Process-measured work from cycle start to outcome, excluding the sleep before the next candle.",
  },
  {
    code: "latency_ms",
    source: "Module 2 · model_reply",
    meaning: "Elapsed provider call time for one model response.",
  },
  {
    code: "ms",
    source: "Module 2 · tool_result",
    meaning: "One tool execution. Calls in a round overlap, so the wall time is their maximum.",
  },
  {
    code: "price_age_seconds",
    source: "Module 1 / Module 3",
    meaning: "Age of a market reading, not the duration of the HTTP request that fetched it.",
  },
  {
    code: "wake_ms → sleep_ms",
    source: "latency_report.py",
    meaning: "Derived stage breakdown: bar close, first model call, tool rounds, overhead and sleep. It complements cycle_finished rather than replacing it.",
  },
];
