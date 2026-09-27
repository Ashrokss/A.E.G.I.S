import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { request } from "../lib/api";
import { useAccounts } from "../lib/hooks";
import { ago } from "../lib/format";
import { Button, Dialog, Empty, ErrorNote, Field, PageHeader, inputClass } from "../components/ui";

type Start = { onboardingId: string; externalId: string; launchUrl: string | null; trustedRoleArn: string | null };

export default function Accounts() {
  const qc = useQueryClient();
  const accounts = useAccounts();
  const whoami = useQuery({
    queryKey: ["whoami"],
    queryFn: () => request<{ accountId: string | null }>("/api/aws-accounts/whoami"),
  });
  const [onboarding, setOnboarding] = useState(false);
  const remove = useMutation({
    mutationFn: (accountId: string) => request(`/api/aws-accounts/${accountId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["aws-accounts"] }),
  });

  return (
    <div className="mx-auto grid max-w-4xl gap-8 px-6 py-8">
      <PageHeader
        title="AWS accounts"
        description="AEGIS runs Terraform only in accounts you connect here, through a role you create in each one. The role can't create IAM users or escape its permissions boundary."
        actions={
          <Button variant="primary" onClick={() => setOnboarding(true)}>
            Connect an account
          </Button>
        }
      />
      <ErrorNote error={remove.error} />
      {accounts.data?.length === 0 ? (
        <Empty title="No accounts connected">Connect one to start your first run. It takes about two minutes.</Empty>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {accounts.data?.map((a) => (
            <li key={a.id} className="flex flex-wrap items-center gap-x-6 gap-y-1 px-4 py-3">
              <div className="w-full min-w-0 sm:w-auto sm:flex-1">
                <p className="font-medium">{a.displayName || "AWS account"}</p>
                <p className="font-mono text-xs break-all text-muted">{a.roleArn}</p>
              </div>
              <span className="font-mono text-sm">{a.accountId}</span>
              <span className="text-xs text-muted">Connected {ago(a.createdAt)}</span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => window.confirm(`Disconnect ${a.accountId}? Runs can no longer use it.`) && remove.mutate(a.accountId)}
              >
                Disconnect
              </Button>
            </li>
          ))}
        </ul>
      )}
      {whoami.data?.accountId && (
        <p className="text-sm text-muted">
          AEGIS itself runs in account <span className="font-mono">{whoami.data.accountId}</span>. You can connect that
          account too; runs still go through the role, never through AEGIS's own identity.
        </p>
      )}
      <OnboardDialog open={onboarding} onClose={() => setOnboarding(false)} />
    </div>
  );
}

function OnboardDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [session, setSession] = useState<Start | null>(null);
  const [roleArn, setRoleArn] = useState("");
  const [name, setName] = useState("");

  const start = useMutation({
    mutationFn: () => request<Start>("/api/aws-accounts/start", { method: "POST" }),
    onSuccess: setSession,
  });
  const complete = useMutation({
    mutationFn: () =>
      request("/api/aws-accounts/complete", {
        body: { onboardingId: session!.onboardingId, roleArn: roleArn.trim(), displayName: name.trim() },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["aws-accounts"] });
      close();
    },
  });

  function close() {
    setSession(null);
    setRoleArn("");
    setName("");
    start.reset();
    complete.reset();
    onClose();
  }

  return (
    <Dialog open={open} onClose={close} title="Connect an AWS account" wide>
      <ol className="grid gap-6">
        <li className="grid gap-3">
          <p className="font-medium">1. Create the AEGIS role in the account</p>
          <p className="text-sm text-muted">
            Sign in to the AWS console for the account you want to connect, then launch the stack. It creates one role
            that only AEGIS can assume, using an external ID generated for this connection.
          </p>
          {!session ? (
            <Button className="w-fit" onClick={() => start.mutate()} busy={start.isPending}>
              Generate the stack link
            </Button>
          ) : (
            <div className="grid gap-3 rounded-lg border border-line bg-bg p-4 text-sm">
              <div>
                <p className="text-muted">External ID</p>
                <p className="font-mono break-all select-all">{session.externalId}</p>
              </div>
              {session.trustedRoleArn && (
                <div>
                  <p className="text-muted">Trusted AEGIS role</p>
                  <p className="font-mono break-all">{session.trustedRoleArn}</p>
                </div>
              )}
              {session.launchUrl ? (
                <a
                  href={session.launchUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex w-fit items-center gap-2 rounded-md bg-teal px-4 py-2 font-medium text-on-accent"
                >
                  Open in CloudFormation <ExternalLink className="size-4" aria-hidden />
                </a>
              ) : (
                <p className="text-amber">
                  The template URL isn't configured on this deployment. Deploy infra/onboarding/aegis-provisioner-role.yaml
                  yourself with the values above.
                </p>
              )}
            </div>
          )}
          <ErrorNote error={start.error} />
        </li>
        <li className="grid gap-3">
          <p className={session ? "font-medium" : "font-medium text-muted"}>2. Paste the role ARN from the stack outputs</p>
          <form
            className="grid gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              complete.mutate();
            }}
          >
            <fieldset disabled={!session} className="grid gap-3 disabled:opacity-50">
              <Field label="RoleArn output">
                <input
                  required
                  value={roleArn}
                  onChange={(e) => setRoleArn(e.target.value)}
                  className={`${inputClass} font-mono`}
                  placeholder="arn:aws:iam::123456789012:role/AegisProvisioner-default"
                />
              </Field>
              <Field label="Name" hint="Optional. Shown in the run picker.">
                <input value={name} onChange={(e) => setName(e.target.value)} className={inputClass} placeholder="Sandbox" />
              </Field>
            </fieldset>
            <ErrorNote error={complete.error} />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={!session || !roleArn.trim()} busy={complete.isPending}>
                Verify and connect
              </Button>
            </div>
          </form>
        </li>
      </ol>
    </Dialog>
  );
}
