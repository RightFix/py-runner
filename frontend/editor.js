/* CodeMirror python cell editor (bundled at build time into vendor/cm.js).
 * Exposes a minimal factory; LSP wiring (lint/completion sources) attaches later.
 */
import { EditorState, Compartment } from '@codemirror/state';
import { EditorView, keymap, lineNumbers } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { python } from '@codemirror/lang-python';
import { autocompletion, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';

export function createPythonEditor(parent, opts) {
  opts = opts || {};
  var language = new Compartment();
  var view = new EditorView({
    parent: parent,
    state: EditorState.create({
      doc: opts.value || '',
      extensions: [
        lineNumbers(),
        history(),
        closeBrackets(),
        autocompletion({ activateOnTyping: false }),
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
          if (update.docChanged && opts.onChange) {
            opts.onChange(view.state.doc.toString());
          }
        }),
        EditorView.theme({
          '&': { backgroundColor: '#0a0d12', color: '#e6edf3', fontSize: '13px' },
          '.cm-content': { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
          '.cm-gutters': { backgroundColor: '#0a0d12', color: '#8b949e', border: 'none' },
          '.cm-activeLineGutter': { backgroundColor: 'transparent' },
        }),
      ],
    }),
  });
  return {
    view: view,
    getValue: function () { return view.state.doc.toString(); },
    setValue: function (text) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    },
    focus: function () { view.focus(); },
    destroy: function () { view.destroy(); },
  };
}
