/* CodeMirror python cell editor (bundled at build time into vendor/cm.js).
 * Exposes a minimal factory; optional LSP attach for completion/hover/lint.
 */
import { EditorState, Compartment } from '@codemirror/state';
import { EditorView, keymap, lineNumbers, hoverTooltip } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { python } from '@codemirror/lang-python';
import { autocompletion, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import { linter, lintGutter, forceLinting } from '@codemirror/lint';

var _lspViews = {}; // uri -> [entries] for diagnostic refresh

function toCmPos(doc, line, character) {
  var l = doc.line(Math.min(line + 1, doc.lines));
  return Math.min(l.from + character, l.to);
}

function severityOf(sev) {
  return sev === 1 ? 'error' : sev === 2 ? 'warning' : 'info';
}

/* Push fresh publishDiagnostics into every live view of a uri. */
export function refreshLspDiagnostics(uri, diagnostics) {
  (_lspViews[uri] || []).forEach(function (entry) {
    entry.diags = diagnostics || [];
    if (entry.view) {
      try { forceLinting(entry.view); } catch (e) { /* view gone */ }
    }
  });
}

export function createPythonEditor(parent, opts) {
  opts = opts || {};
  var lsp = opts.lsp || null; // { client, uri }
  var version = 1;
  var language = new Compartment();
  var entry = { view: null, diags: [] };
  if (lsp) {
    (_lspViews[lsp.uri] = _lspViews[lsp.uri] || []).push(entry);
  }

  function lspPos(doc, offset) {
    var line = doc.lineAt(offset);
    return { line: line.number - 1, character: offset - line.from };
  }

  async function completionSource(context) {
    if (!lsp) return null;
    var word = context.matchBefore(/[\w.]+/);
    if (!word && !context.explicit) return null;
    var doc = context.state.doc;
    var pos = lspPos(doc, context.pos);
    var res;
    try {
      await lsp.client.didChange(lsp.uri, doc.toString(), ++version);
      res = await lsp.client.completion(lsp.uri, pos.line, pos.character);
    } catch (e) {
      return null;
    }
    var items = (res && res.items) || res || [];
    if (!Array.isArray(items)) return null;
    return {
      from: word ? word.from : context.pos,
      options: items.slice(0, 100).map(function (it) {
        return {
          label: it.label,
          detail: it.detail || '',
          info: it.documentation
            ? (typeof it.documentation === 'string' ? it.documentation : it.documentation.value || '')
            : undefined,
          apply: it.insertText || it.label,
        };
      }),
    };
  }

  var extensions = [
    lineNumbers(),
    history(),
    closeBrackets(),
    language.of(python()),
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...historyKeymap,
      {
        key: 'Ctrl-Enter',
        run: function () {
          if (opts.onRun) opts.onRun();
          return true;
        },
      },
    ]),
    EditorView.updateListener.of(function (update) {
      if (update.docChanged) {
        var text = view.state.doc.toString();
        if (lsp) {
          lsp.client.didChange(lsp.uri, text, ++version).catch(function () {});
        }
        if (opts.onChange) opts.onChange(text);
      }
    }),
    EditorView.theme({
      '&': { backgroundColor: '#0a0d12', color: '#e6edf3', fontSize: '13px' },
      '.cm-content': { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
      '.cm-gutters': { backgroundColor: '#0a0d12', color: '#8b949e', border: 'none' },
      '.cm-activeLineGutter': { backgroundColor: 'transparent' },
    }),
  ];

  if (lsp) {
    extensions.push(
      autocompletion({ activateOnTyping: true, override: [completionSource] }),
      hoverTooltip(async function (view, pos) {
        var doc = view.state.doc;
        var p = lspPos(doc, pos);
        var res;
        try {
          res = await lsp.client.hover(lsp.uri, p.line, p.character);
        } catch (e) {
          return null;
        }
        var contents = res && res.contents;
        var text = !contents ? null
          : typeof contents === 'string' ? contents
          : Array.isArray(contents) ? contents.map(function (c) {
              return typeof c === 'string' ? c : (c && c.value) || '';
            }).join('\n')
          : contents.value || null;
        if (!text) return null;
        return { pos: pos, end: pos, create: function () {
          var dom = document.createElement('div');
          dom.style.whiteSpace = 'pre-wrap';
          dom.textContent = text;
          return { dom: dom };
        } };
      }),
      linter(function () {
        var doc = view.state.doc;
        return (entry.diags || []).map(function (d) {
          var from = toCmPos(doc, d.range.start.line, d.range.start.character);
          var to = toCmPos(doc, d.range.end.line, d.range.end.character);
          return { from: from, to: to, severity: severityOf(d.severity), message: d.message };
        });
      }),
      lintGutter(),
    );
  } else {
    extensions.push(autocompletion({ activateOnTyping: false }));
  }

  var view = new EditorView({ parent: parent, state: EditorState.create({
    doc: opts.value || '',
    extensions: extensions,
  })});
  entry.view = view;

  if (lsp) {
    lsp.client.didOpen(lsp.uri, view.state.doc.toString()).catch(function () {});
  }

  return {
    view: view,
    getValue: function () { return view.state.doc.toString(); },
    setValue: function (text) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    },
    focus: function () { view.focus(); },
    destroy: function () {
      if (lsp && _lspViews[lsp.uri]) {
        _lspViews[lsp.uri] = _lspViews[lsp.uri].filter(function (e) { return e !== entry; });
      }
      view.destroy();
    },
  };
}
