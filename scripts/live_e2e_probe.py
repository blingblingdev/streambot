#!/usr/bin/env python3
"""Run two reversible visual decision and input loops against active Desktop."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(VENV_PYTHON, [str(VENV_PYTHON), str(Path(__file__).resolve())])

sys.path.insert(0, str(PROJECT_DIR / "apps" / "core-worker"))

from streambot.config import AutomationProfile
from streambot.connection import connect_paired_worker, desktop_session_is_active
from streambot.decision import WorkflowEngine
from streambot.input import MoonlightCffiTransport, SafeInputDriver
from streambot.live_validation import CalibratedRegion, calibrate_reversible_region
from streambot.models import RunOutcome
from streambot.observation import LatestFrameObserver
from streambot.perception import PerceptionEngine


def base_mapping() -> dict[str, object]:
    """Return the bounded live stream and reversible input configuration."""

    return {
        "name": "live-e2e",
        "stream": {
            "width": 1280,
            "height": 720,
            "fps": 15,
            "bitrate_kbps": 4000,
            "codec": "h264",
        },
        "observation": {
            "sample_fps": 4,
            "decoder": "videotoolbox",
            "software_fallback": True,
        },
        "actions": [{"name": "toggle-overlay", "type": "key", "key_code": 91}],
        "safety": {
            "preserve_existing_desktop": True,
            "dry_run": False,
            "max_actions_per_minute": 12,
        },
    }


def workflow_mapping(calibration: CalibratedRegion) -> dict[str, object]:
    """Build an ephemeral profile around calibrated in-memory templates."""

    value = base_mapping()
    value["perception"] = {
        "regions": [
            {
                "name": "overlay-anchor",
                "x": calibration.x,
                "y": calibration.y,
                "width": calibration.width,
                "height": calibration.height,
            }
        ],
        "predicates": [
            {
                "name": "closed-template",
                "type": "template",
                "region": "overlay-anchor",
                "template": "closed",
                "threshold": calibration.closed_threshold,
            },
            {
                "name": "opened-template",
                "type": "template",
                "region": "overlay-anchor",
                "template": "opened",
                "threshold": calibration.opened_threshold,
            },
        ],
        "signals": [
            {"name": "closed", "operator": "all", "predicates": ["closed-template"]},
            {"name": "opened", "operator": "all", "predicates": ["opened-template"]},
        ],
    }
    value["workflow"] = {
        "initial_state": "waiting",
        "event_history_limit": 100,
        "states": [
            {
                "name": "waiting",
                "timeout_seconds": 8,
                "timeout_state": "failed",
                "transitions": [
                    {
                        "name": "open-overlay",
                        "signal": "closed",
                        "target": "verifying-open",
                        "actions": ["toggle-overlay"],
                        "idempotency_key": "open-overlay",
                        "max_retries": 1,
                        "failure_state": "failed",
                    }
                ],
            },
            {
                "name": "verifying-open",
                "timeout_seconds": 8,
                "timeout_state": "failed",
                "transitions": [
                    {
                        "name": "close-overlay",
                        "signal": "opened",
                        "target": "verifying-closed",
                        "actions": ["toggle-overlay"],
                        "idempotency_key": "close-overlay",
                        "max_retries": 1,
                        "failure_state": "failed",
                    }
                ],
            },
            {
                "name": "verifying-closed",
                "timeout_seconds": 8,
                "timeout_state": "failed",
                "transitions": [
                    {
                        "name": "finish",
                        "signal": "closed",
                        "target": "completed"
                    }
                ],
            },
            {"name": "completed", "terminal": "success"},
            {"name": "failed", "terminal": "failure"},
        ],
    }
    return value


def collect(observer: LatestFrameObserver, count: int) -> list[object]:
    """Collect a bounded number of in-memory frame arrays."""

    frames = []
    for _ in range(count):
        observation = observer.observe(timeout=1.5)
        if observation is not None:
            frames.append(observation.data.copy())
    if len(frames) < count:
        raise RuntimeError("Insufficient live frames")
    return frames


def run_workflow_pass(
    observer: LatestFrameObserver,
    profile: AutomationProfile,
    transport: MoonlightCffiTransport,
    calibration: CalibratedRegion,
    pass_number: int,
) -> dict[str, object]:
    """Execute and verify one complete closed-open-closed state sequence."""

    if profile.workflow is None:
        raise RuntimeError("Live workflow is unavailable")
    inputs = SafeInputDriver(
        profile.actions, profile.safety, profile.stream, transport
    )
    perception = PerceptionEngine(
        profile.perception,
        templates={
            "closed": calibration.closed_template,
            "opened": calibration.opened_template,
        },
    )
    engine = WorkflowEngine(profile.workflow, inputs)
    overlay_open = False
    deadline = time.monotonic() + 25.0
    opened_verified = False
    restored_verified = False
    try:
        while engine.snapshot().outcome is None and time.monotonic() < deadline:
            observation = observer.observe(timeout=1.5)
            if observation is None:
                continue
            signals = perception.evaluate(observation.data).signals
            before = engine.snapshot().state
            snapshot = engine.tick(signals)
            if before == "waiting" and snapshot.state == "verifying-open":
                overlay_open = True
            elif before == "verifying-open" and snapshot.state == "verifying-closed":
                opened_verified = True
                overlay_open = False
            elif before == "verifying-closed" and snapshot.state == "completed":
                restored_verified = True
        snapshot = engine.snapshot()
        if snapshot.outcome is not RunOutcome.SUCCESS:
            raise RuntimeError("Live workflow did not reach success")
        if not opened_verified or not restored_verified or overlay_open:
            raise RuntimeError("Live visual postconditions were incomplete")
        return {
            "pass": pass_number,
            "status": "PASS",
            "events": snapshot.event_count,
            "actions_completed": inputs.actions_completed,
            "protocol_events_sent": inputs.protocol_events_sent,
            "opened_verified": opened_verified,
            "restored_verified": restored_verified,
            "held_inputs": inputs.held_input_count,
        }
    finally:
        try:
            visually_open = False
            if not restored_verified and transport.is_connected:
                open_votes = 0
                observations = 0
                for _ in range(3):
                    observation = observer.observe(timeout=0.75)
                    if observation is None:
                        continue
                    observations += 1
                    open_votes += int(
                        perception.evaluate(observation.data).signals["opened"]
                    )
                visually_open = observations >= 2 and open_votes >= 2
            if (overlay_open or visually_open) and transport.is_connected:
                inputs.execute("toggle-overlay", f"emergency-close-{pass_number}")
        finally:
            inputs.release_all()


def main() -> int:
    """Calibrate without persistence and run two consecutive live passes."""

    phase = "live-e2e"
    previous_umask = os.umask(0o077)
    result: dict[str, object] = {
        "phase": phase,
        "sensitive_details_exposed": False,
        "images_written": 0,
        "existing_desktop_ended": False,
    }
    stage = "configuration"
    try:
        base_profile = AutomationProfile.from_mapping(base_mapping())
        stage = "connection"
        client = connect_paired_worker(base_profile, PROJECT_DIR / ".state" / "poc")
        observer = LatestFrameObserver(client, base_profile)
        stage = "observation"
        with observer:
            transport = MoonlightCffiTransport(
                lambda: bool(client._session and client._session.is_connected)
            )
            calibration_inputs = SafeInputDriver(
                base_profile.actions,
                base_profile.safety,
                base_profile.stream,
                transport,
            )
            calibration_open = False
            try:
                stage = "calibration-capture"
                time.sleep(1.5)
                closed_before = collect(observer, 5)
                calibration_inputs.execute("toggle-overlay", "calibration-open")
                calibration_open = True
                time.sleep(0.6)
                opened = collect(observer, 5)
                calibration_inputs.execute("toggle-overlay", "calibration-close")
                calibration_open = False
                time.sleep(0.6)
                closed_after = collect(observer, 5)
            finally:
                try:
                    if calibration_open and transport.is_connected:
                        calibration_inputs.execute(
                            "toggle-overlay", "calibration-emergency-close"
                        )
                finally:
                    calibration_inputs.release_all()

            stage = "calibration-analysis"
            calibration = calibrate_reversible_region(
                closed_before, opened, closed_after
            )
            profile = AutomationProfile.from_mapping(workflow_mapping(calibration))
            passes = []
            for number in (1, 2):
                stage = f"workflow-pass-{number}"
                passes.append(
                    run_workflow_pass(
                        observer, profile, transport, calibration, number
                    )
                )

        stage = "postcondition"
        desktop_active = desktop_session_is_active(client)
        if not desktop_active:
            raise RuntimeError("Pre-existing Desktop session is no longer active")
        result.update(
            {
                "status": "PASS",
                "decoder": client.decoder_backend,
                "decoder_used_fallback": client.decoder_used_fallback,
                "calibration_separation": round(calibration.separation, 3),
                "passes": passes,
                "desktop_active_after": desktop_active,
                "residual_held_inputs": sum(item["held_inputs"] for item in passes),
            }
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        result.update(
            {
                "status": "FAIL",
                "stage": stage,
                "error_type": type(error).__name__,
            }
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
