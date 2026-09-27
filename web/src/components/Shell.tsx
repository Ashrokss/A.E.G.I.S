import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { useAuth } from "../lib/auth";
import { request } from "../lib/api";
import type { InboxItem } from "../lib/types";
import { cn } from "../lib/format";
import Mark from "./Mark";

export default function Shell() {
  const { identity, logout } = useAuth();
  const inbox = useQuery({
    queryKey: ["inbox"],
    queryFn: () => request<InboxItem[]>("/agent-api/runs/inbox"),
    refetchInterval: 30_000,
  });
  const waiting = inbox.data?.filter((i) => i.state === "pending").length ?? 0;

  const links: Array<{ to: string; label: string; badge?: number }> = [
    { to: "/runs", label: "Runs" },
    { to: "/inbox", label: "Inbox", badge: waiting },
    { to: "/knowledge", label: "Knowledge" },
    { to: "/accounts", label: "Accounts" },
    { to: "/integrations", label: "Integrations" },
    ...(identity?.role === "admin" ? [{ to: "/admin", label: "Admin" }] : []),
  ];

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-14 shrink-0 items-center gap-6 border-b border-line bg-surface px-4">
        <NavLink to="/runs" className="flex items-center gap-2 text-teal" aria-label="AEGIS home">
          <Mark className="size-6" />
          <span className="text-[17px] font-bold tracking-[0.08em] text-fg">AEGIS</span>
        </NavLink>
        <nav className="no-scrollbar flex min-w-0 flex-1 items-center gap-1 overflow-x-auto" aria-label="Main">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              className={({ isActive }) =>
                cn(
                  "relative inline-flex h-14 items-center gap-2 px-3 text-sm whitespace-nowrap transition-colors",
                  isActive
                    ? "text-fg after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-teal"
                    : "text-muted hover:text-fg",
                )
              }
            >
              {l.label}
              {!!l.badge && (
                <span className="rounded-full bg-amber px-1.5 text-xs font-semibold text-on-accent">{l.badge}</span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="hidden items-center gap-3 text-sm sm:flex">
          <span className="text-muted">{identity?.email}</span>
          {identity?.role === "admin" && (
            <span className="rounded border border-line px-1.5 py-0.5 text-xs text-muted">admin</span>
          )}
        </div>
        <button onClick={logout} className="rounded-md p-2 text-muted hover:bg-raised hover:text-fg" title="Sign out">
          <LogOut className="size-4" aria-hidden />
          <span className="sr-only">Sign out</span>
        </button>
      </header>
      <main className="min-h-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}
