<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet } from '../lib/api'

  // Recorded seven_day window history (from oauth_window_log). Grouped by key
  // NAME so the same account across credential rotations forms one continuous
  // timeline, not split by which credential row is currently active.
  type Win = {
    resets_at?: string
    first_seen_at?: string
    last_seen_at?: string
    observations?: number
    max_utilization?: number | null
    // Utilization at the last observation. Within a window utilization only
    // ever rises, so this differs from max_utilization exactly when the
    // counter was wiped — which is the one case worth showing it.
    last_utilization?: number | null
    usage?: { totals?: { cost_usd?: number; cost_partial?: boolean } } | null
  }
  // An undeclared wipe: a weekly counter dropped to zero while its resets_at
  // stayed put. Returned unfiltered by ?kind= — a wipe spans counters, and the
  // 5h re-open that accompanies it is a reset, not a drop.
  type Wipe = {
    window_kind?: string
    observed_at?: string
    from_utilization?: number
    resets_at_claimed?: string
    hours_before_claimed?: number | null
    five_hour_rolled?: number
    five_hour_early_minutes?: number | null
    source?: string
  }
  type HistKey = {
    id: string
    name?: string
    windows?: { seven_day?: Win[]; five_hour?: Win[] }
    wipes?: Wipe[]
  }
  // One merged window (after folding rotation overlaps), ascending by reset.
  type MWin = {
    reset: string
    firstSeenMs: number | null
    lastSeenMs: number | null
    maxPct: number | null
    lastPct: number | null
    cost: number | null
    costPartial: boolean
  }
  type MGroup = { name: string; windows: MWin[] }
  type WGroup = { name: string; wipes: Wipe[] }
  // A window prepared for display: the interval [Starts At, Ends At].
  type Row = {
    key: string
    startsAt: string | null
    endsAt: string | null
    periodMs: number | null
    maxPct: number | null
    lastPct: number | null
    cost: number | null
    costPartial: boolean
  }

  // Starts At / Ends At / Period definition:
  //  - 'resets'   : declared quota boundaries — Starts At = previous reset,
  //                 Ends At = this reset (period ≈ 7d).
  //  - 'observed' : what we actually saw — Starts At = first_seen_at,
  //                 Ends At = last_seen_at (period = observed coverage; a
  //                 short one flags a partially-captured or in-progress window).
  type Mode = 'resets' | 'observed'
  let mode = $state<Mode>('resets')
  // Which rate-limit window to display. Both five_hour and seven_day are
  // recorded; default to seven_day (the one people watch for weekly quota).
  type Kind = 'seven_day' | 'five_hour'
  let kind = $state<Kind>('seven_day')
  const kindLabel = $derived(kind === 'seven_day' ? 'Seven-day' : 'Five-hour')
  let mgroups = $state<MGroup[]>([])
  let wgroups = $state<WGroup[]>([])
  let raw = $state<unknown>(null)
  let histError = $state('')

  const pct = (u: number | null | undefined): number | null =>
    typeof u === 'number' ? Math.round(u <= 1 ? u * 100 : u) : null
  const fmt = (ts?: string | null): string =>
    ts ? ts.replace('T', ' ').slice(0, 16) : '—'
  const msN = (ts?: string | null): number | null => {
    const t = ts ? Date.parse(ts) : NaN
    return Number.isNaN(t) ? null : t
  }
  // Dedup key: reset time rounded to the hour, folding minute-boundary jitter
  // and credential-rotation overlaps of the same window together.
  const hourKey = (ts?: string | null): string => {
    const t = msN(ts)
    return t == null ? String(ts) : String(Math.round(t / 3_600_000))
  }
  function periodStr(m: number | null): string {
    if (m == null || m < 0) return '—'
    const totalH = Math.round(m / 3_600_000)
    if (totalH < 24) return `${totalH}h` // five_hour windows read better in hours
    return `${Math.floor(totalH / 24)}d ${totalH % 24}h`
  }

  function mergeGroups(keys: HistKey[], sel: Kind): MGroup[] {
    const byName = new Map<string, Win[]>()
    for (const k of keys) {
      const wins = k.windows?.[sel] ?? []
      if (!wins.length) continue
      const name = k.name ?? k.id
      byName.set(name, [...(byName.get(name) ?? []), ...wins])
    }
    const out: MGroup[] = []
    for (const [name, wins] of byName) {
      // Bucket windows that reset within the same hour, then COMBINE each
      // bucket: two credentials observing one rotation window merge to the
      // true peak (max utilization), total cost, earliest first-seen and
      // latest last-seen — not just the sample with the most observations.
      const buckets = new Map<string, Win[]>()
      for (const w of wins) {
        const key = hourKey(w.resets_at)
        buckets.set(key, [...(buckets.get(key) ?? []), w])
      }
      const merged: MWin[] = []
      for (const ws of buckets.values()) {
        // Representative reset time = the richest sample (most observations).
        let rep = ws[0]
        for (const w of ws) if ((w.observations ?? 0) > (rep.observations ?? 0)) rep = w
        let maxPct: number | null = null
        let lastPct: number | null = null
        let cost: number | null = null
        let costPartial = false
        let firstSeenMs: number | null = null
        let lastSeenMs: number | null = null
        for (const w of ws) {
          const p = pct(w.max_utilization)
          if (p != null && (maxPct == null || p > maxPct)) maxPct = p
          const c = w.usage?.totals?.cost_usd
          if (c != null) cost = (cost ?? 0) + c
          if (w.usage?.totals?.cost_partial) costPartial = true
          const fs = msN(w.first_seen_at)
          if (fs != null && (firstSeenMs == null || fs < firstSeenMs)) firstSeenMs = fs
          const ls = msN(w.last_seen_at)
          if (ls != null && (lastSeenMs == null || ls > lastSeenMs)) {
            lastSeenMs = ls
            // The freshest credential wins — NOT the max. After a rotation the
            // older credential's final reading is stale, and taking the larger
            // of the two would show a value the account has moved on from.
            lastPct = pct(w.last_utilization)
          }
        }
        merged.push({
          reset: rep.resets_at ?? '',
          firstSeenMs, lastSeenMs, maxPct, lastPct, cost, costPartial,
        })
      }
      merged.sort((a, b) => (msN(a.reset) ?? 0) - (msN(b.reset) ?? 0))
      out.push({ name, windows: merged })
    }
    return out
  }

  function rowsFor(g: MGroup, m: Mode): Row[] {
    const ws = g.windows
    const rows: Row[] = ws.map((w, i) => {
      const startsMs = m === 'resets'
        ? (i > 0 ? msN(ws[i - 1].reset) : null)
        : w.firstSeenMs
      const endsMs = m === 'resets' ? msN(w.reset) : w.lastSeenMs
      const period = startsMs != null && endsMs != null ? endsMs - startsMs : null
      return {
        key: w.reset,
        startsAt: startsMs != null ? new Date(startsMs).toISOString() : null,
        endsAt: endsMs != null ? new Date(endsMs).toISOString() : null,
        periodMs: period,
        maxPct: w.maxPct,
        lastPct: w.lastPct,
        cost: w.cost,
        costPartial: w.costPartial,
      }
    })
    rows.reverse() // newest first
    return rows
  }

  function mergeWipes(keys: HistKey[]): WGroup[] {
    const byName = new Map<string, Wipe[]>()
    for (const k of keys) {
      const ws = k.wipes ?? []
      if (!ws.length) continue
      const name = k.name ?? k.id
      byName.set(name, [...(byName.get(name) ?? []), ...ws])
    }
    return [...byName].map(([name, wipes]) => ({
      name,
      wipes: wipes.sort((a, b) => (msN(b.observed_at) ?? 0) - (msN(a.observed_at) ?? 0)),
    }))
  }

  // A window whose counter was wiped mid-flight still shows its pre-wipe peak,
  // which reads as if the window ran its course. Mark it.
  //
  // Wipes arrive unfiltered by ?kind= on purpose, so the row's own counter has
  // to be matched here: a wipe of some other counter landing inside this row's
  // interval says nothing about this counter, and badging it would assert a
  // drop that never happened.
  //
  // `seven_day` and `limit:weekly_all` are two names for the same weekly
  // counter (wipes are collapsed onto the first). `limit:weekly_scoped:<name>`
  // is a DIFFERENT counter -- a per-scope weekly cap -- so its wipe does not
  // belong on a seven-day row. Those stay in the Undeclared wipes table below,
  // which names the counter in its own column. Five-hour counters are never
  // wiped, so five-hour rows are never badged.
  const isRowCounter = (k?: string): boolean =>
    k === 'seven_day' || k === 'limit:weekly_all'

  function wipesInRow(name: string, r: Row): Wipe[] {
    if (kind !== 'seven_day') return []
    const from = msN(r.startsAt)
    const to = msN(r.endsAt)
    if (from == null || to == null) return []
    const group = wgroups.find((g) => g.name === name)
    return (group?.wipes ?? [])
      .filter((w) => {
        if (!isRowCounter(w.window_kind)) return false
        const at = msN(w.observed_at)
        return at != null && at >= from && at <= to
      })
      // Oldest first. The group list is newest-first for the log table below,
      // but a row reads left to right as its own timeline, so two wipes inside
      // one window have to appear in the order they happened.
      .sort((a, b) => (msN(a.observed_at) ?? 0) - (msN(b.observed_at) ?? 0))
  }

  const groups = $derived(
    mgroups.map((g) => ({ name: g.name, rows: rowsFor(g, mode) }))
  )

  async function load() {
    try {
      const r = await apiGet<{ keys: HistKey[] }>(`/api/oauth/usage/history?kind=${kind}`)
      raw = r
      mgroups = mergeGroups(r.keys ?? [], kind)
      wgroups = mergeWipes(r.keys ?? [])
    } catch (e) { histError = String(e) }
  }

  // Switching the window kind refetches — the API filters by ?kind=.
  function setKind(k: Kind) {
    if (k === kind) return
    kind = k
    mgroups = []
    wgroups = []
    raw = null
    histError = ''
    load()
  }

  onMount(load)
</script>

<h2>{kindLabel} windows (recorded history)</h2>
<div class="toggles">
  <div class="modes">
    <button class:active={kind === 'seven_day'} onclick={() => setKind('seven_day')}>7-day</button>
    <button class:active={kind === 'five_hour'} onclick={() => setKind('five_hour')}>5-hour</button>
  </div>
  <div class="modes">
    <button class:active={mode === 'resets'} onclick={() => (mode = 'resets')}>By resets</button>
    <button class:active={mode === 'observed'} onclick={() => (mode = 'observed')}>By observations</button>
  </div>
</div>
{#if histError}<p class="err">{histError}</p>{/if}
{#if groups.length === 0 && !histError}
  <p class="muted">No recorded {kind} windows yet.</p>
{/if}
{#each groups as g (g.name)}
  <h3>{g.name}</h3>
  <table>
    <thead>
      <tr>
        <th>Starts At</th>
        <th>Ends At</th>
        <th>Period</th>
        <th>Max % → Now %</th>
        <th>Cost</th>
      </tr>
    </thead>
    <tbody>
      {#each g.rows as r (r.key)}
        {@const rowWipes = wipesInRow(g.name, r)}
        <tr>
          <td>{fmt(r.startsAt)}</td>
          <td>{fmt(r.endsAt)}</td>
          <td>{periodStr(r.periodMs)}</td>
          <td>
            {r.maxPct != null ? r.maxPct + '%' : '—'}
            <!-- Max and last differ only when the peak predates the wipe. A
                 counter that was wiped and then climbed back past its old peak
                 has max == last again, so this pair cannot express the dip on
                 its own -- the wipe's own numbers below carry it. -->
            {#if r.lastPct != null && r.maxPct != null && r.lastPct !== r.maxPct}
              <span class="nowpct" title="Where the counter ended this window">
                → {r.lastPct}%
              </span>
            {/if}
            {#each rowWipes as w}
              <span
                class="wiped"
                title="Counter wiped {w.observed_at ? 'on ' + fmt(w.observed_at) : 'mid-window'}, without an announced reset"
              >
                ♻ {w.from_utilization != null ? w.from_utilization + '% → 0%' : 'wiped'}
              </span>
            {/each}
          </td>
          <td>{r.cost != null ? '$' + r.cost.toFixed(2) + (r.costPartial ? '?' : '') : '—'}</td>
        </tr>
      {/each}
    </tbody>
  </table>
  {#each wgroups.filter((w) => w.name === g.name) as wg (wg.name)}
    <details class="wipes" open>
      <summary>Undeclared wipes ({wg.wipes.length})</summary>
      <table>
        <thead>
          <tr>
            <th>Observed At</th>
            <th>Counter</th>
            <th>From</th>
            <th>Claimed Reset</th>
            <th>Early By</th>
            <th>5h Rolled</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {#each wg.wipes as w (w.observed_at + (w.window_kind ?? '') + (w.source ?? ''))}
            <tr>
              <td>{fmt(w.observed_at)}</td>
              <td>{w.window_kind ?? '—'}</td>
              <td>{w.from_utilization != null ? w.from_utilization + '% → 0%' : '—'}</td>
              <td>{fmt(w.resets_at_claimed)}</td>
              <td>{w.hours_before_claimed != null ? w.hours_before_claimed.toFixed(1) + 'h' : '—'}</td>
              <td>
                {#if w.five_hour_rolled}
                  yes{w.five_hour_early_minutes != null
                    ? ` (${Math.round(w.five_hour_early_minutes)}m early)`
                    : ''}
                {:else}no{/if}
              </td>
              <td>{w.source ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </details>
  {/each}
{/each}

{#if raw}
  <details class="raw"><summary>Raw /api/oauth/usage/history ({kind})</summary>
    <pre>{JSON.stringify(raw, null, 2)}</pre>
  </details>
{/if}
