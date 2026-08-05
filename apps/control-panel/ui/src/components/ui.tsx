/** The handful of shapes the console repeats. */

import type { ReactNode } from "react";
import type { Grade } from "../lib/format";
import { GRADE_TEXT } from "../lib/format";

const LED: Record<string, string> = {
  ok: "bg-ok shadow-[0_0_8px_rgba(63,185,80,.7)]",
  warn: "bg-warn shadow-[0_0_8px_rgba(214,160,53,.6)]",
  bad: "bg-bad shadow-[0_0_8px_rgba(240,85,63,.6)]",
  "": "bg-faint",
};

export function Led({ grade = "" as Grade | string }) {
  return (
    <span
      className={`inline-block size-[7px] shrink-0 rounded-full ${LED[grade] ?? LED[""]}`}
    />
  );
}

type ButtonKind = "primary" | "danger" | "ghost";

const BUTTON: Record<ButtonKind, string> = {
  primary: "bg-blue/15 text-blue border-blue/40 hover:bg-blue/25",
  danger: "bg-bad/12 text-bad border-bad/40 hover:bg-bad/22",
  ghost: "bg-panel2 text-muted border-line hover:text-text hover:border-faint",
};

export function Button({
  kind = "ghost",
  small,
  className = "",
  ...rest
}: {
  kind?: ButtonKind;
  small?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...rest}
      className={
        `cursor-pointer rounded-md border font-sans transition-colors ` +
        `disabled:cursor-default disabled:opacity-40 ` +
        (small ? "px-2.5 py-1 text-[11.5px] " : "px-3 py-1.5 text-[12.5px] ") +
        BUTTON[kind] +
        " " +
        className
      }
    />
  );
}

export function Section({
  title,
  extra,
  children,
  className = "",
}: {
  title: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`flex flex-col border-b border-line ${className}`}>
      <h2 className="flex items-center px-3.5 pt-3 pb-1.5 text-[10.5px] font-semibold tracking-[.09em] text-faint uppercase">
        {title}
        {extra}
      </h2>
      <div className="min-h-0 px-3.5 pb-3">{children}</div>
    </section>
  );
}

/** One labelled number in the running-job grid. */
export function Cell({
  label,
  value,
  grade = "",
  unit,
}: {
  label: string;
  value: ReactNode;
  grade?: Grade;
  unit?: string;
}) {
  return (
    <div className="rounded-lg border border-line bg-panel2 px-2.5 py-2">
      <div className="text-[10px] tracking-wide text-faint uppercase">{label}</div>
      <div className={`font-mono text-[15px] ${GRADE_TEXT[grade]}`}>
        {value}
        {unit ? <small className="ml-0.5 text-[10px] text-faint">{unit}</small> : null}
      </div>
    </div>
  );
}

export function Pill({ grade, children }: { grade: Grade; children: ReactNode }) {
  const tone =
    grade === "ok"
      ? "text-ok border-ok/45 bg-ok/10"
      : grade === "warn"
        ? "text-warn border-warn/45 bg-warn/10"
        : grade === "bad"
          ? "text-bad border-bad/45 bg-bad/10"
          : "text-muted border-line";
  return (
    <span className={`shrink-0 rounded-full border px-[7px] text-[10px] ${tone}`}>
      {children}
    </span>
  );
}
