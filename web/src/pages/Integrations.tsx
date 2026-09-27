import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { request } from "../lib/api";
import { useConnections } from "../lib/hooks";
import type { Connection } from "../lib/types";
import { ago } from "../lib/format";
import { Button, Dialog, Dot, Empty, ErrorNote, Field, PageHeader, inputClass } from "../components/ui";

export default function Integrations() {
  const connections = useConnections();
  const [adding, setAdding] = useState<"github" | "jira" | null>(null);
  const of = (p: string) => connections.data?.filter((c) => c.provider === p) ?? [];

  return (
    <div className="mx-auto grid max-w-4xl gap-10 px-6 py-8">
      <PageHeader
        title="Integrations"
        description="Connections are yours alone. Tokens are encrypted at rest and only ever used for your own runs."
      />
      <Section
        title="GitHub"
        description="AEGIS follows the repository's existing Terraform conventions and opens a pull request with every run."
        items={of("github")}
        onAdd={() => setAdding("github")}
        addLabel="Connect a repository"
      />
      <Section
        title="Jira"
        description="Link runs to tickets, answer the agent's questions on the ticket, and move it to review when the change is applied."
        items={of("jira")}
        onAdd={() => setAdding("jira")}
        addLabel="Connect Jira"
      />
      <GithubDialog open={adding === "github"} onClose={() => setAdding(null)} />
      <JiraDialog open={adding === "jira"} onClose={() => setAdding(null)} />
    </div>
  );
}

function Section({
  title,
  description,
  items,
  onAdd,
  addLabel,
}: {
  title: string;
  description: string;
  items: Connection[];
  onAdd: () => void;
  addLabel: string;
}) {
  const qc = useQueryClient();
  const test = useMutation({
    mutationFn: (c: Connection) => request<{ connectedAs: string }>(`/api/integrations/${c.provider}/${c.id}/test`, { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
  const remove = useMutation({
    mutationFn: (c: Connection) => request(`/api/integrations/${c.provider}/${c.id}`, { method: "DELETE" }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
  const learn = useMutation({
    mutationFn: (c: Connection) => request("/agent-api/repo-analysis", { body: { github_connection_id: c.id, force: true } }),
  });

  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="max-w-xl">
          <h2 className="text-lg font-semibold">{title}</h2>
          <p className="text-sm text-muted">{description}</p>
        </div>
        <Button onClick={onAdd}>{addLabel}</Button>
      </div>
      <ErrorNote error={test.error || remove.error || learn.error} />
      {items.length === 0 ? (
        <Empty title={`No ${title} connections`} />
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {items.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
              <Dot tone={c.status === "active" ? "teal" : "amber"} />
              <div className="min-w-0 flex-1">
                <p className="font-medium">{c.displayName}</p>
                <p className="text-xs text-muted">
                  {c.status === "active" ? `Checked ${ago(c.lastValidatedAt)}` : c.lastError || "Needs attention"}
                </p>
              </div>
              {c.provider === "github" && (
                <Button size="sm" variant="ghost" busy={learn.isPending && learn.variables?.id === c.id} onClick={() => learn.mutate(c)}>
                  {learn.isSuccess && learn.variables?.id === c.id ? "Conventions learned" : "Learn conventions"}
                </Button>
              )}
              <Button size="sm" variant="ghost" busy={test.isPending && test.variables?.id === c.id} onClick={() => test.mutate(c)}>
                Test
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => window.confirm(`Remove ${c.displayName}?`) && remove.mutate(c)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

type Repo = { fullName: string; defaultBranch?: string };

function GithubDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const lookup = useMutation({
    mutationFn: () => request<{ connectedAs: string; repos: Repo[] }>("/api/integrations/github/repos/lookup", { body: { token } }),
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
        <Field
          label="Personal access token"
          hint="Fine-grained, for the one repository, with Contents and Pull requests read and write."
        >
          <div className="flex gap-2">
            <input type="password" required value={token} onChange={(e) => setToken(e.target.value)} className={inputClass} autoComplete="off" />
            <Button type="button" onClick={() => lookup.mutate()} busy={lookup.isPending} disabled={!token}>
              Find repositories
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
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={create.isPending}>
            Connect
          </Button>
        </div>
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
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Email">
            <input required type="email" value={form.email} onChange={set("email")} className={inputClass} />
          </Field>
          <Field label="API token">
            <input required type="password" value={form.apiToken} onChange={set("apiToken")} className={inputClass} autoComplete="off" />
          </Field>
          <Field label="Default project" hint="Optional, e.g. OPS">
            <input value={form.defaultProject} onChange={set("defaultProject")} className={inputClass} />
          </Field>
          <Field label="Review status" hint="Where tickets move once applied.">
            <input value={form.reviewTransitionName} onChange={set("reviewTransitionName")} className={inputClass} />
          </Field>
        </div>
        <ErrorNote error={create.error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={create.isPending}>
            Connect
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
