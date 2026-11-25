// Static pre-scan of a workflow script (REQ-003): runs before the vendored runtime starts, so a
// script that would read the clock, draw a random number, load a module or compile code is refused
// before its first agent, with the line, wherever the construct sits. Strings and comments are not
// code and are skipped; the code inside a template's `${...}` is scanned.
import vm from "node:vm";
import { validateScript } from "../vendor/pi-subagents/runtime.js";

export class Refusal extends Error {
  constructor(what, line) {
    super(`refused: ${what} at line ${line}`);
    this.line = line;
  }
}

const BANNED = [
  ["Date.now", /(?<![\w$.])Date\s*\.\s*now\b/g],
  ["Math.random", /(?<![\w$.])Math\s*\.\s*random\b/g],
  ["argless new Date", /(?<![\w$.])new\s+Date\b(?!\s*\(\s*[^)\s])/g],
  ["require", /(?<![\w$.])require\b/g],
  ["import", /(?<![\w$.])import\b/g],
  ["eval", /(?<![\w$.])eval\b/g],
];
const META_DECLARATION = /(^|[\r\n])[ \t]*export[ \t\r\n]+const[ \t\r\n]+meta[ \t\r\n]*=/;

const lineAt = (text, index) => text.slice(0, index).split("\n").length;
const blank = (c) => (c === "\n" ? "\n" : " ");

const KEYWORD_END = /(?:^|[^\w$.])(?:return|typeof|case|in|of|void|delete|throw|new|yield|await|else|do)$/;

/** Whether a `/` at `i` opens a regex literal: it follows an operator, opener or keyword, never a value. */
function regexAt(out, i) {
  let j = out.length - 1;
  while (j >= 0 && /\s/.test(out[j])) j--;
  return j < 0 || !/[\w$)\]]/.test(out[j]) || KEYWORD_END.test(out.slice(Math.max(0, j - 7), j + 1).join(""));
}

/**
 * The source with every string, template text, regex and comment blanked; offsets and newlines kept.
 * A closed literal ends in `0`, so what follows it reads as following a value (`"1" / x` divides).
 */
export function codeOnly(source) {
  const out = [];
  const templates = [];
  let mode = "code";
  let depth = 0;
  for (let i = 0; i < source.length; i++) {
    const c = source[i];
    const next = source[i + 1];
    if (mode === "line") {
      if (c === "\n") mode = "code";
      out.push(blank(c));
    } else if (mode === "block") {
      if (c === "*" && next === "/") { out.push("  "); i++; mode = "code"; } else out.push(blank(c));
    } else if (mode === "'" || mode === '"' || mode === "/") {
      if (c === "\\") { out.push(" ", blank(next ?? "")); i++; continue; }
      if (c === mode) { mode = "code"; out.push("0"); continue; }
      if (c === "\n" && mode !== '"') mode = "code";
      out.push(blank(c));
    } else if (mode === "`") {
      if (c === "\\") { out.push(" ", blank(next ?? "")); i++; continue; }
      if (c === "`") { mode = "code"; out.push("0"); } else if (c === "$" && next === "{") {
        templates.push(depth++);
        mode = "code";
        out.push("  ");
        i++;
      } else out.push(blank(c));
    } else if (c === "/" && next === "/") { mode = "line"; out.push("  "); i++; }
    else if (c === "/" && next === "*") { mode = "block"; out.push("  "); i++; }
    else if (c === "'" || c === '"' || c === "`") { mode = c; out.push(" "); }
    else if (c === "/" && regexAt(out, i)) { mode = "/"; out.push(" "); }
    else if (c === "{") { depth++; out.push(c); }
    else if (c === "}" && templates.length > 0 && templates[templates.length - 1] === depth - 1) {
      templates.pop();
      depth--;
      mode = "`";
      out.push(" ");
    } else {
      if (c === "}") depth--;
      out.push(c);
    }
  }
  return out.join("");
}

/** Index just past the `meta` object literal, or -1. */
export function metaEnd(source) {
  const code = codeOnly(source);
  const declaration = META_DECLARATION.exec(code);
  if (!declaration) return -1;
  const open = code.indexOf("{", declaration.index + declaration[0].length);
  for (let i = open, depth = 0; open !== -1 && i < code.length; i++) {
    if (code[i] === "{") depth++;
    else if (code[i] === "}" && --depth === 0) return i + 1;
  }
  return -1;
}

function syntaxLine(error) {
  const match = /workflow\.js:(\d+)/.exec(String(error?.stack ?? ""));
  return match ? Number(match[1]) : 1;
}

/** Throws a {@link Refusal} for a script the runtime must not start; returns nothing otherwise. */
export function prescan(source) {
  const code = codeOnly(source);
  let first = null;
  for (const [what, pattern] of BANNED) {
    pattern.lastIndex = 0;
    const match = pattern.exec(code);
    if (match && (first === null || match.index < first.index)) first = { what, index: match.index };
  }
  if (first) throw new Refusal(first.what, lineAt(code, first.index));
  let body;
  try {
    ({ body } = validateScript(source));
  } catch (error) {
    throw new Refusal(`an invalid script (${error.message.split("\n")[0]})`, 1);
  }
  try {
    new vm.Script(`(async () => {\n${body}\n})`, { filename: "workflow.js", lineOffset: -1 });
  } catch (error) {
    const refusal = new Refusal("a syntax error", syntaxLine(error));
    refusal.message += `: ${error.message}`;
    throw refusal;
  }
}
