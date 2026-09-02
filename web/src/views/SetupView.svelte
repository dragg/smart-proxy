<script lang="ts">
  import statuslineRaw from '../lib/statusline.sh?raw'

  // Local setup page: how to route the Claude Code CLI through this proxy.
  // The sp-* key is spliced into the snippets purely client-side — it is never
  // sent to or stored by the server. The base URL is taken from the host this
  // dashboard is served from, so the snippet is correct on localhost and prod.
  const base = location.origin
  let proxyKey = $state('')
  const keyShown = $derived(proxyKey.trim() || 'sp-YOUR_KEY')

  const fnScript = $derived(
`# ~/.zshrc (or ~/.bashrc) — every 'claude' now routes through the proxy
claude() {
  ANTHROPIC_BASE_URL="${base}" \\
  ANTHROPIC_API_KEY="${keyShown}" \\
  CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \\
  CLAUDE_STREAM_IDLE_TIMEOUT_MS=1200000 \\
  API_FORCE_IDLE_TIMEOUT=0 \\
  API_TIMEOUT_MS=1800000 \\
  command claude "$@"
}`)

  const oneOff = $derived(
`ANTHROPIC_BASE_URL="${base}" \\
ANTHROPIC_API_KEY="${keyShown}" \\
CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \\
CLAUDE_STREAM_IDLE_TIMEOUT_MS=1200000 \\
API_FORCE_IDLE_TIMEOUT=0 \\
API_TIMEOUT_MS=1800000 \\
command claude`)

  // Status line: the user's ~/.claude/statusline.sh, imported raw so its backslashes
  // and regex escapes survive; the proxy host and the sp-* key are swapped in from
  // this dashboard's origin and the key entered above (client-side only, as above).
  const statuslineScript = $derived(
    statuslineRaw.replaceAll('__PROXY_ORIGIN__', base).replaceAll('__PROXY_KEY__', keyShown))
  const statuslineSettings =
`"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline.sh",
  "refreshInterval": 10
}`

  let copied = $state('')
  async function copy(which: string, text: string) {
    try {
      await navigator.clipboard.writeText(text)
      copied = which
      setTimeout(() => { if (copied === which) copied = '' }, 1500)
    } catch { copied = '' }
  }
</script>

<h2>Route Claude Code through this proxy</h2>
<p class="muted">
  Point the Claude Code CLI at this proxy instead of <code>api.anthropic.com</code>.
  Requests will authenticate with your <code>sp-*</code> key and be served from the
  proxy's rotating pool of Anthropic keys.
</p>

<label class="field">
  <span>Your <code>sp-*</code> proxy key</span>
  <input type="text" spellcheck="false" placeholder="sp-…" bind:value={proxyKey} />
</label>
<p class="muted small">
  Nothing is sent or stored — the key is spliced into the snippet in your browser only.
  No key yet? Create one in the <strong>Keys</strong> tab.
</p>

<h3>1. Persistent — add to <code>~/.zshrc</code> or <code>~/.bashrc</code></h3>
<div class="codeblock">
  <button class="copy" onclick={() => copy('fn', fnScript)}>{copied === 'fn' ? 'Copied' : 'Copy'}</button>
  <pre>{fnScript}</pre>
</div>
<p class="muted small">
  Then reload your shell — <code>source ~/.zshrc</code> (or open a new terminal) — and run <code>claude</code>.
</p>

<h3>2. One-off — single run, this shell only</h3>
<div class="codeblock">
  <button class="copy" onclick={() => copy('oneoff', oneOff)}>{copied === 'oneoff' ? 'Copied' : 'Copy'}</button>
  <pre>{oneOff}</pre>
</div>

<h3>Why each variable</h3>
<table class="vars">
  <tbody>
    <tr>
      <td><code>ANTHROPIC_BASE_URL</code></td>
      <td>Send API requests to this proxy instead of <code>api.anthropic.com</code>.</td>
    </tr>
    <tr>
      <td><code>ANTHROPIC_API_KEY</code></td>
      <td>Your <code>sp-*</code> proxy key. The proxy authenticates you with it, then forwards
        upstream using its own pool of rotating OAuth keys.</td>
    </tr>
    <tr>
      <td><code>CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY</code></td>
      <td><code>1</code> makes Claude Code ask the proxy for its full model list
        (via <code>/v1/models</code>, which the proxy forwards upstream) instead of assuming a
        fixed default set. Surfaces every model the pool can actually serve — needed when a model
        like <strong>Fable</strong> otherwise doesn't show up locally at all.</td>
    </tr>
    <tr>
      <td><code>CLAUDE_STREAM_IDLE_TIMEOUT_MS</code></td>
      <td>Raises Claude Code's stream watchdog from 5 to 20 min. On heavy Opus requests the model
        can go &gt;5 min sending only keep-alive pings (no content); the default watchdog aborts
        that mid-stream with “Response stalled / Stream idle timeout”. Minimum 300000.</td>
    </tr>
    <tr>
      <td><code>API_FORCE_IDLE_TIMEOUT</code></td>
      <td><code>0</code> disables a second, provider-level 5-min idle abort that Claude Code turns
        on for non-direct base URLs like this proxy.</td>
    </tr>
    <tr>
      <td><code>API_TIMEOUT_MS</code></td>
      <td>Overall per-request ceiling (30 min). Kept above the idle timeout so it never cuts a long
        request first. Default 600000.</td>
    </tr>
  </tbody>
</table>

<p class="muted small">
  Requests go to <code>{base}</code>. Confirm they land in the <strong>Traffic</strong> tab.
</p>

<h2>Status line (optional)</h2>
<p class="muted">
  A one-line readout under the prompt: 5h / 7d rate limits and per-model weekly
  limits (e.g.&nbsp;Fable), fetched from the proxy — plus context&nbsp;%, model,
  git branch, session cost, and prompt-cache TTL.
</p>

<h3>1. Save the script to <code>~/.claude/statusline.sh</code></h3>
<div class="codeblock">
  <button class="copy" onclick={() => copy('sl', statuslineScript)}>{copied === 'sl' ? 'Copied' : 'Copy'}</button>
  <pre>{statuslineScript}</pre>
</div>
<p class="muted small">Then make it executable: <code>chmod +x ~/.claude/statusline.sh</code></p>

<h3>2. Enable it in <code>~/.claude/settings.json</code></h3>
<div class="codeblock">
  <button class="copy" onclick={() => copy('slcfg', statuslineSettings)}>{copied === 'slcfg' ? 'Copied' : 'Copy'}</button>
  <pre>{statuslineSettings}</pre>
</div>
<p class="muted small">Merge that <code>"statusLine"</code> key into your existing <code>settings.json</code>.</p>

<h3>Optional toggles</h3>
<table class="vars">
  <tbody>
    <tr>
      <td><code>ANTHROPIC_PROXY_USAGE=true</code></td>
      <td>Shows the 5h / 7d rate-limit % (fetched from the proxy's <code>_oauth_usage</code>) and
        switches context/model formatting to proxy mode. Add it to the <code>claude()</code> wrapper
        above to turn it on.</td>
    </tr>
    <tr>
      <td><code>ENABLE_PROMPT_CACHING_1H=1</code></td>
      <td>Makes the <code>cache …</code> readout count a 1-hour TTL instead of 5 minutes — match it
        to your cache setting / auth.</td>
    </tr>
  </tbody>
</table>

<style>
  .field { display: grid; gap: 6px; max-width: 420px; margin: 12px 0 4px; }
  .field span { font-size: 13px; }
  .small { font-size: 12px; }
  code { background: var(--bg); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
  .codeblock { position: relative; margin: 8px 0; max-width: 760px; }
  .codeblock .copy {
    position: absolute; top: 8px; right: 8px; padding: 4px 10px;
    border: 1px solid #d1d5db; background: #fff; border-radius: 6px;
    cursor: pointer; font-size: 12px; color: var(--ink);
  }
  .codeblock pre { margin: 0; padding-right: 72px; }
  table.vars { max-width: 760px; }
  table.vars td { text-align: left; vertical-align: top; }
  table.vars td:first-child { white-space: nowrap; }
  table.vars td:first-child code { color: var(--ink); }
</style>
