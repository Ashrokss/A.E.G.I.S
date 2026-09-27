import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { useAccounts, useConnections } from "../lib/hooks";
import type { InboxItem } from "../lib/types";
import { STATUS } from "../lib/status";
import { ago } from "../lib/format";
import { Button, Dialog, Dot, Empty, ErrorNote, Field, PageHeader, inputClass } from "../components/ui";

export default function Inbox() {
  const [claiming, setClaiming] = useState<InboxItem | null>(null);
  const inbox = useQuery({
    queryKey: ["inbox"],
    queryFn: () => request<InboxItem[]>("/agent-api/runs/inbox"),
    refetchInterval: 15_000,
  });
  const pending = inbox.data?.filter((i) => i.state === "pending") ?? [];
  const active = inbox.data?.filter((i) => i.state === "active") ?? [];

  return (
    <div className="mx-auto grid max-w-4xl gap-8 px-6 py-8">
      <PageHeader
        title="Inbox"
        description="Jira tickets labelled for AEGIS arrive here. Claim one to choose the AWS account it runs in; nothing starts until you do."
      />
      <section className="grid gap-3">
        <h2 className="font-semibold">Waiting to be claimed</h2>
        {pending.length === 0 ? (
          <Empty title="Nothing waiting">New tickets appear here when the Jira intake is turned on.</Empty>
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line">
            {pending.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-4 px-4 py-3">
                <span className="font-mono text-xs text-muted">{i.ticket_key}</span>
                <span className="min-w-0 flex-1">{i.summary}</span>
                <span className="text-xs text-muted">{ago(i.created_at)}</span>
                <Button size="sm" variant="primary" onClick={() => setClaiming(i)}>
                  Claim
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {active.length > 0 && (
        <section className="grid gap-3">
          <h2 className="font-semibold">Your ticket runs in progress</h2>
          <ul className="divide-y divide-line rounded-lg border border-line">
            {active.map((i) => {
              const s = STATUS[i.status];
              return (
                <li key={i.id}>
                  <Link to={`/runs/${i.id}`} className="flex items-center gap-4 px-4 py-3 hover:bg-raised/50">
                    <span className="font-mono text-xs text-muted">{i.ticket_key}</span>
                    <span className="min-w-0 flex-1">{i.summary}</span>
                    <span className="inline-flex items-center gap-2 text-xs text-muted">
                      <Dot tone={s.tone} pulse={s.working} />
                      {s.label}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      <ClaimDialog item={claiming} onClose={() => setClaiming(null)} />
    </div>
  );
}

function ClaimDialog({ item, onClose }: { item: InboxItem | null; onClose: () => void }) {
  const navigate = useNavigate();
  const accounts = useAccounts();
  const connections = useConnections();
  const [account, setAccount] = useState("");
  const [github, setGithub] = useState("");
  const [jira, setJira] = useState("");
  const claim = useMutation({
    mutationFn: () =>
      request(`/agent-api/runs/${item!.id}/attach`, {
        body: {
          aws_account_id: account || accounts.data?.[0]?.accountId,
          github_connection_id: github || null,
          jira_connection_id: jira || null,
        },
      }),
    onSuccess: () => navigate(`/runs/${item!.id}`),
  });
  const pick = (provider: string) => connections.data?.filter((c) => c.provider === provider) ?? [];

  return (
    <Dialog open={!!item} onClose={onClose} title={item ? `Claim ${item.ticket_key}` : ""}>
      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          claim.mutate();
        }}
      >
        <p className="text-sm text-muted">{item?.summary}</p>
        <Field label="AWS account">
          <select required value={account} onChange={(e) => setAccount(e.target.value)} className={inputClass}>
            {accounts.data?.map((a) => (
              <option key={a.id} value={a.accountId}>
                {a.displayName ? `${a.displayName} (${a.accountId})` : a.accountId}
              </option>
            ))}
          </select>
        </Field>
        <Field label="GitHub repository">
          <select value={github} onChange={(e) => setGithub(e.target.value)} className={inputClass}>
            <option value="">None</option>
            {pick("github").map((c) => (
              <option key={c.id} value={c.id}>
                {c.displayName}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Jira connection" hint="Lets AEGIS reply on the ticket and move it to review.">
          <select value={jira} onChange={(e) => setJira(e.target.value)} className={inputClass}>
            <option value="">None</option>
            {pick("jira").map((c) => (
              <option key={c.id} value={c.id}>
                {c.displayName}
              </option>
            ))}
          </select>
        </Field>
        <ErrorNote error={claim.error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={claim.isPending} disabled={!accounts.data?.length}>
            Claim and start
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
