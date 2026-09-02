<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet, apiPost } from '../lib/api'

  // One entry per supported limit kind. Adding a kind later is one entry here
  // plus one in LIMIT_KINDS on the server — the form and table adapt.
  const LIMIT_KINDS = [
    { id: 'daily_usd', label: '24h spend limit', unit: '$', short: '24h' },
  ] as const

  type LimitInfo = {
    limit_usd: number | null
    spent_usd: number
    remaining_usd: number | null
    percent: number | null
    resets_at: string
    exceeded: boolean
  }
  type Key = {
    key_prefix: string
    name: string
    active: boolean
    created_at: string | null
    limits: Record<string, number>
    usage: Record<string, LimitInfo>
  }

  let keys = $state<Key[]>([])
  let reloadMsg = $state('')
  let error = $state('')
  let busy = $state(false)
  let newName = $state('')
  let createdKey = $state('')
  let copied = $state(false)
  let editing = $state<string | null>(null)          // created_at of the open form
  let draft = $state<Record<string, string>>({})     // kind id -> raw input value

  async function load() {
    try { keys = (await apiGet<{ keys: Key[] }>('/api/keys')).keys }
    catch (e) { error = String(e) }
  }
  async function create(e: Event) {
    e.preventDefault()
    error = ''
    const name = newName.trim()
    if (!name) return
    busy = true
    copied = false
    try {
      const r = await apiPost<{ key: string; name: string }>('/api/keys', { name })
      createdKey = r.key       // full key — shown once, it's immediately usable
      newName = ''
      await load()
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  async function copyKey() {
    try { await navigator.clipboard.writeText(createdKey); copied = true }
    catch { copied = false }
  }
  async function toggle(k: Key) {
    error = ''
    busy = true
    try {
      // Identify by created_at, not key_prefix: distinct keys can share the
      // same 12-char prefix (prefix matching would be ambiguous on the server).
      await apiPost('/api/keys/active', { created_at: k.created_at, active: !k.active })
      await load()
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  async function reload() {
    error = ''
    reloadMsg = ''
    busy = true
    try {
      const r = await apiPost<{ status: string; active: number }>('/api/reload')
      reloadMsg = `${r.status} — ${r.active} active`
    } catch (e) { error = String(e) }
    finally { busy = false }
  }

  function openEdit(k: Key) {
    editing = k.created_at
    const next: Record<string, string> = {}
    for (const kind of LIMIT_KINDS) {
      const v = k.limits?.[kind.id]
      next[kind.id] = v == null ? '' : String(v)
    }
    draft = next
  }
  function closeEdit() {
    editing = null
    draft = {}
  }
  async function saveLimits(k: Key) {
    error = ''
    busy = true
    try {
      // Blank input means "no limit" → null clears the row server-side.
      // draft is declared Record<string, string>, but Svelte's number-input
      // binding coerces the bound value to a number (or null) at runtime, so
      // don't trust the declared type here — coerce to string before trim.
      const limits: Record<string, number | null> = {}
      for (const kind of LIMIT_KINDS) {
        const value = draft[kind.id]
        const raw = (value == null ? '' : String(value)).trim()
        limits[kind.id] = raw === '' ? null : Number(raw)
      }
      const bad = LIMIT_KINDS.find(
        (kind) => limits[kind.id] !== null && !Number.isFinite(limits[kind.id] as number))
      if (bad) {
        error = `${bad.label} must be a number`
        return
      }
      await apiPost('/api/keys/limits', { created_at: k.created_at, limits })
      closeEdit()
      await load()
    } catch (e) { error = String(e) }
    finally { busy = false }
  }

  function money(v: number | null | undefined): string {
    return v == null ? '—' : `$${v.toFixed(2)}`
  }
  function limitSummary(k: Key): string {
    const parts = LIMIT_KINDS
      .filter((kind) => k.limits?.[kind.id] != null)
      .map((kind) => `${money(k.limits[kind.id])} / ${kind.short}`)
    return parts.length ? parts.join(', ') : '—'
  }
  function spentSummary(k: Key): string {
    const info = k.usage?.['daily_usd']
    if (!info) return '—'
    const pct = info.percent == null ? '' : ` · ${Math.round(info.percent)}%`
    return `${money(info.spent_usd)}${pct}`
  }
  function resetsIn(k: Key): string {
    const iso = k.usage?.['daily_usd']?.resets_at
    if (!iso) return '—'
    const secs = (new Date(iso).getTime() - Date.now()) / 1000
    if (!Number.isFinite(secs) || secs <= 0) return 'now'
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    return h ? `${h}h ${m}m` : `${m}m`
  }

  onMount(load)
</script>

<form class="create" onsubmit={create}>
  <input placeholder="new key name" bind:value={newName} disabled={busy} />
  <button class="primary" type="submit" disabled={busy || !newName.trim()}>Create key</button>
</form>

{#if createdKey}
  <div class="created">
    <span>New key — copy now, it won't be shown again (already active):</span>
    <code>{createdKey}</code>
    <button type="button" onclick={copyKey}>{copied ? 'Copied ✓' : 'Copy'}</button>
    <button type="button" onclick={() => { createdKey = ''; copied = false }}>Dismiss</button>
  </div>
{/if}

<div class="toolbar">
  <button class="primary" type="button" onclick={reload} disabled={busy}>Reload proxy</button>
  <span>{reloadMsg}</span>
</div>

{#if error}<p class="err">{error}</p>{/if}

<table>
  <thead>
    <tr>
      <th>Key</th><th>Name</th><th>Active</th>
      <th>Limits</th><th>Spent</th><th>Resets in</th><th></th>
    </tr>
  </thead>
  <tbody>
    {#each keys as k (k.created_at)}
      <tr class:over={k.usage?.['daily_usd']?.exceeded}>
        <td>{k.key_prefix}…</td>
        <td>{k.name}</td>
        <td>{k.active ? 'yes' : 'no'}</td>
        <td>{limitSummary(k)}</td>
        <td>{spentSummary(k)}</td>
        <td>{resetsIn(k)}</td>
        <td>
          <button type="button" onclick={() => openEdit(k)} disabled={busy}>Limits</button>
          <button type="button" onclick={() => toggle(k)} disabled={busy}>
            {k.active ? 'Disable' : 'Enable'}
          </button>
        </td>
      </tr>
      {#if editing === k.created_at}
        <tr class="limit-form">
          <td colspan="7">
            <form onsubmit={(e) => { e.preventDefault(); saveLimits(k) }}>
              {#each LIMIT_KINDS as kind}
                <label>
                  {kind.label} ({kind.unit})
                  <input
                    type="number" min="0.01" step="0.01" placeholder="unlimited"
                    bind:value={draft[kind.id]} disabled={busy} />
                </label>
              {/each}
              <span class="hint">empty = unlimited</span>
              <button class="primary" type="submit" disabled={busy}>Save</button>
              <button type="button" onclick={closeEdit} disabled={busy}>Cancel</button>
            </form>
          </td>
        </tr>
      {/if}
    {/each}
  </tbody>
</table>

<style>
  tr.over td { color: #b00020; }
  tr.limit-form td { background: rgba(127, 127, 127, 0.08); }
  tr.limit-form form { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  tr.limit-form label { display: flex; align-items: center; gap: 0.4rem; }
  tr.limit-form input { width: 8rem; }
  tr.limit-form .hint { opacity: 0.6; font-size: 0.85em; }
</style>
