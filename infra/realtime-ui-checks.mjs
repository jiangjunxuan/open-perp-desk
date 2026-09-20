export async function checkRealtime({ evaluate, command, origin, screenshot }) {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const results = [];
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 },
    { name: "narrow-mobile", width: 320, height: 740 },
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
    const trades = await evaluate(`(async () => {
      const tab = $("#orderbook-tab-trades");
      const bookHeight = $("#orderbook").getBoundingClientRect().height;
      tab.click();
      for (let attempt = 0; attempt < 120; attempt++) {
        if (state.marketStream?.trades?.[state.symbol]?.fresh
            && document.querySelectorAll("#trade-tape .trade-row").length) break;
        await new Promise(resolve => setTimeout(resolve, 125));
      }
      const rows = [...document.querySelectorAll("#trade-tape .trade-row")];
      const live = state.marketStream?.trades?.[state.symbol];
      const result = {
        selected: tab.getAttribute("aria-selected") === "true",
        panelVisible: !$("#orderbook-view-trades").hidden && $("#orderbook-view-book").hidden,
        fresh: live?.fresh === true,
        rows: rows.length,
        hasBuyOrSellTone: rows.some(row => row.classList.contains("buy") || row.classList.contains("sell")),
        state: $("#orderbook-state").textContent,
        stableHeight: $("#orderbook").getBoundingClientRect().height === bookHeight,
        compactTimes: rows.every(row => /^\\d{2}:\\d{2}:\\d{2}$/.test(row.querySelector("time")?.textContent)
          && row.querySelector("time").title.endsWith("UTC+8")),
        columnsFit: rows.every(row => [...row.children].every(cell => cell.scrollWidth <= cell.clientWidth + 1)),
        units: $(".trade-tape-header").textContent.includes("数量(张)")
          && $(".orderbook-header").textContent.includes("累计(张)"),
      };
      return result;
    })()`);
    if (!trades.selected || !trades.panelVisible || !trades.fresh || trades.rows < 1 || !trades.hasBuyOrSellTone
        || trades.state !== "已连接" || !trades.stableHeight || !trades.compactTimes || !trades.columnsFit || !trades.units) {
      throw new Error(`${viewport.name}: live trades view failed ${JSON.stringify(trades)}`);
    }
    await evaluate('$("#orderbook").scrollIntoView({ block: "start", behavior: "instant" })');
    await screenshot(`openperpdesk-trades-${viewport.name}.png`);
    await evaluate('$("#orderbook-tab-book").click(); window.scrollTo({ top: 0, behavior: "instant" })');
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
        && $("#market-tag").textContent === "行情已暂停"
        && $("#orderbook-state").textContent === "行情已暂停";
    })()`);
    if (!paused) throw new Error(`${viewport.name}: pause did not freeze the live view`);
    await evaluate('$("#toggle-market-refresh").click()');
    for (let attempt = 0; attempt < 100; attempt++) {
      if (await evaluate('$("#market-tag").textContent === "已连接"')) break;
      await wait(100);
    }
    const resumed = await evaluate('!state.marketPaused && $("#market-tag").textContent === "已连接"');
    if (!resumed) throw new Error(`${viewport.name}: live view did not resume`);
    const orderbook = await evaluate(`(() => {
      marketFeed.close();
      controlFeed.close();
      const snapshot = state.marketStream;
      const time = Date.parse("2026-09-13T00:00:01Z");
      const valid = { px: "100", sz: "2.5", side: "sell", ts: String(time) };
      const invalid = [
        null, {...valid, side: "unknown"}, {...valid, side: undefined},
        {...valid, sz: "0"}, {...valid, sz: "-1"}, {...valid, sz: "Infinity"},
        {...valid, px: "NaN"}, {...valid, px: "0"},
        {...valid, ts: "0"}, {...valid, ts: "-1"}, {...valid, ts: "invalid"},
        {...valid, ts: "Infinity"}, {...valid, ts: "8640000000000001"},
      ];
      const tradeRecord = { fresh: true, data: [valid, ...invalid, {...valid, side: "buy", ts: String(time + 1000)}] };
      const bookRecord = { fresh: true, data: { asks: [["101", "2"]], bids: [["99", "3"]] } };
      state.marketStream = {
        ...snapshot, trades: {[state.symbol]: tradeRecord}, order_books: {[state.symbol]: bookRecord},
      };
      state.marketFeedState = "open";
      $("#orderbook-tab-trades").click();
      const rows = [...document.querySelectorAll("#trade-tape .trade-row")];
      const invalidRowsRejected = rows.length === 2 && rows[0].classList.contains("buy")
        && rows[1].classList.contains("sell") && rows.every(row => row.getAttribute("aria-label").endsWith("张"));
      const timestampsCorrect = rows[0]?.querySelector("time").textContent === "08:00:02"
        && rows[0]?.querySelector("time").dateTime === "2026-09-13T00:00:02.000Z"
        && rows[0]?.querySelector("time").title.includes("2026");
      const originalHeight = $("#orderbook").getBoundingClientRect().height;
      const originalRows = $("#trade-tape").innerHTML;
      $("#toggle-market-refresh").click();
      const pauseRetainsTrades = state.marketPaused && state.marketStream.trades[state.symbol] === tradeRecord
        && $("#trade-tape").innerHTML === originalRows && $("#orderbook-state").textContent === "行情已暂停";
      state.marketPaused = false;
      const statusChecks = ["book", "trades"].every(view => {
        state.orderbookView = view;
        state.marketFeedState = "open";
        renderOrderBook();
        const ready = $("#orderbook-state").textContent === "已连接";
        state.marketFeedState = "offline";
        updateMarketRefreshControl();
        const disconnected = $("#orderbook-state").textContent === "行情连接中断"
          && $("#orderbook-state").dataset.tone === "warning";
        state.marketFeedState = "connecting";
        updateMarketRefreshControl();
        const connecting = $("#orderbook-state").textContent === "连接中";
        state.marketFeedState = "open";
        const record = view === "book" ? bookRecord : tradeRecord;
        record.fresh = false;
        updateMarketRefreshControl();
        const stale = $("#orderbook-state").textContent === (view === "book" ? "深度延迟" : "逐笔延迟");
        record.fresh = true;
        return ready && disconnected && connecting && stale;
      });
      tradeRecord.data = invalid;
      renderOrderBook();
      const emptyNotConnected = !$("#trade-tape .trade-row") && $("#orderbook-state").textContent === "等待成交";
      const emptyHeightStable = $("#orderbook").getBoundingClientRect().height === originalHeight;
      tradeRecord.data = Array.from({length: 80}, (_, index) => ({...valid, ts: String(time + index)}));
      renderOrderBook();
      const tradePanel = $("#orderbook-view-trades");
      const tapeBounded = document.querySelectorAll("#trade-tape .trade-row").length === 40
        && tradePanel.scrollHeight > tradePanel.clientHeight && tradePanel.clientHeight === 420
        && $("#orderbook").getBoundingClientRect().height === originalHeight;
      tradePanel.scrollTop = 100;
      const scrollTop = tradePanel.scrollTop;
      renderOrderBook();
      const scrollPreserved = tradePanel.scrollTop === scrollTop && scrollTop > 0;
      const bookTab = $("#orderbook-tab-book");
      const tradesTab = $("#orderbook-tab-trades");
      bookTab.click();
      bookTab.focus();
      bookTab.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
      const endKey = document.activeElement === tradesTab && tradesTab.tabIndex === 0 && bookTab.tabIndex === -1;
      tradesTab.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
      const homeKey = document.activeElement === bookTab && bookTab.tabIndex === 0 && tradesTab.tabIndex === -1;
      bookTab.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
      const wrapKey = document.activeElement === tradesTab && state.orderbookView === "trades";
      tradesTab.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
      const keyboardTabs = endKey && homeKey && wrapKey && document.activeElement === bookTab;
      const tabHeightStable = $("#orderbook").getBoundingClientRect().height === originalHeight;
      state.marketStream = snapshot;
      renderOrderBook();
      updateMarketRefreshControl();
      return { invalidRowsRejected, timestampsCorrect, pauseRetainsTrades, statusChecks, emptyNotConnected,
        emptyHeightStable, tapeBounded, scrollPreserved, keyboardTabs, tabHeightStable };
    })()`);
    if (Object.values(orderbook).some(value => value !== true)) {
      throw new Error(`${viewport.name}: orderbook state fixtures failed ${JSON.stringify(orderbook)}`);
    }
    const fixtures = await evaluate(`(async () => {
      marketFeed?.close();
      controlFeed?.close();
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
      latest.onState("offline");
      const disconnectedQuoteCleared = $("#market-price").textContent === "--"
        && $("#market-funding").textContent === "--"
        && $("#market-oi").textContent === "--";
      latest.onState("open");
      latest.onEvent("market", { bar: state.bar, tickers: { [state.symbol]: record }, candles: {} });
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
      state.marketOverview = {
        funding_rate: { fundingRate: "0.1" },
        open_interest: { oi: "123" },
      };
      renderMarketOverview();
      const auxiliaryDataCleared = $("#market-funding").textContent === "--"
        && $("#market-oi").textContent === "--";
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
        private_stream: {configured: true, ready: true},
      };
      state.privateFeedState = "open";
      state.privateAccountReady = true;
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
      connectPrivateFeed();
      const privateCallbacks = latest;
      privateCallbacks.onState("open");
      applyPrivateEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [{totalEq: "4321"}] });
      const accountUpdated = $("#metric-equity").textContent === "4,321" && $("#positions-tag").textContent === "实时同步";
      privateCallbacks.onState("offline");
      const privateDataCleared = $("#metric-equity").textContent === "--"
        && $("#metric-pnl").textContent === "--"
        && $("#positions-tag").textContent === "账户推送断开"
        && $("#pnl-summary").textContent === "暂无实时账户数据";
      privateCallbacks.onEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [{totalEq: "999999"}] });
      const offlineAccountIgnored = $("#metric-equity").textContent === "--";
      privateCallbacks.onState("open");
      privateCallbacks.onEvent("heartbeat", {});
      const privateHeartbeatCannotUnlock = !executionGateOpen(allowed) && !state.privateAccountReady;
      privateCallbacks.onEvent("account", { configured: true, connected: true, authenticated: true, ready: false, balance: [{totalEq: "999999"}] });
      const privateLoginCannotUnlock = !executionGateOpen(allowed) && !state.privateAccountReady
        && $("#metric-equity").textContent === "--" && $("#positions-tag").textContent === "等待账户回报";
      api = async () => ({configured: true, balance: [{totalEq: "999999"}], data: []});
      await loadPrivate();
      const restCannotRestoreDisconnectedAccount = $("#metric-equity").textContent === "--"
        && !state.privateAccountReady && state.records.positions === null;
      api = oldApi;
      privateCallbacks.onEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [{totalEq: "4321"}] });
      applyStatus({...allowed, account_stream: {configured: true, connected: true, authenticated: true, account_ready: false}});
      const accountLoginShowsWaiting = $("#state-account-stream").textContent === "等待数据";
      applyStatus({...allowed, account_stream: {configured: true, connected: true, authenticated: true, account_ready: true}});
      const accountRecoveryShowsReady = $("#top-execution").textContent === "模拟盘执行已启用"
        && $("#state-account-stream").textContent === "在线";
      applyStatus({...allowed, private_stream: {configured: true, ready: false}});
      const privateStreamDisconnectLocksNewOrders = !executionGateOpen(state.status)
        && $("#execute-signal").disabled;
      const algoOutageDoesNotAddCloseLock = executionGateOpen(state.status, "close");
      const closeOnlyLabelsMatch = ["#top-execution", "#mobile-execution", "#ticket-lock-label", "#metric-risk"]
        .every(selector => $(selector).textContent === "仅可平仓");
      const unknownPrivateStreamLocksNewOrders = [undefined, null, {}, {ready: "true"}].every(
        private_stream => !executionGateOpen({...allowed, private_stream}),
      );
      const closeStillRequiresSafety = !executionGateOpen({
        ...allowed, safety_control: {emergency_stopped: true, execution_allowed: false},
      }, "close");
      const originalAnalysis = state.analysis;
      const outageStatus = state.status;
      renderAnalysis({signal: {inst_id: state.symbol, action: "close"}});
      const closeButtonAvailableOnRender = !$("#execute-signal").disabled;
      applyStatus(outageStatus);
      const closeButtonAvailableOnStatus = !$("#execute-signal").disabled;
      updatePrivateActionAvailability();
      const closeButtonAvailableOnRefresh = !$("#execute-signal").disabled;
      renderAnalysis({signal: {inst_id: state.symbol, action: "open_long"}});
      const openingButtonStaysBlocked = $("#execute-signal").disabled;
      renderAnalysis(originalAnalysis);
      applyStatus(allowed);
      let resolvePerformance;
      api = () => new Promise(resolve => { resolvePerformance = resolve; });
      const latePerformance = refreshLivePerformance();
      privateCallbacks.onState("offline");
      const privateDisconnectUpdatesGateLabels = $("#top-execution").textContent === "执行已锁定"
        && $("#mobile-execution").textContent === "执行已锁定"
        && $("#ticket-lock-label").textContent === "执行已锁定" && $("#execute-signal").disabled;
      privateCallbacks.onState("open");
      privateCallbacks.onEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [{totalEq: "5432"}] });
      resolvePerformance({data: {ending_equity: 999999, net_pnl: 777, fills: 3}});
      await latePerformance;
      api = oldApi;
      const stalePerformanceRejected = $("#performance-net-pnl").textContent === "--"
        && $("#metric-equity").textContent === "5,432";
      privateCallbacks.onEvent("account", { configured: true, connected: false, authenticated: false, balance: [] });
      privateCallbacks.onEvent("positions", {data: []});
      privateCallbacks.onEvent("orders", {data: []});
      const disconnectedLedgerIgnored = state.records.positions === null && state.records.orders === null
        && $("#metric-pnl").textContent === "--";
      privateCallbacks.onEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [] });
      const emptyBalanceCleared = $("#metric-equity").textContent === "--";
      privateCallbacks.onEvent("positions", {data: []});
      const confirmedZeroPositionPnl = $("#metric-pnl").textContent === "0";
      applyPrivateEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [{adjEq: "999"}] });
      const adjustedEquityNotTotal = $("#metric-equity").textContent === "--"
        && $("#metric-equity-note").textContent === "账户总权益缺失";
      applyPrivateEvent("account", { configured: true, connected: true, authenticated: true, ready: true, balance: [{totalEq: "0", adjEq: "999"}] });
      const zeroEquityPreserved = $("#metric-equity").textContent === "0";
      $("#fast-period").value = "17";
      state.strategyDirty = true;
      applyPrivateEvent("strategies", {data: [{strategy_id:"structured-technical", enabled:true, config:{fast_period:9}}]});
      const draftPreserved = $("#fast-period").value === "17";
      applyPrivateEvent("locked", {});
      const accessRevoked = state.token === "" && $("#metric-equity").textContent === "--" && $("#execute-signal").disabled;
      openLiveStream = oldOpen;
      return { disconnected, wrongPeriodIgnored, realPriceUpdated, disconnectedQuoteCleared,
        pauseRejectsPendingSnapshot, noSnapshotFallback, auxiliaryDataCleared, privateDataCleared, marketDisconnectLocksExecution,
        offlineAccountIgnored, privateHeartbeatCannotUnlock, privateLoginCannotUnlock, restCannotRestoreDisconnectedAccount,
        accountLoginShowsWaiting, accountRecoveryShowsReady, privateStreamDisconnectLocksNewOrders,
        algoOutageDoesNotAddCloseLock, closeOnlyLabelsMatch, unknownPrivateStreamLocksNewOrders, closeStillRequiresSafety,
        closeButtonAvailableOnRender, closeButtonAvailableOnStatus, closeButtonAvailableOnRefresh,
        openingButtonStaysBlocked,
        privateDisconnectUpdatesGateLabels,
        stalePerformanceRejected, disconnectedLedgerIgnored, emptyBalanceCleared, confirmedZeroPositionPnl,
        disconnectedExecutionLocked, reconnectingExecutionLocked, heartbeatCannotUnlock,
        freshControlStatusAccepted, staleStatusRejected, accountUpdated, adjustedEquityNotTotal, zeroEquityPreserved,
        draftPreserved, accessRevoked };
    })()`);
    if (Object.values(fixtures).some(value => value !== true)) {
      throw new Error(`${viewport.name}: realtime state fixtures failed ${JSON.stringify(fixtures)}`);
    }
    const overflow = await evaluate("document.documentElement.scrollWidth > innerWidth");
    if (overflow) throw new Error(`${viewport.name}: realtime label caused horizontal overflow`);
    results.push({ viewport: viewport.name, trades, orderbook, ...samples, paused, resumed, ...fixtures });
  }
  return results;
}
