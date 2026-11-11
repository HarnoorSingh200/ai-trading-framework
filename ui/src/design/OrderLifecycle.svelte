<svg
  class="lifecycle"
  viewBox="0 0 980 312"
  role="img"
  aria-labelledby="order-lifecycle-title order-lifecycle-desc"
>
  <title id="order-lifecycle-title">Order lifecycle</title>
  <desc id="order-lifecycle-desc">
    A decision starts pending, becomes open only after a fitting live price, and ends closed
    at target, stop or timeout, or expired if it never fills.
  </desc>

  <defs>
    <marker
      id="lifecycle-arrow"
      markerWidth="8"
      markerHeight="7"
      refX="7"
      refY="3.5"
      orient="auto"
      markerUnits="userSpaceOnUse"
    >
      <path class="state-arrow-head" d="M0,0 L8,3.5 L0,7 Z" />
    </marker>
    <marker
      id="lifecycle-arrow-accent"
      markerWidth="8"
      markerHeight="7"
      refX="7"
      refY="3.5"
      orient="auto"
      markerUnits="userSpaceOnUse"
    >
      <path class="state-arrow-accent-head" d="M0,0 L8,3.5 L0,7 Z" />
    </marker>
  </defs>

  <path class="state-edge" d="M68 116 H118" marker-end="url(#lifecycle-arrow)" />
  <path
    class="state-edge accent-edge"
    d="M266 112 H383"
    marker-end="url(#lifecycle-arrow-accent)"
  />
  <path class="state-edge" d="M534 112 H638" marker-end="url(#lifecycle-arrow)" />
  <path
    class="state-edge"
    d="M192 156 C192 206 258 235 324 235"
    marker-end="url(#lifecycle-arrow)"
  />

  <text class="transition-label" x="332" y="91" text-anchor="middle">
    first live price fits plan
  </text>
  <text class="transition-label" x="586" y="91" text-anchor="middle">
    target | stop | timeout
  </text>
  <text class="transition-label" x="250" y="211" text-anchor="middle">
    no fitting price / after close
  </text>

  <circle class="start-dot" cx="48" cy="116" r="7" />

  <rect class="state" x="118" y="76" width="148" height="76" rx="8" />
  <text class="state-name" x="192" y="105" text-anchor="middle">Pending</text>
  <text class="state-meta" x="192" y="126" text-anchor="middle">decision_id recorded</text>

  <rect class="state focal" x="383" y="76" width="151" height="76" rx="8" />
  <text class="state-name" x="459" y="105" text-anchor="middle">Open</text>
  <text class="state-meta" x="459" y="126" text-anchor="middle">entry = first live tick</text>

  <rect class="state" x="638" y="76" width="174" height="76" rx="8" />
  <text class="state-name" x="725" y="105" text-anchor="middle">Closed</text>
  <text class="state-meta" x="725" y="126" text-anchor="middle">settlement stored</text>

  <rect class="state expired" x="324" y="198" width="172" height="72" rx="8" />
  <text class="state-name" x="410" y="226" text-anchor="middle">Expired</text>
  <text class="state-meta" x="410" y="247" text-anchor="middle">never became a position</text>

  <text class="safety-note" x="725" y="174" text-anchor="middle">
    one price clearing both → stop
  </text>
</svg>

<style>
  .lifecycle {
    display: block;
    width: 100%;
    min-width: 760px;
    height: auto;
    background: var(--surface);
  }

  .state-edge {
    fill: none;
    stroke: var(--line-2);
    stroke-width: 1.4;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .accent-edge {
    stroke: var(--accent);
    stroke-width: 1.8;
  }

  .state-arrow-head {
    fill: var(--line-2);
  }

  .state-arrow-accent-head {
    fill: var(--accent);
  }

  .start-dot {
    fill: var(--ink);
  }

  .state {
    fill: var(--bg);
    stroke: var(--line-2);
    stroke-width: 1;
  }

  .state.focal {
    fill: var(--accent-soft);
    stroke: var(--accent);
    stroke-width: 1.2;
  }

  .state.expired {
    fill: var(--warn-soft);
    stroke: var(--warn);
  }

  text {
    font-family: var(--sans);
  }

  .state-name {
    fill: var(--ink);
    font-size: 13px;
    font-weight: 650;
  }

  .state-meta,
  .transition-label,
  .safety-note {
    fill: var(--muted);
    font-size: 10px;
  }

  .transition-label {
    font-weight: 550;
  }

  .safety-note {
    fill: var(--warn);
  }
</style>
