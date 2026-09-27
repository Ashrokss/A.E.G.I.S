import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { request } from "../lib/api";
import type { RunSummary } from "../lib/types";
import { STATUS } from "../lib/status";
import { ago, cn } from "../lib/format";
import { Button, Dot } from "../components/ui";
import NewRunDialog from "./runs/NewRunDialog";
import Mission from "./runs/Mission";

const STARTERS = [
  "A private S3 bucket for build artifacts, versioned and encrypted, with objects expiring after 90 days",
  "An SQS queue with a dead-letter queue and an SNS topic that fans out to it",
  "A VPC with two public and two private subnets across two availability zones",
];

export default function Runs() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [draft, setDraft] = useState<string | null>(null);

  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => request<{ runs: RunSummary[] }>("/agent-api/runs").then((r) => r.runs),
    refetchInterval: 5_000,
  });

  return (
    <div className="flex h-full min-h-0">
      <aside
        className={cn(
          "w-full shrink-0 flex-col border-r border-line bg-surface md:flex md:w-72",
          runId ? "hidden" : "flex",
        )}
        aria-label="Runs"
      >
        <div className="p-3">
          <Button variant="primary" className="w-full" onClick={() => setDraft("")}>
            <Plus className="size-4" aria-hidden /> New run
          </Button>
        </div>
        <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          {runs.data?.map((r) => {
            const s = STATUS[r.status] ?? STATUS.IDLE;
            return (
              <li key={r.id}>
                <Link
                  to={`/runs/${r.id}`}
                  className={cn(
                    "grid gap-1 rounded-md px-3 py-2.5 transition-colors",
                    r.id === runId ? "bg-raised" : "hover:bg-raised/60",
                  )}
                >
                  <span className="line-clamp-2 text-sm">{r.title || "Untitled run"}</span>
                  <span className="flex items-center gap-2 text-xs text-muted">
                    <Dot tone={s.tone} pulse={s.working} />
                    {s.label}
                    <span className="ml-auto">{ago(r.updated_at)}</span>
                  </span>
                </Link>
              </li>
            );
          })}
          {runs.data?.length === 0 && (
            <li className="px-3 py-6 text-sm text-muted">Your runs appear here.</li>
          )}
        </ul>
      </aside>

      <section className={cn("min-w-0 flex-1", !runId && "hidden md:block")}>
        {runId ? (
          <Mission key={runId} runId={runId} />
        ) : (
          <div className="mx-auto grid max-w-2xl gap-8 px-6 py-16">
            <div>
              <h1 className="text-3xl font-semibold tracking-tight">What should AEGIS build?</h1>
              <p className="mt-2 text-muted">
                Describe it in plain language. AEGIS asks what it needs to know, writes the Terraform, and shows you
                the plan before anything is created.
              </p>
            </div>
            <ul className="grid gap-2">
              {STARTERS.map((s) => (
                <li key={s}>
                  <button
                    onClick={() => setDraft(s)}
                    className="w-full rounded-lg border border-line bg-surface px-4 py-3 text-left text-sm hover:border-teal"
                  >
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
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
    </div>
  );
}
