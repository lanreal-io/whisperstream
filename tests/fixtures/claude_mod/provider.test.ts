// Copy into a materialized plugin root and run: claude plugin test <that-root>.
import { test, expect, mock } from 'claude-code/testing';

test('shipped mod uses the provider timer and submits an idle reply', async ($, on) => {
  const clock = mock.clock(on);
  const packets: any[] = [];
  const replies: any[] = [];
  on('env.get', async () => ({ value: undefined }));
  on('session.id', async () => ({ value: 'fixture-session' }));
  on('session.surfaces', async () => ({ value: ['terminal'] }));
  on('session.version', async () => ({ value: { version: '2.1.287' } }));
  on('session.cwd', async () => ({ value: '/fixture' }));
  on('fs.read', async ($, e) => ({ value: JSON.stringify(e.path.endsWith('runtime-target.json')
    ? { data_dir: '/fixture' } : { port: 45678, token: 'a'.repeat(64) }) }));
  on('http.fetch', async ($, e) => {
    if (e.url.endsWith('/verify')) return { value: { ok: true, status: 200, text: '{"ok":true}' } };
    const body = JSON.parse(e.init.body);
    packets.push(body);
    return { value: { ok: true, status: 200, text: JSON.stringify({ ok: true, handle: 'opaque',
      reply: body.completion ? null : 'A user reply' }) } };
  });
  on('prompt.submit', async ($, e) => { replies.push(e); return { text: e.text }; });
  on('session.start', async ($, e) => e);
  on('turn.complete', async ($, e) => ({ text: e.answer }));
  await $.session.start({ cwd: '/fixture', surface: 'terminal', isInteractive: true });
  await $.turn.complete({ turnId: 'fixture-turn', answer: 'A response', isAborted: false });
  expect(packets.length).toBe(0);
  await clock.advance(750);
  expect(packets[0].completion.text).toBe('A response');
  await clock.advance(750);
  expect(replies[0].text).toBe('A user reply');
  expect(replies[0].origin.kind).toBe('plugin');
});
