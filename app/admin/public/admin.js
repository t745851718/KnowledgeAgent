'use strict';

const API = '/api/v1/admin';
const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || `请求失败 (${response.status})`);
  return payload.data;
}

function text(value) { return String(value ?? ''); }
function empty(container) { container.append($('#empty').content.cloneNode(true)); }
function metric(label, value, unit = '') {
  const node = document.createElement('div'); node.className = 'metric';
  const caption = document.createElement('span'); caption.textContent = label;
  const number = document.createElement('strong'); number.textContent = `${value}${unit}`;
  node.append(caption, number); return node;
}

function renderSummary(summary) {
  const target = $('#summary'); target.replaceChildren(
    metric('处理中文档', summary.active_ingestions),
    metric('已记录入库', summary.completed_ingestions),
    metric('RAG 请求', summary.rag_requests),
    metric('生成 Tokens', summary.total_generated_tokens),
    metric('平均生成速度', summary.average_tokens_per_second, ' t/s'),
  );
}

function renderIngestions(items) {
  const target = $('#ingestions'); target.replaceChildren(); if (!items.length) return empty(target);
  items.forEach((item) => {
    const node = document.createElement('details'); const summary = document.createElement('summary');
    const name = document.createElement('strong'); name.textContent = item.name;
    const status = document.createElement('span'); status.textContent = `${item.status} · ${item.total_ms} ms`;
    summary.append(name, status); const detail = document.createElement('div'); detail.className = 'detail';
    const stages = document.createElement('div'); stages.className = 'stages';
    Object.entries(item.stages_ms || {}).forEach(([stage, duration]) => { const pill=document.createElement('span'); pill.className='pill'; pill.textContent=`${stage} ${duration} ms`; stages.append(pill); });
    detail.append(stages); if (item.error) { const error=document.createElement('p'); error.textContent=item.error; detail.append(error); }
    node.append(summary, detail); target.append(node);
  });
}

function renderRag(items) {
  const target = $('#rag-runs'); target.replaceChildren(); if (!items.length) return empty(target);
  items.forEach((item) => {
    const node = document.createElement('details'); const summary = document.createElement('summary');
    const name = document.createElement('strong'); name.textContent = `${item.model} · ${item.request_id}`;
    const status = document.createElement('span'); status.textContent = `${item.usage?.completion_tokens || 0} tokens · ${item.tokens_per_second} t/s`;
    summary.append(name, status); const detail = document.createElement('div'); detail.className = 'detail';
    const promptTitle=document.createElement('h3'); promptTitle.textContent='输入提示词'; const prompt=document.createElement('pre'); prompt.textContent=JSON.stringify(item.prompt, null, 2);
    const outputTitle=document.createElement('h3'); outputTitle.textContent='模型输出'; const output=document.createElement('pre'); output.textContent=text(item.output);
    detail.append(promptTitle,prompt,outputTitle,output); node.append(summary,detail); target.append(node);
  });
}

async function load() {
  try { const data=await api('/metrics'); renderSummary(data.summary); renderIngestions(data.ingestions); renderRag(data.rag_runs); }
  catch (error) { $('#summary').replaceChildren(metric('加载失败', error.message)); }
}

async function loadConfig() {
  const config=await api('/config'); const form=$('#config-form');
  Object.entries(config).forEach(([name,value]) => { if (form.elements[name]) form.elements[name].value=value; });
}

$('#config-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const form=event.currentTarget;
  const body={ chat_model:form.elements.chat_model.value.trim() };
  for (const name of ['retrieval_limit','rerank_limit']) body[name]=Number(form.elements[name].value);
  try { await api('/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); $('#save-status').textContent='已应用（进程重启后恢复环境值）'; }
  catch(error){ $('#save-status').textContent=error.message; }
});
$('#refresh').addEventListener('click', load);
Promise.all([load(), loadConfig()]);
