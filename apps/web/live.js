function openLiveStream(path, { token = "", onEvent, onState }) {
  let closed = false;
  let controller;
  let timer;
  let watchdog;
  let failures = 0;
  let lastEvent = 0;

  async function connect() {
    if (closed) return;
    controller = new AbortController();
    onState("connecting");
    lastEvent = performance.now();
    watchdog = setInterval(() => {
      if (performance.now() - lastEvent > 12000) controller.abort();
    }, 1000);
    try {
      const response = await fetch(path, {
        headers: token ? { "X-Admin-Token": token } : {},
        cache: "no-store", signal: controller.signal,
      });
      if (closed) {
        await response.body?.cancel().catch(() => {});
        return;
      }
      if ([401, 403].includes(response.status)) {
        closed = true;
        onState("locked");
        return;
      }
      if (!response.ok || !response.headers.get("content-type")?.includes("text/event-stream")) {
        throw new Error("Live stream unavailable");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      lastEvent = performance.now();
      try {
        while (!closed) {
          const { value, done } = await reader.read();
          if (done) throw new Error("Live stream ended");
          if (closed) break;
          buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
          if (buffer.length > 2 ** 21) throw new Error("Live event too large");
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            if (closed) break;
            const lines = frame.split("\n");
            const event = lines.find(line => line.startsWith("event:"))?.slice(6).trim() || "message";
            const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
            if (!data) continue;
            const payload = JSON.parse(data);
            lastEvent = performance.now();
            failures = 0;
            onState("open");
            if (!closed) onEvent(event, payload);
          }
        }
      } finally {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    } catch {
      if (!closed) onState("offline");
    } finally {
      clearInterval(watchdog);
      if (!closed) timer = setTimeout(connect, Math.min(15000, 1000 * 2 ** Math.min(failures++, 4)));
    }
  }

  connect();
  return {
    close() {
      closed = true;
      clearTimeout(timer);
      clearInterval(watchdog);
      controller?.abort();
    },
  };
}
