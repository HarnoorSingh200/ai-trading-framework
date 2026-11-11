<script>
  import OrderLifecycle from "./OrderLifecycle.svelte";
  import SequenceDiagram from "./SequenceDiagram.svelte";

  let { module } = $props();
</script>

<article id={module.id} class="module" aria-labelledby={`${module.id}-title`}>
  <header class="module-head">
    <div class="module-meta">
      <span class="module-number numeric">{module.number}</span>
      <span>{module.name}</span>
      <span>{module.role}</span>
    </div>
    <h2 id={`${module.id}-title`}>{module.title}</h2>
    <p>{module.summary}</p>
  </header>

  <SequenceDiagram diagram={module.sequence} />

  {#if module.id === "design-orders"}
    <div class="lifecycle-block">
      <div class="subhead">
        <p class="kicker">Order state machine</p>
        <h3>Four states, and no broker transition.</h3>
      </div>
      <div class="lifecycle-shell">
        <OrderLifecycle />
      </div>
    </div>
  {/if}

  <div class="module-detail">
    <div class="contract">
      <div class="subhead">
        <p class="kicker">Message contract</p>
        <h3>What crosses the boundary</h3>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Direction</th>
              <th>Message</th>
              <th>Transport</th>
              <th>Timing / guard</th>
            </tr>
          </thead>
          <tbody>
            {#each module.contract as row (row.direction)}
              <tr>
                <th scope="row">{row.direction}</th>
                <td>{row.message}</td>
                <td>{row.transport}</td>
                <td>{row.timing}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>

    <aside class="signals" aria-label={`${module.name} timing and evidence`}>
      <div class="subhead">
        <p class="kicker">Latency & truth</p>
        <h3>Where the time is recorded</h3>
      </div>
      <dl>
        {#each module.signals as [term, detail] (term)}
          <div>
            <dt>{term}</dt>
            <dd>{detail}</dd>
          </div>
        {/each}
      </dl>
      <p class="latency-note">{module.latencyNote}</p>
    </aside>
  </div>
</article>

<style>
  h2,
  h3,
  p {
    margin-top: 0;
  }

  .kicker {
    margin: 0 0 var(--space-3);
    color: var(--accent);
    font-size: var(--step--1);
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .module {
    min-width: 0;
    padding: var(--space-6) 0;
    scroll-margin-top: 82px;
    border-bottom: 1px solid var(--line);
  }

  .module:last-child {
    border-bottom: 0;
  }

  .module-head {
    display: grid;
    grid-template-columns: minmax(300px, 0.78fr) minmax(0, 1.22fr);
    gap: clamp(32px, 6vw, 90px);
    align-items: end;
    margin-bottom: var(--space-5);
    min-width: 0;
  }

  .module-meta {
    display: flex;
    grid-column: 1 / -1;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: calc(var(--space-3) * -1);
  }

  .module-meta span {
    color: var(--muted);
    font-size: var(--step--1);
    font-weight: 650;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .module-meta span + span {
    padding-left: var(--space-2);
    border-left: 1px solid var(--line-2);
    color: var(--faint);
  }

  .module-meta .module-number {
    color: var(--accent);
    font-variant-numeric: tabular-nums;
  }

  .module-head h2 {
    max-width: 21ch;
    margin-bottom: 0;
    font-size: clamp(26px, 3vw, 40px);
    font-weight: 625;
    letter-spacing: -0.035em;
    line-height: 1.06;
  }

  .module-head > p {
    max-width: 72ch;
    margin-bottom: 0;
    color: var(--ink-2);
    line-height: 1.65;
  }

  .module-detail {
    display: grid;
    grid-template-columns: minmax(0, 1.45fr) minmax(290px, 0.55fr);
    gap: clamp(32px, 5vw, 72px);
    margin-top: var(--space-5);
    min-width: 0;
  }

  .contract,
  .signals {
    min-width: 0;
  }

  .subhead {
    margin-bottom: var(--space-3);
  }

  .subhead .kicker {
    margin-bottom: var(--space-2);
  }

  .subhead h3 {
    margin-bottom: 0;
    font-size: var(--step-2);
    font-weight: 620;
    letter-spacing: -0.02em;
  }

  .table-scroll {
    max-width: 100%;
    overflow-x: auto;
    border-top: 1px solid var(--line-2);
  }

  table {
    width: 100%;
    min-width: 760px;
    border-collapse: collapse;
  }

  th,
  td {
    padding: 11px 10px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    vertical-align: top;
    font-size: var(--step--1);
    line-height: 1.45;
  }

  thead th {
    color: var(--muted);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  tbody th {
    width: 20%;
    color: var(--ink);
    font-weight: 650;
  }

  tbody td {
    color: var(--muted);
  }

  tbody td:nth-child(3) {
    color: var(--ink-2);
  }

  tbody tr:hover {
    background: var(--surface);
  }

  .signals {
    padding-left: clamp(0px, 2vw, 30px);
    border-left: 1px solid var(--line);
  }

  .signals dl {
    margin: 0;
  }

  .signals dl > div {
    padding: var(--space-3) 0;
    border-top: 1px solid var(--line);
  }

  .signals dt {
    margin-bottom: 3px;
    color: var(--ink);
    font-size: var(--step--1);
    font-weight: 650;
  }

  .signals dd,
  .latency-note {
    margin: 0;
    color: var(--muted);
    font-size: var(--step--1);
    line-height: 1.55;
  }

  .latency-note {
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--line-2);
  }

  .lifecycle-block {
    display: grid;
    grid-template-columns: minmax(240px, 0.45fr) minmax(0, 1.55fr);
    gap: clamp(26px, 5vw, 72px);
    align-items: center;
    margin-top: var(--space-5);
  }

  .lifecycle-shell {
    overflow-x: auto;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
  }

  @media (max-width: 1180px) {
    .module-head,
    .module-detail,
    .lifecycle-block {
      grid-template-columns: 1fr;
    }

    .module-meta {
      grid-column: 1;
      margin-bottom: 0;
    }

    .signals {
      padding-left: 0;
      padding-top: var(--space-4);
      border-top: 1px solid var(--line);
      border-left: 0;
    }
  }

  @media (max-width: 620px) {
    .module {
      scroll-margin-top: 118px;
      padding: var(--space-5) 0;
    }

    .module-meta {
      flex-wrap: wrap;
    }
  }
</style>
