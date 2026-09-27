import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Square } from "lucide-react";
import AnimatedTabs from "@/components/smoothui/animated-tabs";
import AIApproval from "@/components/smoothui/ai-approval";
import AIConversation from "@/components/smoothui/ai-conversation";
import { download, request } from "@/lib/api";
import type { PlanSummary, RunSnapshot, RunStatus } from "@/lib/types";
import { PHASES, STATUS, phaseIndex } from "@/lib/status";
import { tryJson } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button, Dot, ErrorNote, RowMenu, StatusText } from "@/components/ui";
import { PlanCounts } from "@/components/PlanGlyph";
import Conversation, { Composer } from "./Conversation";
import { FilesView, LogsView, PlanView } from "./Inspector";

type Tab = "chat" | "plan" | "files" | "logs";
const TABS: Array<{ id: Tab; label: string }> = [
  { id: "chat", label: "Conversation" },
  { id: "plan", label: "Plan" },
  { id: "files", label: "Files" },
  { id: "logs", label: "Logs" },
];

export default function RunView({ runId }: { runId: string }) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("chat");
  // Bumped to remount the approval card when a decision is cancelled or fails.
  const [approvalKey, setApprovalKey] = useState(0);
  const base = `/agent-api/runs/${encodeURIComponent(runId)}`;

  const snap = useQuery({
    queryKey: ["run", runId],
    queryFn: () => request<RunSnapshot>(`${base}/snapshot`),
    refetchInterval: (q) => {
      const s = q.state.data?.run.status;
      return s && STATUS[s]?.working ? 1_200 : 5_000;
    },
  });

  const status: RunStatus = snap.data?.run.status ?? "IDLE";
  const meta = STATUS[status] ?? STATUS.IDLE;
  const plan = useMemo(() => {
    const msgs = snap.data?.messages ?? [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].type === "plan_summary") return tryJson<PlanSummary>(msgs[i].content);
    }
    return null;
  }, [snap.data]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["run", runId] });
    qc.invalidateQueries({ queryKey: ["runs"] });
  };
  const act = useMutation({
    mutationFn: (action: "approve" | "discard" | "stop") => request(`${base}/${action}`, { method: "POST" }),
    onSuccess: refresh,
    onError: () => setApprovalKey((k) => k + 1),
  });
  const push = useMutation({ mutationFn: () => request(`${base}/push-to-github`, { body: {} }), onSuccess: refresh });
  const send = useMutation({
    mutationFn: (content: string) => request(`${base}/messages`, { body: { role: "user", content, type: "text" } }),
    onSuccess: refresh,
  });

  const awaiting = status === "AWAITING_APPROVAL" && !!plan;
  const destroys = (plan?.counts.destroy ?? 0) + (plan?.counts.replace ?? 0);

  return (
    <div className="mx-auto flex h-[calc(100dvh-3.5rem-1px)] w-full max-w-5xl flex-col px-4 sm:px-6 md:h-[calc(100dvh-4rem-1px)]">
      <header className="grid shrink-0 gap-3 pt-3 sm:gap-5 sm:pt-7">
        <div className="flex items-center justify-between gap-2">
          <Link to="/runs" className="-ml-1 inline-flex items-center gap-1 rounded-lg px-1 py-1 text-sm text-muted hover:text-fg">
            <ChevronLeft className="size-4" aria-hidden /> Runs
          </Link>
          <div className="flex items-center gap-1">
            {meta.working && (
              <Button size="sm" variant="ghost" onClick={() => act.mutate("stop")}>
                <Square className="size-3.5" aria-hidden /> Stop
              </Button>
            )}
            <RowMenu
              label="Run actions"
              items={[
                { label: push.isPending ? "Pushing…" : "Push to GitHub", onSelect: () => push.mutate(), disabled: push.isPending },
                { label: "Download workspace", onSelect: () => download(`${base}/download`, `aegis-${runId.slice(0, 8)}.zip`) },
              ]}
            />
          </div>
        </div>

        <div className="grid gap-1.5 sm:gap-2">
          <h1 className="line-clamp-1 text-lg font-semibold sm:line-clamp-2 sm:text-2xl sm:text-balance">
            {snap.data?.run.goal.split("\n")[0] ?? " "}
          </h1>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="inline-flex items-center gap-2">
              <Dot tone={meta.tone} pulse={meta.working} />
              <StatusText tone={meta.tone}>{meta.label}</StatusText>
            </span>
            {snap.data?.run.jira_ticket_key && <span className="font-mono text-xs text-muted">{snap.data.run.jira_ticket_key}</span>}
          </div>
        </div>

        <Progress status={status} />

        <AnimatedTabs
          variant="segment"
          tabs={TABS}
          activeTab={tab}
          onChange={(id) => setTab(id as Tab)}
          className="w-full sm:w-fit"
        />
        <ErrorNote error={snap.error || push.error || act.error || send.error} />
      </header>

      {tab === "chat" ? (
        <AIConversation className="-mr-2 w-auto flex-1 sm:-mr-3" contentKey={`${snap.data?.messages.length ?? 0}:${meta.working}`}>
          <div className="py-4 pr-2 sm:py-6 sm:pr-3">
            <Conversation
              snapshot={snap.data}
              working={meta.working}
              hideLatestPlan={awaiting}
              onSend={(t) => send.mutate(t)}
              onOpenPlan={() => setTab("plan")}
            />
          </div>
        </AIConversation>
      ) : (
        <div className="-mr-2 min-h-0 flex-1 overflow-y-auto py-4 pr-2 sm:-mr-3 sm:py-6 sm:pr-3">
          {tab === "plan" && <PlanView snapshot={snap.data} plan={plan} />}
          {tab === "files" && <FilesView runId={runId} working={meta.working} />}
          {tab === "logs" && <LogsView runId={runId} working={meta.working} />}
        </div>
      )}

      <footer className="grid shrink-0 gap-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {awaiting && (tab === "chat" || tab === "plan") && (
          <AIApproval
            key={approvalKey}
            className="border-amber/35 bg-amber/[0.06] p-3 sm:p-4"
            question="The plan is ready for your review"
            options={[
              destroys
                ? { id: "approve", label: "Apply and destroy", destructive: true, detail: `${destroys} to destroy` }
                : { id: "approve", label: "Approve and apply" },
              { id: "discard", label: "Discard the plan" },
            ]}
            onDecide={(o) => {
              if (o.id === "approve" && destroys && !window.confirm(`This plan destroys ${destroys} resource(s). Apply it?`)) {
                setApprovalKey((k) => k + 1);
                return;
              }
              act.mutate(o.id as "approve" | "discard");
            }}
          >
            <PlanCounts counts={plan!.counts} className="text-sm" />
          </AIApproval>
        )}
        {tab === "chat" && <Composer working={meta.working} onSend={(t) => send.mutate(t)} onStop={() => act.mutate("stop")} />}
      </footer>
    </div>
  );
}

/** Six slim segments: the run lifecycle at a glance. */
function Progress({ status }: { status: RunStatus }) {
  const current = phaseIndex(status);
  const done = status === "COMPLETED";
  const failed = status === "FAILED";
  return (
    <div className="grid gap-2" aria-label={`Step ${current + 1} of ${PHASES.length}: ${PHASES[current]}`}>
      <div className="flex gap-1.5">
        {PHASES.map((p, i) => (
          <span
            key={p}
            className={cn(
              "h-1 flex-1 rounded-full transition-colors duration-500",
              i < current || (i === current && done)
                ? "bg-teal"
                : i === current
                  ? failed
                    ? "bg-rust"
                    : "animate-pulse bg-teal/60"
                  : "bg-line",
            )}
          />
        ))}
      </div>
      <p className="hidden text-xs text-muted sm:block">
        Step {current + 1} of {PHASES.length}: <span className="text-fg">{PHASES[current]}</span>
      </p>
    </div>
  );
}
