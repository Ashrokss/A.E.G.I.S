import { NavLink, Outlet, useMatch } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Cloud, House, Inbox, LogOut, Plug, Users, Workflow, type LucideIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/lib/auth";
import { request } from "@/lib/api";
import type { InboxItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import Mark from "./Mark";
import ThemeSwitch from "./ThemeSwitch";

type Item = { to: string; label: string; icon: LucideIcon; badge?: number };

export default function Shell() {
  const { identity, logout } = useAuth();
  // A run is a focused screen with its own composer: no tab bar over it.
  const inRun = useMatch("/runs/:runId");
  const inbox = useQuery({
    queryKey: ["inbox"],
    queryFn: () => request<InboxItem[]>("/agent-api/runs/inbox"),
    refetchInterval: 30_000,
  });
  const waiting = inbox.data?.filter((i) => i.state === "pending").length ?? 0;

  const items: Item[] = [
    { to: "/home", label: "Home", icon: House },
    { to: "/runs", label: "Runs", icon: Workflow },
    { to: "/inbox", label: "Inbox", icon: Inbox, badge: waiting },
    { to: "/knowledge", label: "Knowledge", icon: BookOpen },
    { to: "/accounts", label: "Accounts", icon: Cloud },
    { to: "/integrations", label: "Integrations", icon: Plug },
    ...(identity?.role === "admin" ? [{ to: "/admin", label: "People", icon: Users }] : []),
  ];
  const initial = (identity?.email ?? "?").slice(0, 1).toUpperCase();

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-30 border-b border-line/70 bg-bg/80 backdrop-blur-xl">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-4 px-4 sm:px-6 md:h-16">
          <NavLink to="/home" className="flex items-center gap-2 text-teal" aria-label="AEGIS home">
            <Mark className="size-6" />
            <span className="font-display text-[17px] font-bold tracking-[0.06em] text-fg">AEGIS</span>
          </NavLink>

          <nav aria-label="Main" className="mx-auto hidden items-center gap-0.5 rounded-full border border-line bg-surface/70 p-1 md:flex">
            {items.map(({ to, label, icon: Icon, badge }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  cn(
                    "relative inline-flex h-8 items-center gap-2 rounded-full px-3.5 text-sm transition-colors",
                    isActive ? "bg-raised text-fg shadow-sm" : "text-muted hover:text-fg",
                  )
                }
              >
                <Icon className="size-4" aria-hidden />
                {label}
                {!!badge && <span className="size-1.5 rounded-full bg-amber" aria-label={`${badge} waiting`} />}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2 md:ml-0">
          <ThemeSwitch />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className="grid size-9 place-items-center rounded-full border border-line bg-surface text-sm font-semibold transition-colors hover:border-muted/40"
                aria-label="Account menu"
              >
                {initial}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-56 rounded-xl border-line bg-surface p-1">
              <DropdownMenuLabel className="px-2.5 py-2 font-normal">
                <p className="truncate text-sm">{identity?.email}</p>
                <p className="text-xs text-muted">{identity?.role === "admin" ? "Administrator" : "Member"}</p>
              </DropdownMenuLabel>
              <DropdownMenuSeparator className="bg-line" />
              <DropdownMenuItem onSelect={logout} className="rounded-lg px-2.5 py-2">
                <LogOut className="size-4" aria-hidden /> Sign out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          </div>
        </div>
      </header>

      <main>
        <Outlet />
      </main>

      {!inRun && (
        <nav
          aria-label="Main"
          className="fixed inset-x-0 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-30 mx-auto flex w-fit items-center gap-1 rounded-full border border-line bg-surface/90 p-1.5 shadow-2xl shadow-black/40 backdrop-blur-xl md:hidden"
        >
          {items.map(({ to, label, icon: Icon, badge }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "relative grid size-10 place-items-center rounded-full transition-colors",
                  isActive ? "bg-raised text-teal" : "text-muted",
                )
              }
            >
              <Icon className="size-5" aria-hidden />
              <span className="sr-only">{label}</span>
              {!!badge && <span className="absolute top-2 right-2.5 size-1.5 rounded-full bg-amber" aria-hidden />}
            </NavLink>
          ))}
        </nav>
      )}
    </div>
  );
}
