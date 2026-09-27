import { useEffect, useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { clearNextPath, nextPath, useAuth } from "../lib/auth";
import { ErrorNote } from "../components/ui";

export default function Callback() {
  const [params] = useSearchParams();
  const { completeSignIn, identity } = useAuth();
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const code = params.get("code");
    const state = params.get("state");
    if (params.get("error")) setError(new Error(params.get("error_description") || "Sign-in was cancelled."));
    else if (code && state) completeSignIn(code, state).catch(setError);
  }, [params, completeSignIn]);

  useEffect(() => {
    if (identity) clearNextPath();
  }, [identity]);

  if (identity) return <Navigate to={nextPath() ?? "/home"} replace />;
  return (
    <div className="grid h-full place-items-center p-8">
      {error ? (
        <div className="grid max-w-md gap-4">
          <ErrorNote error={error} />
          <a href="/" className="text-sm text-teal underline">
            Back to sign-in
          </a>
        </div>
      ) : (
        <p className="text-muted">Signing you in…</p>
      )}
    </div>
  );
}
