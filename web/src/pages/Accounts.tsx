import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Plus } from "lucide-react";
import { request } from "@/lib/api";
import { useAccounts } from "@/lib/hooks";
import { useAuth } from "@/lib/auth";
import { ago } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button, Dialog, Empty, ErrorNote, Field, Page, PageHeader, Panel, RowMenu, RowSkeleton, inputClass } from "@/components/ui";

type Start = { onboardingId: string; externalId: string; launchUrl: string | null; trustedRoleArn: string | null };

export default function Accounts() {
  const qc = useQueryClient();
  const accounts = useAccounts();
  const { identity } = useAuth();
  const isAdmin = identity?.role === "admin";
  // The operator's own account id is not something clients need to see.
  const whoami = useQuery({
    queryKey: ["whoami"],
    enabled: isAdmin,
    queryFn: () => request<{ accountId: string | null }>("/api/aws-accounts/whoami"),
  });
  const [onboarding, setOnboarding] = useState(false);
  const remove = useMutation({
    mutationFn: (accountId: string) => request(`/api/aws-accounts/${accountId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["aws-accounts"] }),
  });

  return (
    <Page>
      <PageHeader
        title="AWS accounts"
        description="AEGIS runs Terraform only in accounts you connect, through a role you create in each one."
        actions={
          <Button variant="primary" onClick={() => setOnboarding(true)}>
            <Plus aria-hidden /> Connect
          </Button>
        }
      />
      <ErrorNote error={remove.error} />

      {accounts.isLoading ? (
        <RowSkeleton rows={1} />
      ) : !accounts.data?.length ? (
        <Empty title="No accounts yet">Connect one to start your first run. It takes about two minutes.</Empty>
      ) : (
        <Panel>
          <ul className="divide-y divide-line">
            {accounts.data.map((a) => (
              <li key={a.id} className="flex items-center gap-4 px-5 py-4">
                <div className="min-w-0 flex-1">
                  <p className="truncate">{a.displayName || "AWS account"}</p>
                  <p className="mt-0.5 text-sm text-muted">
                    <span className="font-mono text-xs">{a.accountId}</span>
                    <span className="px-1.5 text-line">/</span>
                    Connected {ago(a.createdAt)}
                  </p>
                  {a.stateBucket && (
                    <p className="mt-0.5 truncate text-xs text-muted" title="Terraform state for runs in this account">
                      State in <span className="font-mono">s3://{a.stateBucket}</span>
                    </p>
                  )}
                </div>
                <RowMenu
                  items={[
                    {
                      label: "Disconnect",
                      danger: true,
                      onSelect: () => window.confirm(`Disconnect ${a.accountId}? Runs can no longer use it.`) && remove.mutate(a.accountId),
                    },
                  ]}
                />
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {isAdmin && whoami.data?.accountId && (
        <p className="text-sm text-muted">
          AEGIS itself runs in <span className="font-mono text-xs">{whoami.data.accountId}</span>. You can connect it too; runs still use
          the role, never AEGIS's own identity.
        </p>
      )}
      <Dialog open={onboarding} onClose={() => setOnboarding(false)} title="Connect an AWS account" wide>
        <ConnectAccountForm onDone={() => setOnboarding(false)} onCancel={() => setOnboarding(false)} />
      </Dialog>
    </Page>
  );
}

function Step({ n, active, title, children }: { n: number; active: boolean; title: string; children: React.ReactNode }) {
  return (
    <li className={cn("grid grid-cols-[1.75rem_1fr] gap-x-3 gap-y-3", !active && "opacity-50")}>
      <span
        className={cn(
          "grid size-7 place-items-center rounded-full border text-xs font-medium",
          active ? "border-teal/50 bg-teal/10 text-teal" : "border-line text-muted",
        )}
      >
        {n}
      </span>
      <p className="self-center font-medium">{title}</p>
      <div className="col-start-2 grid gap-3">{children}</div>
    </li>
  );
}

/** The two onboarding steps: launch the CloudFormation stack, paste the role ARN.
 * Used by the Accounts dialog and by the full-screen /connect page for new sign-ups. */
export function ConnectAccountForm({
  onDone,
  onCancel,
  cancelLabel = "Cancel",
}: {
  onDone: () => void;
  onCancel: () => void;
  cancelLabel?: string;
}) {
  const qc = useQueryClient();
  const [session, setSession] = useState<Start | null>(null);
  const [roleArn, setRoleArn] = useState("");
  const [name, setName] = useState("");

  const start = useMutation({ mutationFn: () => request<Start>("/api/aws-accounts/start", { method: "POST" }), onSuccess: setSession });
  const complete = useMutation({
    mutationFn: () => request("/api/aws-accounts/complete", { body: { onboardingId: session!.onboardingId, roleArn: roleArn.trim(), displayName: name.trim() } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["aws-accounts"] });
      onDone();
    },
  });

  return (
    <ol className="grid gap-7">
        <Step n={1} active title="Create the AEGIS role">
          <p className="text-sm text-muted">
            Sign in to the AWS console for the account, then launch the stack. It creates a role that only AEGIS can assume, and the S3 bucket that holds the Terraform state for your runs.
          </p>
          {!session ? (
            <Button className="w-full sm:w-fit" onClick={() => start.mutate()} busy={start.isPending}>
              Generate the stack link
            </Button>
          ) : session.launchUrl ? (
            <a
              href={session.launchUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-teal px-4 text-sm font-medium text-on-accent sm:w-fit"
            >
              Open in CloudFormation <ExternalLink className="size-4" aria-hidden />
            </a>
          ) : (
            <div className="grid gap-2 rounded-xl border border-line bg-bg p-4 text-sm">
              <p className="text-amber">No template URL is configured. Deploy infra/onboarding/aegis-provisioner-role.yaml with:</p>
              <p className="font-mono text-xs break-all">ExternalId = {session.externalId}</p>
              {session.trustedRoleArn && <p className="font-mono text-xs break-all">TrustedRoleArn = {session.trustedRoleArn}</p>}
            </div>
          )}
          <ErrorNote error={start.error} />
        </Step>
        <Step n={2} active={!!session} title="Paste the role ARN">
          <form
            className="grid gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              complete.mutate();
            }}
          >
            <fieldset disabled={!session} className="grid gap-4">
              <Field label="RoleArn (from the stack outputs)">
                <input
                  required
                  value={roleArn}
                  onChange={(e) => setRoleArn(e.target.value)}
                  className={`${inputClass} font-mono text-xs`}
                  placeholder="arn:aws:iam::123456789012:role/AegisProvisioner-default"
                />
              </Field>
              <Field label="Name" hint="Optional. Shown when you start a run.">
                <input value={name} onChange={(e) => setName(e.target.value)} className={inputClass} placeholder="Sandbox" />
              </Field>
            </fieldset>
            <ErrorNote error={complete.error} />
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Button type="button" variant="ghost" onClick={onCancel}>
                {cancelLabel}
              </Button>
              <Button type="submit" variant="primary" disabled={!session || !roleArn.trim()} busy={complete.isPending}>
                Verify and connect
              </Button>
            </div>
          </form>
        </Step>
    </ol>
  );
}
