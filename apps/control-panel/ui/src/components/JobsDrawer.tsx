import type { JobRow } from "../types";
import { api } from "../api";
import { Button, Led } from "./ui";

export function JobsDrawer({
  open,
  jobs,
  onClose,
  onSettings,
  onToast,
}: {
  open: boolean;
  jobs: JobRow[];
  onClose: () => void;
  onSettings: (job: JobRow) => void;
  onToast: (message: string) => void;
}) {
  async function toggle(job: JobRow) {
    const result = job.running ? await api.stopJob(job.name) : await api.startJob(job.name);
    onToast(
      result.ok === true
        ? job.running
          ? `Stopped ${job.name}`
          : `Started ${job.name}`
        : `Action failed: ${result.error}`,
    );
  }

  return (
    <>
      {/* Full height: the header can wrap to two rows on a narrow screen, so
          nothing may assume how tall it is. */}
      <div
        onClick={onClose}
        className={
          `fixed inset-0 z-25 bg-black/40 transition-opacity ` +
          (open ? "opacity-100" : "pointer-events-none opacity-0")
        }
      />
      <aside
        className={
          `fixed inset-y-0 right-0 z-30 flex w-[min(460px,92vw)] flex-col ` +
          `border-l border-line bg-panel transition-transform duration-200 ` +
          (open ? "translate-x-0" : "translate-x-full")
        }
      >
        <div className="flex items-center gap-2 border-b border-line px-4 py-3 text-[12.5px]">
          All jobs
          <div className="flex-1" />
          <Button small onClick={onClose}>
            Close
          </Button>
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4">
          {jobs.length === 0 ? (
            <div className="py-4 text-[13px] text-faint">No jobs found.</div>
          ) : (
            jobs.map((job) => (
              <div
                key={job.name}
                className="flex items-center gap-3 border-t border-line py-3 first:border-t-0"
              >
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{job.title || job.name}</div>
                  <div className="truncate text-[12px] text-muted">{job.description}</div>
                </div>
                <div className="flex items-center gap-1.5 font-mono text-[12px] text-muted">
                  <Led grade={job.running ? "ok" : ""} />
                  {job.running ? "Running" : "Stopped"}
                </div>
                {job.configurable ? (
                  <Button small onClick={() => onSettings(job)}>
                    Settings
                  </Button>
                ) : null}
                <Button
                  small
                  kind={job.running ? "danger" : "primary"}
                  onClick={() => toggle(job)}
                >
                  {job.running ? "Stop" : "Start"}
                </Button>
              </div>
            ))
          )}
        </div>
      </aside>
    </>
  );
}
