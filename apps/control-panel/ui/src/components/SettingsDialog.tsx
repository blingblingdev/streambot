/**
 * A job's settings, changed while it runs.
 *
 * The values are held here as form state, so typing is never interrupted by
 * the once-a-second refresh going on behind the dialog — which is exactly what
 * used to happen when this lived inside the jobs list. A change is sent when
 * the field is committed, and a running job picks it up at its next poll.
 */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { ConfigField, ConfigValue, JobConfig, JobRow } from "../types";
import { Button } from "./ui";

function coerce(field: ConfigField, raw: string | boolean): ConfigValue {
  if (field.type === "boolean") return Boolean(raw);
  if (field.type === "integer") return parseInt(String(raw), 10);
  if (field.type === "number") return parseFloat(String(raw));
  return String(raw);
}

export function SettingsDialog({
  job,
  onClose,
  onToast,
}: {
  job: JobRow;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  const [config, setConfig] = useState<JobConfig | null>(null);
  const [values, setValues] = useState<Record<string, ConfigValue>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.jobConfig(job.name).then((result) => {
      if (!live) return;
      if (result.ok !== true) {
        setError(result.error);
        return;
      }
      setConfig(result);
      setValues(result.values);
    });
    return () => {
      live = false;
    };
  }, [job.name]);

  useEffect(() => {
    const escape = (key: KeyboardEvent) => key.key === "Escape" && onClose();
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);

  async function apply(patch: Record<string, ConfigValue>, label?: string) {
    const result = await api.setJobConfig(job.name, patch);
    if (result.ok !== true) {
      onToast(`Rejected: ${result.detail ?? result.error}`);
      return;
    }
    setValues(result.values);
    onToast(label ? `Applied ${label}` : "Setting saved");
  }

  return (
    <div className="fixed inset-0 z-60 flex items-center justify-center p-5">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative z-1 max-h-[80vh] w-[min(420px,100%)] overflow-y-auto rounded-xl border border-line bg-panel shadow-[0_18px_48px_rgba(0,0,0,.5)]">
        <div className="flex items-center gap-2.5 border-b border-line px-4 py-3.5">
          <div className="flex-1 truncate font-medium">{job.title || job.name}</div>
          <Button small onClick={onClose}>
            Close
          </Button>
        </div>
        <div className="px-4 pt-3 pb-4">
          {error ? (
            <div className="text-[12.5px] text-bad">Settings unavailable: {error}</div>
          ) : !config ? (
            <div className="text-[12.5px] text-faint">Loading…</div>
          ) : (
            <>
              {config.schema.presets.length ? (
                <div className="mb-3 flex flex-wrap gap-1.5">
                  {config.schema.presets.map((preset) => (
                    <Button
                      key={preset.label}
                      small
                      onClick={() => apply(preset.values, preset.label)}
                    >
                      {preset.label}
                    </Button>
                  ))}
                </div>
              ) : null}

              {config.schema.fields.map((field) => {
                const value = values[field.key];
                const commit = (raw: string | boolean) =>
                  apply({ [field.key]: coerce(field, raw) });
                return (
                  <div key={field.key} className="flex items-center gap-2.5 py-1.5">
                    <label
                      className="flex-1 text-[12.5px] text-muted"
                      title={field.help}
                      htmlFor={`cfg-${field.key}`}
                    >
                      {field.label || field.key}
                    </label>
                    {field.type === "enum" ? (
                      <select
                        id={`cfg-${field.key}`}
                        value={String(value ?? "")}
                        onChange={(event) => commit(event.target.value)}
                        className="w-[130px] rounded-md border border-line bg-panel2 px-2 py-1.5 font-mono text-[12.5px] text-text"
                      >
                        {(field.choices ?? []).map((choice) => (
                          <option key={choice} value={choice}>
                            {choice}
                          </option>
                        ))}
                      </select>
                    ) : field.type === "boolean" ? (
                      <input
                        id={`cfg-${field.key}`}
                        type="checkbox"
                        checked={Boolean(value)}
                        onChange={(event) => commit(event.target.checked)}
                      />
                    ) : (
                      <input
                        id={`cfg-${field.key}`}
                        type={field.type === "text" ? "text" : "number"}
                        min={field.min}
                        max={field.max}
                        step={field.type === "number" ? 0.1 : 1}
                        value={String(value ?? "")}
                        onChange={(event) =>
                          setValues((held) => ({
                            ...held,
                            [field.key]: event.target.value,
                          }))
                        }
                        onBlur={(event) => commit(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") event.currentTarget.blur();
                        }}
                        className="w-[130px] rounded-md border border-line bg-panel2 px-2 py-1.5 font-mono text-[12.5px] text-text"
                      />
                    )}
                    <span className="w-[18px] font-mono text-[11px] text-faint">
                      {field.unit}
                    </span>
                  </div>
                );
              })}

              <div className="mt-2.5 text-[11.5px] leading-relaxed text-faint">
                A running job picks these up at its next look — nothing restarts.
                Cycles and Runtime stop it when reached; 0 means no limit.
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
