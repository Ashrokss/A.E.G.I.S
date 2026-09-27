import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, GitPullRequest, Square } from "lucide-react";
import { Link } from "react-router-dom";
import { download, request } from "../../lib/api";
import type { PlanSummary, RunSnapshot } from "../../lib/types";
import { STATUS } from "../../lib/status";
import { cn, tryJson } from "../../lib/format";
import { Button, Dot, ErrorNote, StatusText } from "../../components/ui";
import { PlanCounts } from "../../components/PlanGlyph";
import PhaseRail from "./PhaseRail";
import Conversation from "./Conversation";
import Inspector, { type InspectorTab } from "./Inspector";

export default function Mission({ runId }: { runId: string }) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<InspectorTab>("plan");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const base = `/agent-api/runs/${encodeURIComponent(runId)}`;

  const snap = useQuery({
    queryKey: ["run", runId],
    queryFn: () => request<RunSnapshot>(`${base}/snapshot`),
    refetchInterval: (q) => {
      const s = q.state.data?.run.status;
      return s && STATUS[s]?.working ? 1_200 : 5_000;
    },
  });

  const status = snap.data?.run.status ?? "IDLE";
  const meta = STATUS[status] ?? STATUS.IDLE;

  const plan = useMemo(() => {
    const msgs = snap.data?.messages ?? [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].type === "plan_summary") return tryJson<PlanSummary>(msgs[i].content);
    }
    return null;
  }, [snap.data]);

  // When a plan lands, bring the inspector to it.
  const lastPlanRef = useRef<PlanSummary | null>(null);
  useEffect(() => {
    if (plan && plan !== lastPlanRef.current && lastPlanRef.current !== null) setTab("plan");
    lastPlanRef.current = plan;
  }, [plan]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["run", runId] });
    qc.invalidateQueries({ queryKey: ["runs"] });
  };
  const act = useMutation({
    mutationFn: (action: "approve" | "discard" | "stop") => request(`${base}/${action}`, { method: "POST" }),
    onSuccess: refresh,
  });
  const push = useMutation({
    mutationFn: () => request<{ url: string }>(`${base}/push-to-github`, { body: {} }),
    onSuccess: refresh,
  });
  const send = useMutation({
    mutationFn: (content: string) => request(`${base}/messages`, { body: { role: "user", content, type: "text" } }),
    onSuccess: refresh,
  });

  if (snap.isError) {
    return (
      <div className="p-8">
        <ErrorNote error={snap.error} />
      </div>
    );
  }

  const awaitingApproval = status === "AWAITING_APPROVAL" && !!plan;
  const destroys = (plan?.counts.destroy ?? 0) + (plan?.counts.replace ?? 0);

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="grid gap-3 border-b border-line px-5 py-4">
          <div className="flex items-start gap-3">
            <Link to="/runs" className="mt-1 text-muted hover:text-fg md:hidden" aria-label="All runs">
              <ArrowLeft className="size-4" />
            </Link>
            <div className="min-w-0 flex-1">
              <h1 className="line-clamp-2 font-semibold tracking-tight">
                {snap.data?.run.goal.split("\n")[0] ?? "Loading…"}
              </h1>
              <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
                <span className="inline-flex items-center gap-2">
                  <Dot tone={meta.tone} pulse={meta.working} />
                  <StatusText tone={meta.tone}>{meta.label}</StatusText>
                </span>
                {snap.data?.run.jira_ticket_key && (
                  <span className="font-mono text-xs text-muted">{snap.data.run.jira_ticket_key}</span>
                )}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {meta.working && (
                <Button size="sm" variant="ghost" onClick={() => act.mutate("stop")} title="Stop the agent">
                  <Square className="size-3.5" aria-hidden /> Stop
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => push.mutate()} busy={push.isPending} title="Open a pull request with this run's Terraform">
                <GitPullRequest className="size-4" aria-hidden />
                <span className="hidden lg:inline">Push to GitHub</span>
              </Button>
              <Button size="sm" variant="ghost" onClick={() => download(`${base}/download`, `aegis-${runId.slice(0, 8)}.zip`)} title="Download the workspace">
                <Download className="size-4" aria-hidden />
              </Button>
              <Button size="sm" variant="secondary" className="xl:hidden" onClick={() => setInspectorOpen((v) => !v)}>
                {inspectorOpen ? "Conversation" : "Plan & files"}
              </Button>
            </div>
          </div>
          <PhaseRail status={status} />
          <ErrorNote error={push.error || act.error} />
        </header>

        <div className={cn("min-h-0 flex-1", inspectorOpen && "hidden xl:block")}>
          <Conversation
            snapshot={snap.data}
            working={meta.working}
            hideLatestPlan={awaitingApproval}
            sending={send.isPending}
            onSend={(t) => send.mutate(t)}
            onOpenPlan={() => {
              setTab("plan");
              setInspectorOpen(true);
            }}
            footer={
              awaitingApproval && (
                <div className="flex flex-wrap items-center gap-x-5 gap-y-3 rounded-lg border border-amber/50 bg-amber/5 px-4 py-3">
                  <div className="w-full min-w-0 sm:w-auto sm:flex-1">
                    <p className="font-medium">Review the plan, then decide.</p>
                    <button
                      onClick={() => {
                        setTab("plan");
                        setInspectorOpen(true);
                      }}
                      className="mt-0.5 inline-flex items-baseline gap-3 text-sm hover:underline"
                    >
                      <PlanCounts counts={plan!.counts} />
                      <span className="text-teal xl:hidden">Review</span>
                    </button>
                  </div>
                  <Button variant="ghost" onClick={() => act.mutate("discard")} disabled={act.isPending}>
                    Discard plan
                  </Button>
                  <Button
                    variant={destroys ? "danger" : "primary"}
                    busy={act.isPending && act.variables === "approve"}
                    onClick={() => {
                      if (destroys && !window.confirm(`This plan destroys ${destroys} resource(s). Apply it?`)) return;
                      act.mutate("approve");
                    }}
                  >
                    {destroys ? `Apply, destroying ${destroys}` : "Approve and apply"}
                  </Button>
                </div>
              )
            }
          />
        </div>
        {inspectorOpen && (
          <div className="min-h-0 flex-1 xl:hidden">
            <Inspector runId={runId} snapshot={snap.data} plan={plan} tab={tab} onTab={setTab} working={meta.working} />
          </div>
        )}
      </div>

      <aside className="hidden w-[440px] shrink-0 border-l border-line bg-surface xl:block" aria-label="Plan, files and logs">
        <Inspector runId={runId} snapshot={snap.data} plan={plan} tab={tab} onTab={setTab} working={meta.working} />
      </aside>
    </div>
  );
}
