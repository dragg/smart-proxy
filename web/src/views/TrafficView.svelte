<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet } from '../lib/api'

  // Shapes match build_usage_kind_json / build_sessions_json in
  // src/smart_proxy/usage_dashboard.py exactly. cost/base_cost/cache_cost are
  // always numbers there (default 0.0), but we stay defensive (`?? null`)
  // in case that ever changes; `unknown` is the real "don't trust this
  // total" signal and drives display, same as UsageView.
  type Kind = {
    request_kind: string; proxy_key: string; key_name: string; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number | null; cache_cost: number | null; cost: number | null
    partial: boolean; unknown: boolean
  }
  type SessionKind = {
    request_kind: string; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number | null; cache_cost: number | null; cost: number | null
    partial: boolean; unknown: boolean
  }
  type Session = {
    session_id: string; proxy_key: string; key_name: string
    project: string; title: string
    first_date: string | null; last_date: string | null; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number | null; cache_cost: number | null; cost: number | null
    partial: boolean; unknown: boolean
    kinds: SessionKind[]
  }

  let kinds = $state<Kind[]>([])
  let sessions = $state<Session[]>([])
  let error = $state('')

  let expanded = $state<Set<string>>(new Set())
  const skey = (s: Session) => `${s.session_id}:${s.proxy_key}`
  function toggle(s: Session) {
    const k = skey(s)
    const next = new Set(expanded)
    next.has(k) ? next.delete(k) : next.add(k)
    expanded = next
  }

  const n = (v: number) => v.toLocaleString()
  const tokens = (r: { input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number }) =>
    r.input_tokens + r.output_tokens + r.cache_read_tokens + r.cache_creation_tokens
  const keyLabel = (r: { key_name?: string; proxy_key?: string }) =>
    r.key_name || r.proxy_key?.slice(0, 10) || '—'
  const fmtCost = (v: number | null | undefined, unknown: boolean, partial: boolean) => {
    if (unknown) return v && v > 0 ? `~$${v.toFixed(2)}?` : '?'
    if (v == null) return '—'
    return (partial ? '~$' : '$') + v.toFixed(2)
  }

  // usage_daily buckets are UTC (matching UsageView), so compute the
  // default range in UTC to avoid off-by-one near local midnight.
  const utcYMD = (d: Date) => d.toISOString().slice(0, 10)
  const addDays = (d: Date, days: number) => new Date(d.getTime() + days * 864e5)

  // "Automated" = helper + subagent request kinds (background work);
  // "User" = main (interactive). Anything else (e.g. "unknown") is left
  // out of the split but still shown in the table itself.
  const AUTOMATED_KINDS = new Set(['helper', 'subagent'])
  const kindSummary = $derived.by(() => {
    let automated = 0
    let user = 0
    for (const k of kinds) {
      if (k.unknown || k.cost == null) continue
      if (AUTOMATED_KINDS.has(k.request_kind)) automated += k.cost
      else if (k.request_kind === 'main') user += k.cost
    }
    const total = automated + user
    return { automated, user, total }
  })

  async function load() {
    error = ''
    try {
      const end = utcYMD(new Date())
      const start = utcYMD(addDays(new Date(), -6))
      const [kindsRes, sessionsRes] = await Promise.all([
        apiGet<{ start: string; end: string; kinds: Kind[] }>(
          `/api/usage/kinds?start=${start}&end=${end}`
        ),
        apiGet<{ sessions: Session[] }>('/api/sessions?limit=50'),
      ])
      kinds = kindsRes.kinds
      sessions = sessionsRes.sessions
    } catch (e) {
      error = String(e)
    }
  }

  onMount(load)
</script>

{#if error}<p class="err">{error}</p>{/if}

<h2>By request kind (last 7 days)</h2>
{#if kindSummary.total > 0}
  <p class="muted">
    Automated (helper + subagent): ${kindSummary.automated.toFixed(2)}
    ({((kindSummary.automated / kindSummary.total) * 100).toFixed(0)}%)
    &middot; User (main): ${kindSummary.user.toFixed(2)}
    ({((kindSummary.user / kindSummary.total) * 100).toFixed(0)}%)
  </p>
{/if}
{#if kinds.length === 0 && !error}
  <p class="muted">No usage in this range.</p>
{/if}
{#if kinds.length > 0}
  <table>
    <thead>
      <tr>
        <th>Kind</th><th>Key</th><th>Requests</th><th>Tokens</th><th>Cost</th>
      </tr>
    </thead>
    <tbody>
      {#each kinds as k (`${k.request_kind}:${k.proxy_key}`)}
        <tr>
          <td>{k.request_kind}</td>
          <td>{keyLabel(k)}</td>
          <td>{n(k.requests)}</td>
          <td>{n(tokens(k))}</td>
          <td>{fmtCost(k.cost, k.unknown, k.partial)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<h2>Top sessions by cost</h2>
{#if sessions.length === 0 && !error}
  <p class="muted">No sessions recorded yet.</p>
{/if}
{#if sessions.length > 0}
  <table>
    <thead>
      <tr>
        <th></th><th>Key</th><th>Title</th><th>Session</th>
        <th>First &rarr; Last</th><th>Requests</th><th>Cost</th>
      </tr>
    </thead>
    <tbody>
      {#each sessions as s (`${s.session_id}:${s.proxy_key}`)}
        <tr class="session-row" onclick={() => toggle(s)}>
          <td class="expander">{expanded.has(skey(s)) ? '▾' : '▸'}</td>
          <td>{keyLabel(s)}</td>
          <td class="title-cell">
            {#if s.project}<span class="project">{s.project}</span>{/if}
            {#if s.title}<span class="snippet" title={s.title}>{s.title}</span>{/if}
            {#if !s.project && !s.title}—{/if}
          </td>
          <td>{s.session_id ? s.session_id.slice(0, 8) + '…' : '—'}</td>
          <td>{s.first_date ?? '—'} &rarr; {s.last_date ?? '—'}</td>
          <td>{n(s.requests)}</td>
          <td>{fmtCost(s.cost, s.unknown, s.partial)}</td>
        </tr>
        {#if expanded.has(skey(s))}
          {#each s.kinds as k (`${s.session_id}:${s.proxy_key}:${k.request_kind}`)}
            <tr class="kind-row">
              <td></td><td></td>
              <td class="kind-name">{k.request_kind}</td>
              <td></td><td></td>
              <td>{n(k.requests)}</td>
              <td>{fmtCost(k.cost, k.unknown, k.partial)}</td>
            </tr>
          {/each}
        {/if}
      {/each}
    </tbody>
  </table>
{/if}

<style>
  .session-row { cursor: pointer; }
  .expander { width: 1.2em; color: #888; user-select: none; }
  .title-cell .project { font-weight: 600; }
  .title-cell .snippet { display: block; color: #888; font-size: 0.85em;
    max-width: 32ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kind-row td { color: #666; font-size: 0.9em; }
  .kind-row .kind-name { padding-left: 1.5em; }
</style>
