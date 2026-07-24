# streambot

**A headless automation framework for streamed desktops.**

streambot receives a low-latency desktop video stream as raw frames, evaluates
them with deterministic computer vision (OCR, template matching, colour
predicates), drives a declarative decision workflow, and sends narrowly scoped
synthetic input back over the stream. It needs no guest operating system, no
desktop environment, no on-screen automation window, and no screenshot layer —
frames are decoded straight into NumPy BGR arrays and evaluated in memory.

Frames arrive over the [Moonlight/Sunshine](https://github.com/LizardByte/Sunshine)
desktop-streaming protocol via [`moonlight-python`](https://pypi.org/project/moonlight-python/);
input is sent through the packaged `moonlight-common-c` library. Sunshine streams
any desktop, so the target can be any application you can run on a host you
control.

```
frame → observation → perception → event → coordinator → input
```

Typical uses: robotic process automation over a remote or streamed desktop,
end-to-end UI testing of a streamed application, kiosk and long-running
unattended workflows, and accessibility tooling. It is a general-purpose
perception-and-input platform; the concrete automation for a given target lives
in a pluggable *job* under `jobs/` (see [Jobs](#jobs)).

## Design principles

- **Metadata-only output.** Health and final JSON expose only an allowlist:
  lifecycle state, frame count, input protocol-event count, reconnect count,
  and exception type. Output never includes host addresses, identity material,
  frame content, predicate/signal values, recognized text, or action payloads.
  A dry run leaves `actions_sent` at zero.
- **Fail closed.** The coordinator, input driver, and workflow loader reject
  rather than guess; held input is released on any failure path.
- **Deterministic first.** Prefer declarative predicates and cached results;
  reach for a model only when deterministic recognition cannot resolve the
  current state, and cache any reusable result.
- **Reconnect recovery.** A resumed healthy-frame connection resets the
  consecutive-failure budget while the total reconnect counter stays monotonic.
  A shutdown signal cancels decisions, releases tracked input, disconnects only
  this worker, and reports `cancelled`.

## Install

Create the isolated environment (a project-local `.venv`; nothing is installed
globally):

```bash
./scripts/bootstrap.sh
```

Run the offline self-check — no network discovery, connection, pairing,
streaming, or input:

```bash
./scripts/self_check.py
```

The self-check creates an ephemeral identity in a temporary directory, verifies
its files are private, and exercises a synthetic NumPy-to-image frame path.

Count visible Sunshine hosts without printing their metadata (ephemeral
identity, no connect/pair/stream/input):

```bash
./scripts/discover_count.py
```

## Run a worker

Start the reconnecting worker with one declarative profile:

```bash
./scripts/run_worker.py profiles/observe.json
```

`profiles/observe.json` is observation-only and runs until `SIGINT`/`SIGTERM`; a
workflow profile exits when it reaches a success or failure terminal. Runtime
settings define the frame-liveness timeout, the initial and maximum exponential
backoff, the consecutive-reconnect limit, and the health interval.

For a game-agnostic worker that only publishes frames and serves input over a
local IPC socket — the engine a hot-pluggable job or the control console drives
— use `apps/core-worker/core_worker.py`.

## Perception

`streambot.perception` evaluates in-memory BGR frames using named regions and
strict declarative predicates: a single BGR pixel with tolerance, a region
colour fraction, template similarity, and OCR text containment. Named signals
combine predicates with `all`, `any`, or `not`. Results contain only booleans
and numeric scores — never frames or recognized text.

`profiles/perception-example.json` demonstrates the schema. Templates and OCR
engines are injected at runtime, not embedded in profiles. A built-in NumPy
matcher handles template-aligned regions with no extra dependency;
`OpenCvTemplateMatcher` adds search matching when OpenCV is installed. Each
template identifier is a `.npy` path (loaded with pickle disabled). OCR stays a
programmatic adapter boundary because engine choice and language packs are
target-specific, so the generic CLI rejects OCR profiles without an injected
adapter.

## Decision workflows

`streambot.decision` evaluates perception signals through a strict declarative
state machine. `profiles/workflow-example.json` demonstrates guarded
transitions, state deadlines, success/failure terminals, action identifiers,
retry limits, failure routing, and stable idempotency keys.

An action transition commits its target state only after every action succeeds;
each action gets a stable key so an executor can suppress already-completed work
after a partial failure. Configuration loading rejects ambiguous guards,
duplicate keys, missing deadlines, unknown references, and any state that cannot
reach a terminal. Runtime events carry state/transition metadata, counts, and
exception type only — no frames, signal values, or action payloads.

## Bounded input

`streambot.input` maps declared action names to the input functions exported by
the packaged `moonlight-common-c` library: relative and absolute pointer
movement, mouse button click/press/release, scrolling, and keyboard tap/down/up
with modifier masks. `profiles/input-example.json` demonstrates every action
family and defaults to dry-run mode.

`SafeInputDriver` serializes input, validates every coordinate and range at
profile load, applies a sliding per-minute action limit, and suppresses
completed idempotency keys. It tracks a key or button before pressing it,
attempts emergency release after any failure (three retries), and retains
unresolved held state if all retries fail. Offline tests load the native symbols
but never call them; live input is a separate end-to-end gate.

## Control console

`apps/control-panel/` is a local, browser-based operator console. It supervises
one worker process and drives it through the worker's private IPC socket — it
never opens its own stream connection and binds to `127.0.0.1` only. It shows
connection status, the live frame with detected-control overlays, per-job
metrics (capture→detect and detect→click latency, clicks/min, confidence,
recent errors), and Start/Stop controls. See `apps/control-panel/README.md`.

## Jobs

The platform is target-agnostic; the concrete automation for a given target is a
*job* under `jobs/<name>/`. A job owns its own perception assets and a runner
that reads frames and sends input over the worker's IPC socket, and it publishes
what it currently detects back to the console via the `report-scene` command, so
the overlay reflects whichever job is running. `jobs/` ships empty except for a
short guide — see `jobs/README.md` for the job manifest format and the IPC
surface a runner uses.

## Live-operation safety

Live probes are bounded. In the evaluated upstream release, starting a stream
sends one small mouse movement to force a fresh frame — treat stream startup as
input-producing. A probe must never end a Desktop session that existed before it
connected, and must disconnect only its own client. Persistent identities are
isolated under `.state/<worker-name>/`, pair as a separate client identity, and
are never copied from another client. Record live results without host
addresses or pairing identifiers.

The bounded probes live under `scripts/` (`live_probe.py <phase>`,
`live_recovery_probe.py`, `live_e2e_probe.py`, `observe_probe.py`,
`stability_probe.py`); read the safety notes above before running any of them.

## Tests

The suite uses the stdlib `unittest` runner (there is no pytest):

```bash
.venv/bin/python -m unittest discover -s tests      # full suite
.venv/bin/python -m unittest tests.test_runtime     # single module
```

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — architecture, commands, key invariants, and the
  perception/click latency notes (a guide for humans and AI assistants).
- [`AGENTS.md`](AGENTS.md) — the source of truth for operational and safety
  policy: pairing and credentials, host connection, live-operation safety, and
  the tool-first execution policy.
- [`HOST_CONNECTION_TROUBLESHOOTING.md`](HOST_CONNECTION_TROUBLESHOOTING.md) —
  macOS Local Network Privacy, mDNS, and worker launch-context diagnosis.
- [`apps/control-panel/README.md`](apps/control-panel/README.md) — the control
  console.
- [`jobs/README.md`](jobs/README.md) — how to write a pluggable job.

## License

Released under the [MIT License](LICENSE).
