const TOKEN_KEY = "qat_token";
const USER_KEY = "qat_user";

export interface AuthUser {
  id: string;
  email: string;
  created_at: number;
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function getStoredUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

function setSession(token: string, user: AuthUser) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

interface TokenResponse {
  access_token: string;
  token_type: string;
  user: AuthUser;
}

async function parseErr(r: Response): Promise<string> {
  try {
    const j = await r.json();
    return j.detail || j.error || `Request failed (${r.status})`;
  } catch {
    return `Request failed (${r.status})`;
  }
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) throw new Error(await parseErr(r));
  const data: TokenResponse = await r.json();
  setSession(data.access_token, data.user);
  return data.user;
}

export async function register(email: string, password: string): Promise<AuthUser> {
  const r = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) throw new Error(await parseErr(r));
  const data: TokenResponse = await r.json();
  setSession(data.access_token, data.user);
  return data.user;
}

export function logout() {
  clearSession();
}
