/* App orchestrator: toolbar, run/save flows, boot. Imports all modules. */
import { api } from './api.js';
import { toOutputs } from './outputs.js';
import { state, $, cellSource } from './state.js';
import { ensureSession, interruptSession } from './session.js';
import { refreshFiles, fetchNotebook, saveNotebook, searchNotebooks, renderSearchResults } from './files.js';
import { LspClient } from './lsp.js';
import { refreshLspDiagnostics } from './vendor/cm.js';

var lspSeq = 0;

function uriFor(cell) {
  if (!cell._lsp) cell._lsp = 'untitled:cell-' + (++lspSeq) + '.py';
  return cell._lsp;
}

/* One LSP connection per open notebook; null when unavailable. */
async function ensureLsp() {
  if (state.lsp !== undefined) return state.lsp;
  state.lsp = null;
  try {
    var st = await api('GET', '/lsp-status');
    if (!st.available) return null;
    var wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/lsp';
    var client = LspClient(wsUrl);
    await client.initialize(st.rootUri);
    client.onDiagnostics(function (p) {
      refreshLspDiagnostics(p.uri, p.diagnostics);
    });
    state.lsp = client;
  } catch (e) {
    state.lsp = null;
  }
  return state.lsp;
}

function lspFor(i) {
  if (!state.lsp || !state.nb || !state.nb.cells[i]) return null;
  return { client: state.lsp, uri: uriFor(state.nb.cells[i]) };
}
import { renderCells } from './cells.js';

function markDirty() {
  state.dirty = true;
  $('save').textContent = 'Save *';
}

function draw() {
  renderCells($('cells'), {
    cells: state.nb ? state.nb.cells : [],
    running: state.running,
    onRun: runCell,
    onInterrupt: interrupt,
    lspFor: lspFor,
    onDelete: function (i) {
      if (!confirm('Delete cell?')) return;
      state.nb.cells.splice(i, 1);
      markDirty();
      draw();
    },
    onToggleMd: function (i) {
      state.nb.cells[i]._editing = !state.nb.cells[i]._editing;
      draw();
    },
    onEdit: function (i, text) {
      state.nb.cells[i].source = [text];
      markDirty();
    },
  });
  var ul = $('file-list');
  if (ul) refreshFiles(ul, state.dir, state.path, openNotebook, onEnterDir).catch(function () { /* ignore */ });
}

function onEnterDir(dir) {
  state.dir = dir;
  draw();
}

function setToolbar(on) {
  ['run-all', 'stop', 'add-code', 'add-md', 'save'].forEach(function (id) {
    $(id).disabled = !on;
  });
  if (!on) $('save').textContent = 'Save';
}

async function openNotebook(path) {
  if (state.dirty && !confirm('Discard unsaved changes?')) return;
  if (state.lsp) { try { state.lsp.close(); } catch (e) { /* ignore */ } }
  var nb = await fetchNotebook(path);
  state.path = path;
  state.nb = nb;
  state.session = null;
  state.lsp = undefined;
  state.dirty = false;
  $('filename').textContent = path.split('/').pop();
  setToolbar(true);
  draw();
  ensureLsp().then(draw, draw);
}

async function runCell(i) {
  var cell = state.nb.cells[i];
  if (!cell || cell.cell_type !== 'code' || state.running !== -1) return;
  try { await ensureSession(); }
  catch (e) { alert('Backend: ' + e.message); return; }
  state.running = i;
  draw();
  try {
    var r = await api('POST', '/execute', { session_id: state.session, code: cellSource(cell) });
    cell.outputs = toOutputs(r);
    cell.execution_count = r.execution_count;
  } catch (e) {
    cell.outputs = [{ output_type: 'error', evalue: String(e), traceback: [String(e)] }];
  }
  state.running = -1;
  markDirty();
  draw();
}

async function interrupt() {
  await interruptSession();
}

async function save() {
  if (!state.path) return;
  await saveNotebook(state.path, state.nb);
  state.dirty = false;
  $('save').textContent = 'Save';
}

$('run-all').onclick = async function () {
  for (var i = 0; i < state.nb.cells.length; i++) {
    if (state.nb.cells[i].cell_type !== 'code') continue;
    if (state.running !== -1) break;
    await runCell(i);
  }
};
$('stop').onclick = interrupt;
$('save').onclick = function () { save().catch(function (e) { alert('Save failed: ' + e.message); }); };
$('add-code').onclick = function () {
  state.nb.cells.push({ cell_type: 'code', source: [''], metadata: {}, outputs: [], execution_count: null });
  markDirty();
  draw();
};
$('add-md').onclick = function () {
  state.nb.cells.push({ cell_type: 'markdown', source: [''], metadata: {}, _editing: true });
  markDirty();
  draw();
};
$('new-nb').onclick = async function () {
  var name = prompt('Notebook name', 'Untitled.ipynb');
  if (!name) return;
  if (!/\.ipynb$/i.test(name)) name += '.ipynb';
  state.path = state.dir ? state.dir + '/' + name : name;
  state.nb = { cells: [], metadata: {}, nbformat: 4, nbformat_minor: 5 };
  state.session = null;
  $('filename').textContent = name;
  setToolbar(true);
  await save();
  draw();
};

draw();
var searchTimer = null;
$('search').addEventListener('input', function (e) {
  clearTimeout(searchTimer);
  var q = e.target.value.trim();
  if (!q) { draw(); return; }
  searchTimer = setTimeout(function () {
    searchNotebooks(q).then(function (r) {
      renderSearchResults($('file-list'), r.results || [], r.truncated, openNotebook);
    }).catch(function () { /* ignore */ });
  }, 250);
});
refreshFiles($('file-list'), state.dir, null, openNotebook, onEnterDir).catch(function () {
  $('kernel-pill').textContent = '● backend unreachable';
});
