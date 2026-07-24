# streambot Control Console

A local, browser-based console for launching, watching, and driving the
streambot automation worker. It supervises exactly one worker process and drives
it through the worker's private IPC socket (`.state/<worker>/…-control.sock`) —
the same metadata-only control surface the CLI uses. It never opens its own
stream connection and binds to `127.0.0.1` only.

## Why launch the worker from here

On macOS 15 (Sequoia), Local Network Privacy grants local-subnet access per
**responsible code** — the app macOS blames for the connection, tracked by
code-signing identity plus executable UUID. Homebrew's Python is ad-hoc signed,
so its identity is unstable and its grant depends on the launch context. A
worker launched from an unattributed automation shell can be denied, and every
LAN connection fails with `EHOSTUNREACH` — which surfaces as `discover()`
returning zero hosts even though the host is online.

**Run this console once from a terminal that holds Local Network permission**
(your own Terminal or iTerm — the first launch prompts "allow … to find devices
on your local network"; click Allow). A worker you start from the console is a
child process of it, so macOS attributes the worker's local-network access to
the console's responsible code and it inherits the grant. See
`../../HOST_CONNECTION_TROUBLESHOOTING.md`.

## Run it

From the repository root, in your own terminal:

```bash
.venv/bin/python apps/control-panel/server.py
```

Then open `http://127.0.0.1:8787/`. Options: `--port`, `--state-dir`
(default `.state/poc`), `--control-socket`.

## What it does

- **Start / Stop worker** — supervise one worker child; the control plane
  starts with automation paused.
- **Connection** — a telemetry readout: state, automation enable, frame age
  (colour-graded), reconnect count, and the last error.
- **What the worker sees** — the current scene id and the detected controls as
  a readable list; the recommended control is marked, and clicking a row
  dispatches it by id. A running job publishes what it detects over the
  `report-scene` IPC command, so the overlay reflects whichever job is active.
- **Jobs** — every `jobs/*/job.json` with its running state, per-job metrics
  (capture→detect and detect→click latency, clicks/min, confidence, recent
  errors, last action), and a Start/Stop button.
- **Live frame** — the frame in the browser only, never written to disk (the
  JPEG temp file is deleted immediately). Cadence is selectable — 1 second, 1
  minute, or paused — and paused still refreshes once a minute so the view never
  goes fully stale. Detected controls overlay the frame.
- **Logs** — the tail of the supervised worker's output, on its own tab.

## Safety

- Binds to `127.0.0.1` only; no remote access.
- Keeps no host address, pairing identifier, or credential on disk or in
  responses — the same metadata-only surface as the CLI.
- Automation stays paused until you explicitly resume it.
