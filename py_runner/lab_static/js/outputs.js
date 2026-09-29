/* nbformat output rendering + server-result mapping. */
import { esc } from './api.js';

export function renderOutput(o) {
  if (o.output_type === 'stream') return '<div class="output"><pre>' + esc(o.text || '') + '</pre></div>';
  if (o.output_type === 'error') {
    return '<div class="output error"><pre>' + esc((o.traceback || [o.evalue || '']).join('\n')) + '</pre></div>';
  }
  var d = o.data || {}, parts = '';
  if (d['image/png']) parts += '<div class="output"><img src="data:image/png;base64,' + d['image/png'] + '" /></div>';
  if (d['text/html']) parts += '<div class="output">' + d['text/html'] + '</div>';
  if (d['text/plain']) parts += '<div class="output"><pre>' + esc(d['text/plain']) + '</pre></div>';
  return parts;
}

/* Simplified py-runner result -> nbformat outputs. */
export function toOutputs(r) {
  var out = [];
  if (r.output) out.push({ output_type: 'stream', name: 'stdout', text: r.output });
  var plain = (r.display || []).filter(function (d) { return d.type === 'text/plain' || d.type === 'text/html'; });
  var imgs = (r.display || []).filter(function (d) { return d.type === 'image/png'; });
  if (plain.length) {
    var data = {};
    plain.forEach(function (d) { data[d.type] = d.data; });
    out.push({ output_type: 'execute_result', data: data, metadata: {}, execution_count: r.execution_count });
  }
  imgs.forEach(function (d) {
    out.push({ output_type: 'display_data', data: { 'image/png': d.data }, metadata: {} });
  });
  if (r.error) {
    var lines = r.error.split('\n');
    out.push({ output_type: 'error', evalue: lines[lines.length - 1], traceback: lines });
  }
  return out;
}
