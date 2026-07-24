#!/usr/bin/env python3
"""Start one recovering headless worker from a declarative JSON profile."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix:
    if not VENV_PYTHON.is_file():
        raise SystemExit("Project environment is missing; run ./scripts/bootstrap.sh first")
    os.execv(
        VENV_PYTHON,
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

sys.path.insert(0, str(PROJECT_DIR / "apps" / "core-worker"))

import numpy as np

from streambot.config import AutomationProfile, load_profile
from streambot.connection import connect_paired_worker
from streambot.models import RunOutcome, WorkerHealth
from streambot.runtime import AutomationWorker, health_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run metadata-only headless stream automation"
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=PROJECT_DIR / ".state" / "poc",
        help="paired worker identity directory",
    )
    return parser.parse_args()


def load_templates(profile: AutomationProfile, profile_path: Path) -> dict[str, np.ndarray]:
    """Load declared NumPy templates without enabling pickle payloads."""

    templates: dict[str, np.ndarray] = {}
    for predicate in profile.perception.predicates:
        if predicate.kind != "template" or predicate.template is None:
            continue
        path = Path(predicate.template)
        if not path.is_absolute():
            path = profile_path.resolve().parent / path
        value = np.load(path, allow_pickle=False)
        if not isinstance(value, np.ndarray):
            raise RuntimeError("template did not contain an array")
        templates[predicate.template] = value
    return templates


def emit_health(health: WorkerHealth) -> None:
    """Write one allowlisted status object without host or observation content."""

    print(json.dumps({"type": "health", **health_payload(health)}, sort_keys=True), flush=True)


def main() -> int:
    """Load, run, and gracefully stop one configured worker."""

    previous_umask = os.umask(0o077)
    phase = "worker"
    try:
        args = parse_args()
        profile_path = args.profile.resolve()
        profile = load_profile(profile_path)
        if any(
            predicate.kind == "ocr" for predicate in profile.perception.predicates
        ):
            raise RuntimeError("CLI profile requires a programmatic OCR adapter")
        templates = load_templates(profile, profile_path)
        worker = AutomationWorker(
            profile,
            lambda: connect_paired_worker(profile, args.state_dir),
            templates=templates,
            health_callback=emit_health,
        )

        def request_stop(_signum: int, _frame: object) -> None:
            worker.request_stop()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        outcome = worker.run()
        print(
            json.dumps(
                {
                    "type": "result",
                    "phase": phase,
                    "outcome": outcome.value,
                    "sensitive_details_exposed": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if outcome in {RunOutcome.SUCCESS, RunOutcome.CANCELLED} else 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "type": "result",
                    "phase": phase,
                    "outcome": "failure",
                    "error_type": type(error).__name__,
                    "sensitive_details_exposed": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
