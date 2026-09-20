export async function checkChartAnnotations({ evaluate, command, origin, screenshot }) {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const results = [];
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 },
  ]) {
    await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
    for (const theme of ["dark", "light"]) {
      await command("Page.navigate", { url: `${origin}/?annotations=${viewport.name}-${theme}#markets` });
      let ready = false;
      for (let attempt = 0; attempt < 180; attempt++) {
        ready = await evaluate('typeof state !== "undefined" && state.chartGeometry && state.marketFeedState === "open" && !$("#refresh-market").hasAttribute("aria-busy")');
        if (ready) break;
        await wait(100);
      }
      if (!ready) throw new Error("Annotation test could not load live chart");
      await evaluate(`setTheme("${theme}"); state.chartAnnotations = []; saveChartAnnotations(); $("#price-chart").scrollIntoView({block:"center"});`);
      const clickChart = async (xFraction, yFraction) => {
        const point = await evaluate(`(() => {
          const rect = $("#price-chart").getBoundingClientRect(), g = state.chartGeometry;
          return {x: rect.left + g.padding.left + g.chartWidth * ${xFraction}, y: rect.top + g.padding.top + g.chartHeight * ${yFraction}};
        })()`);
        await command("Input.dispatchMouseEvent", { type: "mouseMoved", ...point });
        await command("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, ...point });
        await command("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, ...point });
      };
      await evaluate('$("[data-chart-tool=horizontal]").click()');
      await clickChart(.3, .4);
      await evaluate('$("[data-chart-tool=trend]").click()');
      await clickChart(.25, .7);
      await clickChart(.7, .3);
      await evaluate('$("[data-chart-tool=text]").click()');
      await clickChart(.4, .25);
      const dialogOpen = await evaluate('$("#chart-note-dialog").open');
      if (!dialogOpen) throw new Error("Text marker did not open its editor");
      await evaluate('$("#chart-note-text").value = "支撑区域"; $("#chart-note-form").requestSubmit()');
      await wait(1200);
      const dragPoint = await evaluate(`(() => {
        const rect = $("#price-chart").getBoundingClientRect(), g = state.chartGeometry;
        return {x: rect.left + g.padding.left + g.chartWidth * .12, y: rect.top + g.yFor(state.chartAnnotations[0].price), before: state.chartAnnotations[0].price};
      })()`);
      await command("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, x: dragPoint.x, y: dragPoint.y });
      await command("Input.dispatchMouseEvent", { type: "mouseMoved", button: "left", buttons: 1, x: dragPoint.x, y: dragPoint.y + 16 });
      await command("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, x: dragPoint.x, y: dragPoint.y + 16 });
      const dragged = await evaluate(`state.chartAnnotations[0].price !== ${dragPoint.before} && state.chartDrag === null`);
      if (!dragged) throw new Error("Dragging a horizontal marker did not move its price");
      const checks = await evaluate(`(() => {
        const checks = {};
        checks.created = state.chartAnnotations.length === 3 && !$("#chart-note-dialog").open;
        checks.livePreserved = state.marketFeedState === "open";
        const saved = JSON.stringify(state.chartAnnotations);
        state.chartRange = 40; renderChart(state.lastCandles);
        state.chartRange = 80; renderChart(state.lastCandles);
        checks.rangeKeepsAnchors = JSON.stringify(state.chartAnnotations) === saved;
        checks.outsideTimeNotClamped = chartTimeToX(state.chartGeometry.candles[0].time - 36000000, state.chartGeometry) < 0;
        $("#chart-annotation-list").value = "0";
        $("#chart-annotation-list").dispatchEvent(new Event("change"));
        checks.selected = state.chartSelection === 0 && !$("#edit-chart-annotation").disabled;
        const oldPrice = state.chartAnnotations[0].price;
        $("#edit-chart-annotation").click();
        $("#chart-note-price").value = String(oldPrice + 1);
        $("#chart-note-form").requestSubmit();
        checks.edited = state.chartAnnotations[0].price === oldPrice + 1;
        $("#delete-chart-annotation").click();
        checks.deleted = state.chartAnnotations.length === 2;
        $("#undo-chart-annotation").click();
        checks.undoDelete = state.chartAnnotations.length === 3;
        $("#clear-chart-annotations").click();
        checks.cleared = state.chartAnnotations.length === 0;
        $("#undo-chart-annotation").click();
        checks.undoClear = state.chartAnnotations.length === 3;
        const originalBar = state.bar;
        state.bar = "4H"; loadChartAnnotations();
        checks.periodIsolation = state.chartAnnotations.length === 0;
        state.bar = originalBar; loadChartAnnotations();
        checks.periodRestored = state.chartAnnotations.length === 3;
        const originalSymbol = state.symbol;
        state.symbol = "ETH-USDT-SWAP"; loadChartAnnotations();
        checks.symbolIsolation = state.chartAnnotations.length === 0;
        state.symbol = originalSymbol; loadChartAnnotations();
        const key = chartStorageKey(), original = localStorage.getItem(key);
        localStorage.setItem(key, "broken"); loadChartAnnotations();
        checks.corruptStorageSafe = state.chartAnnotations.length === 0;
        localStorage.setItem(key, JSON.stringify([null, {type:"trend"}, {type:"text", time:1, price:1, text:4}]));
        loadChartAnnotations();
        checks.malformedStorageSafe = state.chartAnnotations.length === 0;
        localStorage.setItem(key, original); loadChartAnnotations();
        renderChart(state.lastCandles);
        const bounds = $(".annotation-toolbar").getBoundingClientRect();
        checks.controlsFit = [...$(".annotation-toolbar").querySelectorAll("button,select")].every(element => {
          const r = element.getBoundingClientRect();
          return r.left >= bounds.left - 1 && r.right <= bounds.right + 1;
        });
        checks.noOverflow = document.documentElement.scrollWidth <= innerWidth;
        checks.iconsConfigured = [...$(".annotation-toolbar").querySelectorAll("[data-icon]")]
          .every(icon => icon.style.getPropertyValue("--icon").includes(icon.dataset.icon + ".svg"));
        const beforeKeyboard = state.chartAnnotations.length;
        $("[data-chart-tool=horizontal]").click();
        $("#price-chart").dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", bubbles:true}));
        checks.keyboardCreates = state.chartAnnotations.length === beforeKeyboard + 1;
        $("#price-chart").dispatchEvent(new KeyboardEvent("keydown", {key:"Delete", bubbles:true}));
        checks.keyboardDeletes = state.chartAnnotations.length === beforeKeyboard;
        return checks;
      })()`);
      if (Object.values(checks).some(value => value !== true)) throw new Error(`Annotation checks failed: ${JSON.stringify(checks)}`);
      await evaluate('$(".annotation-toolbar").scrollIntoView({block:"start"})');
      await screenshot(`openperpdesk-annotations-${viewport.name}-${theme}.png`);
      await command("Page.reload");
      let persisted = false;
      for (let attempt = 0; attempt < 150; attempt++) {
        persisted = await evaluate('typeof state !== "undefined" && state.chartAnnotations?.length === 3 && state.chartGeometry && state.marketFeedState === "open"');
        if (persisted) break;
        await wait(100);
      }
      if (!persisted) throw new Error("Chart annotations did not survive reload");
      results.push({ viewport: viewport.name, theme, ...checks, dragged, persisted });
      await evaluate('localStorage.removeItem(chartStorageKey()); state.chartAnnotations = [];');
    }
  }
  return results;
}
