/* App orchestrator: toolbar, run/save flows, boot. Imports all modules. */
import { api } from './api.js';
import { toOutputs } from './outputs.js';
import { state, $, cellSource } from './state.js';
import { ensureSession, interruptSession } from './session.js';
import { refreshFiles, fetchNotebook, saveNotebook } from './files.js';
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
  if (ul) refreshFiles(ul, state.path, openNotebook).catch(function () { /* ignore */ });
}

function setToolbar(on) {
  ['run-all', 'stop', 'add-code', 'add-md', 'save'].forEach(function (id) {
    $(id).disabled = !on;
  });
  if (!on) $('save').textContent = 'Save';
}

async function openNotebook(path) {
  if (state.dirty && !confirm('Discard unsaved changes?')) return;
  var nb = await fetchNotebook(path);
  state.path = path;
  state.nb = nb;
  state.session = null;
  state.dirty = false;
  $('filename').textContent = path.split('/').pop();
  setToolbar(true);
  draw();
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
  state.path = name;
  state.nb = { cells: [], metadata: {}, nbformat: 4, nbformat_minor: 5 };
  state.session = null;
  $('filename').textContent = name;
  setToolbar(true);
  await save();
  draw();
};

draw();
refreshFiles($('file-list'), null, openNotebook).catch(function () {
  $('kernel-pill').textContent = '● backend unreachable';
});
