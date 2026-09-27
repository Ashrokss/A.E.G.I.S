import { NavLink, Outlet, useMatch } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, ChevronsUpDown, Cloud, House, Inbox, LogOut, Plug, Users, Workflow, type LucideIcon } from "lucide-react";
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

// Desktop: a fixed sidebar (brand, navigation, theme and account at the foot).
// Phones: a slim top bar plus a floating bottom nav, hidden inside a run.
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
  const roleLabel = identity?.role === "admin" ? "Administrator" : "Member";

  const brand = (
    <NavLink to="/home" className="flex items-center gap-2 text-teal" aria-label="AEGIS home">
      <Mark className="size-6" />
      <span className="font-display text-[17px] font-bold tracking-[0.06em] text-fg">AEGIS</span>
    </NavLink>
  );

  const accountMenu = (trigger: React.ReactNode, align: "start" | "end") => (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent align={align} side={align === "start" ? "top" : "bottom"} className="glass min-w-56 rounded-xl p-1">
        <DropdownMenuLabel className="px-2.5 py-2 font-normal">
          <p className="truncate text-sm">{identity?.email}</p>
          <p className="text-xs text-muted">{roleLabel}</p>
        </DropdownMenuLabel>
        <DropdownMenuSeparator className="bg-line" />
        <DropdownMenuItem onSelect={logout} className="rounded-lg px-2.5 py-2">
          <LogOut className="size-4" aria-hidden /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );

  const avatar = (
    <span className="grid size-8 shrink-0 place-items-center rounded-full border border-line bg-raised text-sm font-semibold">{initial}</span>
  );

  return (
    <div className="min-h-full">
      {/* ── Desktop sidebar ─────────────────────────────────────────────── */}
      <aside className="glass fixed inset-y-3 left-3 z-30 hidden w-56 flex-col rounded-2xl md:flex">
        <div className="flex h-16 items-center px-5">{brand}</div>

        <nav aria-label="Main" className="no-scrollbar grid flex-1 content-start gap-0.5 overflow-y-auto px-3 py-2">
          {items.map(({ to, label, icon: Icon, badge }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "group relative flex h-9 items-center gap-3 rounded-lg px-3 text-sm transition-colors",
                  isActive ? "bg-raised font-medium text-fg" : "text-muted hover:bg-raised/50 hover:text-fg",
                )
              }
            >
              {({ isActive }) => (
                <>
                  {isActive && <span className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-teal" aria-hidden />}
                  <Icon className={cn("size-4 shrink-0", isActive && "text-teal")} aria-hidden />
                  <span className="flex-1">{label}</span>
                  {!!badge && (
                    <span className="grid h-5 min-w-5 place-items-center rounded-full bg-amber/15 px-1.5 text-[11px] font-medium text-amber" aria-label={`${badge} waiting`}>
                      {badge}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="grid gap-3 border-t border-fg/10 p-3">
          <ThemeSwitch className="w-full justify-between [&>button]:flex-1" />
          {accountMenu(
            <button className="flex w-full items-center gap-3 rounded-lg p-2 text-left transition-colors hover:bg-raised/60" aria-label="Account menu">
              {avatar}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm">{identity?.email}</span>
                <span className="block text-xs text-muted">{roleLabel}</span>
              </span>
              <ChevronsUpDown className="size-4 shrink-0 text-muted" aria-hidden />
            </button>,
            "start",
          )}
        </div>
      </aside>

      {/* ── Phone top bar ───────────────────────────────────────────────── */}
      <header className="glass sticky top-0 z-30 md:hidden">
        <div className="flex h-14 items-center gap-3 px-4">
          {brand}
          <div className="ml-auto flex items-center gap-2">
            <ThemeSwitch />
            {accountMenu(
              <button className="rounded-full transition-opacity hover:opacity-80" aria-label="Account menu">
                {avatar}
              </button>,
              "end",
            )}
          </div>
        </div>
      </header>

      <main className="md:pl-[15.5rem]">
        <Outlet />
      </main>

      {!inRun && (
        <nav
          aria-label="Main"
          className="fixed inset-x-0 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-30 mx-auto flex w-fit items-center gap-1 rounded-full p-1.5 glass md:hidden"
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
