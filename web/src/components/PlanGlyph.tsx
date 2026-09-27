import { cn } from "../lib/format";

/** Terraform's own action symbols, the signature of the whole UI. */
export type Action = "create" | "update" | "delete" | "replace" | "read" | "no-op";

export function actionOf(actions: string[]): Action {
  const a = new Set(actions);
  if (a.has("delete") && a.has("create")) return "replace";
  if (a.has("create")) return "create";
  if (a.has("delete")) return "delete";
  if (a.has("update")) return "update";
  if (a.has("read")) return "read";
  return "no-op";
}

const GLYPH: Record<Action, { symbol: string; className: string; label: string }> = {
  create: { symbol: "+", className: "text-teal", label: "create" },
  update: { symbol: "~", className: "text-amber", label: "update in place" },
  delete: { symbol: "−", className: "text-rust", label: "destroy" },
  replace: { symbol: "±", className: "text-rust", label: "destroy and re-create" },
  read: { symbol: "≤", className: "text-muted", label: "read" },
  "no-op": { symbol: "·", className: "text-muted", label: "no change" },
};

export default function PlanGlyph({ action, className }: { action: Action; className?: string }) {
  const g = GLYPH[action];
  return (
    <span className={cn("font-mono font-medium", g.className, className)} title={g.label} aria-label={g.label}>
      {g.symbol}
    </span>
  );
}

/** "+3 ~1 −0" in the same glyph language. */
export function PlanCounts({
  counts,
  className,
}: {
  counts: { add: number; change: number; destroy: number; replace: number };
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-baseline gap-3 font-mono", className)}>
      <span className="text-teal">+{counts.add}</span>
      <span className="text-amber">~{counts.change}</span>
      <span className={counts.destroy ? "text-rust" : "text-muted"}>−{counts.destroy}</span>
      {counts.replace > 0 && <span className="text-rust">±{counts.replace}</span>}
    </span>
  );
}
