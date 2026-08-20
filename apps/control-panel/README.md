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

Connection trouble is never guessed from reconnect counts. The worker
classifies failures itself (`streambot/connection.py`) and manages the host
Desktop session: it joins an active Desktop session, launches Desktop
proactively when the host is idle (nothing pre-existing to displace), and
waits patiently — state `waiting`, situation `host_busy` or `waiting_host` —
while another application's session is active or the host is asleep. It
reconnects automatically the moment the environment recovers; quitting host
sessions stays forbidden; only real errors consume the reconnect budget and
can end in `failed`. The console turns the worker's `last_error_code` into an
actionable banner instead of a guess.

## Run it

From the repository root, in your own terminal:

```bash
.venv/bin/python apps/control-panel/server.py
```

Then open `http://127.0.0.1:8787/`. Options: `--port`, `--state-dir`
(default `.state/poc`), `--control-socket`, `--jobs-dir` (or
`STREAMBOT_JOBS_DIR`) to load jobs from an external repository — see
`../../jobs/README.md`.

## What it does

- **Start / Stop worker** — supervise one worker child; the control plane
  starts with automation paused. Stream disconnect/reconnect are their own
  buttons while a worker is reachable.
- **Timeline** — the running job's activity as a timeline: every look and
  click is a dot on a spine with the step drawn as its phases (transport,
  classify, locate, click), proportionally, so "where did the time go" is
  visible without reading numbers. Clicking a timed row opens a floating
  breakdown with the milliseconds and what the step was about. Bounded, and
  auto-follows unless you scroll up to read.
- **Running job** — direct Stop and Settings buttons plus macro session data
  (uptime, cycles, total clicks, clicks/min, mean confidence, capture→detect,
  locate, detect→click latency, recent errors) fed by an incremental
  `flow-log.jsonl` reader.
- **Settings while it runs** — a job that declares a `config` block in its
  `job.json` gets a dialog (and presets) editable from the console; a running
  job adopts changes at its next poll, nothing restarts. See
  `../../jobs/README.md`.
- **Live frame** — the frame in the browser only, never written to disk (the
  JPEG temp file is deleted immediately). The scene id, actionable tag and
  cadence selector sit on the card; cadence is 1 second, 1 minute, or paused —
  and paused still refreshes once a minute so the view never goes fully
  stale. Detected controls overlay the frame; clicking a marker dispatches by
  id. The stream's vitals (state, freshness, reconnects, control count) sit
  under the picture.
- **Charts** — capture→detect, locate-control, click confidence and
  clicks/min, persisted in a single SQLite file (`.state/control-panel/`
  `metrics.db`) for thirty days. Preset windows from 15 minutes to 30 days;
  the live view slides with now; dragging pans through history (all charts
  together, wheel zooms) and holds still until you resume live. Buckets
  nobody looked in read as zero — a gap is data. A cold console rebuilds the
  store from the flow logs it finds.
- **Jobs drawer** — every `<jobs-dir>/*/job.json` with its running state,
  Start/Stop, and Settings where declared.
- **Logs** — two views on one tab: the system's turning points as the console
  narrates them (stream drops, reconnects, IPC silences, worker adopted, job
  lifecycle — derived by diffing status once a second, timestamped), and the
  worker log's raw tail.
- **Restart-safe** — closing the console never stops the worker or a running
  job: a restarted console re-adopts the worker through its IPC socket and
  jobs through a process scan that verifies the full command line before it
  will ever signal a pid. Stopping anything is always an explicit action.
- **Platform-owned hourly notification** — the long-running console can build
  one normalized snapshot from its existing worker and `JobSupervisor.status()`
  surfaces and publish it through Coconut Shell. It stays silent when every
  registered job is idle, uses one UTC hour-bucket idempotency key, and obtains
  an optional image only through the existing worker snapshot command. No job
  scans state or owns a notification transport.
- **Fits the screen** — the rail is drag-resizable (double-click resets,
  width remembered); below 820px the console stacks with the cards in one
  scrolling column.

## Safety

- Binds to `127.0.0.1` only; no remote access.
- Keeps no host address, pairing identifier, or credential on disk or in
  responses — the same metadata-only surface as the CLI.
- Automation stays paused until you explicitly resume it.

## Coconut Shell publisher

The publisher is fail-closed and disabled by default. Configuration is injected
into the control-panel process; it is not stored in this repository, a job
manifest, a flow log, or the browser response. The local installation uses the
same HMAC signing key as other trusted local producers. The key signs the full
request body and is not a Feishu credential; the key ID keeps a future
per-project-key migration possible without changing Streambot's message contract.

```text
STREAMBOT_COCONUT_SHELL_PUBLISHER_ENABLED=true
STREAMBOT_HOURLY_PUBLISHER_INCLUDE_SNAPSHOT=true
COCONUT_SHELL_BASE_URL=http://127.0.0.1:18081
COCONUT_SHELL_GLOBAL_KEY=<installation signing key>
COCONUT_SHELL_KEY_ID=local-global
```

Use HTTPS except for a loopback-only test server. Enabling the process setting
does not enable any Coconut Shell notification type: each `streambot.*` type
must pass its separate cutover gate. The hourly publisher does not backfill
missed hours. A restart within the same UTC hour reuses the same idempotency
key, so Coconut Shell returns the existing event rather than sending a
duplicate.

Jobs request typed notifications through `JobEvents.notification`; they never
load Coconut Shell or Feishu credentials. Each `job.json` maps its bounded
logical event names to allowlisted `streambot.*` types and declares whether an
image artifact is forbidden, optional, or required. Streambot collects those
events in its existing SQLite event store, builds the native Feishu card,
passes private spooled evidence by signed local path for immediate snapshotting,
waits for a terminal Coconut Shell result, and only then writes a local
acknowledgement. The first enable initializes at the newest collected event,
so historical flow logs are not replayed.

An unavailable snapshot is optional and does not block the text card. The
publisher polls the source-scoped event status to a terminal result and exposes
only safe state, time, event ID, and reason metadata through the existing
control-panel status response. Credentials, response bodies, target content,
raw logs, and frame bytes are never logged or returned.

## Working on the console itself

The browser code is React + TypeScript + Tailwind under `ui/`, built by bun
into `static/`, and **that build output is committed**. The console is the
process that holds the macOS Local Network grant the worker inherits, so
launching it must never come to require a JavaScript toolchain.

```bash
cd apps/control-panel/ui
bun install
bun run dev        # http://127.0.0.1:5173, proxying /api to the real console
bun test           # phase maths, feed trimming, popover placement, formatters
bun run build      # rewrite static/ — commit the result
```

Develop against a console that is actually running: `bun run dev` proxies every
`/api` call to it, so the UI is built against a live worker and a live job
rather than fixtures. `server.py` sends no CORS headers by design, which is why
this is a proxy rather than a direct cross-origin call.

The Python suite fails if `static/` is older than `ui/src`, so a forgotten
rebuild is caught by the tests rather than by a browser showing an old console.
