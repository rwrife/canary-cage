// Pure helpers for reading canary-cage state and locating markers in
// file contents. Kept dependency-free so they can be unit-tested with
// plain Node without VS Code's runtime.

const fs = require("fs");
const path = require("path");

const STATE_DIR = ".canary-cage";
const STATE_FILE = "state.json";

/**
 * Load canary state for a given workspace root. Returns `null` when the
 * state file is missing or unreadable so the caller can degrade quietly.
 *
 * @param {string} root absolute path to the cage root (workspace folder)
 * @returns {{ canaries: Array<{id: string, type: string, path: string, marker: string, planted_at?: string}> } | null}
 */
function loadState(root) {
  const p = path.join(root, STATE_DIR, STATE_FILE);
  let raw;
  try {
    raw = fs.readFileSync(p, "utf8");
  } catch (_err) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.canaries)) return { canaries: [] };
    return parsed;
  } catch (_err) {
    return null;
  }
}

/**
 * Locate every line in `text` that contains `marker`. Returns 0-indexed
 * line numbers so the result lines up with VS Code's Position API.
 *
 * Empty markers never match — they would otherwise paint every line.
 *
 * @param {string} text
 * @param {string} marker
 * @returns {number[]}
 */
function findMarkerLines(text, marker) {
  if (!marker || typeof text !== "string") return [];
  const hits = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes(marker)) hits.push(i);
  }
  return hits;
}

/**
 * Given a workspace root, an absolute file path, and the parsed cage
 * state, return the canaries planted in that file.
 *
 * @param {string} root
 * @param {string} absFilePath
 * @param {{ canaries: Array<any> }} state
 */
function canariesForFile(root, absFilePath, state) {
  if (!state || !Array.isArray(state.canaries)) return [];
  const rel = path.relative(root, absFilePath).split(path.sep).join("/");
  return state.canaries.filter((c) => c.path === rel);
}

module.exports = {
  STATE_DIR,
  STATE_FILE,
  loadState,
  findMarkerLines,
  canariesForFile,
};
