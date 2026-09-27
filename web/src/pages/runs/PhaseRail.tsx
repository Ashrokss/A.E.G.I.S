import { Check } from "lucide-react";
import type { RunStatus } from "../../lib/types";
import { PHASES, phaseIndex } from "../../lib/status";
import { cn } from "../../lib/format";

/** The run lifecycle. A real sequence, so it is numbered. */
export default function PhaseRail({ status }: { status: RunStatus }) {
  const current = phaseIndex(status);
  const failed = status === "FAILED";
  return (
    <ol className="no-scrollbar flex items-center gap-1 overflow-x-auto text-xs" aria-label="Run progress">
      {PHASES.map((p, i) => {
        const done = i < current || (i === current && status === "COMPLETED");
        const active = i === current && !done;
        return (
          <li key={p} className="flex items-center gap-1" aria-current={active ? "step" : undefined}>
            {i > 0 && <span className={cn("h-px w-4 sm:w-8", i <= current ? "bg-teal" : "bg-line")} aria-hidden />}
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 whitespace-nowrap",
                active && !failed && "bg-teal/15 text-teal",
                active && failed && "bg-rust/15 text-rust",
                done && "text-fg",
                !active && !done && "text-muted",
              )}
            >
              <span
                className={cn(
                  "grid size-4 place-items-center rounded-full border text-[10px]",
                  done ? "border-teal bg-teal text-on-accent" : active ? "border-current" : "border-line",
                )}
              >
                {done ? <Check className="size-3" aria-hidden /> : i + 1}
              </span>
              {p}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
