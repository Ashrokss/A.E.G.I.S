import { useMemo, useState } from "react";
import AIMessage from "@/components/smoothui/ai-message";
import AIReasoning from "@/components/smoothui/ai-reasoning";
import AIToolCall from "@/components/smoothui/ai-tool-call";
import AILoader from "@/components/smoothui/ai-loader";
import AIPromptInput from "@/components/smoothui/ai-prompt-input";
import type { DriftReport, FormSpec, JiraTicket, PlanSummary, RunMessage, RunSnapshot, ToolStep } from "@/lib/types";
import { ago, tryJson } from "@/lib/format";
import Markdown from "@/components/Markdown";
import Mark from "@/components/Mark";
import PlanGlyph, { PlanCounts } from "@/components/PlanGlyph";
import { Button, ErrorNote } from "@/components/ui";
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
  onFixDrift,
}: {
  snapshot?: RunSnapshot;
  working: boolean;
  hideLatestPlan?: boolean;
  onSend: (text: string) => void;
  onOpenPlan: () => void;
  onFixDrift?: () => void;
}) {
  const messages = useMemo(() => snapshot?.messages ?? [], [snapshot]);
  const steps = snapshot?.steps ?? [];
  const latestPlan = useMemo(() => messages.map((m) => m.type).lastIndexOf("plan_summary"), [messages]);
  const latestDrift = useMemo(() => messages.map((m) => m.type).lastIndexOf("drift_report"), [messages]);

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
              onFixDrift={i === latestDrift && !working ? onFixDrift : undefined}
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
  onFixDrift,
}: {
  message: RunMessage;
  steps: ToolStep[];
  since: string;
  answered: boolean;
  onSend: (t: string) => void;
  onOpenPlan: () => void;
  onFixDrift?: () => void;
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

  if (m.type === "drift_report") {
    const d = tryJson<DriftReport>(m.content);
    return d ? <DriftCard report={d} onFix={onFixDrift} /> : null;
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

/** The account compared with the run's state (terraform plan -refresh-only). */
function DriftCard({ report: d, onFix }: { report: DriftReport; onFix?: () => void }) {
  if (!d.ok) {
    return <ErrorNote error={new Error(d.error || "The drift check could not run.")} />;
  }
  const drifted = d.drifted ?? [];
  if (drifted.length === 0) {
    return (
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-2xl border border-line bg-surface px-5 py-4">
        <span className="font-medium">No drift</span>
        <span className="text-sm text-muted">
          {d.tracked === 1 ? "The 1 resource matches" : `All ${d.tracked ?? 0} resources match`} the state. Checked {ago(d.checked_at)}.
        </span>
      </div>
    );
  }
  return (
    <div className="grid gap-3 rounded-2xl border border-amber/35 bg-amber/[0.06] p-4 sm:p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-medium">
          {drifted.length} of {d.tracked ?? drifted.length} {d.tracked === 1 ? "resource" : "resources"} changed outside Terraform
        </p>
        <span className="text-xs text-muted">Checked {ago(d.checked_at)}</span>
      </div>
      <ul className="grid gap-2">
        {drifted.map((r) => (
          <li key={r.address} className="grid grid-cols-[1.25rem_minmax(0,1fr)] items-baseline gap-2">
            <PlanGlyph action={r.kind === "deleted" ? "delete" : "update"} className="text-lg leading-none" />
            <div className="min-w-0">
              <p className="font-mono text-[13px] break-all">{r.address}</p>
              <p className="text-xs text-muted">
                {r.kind === "deleted" ? "No longer exists in AWS" : `Changed: ${r.attributes.join(", ") || "attributes"}`}
              </p>
            </div>
          </li>
        ))}
      </ul>
      {onFix && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-1">
          <Button size="sm" variant="primary" onClick={onFix}>
            Plan a fix
          </Button>
          <span className="text-xs text-muted">Puts them back to what the code says. Or ask me to update the code to match AWS.</span>
        </div>
      )}
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
