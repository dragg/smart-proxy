<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet, login, Unauthorized } from './lib/api'
  import UsageView from './views/UsageView.svelte'
  import WindowsView from './views/WindowsView.svelte'
  import CompatView from './views/CompatView.svelte'
  import KeysView from './views/KeysView.svelte'
  import TrafficView from './views/TrafficView.svelte'
  import AnthropicView from './views/AnthropicView.svelte'
  import SetupView from './views/SetupView.svelte'

  type Tab = 'usage' | 'windows' | 'anthropic' | 'compat' | 'keys' | 'traffic' | 'setup'
  const TABS: Tab[] = ['usage', 'windows', 'anthropic', 'compat', 'keys', 'traffic', 'setup']

  function tabFromHash(): Tab {
    const h = location.hash.replace(/^#/, '')
    return (TABS as string[]).includes(h) ? (h as Tab) : 'usage'
  }

  // One-click auth via ?token=sp-… (or ?key=). Deliberately limited to sp-
  // keys: a URL reaches browser history, the Referer of every asset the page
  // loads, and the server's access log. A proxy key is revocable; the admin
  // secret is not, so it is never accepted from the address bar.
  function tokenFromQuery(): string {
    const p = new URLSearchParams(location.search)
    const q = (p.get('token') || p.get('key') || '').trim()
    return q.startsWith('sp-') ? q : ''
  }
  function stripTokenFromUrl() {
    const url = new URL(location.href)
    url.searchParams.delete('token')
    url.searchParams.delete('key')
    history.replaceState(null, '', url.pathname + url.search + url.hash)
  }

  let checking = $state(true)
  let authed = $state(false)
  let token = $state('')
  let loginError = $state('')
  let adminSecretConfigured = $state<boolean | null>(null)
  let tab = $state<Tab>(tabFromHash())

  function setTab(t: Tab) {
    tab = t
    if (location.hash.slice(1) !== t) location.hash = t
  }

  async function probe() {
    try { await apiGet('/api/keys'); authed = true }
    catch (e) {
      authed = false
      if (e instanceof Unauthorized) adminSecretConfigured = e.adminSecretConfigured
    }
    finally { checking = false }
  }

  async function boot() {
    const qtoken = tokenFromQuery()
    if (qtoken) {
      try {
        await login(qtoken)     // validate + set the session cookie
        stripTokenFromUrl()     // don't leave the token in the address bar / history
        authed = true
        checking = false
        return
      } catch {
        stripTokenFromUrl()     // bad token → fall back to the cookie / login form
      }
    }
    await probe()
  }
  boot()

  onMount(() => {
    const onHash = () => { tab = tabFromHash() }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  })

  async function doLogin(e: Event) {
    e.preventDefault()
    loginError = ''
    try { await login(token.trim()); authed = true; token = '' }
    catch (e) {
      loginError = 'Invalid token'
      if (e instanceof Unauthorized) adminSecretConfigured = e.adminSecretConfigured
    }
  }
</script>

{#if checking}
  <p class="pad">Loading…</p>
{:else if !authed}
  <form class="login" onsubmit={doLogin}>
    <h1>SmartProxy</h1>
    <input type="password" placeholder="admin secret or sp- key" bind:value={token} />
    <button type="submit">Sign in</button>
    {#if loginError}<p class="err">{loginError}</p>{/if}
    {#if adminSecretConfigured === false}
      <p class="warn">
        No dashboard admin secret is configured, so nobody can change anything
        here — an <code>sp-</code> key signs in read-only. Set
        <code>ANTHROPIC_PROXY_DASHBOARD_SECRET</code> and restart the proxy.
      </p>
    {/if}
  </form>
{:else}
  <header>
    <strong>SmartProxy</strong>
    <nav>
      <button class:active={tab === 'usage'} onclick={() => setTab('usage')}>Usage</button>
      <button class:active={tab === 'windows'} onclick={() => setTab('windows')}>Windows</button>
      <button class:active={tab === 'anthropic'} onclick={() => setTab('anthropic')}>Anthropic</button>
      <button class:active={tab === 'compat'} onclick={() => setTab('compat')}>Compat</button>
      <button class:active={tab === 'keys'} onclick={() => setTab('keys')}>Keys</button>
      <button class:active={tab === 'traffic'} onclick={() => setTab('traffic')}>Traffic</button>
      <button class:active={tab === 'setup'} onclick={() => setTab('setup')}>Setup</button>
    </nav>
  </header>
  <main>
    {#if tab === 'usage'}<UsageView />
    {:else if tab === 'windows'}<WindowsView />
    {:else if tab === 'anthropic'}<AnthropicView />
    {:else if tab === 'compat'}<CompatView />
    {:else if tab === 'traffic'}<TrafficView />
    {:else if tab === 'setup'}<SetupView />
    {:else}<KeysView />{/if}
  </main>
{/if}
