<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { apiGet, apiPost } from '../lib/api'

  // A proxy key inside a fallback key's scope. Full sp- keys never reach the
  // browser, so created_at is the handle — the same one the Keys tab mutates by.
  type ScopeEntry = { created_at: string; name: string; key_prefix: string }
  type PKey = {
    key_prefix: string; name: string; active: boolean
    created_at: string; limits: Record<string, number>
  }

  type AKey = {
    id: string; key_type: string; status: string; name: string
    subscription_type: string; rate_limit_tier: string; role: string
    scope: ScopeEntry[]
    created_at: string | null; updated_at: string | null
    expires_at: number | null; has_refresh_token: boolean
  }

  // A parked key: upstream rate-limited it and the pool is turning requests away.
  type Cooldown = { key_id: string; model: string | null; seconds_left: number }

  let keys = $state<AKey[]>([])
  let cooldowns = $state<Cooldown[]>([])
  let error = $state('')
  let busy = $state(false)

  // Scope editor state ('' = closed, 'new' = the add-API-key form)
  let scopeId = $state('')
  let proxyKeys = $state<PKey[]>([])
  let scopeSel = $state<Set<string>>(new Set())

  // Add-API-key form state
  let showApiForm = $state(false)
  let apiKeyValue = $state('')
  let apiKeyName = $state('')
  let apiKeyRole = $state<'fallback' | 'primary'>('fallback')

  // Add-OAuth wizard state
  let newName = $state('')
  let oauthState = $state('')       // non-empty → wizard step 2 (paste code)
  let authorizeUrl = $state('')
  let redirectUri = $state('')
  let pasted = $state('')
  let successKeyId = $state('')
  let bc: BroadcastChannel | null = null
  let storageHandler: ((ev: StorageEvent) => void) | null = null

  // Inline rename state
  let renameId = $state('')
  let renameValue = $state('')

  async function load() {
    try {
      const r = await apiGet<{ keys: AKey[]; cooldowns?: Cooldown[] }>('/api/anthropic/keys')
      keys = r.keys
      cooldowns = r.cooldowns ?? []
    }
    catch (e) { error = String(e) }
  }

  function keyName(id: string): string {
    const k = keys.find(k => k.id === id)
    return k?.name || id.slice(0, 12)
  }
  function fmtLeft(seconds: number): string {
    if (seconds < 60) return `${seconds}s`
    const m = Math.floor(seconds / 60)
    if (m < 60) return `${m}m ${seconds % 60}s`
    return `${Math.floor(m / 60)}h ${m % 60}m`
  }
  const clearCooldowns = () =>
    act(() => apiPost('/api/anthropic/cooldowns/clear'))

  function isOn(k: AKey): boolean {
    return k.status === 'active' || k.status === 'low_balance'
  }
  function fmtExpiry(ms: number | null): string {
    if (!ms) return '—'
    const d = ms - Date.now()
    if (d <= 0) return 'expired'
    const h = Math.floor(d / 3600_000)
    const m = Math.floor((d % 3600_000) / 60_000)
    return h > 0 ? `in ${h}h ${m}m` : `in ${m}m`
  }
  function fmtDate(s: string | null): string {
    return s ? s.slice(0, 10) : '—'
  }
  function fmtDateTime(s: string | null): string {
    return s ? s.slice(0, 19).replace('T', ' ') + ' UTC' : ''
  }
  function fmtLived(fromISO: string | null, toISO: string | null): string {
    if (!fromISO || !toISO) return ''
    const ms = Date.parse(toISO) - Date.parse(fromISO)
    if (!(ms > 0)) return ''
    const d = Math.floor(ms / 86400_000)
    const h = Math.floor((ms % 86400_000) / 3600_000)
    return d > 0 ? `${d}d ${h}h` : `${h}h`
  }
  // For an inactive key, updated_at is frozen at the moment it was deactivated
  // (nothing writes the row afterwards), so it doubles as the deactivation time.
  function deactivatedAt(k: AKey): string | null {
    return isOn(k) ? null : k.updated_at
  }

  function stopListening() {
    bc?.close(); bc = null
    if (storageHandler) { window.removeEventListener('storage', storageHandler); storageHandler = null }
  }
  function finish(keyId: string) {
    successKeyId = keyId
    oauthState = ''; authorizeUrl = ''; pasted = ''; newName = ''
    stopListening()
    load()
  }
  // The /callback page (same origin, e.g. via SSH -L forward) broadcasts
  // {ok, state, key_id} when it completes the exchange server-side.
  function listenForCallback(state: string) {
    stopListening()
    try {
      bc = new BroadcastChannel('smart_proxy_oauth')
      bc.onmessage = (ev) => {
        if (ev.data && ev.data.state === state && ev.data.ok) finish(ev.data.key_id)
      }
    } catch { /* BroadcastChannel unavailable — storage event still works */ }
    storageHandler = (ev: StorageEvent) => {
      if (ev.key === 'smart_proxy_oauth_' + state && ev.newValue) {
        try {
          const p = JSON.parse(ev.newValue)
          if (p.ok) finish(p.key_id)
        } catch { /* ignore malformed */ }
      }
    }
    window.addEventListener('storage', storageHandler)
  }
  onDestroy(stopListening)

  async function startOauth(e: Event) {
    e.preventDefault()
    error = ''; successKeyId = ''
    busy = true
    try {
      const r = await apiPost<{ state: string; authorize_url: string; redirect_uri: string }>(
        '/api/anthropic/oauth/start', { name: newName.trim() })
      oauthState = r.state
      authorizeUrl = r.authorize_url
      redirectUri = r.redirect_uri
      listenForCallback(r.state)
      window.open(r.authorize_url, '_blank', 'noopener,noreferrer')
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  async function submitCode(e: Event) {
    e.preventDefault()
    if (!pasted.trim()) return
    error = ''
    busy = true
    try {
      const r = await apiPost<{ ok: boolean; key_id: string }>(
        '/api/anthropic/oauth/submit', { state: oauthState, code: pasted.trim() })
      finish(r.key_id)
    } catch (e) {
      error = 'OAuth attempt failed — start again. ' + String(e)
      cancelOauth()
    }
    finally { busy = false }
  }
  function cancelOauth() {
    oauthState = ''; authorizeUrl = ''; pasted = ''
    stopListening()
  }

  async function act(fn: () => Promise<unknown>) {
    error = ''
    busy = true
    try { await fn(); await load() }
    catch (e) { error = String(e) }
    finally { busy = false }
  }
  const toggle = (k: AKey) =>
    act(() => apiPost('/api/anthropic/keys/status', { id: k.id, active: !isOn(k) }))
  const refresh = (k: AKey) =>
    act(() => apiPost('/api/anthropic/keys/refresh', { id: k.id }))
  const setRole = (k: AKey) =>
    act(() => apiPost('/api/anthropic/keys/role',
      { id: k.id, role: k.role === 'standby' ? 'primary' : 'standby' }))
  // api_key rows toggle primary ↔ fallback. 'primary' is the spending direction:
  // an unscoped paid key serves everything, Claude Code included.
  const setApiRole = (k: AKey) => {
    const next = k.role === 'fallback' ? 'primary' : 'fallback'
    if (next === 'primary' && !confirm(
      `Make "${k.name || k.id}" a primary key? It will serve ALL traffic — including Claude Code — and spend real money.`
    )) return
    return act(() => apiPost('/api/anthropic/keys/role', { id: k.id, role: next }))
  }

  async function startScope(k: AKey) {
    error = ''
    scopeId = k.id
    scopeSel = new Set(k.scope.map((s) => s.created_at))
    if (!proxyKeys.length) {
      try { proxyKeys = (await apiGet<{ keys: PKey[] }>('/api/keys')).keys }
      catch (e) { error = String(e) }
    }
  }
  const hasLimit = (p: PKey) => Object.keys(p.limits || {}).length > 0
  function toggleScope(createdAt: string) {
    const next = new Set(scopeSel)
    if (next.has(createdAt)) next.delete(createdAt); else next.add(createdAt)
    scopeSel = next
  }
  const saveScope = () =>
    act(async () => {
      await apiPost('/api/anthropic/keys/scope',
        { id: scopeId, proxy_keys: [...scopeSel] })
      scopeId = ''
    })

  async function openApiForm() {
    error = ''
    showApiForm = true
    apiKeyValue = ''; apiKeyName = ''; apiKeyRole = 'fallback'
    scopeSel = new Set()
    if (!proxyKeys.length) {
      try { proxyKeys = (await apiGet<{ keys: PKey[] }>('/api/keys')).keys }
      catch (e) { error = String(e) }
    }
  }
  function closeApiForm() { showApiForm = false; apiKeyValue = '' }
  const submitApiKey = (e: Event) => {
    e.preventDefault()
    if (!apiKeyValue.trim()) return
    if (apiKeyRole === 'primary' && !confirm(
      'Add as a primary key? It will serve ALL traffic — including Claude Code — and spend real money.'
    )) return
    return act(async () => {
      await apiPost('/api/anthropic/keys/apikey', {
        api_key: apiKeyValue.trim(),
        name: apiKeyName.trim(),
        role: apiKeyRole,
        proxy_keys: apiKeyRole === 'fallback' ? [...scopeSel] : [],
      })
      closeApiForm()
    })
  }
  const del = (k: AKey) => {
    if (!confirm(`Delete key "${k.name || k.id}"? Usage history is kept; the key stops being used.`)) return
    return act(() => apiPost('/api/anthropic/keys/delete', { id: k.id }))
  }
  function startRename(k: AKey) { renameId = k.id; renameValue = k.name }
  const saveRename = () => {
    const name = renameValue.trim()
    if (!name) return
    return act(async () => {
      await apiPost('/api/anthropic/keys/rename', { id: renameId, name })
      renameId = ''
    })
  }

  onMount(load)

  // The banner shows a live countdown, so refresh it while anything is parked —
  // and only then, so an idle dashboard keeps polling nothing.
  const tick = setInterval(() => { if (cooldowns.length) load() }, 15000)
  onDestroy(() => clearInterval(tick))
</script>

{#if !oauthState}
  <form class="create" onsubmit={startOauth}>
    <input placeholder="new OAuth session name" bind:value={newName} disabled={busy} />
    <button class="primary" type="submit" disabled={busy}>Add OAuth session</button>
    {#if !showApiForm}
      <button type="button" onclick={openApiForm} disabled={busy}>Add API key…</button>
    {/if}
  </form>
{:else}
  <form class="oauth-paste" onsubmit={submitCode}>
    <label for="oauth-cb">Paste the callback URL (or code) from the authorized tab</label>
    <textarea id="oauth-cb" rows="3" placeholder="https://…/callback?code=…&amp;state=…"
              bind:value={pasted} disabled={busy}></textarea>
    <div class="oauth-actions">
      <button class="primary" type="submit" disabled={busy || !pasted.trim()}>Save OAuth key</button>
      <button type="button" onclick={cancelOauth} disabled={busy}>Cancel</button>
    </div>
  </form>
  <p>
    Authorize in the opened tab (popup blocked? <a href={authorizeUrl} target="_blank" rel="noopener noreferrer">open manually</a>),
    then copy the <strong>whole callback URL</strong> from its address bar and paste it above —
    even if the tab shows an error page, the URL is what matters.
  </p>
  <p class="muted small">
    Tip: if the proxy runs on this machine (or via an SSH <code>-L</code> forward to
    <code>{redirectUri}</code>), the tab returns here automatically and you can skip the paste.
  </p>
{/if}

{#if successKeyId}
  <div class="created">
    <span>OAuth key saved and active:</span>
    <code>{successKeyId}</code>
    <button type="button" onclick={() => { successKeyId = '' }}>Dismiss</button>
  </div>
{/if}

{#if error}<p class="err">{error}</p>{/if}

{#if cooldowns.length}
  <div class="cooldowns">
    <div class="cooldowns-body">
      <strong class="warn">Rate-limited — the pool is turning requests away.</strong>
      <ul class="cooldown-list">
        {#each cooldowns as c}
          <li><code>{c.model ?? 'every model'}</code> on {keyName(c.key_id)} — {fmtLeft(c.seconds_left)} left</li>
        {/each}
      </ul>
      <span class="muted small">
        The wait is upstream's own retry-after, clamped to one hour — so a limit that resets
        further out re-arms a fresh hour every hour. Clearing asks upstream again instead of
        sitting out the clamp; it buys no quota, and if the limit still stands the next
        request simply re-arms it.
      </span>
    </div>
    <button type="button" onclick={clearCooldowns} disabled={busy}>Clear cooldowns</button>
  </div>
{/if}

{#if showApiForm}
  <form class="scope-editor" onsubmit={submitApiKey}>
    <p class="scope-title">Add an Anthropic API key</p>
    <input type="password" autocomplete="off" placeholder="sk-ant-…"
           bind:value={apiKeyValue} disabled={busy} />
    <input placeholder="label (optional)" bind:value={apiKeyName} disabled={busy} />
    <label class="scope-row">
      <input type="radio" value="fallback" bind:group={apiKeyRole} disabled={busy} />
      <span><strong>fallback</strong> — paid backup for the consumers below only, never Claude Code</span>
    </label>
    <label class="scope-row">
      <input type="radio" value="primary" bind:group={apiKeyRole} disabled={busy} />
      <span><strong>primary</strong> — serves all traffic, Claude Code included</span>
    </label>
    {#if apiKeyRole === 'fallback'}
      <p class="muted small">
        Consumers allowed to fall back onto this key. Leave empty and the key stays inert.
      </p>
      {#each proxyKeys.filter((p) => p.active) as p (p.created_at)}
        <label class="scope-row" class:disabled={!hasLimit(p)}>
          <input type="checkbox" checked={scopeSel.has(p.created_at)}
                 disabled={busy || !hasLimit(p)}
                 onchange={() => toggleScope(p.created_at)} />
          <span>{p.name || p.key_prefix}</span>
          <code class="small">{p.key_prefix}…</code>
          {#if !hasLimit(p)}<span class="muted small">no spend limit — set one in Keys first</span>{/if}
        </label>
      {/each}
    {/if}
    <div class="oauth-actions">
      <button class="primary" type="submit" disabled={busy || !apiKeyValue.trim()}>Add key</button>
      <button type="button" onclick={closeApiForm} disabled={busy}>Cancel</button>
    </div>
  </form>
{/if}

{#if scopeId}
  <div class="scope-editor">
    <p class="scope-title">
      Proxy keys allowed to fall back onto this paid key
      <span class="muted small">— everything else keeps getting a 429 instead. Claude Code traffic is blocked outright, whichever key it comes from.</span>
    </p>
    {#each proxyKeys.filter((p) => p.active) as p (p.created_at)}
      <label class="scope-row" class:disabled={!hasLimit(p)}>
        <input type="checkbox" checked={scopeSel.has(p.created_at)}
               disabled={busy || !hasLimit(p)}
               onchange={() => toggleScope(p.created_at)} />
        <span>{p.name || p.key_prefix}</span>
        <code class="small">{p.key_prefix}…</code>
        {#if !hasLimit(p)}<span class="muted small">no spend limit — set one in Keys first</span>{/if}
      </label>
    {/each}
    <div class="oauth-actions">
      <button class="primary" type="button" onclick={saveScope} disabled={busy}>Save scope</button>
      <button type="button" onclick={() => { scopeId = '' }} disabled={busy}>Cancel</button>
    </div>
  </div>
{/if}

<table>
  <thead>
    <tr><th>Name</th><th>Type</th><th>Status</th><th>Subscription</th><th>Role</th><th>Scope</th><th>Token expires</th><th>Created</th><th>Deactivated</th><th></th></tr>
  </thead>
  <tbody>
    {#each keys as k (k.id)}
      <tr>
        <td>
          {#if renameId === k.id}
            <input bind:value={renameValue} disabled={busy} />
            <button type="button" onclick={saveRename} disabled={busy || !renameValue.trim()}>Save</button>
            <button type="button" onclick={() => { renameId = '' }} disabled={busy}>Cancel</button>
          {:else}
            {k.name || '—'}
            <button type="button" onclick={() => startRename(k)} disabled={busy}>Rename</button>
          {/if}
        </td>
        <td>{k.key_type}</td>
        <td>{k.status}</td>
        <td>{k.subscription_type}{k.rate_limit_tier ? ` / ${k.rate_limit_tier}` : ''}</td>
        <td>{k.role}</td>
        <td>
          {#if k.role !== 'fallback'}
            —
          {:else if k.scope.length}
            {k.scope.map((s) => s.name || s.key_prefix).join(', ')}
          {:else}
            <span class="warn">no consumers — never used</span>
          {/if}
        </td>
        <td>{k.key_type === 'oauth' ? fmtExpiry(k.expires_at) : '—'}</td>
        <td>{fmtDate(k.created_at)}</td>
        <td>
          {#if deactivatedAt(k)}
            <span title={fmtDateTime(deactivatedAt(k))}>{fmtDate(deactivatedAt(k))}</span>
            {#if fmtLived(k.created_at, deactivatedAt(k))}
              <span class="lived">lived {fmtLived(k.created_at, deactivatedAt(k))}</span>
            {/if}
          {:else}
            —
          {/if}
        </td>
        <td>
          <button type="button" onclick={() => toggle(k)} disabled={busy}>{isOn(k) ? 'Disable' : 'Enable'}</button>
          {#if k.key_type === 'oauth' && k.has_refresh_token}
            <button type="button" onclick={() => refresh(k)} disabled={busy}>Refresh</button>
          {/if}
          {#if k.key_type === 'oauth'}
            <button type="button" onclick={() => setRole(k)} disabled={busy}>
              {k.role === 'standby' ? 'Make primary' : 'Make standby'}
            </button>
          {:else if k.key_type === 'api_key'}
            <button type="button" onclick={() => setApiRole(k)} disabled={busy}>
              {k.role === 'fallback' ? 'Make primary' : 'Make fallback'}
            </button>
            {#if k.role === 'fallback'}
              <button type="button" onclick={() => startScope(k)} disabled={busy}>Scope…</button>
            {/if}
          {/if}
          <button type="button" onclick={() => del(k)} disabled={busy}>Delete</button>
        </td>
      </tr>
    {/each}
  </tbody>
</table>

<style>
  .lived { display: block; font-size: 12px; color: var(--muted); }
  .oauth-paste { display: flex; flex-direction: column; gap: 8px; max-width: 720px; margin: 12px 0; }
  .oauth-paste label { font-size: 13px; color: var(--muted); }
  .oauth-paste textarea {
    width: 100%; min-height: 72px; padding: 8px 10px; box-sizing: border-box;
    border: 1px solid #d1d5db; border-radius: 6px; font: inherit; font-size: 13px; resize: vertical;
  }
  .oauth-actions { display: flex; gap: 10px; }
  .small { font-size: 12px; }
  .scope-editor {
    display: flex; flex-direction: column; gap: 6px; max-width: 720px;
    margin: 12px 0; padding: 12px; border: 1px solid #d1d5db; border-radius: 6px;
  }
  .scope-title { margin: 0 0 4px; }
  .scope-row { display: flex; align-items: center; gap: 8px; }
  .scope-row.disabled { opacity: 0.55; }
  .warn { color: #b45309; }
  .cooldowns {
    display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
    margin: 12px 0; padding: 12px; max-width: 720px;
    border: 1px solid #fcd34d; border-radius: 6px; background: #fffbeb;
  }
  .cooldowns-body { display: flex; flex-direction: column; gap: 6px; }
  .cooldowns button { flex: none; }
  .cooldown-list { margin: 0; padding-left: 18px; font-size: 13px; }
</style>
