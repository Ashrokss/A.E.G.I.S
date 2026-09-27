import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { request, streamEvents } from "../../lib/api";
import type { PlanSummary, RunSnapshot } from "../../lib/types";
import { cn, tryJson } from "../../lib/format";
import PlanGlyph, { actionOf, PlanCounts } from "../../components/PlanGlyph";
import { Empty } from "../../components/ui";

export type InspectorTab = "plan" | "files" | "logs";

export default function Inspector({
  runId,
  snapshot,
  plan,
  tab,
  onTab,
  working,
}: {
  runId: string;
  snapshot?: RunSnapshot;
  plan: PlanSummary | null;
  tab: InspectorTab;
  onTab: (t: InspectorTab) => void;
  working: boolean;
}) {
  const tabs: Array<[InspectorTab, string]> = [
    ["plan", "Plan"],
    ["files", "Files"],
    ["logs", "Logs"],
  ];
  return (
    <div className="flex h-full flex-col">
      <div role="tablist" className="flex shrink-0 gap-1 border-b border-line px-3">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => onTab(id)}
            className={cn(
              "relative h-11 px-3 text-sm",
              tab === id ? "text-fg after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-teal" : "text-muted hover:text-fg",
            )}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "plan" && <PlanTab snapshot={snapshot} plan={plan} />}
        {tab === "files" && <FilesTab runId={runId} working={working} />}
        {tab === "logs" && <LogsTab runId={runId} working={working} />}
      </div>
    </div>
  );
}

// ── Plan: the ledger ──────────────────────────────────────────────────────────

type TfStep = { step: string; status: "running" | "succeeded" | "failed"; detail?: string };

function PlanTab({ snapshot, plan }: { snapshot?: RunSnapshot; plan: PlanSummary | null }) {
  const { steps, raw } = useMemo(() => {
    const byStep = new Map<string, TfStep>();
    let raw: string | null = null;
    for (const m of snapshot?.messages ?? []) {
      if (m.type === "tf_step") {
        const s = tryJson<TfStep>(m.content);
        if (s) {
          if (s.step === "init") byStep.clear();
          byStep.set(s.step, s);
        }
      }
      if (m.type === "plan_preview") raw = m.content.replace(/^Plan results:\n?/, "");
    }
    return { steps: [...byStep.values()], raw };
  }, [snapshot]);

  const order: Record<string, number> = { create: 0, replace: 1, update: 2, delete: 3 };
  const changes = [...(plan?.changes ?? [])]
    .map((c) => ({ ...c, action: actionOf(c.actions) }))
    .filter((c) => c.action !== "no-op" && c.action !== "read")
    .sort((a, b) => (order[a.action] ?? 9) - (order[b.action] ?? 9) || (a.address < b.address ? -1 : 1));

  return (
    <div className="grid gap-5 p-4">
      {steps.length > 0 && (
        <ol className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {steps.map((s) => (
            <li
              key={s.step}
              className={cn(
                "inline-flex items-center gap-1.5",
                s.status === "failed" ? "text-rust" : s.status === "running" ? "text-teal" : "text-muted",
              )}
            >
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  s.status === "failed" ? "bg-rust" : s.status === "running" ? "animate-pulse bg-teal" : "bg-teal",
                )}
                aria-hidden
              />
              terraform {s.step}
              {s.detail === "destroy" && " (destroy)"}
            </li>
          ))}
        </ol>
      )}

      {!plan ? (
        <Empty title="No plan yet">AEGIS runs terraform plan once the configuration is written. It appears here.</Empty>
      ) : (
        <>
          <div className="flex items-baseline justify-between gap-3">
            <p className="font-medium">
              {changes.length} {changes.length === 1 ? "change" : "changes"}
            </p>
            <PlanCounts counts={plan.counts} className="text-sm" />
          </div>
          {changes.length === 0 ? (
            <p className="text-sm text-muted">No changes. Your infrastructure matches the configuration.</p>
          ) : (
            <ul className="grid">
              {changes.map((c) => (
                <li
                  key={c.address}
                  className={cn(
                    "grid grid-cols-[2rem_minmax(0,1fr)] items-baseline border-b border-line py-2.5 last:border-0",
                    (c.action === "delete" || c.action === "replace") && "bg-rust/5",
                  )}
                >
                  <PlanGlyph action={c.action} className="pl-1.5 text-[22px] leading-none font-bold" />
                  <span className={cn("font-mono text-[13px] break-all", c.action === "delete" && "text-rust")}>
                    {c.address}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {raw && (
            <details className="group">
              <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-sm text-muted hover:text-fg">
                <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
                Full terraform output
              </summary>
              <pre className="mt-2 overflow-x-auto rounded-md border border-line bg-bg p-3 font-mono text-xs leading-relaxed whitespace-pre">
                {raw}
              </pre>
            </details>
          )}
        </>
      )}
    </div>
  );
}

// ── Files ─────────────────────────────────────────────────────────────────────

function FilesTab({ runId, working }: { runId: string; working: boolean }) {
  const base = `/agent-api/runs/${encodeURIComponent(runId)}`;
  const [selected, setSelected] = useState<string | null>(null);
  const files = useQuery({
    queryKey: ["run-files", runId],
    queryFn: () => request<{ files: Array<{ name: string; size: number }> }>(`${base}/files`).then((r) => r.files),
    refetchInterval: working ? 3_000 : false,
  });
  const current = selected ?? files.data?.[0]?.name ?? null;
  const content = useQuery({
    queryKey: ["run-file", runId, current],
    enabled: !!current,
    queryFn: () =>
      request<{ content: string }>(`${base}/files/${current!.split("/").map(encodeURIComponent).join("/")}`).then((r) => r.content),
    refetchInterval: working ? 3_000 : false,
  });

  if (files.data?.length === 0) {
    return (
      <div className="p-4">
        <Empty title="No files yet">The Terraform AEGIS writes shows up here as it goes.</Empty>
      </div>
    );
  }
  return (
    <div className="grid">
      <ul className="flex flex-wrap gap-1 border-b border-line p-2">
        {files.data?.map((f) => (
          <li key={f.name}>
            <button
              onClick={() => setSelected(f.name)}
              className={cn(
                "rounded px-2 py-1 font-mono text-xs",
                f.name === current ? "bg-raised text-fg" : "text-muted hover:text-fg",
              )}
            >
              {f.name}
            </button>
          </li>
        ))}
      </ul>
      {content.data !== undefined && (
        <pre className="overflow-x-auto p-4 font-mono text-[12.5px] leading-relaxed">
          {content.data.split("\n").map((line, i) => (
            <div key={i} className="grid grid-cols-[2.5rem_1fr]">
              <span className="pr-3 text-right text-muted/60 select-none">{i + 1}</span>
              <span className="whitespace-pre">{line || " "}</span>
            </div>
          ))}
        </pre>
      )}
    </div>
  );
}

// ── Logs: authenticated SSE ────────────────────────────────────────────────────

function LogsTab({ runId, working }: { runId: string; working: boolean }) {
  const [lines, setLines] = useState<string[]>([]);
  const boxRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    let buffer: string[] = [];
    const flush = setInterval(() => {
      if (!buffer.length) return;
      const chunk = buffer;
      buffer = [];
      setLines((prev) => [...prev, ...chunk].slice(-1000));
    }, 200);
    (async () => {
      // The agent closes the stream (DONE) after each plan/apply; reconnect.
      while (!ctrl.signal.aborted) {
        try {
          await streamEvents(`/agent-api/runs/${encodeURIComponent(runId)}/stream`, (d) => {
            if (d !== "DONE") buffer.push(d);
          }, ctrl.signal);
        } catch {
          /* network blip or aborted */
        }
        await new Promise((r) => setTimeout(r, 2_000));
      }
    })();
    return () => {
      ctrl.abort();
      clearInterval(flush);
    };
  }, [runId]);

  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  if (!lines.length) {
    return (
      <div className="p-4">
        <Empty title={working ? "Waiting for output" : "No output in this session"}>
          Terraform and agent output streams here live while the run is working.
        </Empty>
      </div>
    );
  }
  return (
    <pre ref={boxRef} className="h-full overflow-auto p-4 font-mono text-xs leading-relaxed">
      {lines.map((l, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap break-all",
            /^(ERROR|STDERR)/.test(l) ? "text-rust" : /^WARN/.test(l) ? "text-amber" : /^EXIT/.test(l) ? "text-teal" : "text-fg/80",
          )}
        >
          {l.replace(/^(STDOUT|INFO): ?/, "")}
        </div>
      ))}
    </pre>
  );
}
