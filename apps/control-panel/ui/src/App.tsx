import { isShotMode, useEventStream } from "./api";

export function App() {
  const { payload, connected } = useEventStream(!isShotMode());
  const running = payload?.jobs.filter((job) => job.running) ?? [];
  return (
    <div className="p-6 font-mono text-sm">
      <div className="text-muted">scaffold</div>
      <div>situation: {payload?.status.situation ?? "—"}</div>
      <div>stream: {connected ? "connected" : "waiting"}</div>
      <div>jobs: {payload?.jobs.length ?? 0}</div>
      <div>running: {running.map((job) => job.name).join(", ") || "none"}</div>
    </div>
  );
}
