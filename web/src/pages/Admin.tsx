import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "../lib/api";
import type { Identity, Role } from "../lib/types";
import { ago } from "../lib/format";
import { Button, Dialog, ErrorNote, Field, PageHeader, inputClass } from "../components/ui";

type AppUser = Identity & { cognitoSub: string };

export default function Admin() {
  const qc = useQueryClient();
  const [inviting, setInviting] = useState(false);
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => request<{ users: AppUser[] }>("/api/users").then((r) => r.users),
  });
  const promote = useMutation({
    mutationFn: (u: AppUser) => request(`/api/users/${encodeURIComponent(u.cognitoSub)}`, { method: "PATCH", body: { role: "admin" } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  return (
    <div className="mx-auto grid max-w-4xl gap-8 px-6 py-8">
      <PageHeader
        title="People"
        description="Everyone who can sign in to AEGIS. Admins also manage people and decide what enters the knowledge base."
        actions={
          <Button variant="primary" onClick={() => setInviting(true)}>
            Invite someone
          </Button>
        }
      />
      <ErrorNote error={users.error || promote.error} />
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr className="border-b border-line">
              <th className="px-4 py-2 font-medium">Email</th>
              <th className="px-4 py-2 font-medium">Role</th>
              <th className="px-4 py-2 font-medium">Last sign-in</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {users.data?.map((u) => (
              <tr key={u.id} className="border-b border-line last:border-0">
                <td className="px-4 py-2.5">
                  {u.email}
                  {!u.enabled && <span className="ml-2 text-xs text-rust">disabled</span>}
                </td>
                <td className="px-4 py-2.5">{u.role === "admin" ? "Admin" : "Member"}</td>
                <td className="px-4 py-2.5 text-muted">{u.lastLogin ? ago(u.lastLogin) : "Never"}</td>
                <td className="px-4 py-2.5 text-right">
                  {u.role === "user" && (
                    <Button
                      size="sm"
                      variant="ghost"
                      busy={promote.isPending && promote.variables?.id === u.id}
                      onClick={() => window.confirm(`Make ${u.email} an admin? This can't be undone.`) && promote.mutate(u)}
                    >
                      Make admin
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <InviteDialog open={inviting} onClose={() => setInviting(false)} />
    </div>
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
    <Dialog open={open} onClose={close} title="Invite someone">
      {invite.data ? (
        <div className="grid gap-4">
          <p>{invite.data.user.email} can now sign in.</p>
          {invite.data.temporaryPassword && (
            <div className="rounded-lg border border-amber/50 bg-amber/5 p-4 text-sm">
              <p className="font-medium">Temporary password</p>
              <p className="mt-1 font-mono text-base select-all">{invite.data.temporaryPassword}</p>
              <p className="mt-2 text-muted">
                Shown once. Send it to them privately; they choose a new one at first sign-in.
              </p>
            </div>
          )}
          <Button variant="primary" className="w-fit" onClick={close}>
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
              <option value="user">Member: runs, accounts, integrations, knowledge</option>
              <option value="admin">Admin: also people and knowledge approvals</option>
            </select>
          </Field>
          <ErrorNote error={invite.error} />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" busy={invite.isPending}>
              Invite
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}
