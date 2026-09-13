import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const version = "0.468.0";
const base = `https://raw.githubusercontent.com/lucide-icons/lucide/${version}`;
const destination = path.resolve("apps/web/assets/icons");
const icons = [
  "layout-dashboard", "chart-candlestick", "wallet", "list-ordered",
  "arrow-left-right", "workflow", "chart-no-axes-combined", "shield-check",
  "scroll-text", "key-round", "refresh-cw", "pause", "play", "x",
  "circle-stop", "chevron-right", "bell", "flask-conical", "scan-line",
  "lock-keyhole", "panel-left-close", "panel-left-open", "download", "sun", "moon",
];

await mkdir(destination, { recursive: true });
await Promise.all([
  ...icons.map((name) => ({ remote: `icons/${name}.svg`, local: `${name}.svg` })),
  { remote: "LICENSE", local: "LICENSE" },
].map(async ({ remote, local }) => {
  const response = await fetch(`${base}/${remote}`);
  if (!response.ok) throw new Error(`Lucide ${remote}: HTTP ${response.status}`);
  const content = await response.text();
  if (local.endsWith(".svg") && !content.includes("<svg")) {
    throw new Error(`Invalid SVG asset: ${local}`);
  }
  await writeFile(path.join(destination, local), content);
}));
console.log(`Vendored ${icons.length} Lucide ${version} icons and license.`);
