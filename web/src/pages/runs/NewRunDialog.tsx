import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { request } from "../../lib/api";
import { useAccounts, useConnections } from "../../lib/hooks";
import { Button, Dialog, ErrorNote, Field, inputClass } from "../../components/ui";

type Created = { id: string };

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
      request<Created>("/agent-api/runs", {
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
    <Dialog open={goal !== null} onClose={onClose} title="New run" wide>
      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="What should AEGIS build or change?">
          <textarea
            required
            rows={4}
            value={text}
            onChange={(e) => setText(e.target.value)}
            className={`${inputClass} h-auto py-2`}
            placeholder="A private S3 bucket for build artifacts with versioning and 90-day expiry"
          />
        </Field>

        {noAccounts ? (
          <p className="rounded-md border border-amber/40 bg-amber/10 px-3 py-2 text-sm">
            Connect an AWS account first. AEGIS only ever runs Terraform in accounts you onboard.{" "}
            <Link to="/accounts" className="text-teal underline" onClick={onClose}>
              Go to Accounts
            </Link>
          </p>
        ) : (
          <Field label="AWS account" hint="Terraform runs here, through the AEGIS role you deployed in it.">
            <select required value={account} onChange={(e) => setAccount(e.target.value)} className={inputClass}>
              {accounts.data?.map((a) => (
                <option key={a.id} value={a.accountId}>
                  {a.displayName ? `${a.displayName} (${a.accountId})` : a.accountId}
                </option>
              ))}
            </select>
          </Field>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="GitHub repository" hint="Optional. AEGIS follows its conventions and opens a pull request.">
            <select value={github} onChange={(e) => setGithub(e.target.value)} className={inputClass}>
              <option value="">None</option>
              {githubConns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.displayName}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Jira" hint="Optional. Links the run to a ticket.">
            <select value={jira} onChange={(e) => setJira(e.target.value)} className={inputClass}>
              <option value="">None</option>
              {jiraConns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.displayName}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {jira && (
          <Field label="Ticket key">
            <input value={ticket} onChange={(e) => setTicket(e.target.value)} className={inputClass} placeholder="OPS-142" />
          </Field>
        )}
        {github && <RepoStatus connectionId={github} />}

        <ErrorNote error={create.error} />
        <div className="flex justify-end gap-2">
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

type Analysis = { status: string; repo?: string; target_dir?: string; analyzed_at?: string };

function RepoStatus({ connectionId }: { connectionId: string }) {
  const q = useQuery({
    queryKey: ["repo-analysis", connectionId],
    queryFn: () => request<Analysis>(`/agent-api/repo-analysis/by-connection/${encodeURIComponent(connectionId)}`),
  });
  const analyse = useMutation({
    mutationFn: () => request<Analysis>("/agent-api/repo-analysis", { body: { github_connection_id: connectionId } }),
    onSuccess: () => q.refetch(),
  });
  if (!q.data) return null;
  return q.data.status === "not_analyzed" ? (
    <p className="flex flex-wrap items-center gap-3 text-sm text-muted">
      AEGIS hasn't read this repository's conventions yet.
      <Button type="button" size="sm" onClick={() => analyse.mutate()} busy={analyse.isPending}>
        Learn conventions
      </Button>
    </p>
  ) : (
    <p className="text-sm text-muted">Repository conventions learned. New code will follow them.</p>
  );
}
