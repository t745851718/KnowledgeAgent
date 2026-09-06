'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createApp, mapInternalPath } = require('../server');

test('maps public API paths to their internal counterparts', () => {
  assert.equal(mapInternalPath('POST', '/documents'), '/documents/ingestions');
  assert.equal(mapInternalPath('POST', '/chat/completions'), '/rag/completions');
  assert.equal(mapInternalPath('GET', '/documents/doc_1'), '/documents/doc_1');
});

test('loads the shared root dotenv file for the web process', () => {
  const script = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
  assert.match(script, /require\('dotenv'\)\.config/);
  assert.match(script, /path\.resolve\(__dirname, '\.\.\/\.\.', '\.env'\)/);
  assert.match(script, /override: false/);
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

test('serves the separate admin monitoring console', async (t) => {
  const server = createApp().listen(0);
  t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/admin/`);
  const page = await response.text();
  assert.equal(response.status, 200);
  assert.match(page, /运行监控台/);
  assert.match(page, /id="config-form"/);
});

test('serves local KaTeX assets and enables safe math rendering', async (t) => {
  const server = createApp().listen(0);
  t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const [pageResponse, scriptResponse] = await Promise.all([
    fetch(`${origin}/`),
    fetch(`${origin}/vendor/katex/katex.min.js`),
  ]);
  const [page, script] = await Promise.all([pageResponse.text(), scriptResponse.text()]);

  assert.equal(pageResponse.status, 200);
  assert.equal(scriptResponse.status, 200);
  assert.match(page, /vendor\/katex\/katex\.min\.css/);
  assert.match(page, /vendor\/katex\/contrib\/auto-render\.min\.js/);
  assert.match(script, /KaTeX/);

  const appScript = fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8');
  assert.match(appScript, /renderMathInElement\(container/);
  assert.match(appScript, /throwOnError: false/);
  assert.match(appScript, /trust: false/);
  assert.match(appScript, /MATHPLACEHOLDER/);
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

test('web search remains a per-request frontend option', () => {
  const script = fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
  assert.match(page, /id="web-search"/);
  assert.match(script, /web_search_enabled: els\.webSearch\.checked/);
});

test('applies generated conversation summaries from the SSE start event', () => {
  const script = fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8');
  assert.match(script, /event === 'start' && data\.conversation_title/);
  assert.match(script, /applyConversationTitle\(state\.conversationId, data\.conversation_title\)/);
  assert.match(script, /JSON\.stringify\(\{ title: '新对话' \}\)/);
});

test('markdown folders upload their relative image assets', () => {
  const script = fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
  assert.match(page, /id="folder-input"[^>]*webkitdirectory/);
  assert.match(script, /form\.append\('asset_paths'/);
  assert.match(script, /file\.webkitRelativePath/);
  assert.match(script, /doc\.missing_images/);
});

test('filters internal fields from public document responses', async (t) => {
  const app = createApp({ serverBaseUrl:'http://core.test', fetchImpl: async () => new Response(JSON.stringify({
    items:[{
      id:'doc_1', owner_id:'user_test', name:'guide.md',
      storage_path:'/private/uploads/doc_1/source.md',
      volume_path:'documents/user_test/doc_1/source.md',
      sha256:'internal-hash', idempotency_key:'upload-key',
      image_keys:{0:['secret-key']}, asset_keys:{'a.png':'secret-asset'}, markdown_path:'folder/guide.md',
    }],
    next_cursor:null, has_more:false,
  }), { status:200, headers:{'content-type':'application/json'} }) });
  const server = app.listen(0); t.after(() => server.close());
  await new Promise((resolve) => server.once('listening', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/api/v1/documents`);
  const payload = await response.json();
  assert.deepEqual(payload.data.items, [{ id:'doc_1', name:'guide.md' }]);
});

test('renders only clickable web sources and not retrieved source chunks', () => {
  const script = fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
  const styles = fs.readFileSync(path.join(__dirname, '../public/styles.css'), 'utf8');

  assert.doesNotMatch(script, /renderCitations|querySelector\('\.citations'\)/);
  assert.doesNotMatch(page, /核对原文引用/);
  assert.match(script, /citation-anchor/);
  assert.match(script, /inline-citation-images/);
  assert.match(script, /citation\?\.images/);
  assert.match(script, /citation\?\.source_type !== 'web'/);
  assert.match(script, /noopener noreferrer/);
  assert.match(styles, /\.web-citations/);
});
