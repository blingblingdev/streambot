/**
 * The session's shape over time, the way a monitoring tool draws it: the
 * x axis IS the requested window — fixed edges, zero-filled buckets — so a
 * live view slides as now advances instead of cramming ever more points into
 * one frame, and dragging pans through history (all charts move together;
 * the wheel zooms). A gesture reports where it landed and the server is asked
 * for that window; the chart never invents data it was not given.
 *
 * ECharts is imported by module rather than as the whole library: four line
 * charts need the line chart, the axes, the tooltip, dataZoom and the canvas
 * renderer, and nothing else.
 */

import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  DataZoomInsideComponent,
  GridComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomInsideComponent,
  CanvasRenderer,
]);

const GROUP = "console-metrics";

export interface ChartWindow {
  start: number;
  end: number;
  step: number;
}

export interface ChartLine {
  name: string;
  color: string;
  values: number[];
}

function option(format: (value: number) => string) {
  return {
    animation: false,
    grid: { left: 44, right: 12, top: 10, bottom: 22 },
    tooltip: {
      trigger: "axis" as const,
      backgroundColor: "#141922",
      borderColor: "#28303d",
      textStyle: { color: "#e8ecf3", fontSize: 12 },
      valueFormatter: format,
    },
    xAxis: {
      type: "time" as const,
      axisLine: { lineStyle: { color: "#28303d" } },
      axisLabel: { color: "#5a6474", fontSize: 10.5, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value" as const,
      scale: false,
      min: 0,
      axisLabel: { color: "#5a6474", fontSize: 10.5 },
      splitLine: { lineStyle: { color: "rgba(255,255,255,.06)" } },
    },
    dataZoom: [
      {
        type: "inside" as const,
        filterMode: "none" as const,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
      },
    ],
    series: [] as object[],
  };
}

export function Chart({
  lines,
  window: win,
  format,
  onPan,
  onSelect,
}: {
  /** One line per job, all on the shared window grid. */
  lines: ChartLine[];
  window: ChartWindow | null;
  format: (value: number) => string;
  /** A drag/zoom gesture, as percentages of the shown window. */
  onPan?: (startPct: number, endPct: number) => void;
  /** A shift-drag selection, in absolute seconds. */
  onSelect?: (startSec: number, endSec: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const panRef = useRef(onPan);
  panRef.current = onPan;
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  // The selection rectangle while a shift-drag is in flight, in host px.
  const [band, setBand] = useState<{ x0: number; x1: number } | null>(null);

  // Shift-drag selects a time range, the way a monitoring tool zooms in.
  // Captured on the host before echarts sees it, so the pan gesture the
  // chart would otherwise perform never starts.
  const beginSelect = (down: React.PointerEvent) => {
    if (!down.shiftKey || !host.current || !chart.current) return;
    down.preventDefault();
    down.stopPropagation();
    const rect = host.current.getBoundingClientRect();
    const origin = down.clientX - rect.left;
    setBand({ x0: origin, x1: origin });
    const move = (event: PointerEvent) =>
      setBand({ x0: origin, x1: event.clientX - rect.left });
    const up = (event: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      setBand(null);
      const landed = event.clientX - rect.left;
      if (Math.abs(landed - origin) < 5 || !chart.current) return;
      const toSec = (x: number): number | null => {
        const point = chart.current!.convertFromPixel({ gridIndex: 0 }, [x, 0]);
        return point ? Math.round(point[0]! / 1000) : null;
      };
      const a = toSec(Math.min(origin, landed));
      const b = toSec(Math.max(origin, landed));
      if (a !== null && b !== null && b > a) selectRef.current?.(a, b);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up, { once: true });
  };

  useEffect(() => {
    if (!host.current) return;
    const instance = echarts.init(host.current);
    instance.setOption(option(format));
    // One group, so dragging any chart pans all of them together.
    instance.group = GROUP;
    echarts.connect(GROUP);
    instance.on("datazoom", (params: unknown) => {
      const event = params as { batch?: Array<{ start?: number; end?: number }>; start?: number; end?: number };
      const gesture = event.batch?.[0] ?? event;
      if (gesture.start == null || gesture.end == null) return;
      panRef.current?.(gesture.start, gesture.end);
    });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
    // Colour and formatter are fixed per card.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!chart.current || !win) return;
    chart.current.setOption(
      {
        xAxis: { min: win.start * 1000, max: win.end * 1000 },
        // The fetched data covers exactly the axis again; any leftover
        // gesture offset would double-apply it.
        dataZoom: [{ start: 0, end: 100 }],
        series: lines.map((line) => ({
          type: "line" as const,
          name: line.name,
          showSymbol: false,
          lineStyle: { color: line.color, width: 1.6 },
          itemStyle: { color: line.color },
          areaStyle: lines.length === 1 ? { color: line.color, opacity: 0.08 } : undefined,
          data: line.values.map((value, index) => [
            (win.start + index * win.step) * 1000,
            value,
          ]),
        })),
      },
      { replaceMerge: ["series"] }, // a job leaving the window takes its line with it
    );
  }, [lines, win]);

  return (
    <div
      ref={host}
      onPointerDownCapture={beginSelect}
      className="absolute inset-0"
    >
      {band ? (
        <div
          className="pointer-events-none absolute inset-y-2 z-10 border-x border-blue/60 bg-blue/15"
          style={{
            left: Math.min(band.x0, band.x1),
            width: Math.abs(band.x1 - band.x0),
          }}
        />
      ) : null}
    </div>
  );
}
