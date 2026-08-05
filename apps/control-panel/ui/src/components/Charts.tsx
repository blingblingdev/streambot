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

import { useEffect, useRef } from "react";
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

function option(color: string, format: (value: number) => string) {
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
    series: [
      {
        type: "line" as const,
        showSymbol: false,
        lineStyle: { color, width: 1.6 },
        itemStyle: { color },
        areaStyle: { color, opacity: 0.08 },
        data: [] as [number, number][],
      },
    ],
  };
}

export function Chart({
  values,
  window: win,
  color,
  format,
  onPan,
}: {
  values: number[];
  window: ChartWindow | null;
  color: string;
  format: (value: number) => string;
  /** A drag/zoom gesture, as percentages of the shown window. */
  onPan?: (startPct: number, endPct: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const panRef = useRef(onPan);
  panRef.current = onPan;

  useEffect(() => {
    if (!host.current) return;
    const instance = echarts.init(host.current);
    instance.setOption(option(color, format));
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
    chart.current.setOption({
      xAxis: { min: win.start * 1000, max: win.end * 1000 },
      // The fetched data covers exactly the axis again; any leftover gesture
      // offset would double-apply it.
      dataZoom: [{ start: 0, end: 100 }],
      series: [
        {
          data: values.map((value, index) => [
            (win.start + index * win.step) * 1000,
            value,
          ]),
        },
      ],
    });
  }, [values, win]);

  return <div ref={host} className="absolute inset-0" />;
}
