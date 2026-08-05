import { describe, expect, test } from "bun:test";

import {
  FEED_MAX_ROWS,
  hasTiming,
  mergeEvents,
  phases,
  placePopover,
  totalMs,
} from "./timeline";
import { freshness, fmtUptime, msGrade, scoreGrade } from "./format";
import type { FlowEvent } from "../types";

const look = (i: number, over: Partial<FlowEvent> = {}): FlowEvent => ({
  i,
  event: "perceive",
  t: 1785897100 + i,
  perceive_ms: 59,
  classify_ms: 46.5,
  resolve_ms: 4.1,
  screen: "workshop-lobby",
  ...over,
});

describe("phases", () => {
  test("what is left after the worker's own work is transport", () => {
    const parts = phases(look(1));
    expect(parts.map((p) => p.key)).toEqual(["tp", "cl", "lo"]);
    expect(parts[0]!.ms).toBeCloseTo(59 - 46.5 - 4.1, 5);
  });

  test("a click adds its own segment and lands in the total", () => {
    const click = look(2, { event: "click", perceive_ms: 68.5, act_ms: 228.7 });
    expect(phases(click).map((p) => p.key)).toEqual(["tp", "cl", "lo", "ac"]);
    expect(totalMs(click)).toBeCloseTo(297.2, 5);
  });

  test("a clock that disagrees with itself never draws a negative segment", () => {
    const skewed = look(3, { perceive_ms: 10, classify_ms: 40, resolve_ms: 5 });
    expect(phases(skewed)[0]!.ms).toBe(0);
  });

  test("an untimed event has no bar", () => {
    const doing: FlowEvent = { i: 4, event: "doing", t: 1, what: "idling" };
    expect(hasTiming(doing)).toBe(false);
    expect(phases(doing)).toEqual([]);
    expect(totalMs(doing)).toBeNull();
  });
});

describe("mergeEvents", () => {
  test("appends only what is newer than what is held", () => {
    const held = [look(1), look(2)];
    expect(mergeEvents(held, [look(2), look(3)]).map((e) => e.i)).toEqual([1, 2, 3]);
  });

  test("re-sending the same tick changes nothing", () => {
    const held = [look(1), look(2)];
    expect(mergeEvents(held, [look(1), look(2)])).toBe(held);
  });

  test("stays bounded, dropping from the front", () => {
    let held: FlowEvent[] = [];
    for (let i = 1; i <= FEED_MAX_ROWS + 40; i++) held = mergeEvents(held, [look(i)]);
    expect(held.length).toBe(FEED_MAX_ROWS);
    expect(held[0]!.i).toBe(41);
    expect(held[held.length - 1]!.i).toBe(FEED_MAX_ROWS + 40);
  });

  test("a restarted job renumbers from the start, so the feed starts over", () => {
    const held = [look(80), look(81)];
    expect(mergeEvents(held, [look(1), look(2)]).map((e) => e.i)).toEqual([1, 2]);
  });
});

describe("placePopover", () => {
  const size = { width: 240, height: 130 };
  const viewport = { width: 1440, height: 900 };

  test("sits beside the pointer", () => {
    expect(placePopover({ clientX: 200, clientY: 400 }, size, viewport)).toEqual({
      left: 214,
      top: 390,
    });
  });

  test("flips to the other side rather than leaving the screen", () => {
    const at = placePopover({ clientX: 1400, clientY: 400 }, size, viewport);
    expect(at.left).toBe(1400 - size.width - 14);
  });

  test("lifts off the bottom edge", () => {
    const at = placePopover({ clientX: 100, clientY: 880 }, size, viewport);
    expect(at.top).toBe(viewport.height - size.height - 8);
  });

  test("never goes above or left of the margin", () => {
    const at = placePopover({ clientX: 2, clientY: 2 }, size, { width: 300, height: 200 });
    expect(at.left).toBeGreaterThanOrEqual(8);
    expect(at.top).toBeGreaterThanOrEqual(8);
  });
});

describe("panWindow", () => {
  test("a drag left lands on the earlier window it points at", async () => {
    const { panWindow } = await import("./timerange");
    const now = 100_000;
    // Shown 90000..93600 (1h); dragged half a window back.
    const landed = panWindow(90_000, 93_600, -50, 50, now);
    expect(landed).toEqual({ start: 88_200, end: 91_800, live: false });
  });

  test("a wheel zoom-in narrows onto the middle", async () => {
    const { panWindow } = await import("./timerange");
    const landed = panWindow(90_000, 93_600, 25, 75, 100_000);
    expect(landed.end - landed.start).toBe(1800);
    expect(landed.live).toBe(false);
  });

  test("panning into the future clamps to now and reads as live", async () => {
    const { panWindow } = await import("./timerange");
    const now = 100_000;
    const landed = panWindow(now - 3600, now, 50, 150, now);
    expect(landed.end).toBe(now);
    expect(landed.live).toBe(true);
    expect(landed.end - landed.start).toBe(3600);
  });

  test("cannot pan past the retention horizon or below a minute", async () => {
    const { panWindow, MAX_RANGE_SECONDS } = await import("./timerange");
    const now = 100_000_000;
    const ancient = panWindow(now - 3600, now, -1e9, -1e9 + 100, now);
    expect(ancient.start).toBeGreaterThanOrEqual(now - MAX_RANGE_SECONDS);
    const tiny = panWindow(now - 3600, now - 1800, 50, 50.001, now);
    expect(tiny.end - tiny.start).toBeGreaterThanOrEqual(60);
  });
});

describe("rail width", () => {
  test("clamps to its bounds and rejects nonsense", async () => {
    const { clampRailWidth, RAIL_DEFAULT, RAIL_MAX, RAIL_MIN } = await import("./rail");
    expect(clampRailWidth(400)).toBe(400);
    expect(clampRailWidth(50)).toBe(RAIL_MIN);
    expect(clampRailWidth(5000)).toBe(RAIL_MAX);
    expect(clampRailWidth(Number.NaN)).toBe(RAIL_DEFAULT);
  });
});

describe("formatters", () => {
  test("uptime reads in the largest useful unit", () => {
    expect(fmtUptime(null)).toBe("—");
    expect(fmtUptime(45)).toBe("45s");
    expect(fmtUptime(605)).toBe("10m 5s");
    expect(fmtUptime(7325)).toBe("2h 2m");
  });

  test("latency grades on the click budget", () => {
    expect(msGrade(120, 300, 600)).toBe("ok");
    expect(msGrade(450, 300, 600)).toBe("warn");
    expect(msGrade(900, 300, 600)).toBe("bad");
  });

  test("confidence grades the other way round", () => {
    expect(scoreGrade(0.99)).toBe("ok");
    expect(scoreGrade(0.85)).toBe("warn");
    expect(scoreGrade(0.5)).toBe("bad");
    expect(scoreGrade(null)).toBe("");
  });

  test("frame freshness says live, late, or lagging", () => {
    expect(freshness(80).text).toBe("Live");
    expect(freshness(3000).grade).toBe("warn");
    expect(freshness(9000).text).toBe("Lagging 9.0 s");
    expect(freshness(null).text).toBe("—");
  });
});
