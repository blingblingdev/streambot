/**
 * The session's shape over time.
 *
 * ECharts is imported by module rather than as the whole library: four line
 * charts need the line chart, the two axes, the tooltip and the canvas
 * renderer, and nothing else. That is the difference between a megabyte and a
 * couple of hundred kilobytes.
 */

import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

import type { Point } from "../types";

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

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
      scale: true,
      axisLabel: { color: "#5a6474", fontSize: 10.5 },
      splitLine: { lineStyle: { color: "rgba(255,255,255,.06)" } },
    },
    series: [
      {
        type: "line" as const,
        showSymbol: false,
        sampling: "lttb" as const,
        lineStyle: { color, width: 1.6 },
        itemStyle: { color },
        areaStyle: { color, opacity: 0.08 },
        data: [] as [number, number][],
      },
    ],
  };
}

export function Chart({
  points,
  color,
  format,
}: {
  points: Point[];
  color: string;
  format: (value: number) => string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const instance = echarts.init(host.current);
    instance.setOption(option(color, format));
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
    // The colour and formatter are fixed per card; re-creating on every render
    // would throw away the chart's own animation state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chart.current?.setOption({
      // Seconds on the wire, milliseconds on a time axis.
      series: [{ data: points.map(([t, value]) => [t * 1000, value]) }],
    });
  }, [points]);

  return <div ref={host} className="absolute inset-0" />;
}
