// Transcript-class values are RAM-only. Never log prompts, answers or credentials.
let instance = '', sessionId = '', handle = '', sequence = 0, revision = 0;
let busy = true, ended = false, polling = false, timer = null, submitting = null, pluginVersion = '';
let pending = [];
const newInstance = () => Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
const reset = (id) => {
  instance = newInstance(); sessionId = id; handle = ''; sequence = 0;
  pending = []; submitting = null; busy = true; revision++;
};

async function readPluginVersion($) {
  try {
    const text = await $.fs.read(`${$.plugin.root}/.claude-plugin/plugin.json`);
    if (text.length > 4096) return '';
    const manifest = JSON.parse(text);
    return typeof manifest.version === 'string' && manifest.version.length <= 64 ? manifest.version : '';
  } catch {
    return '';
  }
}

async function tick($) {
  if (polling || ended) return;
  polling = true;
  try {
    const id = await $.session.id();
    if (!id || typeof id !== 'string') return;
    if (id !== sessionId) reset(id);
    const surfaces = await $.session.surfaces();
    const surface = surfaces.includes('desktop') ? 'desktop' : surfaces.includes('terminal') ? 'cli' : null;
    if (!surface) return; // No local UI in print/remote-only sessions.
    const version = await $.session.version();
    const targetText = await $.fs.read(`${$.plugin.root}/runtime-target.json`);
    if (targetText.length > 4096) return;
    const target = JSON.parse(targetText);
    if (typeof target.data_dir !== 'string' || !/^(?:[A-Za-z]:[\\/]|\/)/.test(target.data_dir)) return;
    const discoveryText = await $.fs.read(`${target.data_dir}/claude-hud-port.json`);
    if (discoveryText.length > 4096) return;
    const discovery = JSON.parse(discoveryText);
    if (!Number.isInteger(discovery.port) || discovery.port < 1 || discovery.port > 65535 || !/^[a-f0-9]{64}$/.test(discovery.token)) return;
    const url = `http://127.0.0.1:${discovery.port}`;
    const headers = { 'x-whisperstream-hook-token': discovery.token, 'Content-Type': 'application/json' };
    const proof = await $.http.fetch(`${url}/api/agent-hook/verify`, { headers });
    if (!proof.ok || proof.text.length > 4096 || JSON.parse(proof.text).ok !== true) return;
    const epoch = revision, currentInstance = instance, currentId = sessionId;
    const completion = !busy ? pending[0] : null;
    const packet = {
      action: handle ? 'poll' : 'connect', instance, handle, session_id: currentId,
      sequence: ++sequence, revision: epoch, surface, engine_version: version.version,
      state: busy ? 'busy' : 'idle', completion,
    };
    if (!handle) {
      packet.cwd = await $.session.cwd();
      // Lets the app tell a handshake from this plugin build apart from an older cached one.
      if (!pluginVersion) pluginVersion = await readPluginVersion($);
      if (pluginVersion) packet.plugin_version = pluginVersion;
    }
    if (currentInstance !== instance || epoch !== revision || currentId !== await $.session.id()) return;
    // Network work happens only on this timer, never in a turn/Stop handler.
    const response = await $.http.fetch(`${url}/api/agent-transport/claude`, {
      method: 'POST', headers, body: JSON.stringify(packet),
    });
    if (!response.ok || response.text.length > 65536) return;
    const result = JSON.parse(response.text);
    if (currentInstance !== instance || currentId !== await $.session.id()) return;
    if (result.reset) {
      // Rediscovery must retain an unsent completion and the current idle
      // state. It never carries a reply or handle across app restarts.
      instance = newInstance(); handle = ''; sequence = 0; submitting = null; revision++;
      return;
    }
    if (result.ok !== true || typeof result.handle !== 'string') return;
    handle = result.handle;
    if (completion && pending[0] === completion) pending.shift();
    if (typeof result.reply !== 'string' || !result.reply.trim() || result.reply.length > 10000) return;
    if (busy || epoch !== revision || ended) return;
    submitting = { text: result.reply, revision: epoch };
    try {
      try {
        // asUser delivers the reply as the user's own words, without the
        // engine's "plugin sent a message" frame. An engine that predates the
        // field rejects the call before submitting (a hook's drop resolves, it
        // never throws), so resend it framed.
        await $.prompt.submit({ text: result.reply, asUser: true });
      } catch {
        await $.prompt.submit({ text: result.reply });
      }
    } finally {
      submitting = null;
    }
  } catch {
    // Fail open; no stale reply is retried. A new timer can rediscover the app.
  } finally {
    polling = false;
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    // Existing PTY wrappers already own delivery. Don't create a competing route.
    if (!await $.env.get('WHISPERSTREAM_SESSION_TOKEN')) {
      reset(await $.session.id());
      ended = false;
      if (timer) timer.cancel();
      timer = $.clock.every(750, () => tick($));
    }
    return next(e);
  });
  on('prompt.submit', async ($, e, next) => {
    if ((next.origin?.plugin === $.plugin.name || (e.origin?.kind === 'plugin' && e.origin.name === $.plugin.name)) && submitting) {
      if (busy || ended || revision !== submitting.revision) return { drop: 'Session changed before reply delivery.' };
    }
    pending = []; busy = true; revision++;
    return next(e);
  });
  on('turn.start', async ($, e, next) => {
    const id = await $.session.id();
    if (id !== sessionId) reset(id);
    pending = []; busy = true; revision++;
    return next(e);
  });
  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      const id = await $.session.id();
      if (id !== sessionId) reset(id);
      busy = false; revision++;
      if (!e.isAborted && typeof e.answer === 'string' && e.answer.trim()) {
        pending.push({ text: e.answer.slice(0, 200000), turn_id: e.turnId });
        while (pending.length > 4 || pending.reduce((n, item) => n + item.text.length, 0) > 200000) pending.shift();
      }
    }
    return next(e);
  });
  on('session.end', async ($, e, next) => {
    // No old draft/answer survives clear, resume, branch, logout, or process exit.
    reset('');
    ended = !['clear', 'resume'].includes(e.reason);
    if (ended && timer) { timer.cancel(); timer = null; }
    return next(e);
  });
}
