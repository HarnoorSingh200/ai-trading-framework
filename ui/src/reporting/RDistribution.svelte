<script>
  import { signedRupees } from "../lib/format.js";
  import { shareOf } from "../lib/reporting.js";

  let { bins = [] } = $props();

  let hoveredKey = $state(null);
  let pinnedKey = $state(null);

  const total = $derived(bins.reduce((sum, bin) => sum + Number(bin.count || 0), 0));
  const largest = $derived(
    Math.max(1, ...bins.map((bin) => Number(bin.count || 0))),
  );
  const active = $derived(
    bins.find((bin) => bin.key === (hoveredKey ?? pinnedKey)) ?? null,
  );

  function barTone(key) {
    if (["le_2", "m2_m1", "m1_0"].includes(key)) return "down";
    if (key === "flat") return "flat";
    return "up";
  }
</script>

{#if total}
  <div class="distribution">
    {#each bins as bin (bin.key)}
      <button
        type="button"
        class="row"
        class:active={active?.key === bin.key}
        aria-pressed={pinnedKey === bin.key}
        aria-label={`${bin.label}: ${bin.count} trades, ${signedRupees(bin.gross)}`}
        onclick={() => (pinnedKey = pinnedKey === bin.key ? null : bin.key)}
        onpointerenter={() => (hoveredKey = bin.key)}
        onpointerleave={() => (hoveredKey = null)}
        onfocus={() => (hoveredKey = bin.key)}
        onblur={() => (hoveredKey = null)}
      >
        <span class="bin-label">{bin.label}</span>
        <span class="track" aria-hidden="true">
          <span
            class="fill {barTone(bin.key)}"
            style={`width: ${bin.count ? Math.max(3, (bin.count / largest) * 100) : 0}%`}
            title={`${bin.count} trades · ${signedRupees(bin.gross)}`}
          ></span>
        </span>
        <span class="count numeric">
          {bin.count}
          <small>{shareOf(bin.count, total).toFixed(0)}%</small>
        </span>
      </button>
    {/each}
  </div>
  <p class="note" aria-live="polite">
    {active
      ? `${active.label}: ${active.count} trades produced ${signedRupees(active.gross)}.`
      : "Hover or focus a bucket for its count and gross result. Click to pin it."}
  </p>
{:else}
  <p class="empty">No closed trade carries an R multiple yet.</p>
{/if}

<style>
  .distribution {
    display: grid;
    gap: 9px;
  }

  .row {
    display: grid;
    grid-template-columns: 78px minmax(80px, 1fr) 45px;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    padding: 0;
    background: none;
    border: 0;
    color: inherit;
    text-align: left;
  }

  .bin-label {
    color: var(--muted);
    font-size: var(--step--1);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .track {
    height: 9px;
    overflow: hidden;
    background: var(--surface);
    border-radius: 2px;
  }

  .row.active .track {
    outline: 1px solid var(--line-2);
    outline-offset: 2px;
  }

  .fill {
    display: block;
    height: 100%;
    border-radius: 2px;
  }

  .fill.up {
    background: var(--up);
  }

  .fill.down {
    background: var(--down);
  }

  .fill.flat {
    background: var(--faint);
  }

  .count {
    display: flex;
    align-items: baseline;
    justify-content: flex-end;
    gap: 5px;
    color: var(--ink-2);
    font-size: var(--step--1);
    font-weight: 600;
  }

  .count small {
    min-width: 24px;
    color: var(--faint);
    font-size: 9px;
    font-weight: 500;
    text-align: right;
  }

  .note {
    margin: var(--space-3) 0 0;
    color: var(--faint);
    font-size: var(--step--1);
  }

  .empty {
    margin: var(--space-3) 0;
    color: var(--muted);
  }

  @media (max-width: 560px) {
    .row {
      grid-template-columns: 68px minmax(60px, 1fr) 42px;
    }
  }
</style>
