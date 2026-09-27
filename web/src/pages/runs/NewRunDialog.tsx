import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { request } from "@/lib/api";
import { useAccounts, useConnections } from "@/lib/hooks";
import { Button, Dialog, ErrorNote, Field, inputClass } from "@/components/ui";
import AISuggestions from "@/components/smoothui/ai-suggestions";

export const STARTERS = [
  "A private S3 bucket for build artifacts, versioned, encrypted, expiring after 90 days",
  "An SQS queue with a dead-letter queue and an SNS topic that fans out to it",
  "A VPC with public and private subnets across two availability zones",
].map((label, i) => ({ id: String(i), label }));

export default function NewRunDialog({
  goal,
  onClose,
  onCreated,
}: {
  goal: string | null;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const accounts = useAccounts();
  const connections = useConnections();
  const [text, setText] = useState("");
  const [account, setAccount] = useState("");
  const [github, setGithub] = useState("");
  const [jira, setJira] = useState("");
  const [ticket, setTicket] = useState("");

  useEffect(() => {
    if (goal !== null) setText(goal);
  }, [goal]);
  useEffect(() => {
    if (!account && accounts.data?.length) setAccount(accounts.data[0].accountId);
  }, [accounts.data, account]);

  const githubConns = connections.data?.filter((c) => c.provider === "github") ?? [];
  const jiraConns = connections.data?.filter((c) => c.provider === "jira") ?? [];

  const create = useMutation({
    mutationFn: () =>
      request<{ id: string }>("/agent-api/runs", {
        body: {
          goal: text.trim(),
          aws_account_id: account,
          github_connection_id: github || null,
          jira_connection_id: jira || null,
          jira_ticket_key: jira && ticket.trim() ? ticket.trim() : null,
        },
      }),
    onSuccess: (r) => onCreated(r.id),
  });

  const noAccounts = accounts.isSuccess && accounts.data.length === 0;

  return (
    <Dialog open={goal !== null} onClose={onClose} title="New run" description="Describe the change in plain language." wide>
      <form
        className="grid gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <textarea
          required
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label="What should AEGIS build or change?"
          className={`${inputClass} h-auto py-3 text-[15px] leading-relaxed`}
          placeholder="A private S3 bucket for build artifacts with versioning and a 90-day expiry"
        />
        {!text.trim() && <AISuggestions label="Or start from" suggestions={STARTERS} onSelect={(s) => setText(s.label)} />}

        {noAccounts ? (
          <p className="rounded-xl border border-amber/35 bg-amber/[0.06] px-4 py-3 text-sm">
            Connect an AWS account first. AEGIS only runs in accounts you connect.{" "}
            <Link to="/accounts" className="text-teal underline" onClick={onClose}>
              Connect an account
            </Link>
          </p>
        ) : (
          <Field label="AWS account">
            <select required value={account} onChange={(e) => setAccount(e.target.value)} className={inputClass}>
              {accounts.data?.map((a) => (
                <option key={a.id} value={a.accountId}>
                  {a.displayName ? `${a.displayName} (${a.accountId})` : a.accountId}
                </option>
              ))}
            </select>
          </Field>
        )}

        <details className="group rounded-xl border border-line" open={!!github || !!jira}>
          <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm">
            <ChevronRight className="size-4 text-muted transition-transform group-open:rotate-90" aria-hidden />
            GitHub and Jira
            <span className="ml-auto text-xs text-muted">Optional</span>
          </summary>
          <div className="grid gap-4 border-t border-line p-4">
            <Field label="GitHub repository" hint="AEGIS follows its conventions and opens a pull request.">
              <select value={github} onChange={(e) => setGithub(e.target.value)} className={inputClass}>
                <option value="">None</option>
                {githubConns.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.displayName}
                  </option>
                ))}
              </select>
            </Field>
            {github && <RepoStatus connectionId={github} />}
            <Field label="Jira">
              <select value={jira} onChange={(e) => setJira(e.target.value)} className={inputClass}>
                <option value="">None</option>
                {jiraConns.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.displayName}
                  </option>
                ))}
              </select>
            </Field>
            {jira && (
              <Field label="Ticket key">
                <input value={ticket} onChange={(e) => setTicket(e.target.value)} className={inputClass} placeholder="OPS-142" />
              </Field>
            )}
          </div>
        </details>

        <ErrorNote error={create.error} />
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={create.isPending} disabled={!text.trim() || !account}>
            Start run
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function RepoStatus({ connectionId }: { connectionId: string }) {
  const q = useQuery({
    queryKey: ["repo-analysis", connectionId],
    queryFn: () => request<{ status: string }>(`/agent-api/repo-analysis/by-connection/${encodeURIComponent(connectionId)}`),
  });
  const analyse = useMutation({
    mutationFn: () => request("/agent-api/repo-analysis", { body: { github_connection_id: connectionId } }),
    onSuccess: () => q.refetch(),
  });
  if (!q.data) return null;
  return q.data.status === "not_analyzed" ? (
    <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted">
      AEGIS hasn't read this repository's conventions yet.
      <Button type="button" size="sm" onClick={() => analyse.mutate()} busy={analyse.isPending}>
        Learn conventions
      </Button>
    </div>
  ) : (
    <p className="text-sm text-muted">Conventions learned. New code will follow them.</p>
  );
}
