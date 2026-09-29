/* Notebook file browser, navigation + recursive search. */
import { api } from './api.js';

/* List one directory: subdirs for navigation, .ipynb files to open. */
export async function refreshFiles(ul, dir, currentPath, onOpen, onEnterDir) {
  var list = await api('GET', '/files?path=' + encodeURIComponent(dir || ''));
  ul.innerHTML = '';
  if (dir) {
    var up = document.createElement('li');
    up.textContent = '⬆ ..';
    up.onclick = function () {
      var parts = dir.split('/');
      parts.pop();
      onEnterDir(parts.join('/'));
    };
    ul.appendChild(up);
  }
  (list.dirs || []).forEach(function (d) {
    var li = document.createElement('li');
    li.textContent = '📁 ' + d.name;
    li.onclick = function () { onEnterDir(d.path); };
    ul.appendChild(li);
  });
  (list.files || []).forEach(function (f) {
    var li = document.createElement('li');
    li.textContent = '📓 ' + f.split('/').pop();
    if (f === currentPath) li.className = 'active';
    li.onclick = function () { onOpen(f); };
    ul.appendChild(li);
  });
  return list.path;
}

/* Recursive filename search across the whole root. */
export function searchNotebooks(q) {
  return api('GET', '/search?q=' + encodeURIComponent(q));
}

export function renderSearchResults(ul, results, truncated, onOpen) {
  ul.innerHTML = '';
  if (!results.length) {
    var li = document.createElement('li');
    li.textContent = 'No matches';
    ul.appendChild(li);
    return;
  }
  results.forEach(function (f) {
    var item = document.createElement('li');
    item.textContent = '📓 ' + f;
    item.onclick = function () { onOpen(f); };
    ul.appendChild(item);
  });
  if (truncated) {
    var more = document.createElement('li');
    more.textContent = '…showing first ' + results.length;
    ul.appendChild(more);
  }
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
