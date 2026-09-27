import { Button, ErrorNote } from "../components/ui";
import Mark from "../components/Mark";
import PlanGlyph from "../components/PlanGlyph";
import { useAuth } from "../lib/auth";

// The hero is the thing AEGIS exists to produce: a plan you can read before
// anything touches your account.
const SAMPLE = [
  { action: "create", address: "aws_s3_bucket.artifacts" },
  { action: "create", address: "aws_s3_bucket_versioning.artifacts" },
  { action: "update", address: "aws_iam_role_policy.ci" },
  { action: "delete", address: "aws_instance.legacy_worker" },
] as const;

export default function Login() {
  const { login, error } = useAuth();
  return (
    <div className="grid min-h-full lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <section className="flex flex-col justify-between gap-12 p-8 sm:p-12">
        <div className="flex items-center gap-2 text-teal">
          <Mark className="size-7" />
          <span className="text-lg font-bold tracking-[0.08em] text-fg">AEGIS</span>
        </div>

        <div className="max-w-md">
          <h1 className="text-4xl leading-[1.1] font-semibold tracking-tight sm:text-5xl">
            Describe the infrastructure. Review the plan. Then apply.
          </h1>
          <p className="mt-5 text-lg text-muted">
            AEGIS writes Terraform for your AWS accounts, shows you every change before it happens, and opens a pull
            request with the code.
          </p>
          <div className="mt-8 grid gap-3">
            <Button variant="primary" onClick={login} className="h-11 w-fit px-6 text-base">
              Sign in
            </Button>
            <ErrorNote error={error} />
          </div>
        </div>

        <p className="text-sm text-muted">Access is by invitation. Ask your AEGIS administrator for an account.</p>
      </section>

      <section className="hidden items-center border-l border-line bg-surface p-12 lg:flex" aria-hidden>
        <div className="w-full max-w-xl font-mono text-[15px]">
          <p className="mb-6 font-sans text-sm text-muted">Terraform will perform the following actions</p>
          <ul className="grid gap-3">
            {SAMPLE.map((row) => (
              <li key={row.address} className="flex items-baseline gap-4">
                <PlanGlyph action={row.action} className="w-5 text-2xl leading-none" />
                <span className={row.action === "delete" ? "text-rust" : "text-fg"}>{row.address}</span>
              </li>
            ))}
          </ul>
          <p className="mt-8 border-t border-line pt-5">
            Plan: <span className="text-teal">2 to add</span>, <span className="text-amber">1 to change</span>,{" "}
            <span className="text-rust">1 to destroy</span>.
          </p>
          <p className="mt-2 font-sans text-sm text-muted">Nothing is applied until you approve it.</p>
        </div>
      </section>
    </div>
  );
}
