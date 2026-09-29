/* Cell DOM rendering. Code cells use CodeMirror (bundled vendor/cm.js);
 * markdown cells keep a plain textarea. Pure w.r.t. app state. */
import { md } from './markdown.js';
import { renderOutput } from './outputs.js';
import { cellSource } from './state.js';
import { createPythonEditor } from './vendor/cm.js';

/* ctx: { cells, running, onRun(i), onInterrupt(), onDelete(i), onToggleMd(i), onEdit(i, text) } */
export function renderCells(host, ctx) {
  host.innerHTML = '';
  ctx.cells.forEach(function (cell, i) {
    var el = document.createElement('div');
    el.className = 'cell';
    var running = ctx.running === i;
    var bar = '<div class="cell-bar"><span class="prompt">' +
      (cell.cell_type === 'code'
        ? 'In [' + (running ? '*' : (cell.execution_count == null ? ' ' : cell.execution_count)) + ']:'
        : cell.cell_type) +
      '</span><span class="spacer"></span>' +
      (cell.cell_type === 'code'
        ? '<button data-run="' + i + '">' + (running ? '⏹' : '▶') + '</button>'
        : '<button data-md="' + i + '">Edit/Preview</button>') +
      '<button data-del="' + i + '">Delete</button></div>';
    el.innerHTML = bar;
    if (cell.cell_type === 'markdown' && !cell._editing) {
      var prev = document.createElement('div');
      prev.className = 'md-preview';
      prev.innerHTML = md(cellSource(cell));
      el.appendChild(prev);
    } else if (cell.cell_type === 'code') {
      var cmHost = document.createElement('div');
      cmHost.className = 'cm-host';
      el.appendChild(cmHost);
      (function (idx) {
        createPythonEditor(cmHost, {
          value: cellSource(cell),
          onChange: function (text) { ctx.onEdit(idx, text); },
          onRun: function () { ctx.onRun(idx); },
          lsp: ctx.lspFor ? ctx.lspFor(idx) : null,
        });
      })(i);
    } else {
      var ta = document.createElement('textarea');
      ta.value = cellSource(cell);
      ta.oninput = function () {
        ctx.onEdit(i, ta.value);
        ta.style.height = 'auto';
        ta.style.height = ta.scrollHeight + 'px';
      };
      el.appendChild(ta);
      requestAnimationFrame(function () { ta.style.height = ta.scrollHeight + 'px'; });
    }
    if (cell.cell_type === 'code' && cell.outputs && cell.outputs.length) {
      var out = document.createElement('div');
      out.className = 'outputs';
      out.innerHTML = cell.outputs.map(renderOutput).join('');
      el.appendChild(out);
    }
    host.appendChild(el);
  });
  host.querySelectorAll('[data-run]').forEach(function (b) {
    b.onclick = function () {
      var i = Number(b.getAttribute('data-run'));
      if (ctx.running === i) ctx.onInterrupt();
      else ctx.onRun(i);
    };
  });
  host.querySelectorAll('[data-md]').forEach(function (b) {
    b.onclick = function () { ctx.onToggleMd(Number(b.getAttribute('data-md'))); };
  });
  host.querySelectorAll('[data-del]').forEach(function (b) {
    b.onclick = function () { ctx.onDelete(Number(b.getAttribute('data-del'))); };
  });
}
