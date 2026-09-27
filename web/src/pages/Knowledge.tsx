import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Plus, Upload } from "lucide-react";
import AnimatedTabs from "@/components/smoothui/animated-tabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { request } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { KbAnswer, KbCandidate, KbDocument, KbGap, KbThread, RunMessage } from "@/lib/types";
import { ago, tryJson } from "@/lib/format";
import { cn } from "@/lib/utils";
import Markdown from "@/components/Markdown";
import AIMessage from "@/components/smoothui/ai-message";
import AILoader from "@/components/smoothui/ai-loader";
import AICitation from "@/components/smoothui/ai-citation";
import AISources from "@/components/smoothui/ai-sources";
import { Button, Empty, ErrorNote, Page, PageHeader, Panel, RowMenu, RowSkeleton } from "@/components/ui";
import { AegisAvatar, Composer } from "./runs/Conversation";

type View = "ask" | "documents" | "review";

export default function Knowledge() {
  const { identity } = useAuth();
  const [view, setView] = useState<View>("ask");
  const tabs = [
    { id: "ask", label: "Ask" },
    { id: "documents", label: "Documents" },
    ...(identity?.role === "admin" ? [{ id: "review", label: "Review" }] : []),
  ];
  return (
    <div className="flex w-full max-w-6xl flex-col px-4 sm:px-6 lg:px-10">
      <div className="grid gap-5 pt-6 sm:pt-10">
        <PageHeader title="Knowledge" description="Answers from your team's own runbooks and standards, with every source cited." />
        <AnimatedTabs variant="segment" tabs={tabs} activeTab={view} onChange={(id) => setView(id as View)} className="w-full sm:w-fit" />
      </div>
      {view === "ask" && <Ask />}
      {view === "documents" && <Documents />}
      {view === "review" && <Review />}
    </div>
  );
}

type KbSnapshot = { thread: KbThread; messages: RunMessage[]; is_running: boolean };

function Ask() {
  const qc = useQueryClient();
  const [threadId, setThreadId] = useState<string | null>(null);
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

  return (
    <div className="flex min-h-[calc(100dvh-16rem)] flex-col gap-6 pt-6 pb-28 md:pb-10">
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-sm text-muted">{snap.data?.thread.title || "New question"}</p>
        <div className="flex gap-1">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="ghost" disabled={!threads.data?.length}>
                <History aria-hidden /> History
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="glass max-h-80 w-72 overflow-y-auto rounded-xl p-1">
              <DropdownMenuLabel className="px-2.5 text-xs font-normal text-muted">Your questions</DropdownMenuLabel>
              {threads.data?.map((t) => (
                <DropdownMenuItem key={t.id} onSelect={() => setThreadId(t.id)} className="grid gap-0.5 rounded-lg px-2.5 py-2">
                  <span className="line-clamp-1">{t.title || "Untitled"}</span>
                  <span className="text-xs text-muted">{ago(t.updated_at)}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          {threadId && (
            <Button size="sm" variant="ghost" onClick={() => setThreadId(null)}>
              <Plus aria-hidden /> New
            </Button>
          )}
        </div>
      </div>

      <div className="grid flex-1 content-start gap-6">
        {!threadId && (
          <Empty title="Ask anything your documents cover">
            If they don't cover it, AEGIS says so, and asks before searching the web.
          </Empty>
        )}
        {snap.data?.messages.map((m, i) => <KbEntry key={i} message={m} />)}
        {snap.data?.is_running && (
          <div className="flex items-center gap-3" aria-live="polite">
            <AegisAvatar />
            <AILoader variant="dots" label="Searching the knowledge base" showElapsed />
          </div>
        )}
        <ErrorNote error={send.error} />
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-24 md:bottom-6">
        <Composer working={!!snap.data?.is_running || send.isPending} onSend={(t) => send.mutate(t)} placeholder="Ask about your runbooks and standards" />
      </div>
    </div>
  );
}

const host = (u: string) => {
  try {
    return new URL(u).hostname.replace(/^www\./, "");
  } catch {
    return u;
  }
};

function KbEntry({ message: m }: { message: RunMessage }) {
  if (m.role === "user") {
    return (
      <AIMessage from="user">
        <Markdown>{m.content}</Markdown>
      </AIMessage>
    );
  }
  const a = m.type === "kb_answer" ? tryJson<KbAnswer>(m.content) : null;
  const text = a ? a.answer : m.content;
  return (
    <AIMessage from="assistant" bubble={false} avatar={<AegisAvatar />} copyText={text}>
      <div className="grid gap-3">
        <div className="text-[15px] leading-relaxed">
          <Markdown>{text}</Markdown>
        </div>
        {a && (a.doc_citations.length > 0 || a.web_citations.length > 0) && (
          <div className="flex flex-wrap items-center gap-1.5">
            {a.doc_citations.map((c) => (
              <span key={c} className="rounded-full border border-line px-2.5 py-0.5 text-xs text-muted">
                {c}
              </span>
            ))}
            {a.web_citations.map((u, i) => (
              <AICitation key={u} label={i + 1} title={host(u)} url={u} description="From the web, not your documents" />
            ))}
          </div>
        )}
        {a && a.web_citations.length > 0 && (
          <AISources label="Web sources" sources={a.web_citations.map((u) => ({ id: u, title: host(u), url: u }))} />
        )}
        {a && (
          <p className={cn("text-xs", a.confidence === "low" ? "text-amber" : "text-muted")}>
            Confidence {a.confidence}
            {a.confidence_reason ? `: ${a.confidence_reason}` : ""}
          </p>
        )}
      </div>
    </AIMessage>
  );
}

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
    <Page className="max-w-none px-0 sm:px-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted">Markdown, text, PDF, Word, Excel or CSV, up to 10 MB each.</p>
        <label className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-lg bg-teal px-4 text-sm font-medium text-on-accent">
          <Upload className="size-4" aria-hidden />
          {upload.isPending ? "Uploading" : "Upload"}
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
      </div>
      <ErrorNote error={upload.error || remove.error} />
      {docs.isLoading ? (
        <RowSkeleton />
      ) : !docs.data?.length ? (
        <Empty title="No documents yet">Upload runbooks and standards so AEGIS can answer from them.</Empty>
      ) : (
        <Panel>
          <ul className="divide-y divide-line">
            {docs.data.map((d) => (
              <li key={d.id} className="flex items-center gap-4 px-5 py-4">
                <div className="min-w-0 flex-1">
                  <p className="truncate">{d.title || d.filename}</p>
                  <p className={cn("mt-0.5 text-sm", d.status === "failed" ? "text-rust" : d.status === "ready" ? "text-muted" : "text-amber")}>
                    {d.status === "ready" ? `${d.chunk_count} passages` : d.status === "pending_approval" ? "Awaiting approval" : d.status}
                    <span className="px-1.5 text-line">/</span>
                    <span className="text-muted">{ago(d.uploaded_at)}</span>
                  </p>
                </div>
                <RowMenu items={[{ label: "Delete", danger: true, onSelect: () => window.confirm(`Delete ${d.filename}?`) && remove.mutate(d.id) }]} />
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </Page>
  );
}

function Review() {
  const qc = useQueryClient();
  const kb = (p: string) => `/agent-api/knowledge-base${p}`;
  const candidates = useQuery({ queryKey: ["kb-candidates"], queryFn: () => request<{ candidates: KbCandidate[] }>(kb("/candidates")).then((r) => r.candidates) });
  const docs = useQuery({ queryKey: ["kb-docs"], queryFn: () => request<{ documents: KbDocument[] }>(kb("/documents")).then((r) => r.documents) });
  const gaps = useQuery({ queryKey: ["kb-gaps"], queryFn: () => request<{ gaps: KbGap[] }>(kb("/gaps")).then((r) => r.gaps.filter((g) => g.status === "open")) });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => request(kb(path), { method: "POST", body }),
    onSuccess: () => ["kb-candidates", "kb-docs", "kb-gaps"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })),
  });
  const pendingDocs = docs.data?.filter((d) => d.status === "pending_approval") ?? [];
  const nothing = !candidates.data?.length && !pendingDocs.length && !gaps.data?.length;

  return (
    <Page className="max-w-none px-0 sm:px-0">
      <p className="text-sm text-muted">Nothing becomes knowledge on its own. Approve what should.</p>
      <ErrorNote error={act.error} />
      {nothing && <Empty title="Nothing to review" />}

      {!!candidates.data?.length && (
        <section className="grid gap-3">
          <h2 className="text-sm font-medium text-muted">Researched answers</h2>
          {candidates.data.map((c) => (
            <Panel key={c.id} className="grid gap-4 p-5">
              <p className="font-medium">{c.question}</p>
              <div className="max-h-56 overflow-y-auto text-sm text-muted">
                <Markdown>{c.consolidated_answer}</Markdown>
              </div>
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
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
                  Add to knowledge
                </Button>
              </div>
            </Panel>
          ))}
        </section>
      )}

      {pendingDocs.length > 0 && (
        <section className="grid gap-3">
          <h2 className="text-sm font-medium text-muted">Documents awaiting approval</h2>
          <Panel>
            <ul className="divide-y divide-line">
              {pendingDocs.map((d) => (
                <li key={d.id} className="flex items-center gap-4 px-5 py-4">
                  <span className="min-w-0 flex-1 truncate">{d.title || d.filename}</span>
                  <RowMenu
                    items={[
                      { label: "Approve", onSelect: () => act.mutate({ path: `/documents/${d.id}/approve` }) },
                      { label: "Reject", danger: true, onSelect: () => act.mutate({ path: `/documents/${d.id}/reject-pending` }) },
                    ]}
                  />
                </li>
              ))}
            </ul>
          </Panel>
        </section>
      )}

      {!!gaps.data?.length && (
        <section className="grid gap-3">
          <h2 className="text-sm font-medium text-muted">Unanswered questions</h2>
          <Panel>
            <ul className="divide-y divide-line">
              {gaps.data.map((g) => (
                <li key={g.id} className="flex items-center gap-4 px-5 py-4">
                  <div className="min-w-0 flex-1">
                    <p className="line-clamp-2">{g.question}</p>
                    <p className="mt-0.5 text-sm text-muted">
                      Asked {g.hit_count} {g.hit_count === 1 ? "time" : "times"}
                    </p>
                  </div>
                  <RowMenu
                    items={[
                      { label: "Research it", onSelect: () => act.mutate({ path: `/gaps/${g.id}/research` }) },
                      { label: "Dismiss", onSelect: () => act.mutate({ path: `/gaps/${g.id}/reject` }) },
                    ]}
                  />
                </li>
              ))}
            </ul>
          </Panel>
        </section>
      )}
    </Page>
  );
}
