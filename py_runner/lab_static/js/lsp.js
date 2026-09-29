/* Minimal LSP client over WebSocket. No dependencies. */
export function LspClient(url) {
  var ws = null;
  var nextId = 1;
  var pending = {};
  var openPromise = null;
  var diagHandlers = [];

  function connect() {
    if (openPromise) return openPromise;
    openPromise = new Promise(function (resolve, reject) {
      try {
        ws = new WebSocket(url);
      } catch (e) { reject(e); return; }
      ws.onopen = function () { resolve(); };
      ws.onerror = function () { reject(new Error('lsp socket failed')); };
      ws.onmessage = function (ev) { onMessage(ev.data); };
      ws.onclose = function () {
        openPromise = null;
        Object.keys(pending).forEach(function (id) {
          pending[id][1](new Error('lsp socket closed'));
          delete pending[id];
        });
      };
    });
    return openPromise;
  }

  function stripFrame(text) {
    var i = text.indexOf('\r\n\r\n');
    if (i >= 0) return text.slice(i + 4);
    i = text.indexOf('\n\n');
    return i >= 0 ? text.slice(i + 2) : text;
  }

  function onMessage(data) {
    var msg;
    try { msg = JSON.parse(typeof data === 'string' ? stripFrame(data) : data); }
    catch (e) { return; }
    if (msg.id !== undefined && pending[msg.id]) {
      var cb = pending[msg.id];
      delete pending[msg.id];
      if (msg.error) cb[1](new Error(msg.error.message || 'lsp error'));
      else cb[0](msg.result);
      return;
    }
    if (msg.method === 'textDocument/publishDiagnostics') {
      diagHandlers.forEach(function (h) {
        try { h(msg.params); } catch (e) { /* ignore */ }
      });
    }
  }

  function send(obj) {
    var body = JSON.stringify(obj);
    ws.send('Content-Length: ' + body.length + '\r\n\r\n' + body);
  }

  async function request(method, params) {
    await connect();
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      pending[id] = [resolve, reject];
      send({ jsonrpc: '2.0', id: id, method: method, params: params });
    });
  }

  function notify(method, params) {
    return connect().then(function () {
      send({ jsonrpc: '2.0', method: method, params: params });
    }).catch(function () { /* server gone */ });
  }

  return {
    initialize: function (rootUri) {
      return request('initialize', {
        processId: null, rootUri: rootUri,
        capabilities: {
          textDocument: {
            completion: { completionItem: { snippetSupport: false } },
            hover: { contentFormat: ['plaintext'] },
            publishDiagnostics: {},
          },
        },
      }).then(function () {
        return notify('initialized', {});
      });
    },
    didOpen: function (uri, text) {
      return notify('textDocument/didOpen', {
        textDocument: { uri: uri, languageId: 'python', version: 1, text: text },
      });
    },
    didChange: function (uri, text, version) {
      return notify('textDocument/didChange', {
        textDocument: { uri: uri, version: version },
        contentChanges: [{ text: text }],
      });
    },
    completion: function (uri, line, character) {
      return request('textDocument/completion', {
        textDocument: { uri: uri },
        position: { line: line, character: character },
      });
    },
    hover: function (uri, line, character) {
      return request('textDocument/hover', {
        textDocument: { uri: uri },
        position: { line: line, character: character },
      });
    },
    onDiagnostics: function (cb) { diagHandlers.push(cb); },
    close: function () { try { ws && ws.close(); } catch (e) { /* ignore */ } },
  };
}
