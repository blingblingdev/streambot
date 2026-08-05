# Jobs

A **job** is a pluggable, target-specific automation for one application. The
`streambot` engine is target-agnostic: the core worker
(`apps/core-worker/core_worker.py`) only receives frames, serves input, and
exposes a local IPC control socket. A job brings the perception and the policy —
it reads frames over that socket, decides what to click, and reports what it
sees. Jobs are hot-pluggable: the console lists every `jobs/*/job.json` and can
start or stop each one, and the running job's detections drive the console
overlay.

This directory ships empty of jobs. Add your own under `jobs/<name>/`.

## Manifest

Each job is a directory with a `job.json`:

```json
{
  "name": "example",
  "title": "Example click loop",
  "description": "Clicks a template whenever it appears.",
  "runner": ["jobs/example/runner.py"]
}
```

- `runner` is the command the console runs to start the job; extra list
  entries are passed as arguments. The console runs it with the project venv.
  Runner paths resolve against the root of the repository the jobs live in
  (the parent of the jobs directory), so `jobs/example/runner.py` works both
  here and in an external jobs repository.

## Settings an operator can change while it runs

A job declares its tunables; the console renders them and writes the values;
`streambot.job_config` re-reads them in the running job. Nothing is pushed and
nothing restarts — the job adopts a change at its next poll, so a setting can
never land halfway through an action.

```json
"config": {
  "fields": [
    {"key": "idle_seconds", "label": "Idle", "type": "integer",
     "min": 30, "max": 3600, "default": 210, "unit": "s"}
  ],
  "presets": [{"label": "Short", "values": {"idle_seconds": 210}}]
}
```

Types are `integer`, `number`, `boolean`, `enum` (with `choices`) and `text`;
numeric fields must declare `min` and `max`, because bounds are what let the
console refuse nonsense before it reaches a running job. Every job also gets
`max_cycles`, `max_seconds` (**0 means unlimited**) and `poll_seconds` without
declaring them, and may redeclare any of the three to tighten its bounds.

Declarations are versioned with the job. Values are not: they are this
machine's preferences, and live in `$STREAMBOT_HOME/.state/job-config/<job>.json`,
written atomically so a job mid-poll never reads half a file.

## External jobs repository

Jobs do not have to live in this checkout. Point the console at any
directory of `<name>/job.json` entries with `--jobs-dir` or the
`STREAMBOT_JOBS_DIR` environment variable:

```bash
STREAMBOT_JOBS_DIR=/path/to/your-jobs-repo/jobs \
  .venv/bin/python apps/control-panel/server.py
```

Job runners the console starts inherit `STREAMBOT_HOME` (this checkout — the
venv, the `streambot` package, and the worker's `.state` socket live here)
and `STREAMBOT_JOBS_DIR`, and run with the jobs repository root as their
working directory. Any number of runners share the single worker stream over
the IPC socket, so a separately managed private jobs repository plus this
platform is a complete replacement for keeping jobs in-tree.

A job owns its own assets (templates as pickle-disabled `.npy` BGR arrays,
OCR language packs, etc.) under its own directory. Keep mutable runtime output
(logs, captures) out of version control; `*/flow-log.jsonl` is already ignored.

## IPC surface

A runner talks to the worker over its control socket
(`.state/<worker>/…-control.sock`) with
`streambot.control_plane.send_control_command`:

- `snapshot` `{output}` — write the latest frame to `output` (use a `.jpg` path;
  it is ~10x faster than PNG to encode/decode).
- `status` — worker health and the current page state.
- `click` / `point` `{x, y}`, `move-rel` `{dx, dy}`, `press`, `escape`, `enter`,
  `backspace` — synthetic input (accepted only while automation is paused, which
  is the default, so a job owns input exclusively).
- `dispatch` `{control_id}` — click a control the job previously reported.
- `report-scene` `{primary_layout, controls, recommended_control_id, ...}` —
  publish what the job currently detects; this becomes the console overlay.
- `register-elements` `{declaration_path, assets_dir}` — hand the worker an
  element declaration (see `streambot.elements`). It loads and validates the
  templates the declaration names and keeps a working copy for this session
  only; the files, their provenance and their history stay in the job.
- `analyze` `{elements}` — classify the latest frame and locate those elements
  (all declared ones if omitted), returning `{screen, instances, classify_ms,
  resolve_ms}`. Runs on the calling connection's thread, so it neither waits
  for the frame loop nor delays anyone else's `status`.

Pass `job="<name>"` to `send_control_command` on every call. Each operation the
worker performs — a look, an analysis, an input — is appended to
`.state/<worker>/operations.jsonl` with that name, its outcome and its
duration. That record is the platform's, not the job's: it says what was done
to the machine, while the job's own `flow-log.jsonl` says what the job meant.

Analysis belongs here rather than in each job for two reasons. It is the same
pipeline every target needs, so a job that implements it again is maintaining a
copy of the platform. And an operation the worker performs is one the worker
can account for — anything a job does with its own matcher is invisible.

## Click-loop recipe

A click loop must observe → match → click well inside the lifetime of the
shortest control. Two costs dominate — benchmark them first:

1. Request a **JPEG** snapshot, not PNG (~64ms vs ~617ms round-trip).
2. **Band-limit** template matching to each control's fixed region rather than
   the full frame (~65ms vs ~2568ms for four controls), and check controls in
   priority order with early-exit.

A control often renders in multiple visual states; give each element a list of
state templates and take the max score, adding a template the first time you
meet a new state.

## Minimal runner sketch

```python
from pathlib import Path
from streambot.control_plane import send_control_command

SOCK = Path(".state/poc/core-control.sock")

def poll():
    send_control_command(SOCK, "snapshot", arguments={"output": "/tmp/f.jpg"})
    frame = load_bgr("/tmp/f.jpg")
    hit = match_your_template(frame)          # band-limited template match
    send_control_command(SOCK, "report-scene", arguments={
        "primary_layout": "example",
        "controls": [hit] if hit else [],
        "recommended_control_id": hit["control_id"] if hit else None,
    })
    if hit:
        send_control_command(SOCK, "click",
                             arguments={"x": hit["x"], "y": hit["y"]})
```
