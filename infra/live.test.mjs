import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { setImmediate as nextTurn } from "node:timers/promises";

const source = await readFile(new URL("../apps/web/live.js", import.meta.url), "utf8");

function runtime(fetch, timing = {}) {
  return runInNewContext(`${source}\nopenLiveStream`, {
    fetch, AbortController, TextDecoder, performance,
    setTimeout, clearTimeout, setInterval, clearInterval,
    ...timing,
  });
}

test("private event parsing supports split UTF-8 and never puts token in URL", async () => {
  let aborted = false;
  const frames = new TextEncoder().encode('event: activity\r\ndata: {"message":"实时成交"}\r\n\r\n');
  let options;
  let url;
  const open = runtime(async (path, config) => {
    url = path;
    options = config;
    return new Response(new ReadableStream({
      start(controller) {
        for (const byte of frames) controller.enqueue(new Uint8Array([byte]));
        config.signal.addEventListener("abort", () => {
          aborted = true;
          controller.error(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      },
    }), { headers: { "Content-Type": "text/event-stream" } });
  });
  let feed;
  const received = new Promise(resolve => {
    feed = open("/api/v1/account/events", {
      token: "fixture-only-token", onState() {},
      onEvent(event, payload) { resolve({ event, payload }); },
    });
  });
  try {
    const result = await received;
    assert.equal(result.event, "activity");
    assert.equal(result.payload.message, "实时成交");
    assert.equal(url, "/api/v1/account/events");
    assert.equal(options.headers["X-Admin-Token"], "fixture-only-token");
  } finally {
    feed.close();
  }
  assert.equal(options.signal.aborted, true);
  assert.equal(aborted, true);
});

test("authentication rejection locks the stream without retrying", async () => {
  let calls = 0;
  const open = runtime(async () => { calls++; return new Response("", { status: 401 }); });
  let feed;
  const locked = new Promise(resolve => {
    feed = open("/api/v1/account/events", {
      token: "invalid-fixture", onEvent() { assert.fail("No private event allowed"); },
      onState(state) { if (state === "locked") resolve(); },
    });
  });
  await locked;
  feed.close();
  assert.equal(calls, 1);
});

test("a closed connection cannot publish a late authentication result", async () => {
  let resolve;
  const states = [];
  const open = runtime(() => new Promise(done => { resolve = done; }));
  const feed = open("/api/v1/account/events", {
    onState(value) { states.push(value); },
    onEvent() { assert.fail("Closed feed published data"); },
  });
  feed.close();
  resolve(new Response("", { status: 401 }));
  await nextTurn();
  assert.deepEqual(states, ["connecting"]);
});

test("a closed reader drops late buffered data and cancels its body", async () => {
  let producer;
  let cancelled = false;
  const states = [];
  const open = runtime(async () => new Response(new ReadableStream({
    start(controller) { producer = controller; },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "text/event-stream" } }));
  const feed = open("/api/v1/market/events", {
    onState(value) { states.push(value); },
    onEvent() { assert.fail("Closed feed published data"); },
  });
  await nextTurn();
  feed.close();
  producer.enqueue(new TextEncoder().encode('event: market\ndata: {"price":1}\n\n'));
  await nextTurn();
  assert.deepEqual(states, ["connecting"]);
  assert.equal(cancelled, true);
});

test("broken transport and malformed data reconnect before publishing", async () => {
  let calls = 0;
  const states = [];
  const open = runtime(async (_path, options) => {
    calls++;
    if (calls === 1) return new Response("", { status: 502 });
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(
          calls === 2 ? "event: status\ndata: malformed\n\n" : 'event: status\ndata: {"ready":true}\n\n',
        ));
        options.signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")), { once: true });
      },
    }), { headers: { "Content-Type": "text/event-stream" } });
  }, { setTimeout: callback => setTimeout(callback, 5) });
  let feed;
  const payload = await new Promise(resolve => {
    feed = open("/api/v1/system/events", {
      onState(value) { states.push(value); },
      onEvent(_event, value) { resolve(value); },
    });
  });
  feed.close();
  assert.equal(calls, 3);
  assert.equal(payload.ready, true);
  assert.equal(states.filter(value => value === "offline").length, 2);
  assert.equal(states.filter(value => value === "open").length, 1);
});

test("repeated 503 responses keep retrying with capped backoff and recover", async () => {
  const states = [];
  const delays = [];
  let calls = 0;
  const open = runtime(async (_path, options) => {
    calls++;
    if (calls <= 10 || calls === 12) return new Response("", { status: 503 });
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(`event: market\ndata: {"attempt":${calls}}\n\n`));
        if (calls === 11) controller.close();
        else options.signal.addEventListener("abort", () => {
          controller.error(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      },
    }), { headers: { "Content-Type": "text/event-stream" } });
  }, {
    setTimeout(callback, delay) {
      delays.push(delay);
      return setTimeout(callback, 1);
    },
  });
  const events = [];
  let feed;
  try {
    await new Promise(resolve => {
      feed = open("/api/v1/market/events", {
        onState(value) { states.push(value); },
        onEvent(_event, payload) {
          events.push(payload.attempt);
          if (payload.attempt === 13) resolve();
        },
      });
    });
    assert.equal(calls, 13);
    assert.deepEqual(events, [11, 13]);
    assert.deepEqual(delays.slice(0, 10), [1000, 2000, 4000, 8000, 15000, 15000, 15000, 15000, 15000, 15000]);
    assert.deepEqual(delays.slice(10), [1000, 2000]);
    assert.equal(states.filter(value => value === "offline").length, 12);
    assert.equal(states.filter(value => value === "open").length, 2);
  } finally {
    feed.close();
  }
});

test("closing a 503 retry cancels the pending reconnect", async () => {
  let calls = 0;
  let scheduled;
  let cleared;
  const open = runtime(async () => {
    calls++;
    return new Response("", { status: 503 });
  }, {
    setTimeout(callback) { scheduled = callback; return 42; },
    clearTimeout(id) { cleared = id; },
  });
  const feed = open("/api/v1/market/events", { onState() {}, onEvent() {} });
  await nextTurn();
  assert.equal(typeof scheduled, "function");
  feed.close();
  assert.equal(cleared, 42);
  await scheduled();
  assert.equal(calls, 1);
});
