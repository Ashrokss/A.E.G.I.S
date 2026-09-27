import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, SendHorizontal, Upload } from "lucide-react";
import { request } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { KbAnswer, KbCandidate, KbDocument, KbGap, KbThread, RunMessage } from "../lib/types";
import { ago, cn, tryJson } from "../lib/format";
import Markdown from "../components/Markdown";
import { Button, Empty, ErrorNote, PageHeader } from "../components/ui";

type View = "ask" | "documents" | "review";

export default function Knowledge() {
  const { identity } = useAuth();
  const [view, setView] = useState<View>("ask");
  const views: Array<[View, string]> = [
    ["ask", "Ask"],
    ["documents", "Documents"],
    ...(identity?.role === "admin" ? ([["review", "Review queue"]] as Array<[View, string]>) : []),
  ];
  return (
    <div className="flex h-full flex-col">
      <div role="tablist" className="flex shrink-0 gap-1 border-b border-line px-4">
        {views.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={view === id}
            onClick={() => setView(id)}
            className={cn(
              "relative h-11 px-3 text-sm",
              view === id ? "text-fg after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-teal" : "text-muted hover:text-fg",
            )}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1">
        {view === "ask" && <Ask />}
        {view === "documents" && <Documents />}
        {view === "review" && <Review />}
      </div>
    </div>
  );
}

// ── Ask ───────────────────────────────────────────────────────────────────────

type KbSnapshot = { thread: KbThread; messages: RunMessage[]; is_running: boolean };

function Ask() {
  const qc = useQueryClient();
  const [threadId, setThreadId] = useState<string | null>(null);
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  const threads = useQuery({
    queryKey: ["kb-threads"],
    queryFn: () => request<{ threads: KbThread[] }>("/agent-api/kb-chat/threads").then((r) => r.threads),
  });
  const snap = useQuery({
    queryKey: ["kb-thread", threadId],
    enabled: !!threadId,
    queryFn: () => request<KbSnapshot>(`/agent-api/kb-chat/threads/${threadId}/snapshot`),
    refetchInterval: (q) => (q.state.data?.is_running ? 1_500 : false),
  });
  const send = useMutation({
    mutationFn: async (content: string) => {
      let id = threadId;
      if (!id) {
        id = (await request<{ id: string }>("/agent-api/kb-chat/threads", { body: { title: null } })).id;
        setThreadId(id);
      }
      await request(`/agent-api/kb-chat/threads/${id}/messages`, { body: { role: "user", content, type: "text" } });
      return id;
    },
    onSuccess: (id) => {
      qc.invalidateQueries({ queryKey: ["kb-thread", id] });
      qc.invalidateQueries({ queryKey: ["kb-threads"] });
    },
  });

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [snap.data?.messages.length]);

  const submit = () => {
    const t = text.trim();
    if (t) {
      send.mutate(t);
      setText("");
    }
  };

  return (
    <div className="flex h-full min-h-0">
      <aside className="hidden w-64 shrink-0 flex-col border-r border-line bg-surface md:flex">
        <div className="p-3">
          <Button className="w-full" onClick={() => setThreadId(null)}>
            <Plus className="size-4" aria-hidden /> New question
          </Button>
        </div>
        <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          {threads.data?.map((t) => (
            <li key={t.id}>
              <button
                onClick={() => setThreadId(t.id)}
                className={cn(
                  "grid w-full gap-0.5 rounded-md px-3 py-2 text-left",
                  t.id === threadId ? "bg-raised" : "hover:bg-raised/60",
                )}
              >
                <span className="line-clamp-2 text-sm">{t.title || "Untitled"}</span>
                <span className="text-xs text-muted">{ago(t.updated_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-6">
          <div className="mx-auto grid max-w-3xl gap-5">
            {!threadId && (
              <div className="grid gap-2 py-6">
                <h1 className="text-2xl font-semibold tracking-tight">Ask your knowledge base</h1>
                <p className="text-muted">
                  Answers come from the runbooks, standards and postmortems your team uploaded, with every source
                  cited. If the documents don't cover it, AEGIS says so and asks before searching the web.
                </p>
              </div>
            )}
            {snap.data?.messages.map((m, i) => <KbEntry key={i} message={m} />)}
            {snap.data?.is_running && <p className="text-sm text-muted">Searching the knowledge base…</p>}
            <ErrorNote error={send.error} />
            <div ref={endRef} />
          </div>
        </div>
        <form
          className="mx-auto mb-5 flex w-full max-w-3xl items-end gap-2 rounded-lg border border-line bg-surface p-2 focus-within:border-teal"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            placeholder="What are our tagging standards for production resources?"
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm focus:outline-none"
            aria-label="Question"
          />
          <Button type="submit" variant="primary" size="sm" busy={send.isPending} disabled={!text.trim()} aria-label="Ask">
            <SendHorizontal className="size-4" aria-hidden />
          </Button>
        </form>
      </div>
    </div>
  );
}

function KbEntry({ message: m }: { message: RunMessage }) {
  if (m.type === "kb_answer") {
    const a = tryJson<KbAnswer>(m.content);
    if (!a) return null;
    return (
      <article className="grid gap-3 rounded-lg border border-line bg-surface p-4">
        <Markdown>{a.answer}</Markdown>
        {(a.doc_citations.length > 0 || a.web_citations.length > 0) && (
          <div className="grid gap-2 border-t border-line pt-3 text-sm">
            {a.doc_citations.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                <span className="text-muted">From your documents:</span>
                {a.doc_citations.map((c) => (
                  <span key={c} className="rounded border border-line px-1.5 text-xs">
                    {c}
                  </span>
                ))}
              </div>
            )}
            {a.web_citations.length > 0 && (
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                <span className="text-muted">From the web (not yet approved as knowledge):</span>
                {a.web_citations.map((u) => (
                  <a key={u} href={u} target="_blank" rel="noreferrer" className="break-all text-teal underline">
                    {new URL(u).hostname}
                  </a>
                ))}
              </div>
            )}
          </div>
        )}
        <p className={cn("text-xs", a.confidence === "low" ? "text-amber" : "text-muted")}>
          Confidence: {a.confidence}
          {a.confidence_reason ? `. ${a.confidence_reason}` : ""}
        </p>
      </article>
    );
  }
  const mine = m.role === "user";
  return (
    <article className="grid gap-1">
      <span className={cn("text-xs font-semibold", mine ? "text-fg" : "text-teal")}>{mine ? "You" : "AEGIS"}</span>
      <Markdown>{m.content}</Markdown>
    </article>
  );
}

// ── Documents ─────────────────────────────────────────────────────────────────

function Documents() {
  const qc = useQueryClient();
  const docs = useQuery({
    queryKey: ["kb-docs"],
    queryFn: () => request<{ documents: KbDocument[] }>("/agent-api/knowledge-base/documents").then((r) => r.documents),
    refetchInterval: (q) => (q.state.data?.some((d) => d.status === "processing") ? 3_000 : false),
  });
  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return request("/agent-api/knowledge-base/documents", { form });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kb-docs"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => request(`/agent-api/knowledge-base/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kb-docs"] }),
  });

  return (
    <div className="mx-auto grid max-w-4xl gap-6 overflow-y-auto px-6 py-8">
      <PageHeader
        title="Documents"
        description="Markdown, text, PDF, Word, Excel and CSV, up to 10 MB each. Uploads are chunked and indexed for search."
        actions={
          <label className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-md bg-teal px-4 text-sm font-medium text-on-accent">
            <Upload className="size-4" aria-hidden />
            {upload.isPending ? "Uploading…" : "Upload a document"}
            <input
              type="file"
              className="sr-only"
              accept=".md,.txt,.pdf,.docx,.xlsx,.csv"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) upload.mutate(f);
                e.target.value = "";
              }}
            />
          </label>
        }
      />
      <ErrorNote error={upload.error || remove.error} />
      {docs.data?.length === 0 ? (
        <Empty title="No documents yet">Upload your team's runbooks and standards so AEGIS can answer from them.</Empty>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {docs.data?.map((d) => (
            <li key={d.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
              <div className="w-full min-w-0 sm:w-auto sm:flex-1">
                <p className="font-medium">{d.title || d.filename}</p>
                <p className="text-xs text-muted">
                  {d.filename}, uploaded {ago(d.uploaded_at)}
                </p>
              </div>
              <span className={cn("text-sm", d.status === "ready" ? "text-muted" : d.status === "failed" ? "text-rust" : "text-amber")}>
                {d.status === "ready" ? `${d.chunk_count} passages` : d.status === "pending_approval" ? "Awaiting approval" : d.status}
              </span>
              <Button size="sm" variant="ghost" onClick={() => window.confirm(`Delete ${d.filename}?`) && remove.mutate(d.id)}>
                Delete
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Review (admin) ────────────────────────────────────────────────────────────

function Review() {
  const qc = useQueryClient();
  const kb = (p: string) => `/agent-api/knowledge-base${p}`;
  const candidates = useQuery({
    queryKey: ["kb-candidates"],
    queryFn: () => request<{ candidates: KbCandidate[] }>(kb("/candidates")).then((r) => r.candidates),
  });
  const docs = useQuery({
    queryKey: ["kb-docs"],
    queryFn: () => request<{ documents: KbDocument[] }>(kb("/documents")).then((r) => r.documents),
  });
  const gaps = useQuery({
    queryKey: ["kb-gaps"],
    queryFn: () => request<{ gaps: KbGap[] }>(kb("/gaps")).then((r) => r.gaps.filter((g) => g.status === "open")),
  });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => request(kb(path), { method: "POST", body }),
    onSuccess: () => ["kb-candidates", "kb-docs", "kb-gaps"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })),
  });
  const pendingDocs = docs.data?.filter((d) => d.status === "pending_approval") ?? [];

  return (
    <div className="mx-auto grid max-w-4xl gap-10 overflow-y-auto px-6 py-8">
      <PageHeader
        title="Review queue"
        description="Nothing becomes knowledge on its own. Web findings and generated write-ups wait here until an admin approves them."
      />
      <ErrorNote error={act.error} />

      <section className="grid gap-3">
        <h2 className="font-semibold">Answers researched on the web</h2>
        {!candidates.data?.length ? (
          <Empty title="Nothing to review" />
        ) : (
          candidates.data.map((c) => (
            <article key={c.id} className="grid gap-3 rounded-lg border border-line p-4">
              <p className="font-medium">{c.question}</p>
              <div className="max-h-60 overflow-y-auto text-sm">
                <Markdown>{c.consolidated_answer}</Markdown>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="mr-auto text-xs text-muted">
                  {c.created_by ? `Asked by ${c.created_by}, ` : ""}
                  {ago(c.created_at)}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    const reason = window.prompt("Why is this being rejected?");
                    if (reason) act.mutate({ path: `/candidates/${c.id}/reject`, body: { reason } });
                  }}
                >
                  Reject
                </Button>
                <Button size="sm" variant="primary" onClick={() => act.mutate({ path: `/candidates/${c.id}/approve` })}>
                  Add to knowledge base
                </Button>
              </div>
            </article>
          ))
        )}
      </section>

      {pendingDocs.length > 0 && (
        <section className="grid gap-3">
          <h2 className="font-semibold">Documents awaiting approval</h2>
          <ul className="divide-y divide-line rounded-lg border border-line">
            {pendingDocs.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <span className="min-w-0 flex-1">{d.title || d.filename}</span>
                <Button size="sm" variant="ghost" onClick={() => act.mutate({ path: `/documents/${d.id}/reject-pending` })}>
                  Reject
                </Button>
                <Button size="sm" variant="primary" onClick={() => act.mutate({ path: `/documents/${d.id}/approve` })}>
                  Approve
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="grid gap-3">
        <h2 className="font-semibold">Questions the knowledge base couldn't answer</h2>
        {!gaps.data?.length ? (
          <Empty title="No open gaps" />
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line">
            {gaps.data.map((g) => (
              <li key={g.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="w-full min-w-0 sm:w-auto sm:flex-1">
                  <p>{g.question}</p>
                  <p className="text-xs text-muted">
                    Asked {g.hit_count} {g.hit_count === 1 ? "time" : "times"}, last {ago(g.last_seen)}
                  </p>
                </div>
                <Button size="sm" variant="ghost" onClick={() => act.mutate({ path: `/gaps/${g.id}/reject` })}>
                  Dismiss
                </Button>
                <Button size="sm" onClick={() => act.mutate({ path: `/gaps/${g.id}/research` })}>
                  Research it
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
