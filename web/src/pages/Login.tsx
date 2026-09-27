import { useRef, useState } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { SplitText } from "gsap/SplitText";
import { TextPlugin } from "gsap/TextPlugin";
import { DrawSVGPlugin } from "gsap/DrawSVGPlugin";
import { ArrowRight, GitPullRequest, KeyRound, ScanEye } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import AuroraCurtain from "@/components/smoothui/aurora-curtain";
import BorderBeam from "@/components/smoothui/border-beam";
import SmoothButton from "@/components/smoothui/smooth-button";
import PlanGlyph, { type Action } from "@/components/PlanGlyph";
import { useAuth } from "@/lib/auth";
import ThemeSwitch from "@/components/ThemeSwitch";

gsap.registerPlugin(useGSAP, SplitText, TextPlugin, DrawSVGPlugin);

// The hero is what AEGIS exists to produce: a plan you read before anything
// touches your account. One GSAP timeline plays the whole page, once.
const PLAN: Array<{ action: Action; address: string }> = [
  { action: "create", address: "aws_s3_bucket.artifacts" },
  { action: "create", address: "aws_s3_bucket_versioning.artifacts" },
  { action: "create", address: "aws_s3_bucket_lifecycle_configuration.artifacts" },
  { action: "update", address: "aws_iam_role_policy.ci_publish" },
  { action: "delete", address: "aws_instance.legacy_worker" },
];
const COUNTS = { add: 3, change: 1, destroy: 1 };
const COMMAND = "terraform plan -out=tfplan";

const PROMISES = [
  { icon: ScanEye, text: "Every change shown as a plan before it happens" },
  { icon: KeyRound, text: "Runs only through a role you create in your account" },
  { icon: GitPullRequest, text: "A pull request with the code for every run" },
];

export default function Login() {
  const { login, error } = useAuth();
  const [busy, setBusy] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useGSAP(
    (_ctx, contextSafe) => {
      const q = gsap.utils.selector(root);
      const css = getComputedStyle(document.documentElement);
      const teal = css.getPropertyValue("--teal").trim();
      const amber = css.getPropertyValue("--amber").trim();
      const counters = q<HTMLElement>("[data-count]");
      const cmd = q<HTMLElement>("[data-cmd]")[0];

      const showFinalState = () => {
        cmd.textContent = COMMAND;
        counters.forEach((el) => (el.textContent = String(COUNTS[el.dataset.count as keyof typeof COUNTS])));
        gsap.set(root.current, { autoAlpha: 1 });
      };

      const mm = gsap.matchMedia();

      mm.add("(prefers-reduced-motion: reduce)", showFinalState);

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        // Split after the webfonts load, or the line masks measure the fallback font.
        const build = contextSafe!(() => {
          gsap.set(root.current, { autoAlpha: 1 });

          const title = SplitText.create(q(".hero-title"), { type: "lines,words,chars", mask: "lines" });
          const lead = SplitText.create(q(".hero-lead"), { type: "lines", mask: "lines" });

          // The last line's characters sweep from teal to amber.
          const gradChars = title.chars.filter((c) => c.closest(".hero-grad"));
          const ramp = gsap.utils.interpolate(teal, amber);
          gradChars.forEach((c, i) => ((c as HTMLElement).style.color = ramp(i / Math.max(gradChars.length - 1, 1))));

          const tally = { add: 0, change: 0, destroy: 0 };
          const tl = gsap.timeline({ defaults: { ease: "expo.out", duration: 1.1 } });

          tl.from(q(".mark-shield"), { drawSVG: 0, duration: 1.3, ease: "power2.inOut" })
            .from(q(".mark-plus"), { scale: 0, transformOrigin: "50% 50%", ease: "back.out(3)", duration: 0.7 }, "-=0.55")
            .from(q(".wordmark"), { x: -14, autoAlpha: 0, duration: 0.9 }, "<")
            .from(title.chars, { yPercent: 115, rotation: 8, stagger: 0.018, duration: 1.2 }, 0.2)
            .from(lead.lines, { yPercent: 100, stagger: 0.09 }, "-=0.95")
            .from(q(".signin"), { y: 48, autoAlpha: 0, scale: 0.97, duration: 1.3 }, "-=0.9")
            .from(q(".signin-item"), { y: 14, autoAlpha: 0, stagger: 0.07, duration: 0.9 }, "-=1.0")
            .from(
              q(".terminal"),
              { y: 70, rotationX: 18, autoAlpha: 0, transformPerspective: 1400, transformOrigin: "50% 100%", duration: 1.6 },
              0.45,
            )
            .to(cmd, { text: COMMAND, duration: 1.0, ease: "none" }, "-=0.6")
            .from(q(".plan-line"), { autoAlpha: 0, x: -12, stagger: 0.16, duration: 0.6, ease: "power3.out" }, "+=0.15")
            .to(
              tally,
              {
                ...COUNTS,
                duration: 0.9,
                ease: "power2.out",
                snap: { add: 1, change: 1, destroy: 1 },
                onUpdate: () => counters.forEach((el) => (el.textContent = String(tally[el.dataset.count as keyof typeof COUNTS]))),
              },
              "-=0.5",
            )
            .from(q(".approval"), { y: 18, autoAlpha: 0, duration: 0.9 }, "-=0.3")
            .from(q(".approve-btn"), { scale: 0.85, ease: "elastic.out(1, 0.45)", duration: 1.4 }, "<0.15");

          // Continuous: the scene leans toward the pointer, with GSAP's smooth lag.
          const lean = {
            tx: gsap.quickTo(q(".terminal-float"), "x", { duration: 1.1, ease: "power3" }),
            ty: gsap.quickTo(q(".terminal-float"), "y", { duration: 1.1, ease: "power3" }),
            rx: gsap.quickTo(q(".terminal-float"), "rotationX", { duration: 1.1, ease: "power3" }),
            ry: gsap.quickTo(q(".terminal-float"), "rotationY", { duration: 1.1, ease: "power3" }),
            hx: gsap.quickTo(q(".hero-title"), "x", { duration: 1.4, ease: "power3" }),
          };
          gsap.set(q(".terminal-float"), { transformPerspective: 1400 });
          const onMove = (e: PointerEvent) => {
            const nx = e.clientX / window.innerWidth - 0.5;
            const ny = e.clientY / window.innerHeight - 0.5;
            lean.tx(nx * -22);
            lean.ty(ny * -16);
            lean.ry(nx * 7);
            lean.rx(ny * -6);
            lean.hx(nx * 10);
          };
          window.addEventListener("pointermove", onMove);

          // Magnetic sign-in button.
          const magnet = q<HTMLElement>(".magnet")[0];
          const mx = gsap.quickTo(magnet, "x", { duration: 0.5, ease: "power3" });
          const my = gsap.quickTo(magnet, "y", { duration: 0.5, ease: "power3" });
          const onMagnet = (e: PointerEvent) => {
            const r = magnet.getBoundingClientRect();
            mx((e.clientX - (r.left + r.width / 2)) * 0.18);
            my((e.clientY - (r.top + r.height / 2)) * 0.35);
          };
          const onRelease = () => gsap.to(magnet, { x: 0, y: 0, duration: 1.1, ease: "elastic.out(1, 0.35)" });
          magnet.addEventListener("pointermove", onMagnet);
          magnet.addEventListener("pointerleave", onRelease);

          return () => {
            window.removeEventListener("pointermove", onMove);
            magnet.removeEventListener("pointermove", onMagnet);
            magnet.removeEventListener("pointerleave", onRelease);
          };
        });

        let cleanup: (() => void) | undefined;
        let cancelled = false;
        document.fonts.ready.then(() => {
          if (!cancelled) cleanup = build();
        });
        return () => {
          cancelled = true;
          cleanup?.();
        };
      });

      return () => mm.revert();
    },
    { scope: root },
  );

  return (
    <AuroraCurtain
      className="min-h-full bg-background"
      colors={["var(--teal)", "#1f8f82", "var(--amber)"]}
      intensity={0.8}
      speed={0.5}
      bands={3}
      blur={1}
    >
      {/* A soft mesh gradient under the aurora, then a vertical fade so the
          text sits on calm colour. No grain: the surface should read as smooth. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 -z-10"
        style={{
          backgroundImage: [
            "radial-gradient(60% 55% at 78% 30%, color-mix(in oklab, var(--teal) 28%, transparent), transparent 70%)",
            "radial-gradient(45% 45% at 18% 85%, color-mix(in oklab, var(--amber) 16%, transparent), transparent 70%)",
            "radial-gradient(70% 60% at 30% 10%, color-mix(in oklab, #1f8f82 18%, transparent), transparent 70%)",
          ].join(", "),
        }}
      />
      <div aria-hidden className="pointer-events-none absolute inset-0 bg-gradient-to-b from-background/60 via-background/25 to-background/5" />

      <div className="absolute top-4 right-4 z-20 sm:top-6 sm:right-6">
        <ThemeSwitch />
      </div>

      {/* Hidden until GSAP has set every starting state: no flash of the final layout. */}
      <div
        ref={root}
        className="invisible relative mx-auto grid min-h-dvh max-w-7xl items-center gap-12 px-6 py-10 sm:px-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] lg:gap-16"
      >
        <section className="grid max-w-xl gap-8">
          <div className="flex items-center gap-2.5 text-primary">
            <svg viewBox="0 0 32 32" className="size-8" aria-hidden>
              <path
                className="mark-shield"
                d="M16 2 4 7v9c0 7 5 12 12 14 7-2 12-7 12-14V7z"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinejoin="round"
              />
              <path className="mark-plus" d="M16 10v12M10 16h12" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
            </svg>
            <span className="wordmark text-xl font-bold tracking-[0.08em] text-foreground">AEGIS</span>
          </div>

          <div className="grid gap-5">
            <h1 className="hero-title text-4xl leading-[1.08] font-semibold tracking-tight [font-kerning:none] sm:text-6xl">
              <span className="block">Describe it.</span>
              <span className="block">Review the plan.</span>
              <span className="hero-grad block text-primary">Then apply.</span>
            </h1>
            <p className="hero-lead max-w-md text-lg text-muted-foreground">
              AEGIS writes Terraform for your AWS accounts, shows you every change before it happens, and opens a pull
              request with the code.
            </p>
          </div>

          <div className="signin max-w-md">
            <BorderBeam colorFrom="var(--teal)" colorTo="var(--amber)" duration={9} radius={14} size={120} pauseOnHover>
              <Card className="gap-5 rounded-[14px] border-line bg-card/75 shadow-2xl shadow-black/10 backdrop-blur-xl dark:shadow-black/40">
                <CardHeader className="signin-item">
                  <CardTitle className="text-lg">Sign in to AEGIS</CardTitle>
                  <CardDescription>Use the account your administrator invited.</CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4">
                  <div className="magnet signin-item">
                    <SmoothButton
                      variant="candy"
                      size="lg"
                      className="w-full [--btn-fg:var(--on-accent)] [--btn-hover:color-mix(in_oklab,var(--teal)_78%,black)] [--btn:var(--teal)]"
                      loading={busy}
                      suffix={<ArrowRight aria-hidden />}
                      onClick={async () => {
                        setBusy(true);
                        await login();
                        setBusy(false);
                      }}
                    >
                      Continue with single sign-on
                    </SmoothButton>
                  </div>
                  {error && (
                    <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                      {error}
                    </p>
                  )}
                  <Separator className="signin-item" />
                  <ul className="grid gap-2.5">
                    {PROMISES.map(({ icon: Icon, text }) => (
                      <li key={text} className="signin-item flex items-start gap-3 text-sm text-muted-foreground">
                        <Icon className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
                        {text}
                      </li>
                    ))}
                  </ul>
                </CardContent>
                <CardFooter className="signin-item text-xs text-muted-foreground">
                  Access is by invitation. No account yet? Ask your AEGIS administrator.
                </CardFooter>
              </Card>
            </BorderBeam>
          </div>
        </section>

        <section aria-label="Example: a Terraform plan waiting for approval" className="hidden lg:block">
          <div className="terminal-float grid gap-4 will-change-transform">
            <div className="terminal overflow-hidden rounded-2xl border border-line bg-surface/90 shadow-2xl shadow-black/10 backdrop-blur-xl dark:shadow-black/50">
              <div className="flex items-center gap-2 border-b border-line bg-raised/60 px-5 py-3.5">
                <span className="size-3 rounded-full bg-rust/80" />
                <span className="size-3 rounded-full bg-amber/80" />
                <span className="size-3 rounded-full bg-teal/80" />
                <span className="ml-3 text-xs text-muted-foreground">aegis ~ environments/prod</span>
              </div>
              <div className="grid gap-3 p-6 font-mono text-[14px]">
                <p className="flex items-center gap-2">
                  <span className="text-teal">$</span>
                  <span data-cmd className="text-foreground" />
                  <span className="inline-block h-4 w-2 animate-pulse bg-teal/80" aria-hidden />
                </p>
                <p className="plan-line font-sans text-sm text-muted-foreground">Terraform will perform the following actions</p>
                <ul className="grid gap-2.5">
                  {PLAN.map((row) => (
                    <li key={row.address} className="plan-line flex items-baseline gap-4">
                      <PlanGlyph action={row.action} className="w-5 text-2xl leading-none font-bold" />
                      <span className={row.action === "delete" ? "text-rust" : "text-foreground"}>{row.address}</span>
                    </li>
                  ))}
                </ul>
                <p className="plan-line mt-2 border-t border-white/5 pt-4">
                  Plan: <span className="text-teal"><span data-count="add">0</span> to add</span>,{" "}
                  <span className="text-amber"><span data-count="change">0</span> to change</span>,{" "}
                  <span className="text-rust"><span data-count="destroy">0</span> to destroy</span>.
                </p>
              </div>
            </div>

            <div className="approval flex items-center justify-between gap-4 rounded-xl border border-amber/30 bg-card/70 px-5 py-3.5 backdrop-blur-xl">
              <Badge variant="outline" className="gap-1.5 border-amber/40 bg-amber/10 text-amber">
                <span className="size-1.5 animate-pulse rounded-full bg-amber" />
                Awaiting your approval
              </Badge>
              <span className="approve-btn relative inline-flex">
                <span className="absolute inset-0 animate-ping rounded-md bg-primary/30 [animation-duration:2.4s]" />
                <span className="relative rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground">
                  Approve and apply
                </span>
              </span>
            </div>
            <p className="text-center text-sm text-muted-foreground">Nothing is applied until you approve it.</p>
          </div>
        </section>
      </div>
    </AuroraCurtain>
  );
}
