<svg
  class="system-map"
  viewBox="0 0 980 500"
  role="img"
  aria-labelledby="system-map-title system-map-desc"
>
  <title id="system-map-title">Trading Framework v3 message map</title>
  <desc id="system-map-desc">
    The broker feeds market data; agents read market facts and send decisions to the
    order manager; reporting reads from Modules 1, 2 and 3 and keeps the local and S3 archive.
  </desc>

  <defs>
    <marker
      id="map-arrow"
      markerWidth="8"
      markerHeight="7"
      refX="7"
      refY="3.5"
      orient="auto"
      markerUnits="userSpaceOnUse"
    >
      <path class="map-arrow-head" d="M0,0 L8,3.5 L0,7 Z" />
    </marker>
    <marker
      id="map-arrow-accent"
      markerWidth="8"
      markerHeight="7"
      refX="7"
      refY="3.5"
      orient="auto"
      markerUnits="userSpaceOnUse"
    >
      <path class="map-arrow-accent-head" d="M0,0 L8,3.5 L0,7 Z" />
    </marker>
  </defs>

  <!-- Connections are painted first so every arrow sits behind its endpoint. -->
  <path class="map-edge" d="M170 140 H210" marker-end="url(#map-arrow)" />
  <text class="edge-label" x="190" y="128" text-anchor="middle">WSS + HTTPS</text>

  <path class="map-edge" d="M420 140 H360" marker-end="url(#map-arrow)" />
  <text class="edge-label" x="390" y="128" text-anchor="middle">pull facts</text>

  <path
    class="map-edge accent-edge"
    d="M570 140 H630"
    marker-end="url(#map-arrow-accent)"
  />
  <text class="edge-label accent-label" x="600" y="128" text-anchor="middle">decisions</text>

  <path
    class="map-edge dashed"
    d="M705 100 V58 H285 V100"
    marker-end="url(#map-arrow)"
  />
  <text class="edge-label" x="495" y="49" text-anchor="middle">prices · SSE + 15s fallback</text>

  <path class="map-edge dashed" d="M495 180 V315" marker-end="url(#map-arrow)" />
  <text class="edge-label" x="505" y="255" text-anchor="start">usage</text>

  <path
    class="map-edge dashed"
    d="M285 180 V350 H430"
    marker-end="url(#map-arrow)"
  />
  <text class="edge-label" x="345" y="340" text-anchor="middle">benchmark</text>

  <path
    class="map-edge dashed"
    d="M705 180 V350 H590"
    marker-end="url(#map-arrow)"
  />
  <text class="edge-label" x="650" y="340" text-anchor="middle">day book</text>

  <!-- Broker boundary -->
  <rect class="node external" x="30" y="100" width="140" height="80" rx="8" />
  <text class="node-kicker" x="46" y="124">BROKER</text>
  <text class="node-name" x="46" y="148">Kite web</text>
  <text class="node-meta" x="46" y="166">session · history · WSS</text>

  <!-- Modules -->
  <rect class="node" x="210" y="100" width="150" height="80" rx="8" />
  <text class="node-index" x="226" y="124">01</text>
  <text class="node-name" x="246" y="124">Market data</text>
  <text class="node-meta" x="226" y="148">history · ticks · candles</text>
  <text class="node-meta" x="226" y="164">indicators · benchmark</text>

  <rect class="node" x="420" y="100" width="150" height="80" rx="8" />
  <text class="node-index" x="436" y="124">02</text>
  <text class="node-name" x="456" y="124">Agent engine</text>
  <text class="node-meta" x="436" y="148">own clock · model rounds</text>
  <text class="node-meta" x="436" y="164">notes · sized decisions</text>

  <rect class="node focal" x="630" y="100" width="150" height="80" rx="8" />
  <text class="node-index accent-text" x="646" y="124">03</text>
  <text class="node-name" x="666" y="124">Order manager</text>
  <text class="node-meta" x="646" y="148">paper positions</text>
  <text class="node-meta" x="646" y="164">target · stop · clock</text>

  <rect class="node" x="430" y="315" width="160" height="88" rx="8" />
  <text class="node-index" x="446" y="339">04</text>
  <text class="node-name" x="466" y="339">Reporting</text>
  <text class="node-meta" x="446" y="363">local history archive</text>
  <text class="node-meta" x="446" y="379">tokens · benchmark · S3</text>

  <!-- Runner is a supervisor, not a data bus. -->
  <rect class="runner-band" x="30" y="432" width="910" height="36" rx="7" />
  <text class="runner-label" x="48" y="455">RUNNER</text>
  <text class="runner-copy" x="112" y="455">
    starts · waits for readiness · probes /health every 30s · restarts · stops
  </text>
</svg>

<style>
  .system-map {
    display: block;
    width: 100%;
    min-width: 760px;
    height: auto;
    background: var(--surface);
  }

  .map-edge {
    fill: none;
    stroke: var(--line-2);
    stroke-width: 1.3;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .map-edge.dashed {
    stroke-dasharray: 5 4;
  }

  .accent-edge {
    stroke: var(--accent);
    stroke-width: 1.8;
  }

  .map-arrow-head {
    fill: var(--line-2);
  }

  .map-arrow-accent-head {
    fill: var(--accent);
  }

  .edge-label {
    fill: var(--muted);
    font-family: var(--sans);
    font-size: 10px;
    font-weight: 550;
  }

  .edge-label.accent-label {
    fill: var(--accent);
    font-weight: 650;
  }

  .node {
    fill: var(--bg);
    stroke: var(--line-2);
    stroke-width: 1;
  }

  .node.external {
    fill: var(--surface-2);
    stroke-dasharray: 4 3;
  }

  .node.focal {
    fill: var(--accent-soft);
    stroke: var(--accent);
    stroke-width: 1.2;
  }

  .node-index {
    fill: var(--muted);
    font-family: var(--sans);
    font-size: 9px;
    font-weight: 700;
  }

  .node-index.accent-text {
    fill: var(--accent);
  }

  .node-kicker {
    fill: var(--muted);
    font-family: var(--sans);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.1em;
  }

  text {
    font-family: var(--sans);
  }

  .node-name {
    fill: var(--ink);
    font-size: 12px;
    font-weight: 650;
  }

  .node-meta {
    fill: var(--muted);
    font-size: 9.5px;
  }

  .runner-band {
    fill: var(--warn-soft);
    stroke: var(--warn);
    stroke-width: 1;
  }

  .runner-label {
    fill: var(--warn);
    font-size: 9px;
    font-weight: 750;
    letter-spacing: 0.1em;
  }

  .runner-copy {
    fill: var(--ink-2);
    font-size: 10px;
  }
</style>
