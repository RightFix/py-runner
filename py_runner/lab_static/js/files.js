/* Notebook file browser + load/save. Callbacks injected by app.js (no cycles). */
import { api } from './api.js';

export async function refreshFiles(ul, currentPath, onOpen) {
  var list = await api('GET', '/files');
  ul.innerHTML = '';
  (list.files || []).forEach(function (f) {
    var li = document.createElement('li');
    li.textContent = f;
    if (f === currentPath) li.className = 'active';
    li.onclick = function () { onOpen(f); };
    ul.appendChild(li);
  });
}

export function fetchNotebook(path) {
  return api('GET', '/file?path=' + encodeURIComponent(path));
}

export function saveNotebook(path, nb) {
  var clean = JSON.parse(JSON.stringify(nb, function (k, v) {
    return k.charAt(0) === '_' ? undefined : v;
  }));
  return api('PUT', '/file?path=' + encodeURIComponent(path), clean);
}
