<script>
  import { signedRupees } from "../lib/format.js";
  import { exitColour, shareOf } from "../lib/reporting.js";

  let { exits = [] } = $props();

  const total = $derived(exits.reduce((sum, row) => sum + Number(row.count || 0), 0));
  const description = $derived(
    exits.map((row) => `${row.label}: ${row.count}`).join(", ") ||
      "No closed trades to break down",
  );
</script>

{#if total}
  <div
    class="mix"
    role="img"
    aria-label={`How trades ended: ${description}`}
  >
    {#each exits as row (row.reason)}
      <span
        class="segment"
        style={`flex-grow: ${row.count}; background: ${exitColour(row.reason)}`}
        title={`${row.label}: ${row.count}`}
      ></span>
    {/each}
  </div>

  <div class="legend">
    {#each exits as row (row.reason)}
      <div class="exit-row">
        <span class="swatch" style={`background: ${exitColour(row.reason)}`}></span>
        <span class="exit-name">{row.label}</span>
        <span class="numeric">{row.count}</span>
        <span class="numeric share">{shareOf(row.count, total).toFixed(0)}%</span>
        <span class="numeric gross {row.gross > 0 ? "up" : row.gross < 0 ? "down" : ""}">
          {signedRupees(row.gross)}
        </span>
      </div>
    {/each}
  </div>
{:else}
  <p class="empty">No closed trade has an exit reason yet.</p>
{/if}

<style>
  .mix {
    display: flex;
    gap: 2px;
    height: 13px;
    overflow: hidden;
    border-radius: 3px;
  }

  .segment {
    min-width: 5px;
    height: 100%;
  }

  .legend {
    display: grid;
    gap: 8px;
    margin-top: var(--space-3);
  }

  .exit-row {
    display: grid;
    grid-template-columns: 8px minmax(86px, 1fr) 32px 38px 72px;
    align-items: center;
    gap: var(--space-2);
    color: var(--muted);
    font-size: var(--step--1);
  }

  .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
  }

  .exit-name {
    color: var(--ink-2);
  }

  .exit-row .numeric {
    text-align: right;
  }

  .share {
    color: var(--faint);
  }

  .gross {
    color: var(--ink-2);
    font-weight: 550;
  }

  .empty {
    margin: var(--space-3) 0;
    color: var(--muted);
  }
</style>
