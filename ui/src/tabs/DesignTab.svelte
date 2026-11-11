<script>
  import ModuleSection from "../design/ModuleSection.svelte";
  import SystemMap from "../design/SystemMap.svelte";
  import {
    LATENCY_METRICS,
    MODULES,
    TRANSPORT_RULES,
  } from "../design/diagramContent.js";
</script>

<section class="intro" data-screen-label="Framework design guide">
  <div class="intro-copy">
    <p class="kicker">Messages · state · latency</p>
    <h1>How the framework moves a market fact.</h1>
    <p class="lede">
      Each module owns one part of the day, exposes it through an API and keeps its
      storage to itself. Follow the arrows to see what is sent, which direction it
      travels and where the wait actually lives.
    </p>
    <div class="intro-facts">
      <span><strong>4</strong> services</span>
      <span><strong>1</strong> supervisor</span>
      <span><strong>0</strong> live execution paths</span>
    </div>
  </div>

  <div class="latency-key" aria-labelledby="latency-key-title">
    <div class="latency-key-head">
      <p class="kicker">Read latency honestly</p>
      <h2 id="latency-key-title">A response time is not a market age.</h2>
    </div>
    <dl>
      <div>
        <dt class="numeric">latency_ms</dt>
        <dd>One model call, measured by Module 2.</dd>
      </div>
      <div>
        <dt class="numeric">ms</dt>
        <dd>One tool call; parallel calls overlap.</dd>
      </div>
      <div>
        <dt class="numeric">price_age_seconds</dt>
        <dd>How old the market reading is.</dd>
      </div>
      <div>
        <dt class="numeric">cycle_finished</dt>
        <dd>Work before the next sleep, measured by the agent process.</dd>
      </div>
      <div>
        <dt class="numeric">wake → stages</dt>
        <dd>Model, tool-wall, overhead and sleep breakdowns derived from activity.</dd>
      </div>
    </dl>
    <p>
      If a module does not store a duration, the guide says so. It never turns an
      old price or a guessed request time into a fresh metric.
    </p>
  </div>
</section>

<section class="map-section" aria-labelledby="system-map-heading">
  <div class="section-head">
    <div>
      <p class="kicker">System map</p>
      <h2 id="system-map-heading">Ownership first, then messages.</h2>
    </div>
    <p>
      Solid arrows carry the primary handoff. Dashed arrows are pull, stream or
      background paths. The runner supervises processes; it never carries market
      data, orders or decisions.
    </p>
  </div>
  <div class="diagram-scroll">
    <SystemMap />
  </div>
  <div class="transport-rules">
    {#each TRANSPORT_RULES as rule, index (rule.title)}
      <div>
        <span class="numeric">0{index + 1}</span>
        <strong>{rule.title}</strong>
        <p>{rule.detail}</p>
      </div>
    {/each}
  </div>
</section>

<section class="day" aria-labelledby="day-heading">
  <div class="section-head">
    <div>
      <p class="kicker">The operator’s day</p>
      <h2 id="day-heading">Readiness comes before trading.</h2>
    </div>
    <p>
      These are default clock moments. The trading window is operator policy and
      can move together through the three shared environment settings.
    </p>
  </div>
  <ol class="day-line">
    <li>
      <time>09:10</time>
      <strong>Scheduled host starts</strong>
      <span>Deployment clock, not a trading rule.</span>
    </li>
    <li>
      <time>T+0</time>
      <strong>M1 + M4 start</strong>
      <span>Market data and the archive boot together.</span>
    </li>
    <li>
      <time>≤ 7 min</time>
      <strong>Readiness gate</strong>
      <span>60s minimum; session, history, feed and ≥90% indicators.</span>
    </li>
    <li>
      <time>09:25</time>
      <strong>M3 then M2</strong>
      <span>Five-minute lead leaves subscriptions and clocks in place.</span>
    </li>
    <li>
      <time>09:30–14:31:30</time>
      <strong>Decisions allowed</strong>
      <span>The 14:30 candle uses the 90s grace window.</span>
    </li>
    <li>
      <time>14:32:30</time>
      <strong>M2 stops</strong>
      <span>Usage is archived first; 60s after the last possible cycle.</span>
    </li>
    <li>
      <time>14:50</time>
      <strong>Flat by policy</strong>
      <span>Book stops after a 20s settle; M1 and M4 continue.</span>
    </li>
  </ol>
</section>

<nav class="module-index" aria-label="Module workflows">
  {#each MODULES as module (module.id)}
    <a href={`#${module.id}`}>
      <span class="numeric">{module.number}</span>
      <strong>{module.name}</strong>
      <small>{module.role}</small>
    </a>
  {/each}
</nav>

<div class="modules">
  {#each MODULES as module (module.id)}
    <ModuleSection {module} />
  {/each}
</div>

<section class="latency-section" aria-labelledby="latency-heading">
  <div class="section-head">
    <div>
      <p class="kicker">Latency ledger</p>
      <h2 id="latency-heading">Measured, derived and deliberately absent.</h2>
    </div>
    <p>
      The activity log records model and tool stages, and Module 2 now measures
      the full cycle before it sleeps. The analysis script reconstructs the stage
      breakdown from timestamps. HTTP duration for Modules 1, 3 and 4 is not
      currently persisted.
    </p>
  </div>

  <div class="metric-list">
    {#each LATENCY_METRICS as metric (metric.code)}
      <div>
        <code>{metric.code}</code>
        <span>{metric.source}</span>
        <p>{metric.meaning}</p>
      </div>
    {/each}
  </div>

  <div class="latency-boundary">
    <div>
      <span class="marker measured"></span>
      <strong>Measured</strong>
      <p>Model call, tool call, full agent cycle, market price age and event timestamps.</p>
    </div>
    <div>
      <span class="marker derived"></span>
      <strong>Derived</strong>
      <p>Wake, model total, tool wall, overhead and sleep; older cycles without cycle_finished.</p>
    </div>
    <div>
      <span class="marker absent"></span>
      <strong>Not persisted</strong>
      <p>Per-request duration for Module 1, 3 and 4 HTTP calls.</p>
    </div>
  </div>
</section>

<section class="support" aria-labelledby="support-heading">
  <div class="section-head">
    <div>
      <p class="kicker">Around the four</p>
      <h2 id="support-heading">The runner starts the system. Shared support keeps it safe.</h2>
    </div>
  </div>

  <div class="support-grid">
    <article>
      <h3>Runner</h3>
      <p>
        Starts M1 and M4, waits for Module 1’s full readiness statement, then starts
        M3 before M2. It probes health every 30 seconds, retries a dead child,
        archives usage before stopping M2, and syncs the day book before stopping M3.
      </p>
      <ul>
        <li>Readiness poll: 5s · minimum 60s · ceiling 420s.</li>
        <li>Health: 30s interval · 3s probe timeout in the runner loop.</li>
        <li>Restart: up to 5 attempts with a 5s delay.</li>
      </ul>
    </article>

    <article>
      <h3>Shared support</h3>
      <p>
        <code>common/</code> carries the IST trading window, secret loading,
        rotating logs, holiday rules and the queued Telegram notifier. It has no
        market data, position or decision of its own.
      </p>
      <ul>
        <li>One trading window read by both M2 and M3.</li>
        <li>Credentials stay outside the repository.</li>
        <li>Notifications are asynchronous and never a trading dependency.</li>
      </ul>
    </article>
  </div>

  <p class="paper-boundary">
    <span></span>
    Paper-only boundary: no module holds a broker order adapter.
  </p>
</section>

<footer class="design-footer">
  <p>Defaults are shown. Environment settings can change cadence, timeouts and the operator’s window.</p>
  <a href="#top">Back to the top</a>
</footer>

<style>
  .intro {
    display: grid;
    grid-template-columns: minmax(0, 1.08fr) minmax(420px, 0.92fr);
    gap: clamp(42px, 7vw, 104px);
    align-items: start;
    padding: var(--space-5) 0 var(--space-6);
    border-bottom: 1px solid var(--line);
  }

  .kicker {
    margin: 0 0 var(--space-3);
    color: var(--accent);
    font-size: var(--step--1);
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  h1,
  h2,
  h3,
  p {
    margin-top: 0;
  }

  h1 {
    max-width: 12ch;
    margin-bottom: var(--space-4);
    font-size: clamp(40px, 5vw, 68px);
    font-weight: 650;
    letter-spacing: -0.045em;
    line-height: 0.98;
  }

  .lede {
    max-width: 64ch;
    margin-bottom: var(--space-4);
    color: var(--ink-2);
    font-size: var(--step-1);
    line-height: 1.62;
  }

  .intro-facts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-5);
    color: var(--muted);
    font-size: var(--step--1);
  }

  .intro-facts strong {
    color: var(--ink);
    font-size: var(--step-1);
    font-variant-numeric: tabular-nums;
  }

  .latency-key {
    border-top: 1px solid var(--line-2);
    border-bottom: 1px solid var(--line-2);
    padding: var(--space-4) 0;
  }

  .latency-key-head {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-4);
    align-items: baseline;
    margin-bottom: var(--space-3);
  }

  .latency-key-head .kicker {
    margin: 0;
  }

  .latency-key h2 {
    margin: 0;
    font-size: var(--step-2);
    font-weight: 620;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .latency-key dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
  }

  .latency-key dl > div {
    padding: var(--space-3) var(--space-3) var(--space-3) 0;
    border-top: 1px solid var(--line);
  }

  .latency-key dl > div:nth-child(even) {
    padding-left: var(--space-3);
    border-left: 1px solid var(--line);
  }

  .latency-key dt {
    margin-bottom: 3px;
    color: var(--ink);
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 10px;
    font-weight: 650;
  }

  .latency-key dd,
  .latency-key > p {
    margin: 0;
    color: var(--muted);
    font-size: var(--step--1);
    line-height: 1.5;
  }

  .latency-key > p {
    padding-top: var(--space-3);
    border-top: 1px solid var(--line);
  }

  .map-section,
  .day,
  .latency-section,
  .support {
    padding: var(--space-6) 0;
    border-bottom: 1px solid var(--line);
  }

  .section-head {
    display: grid;
    grid-template-columns: minmax(280px, 0.9fr) minmax(320px, 1.1fr);
    gap: clamp(30px, 6vw, 92px);
    align-items: end;
    margin-bottom: var(--space-5);
  }

  .section-head h2 {
    max-width: 22ch;
    margin: 0;
    font-size: var(--step-3);
    font-weight: 625;
    letter-spacing: -0.03em;
    line-height: 1.12;
  }

  .section-head > p {
    max-width: 70ch;
    margin: 0;
    color: var(--muted);
    line-height: 1.62;
  }

  .diagram-scroll {
    overflow-x: auto;
    border: 1px solid var(--line);
    border-radius: var(--radius);
  }

  .transport-rules {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-bottom: 1px solid var(--line);
    margin-top: var(--space-3);
  }

  .transport-rules > div {
    min-width: 0;
    padding: var(--space-3) var(--space-3) 0 0;
  }

  .transport-rules > div + div {
    padding-left: var(--space-3);
    border-left: 1px solid var(--line);
  }

  .transport-rules span {
    display: block;
    margin-bottom: var(--space-2);
    color: var(--accent);
    font-size: var(--step--1);
    font-weight: 700;
  }

  .transport-rules strong {
    color: var(--ink);
    font-size: var(--step--1);
  }

  .transport-rules p {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: var(--step--1);
    line-height: 1.5;
  }

  .day-line {
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: 1px solid var(--line-2);
  }

  .day-line li {
    position: relative;
    padding: var(--space-4) var(--space-2) 0 0;
  }

  .day-line li::before {
    content: "";
    position: absolute;
    top: -4px;
    left: 0;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
  }

  .day-line time {
    display: block;
    margin-bottom: var(--space-2);
    color: var(--accent);
    font-size: var(--step--1);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }

  .day-line strong {
    display: block;
    margin-bottom: 3px;
    color: var(--ink);
    font-size: var(--step--1);
  }

  .day-line span {
    display: block;
    color: var(--muted);
    font-size: 10px;
    line-height: 1.45;
  }

  .module-index {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-bottom: 1px solid var(--line);
  }

  .module-index a {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 2px var(--space-3);
    padding: var(--space-4) var(--space-3);
    color: inherit;
    text-decoration: none;
  }

  .module-index a:first-child {
    padding-left: 0;
  }

  .module-index a + a {
    border-left: 1px solid var(--line);
  }

  .module-index .numeric {
    grid-row: span 2;
    align-self: start;
    color: var(--accent);
    font-size: var(--step--1);
    font-weight: 700;
  }

  .module-index strong {
    color: var(--ink);
    font-size: var(--step--1);
  }

  .module-index small {
    color: var(--muted);
    font-size: 10px;
  }

  .module-index a:hover strong {
    color: var(--accent);
  }

  .modules {
    border-bottom: 1px solid var(--line);
  }

  .metric-list {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-top: 1px solid var(--line-2);
    border-bottom: 1px solid var(--line-2);
  }

  .metric-list > div {
    padding: var(--space-4) var(--space-3);
  }

  .metric-list > div:first-child {
    padding-left: 0;
  }

  .metric-list > div + div {
    border-left: 1px solid var(--line);
  }

  .metric-list code {
    display: block;
    margin-bottom: 3px;
    color: var(--accent);
    font-size: var(--step--1);
    font-weight: 650;
  }

  .metric-list span {
    display: block;
    margin-bottom: var(--space-2);
    color: var(--ink);
    font-size: var(--step--1);
    font-weight: 650;
  }

  .metric-list p,
  .latency-boundary p {
    margin: 0;
    color: var(--muted);
    font-size: var(--step--1);
    line-height: 1.5;
  }

  .latency-boundary {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--space-5);
    margin-top: var(--space-4);
  }

  .latency-boundary strong {
    display: block;
    margin: var(--space-2) 0 3px;
    color: var(--ink);
    font-size: var(--step--1);
  }

  .marker {
    display: block;
    width: 28px;
    height: 3px;
    border-radius: 2px;
  }

  .marker.measured {
    background: var(--up);
  }

  .marker.derived {
    background: var(--accent);
  }

  .marker.absent {
    background: var(--faint);
  }

  .support .section-head {
    margin-bottom: var(--space-5);
  }

  .support-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: clamp(32px, 7vw, 100px);
    border-top: 1px solid var(--line-2);
    padding-top: var(--space-4);
  }

  .support-grid h3 {
    margin-bottom: var(--space-3);
    font-size: var(--step-2);
    font-weight: 620;
    letter-spacing: -0.02em;
  }

  .support-grid p {
    color: var(--ink-2);
    line-height: 1.65;
  }

  .support-grid ul {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding-left: var(--space-4);
    color: var(--muted);
    font-size: var(--step--1);
  }

  code {
    padding: 1px 4px;
    background: var(--surface-2);
    border-radius: 4px;
    color: var(--ink-2);
  }

  .paper-boundary {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: var(--space-5) 0 0;
    color: var(--warn);
    font-size: var(--step--1);
    font-weight: 650;
  }

  .paper-boundary span {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--warn);
  }

  .design-footer {
    display: flex;
    justify-content: space-between;
    gap: var(--space-4);
    padding-top: var(--space-4);
    color: var(--faint);
    font-size: var(--step--1);
  }

  .design-footer p {
    margin: 0;
  }

  .design-footer a {
    color: var(--accent);
    text-decoration: none;
  }

  .design-footer a:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  @media (max-width: 1180px) {
    .intro,
    .section-head {
      grid-template-columns: 1fr;
    }

    .latency-key {
      max-width: 760px;
    }

    .day-line {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      row-gap: var(--space-4);
    }

    .metric-list {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .metric-list > div:nth-child(3) {
      border-left: 0;
      padding-left: 0;
    }
  }

  @media (max-width: 820px) {
    .transport-rules,
    .module-index,
    .latency-boundary,
    .support-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0;
    }

    .transport-rules > div:nth-child(odd),
    .latency-boundary > div:nth-child(odd) {
      padding-left: 0;
      border-left: 0;
    }

    .transport-rules > div:nth-child(-n + 2),
    .latency-boundary > div:nth-child(-n + 2) {
      padding-bottom: var(--space-3);
      border-bottom: 1px solid var(--line);
    }

    .module-index a:nth-child(3) {
      border-left: 0;
      padding-left: 0;
    }

    .module-index a:nth-child(-n + 2) {
      border-bottom: 1px solid var(--line);
    }

    .support-grid {
      gap: var(--space-5);
    }
  }

  @media (max-width: 620px) {
    .intro {
      gap: var(--space-5);
    }

    h1 {
      font-size: 42px;
    }

    .lede {
      font-size: var(--step-0);
    }

    .latency-key-head {
      grid-template-columns: 1fr;
      gap: 0;
    }

    .latency-key dl,
    .transport-rules,
    .module-index,
    .metric-list,
    .latency-boundary,
    .support-grid {
      grid-template-columns: 1fr;
    }

    .latency-key dl > div:nth-child(even),
    .latency-key dl > div,
    .transport-rules > div,
    .transport-rules > div + div,
    .metric-list > div,
    .metric-list > div + div,
    .metric-list > div:nth-child(3),
    .module-index a,
    .module-index a + a,
    .module-index a:nth-child(3) {
      padding: var(--space-3) 0;
      border-left: 0;
      border-bottom: 1px solid var(--line);
    }

    .module-index a {
      padding-left: 0;
    }

    .day-line {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .design-footer {
      flex-direction: column;
    }
  }
</style>
