import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronRight, SendHorizontal } from "lucide-react";
import type { FormSpec, JiraTicket, PlanSummary, RunMessage, RunSnapshot, ToolStep } from "../../lib/types";
import { cn, tryJson } from "../../lib/format";
import Markdown from "../../components/Markdown";
import { PlanCounts } from "../../components/PlanGlyph";
import { Button } from "../../components/ui";
import FormCard from "./FormCard";

const HIDDEN = new Set(["plan_preview", "tf_step", "internal_log", "approval", "discard"]);

/** Tool steps that ran between the previous message and this one. */
function stepsFor(messages: RunMessage[], steps: ToolStep[], index: number) {
  const until = messages[index].created_at;
  const since = index > 0 ? messages[index - 1].created_at : "";
  return steps.filter((s) => s.created_at > since && s.created_at <= until);
}

export default function Conversation({
  snapshot,
  working,
  sending,
  onSend,
  onOpenPlan,
  footer,
  hideLatestPlan,
}: {
  snapshot?: RunSnapshot;
  working: boolean;
  sending: boolean;
  onSend: (text: string) => void;
  onOpenPlan: () => void;
  footer?: ReactNode;
  hideLatestPlan?: boolean;
}) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const messages = useMemo(() => snapshot?.messages ?? [], [snapshot]);
  const steps = snapshot?.steps ?? [];
  const latestPlan = useMemo(() => messages.map((m) => m.type).lastIndexOf("plan_summary"), [messages]);

  const activity = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].type === "internal_log") return messages[i].content;
      if (messages[i].role === "user") break;
    }
    return null;
  }, [messages]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, working]);

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    onSend(t);
    setText("");
  };

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        <ol className="mx-auto grid max-w-3xl gap-5">
          {messages.map((m, i) => {
            if (HIDDEN.has(m.type)) {
              if (m.type === "approval" || m.type === "discard") {
                return (
                  <li key={i} className="text-sm text-muted">
                    {m.type === "approval" ? "You approved the plan." : "You discarded the plan."}
                  </li>
                );
              }
              return null;
            }
            // While a plan awaits approval, the approval bar stands in for its summary line.
            if (hideLatestPlan && i === latestPlan) return null;
            const answered = messages.slice(i + 1).some((n) => n.role === "user");
            return (
              <li key={i}>
                <Entry
                  message={m}
                  steps={m.role === "user" ? [] : stepsFor(messages, steps, i)}
                  answered={answered}
                  onSend={onSend}
                  onOpenPlan={onOpenPlan}
                />
              </li>
            );
          })}
          {working && (
            <li className="flex items-center gap-2 text-sm text-muted" aria-live="polite">
              <span className="size-1.5 animate-pulse rounded-full bg-teal" aria-hidden />
              {activity ?? "Working…"}
            </li>
          )}
        </ol>
        <div ref={endRef} />
      </div>

      <div className="mx-auto grid w-full max-w-3xl gap-3 px-5 pb-5">
        {footer}
        <form
          className="flex items-end gap-2 rounded-lg border border-line bg-surface p-2 focus-within:border-teal"
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
            placeholder={working ? "AEGIS is working. You can still add context." : "Reply, or ask for a change to the plan"}
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm focus:outline-none"
            aria-label="Message"
          />
          <Button type="submit" variant="primary" size="sm" busy={sending} disabled={!text.trim()} aria-label="Send">
            <SendHorizontal className="size-4" aria-hidden />
          </Button>
        </form>
      </div>
    </div>
  );
}

function Entry({
  message: m,
  steps,
  answered,
  onSend,
  onOpenPlan,
}: {
  message: RunMessage;
  steps: ToolStep[];
  answered: boolean;
  onSend: (t: string) => void;
  onOpenPlan: () => void;
}) {
  const mine = m.role === "user";

  if (m.type === "plan_summary") {
    const plan = tryJson<PlanSummary>(m.content);
    if (!plan) return null;
    return (
      <button
        onClick={onOpenPlan}
        className="flex w-full items-center gap-4 rounded-lg border border-line bg-surface px-4 py-3 text-left hover:border-teal"
      >
        <span className="font-medium">Terraform plan ready</span>
        <PlanCounts counts={plan.counts} className="text-sm" />
        <span className="ml-auto text-sm text-teal">Review</span>
      </button>
    );
  }

  if (m.type === "form") {
    const spec = tryJson<FormSpec>(m.content);
    return spec ? <FormCard spec={spec} answered={answered} onSubmit={onSend} /> : null;
  }

  if (m.type === "jira_tickets") {
    const data = tryJson<{ tickets: JiraTicket[] }>(m.content);
    return (
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-sm">
          <tbody>
            {data?.tickets.map((t) => (
              <tr key={t.key} className="border-b border-line last:border-0">
                <td className="px-3 py-2 font-mono text-xs whitespace-nowrap text-muted">{t.key}</td>
                <td className="px-3 py-2">{t.summary}</td>
                <td className="px-3 py-2 text-muted whitespace-nowrap">{t.status}</td>
                <td className="px-3 py-2 text-right">
                  <Button size="sm" onClick={() => onSend(`Execute ${t.key}`)}>
                    Run this ticket
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <article className={cn("grid gap-1.5", m.type === "error" && "rounded-lg border border-rust/40 bg-rust/5 p-4")}>
      <header className="flex items-baseline gap-2 text-xs">
        <span className={cn("font-semibold", mine ? "text-fg" : m.type === "error" ? "text-rust" : "text-teal")}>
          {mine ? "You" : "AEGIS"}
        </span>
      </header>
      <div className={cn("text-[15px]", mine && "text-fg/90")}>
        <Markdown>{m.content}</Markdown>
      </div>
      {steps.length > 0 && <Steps steps={steps} />}
    </article>
  );
}

const STEP_LABEL: Record<string, string> = {
  search_terraform_docs: "Searched docs",
  fetch_terraform_docs: "Read docs",
  write_tf_file: "Wrote",
  read_tf_file: "Read",
  list_workspace_files: "Listed files",
  start_terraform_plan: "Ran terraform plan",
  ask_user_form: "Asked you",
  fetch_jira_tickets: "Searched Jira",
  get_jira_ticket: "Read ticket",
  update_jira_ticket_status: "Updated ticket",
  create_folder: "Created folder",
};

function Steps({ steps }: { steps: ToolStep[] }) {
  return (
    <details className="group text-sm">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-muted hover:text-fg">
        <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
        {steps.length} {steps.length === 1 ? "step" : "steps"}
      </summary>
      <ol className="mt-2 grid gap-1 border-l border-line pl-4">
        {steps.map((s, i) => (
          <li key={i} className="text-muted">
            <span className="text-fg/80">{STEP_LABEL[s.name] ?? s.name}</span>
            {s.args && <span className="ml-2 font-mono text-xs break-all">{s.args}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}
