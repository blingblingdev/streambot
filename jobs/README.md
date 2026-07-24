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

- `runner` is the command (relative to the repo root) the console runs to start
  the job; extra list entries are passed as arguments. The console runs it with
  the project venv.

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
