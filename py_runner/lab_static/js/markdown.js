/* Minimal markdown: headings, code fences, bold/italic/code, lists, links. */
import { esc } from './api.js';

export function md(src) {
  var lines = String(src).split('\n'), html = '', inCode = false, inList = false, code = [];
  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
    return s;
  }
  lines.forEach(function (ln) {
    if (/^```/.test(ln)) {
      if (inCode) { html += '<pre><code>' + esc(code.join('\n')) + '</code></pre>'; code = []; }
      inCode = !inCode;
      return;
    }
    if (inCode) { code.push(ln); return; }
    var h = ln.match(/^(#{1,4})\s+(.*)/);
    if (h) { html += '<h' + h[1].length + '>' + inline(h[2]) + '</h' + h[1].length + '>'; return; }
    var li = ln.match(/^[-*]\s+(.*)/);
    if (li) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(li[1]) + '</li>';
      return;
    }
    if (inList) { html += '</ul>'; inList = false; }
    if (ln.trim()) html += '<p>' + inline(ln) + '</p>';
  });
  if (inList) html += '</ul>';
  if (inCode) html += '<pre><code>' + esc(code.join('\n')) + '</code></pre>';
  return html;
}
