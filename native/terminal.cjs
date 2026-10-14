#!/usr/bin/env node
'use strict';

// The official CLI owns a PTY; this host only projects its VT screen and the
// read-only Python collector. No official files, authentication or prompts are
// touched by the host.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { spawn } = require('node:child_process');
const readline = require('node:readline');

const CSI = '\x1b[';
const COLOR_NAMES = { red: 203, green: 114, yellow: 220, cyan: 81, magenta: 141, white: 255, blue: 75, dim: 245 };
const DEFAULT_COLORS = { model: 81, good: 114, warn: 220, bad: 203, muted: 245, agent: 141, dim: 238 };
const GRAPHEMES = new Intl.Segmenter(undefined, { granularity: 'grapheme' });

function loadDependencies(env = process.env) {
  let prefix = env.CODEX_HUD_NODE_HOME || env.CODEX_HUD_NATIVE_NODE;
  let configured;
  if (!prefix && env.CODEX_HUD_CONFIG_HOME) {
    try { configured = JSON.parse(fs.readFileSync(path.join(env.CODEX_HUD_CONFIG_HOME, 'settings.json'), 'utf8')).native_node; } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  prefix ||= configured;
  const requireFrom = prefix ? createRequire(path.join(path.resolve(prefix), 'package.json')) : require;
  return { pty: requireFrom('@lydell/node-pty'), Terminal: requireFrom('@xterm/headless').Terminal };
}

function safeText(text) {
  return String(text ?? '').replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '').replace(/[\r\n\t]/g, ' ');
}

function keyId(key) { return Array.isArray(key) && key.length === 2 ? JSON.stringify(key) : null; }
function rootId(snapshot) { return snapshot?.root?.id ?? null; }

function colorSgr(value) {
  if (Number.isInteger(value) && value >= 0 && value <= 255) return `38;5;${value}`;
  if (Object.hasOwn(COLOR_NAMES, value)) return `38;5;${COLOR_NAMES[value]}`;
  if (typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)) {
    return `38;2;${[1, 3, 5].map(i => parseInt(value.slice(i, i + 2), 16)).join(';')}`;
  }
  return '39';
}

// Preserve terminal cell widths when clipping the collector's plain HUD text.
function charWidth(char) {
  const cp = char.codePointAt(0);
  if (/\p{Mark}/u.test(char) || cp === 0x200d || cp === 0xfe0f) return 0;
  return cp >= 0x1100 && (cp <= 0x115f || cp === 0x2329 || cp === 0x232a ||
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) || (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe10 && cp <= 0xfe19) ||
    (cp >= 0xfe30 && cp <= 0xfe6f) || (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) || (cp >= 0x1f300 && cp <= 0x1faff) || cp >= 0x20000) ? 2 : 1;
}

function clip(text, width) {
  let result = '', used = 0;
  for (const char of safeText(text)) {
    const size = charWidth(char);
    if (used + size > width) break;
    result += char;
    used += size;
  }
  return result;
}

function wrapText(text, width) {
  width = Math.max(1, Math.floor(width));
  const lines = [];
  let line = '', used = 0;
  for (const { segment } of GRAPHEMES.segment(safeText(text))) {
    const widths = [...segment].map(charWidth);
    // Keep combined glyphs together (accents, emoji modifiers and ZWJ
    // sequences). Chinese and other East Asian wide characters use two cells.
    const size = /\p{Extended_Pictographic}|\p{Regional_Indicator}/u.test(segment) ? Math.max(2, ...widths) : widths.reduce((sum, value) => sum + value, 0);
    if (line && used + size > width) { lines.push(line); line = ''; used = 0; }
    line += segment;
    used += size;
  }
  lines.push(line);
  return lines;
}

function styledRow(row, width, colors, selected = false) {
  let result = '', used = 0;
  for (const [value, kind] of row.spans || []) {
    const text = clip(value, Math.max(0, width - used));
    if (!text) continue;
    result += `${CSI}0;${selected ? '1;7' : colorSgr(colors[kind])}m${text}`;
    for (const char of text) used += charWidth(char);
  }
  return result + `${CSI}0m`;
}

function cellSgr(cell) {
  const flags = [];
  for (const [method, code] of [['isBold', 1], ['isDim', 2], ['isItalic', 3], ['isUnderline', 4],
    ['isBlink', 5], ['isInverse', 7], ['isInvisible', 8], ['isStrikethrough', 9]]) {
    if (cell[method]?.()) flags.push(code);
  }
  for (const [prefix, fg] of [['Fg', 38], ['Bg', 48]]) {
    if (cell[`is${prefix}RGB`]()) {
      const rgb = cell[`get${prefix}Color`]();
      flags.push(fg, 2, (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255);
    } else if (cell[`is${prefix}Palette`]()) flags.push(fg, 5, cell[`get${prefix}Color`]());
  }
  return `${CSI}0${flags.length ? ';' + flags.join(';') : ''}m`;
}

function terminalLine(line, cols) {
  if (!line) return '';
  let output = '', style = '';
  for (let x = 0; x < cols; x++) {
    const cell = line.getCell(x);
    if (!cell || cell.getWidth() === 0) continue;
    const next = cellSgr(cell);
    if (next !== style) { output += next; style = next; }
    output += cell.getChars() || ' ';
  }
  return output + `${CSI}0m`;
}

function isEmptyComposer(terminal, cursorVisible = true) {
  const buffer = terminal.buffer.active;
  if (!cursorVisible || buffer.cursorX !== 2) return false;
  const line = buffer.getLine(buffer.baseY + buffer.cursorY);
  if (!line || line.translateToString(true) !== '› Ask Codex to do anything') return false;
  // Identical user text is a draft, not the dim placeholder.
  for (let x = 2; x < 25; x++) {
    const cell = line.getCell(x);
    if (!cell || !cell.isDim()) return false;
  }
  return true;
}

function composerFingerprint(screen) {
  const terminal = screen.terminal, buffer = terminal.buffer.active;
  const line = buffer.getLine(buffer.baseY + buffer.cursorY);
  const cells = [];
  for (let x = 0; x < terminal.cols; x++) {
    const cell = line?.getCell(x);
    cells.push(cell ? [cell.getChars(), cell.getWidth(), Boolean(cell.isDim())] : null);
  }
  return JSON.stringify([buffer.type, buffer.cursorX, buffer.cursorY, screen.cursorVisible, cells]);
}

class ComposerGuard {
  constructor(screen) { this.screen = screen; this.pending = null; }
  forwarded(token) {
    // Cursor navigation can be forwarded without a redraw. Wait for a real
    // composer change only after inputs that can edit text or submit/open it.
    const pasteBody = token.name === 'paste' ? token.raw.replace(/^\x1b\[200~/, '').replace(/\x1b\[201~$/, '') : '';
    const changesExisting = !this.screen.emptyComposer() && (token.name === 'enter' || token.raw === '\x7f' || token.raw === '\x08');
    if (pasteBody || changesExisting || (token.name === 'other' && /^[^\x00-\x1f\x7f-\x9f]/u.test(token.raw))) {
      this.pending ??= composerFingerprint(this.screen);
    }
  }
  observe() { if (this.pending !== null && this.pending !== composerFingerprint(this.screen)) this.pending = null; }
  empty() { return this.pending === null && this.screen.emptyComposer(); }
  reset() { this.pending = null; }
  rebase() { if (this.pending !== null) this.pending = composerFingerprint(this.screen); }
}

class VirtualScreen {
  constructor(Terminal, cols, rows, onParsed = () => {}, onReply = () => {}) {
    this.terminal = new Terminal({ cols, rows, allowProposedApi: true, scrollback: 2000 });
    this.cursorVisible = true;
    this.cursorStyle = `${CSI}0 q`;
    this.pendingWrites = 0;
    this.terminal.onData(onReply);
    for (const [final, visible] of [['h', true], ['l', false]]) {
      this.terminal.parser.registerCsiHandler({ prefix: '?', final }, params => {
        if (params.includes(25)) this.cursorVisible = visible;
        return false; // Observe; xterm still applies every official mode.
      });
    }
    this.terminal.parser.registerCsiHandler({ intermediates: ' ', final: 'q' }, params => {
      const value = Number(params[0]) || 0;
      this.cursorStyle = `${CSI}${value} q`;
      return false;
    });
    this.onParsed = onParsed;
  }
  write(data, afterParsed = () => {}) {
    this.pendingWrites++;
    this.terminal.write(data, () => {
      this.pendingWrites--;
      afterParsed();
      // A hide/show sequence may straddle PTY data events. Never paint the
      // intermediate screen in a DEC synchronized-output transaction.
      if (!this.pendingWrites && !this.terminal.modes.synchronizedOutputMode) this.onParsed();
    });
  }
  resize(cols, rows) { this.terminal.resize(cols, rows); }
  lines() {
    const terminal = this.terminal, buffer = terminal.buffer.active;
    return Array.from({ length: terminal.rows }, (_, y) => terminalLine(buffer.getLine(buffer.viewportY + y), terminal.cols));
  }
  emptyComposer() { return !this.pendingWrites && !this.terminal.modes.synchronizedOutputMode && isEmptyComposer(this.terminal, this.cursorVisible); }
  dispose() { this.terminal.dispose(); }
}

// Streaming decoder preserves unknown sequences, editing keys and pasted
// payloads verbatim. Only named HUD bindings are interpreted by the controller.
class InputDecoder {
  constructor() { this.buffer = ''; this.paste = false; }
  push(data, flush = false) {
    this.buffer += data;
    const tokens = [];
    const emit = (raw, name = 'other') => tokens.push({ raw, name });
    while (this.buffer) {
      if (this.paste) {
        const end = this.buffer.indexOf(`${CSI}201~`);
        if (end >= 0) {
          emit(this.buffer.slice(0, end + 6), 'paste');
          this.buffer = this.buffer.slice(end + 6);
          this.paste = false;
          continue;
        }
        // Keep a possible fragmented end marker, forwarding the body now.
        const retained = flush ? 0 : Math.min(5, this.buffer.length);
        if (this.buffer.length > retained) { emit(this.buffer.slice(0, -retained || undefined), 'paste'); this.buffer = retained ? this.buffer.slice(-retained) : ''; }
        break;
      }
      if (this.buffer[0] !== '\x1b') {
        const char = [...this.buffer][0];
        const names = { '\r': 'enter', '\n': 'enter', '\x02': 'prefix', '\x03': 'interrupt' };
        emit(char, names[char] || 'other');
        this.buffer = this.buffer.slice(char.length);
        continue;
      }
      const sequence = this.buffer.match(/^\x1b(?:\[[0-?]*[ -/]*[@-~]|O[@-~])/);
      if (sequence) {
        const raw = sequence[0];
        const name = /^\x1b(?:\[|O)B$/.test(raw) ? 'down' : /^\x1b(?:\[|O)A$/.test(raw) ? 'up' :
          /^\x1b\[1;3B$/.test(raw) ? 'altDown' : raw === `${CSI}19~` ? 'config' : raw === `${CSI}200~` ? 'paste' : 'other';
        emit(raw, name);
        if (raw === `${CSI}200~`) this.paste = true;
        this.buffer = this.buffer.slice(raw.length);
        continue;
      }
      // Some macOS terminals encode Option+Down as ESC followed by Down.
      if (this.buffer.startsWith('\x1b\x1b[B')) { emit(this.buffer.slice(0, 4), 'altDown'); this.buffer = this.buffer.slice(4); continue; }
      if (!flush && (this.buffer === '\x1b' || /^\x1b(?:\[[0-?]*[ -/]*|O|\x1b(?:\[[0-?]*)?)$/.test(this.buffer))) break;
      emit('\x1b', 'escape');
      this.buffer = this.buffer.slice(1);
    }
    return tokens;
  }
}

class HudController {
  constructor(send = () => {}) {
    this.send = send;
    this.snapshot = {};
    this.rows = [];
    this.allRows = [];
    this.root = null;
    this.mode = 'cli';
    this.selected = null;
    this.scroll = 0;
    this.detailOffset = 0;
    this.detailLines = [];
    this.detailWidth = 80;
    this.detailPageRows = 1;
    this.detailCache = null;
    this.requestId = 0;
    this.pendingDetail = null;
    this.prefix = false;
    this.configIndex = 0;
    this.configError = '';
  }
  update(packet) {
    if (packet.type === 'snapshot') {
      const root = rootId(packet.snapshot);
      if (root !== this.root) {
        this.mode = 'cli'; this.selected = null; this.scroll = 0;
        this.pendingDetail = null; this.detailLines = []; this.detailOffset = 0;
      }
      this.root = root;
      this.snapshot = packet.snapshot || {};
      this.rows = Array.isArray(packet.rows) ? packet.rows : [];
      this.allRows = Array.isArray(packet.all_rows) ? packet.all_rows : this.rows;
      const inspectingRecordedAgent = this.mode === 'detail' && this.selected &&
        (this.selected[0] === 'native' ? this.snapshot.agents : this.snapshot.offload?.jobs)?.some(item => item.id === this.selected[1]);
      if (this.selected && !this.selectable().some(row => keyId(row.key) === keyId(this.selected)) && !inspectingRecordedAgent) {
        this.selected = null; this.mode = 'cli'; this.pendingDetail = null;
      }
      if (this.mode === 'detail') this.requestDetail();
    } else if (packet.type === 'detail' && this.mode === 'detail' && packet.request_id === this.pendingDetail &&
      packet.root === this.root && keyId(packet.key) === keyId(this.selected)) {
      this.detailLines = packet.error ? [String(packet.error)] : (packet.lines || []).map(String);
    } else if (packet.type === 'config') this.configError = packet.error ? String(packet.error) : '';
  }
  selectable() { return this.allRows.filter(row => keyId(row.key)); }
  enterHud() {
    const rows = this.selectable();
    if (!rows.length) return false;
    this.selected = rows[0].key; this.mode = 'hud'; this.scroll = 0;
    return true;
  }
  requestDetail() {
    this.pendingDetail = ++this.requestId;
    this.send({ type: 'detail', request_id: this.pendingDetail, root: this.root, key: this.selected });
  }
  input(token, empty) {
    if (this.prefix) {
      this.prefix = false;
      if (token.name === 'down') { const consumed = this.enterHud(); return { consumed, prepend: consumed ? '' : '\x02' }; }
      if (token.raw === 'C') { this.mode = 'config'; return { consumed: true }; }
      return { consumed: false, prepend: '\x02' };
    }
    if (token.name === 'prefix' && this.mode === 'cli') { this.prefix = true; return { consumed: true }; }
    if (token.name === 'config') { this.mode = 'config'; return { consumed: true }; }
    if (this.mode === 'cli') {
      return { consumed: (token.name === 'altDown' || (token.name === 'down' && empty)) && this.enterHud() };
    }
    if (token.name === 'interrupt') { this.mode = 'cli'; this.selected = null; return { consumed: false }; }
    if (token.name === 'escape') {
      if (this.mode === 'detail' && this.selectable().some(row => keyId(row.key) === keyId(this.selected))) this.mode = 'hud';
      else { this.mode = 'cli'; this.selected = null; }
      return { consumed: true };
    }
    if (this.mode === 'hud') {
      if (token.name === 'down' || token.name === 'up') {
        const rows = this.selectable(), index = rows.findIndex(row => keyId(row.key) === keyId(this.selected));
        if (token.name === 'up' && index === 0) { this.mode = 'cli'; this.selected = null; return { consumed: true }; }
        this.selected = rows[Math.max(0, Math.min(rows.length - 1, index + (token.name === 'down' ? 1 : -1)))]?.key || null;
      } else if (token.name === 'enter') {
        this.mode = 'detail'; this.detailOffset = 0; this.detailLines = ['Loading…']; this.requestDetail();
      } else {
        // Typing immediately returns to the CLI without changing its draft.
        this.mode = 'cli'; this.selected = null; return { consumed: false };
      }
    } else if (this.mode === 'detail') {
      if (token.name === 'down') this.detailOffset = Math.min(Math.max(0, this.wrappedDetails(this.detailWidth).length - this.detailPageRows), this.detailOffset + 1);
      else if (token.name === 'up') this.detailOffset = Math.max(0, this.detailOffset - 1);
    } else if (this.mode === 'config') {
      if (token.name === 'down') this.configIndex = Math.min(3, this.configIndex + 1);
      else if (token.name === 'up') this.configIndex = Math.max(0, this.configIndex - 1);
      else if (token.name === 'enter') this.send({ type: 'config', request_id: ++this.requestId, changes: { preset: ['minimal', 'standard', 'full', 'focus'][this.configIndex] } });
    }
    return { consumed: true };
  }
  height(totalRows, fallback = 11) {
    const rows = this.mode === 'hud' ? this.allRows : this.rows;
    if (!rows.length) return 0;
    const desired = Number(this.snapshot.display?.height) || fallback;
    return Math.max(0, Math.min(desired, Math.floor(totalRows / 2), rows.length + (this.mode === 'hud' ? 1 : 0)));
  }
  hudLines(height, width) {
    if (!height) return [];
    const colors = { ...DEFAULT_COLORS, ...this.snapshot.display?.colors };
    const source = this.mode === 'hud' ? this.allRows : this.rows;
    const footer = this.mode === 'hud' || source.length > height;
    let pinnedCount = 0;
    while (source[pinnedCount]?.pinned) pinnedCount++;
    const pinnedRoom = Math.min(pinnedCount, Math.max(0, height - Number(footer) - Number(source.length > pinnedCount)));
    const pinned = source.slice(0, pinnedRoom), details = source.slice(pinnedRoom);
    const room = Math.max(0, height - pinned.length - Number(footer));
    this.scroll = Math.min(this.scroll, Math.max(0, details.length - room));
    const target = details.findIndex(row => keyId(row.key) === keyId(this.selected) && this.selected);
    if (target >= 0 && room) {
      if (target < this.scroll) this.scroll = target;
      if (target >= this.scroll + room) this.scroll = target - room + 1;
    }
    const visible = pinned.concat(details.slice(this.scroll, this.scroll + room));
    const lines = visible.map(row => styledRow(row, width, colors, this.mode === 'hud' && keyId(row.key) === keyId(this.selected)));
    if (footer) {
      const language = this.snapshot.display?.language;
      const hint = this.mode === 'hud' ? (language === 'en' ? ' ↑↓ select · Enter inspect · Esc back to input' : ' ↑↓ 选择 · Enter 查看 · Esc 返回输入') :
        (language === 'en' ? ' ↓ from empty input · Alt+↓ inspect agents' : ' 空输入框 ↓ · Alt+↓ 选择 agent');
      lines.push(styledRow({ spans: [[hint, 'dim']] }, width, colors));
    }
    return lines.concat(Array(Math.max(0, height - lines.length)).fill(''));
  }
  wrappedDetails(width) {
    if (!this.detailCache || this.detailCache.width !== width || this.detailCache.source !== this.detailLines) {
      this.detailCache = { width, source: this.detailLines, lines: this.detailLines.flatMap(line => wrapText(line, width)) };
    }
    return this.detailCache.lines;
  }
  overlayLines(height, width) {
    const colors = { ...DEFAULT_COLORS, ...this.snapshot.display?.colors };
    if (this.mode === 'config') {
      const rows = [' Codex HUD · configuration', '', ...['minimal', 'standard', 'full', 'focus'].map((name, i) => `${this.configIndex === i ? ' ›' : '  '} ${name}`), '', this.configError, ' ↑↓ select · Enter apply · Esc back'];
      return rows.map((text, i) => styledRow({ spans: [[text, i === 0 ? 'model' : 'text']] }, width, colors, i === this.configIndex + 2));
    }
    this.detailWidth = width;
    this.detailPageRows = Math.max(1, height - 2);
    const details = this.wrappedDetails(width);
    this.detailOffset = Math.max(0, Math.min(this.detailOffset, Math.max(0, details.length - this.detailPageRows)));
    const header = ` Agent ${this.selected?.[0]} · ${this.selected?.[1]} · read only`;
    const lines = [styledRow({ spans: [[header, 'model']] }, width, colors)];
    lines.push(...details.slice(this.detailOffset, this.detailOffset + Math.max(0, height - 2)));
    while (lines.length < height - 1) lines.push('');
    lines.push(styledRow({ spans: [[' ↑↓ scroll · Esc back to HUD', 'dim']] }, width, colors));
    return lines;
  }
}

class DiffRenderer {
  constructor(write) { this.write = write; this.previous = []; this.lastCursor = ''; this.lastModes = ''; }
  invalidate() { this.previous = []; this.lastCursor = ''; this.lastModes = ''; }
  render(lines, cursor, modes = '') {
    let changes = '';
    for (let y = 0; y < lines.length; y++) {
      if (lines[y] !== this.previous[y]) changes += `${CSI}${y + 1};1H${CSI}0m${CSI}2K${lines[y]}`;
    }
    const cursorState = `${CSI}${cursor.y + 1};${cursor.x + 1}H${cursor.style || ''}${CSI}?25${cursor.visible ? 'h' : 'l'}`;
    const changedCursor = cursorState !== this.lastCursor;
    const changedModes = modes !== this.lastModes;
    if (!changes && !changedCursor && !changedModes) return false;
    // One synchronized frame, and no cursor hide/show roundtrip per HUD tick.
    this.write(`${CSI}?2026h${changes}${CSI}0m${changedModes ? modes : ''}${changes || changedCursor ? cursorState : ''}${CSI}?2026l`);
    this.previous = lines;
    this.lastCursor = cursorState;
    this.lastModes = modes;
    return true;
  }
}

function childEnvironment(runtime, env = process.env) {
  const result = { ...env, CODEX_HUD_ACTIVE: '1', CODEX_HUD_RUNTIME: runtime, RUST_LOG: 'warn,codex_core::session=info' };
  delete result.CODEX_THREAD_ID;
  delete result.CODEX_SESSION_ID;
  return result;
}

function atomicJson(filename, value) {
  const temporary = `${filename}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value), { mode: 0o600 });
  fs.renameSync(temporary, filename);
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; if (error.code === 'EPERM') return true; throw error; }
}

async function waitForOwnedExit(child, bridge, hasExited, timeout = 7000) {
  const deadline = Date.now() + timeout;
  while ((!hasExited() && (!child?.pid || processAlive(child.pid))) || (bridge?.pid && processAlive(bridge.pid))) {
    if (Date.now() >= deadline) throw new Error(`Owned children did not stop: CLI ${child?.pid ?? 'pending'}, bridge ${bridge?.pid ?? 'none'}`);
    await new Promise(resolve => setTimeout(resolve, 25));
  }
}

async function run(runtime, options = {}) {
  runtime = path.resolve(runtime);
  const spec = JSON.parse(fs.readFileSync(path.join(runtime, 'spec.json'), 'utf8'));
  if (path.resolve(spec.runtime) !== runtime || !Array.isArray(spec.argv) || !spec.argv.length ||
    !Array.isArray(spec.bridge_argv) || !spec.bridge_argv.length) throw new Error('Invalid native terminal specification');
  if (runtime === path.parse(runtime).root || runtime === path.resolve(spec.home || '.') || runtime === path.resolve(spec.cwd || '.')) {
    throw new Error('Native terminal runtime must be an isolated launch directory');
  }
  const stdin = options.stdin || process.stdin, stdout = options.stdout || process.stdout;
  if (!stdin.isTTY || !stdout.isTTY) throw new Error('Codex HUD native terminal requires an interactive terminal');
  const { pty, Terminal } = options.dependencies || loadDependencies();
  let cols = Math.max(2, stdout.columns || 80), rows = Math.max(2, stdout.rows || 24);
  let child, bridge, screen, composer, childData, childExit, bridgeInput, renderTimer, escapeTimer, prefixTimer, pidTimer, closed = false, childExited = false;
  let cleanupPromise;
  const rawBefore = Boolean(stdin.isRaw);
  const renderer = new DiffRenderer(data => stdout.write(data));
  const decoder = new InputDecoder();
  const log = message => {
    try { fs.appendFileSync(path.join(runtime, 'native.log'), safeText(message) + '\n', { mode: 0o600 }); } catch {}
  };
  const debug = message => { if (process.env.CODEX_HUD_NATIVE_DEBUG === '1') log(`${new Date().toISOString()} ${message}`); };
  const send = packet => { if (bridge?.stdin.writable && !bridge.stdin.destroyed) bridge.stdin.write(JSON.stringify(packet) + '\n'); };
  const controller = new HudController(send);
  const desiredMainRows = () => Math.max(1, rows - controller.height(rows, spec.hud_height));
  let mainRows = desiredMainRows();
  const resizeChild = () => {
    const desired = desiredMainRows();
    if (mainRows !== desired || screen.terminal.cols !== cols) {
      mainRows = desired;
      screen.resize(cols, mainRows);
      composer?.rebase();
      if (!childExited) child.resize(cols, mainRows);
      renderer.invalidate();
    }
  };
  const render = () => {
    renderTimer = null;
    if (closed || screen.pendingWrites || screen.terminal.modes.synchronizedOutputMode) return;
    resizeChild();
    const terminal = screen.terminal, buffer = terminal.buffer.active;
    const overlay = controller.mode === 'detail' || controller.mode === 'config';
    let lines = overlay ? controller.overlayLines(rows, cols) : screen.lines().concat(controller.hudLines(rows - mainRows, cols));
    lines = lines.slice(0, rows);
    while (lines.length < rows) lines.push('');
    const tracking = controller.mode === 'cli' ? terminal.modes.mouseTrackingMode : 'none';
    const trackingCode = { x10: 9, vt200: 1000, drag: 1002, any: 1003 }[tracking];
    const modes = `${CSI}?2004${terminal.modes.bracketedPasteMode ? 'h' : 'l'}${CSI}?1004${terminal.modes.sendFocusMode ? 'h' : 'l'}` +
      `${CSI}?9l${CSI}?1000l${CSI}?1002l${CSI}?1003l${CSI}?1006l` + (trackingCode ? `${CSI}?${trackingCode}h${CSI}?1006h` : '');
    renderer.render(lines, { x: Math.min(cols - 1, buffer.cursorX), y: Math.min(mainRows - 1, buffer.cursorY),
      visible: controller.mode === 'cli' && screen.cursorVisible, style: screen.cursorStyle }, modes);
  };
  const scheduleRender = () => { if (!closed && !renderTimer) renderTimer = setTimeout(render, 8); };
  const finished = new Promise(resolve => {
    const cleanup = code => {
      if (closed) return cleanupPromise;
      closed = true;
      clearTimeout(renderTimer); clearTimeout(escapeTimer); clearTimeout(prefixTimer);
      stdin.removeListener('data', onInput); stdout.removeListener('resize', onResize);
      stdin.pause();
      if (stdin.setRawMode) { try { stdin.setRawMode(rawBefore); } catch {} }
      for (const [name, handler] of Object.entries(signals)) process.removeListener(name, handler);
      stdout.write(`${CSI}0m${CSI}?2004l${CSI}?1004l${CSI}?9l${CSI}?1000l${CSI}?1002l${CSI}?1003l${CSI}?1006l${CSI}?25h${CSI}?7h${CSI}?1049l`);
      debug(`cleanup code=${code} cli_pid=${child?.pid} bridge_pid=${bridge?.pid}`);
      // Windows node-pty's normal exit closes only its output socket; its
      // ConPTY output worker remains referenced until public kill() disposes
      // it. Release our PTY even when the official process already exited.
      if (child && (!childExited || process.platform === 'win32')) {
        try { child.kill(); } catch (error) { log(error.message); }
        // Windows node-pty closes ConPTY and kills its console list
        // asynchronously. Terminate only our verified direct child promptly.
        if (!childExited && process.platform === 'win32' && Number.isInteger(child.pid) && child.pid > 0) {
          try { process.kill(child.pid); } catch (error) { if (error.code !== 'ESRCH') log(error.message); }
        }
      }
      if (bridge?.pid && bridge.exitCode === null) { try { bridge.kill(); } catch (error) { log(error.message); } }
      cleanupPromise = (async () => {
        try {
          // Keep PTY output drained and the exit listener alive while ConPTY
          // closes; removing them early can leave console processes behind.
          if (child) await waitForOwnedExit(child, bridge, () => childExited);
          debug('owned children stopped');
        } catch (error) { log(error.message); code = 1; (options.stderr || process.stderr).write(`Codex HUD: ${error.message}\n`); }
        clearInterval(pidTimer);
        childData?.dispose(); childExit?.dispose(); bridgeInput?.close(); screen?.dispose();
        // A Windows cwd/console handle may be released shortly after exit.
        // Retry asynchronously, so socket/process exit events can still run.
        try { await fs.promises.rm(runtime, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
        catch (error) { code = 1; (options.stderr || process.stderr).write(`Codex HUD cleanup: ${error.message}\n`); }
        if (process.env.CODEX_HUD_NATIVE_DEBUG === '1') {
          // Public resource types only, after the UI has restored the outer
          // terminal. An unref'd probe runs only if a handle keeps us alive.
          const probe = setTimeout(() => (options.stderr || process.stderr).write(`Codex HUD shutdown resources: ${JSON.stringify(process.getActiveResourcesInfo())}\n`), 1500);
          probe.unref();
        }
        resolve(code);
      })();
      return cleanupPromise;
    };
    const signals = { SIGINT: () => cleanup(130), SIGTERM: () => cleanup(143), SIGHUP: () => cleanup(129),
      ...(process.platform === 'win32' ? { SIGBREAK: () => cleanup(130) } : {}) };
    const forward = (data, token) => { if (!closed && !childExited) { if (token) composer.forwarded(token); child.write(data); } };
    const route = tokens => {
      for (const token of tokens) {
        const hadPrefix = controller.prefix;
        // A key can arrive before the official CLI has echoed the previous
        // edit. Do not select HUD from the stale empty-composer screen.
        const result = controller.input(token, composer.empty());
        if (hadPrefix) clearTimeout(prefixTimer);
        if (controller.prefix) prefixTimer = setTimeout(() => { controller.prefix = false; forward('\x02'); }, 2000);
        if (!result.consumed) forward((result.prepend || '') + token.raw, token);
      }
      scheduleRender();
    };
    const onInput = data => {
      clearTimeout(escapeTimer);
      route(decoder.push(data));
      if (decoder.buffer && !decoder.paste) escapeTimer = setTimeout(() => route(decoder.push('', true)), 35);
    };
    const onResize = () => {
      cols = Math.max(2, stdout.columns || 80); rows = Math.max(2, stdout.rows || 24);
      send({ type: 'size', cols, rows }); renderer.invalidate(); scheduleRender();
    };
    try {
      debug(`startup cols=${cols} rows=${rows} cwd=${spec.cwd}`);
      screen = new VirtualScreen(Terminal, cols, mainRows, scheduleRender, data => { if (child && !childExited) child.write(data); });
      composer = new ComposerGuard(screen);
      child = pty.spawn(spec.argv[0], spec.argv.slice(1), { name: 'xterm-256color', cols, rows: mainRows,
        cwd: spec.cwd, env: childEnvironment(runtime), ...(process.platform === 'win32' ? {} : { encoding: 'utf8' }), conptyInheritCursor: false });
      const runner = { pid: child.pid > 0 ? child.pid : null, started: Date.now() / 1000 };
      atomicJson(path.join(runtime, 'runner.json'), runner);
      // ConPTY's public pid is initially 0, then becomes the real CLI pid
      // after its asynchronous pipe connection. Never publish PID 0.
      const publishPid = () => {
        if (child.pid > 0 && runner.pid !== child.pid) {
          runner.pid = child.pid;
          try { atomicJson(path.join(runtime, 'runner.json'), runner); debug(`cli ready pid=${runner.pid}`); } catch (error) { if (!closed) log(error.message); }
        }
        if (runner.pid) clearInterval(pidTimer);
      };
      pidTimer = setInterval(publishPid, 25);
      childData = child.onData(data => {
        publishPid(); debug(`cli data bytes=${data.length}`);
        if (!closed) screen.write(data, () => { composer.observe(); debug(`cli parsed cursor=${screen.terminal.buffer.active.cursorX},${screen.terminal.buffer.active.cursorY} empty=${screen.emptyComposer()} sync=${screen.terminal.modes.synchronizedOutputMode}`); });
      });
      childExit = child.onExit(event => { childExited = true; debug(`cli exit code=${event.exitCode}`); cleanup(event.exitCode || 0); });
      bridge = spawn(spec.bridge_argv[0], spec.bridge_argv.slice(1), { cwd: spec.cwd,
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
      runner.bridge_pid = bridge.pid;
      atomicJson(path.join(runtime, 'runner.json'), runner); debug(`bridge started pid=${bridge.pid}`);
      bridge.stdin.on('error', error => log(error.message));
      bridge.stderr.setEncoding('utf8'); bridge.stderr.on('data', log);
      bridge.on('error', error => { log(error.message); controller.update({ type: 'snapshot', snapshot: {}, rows: [] }); scheduleRender(); });
      bridge.on('exit', () => { if (!closed) { controller.update({ type: 'snapshot', snapshot: {}, rows: [] }); scheduleRender(); } });
      bridgeInput = readline.createInterface({ input: bridge.stdout });
      bridgeInput.on('line', line => {
        try {
          if (closed) return;
          const previousRoot = controller.root;
          const packet = JSON.parse(line);
          debug(`bridge packet type=${packet.type} rows=${packet.rows?.length}`);
          controller.update(packet);
          if (controller.root !== previousRoot) composer.reset();
          scheduleRender();
        } catch (error) { log(`Bridge protocol: ${error.message}`); }
      });
      for (const [name, handler] of Object.entries(signals)) process.on(name, handler);
      stdout.write(`${CSI}?1049h${CSI}?7l${CSI}2J`);
      debug('outer VT initialized');
      stdin.setEncoding('utf8'); stdin.setRawMode(true); stdin.resume();
      stdin.on('data', onInput); stdout.on('resize', onResize);
      send({ type: 'size', cols, rows }); scheduleRender();
    } catch (error) { log(error.stack || error.message); cleanup(1); (options.stderr || process.stderr).write(`Codex HUD: ${error.message}\n`); }
  });
  return finished;
}

module.exports = { loadDependencies, safeText, keyId, rootId, colorSgr, clip, wrapText, styledRow, terminalLine,
  isEmptyComposer, composerFingerprint, ComposerGuard, VirtualScreen, InputDecoder, HudController, DiffRenderer, childEnvironment, processAlive, waitForOwnedExit, run };

if (require.main === module) {
  if (!process.argv[2]) { process.stderr.write('Usage: node native/terminal.cjs <runtime>\n'); process.exitCode = 2; }
  else run(process.argv[2]).then(code => { process.exitCode = code; }).catch(error => { process.stderr.write(`Codex HUD: ${error.message}\n`); process.exitCode = 1; });
}
