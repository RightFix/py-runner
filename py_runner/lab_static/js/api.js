/* HTTP + escaping helpers. */
export async function api(method, path, body) {
  var res = await fetch('/api' + path, {
    method: method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  var json = null;
  try { json = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) throw new Error((json && json.error) || ('HTTP ' + res.status));
  return json;
}

export function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
