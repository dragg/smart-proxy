<script lang="ts">
  import { onMount, tick } from 'svelte'
  import Chart from 'chart.js/auto'
  import { apiGet } from '../lib/api'

  type Model = {
    provider: string; model: string; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number | null; cache_cost: number | null; cost: number | null
  }
  type Group = {
    label: string; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number; cache_cost: number; known_cost: number; unknown: boolean
    models: Model[]
  }
  type SeriesPoint = {
    hour: string; requests: number; cost: number; unknown: boolean
  }
  type Usage = {
    granularity: 'day' | 'hour'
    start: string; end: string
    total_known_cost: number; groups: Group[]
    covered_from?: string | null
    series?: SeriesPoint[]
  }

  const n = (v: number) => v.toLocaleString()
  const hh = (v: number) => String(v).padStart(2, '0')

  // usage_daily and usage_bucket are both UTC-bucketed, so every preset is
  // computed in UTC. Never `datetime-local` for the hour inputs — that widget
  // is local time and would silently shift the window.
  const utcYMD = (d: Date) => d.toISOString().slice(0, 10)
  const addDays = (d: Date, days: number) => new Date(d.getTime() + days * 864e5)
  function utcToday(): Date {
    const t = new Date()
    return new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate()))
  }
  function mondayOf(d: Date): Date {
    return addDays(d, -((d.getUTCDay() + 6) % 7)) // 0 = Monday
  }
  function utcNowHour(): Date {
    const t = new Date()
    t.setUTCMinutes(0, 0, 0)
    return t
  }
  const hourLabel = (d: Date) => d.toISOString().slice(0, 13) // YYYY-MM-DDTHH

  type Preset = { label: string; range: () => [string, string] }
  const presets: Preset[] = [
    { label: 'Today', range: () => { const t = utcToday(); return [utcYMD(t), utcYMD(t)] } },
    { label: 'Yesterday', range: () => { const y = addDays(utcToday(), -1); return [utcYMD(y), utcYMD(y)] } },
    { label: 'This week', range: () => { const t = utcToday(); return [utcYMD(mondayOf(t)), utcYMD(t)] } },
    { label: 'Last week', range: () => { const m = mondayOf(utcToday()); return [utcYMD(addDays(m, -7)), utcYMD(addDays(m, -1))] } },
    { label: 'This month', range: () => { const t = utcToday(); return [utcYMD(new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), 1))), utcYMD(t)] } },
    { label: 'Last month', range: () => {
        const t = utcToday()
        const first = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() - 1, 1))
        const last = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), 0))
        return [utcYMD(first), utcYMD(last)]
      } },
  ]

  // Both bounds are inclusive buckets, so "last 6h" spans nowHour-5h .. nowHour.
  type HourPreset = { label: string; range: () => [string, string] }
  const hourPresets: HourPreset[] = [
    { label: 'Last 6h', range: () => {
        const now = utcNowHour()
        return [hourLabel(new Date(now.getTime() - 5 * 36e5)), hourLabel(now)]
      } },
    { label: 'Last 24h', range: () => {
        const now = utcNowHour()
        return [hourLabel(new Date(now.getTime() - 23 * 36e5)), hourLabel(now)]
      } },
    { label: 'Last 48h', range: () => {
        const now = utcNowHour()
        return [hourLabel(new Date(now.getTime() - 47 * 36e5)), hourLabel(now)]
      } },
    { label: 'Today by hour', range: () => {
        const now = utcNowHour()
        return [`${utcYMD(now)}T00`, hourLabel(now)]
      } },
  ]

  let mode = $state<'day' | 'hour'>('day')
  let start = $state(utcYMD(addDays(utcToday(), -6)))
  let end = $state(utcYMD(utcToday()))
  let startHour = $state(0)
  let endHour = $state(new Date().getUTCHours())
  let data = $state<Usage | null>(null)
  let error = $state('')
  let canvas: HTMLCanvasElement
  // Lives inside an {#if}, so it must be reactive state AND drawn only
  // after the DOM has caught up -- see the tick() in load().
  let seriesCanvas = $state<HTMLCanvasElement | undefined>(undefined)
  let chart: Chart | undefined
  let seriesChart: Chart | undefined

  const hours = Array.from({ length: 24 }, (_, i) => i)

  function applyPreset(p: Preset) {
    const [s, e] = p.range()
    mode = 'day'
    start = s
    end = e
    load()
  }

  function applyHourPreset(p: HourPreset) {
    const [s, e] = p.range()
    mode = 'hour'
    start = s.slice(0, 10)
    startHour = Number(s.slice(11, 13))
    end = e.slice(0, 10)
    endHour = Number(e.slice(11, 13))
    load()
  }

  async function load() {
    error = ''
    const q = mode === 'hour'
      ? `start=${start}T${hh(startHour)}&end=${end}T${hh(endHour)}`
      : `start=${start}&end=${end}`
    try {
      data = await apiGet<Usage>(`/api/usage?${q}`)
      // The per-hour canvas is conditional; it does not exist until Svelte
      // has flushed this assignment.
      await tick()
      draw()
    } catch (e) {
      error = String(e)
    }
  }

  function draw() {
    if (!data || !canvas) return
    chart?.destroy()
    chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: data.groups.map((g) => g.label),
        datasets: [{ label: 'Cost $', data: data.groups.map((g) => Number(g.known_cost.toFixed(2))) }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
      },
    })

    seriesChart?.destroy()
    seriesChart = undefined
    const series = data.series
    if (series && seriesCanvas) {
      seriesChart = new Chart(seriesCanvas, {
        type: 'bar',
        data: {
          labels: series.map((p) => `${p.hour.slice(5).replace('T', ' ')}h`),
          datasets: [{ label: 'Cost $', data: series.map((p) => Number(p.cost.toFixed(4))) }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
        },
      })
    }
  }

  onMount(load)
</script>

<div class="presets">
  {#each presets as p}
    <button type="button" onclick={() => applyPreset(p)}>{p.label}</button>
  {/each}
</div>
<div class="presets">
  {#each hourPresets as p}
    <button type="button" onclick={() => applyHourPreset(p)}>{p.label}</button>
  {/each}
</div>

<form onsubmit={(e) => { e.preventDefault(); load() }}>
  <label><input type="radio" bind:group={mode} value="day" /> Days</label>
  <label><input type="radio" bind:group={mode} value="hour" /> Hours (UTC)</label>

  <input type="date" bind:value={start} />
  {#if mode === 'hour'}
    <select bind:value={startHour}>
      {#each hours as h}<option value={h}>{hh(h)}:00</option>{/each}
    </select>
  {/if}

  <input type="date" bind:value={end} />
  {#if mode === 'hour'}
    <select bind:value={endHour}>
      {#each hours as h}<option value={h}>{hh(h)}:59</option>{/each}
    </select>
  {/if}

  <button class="primary" type="submit">Update</button>
</form>

{#if error}<p class="err">{error}</p>{/if}

{#if data?.granularity === 'hour' && (data.covered_from == null || data.start < data.covered_from)}
  <p class="warn">
    Hourly data starts at <code>{data.covered_from ?? '—'}</code> UTC; earlier hours are
    only available at day precision.
  </p>
{/if}

<div class="chart-wrap"><canvas bind:this={canvas}></canvas></div>

{#if data?.series}
  <div class="chart-wrap"><canvas bind:this={seriesCanvas}></canvas></div>
{/if}

{#if data}
  <p>
    Total known cost: <strong>${data.total_known_cost.toFixed(2)}</strong>
    <span class="muted">· {data.granularity === 'hour' ? 'hour' : 'day'} precision · {data.start} → {data.end} UTC</span>
  </p>
  <table>
    <thead>
      <tr>
        <th>Key / Provider / Model</th>
        <th>Requests</th>
        <th>Input</th>
        <th>Output</th>
        <th>Cache read</th>
        <th>Cache write</th>
        <th>Web search</th>
        <th>Cost (tokens)</th>
        <th>Cost (cache)</th>
        <th>Cost</th>
      </tr>
    </thead>
    <tbody>
      {#each data.groups as g}
        <tr>
          <td><strong>{g.label}</strong></td>
          <td>{n(g.requests)}</td>
          <td>{n(g.input_tokens)}</td>
          <td>{n(g.output_tokens)}</td>
          <td>{n(g.cache_read_tokens)}</td>
          <td>{n(g.cache_creation_tokens)}</td>
          <td>{n(g.web_search_requests)}</td>
          <td>{g.unknown ? '?' : '$' + g.base_cost.toFixed(2)}</td>
          <td>{g.unknown ? '?' : '$' + g.cache_cost.toFixed(2)}</td>
          <td>{g.unknown ? '?' : '$' + g.known_cost.toFixed(2)}</td>
        </tr>
        {#each g.models as m}
          <tr class="model">
            <td>{m.provider} / {m.model}</td>
            <td>{n(m.requests)}</td>
            <td>{n(m.input_tokens)}</td>
            <td>{n(m.output_tokens)}</td>
            <td>{n(m.cache_read_tokens)}</td>
            <td>{n(m.cache_creation_tokens)}</td>
            <td>{n(m.web_search_requests)}</td>
            <td>{m.base_cost == null ? '?' : '$' + m.base_cost.toFixed(2)}</td>
            <td>{m.cache_cost == null ? '?' : '$' + m.cache_cost.toFixed(2)}</td>
            <td>{m.cost == null ? '?' : '$' + m.cost.toFixed(2)}</td>
          </tr>
        {/each}
      {/each}
    </tbody>
  </table>
{/if}
