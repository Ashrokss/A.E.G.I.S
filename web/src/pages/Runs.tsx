import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Plus } from "lucide-react";
import { request } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { STATUS } from "@/lib/status";
import { ago } from "@/lib/format";
import { Button, Dot, Page, PageHeader, Panel, RowSkeleton } from "@/components/ui";
import AISuggestions from "@/components/smoothui/ai-suggestions";
import NewRunDialog, { STARTERS } from "./runs/NewRunDialog";
import RunView from "./runs/RunView";

export default function Runs() {
  const { runId } = useParams();
  if (runId) return <RunView key={runId} runId={runId} />;
  return <RunList />;
}

function RunList() {
  const navigate = useNavigate();
  const [draft, setDraft] = useState<string | null>(null);
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => request<{ runs: RunSummary[] }>("/agent-api/runs").then((r) => r.runs),
    refetchInterval: 5_000,
  });

  return (
    <Page>
      <PageHeader
        title="Runs"
        description="Describe what you need. AEGIS writes the Terraform and waits for your approval before changing anything."
        actions={
          <Button variant="primary" onClick={() => setDraft("")}>
            <Plus aria-hidden /> New run
          </Button>
        }
      />

      {runs.isLoading ? (
        <RowSkeleton />
      ) : runs.data?.length ? (
        <Panel>
          <ul className="divide-y divide-line">
            {runs.data.map((r) => {
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
      ) : (
        <AISuggestions
          label="Start with one of these, or write your own"
          suggestions={STARTERS}
          onSelect={(s) => setDraft(s.label)}
        />
      )}

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
