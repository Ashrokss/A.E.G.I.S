import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import Mark from "@/components/Mark";
import ThemeSwitch from "@/components/ThemeSwitch";
import { Panel } from "@/components/ui";
import { ConnectAccountForm } from "./Accounts";

/** First stop for a new sign-up: connect an AWS account, on its own screen
 * (no app shell behind it). Cancel returns to the sign-in page; an account can also be connected later from Accounts. */
export default function Connect() {
  const { identity, logout } = useAuth();
  const navigate = useNavigate();
  const home = () => navigate("/home", { replace: true });

  return (
    <div className="min-h-full bg-bg">
      <header className="flex h-16 items-center gap-3 px-4 sm:px-8">
        <span className="flex items-center gap-2 text-teal">
          <Mark className="size-6" />
          <span className="font-display text-[17px] font-bold tracking-[0.06em] text-fg">AEGIS</span>
        </span>
        <div className="ml-auto">
          <ThemeSwitch />
        </div>
      </header>

      <main className="mx-auto grid w-full max-w-2xl gap-6 px-4 pt-4 pb-16 sm:pt-10">
        <div className="grid gap-2">
          <p className="text-sm text-muted">Signed in as {identity?.email}</p>
          <h1 className="text-2xl font-semibold sm:text-3xl">Connect your AWS account</h1>
          <p className="text-pretty text-muted">
            AEGIS runs Terraform only through a role you create in your own account, and keeps the state there too. You
            never share access keys, and nothing changes until you approve a plan.
          </p>
        </div>
        <Panel className="p-5 sm:p-7">
          <ConnectAccountForm onDone={home} onCancel={logout} />
        </Panel>
      </main>
    </div>
  );
}
