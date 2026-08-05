/**
 * The time window, chosen the way a monitoring tool does it: one button whose
 * label says what you are looking at, opening a panel with the quick ranges
 * on one side and an absolute from/to on the other. It lives at a fixed spot
 * on the right of the bar, so the legend growing or shrinking on the left
 * never moves it.
 */

import { useEffect, useRef, useState } from "react";

import {
  MAX_RANGE_SECONDS,
  RANGE_PRESETS,
  clampWindow,
  windowLabel,
} from "../lib/timerange";
import { Button } from "./ui";

function toLocalInput(t: number): string {
  const d = new Date(t * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(value: string): number | null {
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

export function TimePicker({
  range,
  pinnedEnd,
  onApply,
}: {
  range: number;
  pinnedEnd: number | null;
  /** live=true means "last <range>, following now". */
  onApply: (rangeSeconds: number, pinnedEnd: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const now = Math.floor(Date.now() / 1000);
  const end = pinnedEnd ?? now;
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Prefill the absolute inputs with the window being looked at.
  useEffect(() => {
    if (!open) return;
    setFromText(toLocalInput(end - range));
    setToText(toLocalInput(end));
    setProblem(null);
  }, [open, range, end]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  function applyAbsolute() {
    const from = fromLocalInput(fromText);
    const to = fromLocalInput(toText);
    if (from === null || to === null) return setProblem("unreadable time");
    if (to <= from) return setProblem("end must be after start");
    if (to - from > MAX_RANGE_SECONDS) return setProblem("history keeps 30 days");
    const landed = clampWindow(from, to, Math.floor(Date.now() / 1000));
    onApply(landed.end - landed.start, landed.live ? null : landed.end);
    setOpen(false);
  }

  return (
    <div ref={box} className="relative">
      <Button small onClick={() => setOpen((held) => !held)} className="font-mono">
        <span className="mr-1 text-faint">⏱</span>
        {windowLabel(range, pinnedEnd, now)}
      </Button>
      {open ? (
        <div className="absolute right-0 z-50 mt-1 flex w-[330px] rounded-lg border border-line bg-panel shadow-[0_12px_32px_rgba(0,0,0,.5)]">
          <div className="w-[92px] border-r border-line py-1">
            {RANGE_PRESETS.map((preset) => (
              <button
                key={preset.label}
                onClick={() => {
                  onApply(preset.seconds, null);
                  setOpen(false);
                }}
                className={
                  `block w-full cursor-pointer px-3 py-1.5 text-left font-mono text-[11.5px] ` +
                  (pinnedEnd === null && range === preset.seconds
                    ? "bg-blue/15 text-blue"
                    : "text-muted hover:bg-white/4 hover:text-text")
                }
              >
                Last {preset.label}
              </button>
            ))}
          </div>
          <div className="flex-1 p-3">
            <div className="mb-2 text-[10.5px] tracking-wide text-faint uppercase">
              Absolute range
            </div>
            {(
              [
                ["From", fromText, setFromText],
                ["To", toText, setToText],
              ] as const
            ).map(([label, value, set]) => (
              <label key={label} className="mb-2 block text-[11px] text-muted">
                {label}
                <input
                  type="datetime-local"
                  value={value}
                  onChange={(event) => set(event.target.value)}
                  className="mt-0.5 w-full rounded-md border border-line bg-panel2 px-2 py-1 font-mono text-[11.5px] text-text [color-scheme:dark]"
                />
              </label>
            ))}
            {problem ? (
              <div className="mb-1.5 text-[11px] text-bad">{problem}</div>
            ) : null}
            <Button small kind="primary" onClick={applyAbsolute} className="w-full">
              Apply
            </Button>
            <div className="mt-2 text-[10px] leading-relaxed text-faint">
              Or shift-drag on a chart to select a range; plain drag pans,
              wheel zooms.
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
