// Session is held in an httpOnly cookie set by the backend on /auth/token and
// /auth/register. JavaScript cannot read or write the cookie — every fetch just
// needs `credentials: "include"` to send it on same-site requests to the API.
//
// Result: no XSS-readable token, no localStorage. Logout calls /auth/logout
// which clears the cookie server-side.

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

// Drop-in fetch wrapper that:
//   - sends the session cookie via credentials: "include"
//   - fires "whumpf:unauthorized" on 401 so App can redirect to login
export async function apiFetch(url: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(url, {
    ...init,
    credentials: "include",
    headers: {
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (r.status === 401) {
    window.dispatchEvent(new Event("whumpf:unauthorized"));
  }
  return r;
}

// Best-effort logout — clears the cookie server-side. Caller still updates
// local state regardless of whether the request succeeds.
export async function logout(): Promise<void> {
  try {
    await fetch(`${API_URL}/auth/logout`, { method: "POST", credentials: "include" });
  } catch {
    // Network failure — local state will still be cleared by the caller.
  }
}
