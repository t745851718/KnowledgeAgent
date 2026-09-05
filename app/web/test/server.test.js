'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createApp, mapInternalPath } = require('../server');

test('maps public API paths to their internal counterparts', () => {
  assert.equal(mapInternalPath('POST', '/documents'), '/documents/ingestions');
  assert.equal(mapInternalPath('POST', '/chat/completions'), '/rag/completions');
  assert.equal(mapInternalPath('GET', '/documents/doc_1'), '/documents/doc_1');
});

test('health endpoint follows the v1 response envelope', async (t) => {
  const server = createApp().listen(0);
  t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  const { port } = server.address();
  const response = await fetch(`http://127.0.0.1:${port}/api/v1/health`);
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.code, 'OK');
  assert.equal(payload.data.service, 'knowledgeagent-web');
});

test('proxies conversation requests and wraps upstream failures', async (t) => {
  const calls = [];
  const app = createApp({ serverBaseUrl:'http://core.test', fetchImpl: async (url, init) => {
    calls.push({ url:String(url), method:init.method });
    return new Response(JSON.stringify({ items:[] }), { status:200, headers:{'content-type':'application/json'} });
  }});
  const server = app.listen(0); t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/api/v1/conversations?limit=20`);
  const payload = await response.json();
  assert.deepEqual(calls[0], { url:'http://core.test/internal/v1/conversations?limit=20', method:'GET' });
  assert.deepEqual(payload.data, { items:[] });
});

test('injects the trusted owner into JSON requests', async (t) => {
  let forwarded;
  const app = createApp({ serverBaseUrl:'http://core.test', ownerId:'user_test', fetchImpl: async (_url, init) => {
    forwarded = JSON.parse(init.body);
    return new Response('event: done\ndata: {}\n\n', { status:200, headers:{'content-type':'text/event-stream'} });
  }});
  const server = app.listen(0); t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  await fetch(`http://127.0.0.1:${server.address().port}/api/v1/chat/completions`, { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({ message:'hi' }) });
  assert.equal(forwarded.owner_id, 'user_test');
});

test('filters internal fields from public document responses', async (t) => {
  const app = createApp({ serverBaseUrl:'http://core.test', fetchImpl: async () => new Response(JSON.stringify({
    items:[{
      id:'doc_1', owner_id:'user_test', name:'guide.md',
      storage_path:'/private/uploads/doc_1/source.md',
      volume_path:'documents/user_test/doc_1/source.md',
      sha256:'internal-hash', idempotency_key:'upload-key',
    }],
    next_cursor:null, has_more:false,
  }), { status:200, headers:{'content-type':'application/json'} }) });
  const server = app.listen(0); t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/api/v1/documents`);
  const payload = await response.json();
  assert.deepEqual(payload.data.items, [{ id:'doc_1', name:'guide.md' }]);
});
