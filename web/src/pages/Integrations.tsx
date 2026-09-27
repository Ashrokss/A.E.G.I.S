import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { request } from "@/lib/api";
import { useConnections } from "@/lib/hooks";
import type { Connection } from "@/lib/types";
import { ago } from "@/lib/format";
import { Button, Dialog, Dot, Empty, ErrorNote, Field, Page, PageHeader, Panel, RowMenu, inputClass } from "@/components/ui";

export default function Integrations() {
  const connections = useConnections();
  const [adding, setAdding] = useState<"github" | "jira" | null>(null);
  const of = (p: string) => connections.data?.filter((c) => c.provider === p) ?? [];

  return (
    <Page>
      <PageHeader title="Integrations" description="Your connections are private to you. Tokens are encrypted and used only for your runs." />
      <Section
        title="GitHub"
        description="AEGIS follows the repository's conventions and opens a pull request for every run."
        items={of("github")}
        onAdd={() => setAdding("github")}
      />
      <Section
        title="Jira"
        description="Link runs to tickets and move them to review once applied."
        items={of("jira")}
        onAdd={() => setAdding("jira")}
      />
      <GithubDialog open={adding === "github"} onClose={() => setAdding(null)} />
      <JiraDialog open={adding === "jira"} onClose={() => setAdding(null)} />
    </Page>
  );
}

function Section({ title, description, items, onAdd }: { title: string; description: string; items: Connection[]; onAdd: () => void }) {
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries({ queryKey: ["integrations"] });
  const test = useMutation({
    mutationFn: (c: Connection) => request(`/api/integrations/${c.provider}/${c.id}/test`, { method: "POST" }),
    onSettled: done,
  });
  const remove = useMutation({
    mutationFn: (c: Connection) => request(`/api/integrations/${c.provider}/${c.id}`, { method: "DELETE" }),
    onSettled: done,
  });
  const learn = useMutation({
    mutationFn: (c: Connection) => request("/agent-api/repo-analysis", { body: { github_connection_id: c.id, force: true } }),
  });

  return (
    <section className="grid gap-4">
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold">{title}</h2>
          <p className="text-sm text-muted">{description}</p>
        </div>
        <Button size="sm" onClick={onAdd}>
          <Plus aria-hidden /> Add
        </Button>
      </div>
      <ErrorNote error={test.error || remove.error || learn.error} />
      {items.length === 0 ? (
        <Empty title={`No ${title} connection`} />
      ) : (
        <Panel>
          <ul className="divide-y divide-line">
            {items.map((c) => (
              <li key={c.id} className="flex items-center gap-4 px-5 py-4">
                <Dot tone={c.status === "active" ? "teal" : "amber"} />
                <div className="min-w-0 flex-1">
                  <p className="truncate">{c.displayName}</p>
                  <p className="mt-0.5 truncate text-sm text-muted">
                    {c.status === "active" ? `Checked ${ago(c.lastValidatedAt)}` : c.lastError || "Needs attention"}
                  </p>
                </div>
                <RowMenu
                  items={[
                    ...(c.provider === "github" ? [{ label: "Learn repository conventions", onSelect: () => learn.mutate(c) }] : []),
                    { label: "Test connection", onSelect: () => test.mutate(c) },
                    { label: "Remove", danger: true, onSelect: () => window.confirm(`Remove ${c.displayName}?`) && remove.mutate(c) },
                  ]}
                />
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </section>
  );
}

function Actions({ onClose, busy }: { onClose: () => void; busy: boolean }) {
  return (
    <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
      <Button type="button" variant="ghost" onClick={onClose}>
        Cancel
      </Button>
      <Button type="submit" variant="primary" busy={busy}>
        Connect
      </Button>
    </div>
  );
}

function GithubDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const lookup = useMutation({
    mutationFn: () => request<{ connectedAs: string; repos: Array<{ fullName: string }> }>("/api/integrations/github/repos/lookup", { body: { token } }),
  });
  const create = useMutation({
    mutationFn: () => request("/api/integrations/github/token", { body: { token, repoFullName: repo, baseBranch: branch } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      setToken("");
      setRepo("");
      setBranch("");
      lookup.reset();
      onClose();
    },
  });
  return (
    <Dialog open={open} onClose={onClose} title="Connect a GitHub repository">
      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Personal access token" hint="Fine-grained, for one repository, with Contents and Pull requests set to read and write.">
          <div className="flex gap-2">
            <input type="password" required value={token} onChange={(e) => setToken(e.target.value)} className={inputClass} autoComplete="off" />
            <Button type="button" onClick={() => lookup.mutate()} busy={lookup.isPending} disabled={!token}>
              Find
            </Button>
          </div>
        </Field>
        <Field label="Repository" hint={lookup.data ? `Signed in as ${lookup.data.connectedAs}` : "owner/name"}>
          {lookup.data?.repos.length ? (
            <select required value={repo} onChange={(e) => setRepo(e.target.value)} className={inputClass}>
              <option value="">Choose a repository</option>
              {lookup.data.repos.map((r) => (
                <option key={r.fullName} value={r.fullName}>
                  {r.fullName}
                </option>
              ))}
            </select>
          ) : (
            <input required value={repo} onChange={(e) => setRepo(e.target.value)} className={inputClass} placeholder="you/infrastructure" />
          )}
        </Field>
        <Field label="Base branch" hint="Optional. Defaults to the repository's default branch.">
          <input value={branch} onChange={(e) => setBranch(e.target.value)} className={inputClass} placeholder="main" />
        </Field>
        <ErrorNote error={lookup.error || create.error} />
        <Actions onClose={onClose} busy={create.isPending} />
      </form>
    </Dialog>
  );
}

function JiraDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({ baseUrl: "", email: "", apiToken: "", defaultProject: "", reviewTransitionName: "In Review" });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const create = useMutation({
    mutationFn: () => request("/api/integrations/jira/token", { body: form }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      onClose();
    },
  });
  return (
    <Dialog open={open} onClose={onClose} title="Connect Jira">
      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Site URL">
          <input required value={form.baseUrl} onChange={set("baseUrl")} className={inputClass} placeholder="https://your-team.atlassian.net" />
        </Field>
        <Field label="Email">
          <input required type="email" value={form.email} onChange={set("email")} className={inputClass} />
        </Field>
        <Field label="API token">
          <input required type="password" value={form.apiToken} onChange={set("apiToken")} className={inputClass} autoComplete="off" />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Default project" hint="Optional">
            <input value={form.defaultProject} onChange={set("defaultProject")} className={inputClass} placeholder="OPS" />
          </Field>
          <Field label="Review status" hint="Where tickets move once applied">
            <input value={form.reviewTransitionName} onChange={set("reviewTransitionName")} className={inputClass} />
          </Field>
        </div>
        <ErrorNote error={create.error} />
        <Actions onClose={onClose} busy={create.isPending} />
      </form>
    </Dialog>
  );
}
