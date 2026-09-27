import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { request } from "@/lib/api";
import { useAccounts, useConnections } from "@/lib/hooks";
import type { InboxItem } from "@/lib/types";
import { STATUS } from "@/lib/status";
import { ago } from "@/lib/format";
import { Button, Dialog, Dot, Empty, ErrorNote, Field, Page, PageHeader, Panel, RowSkeleton, inputClass } from "@/components/ui";

export default function Inbox() {
  const [starting, setStarting] = useState<InboxItem | null>(null);
  const inbox = useQuery({
    queryKey: ["inbox"],
    queryFn: () => request<InboxItem[]>("/agent-api/runs/inbox"),
    refetchInterval: 15_000,
  });
  const pending = inbox.data?.filter((i) => i.state === "pending") ?? [];
  const active = inbox.data?.filter((i) => i.state === "active") ?? [];

  return (
    <Page>
      <PageHeader title="Inbox" description="Jira tickets labelled for AEGIS wait here. Start one and choose the AWS account it runs in." />

      {inbox.isLoading ? (
        <RowSkeleton rows={2} />
      ) : pending.length === 0 ? (
        <Empty title="Nothing waiting">New tickets appear here when the Jira intake is on.</Empty>
      ) : (
        <Panel>
          <ul className="divide-y divide-line">
            {pending.map((i) => (
              <li key={i.id} className="flex items-center gap-4 px-5 py-4">
                <div className="min-w-0 flex-1">
                  <p className="truncate">{i.summary}</p>
                  <p className="mt-0.5 text-sm text-muted">
                    <span className="font-mono text-xs">{i.ticket_key}</span>
                    <span className="px-1.5 text-line">/</span>
                    {ago(i.created_at)}
                  </p>
                </div>
                <Button size="sm" variant="primary" onClick={() => setStarting(i)}>
                  Start
                </Button>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {active.length > 0 && (
        <section className="grid gap-3">
          <h2 className="text-sm font-medium text-muted">In progress</h2>
          <Panel>
            <ul className="divide-y divide-line">
              {active.map((i) => {
                const s = STATUS[i.status];
                return (
                  <li key={i.id}>
                    <Link to={`/runs/${i.id}`} className="flex items-center gap-4 px-5 py-4 hover:bg-raised/60">
                      <Dot tone={s.tone} pulse={s.working} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate">{i.summary}</p>
                        <p className="mt-0.5 text-sm text-muted">{s.label}</p>
                      </div>
                      <ChevronRight className="size-4 text-muted" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
          </Panel>
        </section>
      )}
      <StartDialog item={starting} onClose={() => setStarting(null)} />
    </Page>
  );
}

function StartDialog({ item, onClose }: { item: InboxItem | null; onClose: () => void }) {
  const navigate = useNavigate();
  const accounts = useAccounts();
  const connections = useConnections();
  const [account, setAccount] = useState("");
  const [github, setGithub] = useState("");
  const [jira, setJira] = useState("");
  const start = useMutation({
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
    <Dialog open={!!item} onClose={onClose} title={item ? `Start ${item.ticket_key}` : ""} description={item?.summary}>
      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          start.mutate();
        }}
      >
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
        <ErrorNote error={start.error} />
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={start.isPending} disabled={!accounts.data?.length}>
            Start run
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
