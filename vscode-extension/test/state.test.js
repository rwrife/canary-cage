const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { loadState, findMarkerLines, canariesForFile } = require("../state");

function mktmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "canary-vscode-"));
}

test("loadState returns null when state.json is missing", () => {
  const root = mktmp();
  assert.equal(loadState(root), null);
});

test("loadState parses a valid state file", () => {
  const root = mktmp();
  const dir = path.join(root, ".canary-cage");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "state.json"),
    JSON.stringify({
      schema_version: 1,
      canaries: [
        { id: "c1", type: "markdown", path: "README.md", marker: "<!-- cc:c1 -->" },
      ],
    })
  );
  const state = loadState(root);
  assert.equal(state.canaries.length, 1);
  assert.equal(state.canaries[0].id, "c1");
});

test("loadState returns null on malformed JSON", () => {
  const root = mktmp();
  const dir = path.join(root, ".canary-cage");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "state.json"), "{ not json");
  assert.equal(loadState(root), null);
});

test("findMarkerLines locates every line containing the marker", () => {
  const text = ["hello", "<!-- cc:abc --> trap", "world", "again <!-- cc:abc -->"].join("\n");
  assert.deepEqual(findMarkerLines(text, "<!-- cc:abc -->"), [1, 3]);
});

test("findMarkerLines refuses empty markers", () => {
  assert.deepEqual(findMarkerLines("a\nb\nc", ""), []);
});

test("findMarkerLines handles CRLF newlines", () => {
  const text = "alpha\r\nbeta MARK\r\ngamma";
  assert.deepEqual(findMarkerLines(text, "MARK"), [1]);
});

test("canariesForFile filters by relative path", () => {
  const root = "/tmp/fake-root";
  const state = {
    canaries: [
      { id: "a", type: "markdown", path: "README.md", marker: "x" },
      { id: "b", type: "todo", path: "src/foo.py", marker: "y" },
      { id: "c", type: "docstring", path: "src/foo.py", marker: "z" },
    ],
  };
  const hits = canariesForFile(root, path.join(root, "src", "foo.py"), state);
  assert.deepEqual(hits.map((c) => c.id), ["b", "c"]);
});

test("canariesForFile tolerates missing state", () => {
  assert.deepEqual(canariesForFile("/x", "/x/y", null), []);
  assert.deepEqual(canariesForFile("/x", "/x/y", {}), []);
});
