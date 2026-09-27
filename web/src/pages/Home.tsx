import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, ChevronRight, Plus } from "lucide-react";
import { request } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useAccounts, useConnections } from "@/lib/hooks";
import type { InboxItem, KbDocument, RunSummary } from "@/lib/types";
import { STATUS } from "@/lib/status";
import { ago } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button, Dot, Empty, Page, Panel, RowSkeleton } from "@/components/ui";
import NewRunDialog from "./runs/NewRunDialog";

const NEEDS_YOU = new Set(["AWAITING_APPROVAL", "AWAITING_INPUT"]);
const WEEK_MS = 7 * 24 * 3_600_000;

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

/** Server timestamps are naive UTC ISO strings. */
const ts = (s: string) => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`).getTime();

export default function Home() {
  const { identity } = useAuth();
  const navigate = useNavigate();
  const [draft, setDraft] = useState<string | null>(null);

  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => request<{ runs: RunSummary[] }>("/agent-api/runs").then((r) => r.runs),
    refetchInterval: 10_000,
  });
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: () => request<InboxItem[]>("/agent-api/runs/inbox") });
  const docs = useQuery({
    queryKey: ["kb-docs"],
    queryFn: () => request<{ documents: KbDocument[] }>("/agent-api/knowledge-base/documents").then((r) => r.documents),
  });
  const accounts = useAccounts();
  const connections = useConnections();

  const all = runs.data ?? [];
  const waiting = all.filter((r) => NEEDS_YOU.has(r.status));
  const working = all.filter((r) => STATUS[r.status]?.working);
  const appliedThisWeek = all.filter((r) => r.status === "COMPLETED" && Date.now() - ts(r.updated_at) < WEEK_MS);
  const unstarted = inbox.data?.filter((i) => i.state === "pending").length ?? 0;

  const setup = [
    { done: (accounts.data?.length ?? 0) > 0, title: "Connect an AWS account", body: "Runs happen only in accounts you connect.", to: "/accounts" },
    { done: !!connections.data?.some((c) => c.provider === "github"), title: "Connect a GitHub repository", body: "Get a pull request with the code for every run.", to: "/integrations" },
    { done: (docs.data?.length ?? 0) > 0, title: "Upload your standards", body: "Runbooks and conventions AEGIS can answer from.", to: "/knowledge" },
  ];
  const setupLoaded = accounts.isSuccess && connections.isSuccess && docs.isSuccess;
  const setupLeft = setup.filter((s) => !s.done).length;

  const name = identity?.email.split("@")[0] ?? "";

  return (
    <Page>
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-sm text-muted">{greeting()},</p>
          <h1 className="mt-1 text-3xl font-semibold capitalize sm:text-4xl">{name}</h1>
        </div>
        <Button variant="primary" onClick={() => setDraft("")}>
          <Plus aria-hidden /> New run
        </Button>
      </header>

      {/* At a glance */}
      <section aria-label="At a glance" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Waiting for you" value={waiting.length} tone={waiting.length ? "amber" : undefined} loading={runs.isLoading} to="/runs" />
        <Stat label="In progress" value={working.length} tone={working.length ? "teal" : undefined} loading={runs.isLoading} to="/runs" />
        <Stat label="Applied this week" value={appliedThisWeek.length} loading={runs.isLoading} to="/runs" />
        <Stat label="AWS accounts" value={accounts.data?.length ?? 0} loading={accounts.isLoading} to="/accounts" />
      </section>

      {/* Needs your attention */}
      <section className="grid gap-3">
        <h2 className="text-lg font-semibold">Needs your attention</h2>
        {runs.isLoading ? (
          <RowSkeleton rows={2} />
        ) : waiting.length === 0 && unstarted === 0 ? (
          <Panel className="flex items-center gap-3 px-5 py-4 text-sm text-muted">
            <span className="grid size-7 place-items-center rounded-full bg-teal/10 text-teal">
              <Check className="size-4" aria-hidden />
            </span>
            All clear. Nothing is waiting on you.
          </Panel>
        ) : (
          <Panel>
            <ul className="divide-y divide-line">
              {waiting.map((r) => (
                <li key={r.id}>
                  <Link to={`/runs/${r.id}`} className="flex items-center gap-4 px-5 py-4 transition-colors hover:bg-raised/60">
                    <Dot tone="amber" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate">{r.title}</p>
                      <p className="mt-0.5 text-sm text-amber">
                        {r.status === "AWAITING_APPROVAL" ? "Plan ready for your review" : "AEGIS has a question for you"}
                      </p>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
                  </Link>
                </li>
              ))}
              {unstarted > 0 && (
                <li>
                  <Link to="/inbox" className="flex items-center gap-4 px-5 py-4 transition-colors hover:bg-raised/60">
                    <Dot tone="amber" />
                    <div className="min-w-0 flex-1">
                      <p>
                        {unstarted} Jira {unstarted === 1 ? "ticket" : "tickets"} waiting to start
                      </p>
                      <p className="mt-0.5 text-sm text-muted">Start one and choose where it runs</p>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
                  </Link>
                </li>
              )}
            </ul>
          </Panel>
        )}
      </section>

      {/* Get started: only while something is left to do */}
      {setupLoaded && setupLeft > 0 && (
        <section className="grid gap-3">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="text-lg font-semibold">Get started</h2>
            <p className="text-sm text-muted">
              {setup.length - setupLeft} of {setup.length} done
            </p>
          </div>
          <Panel>
            <ol className="divide-y divide-line">
              {setup.map((s, i) => (
                <li key={s.title}>
                  <Link to={s.to} className="flex items-center gap-4 px-5 py-4 transition-colors hover:bg-raised/60">
                    <span
                      className={cn(
                        "grid size-7 shrink-0 place-items-center rounded-full border text-xs font-medium",
                        s.done ? "border-teal bg-teal text-on-accent" : "border-line text-muted",
                      )}
                    >
                      {s.done ? <Check className="size-3.5" aria-hidden /> : i + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className={cn(s.done && "text-muted line-through decoration-line")}>{s.title}</p>
                      {!s.done && <p className="mt-0.5 text-sm text-muted">{s.body}</p>}
                    </div>
                    {!s.done && <ArrowRight className="size-4 shrink-0 text-muted" aria-hidden />}
                  </Link>
                </li>
              ))}
            </ol>
          </Panel>
        </section>
      )}

      {/* Recent runs */}
      <section className="grid gap-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-lg font-semibold">Recent runs</h2>
          {all.length > 0 && (
            <Link to="/runs" className="text-sm text-teal hover:underline">
              View all
            </Link>
          )}
        </div>
        {runs.isLoading ? (
          <RowSkeleton />
        ) : all.length === 0 ? (
          <Empty title="No runs yet">
            Start one by describing the infrastructure you need.
          </Empty>
        ) : (
          <Panel>
            <ul className="divide-y divide-line">
              {all.slice(0, 5).map((r) => {
                const s = STATUS[r.status] ?? STATUS.IDLE;
                return (
                  <li key={r.id}>
                    <Link to={`/runs/${r.id}`} className="flex items-center gap-4 px-5 py-4 transition-colors hover:bg-raised/60">
                      <Dot tone={s.tone} pulse={s.working} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate">{r.title || "Untitled run"}</p>
                        <p className="mt-0.5 text-sm text-muted">
                          {s.label}
                          <span className="px-1.5 text-line">/</span>
                          {ago(r.updated_at)}
                        </p>
                      </div>
                      <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
          </Panel>
        )}
      </section>

      <NewRunDialog
        goal={draft}
        onClose={() => setDraft(null)}
        onCreated={(id) => {
          setDraft(null);
          runs.refetch();
          navigate(`/runs/${id}`);
        }}
      />
    </Page>
  );
}

function Stat({ label, value, tone, loading, to }: { label: string; value: number; tone?: "amber" | "teal"; loading?: boolean; to: string }) {
  return (
    <Link to={to} className="group grid gap-1 rounded-2xl border border-line bg-surface px-4 py-4 transition-colors hover:border-muted/30 sm:px-5">
      <span className="text-sm text-muted">{label}</span>
      {loading ? (
        <span className="mt-1 h-8 w-10 animate-pulse rounded bg-raised" />
      ) : (
        <span className={cn("font-display text-3xl font-semibold tabular-nums", tone === "amber" && "text-amber", tone === "teal" && "text-teal")}>
          {value}
        </span>
      )}
    </Link>
  );
}
