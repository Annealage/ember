/**
 * Renders the agent's reply text as HTML.
 *
 * `renderMarkdown(text)` returns a string for `innerHTML`. It is a small
 * hand-written renderer, not a markdown library: headings, paragraphs, bold,
 * italic, strikethrough, inline code, fenced code, links, lists (nested, with
 * task boxes), tables, block quotes and rules, plus a fixed set of HTML tags
 * the model may write itself.
 *
 * Safety rests on one rule: every character of the model's text reaches the
 * output through `escapeHtml`, and the only markup in the output is literal
 * tags this file writes. An HTML tag the model wrote is re-emitted only when
 * its name is in `ALLOWED_TAGS`, with attributes rebuilt from `ALLOWED_ATTRS`
 * (never copied), and every other tag shows as visible text. Links keep
 * `http`, `https` and `mailto` only. Tags are balanced per block, so a stray
 * `<div>` cannot swallow the rest of the page.
 */

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const VOID_TAGS = new Set(["br", "hr"]);
const ALLOWED_TAGS = new Set([
  "a", "b", "i", "em", "strong", "u", "s", "del", "ins", "sub", "sup", "br", "hr", "code", "kbd",
  "mark", "small", "span", "div", "p", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3",
  "h4", "h5", "h6", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "details",
  "summary",
]);
// Tag -> attributes it may keep; each value is checked by `attrValue`.
const ALLOWED_ATTRS = {
  a: ["href"],
  th: ["colspan", "rowspan", "align"],
  td: ["colspan", "rowspan", "align"],
  ol: ["start"],
  details: ["open"],
};
const BLOCK_HTML_START = /^\s*<(table|div|details|ul|ol|p|h[1-6]|blockquote|pre|hr)(\s|>|\/)/i;
const TAG_RE = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^<>]*?)?)\s*\/?>/g;
const SAFE_URL = /^(https?:\/\/|mailto:)/i;

function attrValue(tag, name, raw) {
  if (name === "href") return SAFE_URL.test(raw) ? raw : null;
  if (name === "colspan" || name === "rowspan" || name === "start") return /^\d{1,3}$/.test(raw) ? raw : null;
  if (name === "align") return /^(left|right|center)$/i.test(raw) ? raw.toLowerCase() : null;
  return raw;
}

function tagAttrs(tag, attrText) {
  const allowed = ALLOWED_ATTRS[tag];
  if (!allowed) return "";
  let out = "";
  for (const name of allowed) {
    const re = new RegExp("(?:^|\\s)" + name + "(?:\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s\"'>]+)))?(?=\\s|$)", "i");
    const m = re.exec(attrText);
    if (!m) continue;
    const raw = m[1] ?? m[2] ?? m[3] ?? "";
    if (name === "open") {
      out += " open";
      continue;
    }
    const value = attrValue(tag, name, raw.trim());
    if (value === null) continue;
    out += " " + name + '="' + escapeHtml(value) + '"';
    if (tag === "a") out += ' target="_blank" rel="noopener noreferrer"';
  }
  return out;
}

// Open-tag bookkeeping for one block of model HTML.
function newTagState() {
  return { stack: [] };
}

function openTag(state, tag, attrText) {
  const html = "<" + tag + tagAttrs(tag, attrText) + ">";
  if (!VOID_TAGS.has(tag)) state.stack.push(tag);
  return html;
}

function closeTag(state, tag) {
  const at = state.stack.lastIndexOf(tag);
  if (at < 0) return "";
  let out = "";
  while (state.stack.length > at) out += "</" + state.stack.pop() + ">";
  return out;
}

function closeAll(state) {
  let out = "";
  while (state.stack.length) out += "</" + state.stack.pop() + ">";
  return out;
}

const LINK_RE = /\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function emphasis(escaped) {
  return escaped
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
}

// Plain text (no code spans, no HTML tags): links, then emphasis.
function plainText(text) {
  let out = "";
  let last = 0;
  let m;
  LINK_RE.lastIndex = 0;
  while ((m = LINK_RE.exec(text))) {
    out += emphasis(escapeHtml(text.slice(last, m.index)));
    const url = m[2] || m[3];
    const label = m[1] ? emphasis(escapeHtml(m[1])) : escapeHtml(url);
    out += '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    last = m.index + m[0].length;
  }
  return out + emphasis(escapeHtml(text.slice(last)));
}

// A run of text with the model's own HTML tags in it.
function withTags(text, state) {
  let out = "";
  let last = 0;
  let m;
  TAG_RE.lastIndex = 0;
  while ((m = TAG_RE.exec(text))) {
    const tag = m[2].toLowerCase();
    if (!ALLOWED_TAGS.has(tag)) continue;
    out += plainText(text.slice(last, m.index));
    out += m[1] ? closeTag(state, tag) : openTag(state, tag, m[3] || "");
    last = m.index + m[0].length;
  }
  return out + plainText(text.slice(last));
}

// Inline markdown for one block. Code spans come out first so nothing inside
// them is read as emphasis or markup.
function inline(text, state) {
  let out = "";
  let last = 0;
  const re = /`([^`\n]+)`/g;
  let m;
  while ((m = re.exec(text))) {
    out += withTags(text.slice(last, m.index), state);
    out += "<code>" + escapeHtml(m[1]) + "</code>";
    last = m.index + m[0].length;
  }
  return out + withTags(text.slice(last), state);
}

function inlineBlock(text) {
  const state = newTagState();
  return inline(text, state) + closeAll(state);
}

const FENCE_RE = /^\s*```[^\n`]*$/;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const QUOTE_RE = /^\s{0,3}>\s?(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

function alignOf(cell) {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "center";
  return right ? "right" : left ? "left" : "";
}

function renderTable(header, sep, rows) {
  const aligns = splitRow(sep).map(alignOf);
  const cell = (tag, text, i) =>
    "<" + tag + (aligns[i] ? ' style="text-align:' + aligns[i] + '"' : "") + ">" + inlineBlock(text) + "</" + tag + ">";
  let html = '<div class="md-table"><table><thead><tr>';
  html += splitRow(header).map((c, i) => cell("th", c, i)).join("");
  html += "</tr></thead><tbody>";
  for (const row of rows) {
    const cells = splitRow(row);
    html += "<tr>" + header_cells(cells, aligns.length, cell) + "</tr>";
  }
  return html + "</tbody></table></div>";
}

function header_cells(cells, width, cell) {
  let out = "";
  for (let i = 0; i < Math.max(width, cells.length); i++) out += cell("td", cells[i] ?? "", i);
  return out;
}

function isBlockStart(lines, i) {
  const line = lines[i];
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    RULE_RE.test(line) ||
    LIST_RE.test(line) ||
    QUOTE_RE.test(line) ||
    BLOCK_HTML_START.test(line) ||
    (line.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1]) && lines[i + 1].includes("-"))
  );
}

function renderList(lines, start) {
  const first = LIST_RE.exec(lines[start]);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    const m = LIST_RE.exec(lines[i]);
    if (m && m[1].length === indent && /\d/.test(m[2]) === ordered) {
      items.push({ head: m[3], rest: [] });
      i++;
      continue;
    }
    if (!items.length) break;
    // Deeper-indented lines (and the blank lines between them) belong to the item.
    const deeper = lines[i].trim() !== "" && /^\s*/.exec(lines[i])[0].length > indent;
    const blankThenDeeper =
      lines[i].trim() === "" &&
      i + 1 < lines.length &&
      lines[i + 1].trim() !== "" &&
      /^\s*/.exec(lines[i + 1])[0].length > indent;
    if (!deeper && !blankThenDeeper) break;
    items[items.length - 1].rest.push(lines[i]);
    i++;
  }
  const tag = ordered ? "ol" : "ul";
  const startAttr = ordered && parseInt(first[2], 10) !== 1 ? ' start="' + parseInt(first[2], 10) + '"' : "";
  let html = "<" + tag + startAttr + ">";
  for (const item of items) {
    let head = item.head;
    let box = "";
    const task = /^\[([ xX])\]\s+(.*)$/.exec(head);
    if (task) {
      box = task[1] === " " ? "\u2610 " : "\u2611 ";
      head = task[2];
    }
    html += "<li>" + box + inlineBlock(head);
    if (item.rest.some((l) => l.trim() !== "")) {
      const pad = Math.min(...item.rest.filter((l) => l.trim() !== "").map((l) => /^\s*/.exec(l)[0].length));
      html += renderBlocks(item.rest.map((l) => l.slice(pad)));
    }
    html += "</li>";
  }
  return { html: html + "</" + tag + ">", next: i };
}

function renderBlocks(lines) {
  let html = "";
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") {
      i++;
      continue;
    }
    if (FENCE_RE.test(line)) {
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++; // closing fence (or the end of a stream still being written)
      html += '<pre class="code">' + escapeHtml(body.join("\n")) + "</pre>";
      continue;
    }
    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = heading[1].length;
      html += "<h" + level + ">" + inlineBlock(heading[2]) + "</h" + level + ">";
      i++;
      continue;
    }
    if (RULE_RE.test(line)) {
      html += "<hr>";
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim() !== "" && lines[j].includes("|")) rows.push(lines[j++]);
      html += renderTable(line, lines[i + 1], rows);
      i = j;
      continue;
    }
    if (QUOTE_RE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) body.push(QUOTE_RE.exec(lines[i++])[1]);
      html += "<blockquote>" + renderBlocks(body) + "</blockquote>";
      continue;
    }
    if (LIST_RE.test(line)) {
      const list = renderList(lines, i);
      html += list.html;
      i = list.next;
      continue;
    }
    if (BLOCK_HTML_START.test(line)) {
      const body = [];
      while (i < lines.length && lines[i].trim() !== "") body.push(lines[i++]);
      const state = newTagState();
      html += inline(body.join("\n"), state) + closeAll(state);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() !== "" && (para.length === 0 || !isBlockStart(lines, i))) para.push(lines[i++]);
    html += "<p>" + inlineBlock(para.join("\n")).replace(/\n/g, "<br>") + "</p>";
  }
  return html;
}

export function renderMarkdown(text) {
  const html = renderBlocks(String(text).replace(/\r\n?/g, "\n").split("\n"));
  // One plain paragraph (a short reply, a streaming first line) needs no wrapper.
  const lone = /^<p>((?:(?!<\/?p>)[\s\S])*)<\/p>$/.exec(html);
  return lone ? lone[1] : html;
}
