'use strict';

const express = require('express');
const path = require('path');
const crypto = require('crypto');

const DEFAULT_PORT = 3000;

function createApp(options = {}) {
  const app = express();
  const serverBaseUrl = (options.serverBaseUrl || process.env.SERVER_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
  const internalToken = options.internalToken ?? process.env.INTERNAL_API_TOKEN;
  const fetchImpl = options.fetchImpl || global.fetch;
  const ownerId = options.ownerId || process.env.DEVELOPMENT_OWNER_ID || 'development-user';

  app.disable('x-powered-by');
  app.use(express.static(path.join(__dirname, 'public')));
  app.use(express.json({ limit: '1mb' }));

  app.get('/api/v1/health', (_req, res) => {
    res.json({
      code: 'OK',
      message: 'success',
      data: {
        status: 'ok',
        service: 'knowledgeagent-web',
        timestamp: new Date().toISOString(),
      },
    });
  });

  app.all('/api/v1/{*resource}', async (req, res) => {
    const requestId = req.get('x-request-id') || crypto.randomUUID();
    res.set('X-Request-Id', requestId);

    const incomingPath = req.path.slice('/api/v1'.length);
    const targetPath = mapInternalPath(req.method, incomingPath);
    const target = new URL(`${serverBaseUrl}/internal/v1${targetPath}`);
    for (const [key, value] of Object.entries(req.query)) {
      if (Array.isArray(value)) value.forEach((item) => target.searchParams.append(key, String(item)));
      else if (value !== undefined) target.searchParams.set(key, String(value));
    }

    const headers = buildUpstreamHeaders(req.headers, requestId, internalToken, ownerId);
    const init = { method: req.method, headers, redirect: 'manual' };
    if (!['GET', 'HEAD'].includes(req.method)) {
      if (req.is('application/json')) init.body = JSON.stringify({ ...req.body, owner_id: ownerId });
      else {
        init.body = req;
        init.duplex = 'half';
      }
    }

    try {
      const upstream = await fetchImpl(target, init);
      res.status(upstream.status);
      copyResponseHeaders(upstream.headers, res);

      if (!upstream.body || req.method === 'HEAD' || upstream.status === 204) return res.end();
      const contentType = upstream.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        const payload = await upstream.json();
        if (!upstream.ok) return res.json(normalizeError(payload, upstream.status, requestId));
        if (payload && payload.code && Object.hasOwn(payload, 'message')) return res.json(payload);
        return res.json({ code: 'OK', message: successMessage(req.method, incomingPath), data: normalizeData(payload, req.method, incomingPath) });
      }
      for await (const chunk of upstream.body) {
        if (!res.write(chunk)) await new Promise((resolve) => res.once('drain', resolve));
      }
      res.end();
    } catch (error) {
      if (res.headersSent) return res.end();
      res.status(502).json({
        code: 'UPSTREAM_SERVICE_ERROR',
        message: '核心服务暂时不可用，请稍后重试',
        details: process.env.NODE_ENV === 'production' ? undefined : { reason: error.message },
        request_id: requestId,
      });
    }
  });

  app.get('{*path}', (_req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));
  return app;
}

function mapInternalPath(method, pathname) {
  if (method === 'POST' && pathname === '/documents') return '/documents/ingestions';
  if (method === 'POST' && pathname === '/chat/completions') return '/rag/completions';
  return pathname;
}

function buildUpstreamHeaders(source, requestId, internalToken, ownerId) {
  const headers = new Headers();
  const forwarded = ['accept', 'content-type', 'idempotency-key'];
  for (const name of forwarded) if (source[name]) headers.set(name, source[name]);
  headers.set('x-request-id', requestId);
  headers.set('x-owner-id', ownerId);
  if (internalToken) headers.set('authorization', `Bearer ${internalToken}`);
  return headers;
}

function successMessage(method, path) {
  if (method === 'POST' && path === '/documents') return '文档已接收，正在处理';
  if (method === 'POST' && path === '/conversations') return '会话已创建';
  return 'success';
}

function normalizeData(payload, method, path) {
  if (method === 'POST' && path === '/documents' && payload?.document_id && !payload.status_url) {
    return { ...payload, status_url: `/api/v1/documents/${payload.document_id}` };
  }
  return payload;
}

function normalizeError(payload, status, requestId) {
  if (payload?.code && payload?.message) return { ...payload, request_id: payload.request_id || requestId };
  const detail = payload?.detail;
  return {
    code: status >= 500 ? 'UPSTREAM_SERVICE_ERROR' : status === 404 ? 'NOT_FOUND' : 'INVALID_ARGUMENT',
    message: typeof detail === 'string' ? detail : detail?.message || '请求未能完成',
    details: typeof detail === 'object' ? detail : undefined,
    request_id: requestId,
  };
}

function copyResponseHeaders(source, res) {
  for (const name of ['content-type', 'cache-control', 'retry-after']) {
    const value = source.get(name);
    if (value) res.set(name, value);
  }
  if ((source.get('content-type') || '').includes('text/event-stream')) {
    res.set('Cache-Control', 'no-cache, no-transform');
    res.set('Connection', 'keep-alive');
    res.set('X-Accel-Buffering', 'no');
    res.flushHeaders();
  }
}

if (require.main === module) {
  const port = Number(process.env.WEB_PORT) || DEFAULT_PORT;
  createApp().listen(port, () => console.log(`KnowledgeAgent Web: http://localhost:${port}`));
}

module.exports = { createApp, mapInternalPath };
