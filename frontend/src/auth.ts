const TOKEN_KEY = "whumpf_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

// Drop-in fetch wrapper that injects auth headers and fires
// "whumpf:unauthorized" on 401 so App can redirect to login.
export async function apiFetch(url: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(url, {
    ...init,
    headers: {
      ...authHeaders(),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (r.status === 401) {
    window.dispatchEvent(new Event("whumpf:unauthorized"));
  }
  return r;
}
