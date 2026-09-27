import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { request } from "@/lib/api";
import type { Identity, Role } from "@/lib/types";
import { ago } from "@/lib/format";
import { Button, Dialog, ErrorNote, Field, Page, PageHeader, Panel, RowMenu, RowSkeleton, inputClass } from "@/components/ui";

type AppUser = Identity & { cognitoSub: string };

export default function Admin() {
  const qc = useQueryClient();
  const [inviting, setInviting] = useState(false);
  const users = useQuery({ queryKey: ["users"], queryFn: () => request<{ users: AppUser[] }>("/api/users").then((r) => r.users) });
  const promote = useMutation({
    mutationFn: (u: AppUser) => request(`/api/users/${encodeURIComponent(u.cognitoSub)}`, { method: "PATCH", body: { role: "admin" } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  return (
    <Page>
      <PageHeader
        title="People"
        description="Everyone who can sign in. Admins also manage people and approve what enters the knowledge base."
        actions={
          <Button variant="primary" onClick={() => setInviting(true)}>
            <Plus aria-hidden /> Invite
          </Button>
        }
      />
      <ErrorNote error={users.error || promote.error} />
      {users.isLoading ? (
        <RowSkeleton />
      ) : (
        <Panel>
          <ul className="divide-y divide-line">
            {users.data?.map((u) => (
              <li key={u.id} className="flex items-center gap-4 px-5 py-4">
                <span className="grid size-9 shrink-0 place-items-center rounded-full bg-raised text-sm font-semibold">
                  {u.email.slice(0, 1).toUpperCase()}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate">{u.email}</p>
                  <p className="mt-0.5 text-sm text-muted">
                    {u.role === "admin" ? "Admin" : "Member"}
                    <span className="px-1.5 text-line">/</span>
                    {u.lastLogin ? `Signed in ${ago(u.lastLogin)}` : "Never signed in"}
                    {!u.enabled && <span className="text-rust"> / Disabled</span>}
                  </p>
                </div>
                {u.role === "user" && (
                  <RowMenu
                    items={[
                      {
                        label: "Make admin",
                        onSelect: () => window.confirm(`Make ${u.email} an admin? This can't be undone.`) && promote.mutate(u),
                      },
                    ]}
                  />
                )}
              </li>
            ))}
          </ul>
        </Panel>
      )}
      <InviteDialog open={inviting} onClose={() => setInviting(false)} />
    </Page>
  );
}

function InviteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("user");
  const invite = useMutation({
    mutationFn: () => request<{ user: AppUser; temporaryPassword?: string }>("/api/users", { body: { email, role } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
  const close = () => {
    setEmail("");
    setRole("user");
    invite.reset();
    onClose();
  };
  return (
    <Dialog open={open} onClose={close} title={invite.data ? "Invitation ready" : "Invite someone"}>
      {invite.data ? (
        <div className="grid gap-5">
          <p className="text-muted">{invite.data.user.email} can now sign in.</p>
          {invite.data.temporaryPassword && (
            <div className="grid gap-2 rounded-xl border border-amber/35 bg-amber/[0.06] p-4">
              <p className="text-sm font-medium">Temporary password</p>
              <p className="font-mono text-base break-all select-all">{invite.data.temporaryPassword}</p>
              <p className="text-sm text-muted">Shown once. Send it privately; they choose a new one at first sign-in.</p>
            </div>
          )}
          <Button variant="primary" className="w-full sm:w-fit" onClick={close}>
            Done
          </Button>
        </div>
      ) : (
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            invite.mutate();
          }}
        >
          <Field label="Email">
            <input required type="email" value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} />
          </Field>
          <Field label="Role">
            <select value={role} onChange={(e) => setRole(e.target.value as Role)} className={inputClass}>
              <option value="user">Member</option>
              <option value="admin">Admin</option>
            </select>
          </Field>
          <ErrorNote error={invite.error} />
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" busy={invite.isPending}>
              Send invite
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}
