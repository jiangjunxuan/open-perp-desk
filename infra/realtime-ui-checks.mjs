export async function checkRealtime({ evaluate, command, origin, screenshot }) {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const results = [];
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
    await command("Page.navigate", { url: `${origin}/?realtime-check=${viewport.name}#markets` });
    let ready;
    for (let attempt = 0; attempt < 150; attempt++) {
      ready = await evaluate(`location.search === "?realtime-check=${viewport.name}" && typeof state !== "undefined" && state.marketFeedState === "open" && state.controlFeedState === "open" && state.marketCandleKey !== null && !$("#refresh-market").hasAttribute("aria-busy")`);
      if (ready) break;
      await wait(100);
    }
    if (!ready) {
      const diagnostics = await evaluate('({url: location.href, hidden: document.hidden, market: state.marketFeedState, control: state.controlFeedState, tag: $("#market-tag").textContent, candle: state.marketCandleKey, busy: $("#refresh-market").hasAttribute("aria-busy")})');
      throw new Error(`${viewport.name}: live feed did not become ready ${JSON.stringify(diagnostics)}`);
    }
    const samples = await evaluate(`(async () => {
      const button = $("#watchlist .watch-item");
      button.focus();
      const rows = [];
      for (let i = 0; i < 24; i++) {
        rows.push({
          receivedAt: state.marketStream.tickers[state.symbol].received_at,
          price: $("#market-price").textContent,
          candle: state.marketCandleKey,
          tag: $("#market-tag").textContent,
        });
        await new Promise(resolve => setTimeout(resolve, 125));
      }
      return {
        uniqueUpdates: new Set(rows.map(row => row.receivedAt)).size,
        uniquePrices: new Set(rows.map(row => row.price)).size,
        uniqueCandles: new Set(rows.map(row => row.candle)).size,
        liveLabel: rows.every(row => row.tag === "已连接"),
        stableWatchNode: button === $("#watchlist .watch-item"),
        focusPreserved: document.activeElement === button,
        samples: rows,
      };
    })()`);
    if (samples.uniqueUpdates < 2 || !samples.liveLabel || !samples.stableWatchNode || !samples.focusPreserved) {
      throw new Error(`${viewport.name}: realtime evidence failed ${JSON.stringify(samples)}`);
    }
    await screenshot(`openperpdesk-realtime-${viewport.name}.png`);
    const paused = await evaluate(`(async () => {
      $("#toggle-market-refresh").click();
      const price = $("#market-price").textContent;
      await new Promise(resolve => setTimeout(resolve, 600));
      return state.marketPaused && price === $("#market-price").textContent
        && $("#market-tag").textContent === "行情已暂停";
    })()`);
    if (!paused) throw new Error(`${viewport.name}: pause did not freeze the live view`);
    await evaluate('$("#toggle-market-refresh").click()');
    for (let attempt = 0; attempt < 100; attempt++) {
      if (await evaluate('$("#market-tag").textContent === "已连接"')) break;
      await wait(100);
    }
    const resumed = await evaluate('!state.marketPaused && $("#market-tag").textContent === "已连接"');
    if (!resumed) throw new Error(`${viewport.name}: live view did not resume`);
    const fixtures = await evaluate(`(async () => {
      marketFeed.close();
      controlFeed.close();
      const oldOpen = openLiveStream;
      let latest;
      openLiveStream = (path, callbacks) => { latest = { path, ...callbacks }; return { close() {} }; };
      connectMarketFeed();
      latest.onState("offline");
      const disconnected = $("#market-tag").textContent === "行情连接中断";
      const record = { fresh: true, received_at: new Date().toISOString(), data: {last: "100", bidPx: "99", askPx: "101", sodUtc8: "90"} };
      latest.onState("open");
      latest.onEvent("market", { bar: "1H", tickers: { [state.symbol]: record }, candles: {} });
      const wrongPeriodIgnored = state.marketStream === null;
      latest.onEvent("market", { bar: state.bar, tickers: { [state.symbol]: record }, candles: {} });
      const realPriceUpdated = $("#market-price").textContent === "100";
      const oldApi = api;
      const replies = [];
      api = () => new Promise(resolve => replies.push(resolve));
      const pendingSnapshot = loadMarket();
      $("#toggle-market-refresh").click();
      replies[0]({data: [["1","999","999","999","999","1"]]});
      await pendingSnapshot;
      api = oldApi;
      const pauseRejectsPendingSnapshot = state.marketPaused && $("#market-price").textContent === "100"
        && !$("#refresh-market").hasAttribute("aria-busy");
      const requests = [];
      api = async path => {
        requests.push(path);
        return {data: [["2","999","999","999","999","1"], ["1","999","999","999","999","1"]]};
      };
      state.marketPaused = false;
      state.marketFeedState = "offline";
      await loadMarket();
      const noSnapshotFallback = $("#market-price").textContent === "100"
        && requests.length === 1 && requests[0].includes("/market/candles")
        && $("#market-tag").textContent === "行情连接中断";
      api = oldApi;
      state.marketFeedState = "open";
      state.marketStream = {tickers: {[state.symbol]: record}};
      const allowed = {
        ...state.status, execution_enabled: true, risk_engine_ready: true, trading_mode: "demo",
        safety_control: {execution_allowed: true, emergency_stopped: false},
      };
      state.controlFeedState = "offline";
      const disconnectedExecutionLocked = !executionGateOpen(allowed);
      connectControlFeed();
      const control = latest;
      control.onState("connecting");
      applyStatus(allowed);
      const reconnectingExecutionLocked = !executionGateOpen(allowed);
      control.onState("open");
      control.onEvent("heartbeat", {});
      const heartbeatCannotUnlock = !executionGateOpen(allowed);
      control.onEvent("status", allowed);
      const freshControlStatusAccepted = executionGateOpen(state.status);
      state.marketFeedState = "offline";
      updateMarketRefreshControl();
      const marketDisconnectLocksExecution = !executionGateOpen(state.status) && $("#execute-signal").disabled;
      state.marketFeedState = "open";
      let resolveStatus;
      api = () => new Promise(resolve => { resolveStatus = resolve; });
      const pendingStatus = loadStatus();
      control.onEvent("status", {...allowed, safety_control: {execution_allowed:false, emergency_stopped:true}});
      resolveStatus(allowed);
      await pendingStatus;
      api = oldApi;
      const staleStatusRejected = state.status.safety_control.emergency_stopped
        && !executionGateOpen(state.status) && $("#execute-signal").disabled;
      state.token = "ui-fixture-not-a-credential";
      state.privateFeedState = "open";
      applyPrivateEvent("account", { configured: true, connected: true, authenticated: true, balance: [{totalEq: "4321"}] });
      const accountUpdated = $("#metric-equity").textContent === "4,321" && $("#positions-tag").textContent === "实时同步";
      $("#fast-period").value = "17";
      state.strategyDirty = true;
      applyPrivateEvent("strategies", {data: [{strategy_id:"structured-technical", enabled:true, config:{fast_period:9}}]});
      const draftPreserved = $("#fast-period").value === "17";
      applyPrivateEvent("locked", {});
      const accessRevoked = state.token === "" && $("#metric-equity").textContent === "--" && $("#execute-signal").disabled;
      openLiveStream = oldOpen;
      return { disconnected, wrongPeriodIgnored, realPriceUpdated, pauseRejectsPendingSnapshot, noSnapshotFallback, marketDisconnectLocksExecution,
        disconnectedExecutionLocked, reconnectingExecutionLocked, heartbeatCannotUnlock,
        freshControlStatusAccepted, staleStatusRejected, accountUpdated, draftPreserved, accessRevoked };
    })()`);
    if (Object.values(fixtures).some(value => value !== true)) {
      throw new Error(`${viewport.name}: realtime state fixtures failed ${JSON.stringify(fixtures)}`);
    }
    const overflow = await evaluate("document.documentElement.scrollWidth > innerWidth");
    if (overflow) throw new Error(`${viewport.name}: realtime label caused horizontal overflow`);
    results.push({ viewport: viewport.name, ...samples, paused, resumed, ...fixtures });
  }
  return results;
}
