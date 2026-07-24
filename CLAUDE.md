# CLAUDE.md

Guidance for AI assistants (and humans) working in this repository.

## What this is

streambot is a headless automation framework for streamed desktops. A worker
receives desktop video frames directly as NumPy BGR arrays (via
`moonlight-python`, over the Moonlight/Sunshine protocol), evaluates them with
deterministic perception, drives a declarative decision workflow, and sends
narrowly scoped synthetic input. No guest OS, desktop environment, automation
window, or screenshot layer. See `README.md` for the full overview.

Data flows one direction:

```
frame → observation → perception → event → coordinator → input
```

## Commands

All Python runs inside the project-local `.venv`; never install packages
globally.

```bash
./scripts/bootstrap.sh          # create .venv and install requirements.txt
./scripts/self_check.py         # offline validation: no network, pairing, or input
```

Tests use the stdlib `unittest` runner (there is no pytest):

```bash
.venv/bin/python -m unittest discover -s tests      # full suite
.venv/bin/python -m unittest tests.test_runtime     # single module
```

Offline/safe entry points: `discover_count.py` (counts hosts, prints no
metadata), `run_worker.py <profile.json>`, and `apps/core-worker/core_worker.py`
(the target-agnostic engine: frames + input + IPC only). Live probes require a
paired identity and an already-active Desktop session — read the safety rules
below first.

## Architecture

The `streambot` package (`apps/core-worker/`) is the reusable, target-agnostic
platform, importable from the project venv via a `.pth` entry created by
`bootstrap.sh`.

- `observation.py` — wraps `moonlight-python`, keeps only the latest decoded
  frame, exposes `Observation` (frame + frame number). VideoToolbox with
  software fallback; throttles NumPy conversion to the configured sample rate.
- `perception.py` — evaluates in-memory BGR frames against named regions with
  strict declarative predicates (BGR pixel + tolerance, region colour fraction,
  template similarity, OCR text containment) combined by `all`/`any`/`not`.
  Results are only booleans and numeric scores — never frames or text.
- `perception_service.py` — turns detections into bounded, sequenced
  `PerceptionEvent`s with cadence modes and overflow protection.
- `coordinator.py` — leases at most one allowlisted action per frame, dispatches
  it, verifies visual feedback frame-by-frame, fails closed, releases held input
  on any error.
- `decision.py` — a strict declarative state machine over perception signals:
  guarded transitions, deadlines, terminals, retry limits, idempotency keys.
- `input.py` — maps action names to `moonlight-common-c` functions. Serializes
  input, validates ranges at load, enforces a per-minute limit, tracks held
  keys/buttons, attempts emergency release on failure.
- `runtime.py` — the long-running reconnecting loop wiring scheduler +
  coordinator + observation together. `health_payload` enforces the allowlist.
- `control_plane.py` — a local Unix-socket IPC surface (snapshot, click, status,
  dispatch, `report-scene`) so a job or the console can drive the worker.
- `config.py`, `models.py`, `events.py`, `connection.py`, `ocr.py`,
  `scene.py`, `control_surface.py` — supporting types and adapters.

`apps/control-panel/` is the local operator console. `jobs/` holds pluggable,
target-specific automation (see `jobs/README.md`); `profiles/` holds generic
example profiles.

### Key invariants

- **Metadata-only output.** Health and final JSON expose only an allowlist:
  lifecycle state, frame count, input protocol-event count, reconnect count,
  exception type. Never emit addresses, identity material, frames,
  predicate/signal values, recognized text, or action payloads. A dry run leaves
  `actions_sent` at zero.
- **Fail closed.** Coordinator, input driver, and workflow loader reject rather
  than guess; held input is released on any failure path.
- **Reconnect recovery.** A resumed healthy-frame connection resets the
  consecutive-failure budget; the total reconnect counter stays monotonic. A
  shutdown signal cancels decisions, releases tracked input, disconnects only
  this worker, and reports `cancelled`.

## Live-operation safety

- Never end a Desktop session that existed before the worker connected.
  Coexistence/live probes require an already-active Desktop session, disconnect
  only their own client, and never call the application quit endpoint.
- Starting a stream sends one small mouse nudge to force a fresh frame — treat
  stream startup as an input-producing action.
- Store persistent identities only under `.state/<worker-name>/`, keep
  key/credential files at mode `0600`, and set `umask 077` before creating an
  identity. Pair each worker as a separate client identity.
- Default experiments to low cost (1280x720, 15 FPS, H.264, modest bitrate).

## Conventions

- All code comments, documentation, and commit messages in English.
- Pin the `moonlight-python` release deliberately; record upgrades.

## Perception/click latency

A click loop must observe → match → click well inside the lifetime of the
shortest control. Two costs dominate; benchmark them before tuning anything else:

1. **Frame acquisition: request a JPEG snapshot, not PNG.** Measured round-trip:
   PNG ~617ms vs JPEG ~64ms (~10x). Lossy noise drops a true template match only
   to ~0.92, still well above a 0.85 threshold.
2. **Template matching: band-limit to the control's fixed region, never
   full-frame.** Four full-frame `TM_CCOEFF_NORMED` matches on 1280x720 cost
   ~2568ms; restricting each to its ~300x120 band cut it to ~65ms (~40x), and
   priority-ordered early-exit makes the common case ~12ms.

Corollary — controls render in multiple visual STATES (colour themes, transient
tags). Give each element a LIST of state templates and take the max score;
capture a fixture of any new state and add a template (promote-on-encounter).

## Operational policy

`AGENTS.md` is the source of truth for operational and safety policy (pairing
and credentials, host connection, live-operation safety, the tool-first
execution policy). When it and this file disagree, follow `AGENTS.md`.
