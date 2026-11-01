'use strict';

const http = require('node:http');
const fs = require('node:fs');

const MAX_BODY = 16 * 1024 * 1024;
const UPSTREAM_IDLE_TIMEOUT_MS = 10 * 60 * 1000;
const DEFAULT_MAX_OUTPUT_TOKENS = 16384;

function payloadRejection(body) {
  // The gateway supports client-executed tools. Never ask the host LM Studio
  // process to execute MCP tools or fetch remote resources on an agent's behalf.
  const unsupported = new Set();
  // Report only a fixed vocabulary, never arbitrary payload values or names.
  const knownTypes = new Set(['local_shell', 'shell', 'web_search', 'web_search_preview',
    'tool_search', 'computer', 'computer_use_preview', 'image_generation',
    'code_interpreter', 'file_search', 'mcp', 'apply_patch']);
  function inspectTools(tools) {
    if (tools == null) return;
    if (!Array.isArray(tools)) { unsupported.add('invalid_tool_list'); return; }
    for (const tool of tools) {
      if (tool && ['function', 'custom'].includes(tool.type)) continue;
      if (tool && tool.type === 'namespace' && Array.isArray(tool.tools)) inspectTools(tool.tools);
      else unsupported.add(knownTypes.has(tool?.type) ? tool.type : 'unknown_tool_type');
    }
  }
  function hasRemoteResource(value) {
    if (!value || typeof value !== 'object') return false;
    return Object.entries(value).some(([key, item]) =>
      ((key === 'url' || key === 'image_url' || key === 'file_url') &&
       typeof item === 'string' && !item.startsWith('data:')) || hasRemoteResource(item));
  }
  if (!body || Array.isArray(body) || typeof body !== 'object') return 'invalid_payload';
  inspectTools(body.tools);
  const reasons = [];
  if (unsupported.size) reasons.push(`unsupported_tool_types: ${[...unsupported].sort().join(', ')}`);
  if (hasRemoteResource(body)) reasons.push('remote_resource_reference');
  return reasons.length ? reasons.join('; ') : null;
}

function safePayload(body) { return payloadRejection(body) === null; }

function createGateway({token, port, hostname = 'host.docker.internal', maxOutputTokens = DEFAULT_MAX_OUTPUT_TOKENS}) {
  if (!token || /\s/.test(token) || !Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error('Invalid LM Studio gateway credentials or port.');
  }
  if (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens < 1) {
    throw new Error('Invalid LM Studio output token limit.');
  }
  const server = http.createServer(async (req, res) => {
    const allowed = (req.method === 'GET' && req.url === '/v1/models') ||
      (req.method === 'POST' && req.url === '/v1/responses');
    const reject = (status, message) => {
      res.writeHead(status, {'content-type': 'application/json', 'connection': 'close'});
      res.end(JSON.stringify({error: {message}}));
    };
    if (!allowed) { req.resume(); return reject(403, 'Gateway route denied.'); }
    let body;
    try {
      let size = 0;
      const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > MAX_BODY) return reject(413, 'Request too large.');
        chunks.push(chunk);
      }
      body = Buffer.concat(chunks);
      if (req.method === 'GET' && body.length) return reject(400, 'Unexpected request body.');
      if (req.method === 'POST') {
        const payload = JSON.parse(body.toString('utf8'));
        const reason = payloadRejection(payload);
        if (reason) return reject(403, `Payload rejected (${reason}). Host tools and remote resources are disabled.`);
        const requested = payload.max_output_tokens;
        if (requested != null && (!Number.isSafeInteger(requested) || requested < 1)) {
          return reject(400, 'max_output_tokens must be a positive integer.');
        }
        // Preserve smaller caller budgets; never allow an unbounded request.
        const limit = Math.min(requested ?? maxOutputTokens, maxOutputTokens);
        if (requested !== limit) {
          payload.max_output_tokens = limit;
          body = Buffer.from(JSON.stringify(payload));
        }
      }
    } catch { return reject(400, 'Invalid request.'); }
    const upstream = http.request({hostname, port, method: req.method, path: req.url,
      headers: {'authorization': `Bearer ${token}`, 'content-type': 'application/json',
        'content-length': body.length, 'accept': req.headers.accept || 'application/json'}}, response => {
      if (response.statusCode < 200 || response.statusCode >= 300) {
        response.resume();
        return reject(response.statusCode >= 400 ? response.statusCode : 502,
          'LM Studio rejected the request. Check its server and token configuration.');
      }
      const headers = {'cache-control': 'no-store'};
      if (response.headers['content-type']) headers['content-type'] = response.headers['content-type'];
      res.writeHead(response.statusCode, headers);
      response.on('error', () => res.destroy());
      response.pipe(res);
    });
    upstream.setTimeout(UPSTREAM_IDLE_TIMEOUT_MS, () => upstream.destroy(new Error('Upstream timeout')));
    upstream.on('error', () => {
      if (res.headersSent) res.destroy();
      else reject(502, 'LM Studio is unavailable.');
    });
    res.on('close', () => upstream.destroy());
    upstream.end(body);
  });
  server.on('connect', (_req, socket) => socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'));
  server.on('upgrade', (_req, socket) => socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'));
  return server;
}

if (require.main === module) {
  try {
    const token = fs.readFileSync('/run/secrets/lmstudio_token', 'utf8').trimEnd();
    const port = Number(process.env.SWARMFORGE_LMSTUDIO_PORT || '1234');
    const maxOutputTokens = Number(process.env.SWARMFORGE_LMSTUDIO_MAX_OUTPUT_TOKENS || DEFAULT_MAX_OUTPUT_TOKENS);
    const server = createGateway({token, port, maxOutputTokens});
    server.on('error', () => { console.error('LM Studio gateway failed to start.'); process.exit(1); });
    server.listen(1234, '0.0.0.0', () => console.log('LM Studio gateway listening on internal port 1234.'));
  } catch {
    console.error('LM Studio gateway requires a valid token file, port and output token limit.');
    process.exit(1);
  }
}

module.exports = {createGateway, safePayload, payloadRejection};
