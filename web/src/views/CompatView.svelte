<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet } from '../lib/api'

  let stats = $state<Record<string, unknown> | null>(null)
  let error = $state('')
  onMount(async () => {
    try { stats = await apiGet('/api/openai-compat/stats') }
    catch (e) { error = String(e) }
  })
</script>

{#if error}<p class="err">{error}</p>{/if}
{#if stats}<pre>{JSON.stringify(stats, null, 2)}</pre>{/if}
