// Cognito hosted UI, authorization code + PKCE (public client, no secret).
// Cognito authenticates; the api authorizes from PostgreSQL (/api/auth/me) —
// the role shown here is always the server's answer, never a token claim.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { authConfigError, COGNITO_CLIENT_ID, COGNITO_DOMAIN } from "./config";
import { ApiError, bindAuth, request } from "./api";
import type { Identity } from "./types";

type Tokens = { accessToken: string; idToken?: string; refreshToken?: string; expiresAt: number };

const SESSION_KEY = "aegis.session";
const PKCE_KEY = "aegis.pkce";
const REFUSAL_KEY = "aegis.refusal";

type AuthState = {
  ready: boolean;
  identity: Identity | null;
  error: string | null;
  login: () => Promise<void>;
  logout: () => void;
  completeSignIn: (code: string, state: string) => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

function randomString(bytes = 32) {
  const buf = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...buf)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challengeFor(verifier: string) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function readSession(): Tokens | null {
  try {
    return JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
  } catch {
    return null;
  }
}

function cognitoLogoutUrl() {
  const url = new URL(`${COGNITO_DOMAIN}/logout`);
  url.searchParams.set("client_id", COGNITO_CLIENT_ID);
  url.searchParams.set("logout_uri", window.location.origin);
  return url.toString();
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [tokens, setTokens] = useState<Tokens | null>(readSession);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(() => sessionStorage.getItem(REFUSAL_KEY) || authConfigError);
  const tokensRef = useRef(tokens);
  tokensRef.current = tokens;

  const store = useCallback((t: Tokens | null) => {
    if (t) sessionStorage.setItem(SESSION_KEY, JSON.stringify(t));
    else sessionStorage.removeItem(SESSION_KEY);
    setTokens(t);
  }, []);

  const logout = useCallback(() => {
    const hadSession = !!tokensRef.current;
    store(null);
    setIdentity(null);
    fetch("/auth/aws/logout", { method: "POST" }).catch(() => {});
    // End the Cognito session too, or the next sign-in is silent.
    if (hadSession && !authConfigError && import.meta.env.VITE_DEMO !== "1") window.location.href = cognitoLogoutUrl();
  }, [store]);

  useEffect(() => {
    bindAuth(() => tokensRef.current?.accessToken ?? null, () => {
      store(null);
      setIdentity(null);
    });
  }, [store]);

  // Restore: re-ask the server who this is (role / enabled can change any time).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (tokensRef.current) {
        try {
          const me = await request<{ user: Identity }>("/api/auth/me");
          if (!cancelled) setIdentity(me.user);
        } catch {
          if (!cancelled) store(null);
        }
      }
      if (!cancelled) setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [store]);

  // Refresh two minutes before expiry.
  useEffect(() => {
    if (!tokens?.refreshToken) return;
    const wait = Math.max(5_000, tokens.expiresAt - Date.now() - 120_000);
    const timer = setTimeout(async () => {
      try {
        const out = await request<{ tokens: Tokens }>("/auth/aws/refresh", {
          body: { refresh_token: tokens.refreshToken },
        });
        store({ ...out.tokens, refreshToken: tokens.refreshToken });
      } catch {
        logout();
      }
    }, wait);
    return () => clearTimeout(timer);
  }, [tokens, store, logout]);

  const login = useCallback(async () => {
    // Demo mode (npm run demo): no Cognito, the in-memory mock answers /api/auth/me.
    // VITE_DEMO is inlined at build time, so a normal build drops this branch entirely.
    if (import.meta.env.VITE_DEMO === "1") {
      store({ accessToken: "demo", expiresAt: Date.now() + 12 * 3_600_000 });
      const me = await request<{ user: Identity }>("/api/auth/me");
      setIdentity(me.user);
      return;
    }
    if (authConfigError) return setError(authConfigError);
    sessionStorage.removeItem(REFUSAL_KEY);
    const state = randomString();
    const verifier = randomString(64);
    sessionStorage.setItem(PKCE_KEY, JSON.stringify({ state, verifier }));
    const url = new URL(`${COGNITO_DOMAIN}/oauth2/authorize`);
    url.search = new URLSearchParams({
      response_type: "code",
      client_id: COGNITO_CLIENT_ID,
      redirect_uri: `${window.location.origin}/auth/callback`,
      scope: "openid profile email",
      state,
      code_challenge: await challengeFor(verifier),
      code_challenge_method: "S256",
    }).toString();
    window.location.href = url.toString();
  }, [store]);

  const completeSignIn = useCallback(
    async (code: string, state: string) => {
      const saved = sessionStorage.getItem(PKCE_KEY);
      // StrictMode runs effects twice; the second call finds nothing and stops.
      if (!saved) return;
      sessionStorage.removeItem(PKCE_KEY);
      const { state: expected, verifier } = JSON.parse(saved);
      if (state !== expected) throw new Error("Sign-in could not be verified. Start again.");

      const out = await request<{ tokens: Tokens }>("/auth/aws/callback", {
        body: { code, redirect_uri: `${window.location.origin}/auth/callback`, code_verifier: verifier },
      });
      store(out.tokens);
      try {
        const me = await request<{ user: Identity }>("/api/auth/me");
        setIdentity(me.user);
        setError(null);
      } catch (e) {
        // Authenticated by Cognito but not an AEGIS user (or disabled): end the
        // Cognito session so the next attempt can use different credentials.
        const message = e instanceof ApiError ? e.message : "Sign-in failed.";
        sessionStorage.setItem(REFUSAL_KEY, message);
        store(null);
        window.location.href = cognitoLogoutUrl();
      }
    },
    [store],
  );

  const value = useMemo(
    () => ({ ready, identity, error, login, logout, completeSignIn }),
    [ready, identity, error, login, logout, completeSignIn],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
