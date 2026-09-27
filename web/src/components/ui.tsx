import { forwardRef, useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { LoaderCircle } from "lucide-react";
import { cn } from "../lib/format";
import type { Tone } from "../lib/status";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-teal text-on-accent hover:brightness-110",
  secondary: "border border-line bg-raised text-fg hover:border-muted",
  ghost: "text-muted hover:text-fg hover:bg-raised",
  danger: "bg-rust text-white hover:brightness-110",
};

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean; size?: "sm" | "md" }
>(function Button({ variant = "secondary", busy, size = "md", className, children, disabled, ...rest }, ref) {
  return (
    <button
      ref={ref}
      disabled={disabled || busy}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "h-8 px-3 text-sm" : "h-9 px-4 text-sm",
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {busy && <LoaderCircle className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
});

const TONE_TEXT: Record<Tone, string> = {
  teal: "text-teal",
  amber: "text-amber",
  rust: "text-rust",
  muted: "text-muted",
};
const TONE_BG: Record<Tone, string> = {
  teal: "bg-teal",
  amber: "bg-amber",
  rust: "bg-rust",
  muted: "bg-muted",
};

export function Dot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex size-2 shrink-0" aria-hidden>
      {pulse && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", TONE_BG[tone])} />}
      <span className={cn("relative size-2 rounded-full", TONE_BG[tone])} />
    </span>
  );
}

export function StatusText({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={cn("text-sm font-medium", TONE_TEXT[tone])}>{children}</span>;
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "h-9 w-full rounded-md border border-line bg-bg px-3 text-sm text-fg placeholder:text-muted/70 focus:border-teal focus:outline-none";

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <p role="alert" className="rounded-md border border-rust/40 bg-rust/10 px-3 py-2 text-sm text-rust">
      {message}
    </p>
  );
}

/** Native <dialog>: focus trapping, Esc and the backdrop come from the platform. */
export function Dialog({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      className={cn(
        "m-auto w-[calc(100%-2rem)] rounded-xl border border-line bg-surface p-0 text-fg shadow-2xl",
        wide ? "max-w-2xl" : "max-w-lg",
      )}
    >
      {open && (
        <div className="grid gap-5 p-6">
          <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
          {children}
        </div>
      )}
    </dialog>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-5">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-muted">{description}</p>}
      </div>
      {actions}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="grid place-items-start gap-2 rounded-lg border border-dashed border-line p-8">
      <p className="font-medium">{title}</p>
      {children && <div className="text-sm text-muted">{children}</div>}
    </div>
  );
}
