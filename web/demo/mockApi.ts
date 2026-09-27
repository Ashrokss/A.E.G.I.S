/**
 * Demo mode: an in-memory stand-in for the api (:3005) and agent (:8000), so
 * the UI can be explored with no backend, AWS or Cognito.
 *
 *   npm run demo        -> http://localhost:5173 (Sign in logs you in as a demo admin)
 *
 * Loaded ONLY by `vite --mode demo` (see vite.config.ts); never part of a
 * normal build. State lives in memory and resets when the server restarts.
 */
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Connect, Plugin } from "vite";

const iso = (minutesAgo = 0) => new Date(Date.now() - minutesAgo * 60_000).toISOString().slice(0, 19);
const uuid = () => crypto.randomUUID();

type Msg = { role: string; type: string; content: string; created_at: string };
type Run = {
  id: string;
  status: string;
  goal: string;
  created_at: string;
  updated_at: string;
  jira_ticket_key: string | null;
  aws_account_id: string;
  messages: Msg[];
  steps: Array<{ created_at: string; name: string; args: string }>;
};

const PLAN = {
  counts: { add: 4, change: 1, destroy: 1, replace: 0 },
  changes: [
    ["aws_s3_bucket.artifacts", ["create"]],
    ["aws_s3_bucket_versioning.artifacts", ["create"]],
    ["aws_s3_bucket_server_side_encryption_configuration.artifacts", ["create"]],
    ["aws_s3_bucket_lifecycle_configuration.artifacts", ["create"]],
    ["aws_iam_role_policy.ci_publish", ["update"]],
    ["aws_s3_bucket.legacy_builds", ["delete"]],
  ].map(([address, actions]) => ({ address, type: "", name: "", actions })),
};

const FILES: Record<string, string> = {
  "environments/prod/artifacts.tf": `resource "aws_s3_bucket" "artifacts" {
  bucket = "acme-build-artifacts"
  tags   = local.tags
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-after-90-days"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
  }
}
`,
  "environments/prod/providers.tf": `terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}
`,
};

function seedRuns(): Run[] {
  const first: Run = {
    id: "demo-s3-artifacts",
    status: "AWAITING_APPROVAL",
    goal: "A private S3 bucket for build artifacts, versioned and encrypted, with objects expiring after 90 days",
    created_at: iso(14),
    updated_at: iso(6),
    jira_ticket_key: "OPS-142",
    aws_account_id: "123456789012",
    messages: [
      { role: "user", type: "text", created_at: iso(14), content: "A private S3 bucket for build artifacts, versioned and encrypted, with objects expiring after 90 days" },
      {
        role: "agent",
        type: "form",
        created_at: iso(13),
        content: JSON.stringify({
          title: "A few details before I write the Terraform",
          description: "Your repository already has an `environments/` layout, so I will follow it.",
          fields: [
            { name: "env", label: "Environment", type: "select", options: ["dev", "staging", "prod"] },
            { name: "retire", label: "Remove the old legacy-builds bucket", type: "checkbox", default: true },
          ],
        }),
      },
      { role: "user", type: "text", created_at: iso(12), content: "- **Environment**: prod\n- **Remove the old legacy-builds bucket**: yes" },
      { role: "agent", type: "tf_step", created_at: iso(9), content: JSON.stringify({ step: "init", status: "succeeded" }) },
      { role: "agent", type: "tf_step", created_at: iso(8), content: JSON.stringify({ step: "validate", status: "succeeded" }) },
      { role: "agent", type: "tf_step", created_at: iso(7), content: JSON.stringify({ step: "plan", status: "succeeded" }) },
      {
        role: "agent",
        type: "plan_preview",
        created_at: iso(7),
        content: 'Plan results:\n  # aws_s3_bucket.artifacts will be created\n  + resource "aws_s3_bucket" "artifacts" {\n      + bucket = "acme-build-artifacts"\n    }\n\nPlan: 4 to add, 1 to change, 1 to destroy.',
      },
      {
        role: "agent",
        type: "text",
        created_at: iso(6.5),
        content:
          "I wrote the bucket in `environments/prod/artifacts.tf` with versioning, SSE-S3 encryption, a 90-day expiry rule and public access blocked. The CI role gets `s3:PutObject` on the new bucket only.\n\nThe old `legacy-builds` bucket is **destroyed** in this plan, as you asked. It must be empty first.",
      },
      { role: "agent", type: "plan_summary", created_at: iso(6), content: JSON.stringify(PLAN) },
    ],
    steps: [
      { created_at: iso(11), name: "search_terraform_docs", args: "query: aws_s3_bucket_lifecycle_configuration expiration" },
      { created_at: iso(10.5), name: "fetch_terraform_docs", args: "url: https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_lifecycle_configuration" },
      { created_at: iso(10), name: "write_tf_file", args: "filename: environments/prod/artifacts.tf" },
      { created_at: iso(9.5), name: "start_terraform_plan", args: "" },
    ],
  };
  const simple = (id: string, status: string, goal: string, ago: number): Run => ({
    id,
    status,
    goal,
    created_at: iso(ago + 5),
    updated_at: iso(ago),
    jira_ticket_key: null,
    aws_account_id: "123456789012",
    messages: [
      { role: "user", type: "text", created_at: iso(ago + 5), content: goal },
      {
        role: "agent",
        type: status === "FAILED" ? "error" : "text",
        created_at: iso(ago),
        content:
          status === "FAILED"
            ? "**Plan failed.** The subnet group needs subnets in at least two availability zones. Tell me which VPC to use and I'll plan again."
            : status === "COMPLETED"
              ? "Applied. The VPC, four subnets and route tables now exist, and I opened a pull request with the code."
              : "Writing the Terraform now.",
      },
    ],
    steps: [],
  });
  return [
    first,
    simple("demo-sqs", "PLANNING", "An SQS queue with a dead-letter queue and an SNS topic that fans out to it", 2),
    simple("demo-vpc", "COMPLETED", "A VPC with public and private subnets across two availability zones", 60 * 19),
    simple("demo-rds", "FAILED", "RDS Postgres for the reporting service", 60 * 48),
  ];
}

const db = {
  me: { id: "demo-admin", email: "you@example.com", role: "admin", enabled: true, lastLogin: iso(), createdAt: iso(60 * 24 * 30), cognitoSub: "demo" },
  runs: seedRuns(),
  inbox: [{ id: "demo-intake", ticket_key: "OPS-151", summary: "CloudFront distribution for the docs site", created_at: iso(35), status: "PENDING_INTAKE", state: "pending" }],
  accounts: [
    { id: uuid(), status: "active", accountId: "123456789012", roleArn: "arn:aws:iam::123456789012:role/AegisProvisioner-default", displayName: "Sandbox", createdAt: iso(60 * 24 * 3) },
  ] as Array<{ id: string; status: string; accountId: string; roleArn: string; displayName: string | null; createdAt: string }>,
  connections: [
    { id: "github_demo", provider: "github", authMethod: "token", displayName: "acme/infrastructure", status: "active", lastError: null, lastValidatedAt: iso(55), createdAt: iso(60 * 24 * 2), config: {} },
  ] as Array<Record<string, unknown>>,
  users: [] as Array<Record<string, unknown>>,
  threads: [] as Array<{ id: string; title: string | null; status: string; created_at: string; updated_at: string; messages: Msg[]; running: boolean }>,
  documents: [
    { id: uuid(), filename: "tagging-standard.md", title: "Tagging standard", status: "ready", error: null, uploaded_at: iso(60 * 24 * 5), chunk_count: 12 },
    { id: uuid(), filename: "network-runbook.pdf", title: "Network runbook", status: "ready", error: null, uploaded_at: iso(60 * 24 * 9), chunk_count: 41 },
  ],
};
db.users = [db.me, { id: "demo-member", cognitoSub: "demo-2", email: "teammate@example.com", role: "user", enabled: true, lastLogin: iso(60 * 5), createdAt: iso(60 * 24 * 10) }];

function send(res: ServerResponse, body: unknown, status = 200) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
}

async function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  try {
    return JSON.parse(Buffer.concat(chunks).toString() || "{}");
  } catch {
    return {};
  }
}

const later = (ms: number, fn: () => void) => setTimeout(fn, ms);
const touch = (run: Run, status?: string) => {
  if (status) run.status = status;
  run.updated_at = iso();
};
const say = (run: Run, content: string, type = "text", role = "agent") =>
  run.messages.push({ role, type, content, created_at: iso() });

async function handle(req: IncomingMessage, res: ServerResponse) {
  const method = req.method ?? "GET";
  const path = (req.url ?? "").split("?")[0];
  const m = (re: RegExp) => path.match(re);
  let g: RegExpMatchArray | null;

  // ── identity ──
  if (path === "/api/auth/me" || path === "/api/auth/authorize") return send(res, { user: db.me });
  if (path.startsWith("/auth/aws/")) return send(res, { status: "ok" });

  // ── runs ──
  if (path === "/agent-api/runs" && method === "GET") {
    return send(res, {
      runs: [...db.runs]
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
        .map((r) => ({ id: r.id, status: r.status, goal: r.goal, title: r.goal.split("\n")[0], created_at: r.created_at, updated_at: r.updated_at, jira_ticket_key: r.jira_ticket_key, aws_account_id: r.aws_account_id })),
    });
  }
  if (path === "/agent-api/runs" && method === "POST") {
    const body = await readBody(req);
    const goal = String(body.goal || "New run");
    const run: Run = { id: uuid(), status: "IDLE", goal, created_at: iso(), updated_at: iso(), jira_ticket_key: null, aws_account_id: String(body.aws_account_id || ""), messages: [], steps: [] };
    say(run, goal, "text", "user");
    db.runs.push(run);
    later(1500, () => {
      say(run, "Reading the request and checking the AWS provider docs…", "internal_log");
      touch(run);
    });
    later(3500, () => {
      say(run, "This is **demo mode**, so I won't write real Terraform. In a real run I would ask any questions I need here, write the configuration, then show you the plan to approve.");
      touch(run, "AWAITING_INPUT");
    });
    return send(res, { id: run.id, status: "IDLE", goal, created_at: run.created_at });
  }
  if (path === "/agent-api/runs/inbox") return send(res, db.inbox);

  if ((g = m(/^\/agent-api\/runs\/([^/]+)\/attach$/))) {
    const item = db.inbox.find((i) => i.id === g![1]);
    if (!item) return send(res, { detail: "Run not found" }, 404);
    db.inbox = db.inbox.filter((i) => i !== item);
    const run: Run = { id: item.id, status: "AWAITING_INPUT", goal: `Execute ${item.ticket_key}: ${item.summary}`, created_at: item.created_at, updated_at: iso(), jira_ticket_key: item.ticket_key, aws_account_id: "123456789012", messages: [], steps: [] };
    say(run, run.goal, "text", "user");
    say(run, "Started. In a real run I'd read the ticket, ask what's missing, and plan the change. (Demo mode.)");
    db.runs.push(run);
    return send(res, { id: run.id, status: "IDLE" });
  }

  if ((g = m(/^\/agent-api\/runs\/([^/]+)(\/.*)?$/))) {
    const run = db.runs.find((r) => r.id === g![1]);
    if (!run) return send(res, { detail: "Run not found" }, 404);
    const rest = g[2] ?? "";

    if (rest === "/snapshot") {
      return send(res, {
        run: { id: run.id, status: run.status, goal: run.goal, created_at: run.created_at, updated_at: run.updated_at, last_message: null, jira_ticket_key: run.jira_ticket_key },
        messages: run.messages,
        steps: run.steps,
      });
    }
    if (rest === "/files") return send(res, { files: run.id === "demo-s3-artifacts" ? Object.keys(FILES).map((name) => ({ name, size: FILES[name].length })) : [] });
    if (rest.startsWith("/files/")) {
      const name = decodeURIComponent(rest.slice("/files/".length));
      return FILES[name] ? send(res, { name, content: FILES[name] }) : send(res, { detail: "File not found" }, 404);
    }
    if (rest === "/stream") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      const lines = ["INFO: terraform init", "STDOUT: Initializing provider plugins...", "STDOUT: - hashicorp/aws v6.14.0", "INFO: terraform plan", "STDOUT: Plan: 4 to add, 1 to change, 1 to destroy.", "EXIT: 0", "DONE"];
      for (const line of lines) {
        res.write(`data: ${line}\n\n`);
        await new Promise((r) => setTimeout(r, 120));
      }
      return res.end();
    }
    if (rest === "/download") return send(res, { detail: "Downloads are not available in demo mode." }, 501);
    if (rest === "/messages" && method === "POST") {
      const body = await readBody(req);
      say(run, String(body.content || ""), "text", "user");
      touch(run, "IDLE");
      later(1800, () => {
        say(run, "Noted. In a real run I'd update the Terraform and plan again. (Demo mode.)");
        touch(run, "AWAITING_INPUT");
      });
      return send(res, { status: "received" });
    }
    if (rest === "/approve") {
      say(run, "Approved. Proceed with apply.", "approval", "user");
      touch(run, "APPLYING");
      later(3000, () => {
        say(run, "Applied: **4 added, 1 changed, 1 destroyed.** I opened a pull request on `acme/infrastructure` with the code.");
        touch(run, "COMPLETED");
      });
      return send(res, { status: "approved" });
    }
    if (rest === "/discard") {
      say(run, "", "discard", "user");
      say(run, "Plan discarded. What would you like to change?");
      touch(run, "AWAITING_INPUT");
      return send(res, { status: "discarded" });
    }
    if (rest === "/stop") {
      touch(run, "AWAITING_INPUT");
      return send(res, { status: "stopping" });
    }
    if (rest === "/push-to-github") {
      say(run, "Pushed to `acme/infrastructure` on branch `provisioning/demo`. Pull request opened. (Demo mode.)");
      return send(res, { ok: true, url: "https://github.com/", branch: "provisioning/demo", repo: "acme/infrastructure", applied: false });
    }
  }

  if ((g = m(/^\/agent-api\/repo-analysis/))) return send(res, { status: "ready", repo: "acme/infrastructure" });

  // ── AWS accounts ──
  if (path === "/api/aws-accounts" && method === "GET") return send(res, { accounts: db.accounts });
  if (path === "/api/aws-accounts/whoami") return send(res, { accountId: "987654321098" });
  if (path === "/api/aws-accounts/start") {
    return send(res, { onboardingId: uuid(), externalId: "demo" + uuid().replace(/-/g, "").slice(0, 28), launchUrl: null, trustedRoleArn: "arn:aws:iam::987654321098:role/aegis-task" });
  }
  if (path === "/api/aws-accounts/complete") {
    const body = await readBody(req);
    const roleArn = String(body.roleArn || "");
    const accountId = /::(\d{12}):/.exec(roleArn)?.[1];
    if (!accountId) return send(res, { error: "Not a valid IAM role ARN (arn:aws:iam::<account>:role/<name>)." }, 400);
    const account = { id: uuid(), status: "active", accountId, roleArn, displayName: String(body.displayName || "") || null, createdAt: iso() };
    db.accounts.push(account);
    return send(res, { ok: true, account });
  }
  if ((g = m(/^\/api\/aws-accounts\/(\d{12})$/)) && method === "DELETE") {
    db.accounts = db.accounts.filter((a) => a.accountId !== g![1]);
    return send(res, { ok: true });
  }

  // ── integrations ──
  if (path === "/api/integrations") return send(res, { connections: db.connections });
  if (path === "/api/integrations/github/repos/lookup") return send(res, { connectedAs: "you", repos: [{ fullName: "acme/infrastructure" }, { fullName: "acme/platform" }] });
  if ((g = m(/^\/api\/integrations\/(github|jira)\/token$/))) {
    const body = await readBody(req);
    const provider = g[1];
    const connection = {
      id: `${provider}_${uuid().slice(0, 8)}`,
      provider,
      authMethod: "token",
      displayName: provider === "github" ? String(body.repoFullName) : String(body.baseUrl || "jira").replace(/^https:\/\//, ""),
      status: "active",
      lastError: null,
      lastValidatedAt: iso(),
      createdAt: iso(),
      config: {},
    };
    db.connections.push(connection);
    return send(res, { connection }, 201);
  }
  if ((g = m(/^\/api\/integrations\/(github|jira)\/([^/]+)\/test$/))) return send(res, { ok: true, connectedAs: "you" });
  if ((g = m(/^\/api\/integrations\/(github|jira)\/([^/]+)$/)) && method === "DELETE") {
    db.connections = db.connections.filter((c) => c.id !== g![2]);
    return send(res, { ok: true });
  }

  // ── people ──
  if (path === "/api/users" && method === "GET") return send(res, { users: db.users });
  if (path === "/api/users" && method === "POST") {
    const body = await readBody(req);
    const user = { id: uuid(), cognitoSub: uuid(), email: String(body.email), role: String(body.role || "user"), enabled: true, lastLogin: null, createdAt: iso() };
    db.users.push(user);
    return send(res, { ok: true, user, temporaryPassword: "Demo-" + uuid().slice(0, 8) + "!" }, 201);
  }
  if ((g = m(/^\/api\/users\/([^/]+)$/)) && method === "PATCH") {
    const user = db.users.find((u) => u.cognitoSub === decodeURIComponent(g![1]));
    if (user) user.role = "admin";
    return send(res, { ok: true, user });
  }

  // ── knowledge base ──
  if (path === "/agent-api/knowledge-base/documents" && method === "GET") return send(res, { documents: db.documents });
  if (path === "/agent-api/knowledge-base/documents" && method === "POST") {
    db.documents.unshift({ id: uuid(), filename: "uploaded-document.md", title: "Uploaded document", status: "ready", error: null, uploaded_at: iso(), chunk_count: 7 });
    return send(res, { ok: true });
  }
  if ((g = m(/^\/agent-api\/knowledge-base\/documents\/([^/]+)$/)) && method === "DELETE") {
    db.documents = db.documents.filter((d) => d.id !== g![1]);
    return send(res, { ok: true });
  }
  if (path === "/agent-api/knowledge-base/candidates") return send(res, { candidates: [] });
  if (path === "/agent-api/knowledge-base/gaps") return send(res, { gaps: [] });
  if (path.startsWith("/agent-api/knowledge-base/")) return send(res, { ok: true });

  if (path === "/agent-api/kb-chat/threads" && method === "GET") {
    return send(res, { threads: db.threads.map(({ messages: _m, running: _r, ...t }) => t) });
  }
  if (path === "/agent-api/kb-chat/threads" && method === "POST") {
    const thread = { id: uuid(), title: null, status: "open", created_at: iso(), updated_at: iso(), messages: [], running: false };
    db.threads.unshift(thread);
    return send(res, { id: thread.id, status: "open" });
  }
  if ((g = m(/^\/agent-api\/kb-chat\/threads\/([^/]+)\/(snapshot|messages|stop|stream)$/))) {
    const thread = db.threads.find((t) => t.id === g![1]);
    if (!thread) return send(res, { detail: "Thread not found" }, 404);
    if (g[2] === "snapshot") {
      const { messages, running, ...t } = thread;
      return send(res, { thread: t, messages, is_running: running, steps: [] });
    }
    if (g[2] === "messages") {
      const body = await readBody(req);
      const content = String(body.content || "");
      thread.messages.push({ role: "user", type: "text", content, created_at: iso() });
      thread.title ??= content.slice(0, 80);
      thread.running = true;
      later(2000, () => {
        thread.messages.push({
          role: "agent",
          type: "kb_answer",
          created_at: iso(),
          content: JSON.stringify({
            answer:
              "Production resources must carry `Environment`, `Owner`, `CostCenter` and `ManagedBy` tags, applied through the provider's `default_tags` block [tagging-standard · Required tags]. Anything created by AEGIS also gets `ManagedBy = AEGIS`.",
            doc_citations: ["tagging-standard · Required tags"],
            web_citations: ["https://docs.aws.amazon.com/tag-editor/latest/userguide/tagging.html"],
            confidence: "high",
            confidence_reason: "answered directly by the tagging standard",
          }),
        });
        thread.running = false;
        thread.updated_at = iso();
      });
      return send(res, { status: "received" });
    }
    return send(res, { ok: true });
  }

  send(res, { detail: `Demo mode has no mock for ${method} ${path}` }, 404);
}

const middleware: Connect.NextHandleFunction = (req, res, next) => {
  const path = (req.url ?? "").split("?")[0];
  if (!/^\/(api|agent-api|auth\/aws)\//.test(path)) return next();
  handle(req, res).catch((err) => send(res, { detail: String(err) }, 500));
};

export function demoApi(): Plugin {
  return {
    name: "aegis-demo-api",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}
