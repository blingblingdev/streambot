# streambot — Operating Instructions

These instructions apply to this directory and all descendants. `AGENTS.md` is
the source of truth for operational and safety policy; where it and `CLAUDE.md`
disagree, follow `AGENTS.md`.

## Purpose

streambot is a lightweight, headless Python worker that automates a streamed
desktop. The worker receives desktop video frames directly as NumPy BGR arrays
(over the Moonlight/Sunshine streaming protocol), evaluates them with
deterministic perception, and sends narrowly scoped synthetic input. It needs no
guest operating system, desktop environment, automation window, or screenshot
layer.

## Current scope

- A worker pairs its own live host identity under `.state/<worker-name>/` and
  uses it directly for bounded live validation.
- The worker may run bounded live discovery, streaming, and automation probes.

## Jobs (target-specific automation)

- Store target-specific work under `jobs/<name>/` — exactly one dedicated
  directory per target, lowercase ASCII kebab-case, with no mixing of profiles,
  assets, or state between target directories.
- Each job documents its objective, operational state, and any target-specific
  safety constraints before unattended execution.
- Prefer deterministic perception, declarative workflows, and cached results.
  Use a model only when deterministic recognition cannot resolve the current
  state, and cache any reusable result.
- See `jobs/README.md` for the manifest format and the IPC surface a runner
  uses.

## Tool-first execution policy

- Perform live observation and input through the persistent worker's IPC
  surface (status, latest-frame snapshot, pointer, keys, `dispatch`,
  `report-scene`, automation pause/resume). Keep exactly one worker connected.
- Do not repeatedly invoke direct clients, short-lived observation scripts, ad
  hoc screenshot loops, or manual coordinate commands when the persistent tools
  can perform the operation.
- A raw or direct method is allowed only once as a bounded diagnostic when the
  existing tools cannot resolve a newly observed interaction class. State the
  missing capability, minimize the diagnostic input and retained data, and
  preserve the existing Desktop session.
- After a raw diagnostic identifies the interaction, implement, test, and
  register the missing capability in the persistent tools before continuing. The
  next occurrence must use the tool-backed path. Never use the same raw fallback
  consecutively; treat repeated fallback as a tool defect to repair (pause
  input, keep perception running, fix, validate with a bounded replay, resume).

## Credentials and pairing

- Never copy or reuse pairing material from another client identity.
- Store persistent identities only under `.state/<worker-name>/`. Keep private
  keys and all credential-bearing files at mode `0600`, and keep their parent
  directories private. Set the process umask to `077` before creating an
  identity.
- Never print host addresses, pairing PINs, private keys, certificates, unique
  client identifiers, or third-party credentials in logs or command output.
- Pair each worker as a separate client identity. Pairing remains manual when
  Sunshine requires PIN entry.

## Host connection workflow

- Read `HOST_CONNECTION_TROUBLESHOOTING.md` before diagnosing macOS Local
  Network Privacy, mDNS discovery, per-process `EHOSTUNREACH`, dual-interface
  routing, or worker launch-context failures.
- Prefer the local control console `apps/control-panel/server.py` for live
  sessions: launched once from a Local-Network-permitted terminal, the worker it
  starts inherits that grant. See `apps/control-panel/README.md`. The console
  never opens its own stream connection; it supervises one worker and drives it
  over the same IPC socket.
- Run every command from the repository root and use the project-local Python
  environment. Query any existing worker through its control socket before
  starting another — a socket pathname may outlive a crashed or stopped worker,
  so its existence alone does not prove a live service.
- Start exactly one long-running worker in a persistent terminal session:

  ```bash
  .venv/bin/python apps/core-worker/core_worker.py --state-dir .state/poc
  ```

  It establishes the paired connection, owns the latest-frame observer and the
  only input session, starts the local control socket, and stays connected until
  it receives `SIGINT` or `SIGTERM`. A newly started control plane keeps
  automation paused until an explicit resume command.
- Wait for a healthy IPC status (an observing worker, a recent frame, and no
  current error) before issuing commands. Use the status payload rather than
  reading raw frames repeatedly.
- Stop the worker with `SIGINT` in its owning terminal, or a targeted `SIGTERM`
  to that exact process. Let its shutdown handler release held input, close only
  its own control socket, detach its observer, and disconnect only its own
  client. Never stop Sunshine, quit the host Desktop application, or terminate a
  pre-existing Desktop session. Treat a leftover socket as stale only after
  confirming the owning worker process has exited.

## Runtime safety

- Use the project-local `.venv`; do not install Python packages globally.
- Pin the evaluated `moonlight-python` release and record upgrades deliberately.
- Stream startup currently sends a small mouse nudge so the host produces a
  fresh frame. Treat stream startup as an input-producing action.
- Never end a Desktop session that existed before the worker connected.
  Coexistence probes must require an already-active Desktop session, disconnect
  only their own client, and must not call the application quit endpoint.
- A managed worker joins an active Desktop session, may launch Desktop itself
  only when the host reports no active application session (nothing
  pre-existing can be displaced), and waits while another application's
  session is active. Quitting host sessions is forbidden unconditionally.
- Every connection must be constructed through `streambot.worker_main`
  (`run_worker_process`); entry scripts under `apps/` and job runners must
  not open their own connections.
- Default experiments to low-cost settings such as 1280x720, 15 FPS, H.264, and
  a modest bitrate. Keep at most the latest decoded frame unless recording is
  explicitly required.
- Reversible live validation may use `scripts/live_e2e_probe.py`; it must
  calibrate templates only in memory, restore the closed visual state, release
  all held input, and treat an inactive Desktop postcondition as failure.
- The production-shaped entry point is `scripts/run_worker.py`; health output
  must remain within the allowlisted metadata schema, and `SIGINT`/`SIGTERM`
  must release tracked input and disconnect only this worker.
- Recovery validation may inject local observation failures but must never
  terminate or relaunch the host Desktop application.

## Authoring and validation

- Write all repository code comments and documentation in English.
- Run `./scripts/bootstrap.sh` to create the isolated environment, and
  `./scripts/self_check.py` for the safe offline validation.
- Before any live probe, verify the target, pairing state, stream settings,
  expected input side effects, and output paths. Record factual live results
  without including host addresses or pairing identifiers.
