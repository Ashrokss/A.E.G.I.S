import { useMemo, useState } from "react";
import AIMessage from "@/components/smoothui/ai-message";
import AIReasoning from "@/components/smoothui/ai-reasoning";
import AIToolCall from "@/components/smoothui/ai-tool-call";
import AILoader from "@/components/smoothui/ai-loader";
import AIPromptInput from "@/components/smoothui/ai-prompt-input";
import type { FormSpec, JiraTicket, PlanSummary, RunMessage, RunSnapshot, ToolStep } from "@/lib/types";
import { tryJson } from "@/lib/format";
import Markdown from "@/components/Markdown";
import Mark from "@/components/Mark";
import { PlanCounts } from "@/components/PlanGlyph";
import { Button } from "@/components/ui";
import FormCard from "./FormCard";

const HIDDEN = new Set(["plan_preview", "tf_step", "internal_log", "approval", "discard"]);

/** Server timestamps are naive UTC ISO strings. */
const ts = (s: string) => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`).getTime();

function stepsFor(messages: RunMessage[], steps: ToolStep[], index: number) {
  const until = messages[index].created_at;
  const since = index > 0 ? messages[index - 1].created_at : "";
  return steps.filter((s) => s.created_at > since && s.created_at <= until);
}

/** The AEGIS agent's avatar: the brand mark in a quiet circle. */
export const AegisAvatar = () => (
  <span className="grid size-7 shrink-0 place-items-center rounded-full border border-line bg-surface text-teal">
    <Mark className="size-4" />
  </span>
);

export default function Conversation({
  snapshot,
  working,
  hideLatestPlan,
  onSend,
  onOpenPlan,
}: {
  snapshot?: RunSnapshot;
  working: boolean;
  hideLatestPlan?: boolean;
  onSend: (text: string) => void;
  onOpenPlan: () => void;
}) {
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

  return (
    <ol className="grid gap-6">
      {messages.map((m, i) => {
        if (m.type === "approval" || m.type === "discard") {
          return (
            <li key={i} className="text-center text-xs text-muted">
              {m.type === "approval" ? "You approved the plan" : "You discarded the plan"}
            </li>
          );
        }
        if (HIDDEN.has(m.type)) return null;
        if (hideLatestPlan && i === latestPlan) return null;
        return (
          <li key={i}>
            <Entry
              message={m}
              steps={m.role === "user" ? [] : stepsFor(messages, steps, i)}
              since={i > 0 ? messages[i - 1].created_at : m.created_at}
              answered={messages.slice(i + 1).some((n) => n.role === "user")}
              onSend={onSend}
              onOpenPlan={onOpenPlan}
            />
          </li>
        );
      })}
      {working && (
        <li className="flex items-center gap-3" aria-live="polite">
          <AegisAvatar />
          <AILoader variant="dots" label={activity ?? "Working"} showElapsed />
        </li>
      )}
    </ol>
  );
}

/** Message box. While the agent works an empty box offers Stop; typing turns it back into Send (add context). */
export function Composer({
  working,
  onSend,
  onStop,
  placeholder,
}: {
  working: boolean;
  onSend: (t: string) => void;
  onStop?: () => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  return (
    <AIPromptInput
      value={draft}
      onValueChange={setDraft}
      state={working ? (onStop && !draft.trim() ? "streaming" : "thinking") : "idle"}
      onSubmit={(v) => {
        setDraft("");
        onSend(v);
      }}
      onStop={onStop}
      placeholder={placeholder ?? (working ? "Add context while AEGIS works" : "Reply, or ask for a change")}
    />
  );
}

function Entry({
  message: m,
  steps,
  since,
  answered,
  onSend,
  onOpenPlan,
}: {
  message: RunMessage;
  steps: ToolStep[];
  since: string;
  answered: boolean;
  onSend: (t: string) => void;
  onOpenPlan: () => void;
}) {
  if (m.role === "user") {
    return (
      <AIMessage from="user">
        <Markdown>{m.content}</Markdown>
      </AIMessage>
    );
  }

  if (m.type === "plan_summary") {
    const plan = tryJson<PlanSummary>(m.content);
    if (!plan) return null;
    return (
      <button
        onClick={onOpenPlan}
        className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 rounded-2xl border border-line bg-surface px-5 py-4 text-left transition-colors hover:border-teal/50"
      >
        <span className="font-medium">Terraform plan</span>
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
      <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-surface">
        {data?.tickets.map((t) => (
          <li key={t.key} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5">
            <div className="min-w-0 flex-1 basis-48">
              <p className="font-mono text-xs text-muted">{t.key}</p>
              <p className="text-sm">{t.summary}</p>
            </div>
            <Button size="sm" onClick={() => onSend(`Execute ${t.key}`)}>
              Run this ticket
            </Button>
          </li>
        ))}
      </ul>
    );
  }

  if (m.type === "error") {
    return (
      <div className="flex gap-3">
        <AegisAvatar />
        <div className="min-w-0 flex-1 rounded-2xl border border-rust/30 bg-rust/[0.06] p-4 text-[15px] leading-relaxed">
          <Markdown>{m.content}</Markdown>
        </div>
      </div>
    );
  }

  const seconds = steps.length ? Math.max(0.1, (ts(m.created_at) - ts(since)) / 1000) : undefined;
  return (
    <div className="grid gap-3">
      {steps.length > 0 && (
        <div className="pl-10">
          <AIReasoning duration={seconds}>
            <div className="grid gap-2">
              {steps.map((s, i) => (
                <AIToolCall key={i} name={STEP_LABEL[s.name] ?? s.name} summary={s.args || undefined} status="success" />
              ))}
            </div>
          </AIReasoning>
        </div>
      )}
      <AIMessage from="assistant" bubble={false} avatar={<AegisAvatar />} copyText={m.content}>
        <div className="text-[15px] leading-relaxed">
          <Markdown>{m.content}</Markdown>
        </div>
      </AIMessage>
    </div>
  );
}

const STEP_LABEL: Record<string, string> = {
  search_terraform_docs: "Searched the docs",
  fetch_terraform_docs: "Read the docs",
  write_tf_file: "Wrote a file",
  read_tf_file: "Read a file",
  list_workspace_files: "Listed files",
  start_terraform_plan: "Ran terraform plan",
  ask_user_form: "Asked you",
  fetch_jira_tickets: "Searched Jira",
  get_jira_ticket: "Read the ticket",
  update_jira_ticket_status: "Updated the ticket",
  create_folder: "Created a folder",
};
