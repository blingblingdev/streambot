/**
 * Build the console into `apps/control-panel/static/`, which is what the
 * Python server serves and what is committed to the repository.
 *
 * Committing the output is deliberate. The console is launched as
 * `.venv/bin/python apps/control-panel/server.py` from a terminal that holds
 * macOS Local Network permission, and the worker inherits that grant by being
 * its child. Nothing on that path may require bun to be installed, or a
 * machine without a JavaScript toolchain could no longer drive the worker.
 */

import { rm, mkdir, writeFile } from "node:fs/promises";
import { $ } from "bun";

const ui = import.meta.dir;
const outdir = `${ui}/../static`;
const assets = `${outdir}/assets`;

await rm(assets, { recursive: true, force: true });
await mkdir(assets, { recursive: true });

const result = await Bun.build({
  entrypoints: [`${ui}/src/main.tsx`],
  outdir: assets,
  target: "browser",
  minify: true,
  naming: "[name]-[hash].[ext]",
  sourcemap: "none", // never ship the paths of this checkout to a browser
  define: { "process.env.NODE_ENV": '"production"' },
});

if (!result.success) {
  for (const log of result.logs) console.error(log);
  process.exit(1);
}

const script = result.outputs.find((output) => output.path.endsWith(".js"));
if (!script) {
  console.error("build produced no script");
  process.exit(1);
}
const scriptName = script.path.split("/").pop()!;

// Tailwind scans the source for the classes actually used and emits only
// those. Run through bun so the CLI resolves from this package.
await $`bun run tailwindcss -i ${ui}/src/index.css -o ${assets}/app.css --minify`.quiet();
const css = Bun.file(`${assets}/app.css`);
const cssHash = Bun.hash(await css.arrayBuffer()).toString(16).slice(0, 8);
const cssName = `app-${cssHash}.css`;
await Bun.write(`${assets}/${cssName}`, css);
await rm(`${assets}/app.css`);

const template = await Bun.file(`${ui}/index.html`).text();
const html = template
  .replace(
    '<script type="module" src="./src/main.tsx"></script>',
    `<script type="module" src="/assets/${scriptName}"></script>`,
  )
  .replace("</head>", `  <link rel="stylesheet" href="/assets/${cssName}" />\n  </head>`);
await writeFile(`${outdir}/index.html`, html, "utf8");

const bytes = (await Promise.all(
  [script.path, `${assets}/${cssName}`].map(async (path) =>
    (await Bun.file(path).arrayBuffer()).byteLength,
  ),
)).reduce((a, b) => a + b, 0);
console.log(`built ${scriptName} + ${cssName} (${Math.round(bytes / 1024)} KB)`);
