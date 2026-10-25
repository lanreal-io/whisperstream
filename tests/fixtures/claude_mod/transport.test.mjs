import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const source = await readFile(new URL('../../../src/whisperstream/claude_plugin/hooks/register.js', import.meta.url), 'utf8');
const { register } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function harness({ wrapper = false, surface = 'terminal', asUserSupported = true } = {}) {
  const handlers = {}, packets = [], prompts = [];
  let tick, sid = 'session-a', response = {}, proof = true, duringRequest;
  const $ = {
    plugin: { root: '/plugin', name: 'fixture' },
    env: { get: async () => wrapper ? 'owned-wrapper' : null },
    session: { id: async () => sid, surfaces: async () => [surface],
      version: async () => ({ version: '2.1.287' }), cwd: async () => '/project' },
    fs: { read: async (path) => JSON.stringify(path.endsWith('runtime-target.json')
      ? { data_dir: '/data' } : path.endsWith('plugin.json') ? { name: 'whisperstream', version: '9.9.9' }
      : { port: 45678, token: 'a'.repeat(64) }) },
    clock: { every: (_, callback) => { tick = callback; return { cancel() { tick = null; } }; } },
    http: { fetch: async (url, init) => {
      if (url.endsWith('/verify')) return { ok: proof, text: JSON.stringify({ ok: proof }) };
      packets.push(JSON.parse(init.body));
      if (duringRequest) await duringRequest();
      return { ok: true, text: JSON.stringify({ ok: true, handle: 'opaque', ...response }) };
    } },
    prompt: { submit: async (event) => {
      // An engine without the field rejects the call before it submits.
      if (event.asUser && !asUserSupported) throw new Error('unknown field asUser');
      const result = await emit('prompt.submit', event, 'fixture');
      if (!result?.drop) prompts.push(event);
    } },
  };
  register((name, handler) => { handlers[name] = handler; });
  async function emit(name, event = {}, origin = 'engine') {
    const next = async (e) => e;
    next.origin = { plugin: origin };
    return handlers[name]($, event, next);
  }
  return { packets, prompts, emit, poll: async () => { if (tick) await tick(); },
    setResponse: (value) => { response = value; }, setSession: (value) => { sid = value; },
    setProof: (value) => { proof = value; }, setDuringRequest: (fn) => { duringRequest = fn; } };
}

test('turn completion is network-free; background poll submits the user reply once', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.emit('turn.complete', { answer: 'Answer', turnId: 'turn-a' });
  assert.equal(h.packets.length, 0);
  await h.poll();
  assert.equal(h.packets[0].completion.text, 'Answer');
  h.setResponse({ reply: 'My reply' });
  await h.poll();
  assert.deepEqual(h.prompts, [{ text: 'My reply', asUser: true }]);
  await h.poll();
  assert.equal(h.prompts.length, 1);
  assert.equal(h.packets.at(-1).state, 'busy');
});

test('a user prompt racing a response invalidates the old reply', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.emit('turn.complete', { answer: 'Answer', turnId: 'turn-a' });
  h.setResponse({ reply: 'Stale' });
  h.setDuringRequest(() => h.emit('prompt.submit', { text: 'New prompt' }));
  await h.poll();
  assert.equal(h.prompts.length, 0);
});

test('a session switch racing a response cannot receive its reply', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.emit('turn.complete', { answer: 'Answer', turnId: 'turn-a' });
  h.setResponse({ reply: 'Stale' });
  h.setDuringRequest(() => h.setSession('session-b'));
  await h.poll();
  assert.equal(h.prompts.length, 0);
  h.setDuringRequest(null);
  await h.poll();
  assert.equal(h.packets.at(-1).session_id, 'session-b');
  assert.equal(h.packets.at(-1).completion, null);
});

test('an undelivered completion cannot become the next turn’s reply target', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.emit('turn.complete', { answer: 'Old answer', turnId: 'turn-a' });
  await h.emit('prompt.submit', { text: 'New prompt' });
  await h.emit('turn.start');
  await h.emit('turn.complete', { answer: 'New answer', turnId: 'turn-b' });
  await h.poll();
  assert.equal(h.packets[0].completion.turn_id, 'turn-b');
  assert.equal(h.packets[0].completion.text, 'New answer');
});

test('backend restart retains an unsent completion but replaces the connection identity', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.poll();
  await h.emit('turn.complete', { answer: 'Answer', turnId: 'turn-a' });
  h.setResponse({ reset: true });
  await h.poll();
  h.setResponse({});
  await h.poll();
  assert.equal(h.packets.at(-1).action, 'connect');
  assert.equal(h.packets.at(-1).completion.text, 'Answer');
  assert.notEqual(h.packets[0].instance, h.packets.at(-1).instance);
});

test('proof failure, remote-only and wrapped sessions cannot forward content', async () => {
  for (const h of [harness({ wrapper: true }), harness({ surface: 'remote' }), harness()]) {
    h.setProof(false);
    await h.emit('session.start');
    await h.emit('turn.complete', { answer: 'Answer', turnId: 'turn-a' });
    await h.poll();
    assert.equal(h.packets.length, 0);
  }
});

test('desktop identifies itself; subagent and aborted answers are not forwarded', async () => {
  const h = harness({ surface: 'desktop' });
  await h.emit('session.start');
  await h.emit('turn.complete', { agentId: 'child', answer: 'Child answer', turnId: 'child' });
  await h.poll();
  assert.equal(h.packets[0].surface, 'desktop');
  assert.equal(h.packets[0].state, 'busy');
  await h.emit('turn.complete', { isAborted: true, answer: 'Partial', turnId: 'root' });
  await h.poll();
  assert.equal(h.packets.at(-1).completion, undefined);
  assert.equal(h.packets.at(-1).state, 'idle');
});

test('logout cancels the timer; clear scrubs old content and resumes discovery', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.emit('turn.complete', { answer: 'Old', turnId: 'old' });
  await h.emit('session.end', { reason: 'clear' });
  h.setSession('new-session');
  await h.poll();
  assert.equal(h.packets[0].completion, null);
  await h.emit('session.end', { reason: 'logout' });
  await h.poll();
  assert.equal(h.packets.length, 1);
});

test('connect reports the loaded plugin version once; polls omit it', async () => {
  const h = harness();
  await h.emit('session.start');
  await h.poll();
  assert.equal(h.packets[0].action, 'connect');
  assert.equal(h.packets[0].plugin_version, '9.9.9');
  await h.poll();
  assert.equal(h.packets.at(-1).action, 'poll');
  assert.equal(h.packets.at(-1).plugin_version, undefined);
});

test('an engine without asUser still gets the reply once, framed', async () => {
  const h = harness({ asUserSupported: false });
  await h.emit('session.start');
  await h.emit('turn.complete', { answer: 'Answer', turnId: 'turn-a' });
  await h.poll();
  h.setResponse({ reply: 'My reply' });
  await h.poll();
  assert.deepEqual(h.prompts, [{ text: 'My reply' }]);
});
