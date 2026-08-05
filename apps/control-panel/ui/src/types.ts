/**
 * The console's wire format, mirroring what `server.py` returns.
 *
 * These are hand-written rather than generated: there are nine endpoints and a
 * generator would be more machinery than the thing it generates. The rule is
 * that every field here exists because a Python line produces it — the
 * authority is `apps/control-panel/server.py` (`ConsoleState.status`,
 * `JobSupervisor.status/config/history`, `FlowLogReader.metrics/history`) and
 * `streambot/job_config.py` for the settings schema.
 *
 * Optional keys are optional because the server omits them, not because they
 * might be null: `ConfigField.min` is absent when unset, while
 * `connection.state` is present and null when there is no IPC. That
 * distinction is load-bearing, so keep `?` and `| null` apart.
 */

export type Situation =
  | "connected"
  | "detached"
  | "waiting_desktop_session"
  | "host_busy"
  | "permission_blocked"
  | "waiting_host"
  | "connecting"
  | "failed"
  | "stopped"
  | "starting"
  | "unknown";

export interface SceneControl {
  control_id: string | null;
  action_kind: string | null;
  confidence: number | null;
  label: string | null;
  x: number | null;
  y: number | null;
}

export interface Status {
  ok: true;
  situation: Situation;
  worker: {
    owned_by_console: boolean;
    pid: number | null;
    socket_present: boolean;
  };
  host_advertising_bonjour: boolean | null;
  connection: {
    state: string | null;
    automation_enabled: boolean | null;
    frame_number: number | null;
    frame_age_ms: number | null;
    reconnects: number | null;
    last_error_type: string | null;
    last_error_code: string | null;
    ipc_error: string | null;
  };
  scene: {
    primary_layout: string | null;
    recommended_control_id: string | null;
    actionable: boolean | null;
    controls: SceneControl[];
  };
  log_tail: string[];
}

export interface JobMetrics {
  uptime_s: number | null;
  clicks_total: number;
  cycles: number;
  mean_score: number | null;
  clicks_per_min: number;
  last_score: number | null;
  last_action: string | null;
  last_action_age_s: number | null;
  perceive_ms: number | null;
  resolve_ms: number | null;
  act_ms: number | null;
  errors_recent: number;
}

/**
 * One line of a job's `flow-log.jsonl`, plus the server-injected `i` the feed
 * dedupes on. `t` is UNIX *seconds* — the console computes uptime against
 * `int(time.time())`, and a job that wrote milliseconds here once reported an
 * uptime of minus fifty-six thousand years.
 */
export interface FlowEvent {
  i: number;
  event: string;
  t: number;
  perceive_ms?: number;
  classify_ms?: number;
  resolve_ms?: number;
  act_ms?: number;
  score?: number;
  element?: string;
  screen?: string | null;
  center?: [number, number];
  completed?: number;
  what?: string;
  wait_s?: number;
  until_s?: number;
  changed?: Record<string, unknown>;
  reason?: string;
  clicks?: number;
  error?: string;
  job?: string;
}

export interface JobRow {
  name: string;
  title: string;
  description: string;
  running: boolean;
  pid: number | null;
  last_log: string;
  metrics: JobMetrics | null;
  events: FlowEvent[];
  configurable: boolean;
}

export type Point = [number, number];

export interface History {
  ok: true;
  perceive: Point[];
  resolve: Point[];
  score: Point[];
  cpm: Point[];
}

export type ConfigValue = string | number | boolean;

export interface ConfigField {
  key: string;
  label: string;
  type: "integer" | "number" | "boolean" | "text" | "enum";
  default: ConfigValue;
  min?: number;
  max?: number;
  choices?: string[];
  unit?: string;
  help?: string;
}

export interface ConfigPreset {
  label: string;
  values: Record<string, ConfigValue>;
}

export interface JobConfig {
  ok: true;
  name: string;
  schema: { fields: ConfigField[]; presets: ConfigPreset[] };
  values: Record<string, ConfigValue>;
  stored: Record<string, unknown>;
}

/** What the SSE stream pushes once a second. Note `jobs` is the bare array
 *  here, while `GET /api/jobs` wraps it in `{ok, jobs}`. */
export interface StreamPayload {
  status: Status;
  jobs: JobRow[];
}

export interface ApiError {
  ok: false;
  error: string;
  detail?: string;
}

export type ApiResult<T> = T | ApiError;

export function failed<T extends { ok: true }>(
  result: ApiResult<T>,
): result is ApiError {
  return result.ok === false;
}
