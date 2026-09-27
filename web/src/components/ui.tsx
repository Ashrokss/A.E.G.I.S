import type { ComponentProps, ReactNode } from "react";
import { LoaderCircle, MoreHorizontal } from "lucide-react";
import { Button as ShButton } from "@/components/ui/button";
import {
  Dialog as ShDialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { Tone } from "@/lib/status";

type Variant = "primary" | "secondary" | "ghost" | "danger";
const VARIANT = { primary: "default", secondary: "outline", ghost: "ghost", danger: "destructive" } as const;
const SIZE = { sm: "sm", md: "default", lg: "lg" } as const;

export function Button({
  variant = "secondary",
  size = "md",
  busy,
  disabled,
  className,
  children,
  ...rest
}: ComponentProps<"button"> & { variant?: Variant; size?: keyof typeof SIZE; busy?: boolean }) {
  return (
    <ShButton
      variant={VARIANT[variant]}
      size={SIZE[size]}
      disabled={disabled || busy}
      className={cn("rounded-lg", variant === "secondary" && "bg-transparent", className)}
      {...rest}
    >
      {busy && <LoaderCircle className="animate-spin" aria-hidden />}
      {children}
    </ShButton>
  );
}

const TONE_TEXT: Record<Tone, string> = { teal: "text-teal", amber: "text-amber", rust: "text-rust", muted: "text-muted" };
const TONE_BG: Record<Tone, string> = { teal: "bg-teal", amber: "bg-amber", rust: "bg-rust", muted: "bg-muted/60" };

export function Dot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex size-2 shrink-0" aria-hidden>
      {pulse && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", TONE_BG[tone])} />}
      <span className={cn("relative size-2 rounded-full", TONE_BG[tone])} />
    </span>
  );
}

export function StatusText({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={cn("text-sm", TONE_TEXT[tone])}>{children}</span>;
}

/** Wrapping <label> keeps the input associated without ids. */
export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="grid gap-2">
      <span className="text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="text-xs leading-relaxed text-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "h-10 w-full rounded-lg border border-line bg-transparent px-3 text-sm text-fg placeholder:text-muted/60 outline-none transition-[border-color,box-shadow] focus-visible:border-teal focus-visible:ring-3 focus-visible:ring-teal/15";

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <p role="alert" className="rounded-xl border border-rust/30 bg-rust/[0.07] px-3.5 py-2.5 text-sm text-rust">
      {message}
    </p>
  );
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <ShDialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        className={cn(
          "max-h-[calc(100dvh-2rem)] gap-6 overflow-y-auto rounded-2xl border-line bg-surface p-6 sm:rounded-2xl",
          wide ? "sm:max-w-xl" : "sm:max-w-md",
        )}
      >
        <DialogHeader className="text-left">
          <DialogTitle className="font-display text-lg font-semibold tracking-tight">{title}</DialogTitle>
          {description && <DialogDescription className="text-muted">{description}</DialogDescription>}
        </DialogHeader>
        {children}
      </DialogContent>
    </ShDialog>
  );
}

/** A centred, comfortably narrow page column. Bottom padding clears the mobile tab bar. */
export function Page({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("mx-auto grid w-full max-w-5xl content-start gap-8 px-4 pt-6 pb-28 sm:px-6 sm:pt-10 md:pb-16", className)}>
      {children}
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
      <div className="min-w-0 max-w-xl">
        <h1 className="text-2xl font-semibold sm:text-3xl">{title}</h1>
        {description && <p className="mt-2 text-pretty text-muted">{description}</p>}
      </div>
      {actions}
    </header>
  );
}

export function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("overflow-hidden rounded-2xl border border-line bg-surface", className)}>{children}</div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="grid place-items-center gap-2 rounded-2xl border border-dashed border-line px-6 py-14 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="max-w-sm text-sm text-pretty text-muted">{children}</div>}
    </div>
  );
}

export function RowMenu({
  label = "More actions",
  items,
}: {
  label?: string;
  items: Array<{ label: string; onSelect: () => void; danger?: boolean; disabled?: boolean }>;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={label}
          className="grid size-9 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-raised hover:text-fg data-[state=open]:bg-raised data-[state=open]:text-fg"
        >
          <MoreHorizontal className="size-4" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48 rounded-xl border-line bg-surface p-1">
        {items.map((i) => (
          <DropdownMenuItem
            key={i.label}
            disabled={i.disabled}
            onSelect={i.onSelect}
            className={cn("rounded-lg px-2.5 py-2", i.danger && "text-rust focus:text-rust")}
          >
            {i.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function RowSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <Panel>
      <ul className="divide-y divide-line">
        {Array.from({ length: rows }, (_, i) => (
          <li key={i} className="grid gap-2 px-5 py-4">
            <span className="h-4 w-2/3 animate-pulse rounded bg-raised" />
            <span className="h-3 w-1/3 animate-pulse rounded bg-raised" />
          </li>
        ))}
      </ul>
    </Panel>
  );
}
