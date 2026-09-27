// One fetch wrapper for both services. Paths are public paths:
//   /api/*, /auth/aws/*   -> api   (CloudFront -> ALB -> :3005)
//   /agent-api/*          -> agent (CloudFront -> ALB -> :8000)

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let tokenGetter: () => string | null = () => null;
let onUnauthorized: () => void = () => {};

export function bindAuth(getToken: () => string | null, unauthorized: () => void) {
  tokenGetter = getToken;
  onUnauthorized = unauthorized;
}

type Options = { method?: string; body?: unknown; form?: FormData; signal?: AbortSignal };

export async function request<T = unknown>(path: string, opts: Options = {}): Promise<T> {
  const token = tokenGetter();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";

  const resp = await fetch(path, {
    method: opts.method ?? (opts.body !== undefined || opts.form ? "POST" : "GET"),
    headers,
    body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
    signal: opts.signal,
  });

  const payload = await resp.json().catch(() => null);
  if (!resp.ok) {
    if (resp.status === 401) onUnauthorized();
    const message = payload?.error || payload?.detail || `Request failed (${resp.status}).`;
    throw new ApiError(String(message), resp.status, payload?.code);
  }
  return payload as T;
}

/** Authenticated download (the endpoint needs a Bearer token, so no plain <a href>). */
export async function download(path: string, filename: string) {
  const token = tokenGetter();
  const resp = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!resp.ok) throw new ApiError(`Download failed (${resp.status}).`, resp.status);
  const url = URL.createObjectURL(await resp.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * Server-sent events over fetch, so the stream can carry the Authorization
 * header EventSource cannot. Resolves when the server ends the stream (the
 * agent sends DONE after each plan/apply) or the signal aborts.
 */
export async function streamEvents(path: string, onData: (line: string) => void, signal: AbortSignal) {
  const token = tokenGetter();
  const resp = await fetch(path, {
    headers: { Accept: "text/event-stream", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    signal,
  });
  if (!resp.ok || !resp.body) throw new ApiError(`Stream failed (${resp.status}).`, resp.status);

  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() ?? "";
    for (const event of events) {
      const data = event
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).replace(/^ /, ""))
        .join("\n");
      if (data) onData(data);
    }
  }
}
