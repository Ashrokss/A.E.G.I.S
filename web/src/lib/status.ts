import type { RunStatus } from "./types";

export type Tone = "teal" | "amber" | "rust" | "muted";

export const STATUS: Record<RunStatus, { label: string; tone: Tone; working: boolean }> = {
  PENDING_INTAKE: { label: "Waiting to start", tone: "muted", working: false },
  IDLE: { label: "Reading your request", tone: "teal", working: true },
  AWAITING_INPUT: { label: "Waiting for you", tone: "amber", working: false },
  PLANNING: { label: "Planning", tone: "teal", working: true },
  AWAITING_APPROVAL: { label: "Plan ready for review", tone: "amber", working: false },
  APPLYING: { label: "Applying", tone: "teal", working: true },
  REPAIRING: { label: "Fixing the configuration", tone: "teal", working: true },
  CHECKING_DRIFT: { label: "Checking for drift", tone: "teal", working: true },
  COMPLETED: { label: "Applied", tone: "muted", working: false },
  FAILED: { label: "Failed", tone: "rust", working: false },
};

/** The run lifecycle as a sequence (mirrors the agent's phase map). */
export const PHASES = ["Understand", "Clarify", "Plan", "Review", "Apply", "Done"] as const;

export function phaseIndex(status: RunStatus): number {
  switch (status) {
    case "PENDING_INTAKE":
    case "IDLE":
      return 0;
    case "AWAITING_INPUT":
      return 1;
    case "PLANNING":
    case "REPAIRING":
      return 2;
    case "AWAITING_APPROVAL":
      return 3;
    case "APPLYING":
      return 4;
    default:
      return 5;
  }
}
