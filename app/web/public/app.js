'use strict';

const API = '/api/v1';
const state = { conversations: [], documents: [], conversationId: null, selectedDocuments: new Set(), streaming: false, pollers: new Map() };
const $ = (selector) => document.querySelector(selector);

const els = {
  sidebar: $('#sidebar'), scrim: $('#scrim'), drawer: $('#library-drawer'), conversationList: $('#conversation-list'),
  title: $('#conversation-title'), messages: $('#messages'), empty: $('#empty-state'), form: $('#chat-form'), input: $('#message-input'),
  send: $('#send-button'), documentList: $('#document-list'), documentCount: $('#document-count'), fileInput: $('#file-input'),
  dropZone: $('#drop-zone'), scope: $('#scope-popover'), scopeDocs: $('#scope-documents'), allDocs: $('#all-documents'), scopeLabel: $('#scope-label'),
};

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, options);
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || `请求失败 (${response.status})`);
  return payload.data;
}

async function loadConversations() {
  try {
    const data = await api('/conversations?limit=50');
    state.conversations = data?.items || [];
    renderConversations();
  } catch (error) { renderConversations(); }
}

async function loadDocuments() {
  try {
    const data = await api('/documents?limit=100');
    state.documents = data?.items || [];
    renderDocuments();
  } catch (error) { renderDocuments(error.message); }
}

function renderConversations() {
  els.conversationList.replaceChildren();
  if (!state.conversations.length) return els.conversationList.append(emptyNote('暂无对话'));
  state.conversations.forEach((conversation) => {
    const button = document.createElement('button');
    button.className = `conversation-item${state.conversationId === conversation.id ? ' active' : ''}`;
    button.innerHTML = `<span>◫</span><span></span><span class="delete">×</span>`;
    button.children[1].textContent = conversation.title || '新对话';
    button.addEventListener('click', (event) => {
      if (event.target.classList.contains('delete')) deleteConversation(conversation.id);
      else selectConversation(conversation);
    });
    els.conversationList.append(button);
  });
}

function renderDocuments(error) {
  els.documentList.replaceChildren();
  els.documentCount.textContent = state.documents.length;
  const indexed = state.documents.filter((doc) => doc.status === 'indexed');
  for (const selected of state.selectedDocuments) if (!indexed.some((doc) => doc.id === selected)) state.selectedDocuments.delete(selected);
  if (!state.documents.length) els.documentList.append(emptyNote(error || '还没有上传文档'));
  state.documents.forEach((doc) => {
    const card = document.createElement('div');
    card.className = 'document-card';
    const progress = Math.max(0, Math.min(100, Number(doc.progress) || 0));
    card.innerHTML = `<div class="document-main"><span class="file-icon"></span><div class="document-info"><strong></strong><small></small></div><button class="delete-doc" title="删除文档">×</button></div>${!['indexed','failed'].includes(doc.status) ? `<div class="progress"><span style="width:${progress}%"></span></div>` : ''}`;
    card.querySelector('.file-icon').textContent = extension(doc.name);
    card.querySelector('strong').textContent = doc.name;
    card.querySelector('small').textContent = documentStatus(doc);
    card.querySelector('.delete-doc').addEventListener('click', () => deleteDocument(doc));
    els.documentList.append(card);
  });
  renderScope();
}

function renderScope() {
  els.scopeDocs.replaceChildren();
  const docs = state.documents.filter((doc) => doc.status === 'indexed');
  docs.forEach((doc) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox'; input.value = doc.id; input.checked = state.selectedDocuments.has(doc.id); input.disabled = els.allDocs.checked;
    input.addEventListener('change', () => { input.checked ? state.selectedDocuments.add(doc.id) : state.selectedDocuments.delete(doc.id); updateScopeLabel(); });
    label.append(input, document.createTextNode(` ${doc.name}`));
    els.scopeDocs.append(label);
  });
  updateScopeLabel();
}

function updateScopeLabel() {
  els.scopeLabel.textContent = els.allDocs.checked || !state.selectedDocuments.size ? '全部文档' : `已选 ${state.selectedDocuments.size} 篇`;
}

async function selectConversation(conversation) {
  state.conversationId = conversation.id;
  els.title.textContent = conversation.title || '新对话';
  renderConversations(); closeOverlays();
  try {
    const data = await api(`/conversations/${encodeURIComponent(conversation.id)}/messages?limit=100`);
    renderMessages(data?.items || []);
  } catch (error) { toast(error.message); }
}

async function createConversation() {
  try {
    const conversation = await api('/conversations', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ title: '新对话' }) });
    state.conversations.unshift(conversation); selectConversation(conversation); els.input.focus();
  } catch (error) { toast(error.message); }
}

async function deleteConversation(id) {
  try {
    await api(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
    state.conversations = state.conversations.filter((item) => item.id !== id);
    if (state.conversationId === id) resetChat();
    renderConversations();
  } catch (error) { toast(error.message); }
}

function renderMessages(messages) {
  els.messages.replaceChildren();
  if (!messages.length) return showEmpty(true);
  showEmpty(false);
  messages.filter((item) => item.role !== 'system').forEach((item) => addMessage(item.role, item.content, item.citations));
  scrollChat();
}

function addMessage(role, content = '', citations = []) {
  showEmpty(false);
  const node = document.createElement('article');
  node.className = `message ${role}`;
  node.innerHTML = `<div class="avatar">${role === 'assistant' ? '✦' : '你'}</div><div class="message-body"><div class="message-role">${role === 'assistant' ? 'KnowledgeAgent' : '你'}</div><div class="message-content"></div><div class="retrieved-images"></div></div>`;
  renderMarkdown(node.querySelector('.message-content'), content);
  renderRetrievedImages(node.querySelector('.retrieved-images'), citations);
  els.messages.append(node); scrollChat();
  return node;
}

function renderRetrievedImages(container, citations = []) {
  container.replaceChildren();
  const seen = new Set();
  citations.flatMap((citation) => citation.images || []).forEach((image) => {
    const key = `${image.document_id}/${image.chunk_index}/${image.image_index}`;
    if (seen.has(key)) return;
    seen.add(key);
    const node = document.createElement('img');
    node.src = `${API}/documents/${encodeURIComponent(image.document_id)}/images/${image.chunk_index}/${image.image_index}`;
    node.alt = '检索到的文档图片';
    node.loading = 'lazy';
    container.append(node);
  });
}

// Render the model's Markdown without allowing model output to inject arbitrary HTML.
function renderMarkdown(container, markdown = '') {
  const escaped = String(markdown)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const blocks = escaped.split(/```([\w+-]*)\n?([\s\S]*?)```/g);
  let html = '';
  for (let index = 0; index < blocks.length; index += 3) {
    html += inlineMarkdown(blocks[index]);
    if (blocks[index + 1] !== undefined) {
      html += `<pre><code class="language-${blocks[index + 1] || 'text'}">${blocks[index + 2].trim()}</code></pre>`;
    }
  }
  container.innerHTML = html;
}

function inlineMarkdown(value) {
  let html = value.replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
    .replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.+?)__/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/_([^_\n]+)_/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  html = html.replace(/(?:<li>[^\n]*<\/li>\n?)+/g, (list) => `<ul>${list}</ul>`);
  return html.split(/\n{2,}/).map((paragraph) => {
    if (/^<(h[1-3]|ul|blockquote|pre)/.test(paragraph.trim())) return paragraph;
    return paragraph.trim() ? `<p>${paragraph.replace(/\n/g, '<br>')}</p>` : '';
  }).join('');
}

async function sendMessage(text) {
  if (state.streaming) return;
  if (!state.conversationId) {
    try {
      const created = await api('/conversations', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ title: text.slice(0, 28) }) });
      state.conversationId = created.id; state.conversations.unshift(created); els.title.textContent = created.title; renderConversations();
    } catch (error) { return toast(error.message); }
  }
  addMessage('user', text);
  const assistant = addMessage('assistant'); assistant.classList.add('pending');
  const content = assistant.querySelector('.message-content');
  state.streaming = true; els.send.disabled = true;
  try {
    const response = await fetch(`${API}/chat/completions`, {
      method:'POST', headers:{'Content-Type':'application/json','Accept':'text/event-stream'},
      body: JSON.stringify({ conversation_id: state.conversationId, message: text, document_ids: els.allDocs.checked ? [] : [...state.selectedDocuments], request_id: crypto.randomUUID() }),
    });
    if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.message || `请求失败 (${response.status})`); }
    await consumeSSE(response.body, (event, data) => {
      if (event === 'delta') renderMarkdown(content, `${content.dataset.raw || ''}${data.content || ''}`);
      if (event === 'delta') content.dataset.raw = `${content.dataset.raw || ''}${data.content || ''}`;
      if (event === 'citations') renderRetrievedImages(assistant.querySelector('.retrieved-images'), data.items);
      if (event === 'error') throw new Error(data.message || '回答生成失败');
      scrollChat();
    });
    await loadConversations();
  } catch (error) {
    if (!content.textContent) content.textContent = `抱歉，${error.message}`;
    toast(error.message);
  } finally {
    assistant.classList.remove('pending'); state.streaming = false; updateSendButton();
  }
}

async function consumeSSE(stream, onEvent) {
  const reader = stream.getReader(); const decoder = new TextDecoder(); let buffer = '';
  while (true) {
    const { value, done } = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || '';
    for (const block of blocks) {
      let event = 'message'; const dataLines = [];
      for (const line of block.split(/\r?\n/)) { if (line.startsWith('event:')) event = line.slice(6).trim(); if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart()); }
      if (dataLines.length) onEvent(event, JSON.parse(dataLines.join('\n')));
    }
    if (done) break;
  }
}

async function uploadFiles(files) {
  const allowed = ['pdf','doc','docx','md','markdown'];
  for (const file of files) {
    if (!allowed.includes(extension(file.name).toLowerCase())) { toast(`${file.name}：不支持的文件类型`); continue; }
    const form = new FormData(); form.append('file', file); form.append('metadata', JSON.stringify({ source:'web-upload' }));
    try {
      const data = await api('/documents', { method:'POST', headers:{'Idempotency-Key':crypto.randomUUID()}, body:form });
      state.documents.unshift({ id:data.document_id, name:file.name, size:file.size, status:data.status, stage:'uploading', progress:0 });
      renderDocuments(); pollDocument(data.document_id); toast(`${file.name} 已加入处理队列`);
    } catch (error) { toast(`${file.name}：${error.message}`); }
  }
  els.fileInput.value = '';
}

function pollDocument(id) {
  if (state.pollers.has(id)) return;
  const poll = async () => {
    try {
      const doc = await api(`/documents/${encodeURIComponent(id)}`);
      const index = state.documents.findIndex((item) => item.id === id);
      if (index >= 0) state.documents[index] = doc; else state.documents.unshift(doc);
      renderDocuments();
      if (['indexed','failed'].includes(doc.status)) { clearInterval(state.pollers.get(id)); state.pollers.delete(id); }
    } catch (_) { /* transient polling failures are retried */ }
  };
  state.pollers.set(id, setInterval(poll, 3000)); poll();
}

async function deleteDocument(doc) {
  if (!confirm(`确定删除“${doc.name}”及其向量数据吗？`)) return;
  try { await api(`/documents/${encodeURIComponent(doc.id)}`, { method:'DELETE' }); state.documents = state.documents.filter((item) => item.id !== doc.id); renderDocuments(); }
  catch (error) { toast(error.message); }
}

function extension(name='') { return (name.split('.').pop() || 'FILE').toUpperCase(); }
function documentStatus(doc) {
  if (doc.status === 'indexed') return `${formatBytes(doc.size)} · 已入库${doc.chunk_count ? ` · ${doc.chunk_count} 个片段` : ''}`;
  if (doc.status === 'failed') return doc.error?.message || '处理失败';
  const stages = { queued:'等待处理', uploading:'正在上传', parsing:'正在解析', splitting:'正在切分', embedding:'正在向量化', indexing:'正在入库' };
  return `${stages[doc.stage] || stages[doc.status] || '处理中'} · ${doc.progress || 0}%`;
}
function formatBytes(bytes) { if (!bytes) return '未知大小'; const units=['B','KB','MB','GB']; const i=Math.min(Math.floor(Math.log(bytes)/Math.log(1024)),3); return `${(bytes/1024**i).toFixed(i ? 1 : 0)} ${units[i]}`; }
function emptyNote(text) { const node=document.createElement('div'); node.className='empty-note'; node.textContent=text; return node; }
function showEmpty(show) { els.empty.hidden=!show; els.messages.classList.toggle('active', !show); }
function resetChat() { state.conversationId=null; els.title.textContent='与知识库对话'; els.messages.replaceChildren(); showEmpty(true); }
function scrollChat() { requestAnimationFrame(() => { $('#chat').scrollTop = $('#chat').scrollHeight; }); }
function toast(message) { const node=document.createElement('div'); node.className='toast'; node.textContent=message; $('#toast-region').append(node); setTimeout(() => node.remove(), 3600); }
function updateSendButton() { els.send.disabled = state.streaming || !els.input.value.trim(); }
function closeOverlays() { els.drawer.classList.remove('open'); els.sidebar.classList.remove('open'); els.scrim.classList.remove('open'); els.drawer.setAttribute('aria-hidden','true'); }
function openLibrary() { els.drawer.classList.add('open'); els.scrim.classList.add('open'); els.drawer.setAttribute('aria-hidden','false'); loadDocuments(); }

els.form.addEventListener('submit', (event) => { event.preventDefault(); const text=els.input.value.trim(); if (!text) return; els.input.value=''; els.input.style.height='auto'; updateSendButton(); sendMessage(text); });
els.input.addEventListener('input', () => { els.input.style.height='auto'; els.input.style.height=`${Math.min(els.input.scrollHeight,160)}px`; updateSendButton(); });
els.input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); els.form.requestSubmit(); } });
$('#new-chat').addEventListener('click', createConversation); $('#refresh-conversations').addEventListener('click', loadConversations);
$('#library-button').addEventListener('click', openLibrary); $('#close-library').addEventListener('click', closeOverlays); els.scrim.addEventListener('click', closeOverlays);
$('#menu-button').addEventListener('click', () => { els.sidebar.classList.add('open'); els.scrim.classList.add('open'); });
els.fileInput.addEventListener('change', () => uploadFiles(els.fileInput.files));
for (const name of ['dragenter','dragover']) els.dropZone.addEventListener(name, (e) => { e.preventDefault(); els.dropZone.classList.add('dragging'); });
for (const name of ['dragleave','drop']) els.dropZone.addEventListener(name, (e) => { e.preventDefault(); els.dropZone.classList.remove('dragging'); });
els.dropZone.addEventListener('drop', (event) => uploadFiles(event.dataTransfer.files));
$('#scope-button').addEventListener('click', () => { els.scope.hidden=!els.scope.hidden; });
els.allDocs.addEventListener('change', () => { renderScope(); });
document.addEventListener('click', (event) => { if (!els.scope.hidden && !els.scope.contains(event.target) && !$('#scope-button').contains(event.target)) els.scope.hidden=true; });
document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => { els.input.value=button.dataset.prompt; updateSendButton(); els.input.focus(); }));

async function init() {
  await Promise.all([loadConversations(), loadDocuments()]);
  state.documents.filter((doc) => !['indexed','failed'].includes(doc.status)).forEach((doc) => pollDocument(doc.id));
  try { await api('/health'); $('#service-dot').classList.add('online'); $('#service-label').textContent='运行正常'; }
  catch (_) { $('#service-label').textContent='连接异常'; }
}
init();
