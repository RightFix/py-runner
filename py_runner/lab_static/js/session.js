/* Kernel session management + status pill. */
import { api } from './api.js';
import { state, $ } from './state.js';

export async function ensureSession() {
  var h = await api('GET', '/health');
  if (!state.session) {
    var s = await api('POST', '/sessions', {});
    state.session = s.session_id;
  }
  var pill = $('kernel-pill');
  pill.textContent = '● Python · ' + (h.prefix || h.executable || 'unknown env');
  pill.className = 'ok';
}

export async function interruptSession() {
  if (!state.session) return;
  try { await api('POST', '/interrupt', { session_id: state.session }); } catch (e) { /* ignore */ }
}
