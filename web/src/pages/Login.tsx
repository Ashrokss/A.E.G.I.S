import { useRef, useState } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { SplitText } from "gsap/SplitText";
import { DrawSVGPlugin } from "gsap/DrawSVGPlugin";
import { ArrowRight, Cloud, GitPullRequest, KeyRound, ScanEye } from "lucide-react";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import AuroraCurtain from "@/components/smoothui/aurora-curtain";
import SmoothButton from "@/components/smoothui/smooth-button";
import { SIGNUP_ENABLED, useAuth } from "@/lib/auth";
import ThemeSwitch from "@/components/ThemeSwitch";

gsap.registerPlugin(useGSAP, SplitText, DrawSVGPlugin);

const PROMISES = [
  { icon: ScanEye, text: "Every change shown as a plan before it happens" },
  { icon: KeyRound, text: "Runs only through a role you create in your account" },
  { icon: GitPullRequest, text: "A pull request with the code for every run" },
];

// One GSAP timeline plays the page, once: the mark draws itself, the headline
// comes into focus word by word, then the sign-in card opens from its centre.
export default function Login() {
  const { login, signup, error } = useAuth();
  const [busy, setBusy] = useState<"login" | "signup" | null>(null);
  const root = useRef<HTMLDivElement>(null);

  useGSAP(
    (_ctx, contextSafe) => {
      const q = gsap.utils.selector(root);
      const mm = gsap.matchMedia();

      mm.add("(prefers-reduced-motion: reduce)", () => {
        gsap.set(root.current, { autoAlpha: 1 });
      });

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        // Split after the webfonts load, or the words measure the fallback font.
        const build = contextSafe!(() => {
          gsap.set(root.current, { autoAlpha: 1 });

          const css = getComputedStyle(document.documentElement);
          const ramp = gsap.utils.interpolate(css.getPropertyValue("--teal").trim(), css.getPropertyValue("--amber").trim());
          const title = SplitText.create(q(".hero-title"), { type: "words,chars" });
          const grad = title.chars.filter((c) => c.closest(".hero-grad"));
          grad.forEach((c, i) => ((c as HTMLElement).style.color = ramp(i / Math.max(grad.length - 1, 1))));

          const tl = gsap.timeline({ defaults: { ease: "expo.out", duration: 1.2 } });
          tl.from(q(".mark-shield"), { drawSVG: "50% 50%", duration: 1.1, ease: "power3.inOut" })
            .from(q(".mark-plus"), { scale: 0, rotation: -90, transformOrigin: "50% 50%", ease: "back.out(2.5)", duration: 0.8 }, "-=0.45")
            .from(q(".wordmark"), { autoAlpha: 0, letterSpacing: "0.4em", duration: 1.2 }, "<")
            .from(title.words, { autoAlpha: 0, filter: "blur(14px)", y: 18, scale: 0.96, stagger: 0.07, duration: 1.1 }, 0.3)
            .from(q(".hero-lead"), { autoAlpha: 0, filter: "blur(8px)", y: 10 }, "-=0.8")
            .from(q(".promise"), { autoAlpha: 0, x: -10, stagger: 0.08, duration: 0.9 }, "-=0.9")
            .from(
              q(".signin"),
              { clipPath: "inset(48% 48% 48% 48% round 14px)", scale: 0.94, autoAlpha: 0, duration: 1.4, ease: "expo.inOut" },
              0.55,
            )
            .from(q(".signin-item"), { y: 12, autoAlpha: 0, stagger: 0.06, duration: 0.9 }, "-=0.55")
            .set(q(".signin"), { clearProps: "clipPath" });
        });

        let cancelled = false;
        document.fonts.ready.then(() => {
          if (!cancelled) build();
        });
        return () => {
          cancelled = true;
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
        className="invisible relative mx-auto grid min-h-dvh max-w-6xl items-center gap-12 px-6 py-16 sm:px-10 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)] lg:gap-20"
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

          <ul className="grid gap-3">
            {PROMISES.map(({ icon: Icon, text }) => (
              <li key={text} className="promise flex items-center gap-3 text-sm text-muted-foreground">
                <span className="glass glass-flat grid size-8 shrink-0 place-items-center rounded-full text-primary">
                  <Icon className="size-4" aria-hidden />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </section>

        <section className="w-full max-w-md justify-self-center lg:justify-self-end">
          <div className="signin">
              <Card className="glass gap-5 rounded-2xl py-8">
                <CardHeader className="signin-item">
                  <CardTitle className="text-xl">Sign in to AEGIS</CardTitle>
                  <CardDescription>{SIGNUP_ENABLED ? "Welcome back. Sign in to your workspace." : "Use the account your administrator invited."}</CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4">
                  <div className="signin-item">
                    <SmoothButton
                      variant="candy"
                      size="lg"
                      className="w-full [--btn-fg:var(--on-accent)] [--btn-hover:color-mix(in_oklab,var(--teal)_78%,black)] [--btn:var(--teal)]"
                      loading={busy === "login"}
                      disabled={!!busy}
                      suffix={<ArrowRight aria-hidden />}
                      onClick={async () => {
                        setBusy("login");
                        await login();
                        setBusy(null);
                      }}
                    >
                      {SIGNUP_ENABLED ? "Sign in" : "Continue with single sign-on"}
                    </SmoothButton>
                  </div>
                  {error && (
                    <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                      {error}
                    </p>
                  )}
                  {SIGNUP_ENABLED ? (
                    <>
                      <div className="signin-item flex items-center gap-3 text-xs text-muted-foreground">
                        <span className="h-px flex-1 bg-line" aria-hidden />
                        New to AEGIS?
                        <span className="h-px flex-1 bg-line" aria-hidden />
                      </div>
                      <div className="signin-item grid gap-2">
                        <SmoothButton
                          variant="outline"
                          size="lg"
                          className="w-full"
                          loading={busy === "signup"}
                          disabled={!!busy}
                          prefix={<Cloud aria-hidden />}
                          onClick={async () => {
                            setBusy("signup");
                            await signup();
                            setBusy(null);
                          }}
                        >
                          Connect your AWS account
                        </SmoothButton>
                        <p className="text-xs leading-relaxed text-muted-foreground">
                          Create your login, then connect your account with a role you create through CloudFormation. You
                          never share access keys.
                        </p>
                      </div>
                    </>
                  ) : (
                    <>
                      <Separator className="signin-item" />
                      <p className="signin-item text-sm text-muted-foreground">
                        Nothing touches your AWS account until you approve the plan.
                      </p>
                    </>
                  )}
                </CardContent>
                <CardFooter className="signin-item text-xs text-muted-foreground">
                  {SIGNUP_ENABLED
                    ? "Nothing touches your AWS account until you approve the plan."
                    : "Access is by invitation. No account yet? Ask your AEGIS administrator."}
                </CardFooter>
              </Card>
          </div>
        </section>
      </div>
    </AuroraCurtain>
  );
}
