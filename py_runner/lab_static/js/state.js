/* Shared notebook state + DOM helpers. Leaf module: imports nothing. */
export var state = { path: null, nb: null, session: null, running: -1, dirty: false };

export function $(id) { return document.getElementById(id); }

export function cellSource(cell) {
  return Array.isArray(cell.source) ? cell.source.join('') : String(cell.source || '');
}
