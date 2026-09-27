export type Role = "user" | "admin";

export interface Identity {
  id: string;
  email: string;
  role: Role;
  enabled: boolean;
  lastLogin: string | null;
  createdAt: string;
}

export type RunStatus =
  | "PENDING_INTAKE"
  | "IDLE"
  | "AWAITING_INPUT"
  | "PLANNING"
  | "AWAITING_APPROVAL"
  | "APPLYING"
  | "REPAIRING"
  | "CHECKING_DRIFT"
  | "COMPLETED"
  | "FAILED";

export interface RunSummary {
  id: string;
  status: RunStatus;
  title: string;
  goal: string;
  created_at: string;
  updated_at: string;
  jira_ticket_key: string | null;
  aws_account_id: string | null;
}

export interface RunMessage {
  role: string;
  content: string;
  type: string;
  created_at: string;
  step?: string | null;
}

export interface ToolStep {
  created_at: string;
  name: string;
  args?: string;
  thought?: string;
}

export interface RunSnapshot {
  run: {
    id: string;
    status: RunStatus;
    goal: string;
    created_at: string;
    updated_at: string;
    last_message: string | null;
    jira_ticket_key: string | null;
  };
  messages: RunMessage[];
  steps: ToolStep[];
}

export interface PlanChange {
  address: string;
  type: string;
  name: string;
  actions: string[];
  kind?: string;
}

/** A `drift_report` message: the account compared with the run's state. */
export interface DriftReport {
  ok: boolean;
  error?: string;
  tracked?: number;
  drifted?: Array<{ address: string; kind: "changed" | "deleted"; attributes: string[] }>;
  checked_at: string;
}

export interface PlanSummary {
  counts: { add: number; change: number; destroy: number; replace: number };
  changes: PlanChange[];
}

export interface FormField {
  name: string;
  label: string;
  type: "select" | "multiselect" | "text" | "checkbox";
  options?: string[];
  default?: string | string[] | boolean;
  help?: string;
}

export interface FormSpec {
  title: string;
  description?: string;
  fields: FormField[];
}

export interface JiraTicket {
  key: string;
  summary: string;
  status?: string;
  priority?: string;
  issue_type?: string;
}

export interface InboxItem {
  id: string;
  ticket_key: string;
  summary: string;
  created_at: string;
  status: RunStatus;
  state: "pending" | "active";
}

export interface AwsAccount {
  id: string;
  status: string;
  accountId: string;
  roleArn: string;
  /** Where this account's Terraform state lives (created by the onboarding stack). */
  stateBucket: string | null;
  displayName: string | null;
  createdAt: string;
}

export interface Connection {
  id: string;
  provider: "github" | "jira";
  authMethod: string;
  displayName: string | null;
  status: string;
  lastError: string | null;
  lastValidatedAt: string | null;
  createdAt: string;
  config: Record<string, unknown>;
}

export interface KbThread {
  id: string;
  title: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface KbAnswer {
  answer: string;
  doc_citations: string[];
  web_citations: string[];
  confidence: string;
  confidence_reason?: string;
}

export interface KbDocument {
  id: string;
  filename: string;
  title: string | null;
  status: string;
  error: string | null;
  uploaded_at: string;
  chunk_count: number;
}

export interface KbCandidate {
  id: string;
  question: string;
  consolidated_answer: string;
  web_citations: string;
  created_by: string | null;
  created_at: string;
}

export interface KbGap {
  id: string;
  question: string;
  reason: string;
  hit_count: number;
  status: string;
  last_seen: string;
}
