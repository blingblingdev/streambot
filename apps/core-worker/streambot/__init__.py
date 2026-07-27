"""Reusable headless stream-automation components."""

from .branch_ledger import BranchLedger, BranchLedgerError
from .config import AutomationProfile, load_profile
from .connection import (
    ConnectFailure,
    DesktopAppMissing,
    DesktopSessionInactive,
    HostSessionBusy,
    HostUnreachable,
    MultipleHostsVisible,
    NoHostVisible,
    connect_paired_worker,
    desktop_session_is_active,
)
from .control_surface import (
    Control,
    ManifestControlScanner,
    ManifestError,
    load_control_manifest,
)
from .layout_detection import LayoutDetector
from .decision import EngineSnapshot, TransitionEvent, WorkflowEngine
from .input import MoonlightCffiTransport, SafeInputDriver
from .models import RunOutcome, WorkerHealth, WorkerState
from .ocr import OcrError, OcrLine, RapidOcrAdapter
from .observation import AutomationMoonlightClient, LatestFrameObserver, Observation
from .perception import PerceptionEngine, PerceptionResult
from .runtime import AutomationWorker, health_payload
from .worker_main import run_worker_process
from .model_fallback import NovelSceneFallback, SceneResolver
from .task_engine import (
    TaskDefinitionError,
    TaskDispatcher,
    TaskEngine,
    TaskError,
    TaskEvent,
    TaskSnapshot,
    load_task_definition,
)
from .scene import (
    ControlFact,
    LineOcrAdapter,
    OcrFact,
    SceneEngine,
    SceneError,
    SceneFacts,
    SceneManifestError,
    load_scene_manifest,
)

__all__ = [
    "AutomationProfile",
    "AutomationWorker",
    "AutomationMoonlightClient",
    "BranchLedger",
    "BranchLedgerError",
    "Control",
    "ControlFact",
    "LayoutDetector",
    "LineOcrAdapter",
    "ManifestControlScanner",
    "ManifestError",
    "load_control_manifest",
    "EngineSnapshot",
    "LatestFrameObserver",
    "MoonlightCffiTransport",
    "NovelSceneFallback",
    "Observation",
    "OcrError",
    "OcrFact",
    "OcrLine",
    "PerceptionEngine",
    "PerceptionResult",
    "SceneEngine",
    "SceneError",
    "SceneFacts",
    "SceneManifestError",
    "SceneResolver",
    "TaskDefinitionError",
    "TaskDispatcher",
    "TaskEngine",
    "TaskError",
    "TaskEvent",
    "TaskSnapshot",
    "load_scene_manifest",
    "load_task_definition",
    "TransitionEvent",
    "RunOutcome",
    "RapidOcrAdapter",
    "SafeInputDriver",
    "WorkerHealth",
    "WorkerState",
    "WorkflowEngine",
    "ConnectFailure",
    "DesktopAppMissing",
    "DesktopSessionInactive",
    "HostSessionBusy",
    "HostUnreachable",
    "MultipleHostsVisible",
    "NoHostVisible",
    "connect_paired_worker",
    "desktop_session_is_active",
    "load_profile",
    "health_payload",
    "run_worker_process",
]
