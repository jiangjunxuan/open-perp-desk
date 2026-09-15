import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { checkResearchUI } from "./research-ui-checks.mjs";
import { checkAppearance } from "./appearance-ui-checks.mjs";
import { checkManagement } from "./management-ui-checks.mjs";
import { checkBillHistory } from "./bill-history-ui-checks.mjs";
import { checkRealtime } from "./realtime-ui-checks.mjs";
import { checkChartAnnotations } from "./chart-annotation-ui-checks.mjs";
import { checkPositionLots } from "./position-lots-ui-checks.mjs";
import { checkProtectionIncidents } from "./protection-incident-ui-checks.mjs";
import { checkProtectionReview } from "./protection-review-ui-checks.mjs";
import { checkTradingView } from "./tradingview-ui-checks.mjs";

const root = process.cwd();
const outputDirectory = path.resolve(root, process.env.OPENPERPDESK_OUTPUT_DIR || "outputs");
const origin = process.env.OPENPERPDESK_ORIGIN || "http://127.0.0.1:8099";
const chromePath = process.env.CHROME_BIN || (
  process.platform === "darwin"
    ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    : "google-chrome"
);
const profile = await mkdtemp(path.join(tmpdir(), "openperpdesk-ui-"));
const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--no-sandbox",
    "--no-zygote",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-background-networking",
    "--disable-component-update",
    ...(process.env.OPENPERPDESK_DISABLE_PROXY === "true" ? ["--no-proxy-server"] : []),
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "pipe"] },
);
let spawnError;
const chromeStderr = [];
chrome.once("error", (error) => { spawnError = error; });
chrome.stderr.on("data", chunk => {
  chromeStderr.push(chunk.toString());
  if (chromeStderr.length > 20) chromeStderr.shift();
});

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let socket;
let nextId = 1;
const pending = new Map();
const browserErrors = [];

async function command(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, 15000);
    pending.set(id, { resolve, reject, timeout });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const response = await command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.text);
  }
  return response.result.value;
}

try {
  let target;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (spawnError) throw spawnError;
    if (chrome.exitCode !== null) {
      throw new Error(`Chrome exited before CDP startup (${chrome.exitCode}): ${chromeStderr.join("").slice(-3000)}`);
    }
    try {
      const portFile = await readFile(path.join(profile, "DevToolsActivePort"), "utf8");
      const port = Number(portFile.split("\n")[0]);
      if (!Number.isInteger(port) || port <= 0) throw new Error("Invalid debug port");
      const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {
        method: "PUT",
      });
      target = await response.json();
      break;
    } catch {
      await delay(100);
    }
  }
  if (!target) throw new Error(`Chrome debugging endpoint did not start: ${chromeStderr.join("").slice(-3000)}`);
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const response = JSON.parse(event.data);
    if (response.method === "Runtime.exceptionThrown") {
      browserErrors.push(response.params.exceptionDetails.exception?.description || response.params.exceptionDetails.text);
      console.error(browserErrors.at(-1));
    }
    if (response.method === "Network.loadingFailed" && response.params.errorText !== "net::ERR_ABORTED") {
      console.error(`Browser network failure: ${response.params.type}: ${response.params.errorText}`);
    }
    const waiting = pending.get(response.id);
    if (!waiting) return;
    pending.delete(response.id);
    clearTimeout(waiting.timeout);
    if (response.error) waiting.reject(new Error(response.error.message));
    else waiting.resolve(response.result || {});
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Network.enable");
  await mkdir(outputDirectory, { recursive: true });

  const results = [];
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "laptop", width: 1024, height: 900 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "landscape", width: 844, height: 390 },
    { name: "narrow-mobile", width: 320, height: 740 },
    { name: "small-mobile", width: 375, height: 812 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    console.error(`Checking trading console layout: ${viewport.name}`);
    await command("Emulation.setDeviceMetricsOverride", {
      width: viewport.width,
      height: viewport.height,
      deviceScaleFactor: 1,
      mobile: viewport.width < 768,
    });
    await command("Page.navigate", { url: `${origin}/` });
    let marketReady = false;
    for (let attempt = 0; attempt < 200; attempt++) {
      await delay(100);
      marketReady = await evaluate('document.querySelector("#market-price")?.textContent !== "--" && document.querySelector("#chart-empty")?.hidden === true && !document.querySelector("#refresh-market")?.hasAttribute("aria-busy")');
      if (marketReady) break;
    }
    if (!marketReady) throw new Error(`${viewport.name} market did not finish loading`);
    const layout = await evaluate(`(() => {
      const canvas = document.querySelector("#price-chart");
      const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      let painted = 0;
      for (let i = 3; i < pixels.length; i += 4) if (pixels[i] > 0) painted++;
      return {
        width: innerWidth,
        clientWidth: document.documentElement.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
        documentHeight: document.documentElement.scrollHeight,
        overflowElements: [...document.querySelectorAll("body *")]
          .filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.right > ${viewport.width} + 1 && rect.width > 0;
          })
          .slice(0, 30)
          .map((element) => ({
            tag: element.tagName,
            id: element.id,
            className: element.className,
            width: Math.round(element.getBoundingClientRect().width),
          })),
        watchItems: document.querySelectorAll(".watch-item").length,
        price: document.querySelector("#market-price").textContent,
        canvasPaintedPixels: painted,
        execution: document.querySelector("#top-execution").textContent
      };
    })()`);
    const screenshot = await command("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
      clip: {
        x: 0,
        y: 0,
        width: viewport.width,
        height: Math.min(layout.documentHeight, viewport.height * 3),
        scale: 1,
      },
    });
    await writeFile(
      path.join(outputDirectory, `openperpdesk-dashboard-${viewport.name}.png`),
      Buffer.from(screenshot.data, "base64"),
    );
    results.push({ viewport: viewport.name, ...layout });
    if (layout.documentWidth > viewport.width || layout.width > viewport.width) {
      throw new Error(`${viewport.name} horizontal overflow: ${JSON.stringify(layout)}`);
    }
    if (layout.canvasPaintedPixels < 100) {
      throw new Error(`${viewport.name} candlestick canvas is blank`);
    }

    const routeChecks = [];
    for (const view of ["markets", "positions", "orders", "fills", "strategies", "performance", "risk", "activity", "overview"]) {
      await evaluate(`document.querySelector('[data-section="${view}"]').click()`);
      await delay(80);
      const route = await evaluate(`(() => {
        const visible = [...document.querySelectorAll(".panel")].filter(el => el.getClientRects().length);
        return {
          view: document.body.dataset.view,
          title: document.querySelector("#page-title").textContent,
          width: document.documentElement.scrollWidth,
          active: document.querySelectorAll('.nav-item[aria-current="page"]').length,
          panels: visible.map(el => el.id),
          mismatch: visible.some(el => !el.dataset.views.split(" ").includes("${view}")),
          focus: document.activeElement.id,
          protectedControlsLocked: [...document.querySelectorAll("#execute-signal, #unlock-live, #emergency-stop, #toggle-worker")].every(el => el.disabled),
        };
      })()`);
      if (route.view !== view || route.active !== 1 || route.mismatch || route.width > viewport.width || route.focus !== "page-title" || !route.protectedControlsLocked) {
        throw new Error(`${viewport.name} route failure: ${JSON.stringify(route)}`);
      }
      routeChecks.push(route);
      if (view === "markets") {
        await evaluate(`(() => {
          window.uiFocusBefore = {
            width: document.querySelector("#price-chart").getBoundingClientRect().width,
            size: document.querySelector("#size-input").value,
            ledger: state.ledger,
            ticket: state.ticket,
            candles: state.lastCandles
          };
          document.querySelector("#size-input").value = "2.75";
          document.querySelector("#toggle-chart-focus").click();
        })()`);
        await delay(80);
        const focusChecks = await evaluate(`(() => {
          const canvas = document.querySelector("#price-chart");
          const rect = canvas.getBoundingClientRect();
          const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
          const execution = document.querySelector(innerWidth < 768 ? "#mobile-execution" : "#top-execution");
          return {
            geometry: { width: rect.width, height: rect.height, viewport: innerWidth,
              clientWidth: document.documentElement.clientWidth,
              workspace: document.querySelector(".workspace").getBoundingClientRect().width,
              chartPanel: document.querySelector(".chart-panel").getBoundingClientRect().width },
            pressed: document.querySelector("#toggle-chart-focus").getAttribute("aria-pressed") === "true",
            chartVisible: rect.width > document.documentElement.clientWidth - 64 && rect.height >= 340,
            noOverflow: document.documentElement.scrollWidth <= innerWidth,
            painted: pixels.some((value, index) => index % 4 === 3 && value > 0),
            safeHeader: execution.getClientRects().length > 0 && execution.textContent === "执行已锁定"
              && document.querySelector("#emergency-stop").getClientRects().length > 0
              && document.querySelector("#emergency-stop").disabled,
            onlyChart: [...document.querySelectorAll(".panel")].filter(el => el.getClientRects().length).every(el => el.id === "markets")
          };
        })()`);
        if (Object.values(focusChecks).some(passed => !passed)) {
          throw new Error(`${viewport.name} focused chart failure: ${JSON.stringify(focusChecks)}`);
        }
        if (["desktop", "mobile"].includes(viewport.name)) {
          const focused = await command("Page.captureScreenshot", { format: "png" });
          await writeFile(path.join(outputDirectory, `openperpdesk-focus-${viewport.name}.png`), Buffer.from(focused.data, "base64"));
        }
        await command("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
        await command("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
        await delay(80);
        focusChecks.restored = await evaluate(`(() => {
          const before = window.uiFocusBefore;
          const restored = !state.chartFocused && document.activeElement.id === "toggle-chart-focus"
            && document.querySelector("#size-input").value === "2.75"
            && state.ticket === before.ticket && state.ledger === before.ledger && state.lastCandles.length === before.candles.length
            && Math.abs(document.querySelector("#price-chart").getBoundingClientRect().width - before.width) < 2;
          document.querySelector("#size-input").value = before.size;
          return restored;
        })()`);
        if (!focusChecks.restored) throw new Error(`${viewport.name} focus exit lost draft, data or layout`);
        focusChecks.routeExit = await evaluate(`(() => {
          document.querySelector("#toggle-chart-focus").click();
          showView("risk");
          const exited = !state.chartFocused && !document.body.classList.contains("chart-focused");
          showView("markets");
          return exited;
        })()`);
        if (!focusChecks.routeExit) throw new Error(`${viewport.name} route left focus mode active`);
        route.chartFocus = focusChecks;
        await delay(80);
        const terminal = await evaluate(`(() => {
          const checks = {};
          checks.onlyOneLedger = ["positions", "orders", "fills"].every(name => {
            document.querySelector('[data-ledger="' + name + '"]').click();
            return !document.querySelector("#" + name).hidden
              && ["positions", "orders", "fills"].filter(id => !document.querySelector("#" + id).hidden).length === 1;
          });
          document.querySelector('[data-ledger="positions"]').click();
          document.querySelector('[data-ledger="positions"]').dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
          checks.ledgerKeyboard = document.activeElement.id === "ledger-tab-orders" && !document.querySelector("#orders").hidden;
          document.querySelector('[data-ledger="positions"]').click();
          const canvas = document.querySelector("#price-chart");
          canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
          const firstTime = document.querySelector("#candle-time").textContent;
          canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
          checks.chartKeyboard = firstTime !== document.querySelector("#candle-time").textContent && !document.querySelector("#chart-cursor").hidden;
          canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
          const before = canvas.toDataURL();
          document.querySelector('[data-chart-mode="line"]').click();
          checks.lineChart = state.chartMode === "line" && canvas.toDataURL() !== before;
          document.querySelector('[data-chart-mode="candles"]').click();
          document.querySelector("#chart-range").value = "40";
          document.querySelector("#chart-range").dispatchEvent(new Event("change"));
          checks.chartRange = state.chartGeometry.candles.length === 40;
          document.querySelector("#chart-range").value = "80";
          document.querySelector("#chart-range").dispatchEvent(new Event("change"));
          renderPreflight({ accepted: true, dry_run: true, preflight: { basis: "exchange", order_notional: 42, current_notional: 60 } });
          checks.exchangeBasis = document.querySelector("#preflight-basis").textContent === "交易所快照"
            && document.querySelector("#preflight-notional").textContent === "42 USD";
          document.querySelector("#size-input").dispatchEvent(new Event("input"));
          checks.draftInvalidation = document.querySelector("#preflight-state").textContent === "未核验"
            && document.querySelector("#preflight-notional").textContent === "--";
          renderPreflight({ accepted: true, dry_run: true, preflight: { basis: "simulation" } });
          checks.simulationIsExplicit = document.querySelector("#preflight-message").textContent.includes("未校验交易所账户");
          clearPreflight();
          document.querySelector("#ticket-tab-execution").click();
          checks.ticketSwitch = !document.querySelector("#execution-pane").hidden
            && document.querySelector("#signal-pane").hidden;
          const sizeBefore = document.querySelector("#size-input").value;
          document.querySelector("#size-input").value = "3";
          document.querySelector("#ticket-tab-execution").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
          checks.ticketKeyboard = document.activeElement.id === "ticket-tab-signal"
            && !document.querySelector("#signal-pane").hidden && document.querySelector("#execution-pane").hidden;
          document.querySelector("#review-order").click();
          checks.ticketKeepsDraft = document.querySelector("#size-input").value === "3"
            && document.activeElement.id === "ticket-tab-execution";
          document.querySelector("#size-input").value = sizeBefore;
          checks.missingValues = [null, undefined, ""].every(value => formatNumber(value) === "--");
          const watch = document.querySelector('.watch-item[data-symbol="BTC-USDT-SWAP"]');
          watch.focus();
          renderWatchlist(state.lastTickers);
          checks.watchlistRetainsFocus = document.activeElement.dataset.symbol === "BTC-USDT-SWAP";
          checks.marketTextFits = [...document.querySelectorAll(".market-stats strong")]
            .every(el => el.scrollWidth <= el.clientWidth + 1);
          checks.noToolbarOverlap = [".chart-toolbar", ".analysis-actions"].every(selector => {
            const boxes = [...document.querySelector(selector).children]
              .filter(el => el.getClientRects().length && !el.classList.contains("sr-only"))
              .map(el => el.getBoundingClientRect());
            return boxes.every((a, i) => boxes.slice(i + 1).every(b =>
              Math.min(a.right, b.right) - Math.max(a.left, b.left) < 1 ||
              Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) < 1));
          });
          checks.toolbarContained = [".chart-toolbar", ".analysis-actions", ".market-workspace-toolbar"].every(selector => {
            const parent = document.querySelector(selector);
            const bounds = parent.getBoundingClientRect();
            return [...parent.children].filter(el => el.getClientRects().length && !el.classList.contains("sr-only"))
              .every(el => {
                const rect = el.getBoundingClientRect();
                return rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1;
              });
          });
          const sync = document.querySelector("#sync-ledger");
          checks.visibleSyncEntry = sync.getClientRects().length > 0 && sync.disabled
            && sync.getAttribute("aria-label") === "同步账户与订单";
          if (innerWidth < 768) {
            const details = document.querySelector("#toggle-market-details");
            const stats = document.querySelector("#market-stats");
            checks.compactMarketDetails = details.getAttribute("aria-expanded") === "false"
              && stats.getClientRects().length === 0;
            details.click();
            checks.expandMarketDetails = details.getAttribute("aria-expanded") === "true"
              && stats.getClientRects().length > 0;
            details.click();
            window.scrollTo(0, 0);
            checks.chartInFirstViewport = canvas.getBoundingClientRect().top >= 0
              && canvas.getBoundingClientRect().top + 80 < innerHeight;
          }
          document.querySelector("#ticket-tab-signal").click();
          return checks;
        })()`);
        if (Object.values(terminal).some(passed => !passed)) {
          throw new Error(`${viewport.name} terminal interaction failure: ${JSON.stringify(terminal)}`);
        }
        route.terminal = terminal;
      }
      if (view === "orders") {
        const filtering = await evaluate(`(() => {
          const checks = {};
          const ordersBody = document.querySelector("#orders-body").innerHTML;
          const orders = [
            { inst_id: "BTC-USDT-SWAP", client_order_id: "fixture-alpha", status: "live", side: "buy", size: 1 },
            { inst_id: "ETH-USDT-SWAP", client_order_id: "fixture-beta", status: "filled", side: "sell", size: 2 },
            { inst_id: "BTC-USDT-SWAP", client_order_id: "fixture-pending", status: "submission_unknown", side: "buy", size: 1 }
          ];
          renderOrders(orders);
          const query = document.querySelector("#orders-query");
          query.value = "btc";
          query.dispatchEvent(new Event("input"));
          checks.contract = document.querySelectorAll("#orders-body tr").length === 2
            && document.querySelector("#ledger-count-orders").textContent === "3";
          const status = document.querySelector("#orders-status");
          status.value = "attention";
          status.dispatchEvent(new Event("change"));
          checks.attention = document.querySelectorAll("#orders-body tr").length === 1
            && document.querySelector("#orders-body").textContent.includes("提交结果待确认");
          query.value = "missing-contract";
          query.dispatchEvent(new Event("input"));
          checks.empty = document.querySelector("#orders-body").textContent.includes("没有匹配的记录");
          document.querySelector('[data-clear-records="orders"]').click();
          checks.clear = query.value === "" && status.value === "all"
            && document.activeElement === query && document.querySelectorAll("#orders-body tr").length === 3;
          query.value = "FIXTURE-BETA";
          query.dispatchEvent(new Event("input"));
          renderOrders([...orders, { inst_id: "ETH-USDT-SWAP", client_order_id: "fixture-new", status: "live" }]);
          checks.filterSurvivesRefresh = document.querySelectorAll("#orders-body tr").length === 1
            && document.querySelector("#orders-filter-count").textContent === "已载入 4 条 · 匹配 1 条";
          query.value = "";
          query.disabled = true;
          status.disabled = true;
          state.records.orders = null;
          document.querySelector("#orders-body").innerHTML = ordersBody;
          document.querySelector("#ledger-count-orders").textContent = "--";
          document.querySelector("#orders-filter-count").textContent = "账户未解锁";
          for (const kind of ["positions", "fills"]) {
            const body = document.querySelector("#" + kind + "-body");
            const before = body.innerHTML;
            recordRenderers[kind]([{ inst_id: "BTC-USDT-SWAP" }, { inst_id: "ETH-USDT-SWAP" }]);
            const input = document.querySelector("#" + kind + "-query");
            input.value = "eth";
            input.dispatchEvent(new Event("input"));
            checks[kind] = body.querySelectorAll("tr").length === 1 && body.textContent.includes("ETH-USDT-SWAP")
              && document.querySelector("#ledger-count-" + kind).textContent === "2";
            input.value = "";
            input.disabled = true;
            body.innerHTML = before;
            state.records[kind] = null;
            document.querySelector("#ledger-count-" + kind).textContent = "--";
            document.querySelector("#" + kind + "-filter-count").textContent = "账户未解锁";
          }
          document.querySelector("#metric-positions").textContent = "--";
          return checks;
        })()`);
        if (Object.values(filtering).some(passed => !passed)) {
          throw new Error(`${viewport.name} record filtering failure: ${JSON.stringify(filtering)}`);
        }
        route.filtering = filtering;
      }
      if (view === "performance") {
        const accounting = await evaluate(`(() => {
          renderPerformance({ valuation_status: "mixed_currency", net_pnl: null, return_pct: null, max_drawdown_pct: null, equity_curve: [] });
          const noFalseZero = document.querySelector("#performance-net-pnl").textContent === "--"
            && document.querySelector("#performance-return").textContent === "收益 --";
          renderAccountBills({
            configured: true, fresh: true, summary: { day_utc: "2026-09-13", by_currency: {
              USDT: { net_pnl: "-2", funding: "-2" }, BTC: { net_pnl: "-0.00000000000000001", funding: "-0.00000000000000001" }
            } },
            data: [{ bill_id: "ui-fixture", kind: "funding", inst_id: "BTC-USD-SWAP", currency: "BTC",
              timestamp_ms: 1789257600000, realized_pnl: "0", fees: "0", funding: "-0.00000000000000001", adjustments: "0", net_pnl: "-0.00000000000000001" }]
          });
          const rawPrecision = document.querySelector("#bills-body").textContent.includes("-0.00000000000000001");
          const body = document.querySelector("#bills-body").innerHTML;
          renderAccountBills({ error: "fixture failure" });
          const retainedOnError = body === document.querySelector("#bills-body").innerHTML && document.querySelector("#bills-status").textContent === "读取失败";
          const width = document.documentElement.scrollWidth;
          renderPerformance({});
          renderAccountBills({ configured: false, data: [] });
          return { noFalseZero, rawPrecision, retainedOnError, width };
        })()`);
        if (!accounting.noFalseZero || !accounting.rawPrecision || !accounting.retainedOnError || accounting.width > viewport.width) {
          throw new Error(`${viewport.name} accounting UI failure: ${JSON.stringify(accounting)}`);
        }
        route.accounting = accounting;
      }
      if (view === "strategies") {
        const settings = await evaluate(`(() => {
          const groups = [...document.querySelectorAll(".settings-fieldset")];
          const actions = document.querySelector(".strategy-save-actions").getBoundingClientRect();
          return {
            grouped: groups.map(el => el.querySelector("legend").textContent).join("|") === "运行模式|技术指标|仓位约束",
            inputsContained: groups.every(group => {
              const box = group.getBoundingClientRect();
              return [...group.querySelectorAll("input")].every(input => {
                const rect = input.getBoundingClientRect();
                return rect.left >= box.left - 1 && rect.right <= box.right + 1;
              });
            }),
            buttonsContained: [...document.querySelectorAll(".strategy-save-actions button")].every(button => {
              const box = button.getBoundingClientRect();
              return box.left >= actions.left - 1 && box.right <= actions.right + 1 && button.scrollWidth <= button.clientWidth + 1;
            })
          };
        })()`);
        if (Object.values(settings).some(passed => !passed)) {
          throw new Error(`${viewport.name} strategy layout failure: ${JSON.stringify(settings)}`);
        }
        route.settings = settings;
      }
      if (view === "risk") {
        const risk = await evaluate(`(() => {
          const checks = {};
          const workspace = document.querySelector(".workspace").getBoundingClientRect();
          const heading = document.querySelector(".page-heading").getBoundingClientRect();
          const limits = document.querySelector("#risk-limits").getBoundingClientRect();
          checks.headingSpansWorkspace = Math.abs(workspace.right - heading.right) < 2;
          checks.limitColumn = innerWidth < 1024 ? Math.abs(workspace.left - limits.left) < 2 : limits.width >= 296;
          const originalStatus = state.status;
          const originalToken = state.token;
          const originalAnalysis = state.analysis;
          const fixture = {
            ...originalStatus, execution_enabled: true, trading_mode: "live", risk_engine_ready: true,
            safety_control: { execution_allowed: true, emergency_stopped: false },
            live_safety: { allowed: false },
            risk_limits: { max_leverage: 2.5, max_position_pct: 7, max_total_notional_pct: 25, min_confidence: .73, max_daily_loss_pct: 0, max_stop_distance_pct: 4 }
          };
          state.token = "ui-local-state-only";
          applyStatus(fixture);
          checks.serverLimits = ["#limit-leverage", "#limit-position", "#limit-exposure", "#limit-confidence", "#limit-daily-loss", "#limit-stop-distance"]
            .map(selector => document.querySelector(selector).textContent).join("|") === "2.5倍|7%|25% 权益|73%|0%|4%";
          renderAnalysis({ signal: { action: "open_long" } });
          checks.liveLockConsistent = document.querySelector("#top-execution").textContent === "执行已锁定"
            && document.querySelector("#execute-signal").disabled;
          applyStatus({ ...fixture, trading_mode: "demo", risk_engine_ready: false });
          checks.riskNotReadyLocked = document.querySelector("#execute-signal").disabled
            && document.querySelector("#operation-summary").textContent.includes("执行已锁定");
          applyStatus({ ...fixture, trading_mode: "demo", safety_control: { execution_allowed: true, emergency_stopped: true } });
          checks.emergencyLocked = document.querySelector("#execute-signal").disabled
            && document.querySelector("#top-execution").textContent === "急停已触发";
          const liveGate = { allowed: false, configuration_enabled: true, mode_is_live: true };
          applyStatus({ ...fixture, live_safety: liveGate,
            safety_control: { execution_allowed: false, emergency_stopped: true } });
          checks.emergencyPreventsUnlock = document.querySelector("#unlock-live").disabled
            && document.querySelector("#live-safety-message").textContent.includes("急停已触发");
          applyStatus({ ...fixture, live_safety: liveGate });
          checks.resumeRequiresLiveUnlock = !document.querySelector("#unlock-live").disabled
            && document.querySelector("#execute-signal").disabled
            && document.querySelector("#live-safety-message").textContent.includes("仍需要人工解锁");
          applyStatus({ ...fixture, live_safety: { ...liveGate, allowed: true } });
          checks.liveAlreadyUnlocked = document.querySelector("#unlock-live").disabled;
          applyStatus({ ...fixture, risk_limits: { max_leverage: null } });
          checks.missingLimitsUnknown = [...document.querySelectorAll(".risk-limits dd")].every(el => el.textContent === "--");
          applyStatus({ ...fixture, market_stream: { candles_connected: true, candles_fresh: false },
            algo_stream: { configured: true, connected: true, authenticated: false } });
          checks.streamReadinessHonest = document.querySelector("#state-candle-stream").textContent === "等待数据"
            && document.querySelector("#state-algo-stream").textContent === "认证中";
          applyStatus({ ...fixture, market_stream: { candles_connected: true, candles_fresh: true },
            algo_stream: { configured: true, connected: true, authenticated: true } });
          checks.streamsReady = document.querySelector("#state-candle-stream").textContent === "在线"
            && document.querySelector("#state-algo-stream").textContent === "在线";
          applyStatus({ ...fixture, market_stream: {} });
          checks.missingStreamUnknown = document.querySelector("#state-candle-stream").textContent === "--";
          setBusy("#sync-ledger", true);
          updatePrivateActionAvailability();
          checks.syncBusyPreserved = document.querySelector("#sync-ledger").disabled
            && document.querySelector("#sync-ledger [data-icon]") !== null;
          setBusy("#sync-ledger", false);
          state.token = originalToken;
          renderAnalysis(originalAnalysis);
          applyStatus(originalStatus);
          checks.syncLockedAgain = document.querySelector("#sync-ledger").disabled;
          return checks;
        })()`);
        if (Object.values(risk).some(passed => !passed)) {
          throw new Error(`${viewport.name} risk presentation failure: ${JSON.stringify(risk)}`);
        }
        route.risk = risk;
      }
      if (["markets", "strategies", "risk"].includes(view)) {
        const documentHeight = await evaluate('document.querySelector("#page-title").focus({ preventScroll: true }); window.scrollTo(0, 0); document.documentElement.scrollHeight');
        await delay(50);
        const shot = await command("Page.captureScreenshot", {
          format: "png",
          captureBeyondViewport: true,
          clip: { x: 0, y: 0, width: viewport.width, height: Math.min(documentHeight, viewport.height * 3), scale: 1 },
        });
        await writeFile(path.join(outputDirectory, `openperpdesk-${view}-${viewport.name}.png`), Buffer.from(shot.data, "base64"));
        if (view === "markets") {
          await evaluate('document.querySelector("#ticket-tab-execution").click()');
          const executionShot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
          await writeFile(path.join(outputDirectory, `openperpdesk-execution-${viewport.name}.png`), Buffer.from(executionShot.data, "base64"));
          await evaluate('document.querySelector("#ticket-tab-signal").click(); window.scrollTo(0, 0)');
        }
      }
    }
    results.at(-1).routeChecks = routeChecks;
    if (viewport.width < 768) {
      await evaluate('document.querySelector("#open-navigation").click()');
      const drawer = await evaluate(`(() => {
        const dialog = document.querySelector("#navigation-dialog");
        const rect = dialog.getBoundingClientRect();
        const links = [...document.querySelectorAll(".mobile-nav-item")];
        return {
          open: dialog.open,
          contained: rect.left >= 0 && rect.right <= innerWidth,
          routes: links.length === 9,
          currentFocused: document.activeElement.matches('.mobile-nav-item[aria-current="page"]'),
          labelsFit: links.every(el => el.scrollWidth <= el.clientWidth),
          noDuplicateDesktopNav: document.querySelector(".sidebar").getClientRects().length === 0
        };
      })()`);
      if (Object.values(drawer).some(passed => !passed)) {
        throw new Error(`${viewport.name} mobile drawer failure: ${JSON.stringify(drawer)}`);
      }
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(path.join(outputDirectory, `openperpdesk-navigation-${viewport.name}.png`), Buffer.from(shot.data, "base64"));
      await command("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await command("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await delay(80);
      drawer.escapeRestoresFocus = await evaluate('!document.querySelector("#navigation-dialog").open && document.activeElement.id === "open-navigation"');
      await evaluate('document.querySelector("#open-navigation").click(); document.querySelector(".mobile-nav-item[data-route=\'strategies\']").click()');
      await delay(80);
      drawer.navigation = await evaluate('!document.querySelector("#navigation-dialog").open && document.body.dataset.view === "strategies" && document.activeElement.id === "page-title"');
      if (!drawer.escapeRestoresFocus || !drawer.navigation) {
        throw new Error(`${viewport.name} drawer navigation or dismissal failure: ${JSON.stringify(drawer)}`);
      }
      results.at(-1).drawer = drawer;
    } else {
      const menuHidden = await evaluate('document.querySelector("#open-navigation").getClientRects().length === 0');
      if (!menuHidden) throw new Error(`${viewport.name} mobile menu visible on desktop`);
      results.at(-1).mobileMenuHidden = true;
    }
    await evaluate('document.querySelector("#open-auth").click()');
    const modal = await evaluate(`(() => {
      const dialog = document.querySelector("#auth-dialog");
      const rect = dialog.getBoundingClientRect();
      return { open: dialog.open, focus: document.activeElement.id, left: rect.left, right: rect.right };
    })()`);
    if (!modal.open || modal.focus !== "admin-token" || modal.left < 0 || modal.right > viewport.width) {
      throw new Error(`${viewport.name} auth dialog failure: ${JSON.stringify(modal)}`);
    }
    await evaluate('document.querySelector("#admin-token").value = "ui-test-not-a-credential"');
    await command("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await command("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await delay(80);
    const modalClosed = await evaluate('!document.querySelector("#auth-dialog").open && document.querySelector("#admin-token").value === "" && document.activeElement.id === "open-auth"');
    if (!modalClosed) throw new Error(`${viewport.name} dialog failed to close, clear input, or restore focus`);
    results.at(-1).modalPassed = true;
    await evaluate('document.querySelector(\'[data-section="markets"]\').click(); document.querySelector(".ticket-lock [data-open-auth]").click()');
    await command("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await command("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    await delay(80);
    const contextualAuth = await evaluate('!document.querySelector("#auth-dialog").open && document.activeElement.matches(".ticket-lock [data-open-auth]")');
    if (!contextualAuth) throw new Error(`${viewport.name} contextual unlock lost focus`);
    results.at(-1).contextualAuth = contextualAuth;
  }

  await evaluate('document.querySelector("#toggle-market-refresh").click()');
  const paused = await evaluate('document.querySelector("#toggle-market-refresh").getAttribute("aria-pressed")');
  if (paused !== "true") throw new Error("Pause control did not change state");
  await evaluate('renderAnalysis({ source: "ui-regression-fixture", signal: { inst_id: "BTC-USDT-SWAP", action: "open_long" } })');
  await evaluate('document.querySelector(\'[data-symbol="ETH-USDT-SWAP"]\').click()');
  for (let attempt = 0; attempt < 50; attempt++) {
    await delay(100);
    if (await evaluate('document.querySelector("#chart-symbol").textContent === "ETH-USDT-SWAP"')) break;
  }
  const symbol = await evaluate('document.querySelector("#symbol").value');
  if (symbol !== "ETH-USDT-SWAP") throw new Error("Watchlist did not switch contract");
  const oldSignalCleared = await evaluate('state.analysis === null && document.querySelector("#execute-signal").disabled && document.querySelector("#preview-signal").disabled');
  if (!oldSignalCleared) throw new Error("Previous contract signal survived a contract switch");
  await evaluate('document.querySelector(\'[data-section="orders"]\').click(); document.querySelector(\'[data-section="fills"]\').click(); history.back()');
  await delay(120);
  const historyPassed = await evaluate('document.body.dataset.view === "orders"');
  if (!historyPassed) throw new Error("Browser back did not restore orders view");
  await command("Page.navigate", { url: `${origin}/#strategies` });
  await delay(1000);
  const deepLinkPassed = await evaluate('document.body.dataset.view === "strategies" && document.querySelector(".strategy-settings").open === true');
  if (!deepLinkPassed) throw new Error("Strategy deep link did not restore view");
  const researchChecks = [];
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "laptop", width: 1024, height: 900 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "landscape", width: 844, height: 390 },
    { name: "narrow-mobile", width: 320, height: 740 },
    { name: "small-mobile", width: 375, height: 812 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
    const checks = await checkResearchUI(evaluate);
    await evaluate('document.querySelector("#open-research-report").click()');
    await delay(80);
    checks.reportLinkFocus = await evaluate('document.activeElement.id === "research-title" && document.body.dataset.view === "strategies"');
    await evaluate('document.querySelector("#report-section").focus()');
    await command("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    await command("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    checks.reportKeyboardFocus = await evaluate('document.activeElement.id === "report-section-body"');
    researchChecks.push({ viewport: viewport.name, ...checks });
    const failed = Object.entries(checks).filter(([, value]) => value !== true);
    if (failed.length) throw new Error(`${viewport.name} research checks failed: ${JSON.stringify(failed)}`);
    await evaluate('document.querySelector("#research-title").blur(); window.scrollTo(0, 0)');
    const screenshot = await command("Page.captureScreenshot", {
      format: "png", captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: viewport.width, height: viewport.height * 2, scale: 1 },
    });
    await writeFile(path.join(outputDirectory, `openperpdesk-research-${viewport.name}.png`), Buffer.from(screenshot.data, "base64"));
    await evaluate('window.__restoreResearchFixture()');
  }
  const contrast = await evaluate(`(() => {
    const styles = getComputedStyle(document.documentElement);
    const color = token => styles.getPropertyValue(token).trim();
    const luminance = hex => {
      const channels = [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255)
        .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
      return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
    };
    const pairs = ["--text", "--muted", "--muted-strong", "--accent", "--warning", "--danger", "--info"]
      .flatMap(foreground => ["--bg", "--surface", "--surface-raised"].map(background => ({
        foreground, background, a: color(foreground), b: color(background)
      })));
    pairs.push({ foreground: "primary-button", background: "--action", a: color("--on-action"), b: color("--action") });
    return pairs.map(({ foreground, background, a, b }) => {
      const first = luminance(a), second = luminance(b);
      return { foreground, background, ratio: Number(((Math.max(first, second) + .05) / (Math.min(first, second) + .05)).toFixed(2)) };
    });
  })()`);
  if (contrast.some(pair => !Number.isFinite(pair.ratio) || pair.ratio < 4.5)) {
    throw new Error(`Text contrast failure: ${JSON.stringify(contrast)}`);
  }
  await command("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "reduce" }],
  });
  const reducedMotion = await evaluate(`(() => {
    setBusy("#sync-ledger", true);
    const disabled = getComputedStyle(document.querySelector("#sync-ledger [data-icon]")).animationName === "none";
    setBusy("#sync-ledger", false);
    return matchMedia("(prefers-reduced-motion: reduce)").matches && disabled;
  })()`);
  if (!reducedMotion) throw new Error("Reduced motion preference was not respected");
  const appearance = await checkAppearance({
    evaluate, command, origin,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  const management = await checkManagement({
    evaluate, command,
    screenshot: async name => {
      const size = await evaluate("({width: innerWidth, height: Math.min(document.documentElement.scrollHeight, innerHeight * 3)})");
      const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
        clip: { x: 0, y: 0, ...size, scale: 1 } });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  const billHistory = await checkBillHistory({
    evaluate, command,
    screenshot: async (name, selector = null) => {
      const size = selector ? await evaluate(`(() => {
        const rect = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
        return {x: 0, y: Math.max(0, scrollY + rect.top - 12), width: innerWidth,
          height: Math.min(document.documentElement.scrollHeight - (scrollY + rect.top - 12), Math.max(innerHeight, rect.height + 24))};
      })()`) : await evaluate("({x: 0, y: 0, width: innerWidth, height: Math.min(document.documentElement.scrollHeight, innerHeight * 3)})");
      const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
        clip: { ...size, scale: 1 } });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  const realtime = await checkRealtime({
    evaluate, command, origin,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  const annotations = await checkChartAnnotations({
    evaluate, command, origin,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  if (browserErrors.length) throw new Error(`Browser exceptions: ${JSON.stringify(browserErrors)}`);
  const positionLots = await checkPositionLots({
    evaluate, command,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  if (browserErrors.length) throw new Error(`Browser exceptions: ${JSON.stringify(browserErrors)}`);
  const protectionIncidents = await checkProtectionIncidents({
    evaluate, command,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  if (browserErrors.length) throw new Error(`Browser exceptions: ${JSON.stringify(browserErrors)}`);
  const protectionReview = await checkProtectionReview({
    evaluate, command,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  if (browserErrors.length) throw new Error(`Browser exceptions: ${JSON.stringify(browserErrors)}`);
  const tradingView = await checkTradingView({
    evaluate, command,
    screenshot: async name => {
      const shot = await command("Page.captureScreenshot", { format: "png" });
      await writeFile(path.join(outputDirectory, name), Buffer.from(shot.data, "base64"));
    },
  });
  if (browserErrors.length) throw new Error(`Browser exceptions: ${JSON.stringify(browserErrors)}`);
  const report = { results, paused, symbol, oldSignalCleared, historyPassed, deepLinkPassed, researchChecks, contrast, reducedMotion, appearance, management, billHistory, realtime, annotations, positionLots, protectionIncidents, protectionReview, tradingView, browserErrors };
  await writeFile(path.join(outputDirectory, "ui-verification.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  socket?.close();
  chrome.kill("SIGTERM");
  await Promise.race([
    new Promise((resolve) => chrome.once("exit", resolve)),
    delay(3000),
  ]);
  if (chrome.exitCode === null && chrome.signalCode === null) chrome.kill("SIGKILL");
  await rm(profile, { recursive: true, force: true, maxRetries: 8, retryDelay: 150 });
}
