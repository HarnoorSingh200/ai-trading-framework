import { createLogger, defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// The site talks only to its own origin. Vite forwards /api to Module 1,
// /agents to Module 2's control service, /orders to Module 3's order book and
// /report to Module 4's archive, so the browser never needs a second address
// and none of them needs a CORS surface. /central goes to the runner, the
// central process whose Control tab reads the process overview, the logs and
// the restart buttons.
const MODULE1 = process.env.MODULE1_URL || "http://127.0.0.1:5201";
const MODULE2 = process.env.MODULE2_URL || "http://127.0.0.1:5202";
const MODULE3 = process.env.MODULE3_URL || "http://127.0.0.1:5203";
const MODULE4 = process.env.MODULE4_URL || "http://127.0.0.1:5204";
const CENTRAL = process.env.CONTROL_URL || "http://127.0.0.1:5205";

// A module that is not up yet is a normal state, not a fault: the site is
// often opened first, and every tab retries until its module answers. So a
// refused connection becomes a 503 with the same JSON shape the modules use -
// the tabs say "waiting" - and the stack trace Vite prints for every retry is
// dropped, because a red wall in the log is not a diagnosis of anything.
const quiet = (proxy) => {
  proxy.on("error", (error, request, response) => {
    if (!response || response.headersSent) return;
    response.writeHead(503, { "Content-Type": "application/json" });
    response.end(
      JSON.stringify({
        ok: false,
        error: {
          code: "MODULE_NOT_UP",
          message: `${request?.url ?? "the module"} is not answering yet`,
          detail: error?.code ?? error?.message ?? "connection refused",
        },
      }),
    );
  });
};

const logger = createLogger();
const logError = logger.error.bind(logger);
logger.error = (message, options) => {
  if (typeof message === "string" && message.includes("http proxy error")) return;
  logError(message, options);
};

export default defineConfig({
  customLogger: logger,
  plugins: [svelte()],
  server: {
    host: "127.0.0.1",
    port: 5200,
    strictPort: true,
    proxy: {
      "/api": {
        target: MODULE1,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        configure: quiet,
      },
      "/agents": {
        target: MODULE2,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/agents/, ""),
        configure: quiet,
      },
      "/orders": {
        target: MODULE3,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/orders/, ""),
        configure: quiet,
      },
      "/report": {
        target: MODULE4,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/report/, ""),
        configure: quiet,
      },
      "/central": {
        target: CENTRAL,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/central/, ""),
        configure: quiet,
      },
    },
  },
});
