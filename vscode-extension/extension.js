// canary-cage VS Code extension — minimal viable slice.
//
// Reads `.canary-cage/state.json` from each workspace folder and decorates
// any line whose text contains a planted canary's `marker` with a 🐤 gutter
// icon plus a hover bubble describing the canary.
//
// Refresh triggers:
//   * editor becomes active
//   * document is saved
//   * `.canary-cage/state.json` changes on disk
//   * the `canaryCage.refresh` command is invoked

const path = require("path");
const vscode = require("vscode");

const { loadState, findMarkerLines, canariesForFile } = require("./state");

/** @type {vscode.TextEditorDecorationType | undefined} */
let decoration;

/** Cache of root => state object so we don't re-read on every keystroke. */
const stateCache = new Map();

function getState(root) {
  if (stateCache.has(root)) return stateCache.get(root);
  const state = loadState(root) || { canaries: [] };
  stateCache.set(root, state);
  return state;
}

function invalidateCache() {
  stateCache.clear();
}

function workspaceRootFor(uri) {
  const folder = vscode.workspace.getWorkspaceFolder(uri);
  return folder ? folder.uri.fsPath : undefined;
}

function decorateEditor(editor) {
  if (!editor || !decoration) return;
  const root = workspaceRootFor(editor.document.uri);
  if (!root) {
    editor.setDecorations(decoration, []);
    return;
  }
  const state = getState(root);
  const planted = canariesForFile(root, editor.document.uri.fsPath, state);
  if (planted.length === 0) {
    editor.setDecorations(decoration, []);
    return;
  }

  const text = editor.document.getText();
  /** @type {vscode.DecorationOptions[]} */
  const options = [];
  for (const canary of planted) {
    const lines = findMarkerLines(text, canary.marker);
    for (const line of lines) {
      const range = new vscode.Range(line, 0, line, 0);
      const md = new vscode.MarkdownString(
        `🐤 **canary-cage tripwire**\n\n` +
          `- **id:** \`${canary.id}\`\n` +
          `- **type:** \`${canary.type}\`\n` +
          (canary.planted_at ? `- **planted:** ${canary.planted_at}\n` : "") +
          `\nRemove via \`canary uproot\` — don't hand-edit.`
      );
      md.isTrusted = false;
      options.push({ range, hoverMessage: md });
    }
  }
  editor.setDecorations(decoration, options);
}

function decorateAllVisible() {
  for (const editor of vscode.window.visibleTextEditors) {
    decorateEditor(editor);
  }
}

function activate(context) {
  const iconPath = context.asAbsolutePath(path.join("media", "canary.svg"));
  decoration = vscode.window.createTextEditorDecorationType({
    gutterIconPath: vscode.Uri.file(iconPath),
    gutterIconSize: "contain",
    overviewRulerColor: "#f0c419",
    overviewRulerLane: vscode.OverviewRulerLane.Left,
  });
  context.subscriptions.push(decoration);

  // Watch every workspace folder's state file.
  const folders = vscode.workspace.workspaceFolders || [];
  for (const folder of folders) {
    const pattern = new vscode.RelativePattern(folder, ".canary-cage/state.json");
    const watcher = vscode.workspace.createFileSystemWatcher(pattern);
    const refresh = () => {
      invalidateCache();
      decorateAllVisible();
    };
    watcher.onDidChange(refresh);
    watcher.onDidCreate(refresh);
    watcher.onDidDelete(refresh);
    context.subscriptions.push(watcher);
  }

  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor((editor) => decorateEditor(editor)),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      for (const editor of vscode.window.visibleTextEditors) {
        if (editor.document === doc) decorateEditor(editor);
      }
    }),
    vscode.commands.registerCommand("canaryCage.refresh", () => {
      invalidateCache();
      decorateAllVisible();
    })
  );

  decorateAllVisible();
}

function deactivate() {
  stateCache.clear();
}

module.exports = { activate, deactivate };
