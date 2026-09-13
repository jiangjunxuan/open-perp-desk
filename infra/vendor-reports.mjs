import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
const packages = [
  {
    name: "marked",
    version: "18.0.13",
    integrity: "xTxVzZsBFwunP6HDmtBkabUQEYArnP7/rMDGmPj9SlrKlQ4i8MdYVow+nJL0eOqwpUqhzBoTBRADGN6uYwPyOw==",
    files: { "lib/marked.umd.js": "marked.js", LICENSE: "MARKED-LICENSE.md" },
  },
  {
    name: "dompurify",
    version: "3.4.15",
    integrity: "EUBjM+B+lkDE41iE82DDSCfkoPGfXx8IxFxPMjNzm/Uk4xDet77rTN9wqlxlVg71kK7XGuUMv6wUxJUwwv+Xyw==",
    files: { "dist/purify.min.js": "purify.min.js", LICENSE: "DOMPURIFY-LICENSE" },
  },
];

const destination = path.resolve("apps/web/assets/reports");
const temporary = await mkdtemp(path.join(tmpdir(), "openperpdesk-vendor-"));
try {
  await mkdir(destination, { recursive: true });
  for (const item of packages) {
    const response = await fetch(`https://registry.npmjs.org/${item.name}/-/${item.name}-${item.version}.tgz`);
    if (!response.ok) throw new Error(`${item.name}: HTTP ${response.status}`);
    const archive = Buffer.from(await response.arrayBuffer());
    if (createHash("sha512").update(archive).digest("base64") !== item.integrity) {
      throw new Error(`${item.name}: archive integrity mismatch`);
    }
    const archivePath = path.join(temporary, `${item.name}.tgz`);
    await writeFile(archivePath, archive);
    for (const [source, target] of Object.entries(item.files)) {
      const { stdout } = await run("tar", ["-xOf", archivePath, `package/${source}`], { maxBuffer: 4 * 1024 * 1024 });
      await writeFile(path.join(destination, target), stdout);
    }
    console.log(`Vendored ${item.name} ${item.version} with license and verified SHA-512.`);
  }
} finally {
  await rm(temporary, { recursive: true, force: true });
}
