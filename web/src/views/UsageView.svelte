<script lang="ts">
  import { onMount } from 'svelte'
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
  type Usage = { start: string; end: string; total_known_cost: number; groups: Group[] }

  const n = (v: number) => v.toLocaleString()

  // usage_daily buckets are UTC (matching the old /_usage), so compute presets
  // in UTC to avoid off-by-one near local midnight.
  const utcYMD = (d: Date) => d.toISOString().slice(0, 10)
  const addDays = (d: Date, days: number) => new Date(d.getTime() + days * 864e5)
  function utcToday(): Date {
    const t = new Date()
    return new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate()))
  }
  function mondayOf(d: Date): Date {
    return addDays(d, -((d.getUTCDay() + 6) % 7)) // 0 = Monday
  }

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

  let start = $state(utcYMD(addDays(utcToday(), -6)))
  let end = $state(utcYMD(utcToday()))
  let data = $state<Usage | null>(null)
  let error = $state('')
  let canvas: HTMLCanvasElement
  let chart: Chart | undefined

  function applyPreset(p: Preset) {
    const [s, e] = p.range()
    start = s
    end = e
    load()
  }

  async function load() {
    error = ''
    try {
      data = await apiGet<Usage>(`/api/usage?start=${start}&end=${end}`)
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
  }

  onMount(load)
</script>

<div class="presets">
  {#each presets as p}
    <button type="button" onclick={() => applyPreset(p)}>{p.label}</button>
  {/each}
</div>

<form onsubmit={(e) => { e.preventDefault(); load() }}>
  <input type="date" bind:value={start} />
  <input type="date" bind:value={end} />
  <button class="primary" type="submit">Update</button>
</form>

{#if error}<p class="err">{error}</p>{/if}
<div class="chart-wrap"><canvas bind:this={canvas}></canvas></div>

{#if data}
  <p>Total known cost: <strong>${data.total_known_cost.toFixed(2)}</strong></p>
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
