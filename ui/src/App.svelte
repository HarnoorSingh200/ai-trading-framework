<script>
  import { currentTheme, followSystem, toggleTheme } from "./lib/theme.js";
  import AgentsTab from "./tabs/AgentsTab.svelte";
  import ControlTab from "./tabs/ControlTab.svelte";
  import DesignTab from "./tabs/DesignTab.svelte";
  import MarketTab from "./tabs/MarketTab.svelte";
  import OrdersTab from "./tabs/OrdersTab.svelte";
  import ReportingTab from "./tabs/ReportingTab.svelte";

  // Four module tabs in the order the framework routes data, then the central
  // process that runs them, then a guide that explains the whole system. Control
  // answers "is it all up"; Design answers "how does it work".
  const TABS = [
    { id: "market", index: 1, label: "Market data", component: MarketTab },
    { id: "agents", index: 2, label: "Agent engine" },
    { id: "orders", index: 3, label: "Order manager" },
    { id: "reporting", index: 4, label: "Reporting" },
    { id: "control", index: 0, label: "Control" },
    { id: "design", index: "i", label: "Design" },
  ];

  //: Every module is built now, so no tab carries a "soon" badge and none of
  //: them has to explain what it will own one day. The Control tab is the
  //: central process's own screen, not a fifth module.
  const BUILT = new Set(["market", "agents", "orders", "reporting", "control", "design"]);

  let active = $state("market");
  let theme = $state("light");
  let nav = $state(null);
  let viewportWidth = $state(window.innerWidth);
  const current = $derived(TABS.find((tab) => tab.id === active) ?? TABS[0]);

  $effect(() => {
    theme = currentTheme();
    return followSystem((next) => (theme = next));
  });

  $effect(() => {
    const measure = () => (viewportWidth = window.innerWidth);
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  });

  // Keep the selected view visible when the header becomes a horizontal
  // scroller. This moves the nav only; the page itself must not jump.
  $effect(() => {
    void active;
    void viewportWidth;
    if (!nav) return;
    const frame = requestAnimationFrame(() => {
      const button = nav.querySelector(`[data-view="${active}"]`);
      if (!button) return;
      const navBox = nav.getBoundingClientRect();
      const tabBox = button.getBoundingClientRect();
      if (tabBox.left < navBox.left) {
        nav.scrollLeft += tabBox.left - navBox.left - 8;
      } else if (tabBox.right > navBox.right) {
        nav.scrollLeft += tabBox.right - navBox.right + 8;
      }
    });
    return () => cancelAnimationFrame(frame);
  });
</script>

<header class="topbar" id="top">
  <div class="brand">
    <span class="wordmark">Trading Framework</span>
    <span class="version">v3</span>
  </div>

  <nav bind:this={nav} aria-label="Workspace views">
    {#each TABS as tab (tab.id)}
      <button
        class="tab"
        class:active={active === tab.id}
        data-view={tab.id}
        aria-current={active === tab.id ? "page" : undefined}
        onclick={() => (active = tab.id)}
      >
        <span class="tab-index numeric">{tab.index}</span>
        <span class="tab-label">{tab.label}</span>
        {#if !BUILT.has(tab.id)}<span class="tab-soon">soon</span>{/if}
      </button>
    {/each}
  </nav>

  <button
    class="theme"
    onclick={() => (theme = toggleTheme(theme))}
    aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
    title={theme === "dark" ? "Light theme" : "Dark theme"}
  >
    {#if theme === "dark"}
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="4.1" />
        <path
          d="M12 2.6v2.3M12 19.1v2.3M2.6 12h2.3M19.1 12h2.3M5.4 5.4l1.6 1.6M17 17l1.6 1.6M18.6 5.4L17 7M7 17l-1.6 1.6"
        />
      </svg>
    {:else}
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M20.6 14.3A8.7 8.7 0 1 1 9.7 3.4a7 7 0 0 0 10.9 10.9Z" />
      </svg>
    {/if}
  </button>
</header>

<main>
  {#if current.id === "market"}
    <MarketTab />
  {:else if current.id === "agents"}
    <AgentsTab />
  {:else if current.id === "orders"}
    <OrdersTab />
  {:else if current.id === "control"}
    <ControlTab />
  {:else if current.id === "design"}
    <DesignTab />
  {:else}
    <ReportingTab />
  {/if}
</main>

<style>
  .topbar {
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    gap: var(--space-6);
    padding: 0 var(--space-5);
    height: 58px;
    background: var(--bg);
    border-bottom: 1px solid var(--line);
  }

  .brand {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    white-space: nowrap;
  }

  .wordmark {
    font-size: var(--step-1);
    font-weight: 650;
    letter-spacing: -0.015em;
  }

  .version {
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--faint);
  }

  nav {
    display: flex;
    align-items: stretch;
    gap: var(--space-5);
    height: 100%;
  }

  .theme {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    background: none;
    border: 1px solid var(--line);
    border-radius: 50%;
    color: var(--muted);
    transition: color 0.12s ease, border-color 0.12s ease, background 0.12s ease;
  }

  .theme:hover {
    color: var(--ink);
    background: var(--surface);
    border-color: var(--line-2);
  }

  .theme svg {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.7;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .tab {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: 0;
    background: none;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--muted);
    font-size: var(--step-0);
    font-weight: 550;
    letter-spacing: -0.005em;
    white-space: nowrap;
    transition: color 0.12s ease, border-color 0.12s ease;
  }

  .tab:hover {
    color: var(--ink);
  }

  .tab.active {
    color: var(--ink);
    border-bottom-color: var(--accent);
  }

  .tab-index {
    font-size: var(--step--1);
    font-weight: 600;
    color: var(--faint);
  }

  .tab.active .tab-index {
    color: var(--accent);
  }

  .tab-soon {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--faint);
  }

  main {
    padding: var(--space-5) var(--space-5) var(--space-6);
    max-width: 1680px;
  }

  @media (max-width: 900px) {
    .topbar {
      gap: var(--space-4);
      padding: 0 var(--space-4);
      overflow-x: auto;
    }

    nav {
      gap: var(--space-4);
    }

    main {
      padding: var(--space-4) var(--space-4) var(--space-6);
    }
  }

  @media (max-width: 620px) {
    .topbar {
      flex-wrap: wrap;
      gap: 0;
      height: auto;
      min-height: 58px;
      overflow: visible;
    }

    .brand {
      min-height: 54px;
    }

    .theme {
      margin-left: auto;
    }

    nav {
      order: 3;
      width: 100%;
      min-height: 40px;
      gap: var(--space-4);
      overflow-x: auto;
    }

    .tab {
      flex: 0 0 auto;
      min-height: 40px;
    }
  }
</style>
