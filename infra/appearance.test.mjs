import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";

const web = new URL("../apps/web/", import.meta.url);
const bootstrap = await readFile(new URL("theme.js", web), "utf8");

for (const [stored, expected] of [[null, "dark"], ["dark", "dark"], ["light", "light"], ["invalid", "dark"]]) {
  test(`appearance bootstrap validates ${JSON.stringify(stored)}`, () => {
    const document = { documentElement: { dataset: {} } };
    runInNewContext(bootstrap, {
      document,
      localStorage: { getItem(key) {
        assert.equal(key, "openperpdesk.theme");
        return stored;
      } },
    });
    assert.equal(document.documentElement.dataset.theme, expected);
  });
}

test("appearance bootstrap tolerates inaccessible storage", () => {
  const document = { documentElement: { dataset: {} } };
  runInNewContext(bootstrap, {
    document,
    get localStorage() { throw new Error("Storage denied"); },
  });
  assert.equal(document.documentElement.dataset.theme, "dark");
});

test("all first-party page scripts are shipped in the Docker web context", async () => {
  const html = await readFile(new URL("index.html", web), "utf8");
  const dockerfile = await readFile(new URL("Dockerfile", web), "utf8");
  const ignores = (await readFile(new URL(".dockerignore", web), "utf8")).split("\n");
  const copySources = dockerfile.split("\n")
    .filter(line => line.startsWith("COPY "))
    .flatMap(line => line.trim().split(/\s+/).slice(1, -1));
  const scripts = [...html.matchAll(/<script src="\/([^/"]+\.js)"/g)].map(match => match[1]);
  assert.ok(scripts.includes("theme.js"));
  assert.ok(scripts.includes("research.js"));
  for (const script of scripts) {
    assert.ok(copySources.includes(script), `${script} missing from Docker COPY`);
    assert.ok(ignores.includes(`!${script}`), `${script} excluded from build context`);
    assert.ok((await readFile(new URL(script, web), "utf8")).length > 0);
  }
  assert.ok(html.indexOf('/theme.js') < html.indexOf('/styles.css'), "Theme must load before CSS");
});
