// Every read goes through the same origin: Vite forwards /api to Module 1,
// /agents to Module 2's control service, /orders to Module 3's order book and
// /report to Module 4's archive, and /central to the runner's control surface,
// so the browser never needs a second address and no service needs a CORS
// surface.

const MODULE1 = "/api";
const MODULE2 = "/agents";
const MODULE3 = "/orders";
const MODULE4 = "/report";
const CENTRAL = "/central";

async function call(base, path, { method = "GET", body } = {}) {
  const response = await fetch(`${base}${path}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = payload.detail?.error || payload.error || {};
    throw new Error(error.message || `${response.status} ${response.statusText}`);
  }
  return payload.data !== undefined ? payload.data : payload;
}

const request = (path, options) => call(MODULE1, path, options);
const control = (path, options) => call(MODULE2, path, options);
const book = (path, options) => call(MODULE3, path, options);
const report = (path, options) => call(MODULE4, path, options);

export const getQuotes = () => request("/v1/market/quotes");
export const getStatus = () => request("/v1/market/status");
export const getUniverse = () => request("/v1/market/universe");
export const getTimeframes = () => request("/v1/market/timeframes");
export const getBoard = (interval) =>
  request(`/v1/market/board?interval=${encodeURIComponent(interval)}`);
export const getIndicatorCatalogue = () => request("/v1/market/indicators/catalogue");
export const getIndicators = (symbol, interval) =>
  request(
    `/v1/market/indicators?symbol=${encodeURIComponent(symbol)}` +
      `&interval=${encodeURIComponent(interval)}`,
  );
export const getCandles = (symbol, interval, limit = 60) =>
  request(
    `/v1/market/candles?symbol=${encodeURIComponent(symbol)}` +
      `&interval=${encodeURIComponent(interval)}&limit=${limit}`,
  );
export const refresh = () => request("/v1/market/refresh", { method: "POST" });
export const login = () => request("/v1/market/session/login", { method: "POST" });

// -- Module 2: the agent registry and its logs ------------------------------ //

export const listAgents = () => control("/v1/agents");
export const createAgent = (definition) =>
  control("/v1/agents", { method: "POST", body: definition });
export const updateAgent = (agentId, definition) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}`, { method: "PUT", body: definition });
export const retireAgent = (agentId) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
export const startAgent = (agentId) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/start`, { method: "POST" });
export const stopAgent = (agentId) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/stop`, { method: "POST" });
export const getAgentActivity = (agentId, limit = 200) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/activity?limit=${limit}`);
export const getAgentDecisions = (agentId, limit = 50) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/decisions?limit=${limit}`);

// The notebook an agent carries forward: written by the agent, read (and
// prunable) by the operator. A lesson that is wrong is worse than none.
export const getAgentNotes = (agentId) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/notes`);
export const deleteAgentNote = (agentId, key) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/notes/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
export const clearAgentNotes = (agentId) =>
  control(`/v1/agents/${encodeURIComponent(agentId)}/notes`, { method: "DELETE" });

// The feed is an EventSource, so it is a URL rather than a fetch.
export const agentStreamUrl = (agentId) =>
  `${MODULE2}/v1/agents/${encodeURIComponent(agentId)}/stream`;

// -- Module 3: the order book ----------------------------------------------- //

export const listOrders = ({ status, agentId, symbol, limit = 200 } = {}) => {
  const query = new URLSearchParams();
  if (status) query.set("status", status);
  if (agentId) query.set("agent_id", agentId);
  if (symbol) query.set("symbol", symbol);
  query.set("limit", String(limit));
  return book(`/v1/orders?${query}`);
};
export const getOrder = (orderId) => book(`/v1/orders/${encodeURIComponent(orderId)}`);
export const getOrderEvents = (orderId) =>
  book(`/v1/orders/${encodeURIComponent(orderId)}/events`);
export const getOrderBookStatus = () => book("/v1/status");
export const reconcileOrders = () => book("/v1/orders/reconcile", { method: "POST" });

// The order feed is an EventSource, so it is a URL rather than a fetch.
export const orderStreamUrl = () => `${MODULE3}/v1/orders/stream`;

// -- Module 4: the long record ---------------------------------------------- //

export const getHistory = ({ agentId, day, status, limit = 200 } = {}) => {
  const query = new URLSearchParams();
  if (agentId) query.set("agent_id", agentId);
  if (day) query.set("day", day);
  if (status) query.set("status", status);
  query.set("limit", String(limit));
  return report(`/v1/orders?${query}`);
};
export const getPerformance = ({ agentId, day } = {}) => {
  const query = new URLSearchParams();
  if (agentId) query.set("agent_id", agentId);
  if (day) query.set("day", day);
  return report(`/v1/performance?${query}`);
};
export const getHistoryDays = () => report("/v1/days");
export const getHistoryAgents = () => report("/v1/agents");
export const getAnalytics = ({ agentId, day } = {}) => {
  const query = new URLSearchParams();
  if (agentId) query.set("agent_id", agentId);
  if (day) query.set("day", day);
  return report(`/v1/analytics?${query}`);
};
export const getReportingStatus = () => report("/v1/status");
export const syncHistory = () => report("/v1/sync", { method: "POST" });
export const getBenchmark = () => report("/v1/benchmark");
export const getTokenUsage = ({ day, agentId } = {}) => {
  const query = new URLSearchParams();
  if (day) query.set("day", day);
  if (agentId) query.set("agent_id", agentId);
  const suffix = query.toString();
  return report(suffix ? `/v1/usage?${suffix}` : "/v1/usage");
};
export const syncTokenUsage = () => report("/v1/usage/sync", { method: "POST" });

// -- The central process: the runner's control surface ---------------------- //

// The status carries the day (phase, window, resting reasons) and one report
// per service: state, pid, uptime, health and how many times it has started.
export const getControlStatus = () => call(CENTRAL, "/v1/status");
export const restartService = (service) =>
  call(CENTRAL, `/v1/services/${encodeURIComponent(service)}/restart`, {
    method: "POST",
  });
export const getServiceLog = (service, kind = "log", lines = 300) =>
  call(
    CENTRAL,
    `/v1/logs/${encodeURIComponent(service)}` +
      `?kind=${encodeURIComponent(kind)}&lines=${encodeURIComponent(lines)}`,
  );

// When a module is not answering, the honest reason may be that the market is
// closed and the central process is resting it - not that something broke.
// The tabs ask before alarming anyone: null means "no central process, or the
// service is not resting", and the plain "not answering" banner stands.
const RESTING_HEADLINES = {
  pre_market: "The market is not open yet.",
  day_over: "The trading day is over.",
  closed_today: "The market is closed today.",
  after_decisions: "The decision window is over.",
};

export async function restingReport(service) {
  try {
    const control = await getControlStatus();
    const report = control?.services?.[service];
    if (!report || report.state !== "resting") return null;
    return {
      headline: RESTING_HEADLINES[control.phase] ?? "Nothing to show here.",
      note: report.detail ?? "the service is resting for the market",
    };
  } catch {
    return null;
  }
}
