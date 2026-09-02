export class Unauthorized extends Error {
  // The server says whether an admin secret is configured at all. Without it
  // an operator who never set ANTHROPIC_PROXY_DASHBOARD_SECRET has no way to
  // learn that from the UI.
  adminSecretConfigured: boolean | null = null
}

export class Forbidden extends Error {
  adminSecretConfigured = false
  hint = ''
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'include', ...init })
  if (r.status === 401 || r.status === 403) {
    let body: any = null
    try { body = await r.json() } catch { /* non-JSON body */ }
    if (r.status === 401) {
      const e = new Unauthorized(body?.error ?? 'unauthorized')
      if (typeof body?.admin_secret_configured === 'boolean') {
        e.adminSecretConfigured = body.admin_secret_configured
      }
      throw e
    }
    const e = new Forbidden(body?.error ?? 'forbidden')
    e.adminSecretConfigured = body?.admin_secret_configured === true
    e.hint = typeof body?.hint === 'string' ? body.hint : ''
    throw e
  }
  if (!r.ok) {
    let message = ''
    try {
      const body = await r.json()
      if (body && typeof body.error === 'string') message = body.error
    } catch { /* non-JSON error body — fall back to the plain message */ }
    throw new Error(message ? `${path}: ${r.status} — ${message}` : `${path}: ${r.status}`)
  }
  return (await r.json()) as T
}

export function apiGet<T>(path: string): Promise<T> {
  return req<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return req<T>(path, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
}

export function login(token: string): Promise<{ ok: boolean; admin: boolean }> {
  return apiPost('/api/session', { token })
}
