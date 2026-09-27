import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { request, streamEvents } from "@/lib/api";
import type { PlanSummary, RunSnapshot } from "@/lib/types";
import { tryJson } from "@/lib/format";
import { cn } from "@/lib/utils";
import PlanGlyph, { actionOf, PlanCounts } from "@/components/PlanGlyph";
import { Empty, Panel } from "@/components/ui";
import AITaskList, { type AITaskStatus } from "@/components/smoothui/ai-task-list";
import AIDiff, { type AIDiffLine } from "@/components/smoothui/ai-diff";
import AIArtifact from "@/components/smoothui/ai-artifact";

type TfStep = { step: string; status: "running" | "succeeded" | "failed"; detail?: string };

const TASK_STATUS: Record<TfStep["status"], AITaskStatus> = { running: "running", succeeded: "done", failed: "failed" };

/** Plan output as a diff: `+` creates, `-` and `-/+` destroys, everything else context. */
function diffLines(raw: string): AIDiffLine[] {
  return raw.split("\n").map((content) => {
    const t = content.trimStart();
    return { content, kind: t.startsWith("+") ? "added" : t.startsWith("-") ? "removed" : "context" };
  });
}

/** The resources and data sources a .tf file declares, for the artifact's preview pane. */
function declared(code: string) {
  return [...code.matchAll(/^\s*(resource|data|module|variable|output)\s+"([^"]+)"(?:\s+"([^"]+)")?/gm)].map((m) => ({
    kind: m[1],
    address: m[3] ? `${m[2]}.${m[3]}` : m[2],
  }));
}

export function PlanView({ snapshot, plan }: { snapshot?: RunSnapshot; plan: PlanSummary | null }) {
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
    <div className="grid gap-5">
      {steps.length > 0 && (
        <AITaskList
          label="Terraform"
          tasks={steps.map((s) => ({
            id: s.step,
            label: `terraform ${s.step}${s.detail === "destroy" ? " (destroy)" : ""}`,
            status: TASK_STATUS[s.status],
          }))}
        />
      )}

      {!plan ? (
        <Empty title="No plan yet">AEGIS runs terraform plan once the configuration is written. It shows up here.</Empty>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-lg font-semibold">
              {changes.length} {changes.length === 1 ? "change" : "changes"}
            </h2>
            <PlanCounts counts={plan.counts} className="text-sm" />
          </div>
          {changes.length === 0 ? (
            <p className="text-sm text-muted">No changes. The infrastructure already matches the configuration.</p>
          ) : (
            <Panel>
              <ul className="divide-y divide-line">
                {changes.map((c) => (
                  <li
                    key={c.address}
                    className={cn(
                      "grid grid-cols-[1.5rem_minmax(0,1fr)] items-baseline gap-3 px-5 py-3.5",
                      (c.action === "delete" || c.action === "replace") && "bg-rust/[0.05]",
                    )}
                  >
                    <PlanGlyph action={c.action} className="text-xl leading-none font-bold" />
                    <span className={cn("font-mono text-[13px] break-all", c.action === "delete" && "text-rust")}>{c.address}</span>
                  </li>
                ))}
              </ul>
            </Panel>
          )}
          {raw && (
            <details className="group">
              <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-sm text-muted hover:text-fg">
                <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
                Full terraform output
              </summary>
              <AIDiff className="mt-3" title="terraform plan" lines={diffLines(raw)} />
            </details>
          )}
        </>
      )}
    </div>
  );
}

export function FilesView({ runId, working }: { runId: string; working: boolean }) {
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
    queryFn: () => request<{ content: string }>(`${base}/files/${current!.split("/").map(encodeURIComponent).join("/")}`).then((r) => r.content),
    refetchInterval: working ? 3_000 : false,
  });

  if (files.data?.length === 0) {
    return <Empty title="No files yet">The Terraform AEGIS writes appears here as it goes.</Empty>;
  }
  return (
    <div className="grid gap-3">
      <ul className="no-scrollbar -mx-1 flex gap-1.5 overflow-x-auto px-1">
        {files.data?.map((f) => (
          <li key={f.name} className="shrink-0">
            <button
              onClick={() => setSelected(f.name)}
              className={cn(
                "rounded-full border px-3 py-1.5 font-mono text-xs transition-colors",
                f.name === current ? "border-teal/40 bg-teal/10 text-fg" : "border-line text-muted hover:text-fg",
              )}
            >
              {f.name.split("/").pop()}
            </button>
          </li>
        ))}
      </ul>
      {current && content.data !== undefined && (
        <AIArtifact
          key={current}
          title={current}
          copyText={content.data}
          defaultPane="code"
          preview={<Declared code={content.data} />}
          code={content.data.split("\n").map((line, i) => (
            <div key={i} className="grid grid-cols-[2.25rem_1fr]">
              <span className="pr-3 text-right text-muted/50 select-none">{i + 1}</span>
              <span className="whitespace-pre">{line || " "}</span>
            </div>
          ))}
        />
      )}
    </div>
  );
}

function Declared({ code }: { code: string }) {
  const items = declared(code);
  if (!items.length) return <p className="text-sm text-muted">Nothing declared in this file.</p>;
  return (
    <ul className="grid gap-1.5">
      {items.map((d, i) => (
        <li key={i} className="flex items-baseline gap-3 text-sm">
          <span className="w-16 shrink-0 text-xs text-muted">{d.kind}</span>
          <span className="font-mono text-[13px] break-all">{d.address}</span>
        </li>
      ))}
    </ul>
  );
}

export function LogsView({ runId, working }: { runId: string; working: boolean }) {
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
      <Empty title={working ? "Waiting for output" : "No output yet"}>
        Terraform and agent output streams here live while the run is working.
      </Empty>
    );
  }
  return (
    <Panel>
      <pre ref={boxRef} className="max-h-[60dvh] overflow-auto p-4 font-mono text-xs leading-relaxed">
        {lines.map((l, i) => (
          <div
            key={i}
            className={cn(
              "break-all whitespace-pre-wrap",
              /^(ERROR|STDERR)/.test(l) ? "text-rust" : /^WARN/.test(l) ? "text-amber" : /^EXIT/.test(l) ? "text-teal" : "text-fg/75",
            )}
          >
            {l.replace(/^(STDOUT|INFO): ?/, "")}
          </div>
        ))}
      </pre>
    </Panel>
  );
}
