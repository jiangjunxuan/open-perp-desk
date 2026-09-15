export async function checkAppearance({ evaluate, command, origin, screenshot }) {
  async function waitFor(expression) {
    for (let attempt = 0; attempt < 400; attempt++) {
      try {
        if (await evaluate(expression)) return;
      } catch {
        // A navigation can replace the JS context between two CDP commands.
      }
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error("Appearance checks: page did not become ready");
  }
  const previousDocument = await evaluate("performance.timeOrigin");
  await command("Page.navigate", { url: `${origin}/?appearance-check=1#markets` });
  await waitFor(`performance.timeOrigin !== ${previousDocument}
    && typeof state !== "undefined" && state.lastCandles?.length > 0 && state.status
    && !document.querySelector("#refresh-market").hasAttribute("aria-busy")`);
  const report = { themes: [], behavior: {} };
  const viewports = [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "wide", width: 1920, height: 1080 },
    { name: "laptop", width: 1024, height: 900 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "landscape", width: 844, height: 390 },
    { name: "mobile", width: 390, height: 844 },
    { name: "narrow-mobile", width: 320, height: 740 },
  ];
  for (const theme of ["light", "dark"]) {
    await evaluate(`setTheme("${theme}")`);
    const contrast = await evaluate(`(() => {
      const css = getComputedStyle(document.documentElement);
      const color = token => css.getPropertyValue(token).trim();
      const luminance = hex => {
        const rgb = hex.slice(1).match(/.{2}/g).map(channel => {
          const value = parseInt(channel, 16) / 255;
          return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
        });
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      const foregrounds = ["--text", "--muted", "--muted-strong", "--accent", "--warning", "--danger", "--info"];
      const backgrounds = ["--bg", "--surface", "--surface-raised", "--surface-soft"];
      const pairs = foregrounds.flatMap(foreground => backgrounds.map(background => [foreground, background]));
      pairs.push(["--on-action", "--action"], ["--on-action", "--action-hover"],
        ["--danger", "--danger-soft"], ["--danger", "--danger-hover"],
        ["--on-chart-label", "--accent"], ["--on-chart-label", "--danger"],
        ["--text", "--control-hover"], ["--text", "--control-active"]);
      return pairs.map(([foreground, background]) => {
        const a = luminance(color(foreground));
        const b = luminance(color(background));
        return { foreground, background, ratio: Math.round((Math.max(a, b) + .05) / (Math.min(a, b) + .05) * 100) / 100 };
      });
    })()`);
    if (contrast.some(pair => !Number.isFinite(pair.ratio) || pair.ratio < 4.5)) {
      throw new Error(`${theme} appearance contrast failure: ${JSON.stringify(contrast.filter(pair => pair.ratio < 4.5))}`);
    }
    const layouts = [];
    for (const viewport of viewports) {
      await command("Emulation.setDeviceMetricsOverride", {
        width: viewport.width, height: viewport.height,
        deviceScaleFactor: 1, mobile: viewport.width < 768,
      });
      for (const route of ["overview", "markets", "positions", "orders", "fills", "strategies", "performance", "risk", "activity"]) {
        await evaluate(`showView("${route}")`);
        await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
        const layout = await evaluate(`(() => {
          const visible = element => element.getClientRects().length > 0;
          const canvas = document.querySelector("#price-chart");
          const controls = [...document.querySelectorAll(".topbar button, .chart-toolbar > *, .ticket-tabs button")]
            .filter(visible);
          const safety = document.querySelector(innerWidth < 768 ? "#mobile-execution" : "#top-execution");
          const reader = document.querySelector("#research-workspace");
          const history = document.querySelector("#research-history");
          const report = document.querySelector("#report-reader");
          return {
            width: document.documentElement.scrollWidth,
            theme: document.documentElement.dataset.theme,
            themeConsistent: getComputedStyle(document.documentElement).colorScheme === "${theme}",
            controlsContained: controls.every(element => {
              const rect = element.getBoundingClientRect();
              const parent = element.parentElement.getBoundingClientRect();
              return rect.left >= parent.left - 1 && rect.right <= parent.right + 1;
            }),
            researchColumns: innerWidth < 1440 || !visible(reader) || (
              history.getBoundingClientRect().right <= report.getBoundingClientRect().left
              && report.getBoundingClientRect().right <= reader.getBoundingClientRect().right
            ),
            safeHeader: visible(safety) && safety.textContent === "执行已锁定",
            executionLocked: ["execute-signal", "unlock-live", "emergency-stop", "toggle-worker"]
              .every(id => document.getElementById(id).disabled),
            painted: !visible(canvas) || canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height)
              .data.some((value, index) => index % 4 === 3 && value > 0),
          };
        })()`);
        if (layout.width > viewport.width || layout.theme !== theme ||
            ["themeConsistent", "controlsContained", "researchColumns", "safeHeader", "executionLocked", "painted"].some(key => !layout[key])) {
          throw new Error(`${theme}/${viewport.name}/${route}: ${JSON.stringify(layout)}`);
        }
        layouts.push({ viewport: viewport.name, route, ...layout });
        if (["desktop", "mobile"].includes(viewport.name) && ["overview", "markets", "strategies", "risk"].includes(route)) {
          await screenshot(`openperpdesk-${theme}-${route}-${viewport.name}.png`);
        }
      }
    }
    report.themes.push({ theme, contrast, layouts });
  }
  report.behavior = await evaluate(`(() => {
    showView("markets");
    showTicket("execution");
    document.querySelector("#size-input").value = "3.25";
    const before = { analysis: state.analysis, status: state.status, candles: state.lastCandles,
      ticket: state.ticket, ledger: state.ledger, revision: state.draftRevision };
    document.querySelector("#open-navigation").click();
    const control = document.querySelector(".navigation-appearance [data-theme-toggle]");
    const mobileToggleVisible = control.getClientRects().length > 0;
    control.click();
    const checks = {
      mobileToggleVisible,
      mobileDialogPreserved: document.querySelector("#navigation-dialog").open,
      toggleApplied: document.documentElement.dataset.theme === "light",
      controlsSynced: [...document.querySelectorAll("[data-theme-toggle]")]
        .every(button => button.getAttribute("aria-pressed") === "true"),
      preferenceSaved: localStorage.getItem("openperpdesk.theme") === "light",
      draftPreserved: document.querySelector("#size-input").value === "3.25"
        && state.ticket === before.ticket && state.ledger === before.ledger
        && state.draftRevision === before.revision,
      tradingStateUntouched: state.status === before.status && state.analysis === before.analysis
        && state.lastCandles === before.candles,
    };
    document.querySelector("#close-navigation").click();
    const originalSet = Storage.prototype.setItem;
    try {
      Storage.prototype.setItem = () => { throw new DOMException("Storage blocked", "SecurityError"); };
      setTheme("dark");
      checks.storageFailureSafe = document.documentElement.dataset.theme === "dark";
    } finally {
      Storage.prototype.setItem = originalSet;
    }
    window.dispatchEvent(new StorageEvent("storage", { key: "openperpdesk.theme", newValue: "light" }));
    checks.crossTabApplied = document.documentElement.dataset.theme === "light";
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated", newValue: "dark" }));
    checks.unrelatedStorageIgnored = document.documentElement.dataset.theme === "light";
    setChartFocus(true);
    setTheme("dark");
    checks.focusModePreserved = state.chartFocused && document.querySelector("#toggle-chart-focus").getAttribute("aria-pressed") === "true";
    setChartFocus(false);
    setTheme("light");
    return checks;
  })()`);
  if (Object.values(report.behavior).some(value => !value)) {
    throw new Error(`Appearance behavior failure: ${JSON.stringify(report.behavior)}`);
  }
  await waitFor('document.activeElement.id === "open-navigation"');
  report.behavior.mobileFocusRestored = true;
  report.behavior.iconAssetsLoaded = await evaluate(`Promise.all(["sun", "moon"].map(async icon => {
    const response = await fetch("/assets/icons/" + icon + ".svg");
    return response.ok && (await response.text()).includes("<svg");
  })).then(results => results.every(Boolean))`);
  const beforeReload = await evaluate("performance.timeOrigin");
  await command("Page.reload");
  await waitFor(`performance.timeOrigin !== ${beforeReload}
    && document.readyState === "complete" && typeof setTheme === "function"`);
  report.behavior.preferenceReloaded = await evaluate(`document.documentElement.dataset.theme === "light"
    && document.querySelector("[data-theme-toggle]").getAttribute("aria-pressed") === "true"`);
  if (!report.behavior.preferenceReloaded) throw new Error("Appearance preference did not survive reload");
  if (!report.behavior.iconAssetsLoaded) throw new Error("Appearance icon assets did not load");
  await evaluate('setTheme("dark")');
  return report;
}
