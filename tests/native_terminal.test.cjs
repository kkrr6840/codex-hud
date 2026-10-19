'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { spawnSync } = require('node:child_process');
const hud = require('../native/terminal.cjs');
const { Terminal, pty } = hud.loadDependencies();
const CSI = '\x1b[';
const prompt = `${CSI}2J${CSI}10;1H› ${CSI}2mAsk Codex to do anything${CSI}0m${CSI}10;3H${CSI}?25h`;
const row = (text, id, pinned = false) => ({ spans: [[text, id ? 'agent' : 'model']], pinned, key: id ? ['native', id] : null });
const packet = (root = 'root-a', rows = [row('Codex model', null, true), row('agent Alpha', 'a'), row('agent Beta', 'b')]) =>
  ({ type: 'snapshot', snapshot: { root: { id: root }, display: { height: 6, language: 'en', colors: { agent: '#ff0088' } } }, rows, all_rows: rows });
const input = (raw, name) => ({ raw, name });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, message, timeout = 6000) {
  const deadline = Date.now() + timeout;
  while (!predicate()) { if (Date.now() > deadline) throw new Error(message); await wait(25); }
}
function write(terminal, data) { return new Promise(resolve => terminal.write(data, resolve)); }
function readFixture(filename) {
  try { return fs.readFileSync(filename, 'utf8').slice(-8000); } catch (error) { return `[${error.code}: ${filename}]`; }
}
function diagnose(error, runtime, state = {}) {
  const diagnostic = { platform: process.platform, ...state, runner: readFixture(path.join(runtime, 'runner.json')),
    native: readFixture(path.join(runtime, 'native.log')), snapshot: readFixture(path.join(runtime, 'snapshot.json')) };
  error.message += `\nNative fixture diagnostics: ${JSON.stringify(diagnostic)}`;
  return error;
}
async function removeFixture(base, originalError) {
  try { await fs.promises.rm(base, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
  catch (error) { if (!originalError) throw error; process.stderr.write(`Additional fixture cleanup failure: ${error.message}\n`); }
}
async function stopFixture(outer, hasExited, runtime, originalError) {
  let owned = {};
  try { owned = JSON.parse(fs.readFileSync(path.join(runtime, 'runner.json'), 'utf8')); } catch {}
  try {
    if (!hasExited()) {
      // Exit through the UI first; hard termination is only a test-failure
      // fallback and targets these fixture PIDs, never the test runner/PID 0.
      outer.write('\x1b'); await wait(60); outer.write('\x1b'); await wait(60); outer.write('/quit\r');
      const deadline = Date.now() + 2000;
      while (!hasExited() && Date.now() < deadline) await wait(25);
    }
    // ConPTY's normal exit leaves the wrapper's output worker referenced;
    // public kill() releases it after the real onExit/exit-code assertions.
    if (!hasExited() || process.platform === 'win32') { try { outer.kill(); } catch {} }
    for (const pid of [owned.pid, owned.bridge_pid, outer.pid]) {
      if (Number.isInteger(pid) && pid > 0 && pid !== process.pid && hud.processAlive(pid)) { try { process.kill(pid); } catch (error) { if (error.code !== 'ESRCH') throw error; } }
    }
    await until(() => [owned.pid, owned.bridge_pid, outer.pid].every(pid => !hud.processAlive(pid)), 'fixture processes stopped before directory removal', 8000);
  } catch (error) { if (!originalError) throw error; process.stderr.write(`Additional fixture stop failure: ${error.message}\n`); }
}

test('empty composer is read from VT cells; draft, hidden cursor and native menu pass through', async () => {
  const terminal = new Terminal({ cols: 80, rows: 24, allowProposedApi: true });
  try {
    await write(terminal, prompt);
    assert.equal(hud.isEmptyComposer(terminal), true);
    assert.equal(hud.isEmptyComposer(terminal, false), false);
    await write(terminal, `${CSI}10;3H${CSI}0mAsk Codex to do anything${CSI}10;3H`);
    assert.equal(hud.isEmptyComposer(terminal), false, 'same user text is not a placeholder');
    await write(terminal, `${CSI}10;1H${CSI}2K› draft${CSI}10;8H`);
    assert.equal(hud.isEmptyComposer(terminal), false);
    await write(terminal, `${CSI}10;1H${CSI}2K› ${CSI}2mSearch commands${CSI}0m${CSI}10;3H`);
    assert.equal(hud.isEmptyComposer(terminal), false);
  } finally { terminal.dispose(); }
});

test('streaming key decoder preserves unknown edit sequences and bracketed paste', () => {
  const decoder = new hud.InputDecoder();
  assert.deepEqual(decoder.push('\x1b['), []);
  assert.deepEqual(decoder.push('B'), [input(`${CSI}B`, 'down')]);
  assert.deepEqual(decoder.push(`${CSI}1;3B\x1b\x1b[B\x1bOA${CSI}3~`),
    [input(`${CSI}1;3B`, 'altDown'), input('\x1b\x1b[B', 'altDown'), input('\x1bOA', 'up'), input(`${CSI}3~`, 'other')]);
  assert.deepEqual(decoder.push('\x1b'), []);
  assert.deepEqual(decoder.push('', true), [input('\x1b', 'escape')]);
  const parts = [decoder.push(`${CSI}200~hello${CSI}B`), decoder.push('中文\x1b[20'), decoder.push('1~')].flat();
  assert.equal(parts.map(part => part.raw).join(''), `${CSI}200~hello${CSI}B中文${CSI}201~`);
  assert.ok(parts.every(part => part.name === 'paste'));
});

test('empty Down selects in HUD, arrows move, Enter requests detail and two Esc return', () => {
  const requests = [], controller = new hud.HudController(request => requests.push(request));
  controller.update(packet());
  assert.equal(controller.input(input(`${CSI}B`, 'down'), false).consumed, false);
  assert.equal(controller.mode, 'cli');
  assert.equal(controller.input(input(`${CSI}B`, 'down'), true).consumed, true);
  assert.equal(controller.mode, 'hud');
  assert.deepEqual(controller.selected, ['native', 'a']);
  assert.ok(controller.hudLines(4, 80)[1].includes(`${CSI}0;1;7magent Alpha`));
  controller.input(input(`${CSI}B`, 'down'), false);
  assert.deepEqual(controller.selected, ['native', 'b']);
  controller.input(input('\r', 'enter'), false);
  assert.equal(controller.mode, 'detail');
  assert.deepEqual(requests[0], { type: 'detail', request_id: 1, root: 'root-a', key: ['native', 'b'] });
  controller.update({ type: 'detail', request_id: 1, root: 'root-a', key: ['native', 'b'], lines: ['real output'] });
  assert.ok(controller.overlayLines(10, 80).includes('real output'));
  controller.input(input('\x1b', 'escape'), false);
  assert.equal(controller.mode, 'hud');
  controller.input(input('\x1b', 'escape'), false);
  assert.equal(controller.mode, 'cli');
});

test('skill selection and task requests stay scoped, cancellable and draft-only', () => {
  const requests = [], c = new hud.HudController(r => requests.push(r));
  const p = packet();
  p.snapshot.agents = [{ id: 'a', status: 'running', turn_id: 't', started: 10 }];
  c.update(p); c.enterHud(); c.input(input('\r', 'enter'), true);
  c.input(input('s', 'other'), true);
  assert.equal(c.mode, 'action');
  assert.equal(requests.filter(r => r.type === 'action').length, 0);
  c.input(input('\r', 'enter'), true); c.input(input('\r', 'enter'), true);
  const first = requests.at(-1);
  assert.equal(requests.filter(r => r.type === 'action').length, 1);
  c.input(input('\x1b', 'escape'), true);
  c.update({ ...first, draft: 'stale request' }); assert.equal(c.takeDraft(true), null);
  c.input(input('s', 'other'), true); c.input(input('\r', 'enter'), true);
  c.update({ ...requests.at(-1), draft: 'stop child a' });
  assert.equal(c.takeDraft(false), null); assert.equal(c.mode, 'action');
  assert.match(c.actionError, /not empty/);
  c.input(input('\r', 'enter'), true); c.update({ ...requests.at(-1), draft: 'stop child a' });
  assert.equal(c.takeDraft(true), 'stop child a'); assert.equal(c.mode, 'cli');
  assert.equal(c.takeDraft(true), null);
  c.enterHud(); c.input(input('\r', 'enter'), true); c.input(input('s', 'other'), true); c.input(input('\r', 'enter'), true);
  const obsolete = requests.at(-1);
  c.update(packet('other-root')); c.update({ ...obsolete, draft: 'wrong conversation' });
  assert.equal(c.takeDraft(true), null); assert.equal(c.pendingAction, null);
  const skill = { spans: [['Skills demo', 'agent']], key: ['skills', 'turn-1'] };
  const skills = packet('root-a', [skill]);
  skills.snapshot.root.skills = ['demo']; skills.snapshot.root.skill_turn_id = 'turn-1';
  c.update(skills); c.enterHud(); c.input(input('\r', 'enter'), true);
  assert.equal(c.selectedItem().kind, 'skills'); assert.equal(c.availableAction(), null);
  c.update({ ...requests.at(-1), lines: ['Path: /skills/demo/SKILL.md'] });
  assert.match(c.overlayLines(10, 80).join('\n'), /SKILL.md/);
  c.input(input('s', 'other'), true); assert.equal(c.mode, 'detail');
  skills.snapshot.root.skill_turn_id = 'turn-2'; skills.rows = []; skills.all_rows = [];
  c.update(skills); assert.equal(c.mode, 'cli');
});

test('prefix Down exposes task history without changing normal visibility', () => {
  const c = new hud.HudController(), p = packet('root-a', []);
  p.history_rows = [row('completed child', 'a')]; p.snapshot.agents = [{ id: 'a', status: 'completed' }];
  c.update(p); assert.equal(c.enterHud(), false);
  c.input(input('\x02', 'prefix'), true); c.input(input(`${CSI}B`, 'down'), true);
  assert.equal(c.mode, 'hud'); assert.deepEqual(c.selected, ['native', 'a']);
  assert.equal(c.availableAction(), 'retry');
  p.snapshot.agents[0].status = 'idle'; p.snapshot.agents[0].ended = 20; c.update(p);
  assert.equal(c.availableAction(), 'retry', 'cancelled tasks with an end time can be retried');
  c.input(input('\x1b', 'escape'), true); assert.equal(c.enterHud(), false);
});

test('config waits for matching success, prevents duplicates and retries failures', () => {
  const requests = [], controller = new hud.HudController(request => requests.push(request));
  controller.update(packet()); controller.enterHud();
  const press = (name, raw = '') => controller.input(input(raw, name), false);
  const f8 = new hud.InputDecoder().push(`${CSI}19~`)[0];
  assert.equal(controller.input(f8, false).consumed, true);
  for (let i = 0; i < 3; i++) press('down');
  assert.equal(press('enter').consumed, true);
  const first = requests[0];
  assert.equal(first.changes.preset, 'focus');
  assert.match(controller.overlayLines(12, 80).join('\n'), /Applying/);
  assert.equal(press('enter').consumed, true);
  press('up');
  assert.equal(requests.length, 1); assert.equal(controller.configIndex, 3);
  controller.update(packet());
  controller.update({ type: 'config', request_id: first.request_id + 1 });
  assert.equal(controller.pendingConfig, first.request_id);
  controller.update({ ...first, error: 'disk full' });
  assert.equal(controller.mode, 'config'); assert.equal(controller.pendingConfig, null);
  assert.match(controller.overlayLines(12, 80).join('\n'), /disk full/);
  press('enter');
  assert.equal(controller.configError, ''); assert.equal(requests.length, 2);
  assert.notEqual(requests[1].request_id, first.request_id);
  controller.update(first);
  assert.equal(controller.mode, 'config');
  controller.update(requests[1]);
  assert.equal(controller.mode, 'cli'); assert.equal(controller.selected, null);
  assert.equal(press('other', 'x').consumed, false);
});

test('config cancellation, reopen and root change reject late replies', () => {
  const requests = [], controller = new hud.HudController(request => requests.push(request));
  controller.update(packet());
  const press = (name, raw = '') => controller.input(input(raw, name), false);
  press('prefix', '\x02');
  assert.deepEqual(press('config', `${CSI}19~`), { consumed: true });
  assert.equal(controller.prefix, false);
  press('escape');
  for (const cancel of ['escape', 'config', 'root']) {
    press('config'); press('down'); press('enter');
    const old = requests.at(-1);
    const selectedPreset = controller.configIndex;
    if (cancel === 'root') controller.update(packet('root-b'));
    else assert.equal(press(cancel).consumed, true);
    assert.equal(controller.pendingConfig, null);
    const mode = controller.mode;
    controller.update({ ...old, error: 'late failure' });
    assert.equal(controller.mode, mode); assert.equal(controller.configError, '');
    if (mode === 'cli') { press('prefix', '\x02'); assert.equal(press('other', 'C').consumed, true); }
    assert.equal(controller.configIndex, selectedPreset);
    press('enter');
    const current = requests.at(-1);
    controller.update(old);
    assert.equal(controller.mode, 'config'); assert.equal(controller.pendingConfig, current.request_id);
    controller.update(current); assert.equal(controller.mode, 'cli');
  }
});

test('Up from first agent returns to input; typing resumes native editing', () => {
  const controller = new hud.HudController(); controller.update(packet());
  controller.enterHud(); controller.input(input(`${CSI}A`, 'up'), false);
  assert.equal(controller.mode, 'cli'); assert.equal(controller.selected, null);
  controller.enterHud();
  assert.equal(controller.input(input('x', 'other'), false).consumed, false);
  assert.equal(controller.mode, 'cli');
});

test('completed row disappears without closing an open final-output detail', () => {
  const controller = new hud.HudController();
  controller.update(packet()); controller.enterHud();
  controller.input(input('\r', 'enter'), true);
  const finished = packet('root-a', []);
  finished.snapshot.agents = [{ id: 'a', status: 'completed' }];
  controller.update(finished);
  assert.equal(controller.mode, 'detail');
  controller.update({ type: 'detail', request_id: controller.pendingDetail, root: 'root-a', key: ['native', 'a'], lines: ['Final answer'] });
  assert.ok(controller.overlayLines(10, 80).includes('Final answer'));
  controller.input(input('\x1b', 'escape'), false);
  assert.equal(controller.mode, 'cli');
  assert.equal(controller.selected, null);
  controller.update(packet()); controller.enterHud(); controller.update(finished);
  assert.equal(controller.mode, 'cli', 'selection returns to input when the selected row completes');
});

test('detail refresh rejects stale requests and root changes clear selection', () => {
  const requests = [], controller = new hud.HudController(request => requests.push(request));
  controller.update(packet()); controller.enterHud(); controller.input(input('\r', 'enter'), true);
  controller.update(packet());
  assert.equal(requests.length, 2, 'each collector update refreshes read-only detail');
  controller.update({ type: 'detail', request_id: 1, root: 'root-a', key: ['native', 'a'], lines: ['stale'] });
  assert.deepEqual(controller.detailLines, ['Loading…']);
  controller.update({ type: 'detail', request_id: 2, root: 'root-a', key: ['native', 'a'], lines: ['live output'] });
  assert.deepEqual(controller.detailLines, ['live output']);
  controller.update(packet('root-b'));
  assert.equal(controller.mode, 'cli'); assert.equal(controller.selected, null);
  controller.update({ type: 'detail', request_id: 2, root: 'root-a', key: ['native', 'a'], lines: ['wrong root'] });
  assert.deepEqual(controller.detailLines, []);
});

test('detail wraps complete long answers by terminal cells and scrolls through every display line', async () => {
  const chinese = '中文回答保留所有文字，以及一段 English answer that exceeds the terminal width.';
  const source = [chinese, '', '组合字符 e\u0301 与 emoji 👩‍💻 保持完整'];
  const wrapped = source.flatMap(line => hud.wrapText(line, 12));
  assert.equal(hud.wrapText(chinese, 12).join(''), chinese, 'no answer content is truncated');
  assert.deepEqual(hud.wrapText('中文中文a', 5), ['中文', '中文a']);
  assert.ok(hud.wrapText(source[2], 12).some(line => line.includes('e\u0301')));
  assert.ok(hud.wrapText(source[2], 12).some(line => line.includes('👩‍💻')));
  const terminal = new Terminal({ cols: 12, rows: wrapped.length + 2, allowProposedApi: true });
  try {
    // Verify Chinese/Latin wraps with the same real VT emulator used by the
    // terminal host: no line crosses the right edge or overwrites a glyph.
    const plain = hud.wrapText(chinese, 12);
    await write(terminal, `${CSI}?7l` + plain.map((line, i) => `${CSI}${i + 1};1H${line}`).join(''));
    assert.equal(plain.map((_, i) => terminal.buffer.active.getLine(i).translateToString(true)).join(''), chinese);
  } finally { terminal.dispose(); }
  const controller = new hud.HudController(); controller.update(packet()); controller.enterHud();
  controller.input(input('\r', 'enter'), true);
  controller.update({ type: 'detail', request_id: 1, root: 'root-a', key: ['native', 'a'], lines: source });
  const seen = new Set();
  for (let i = 0; i < wrapped.length; i++) {
    const view = controller.overlayLines(5, 12).slice(1, 4);
    view.forEach(line => seen.add(line));
    controller.input(input(`${CSI}B`, 'down'), false);
  }
  assert.ok(wrapped.every(line => seen.has(line)), 'all wrapped answer lines can be reached by Down');
  assert.equal(controller.detailOffset, wrapped.length - 3);
  controller.overlayLines(5, 24);
  assert.equal(controller.wrappedDetails(24).join(''), source.join(''), 'resize recomputes wrapping without losing text');
});

test('matching detail errors appear in the read-only overlay', () => {
  const controller = new hud.HudController(); controller.update(packet()); controller.enterHud();
  controller.input(input('\r', 'enter'), true);
  controller.update({ type: 'detail', request_id: 1, root: 'root-a', key: ['native', 'a'], error: 'Agent output is unavailable' });
  assert.ok(controller.overlayLines(10, 80).includes('Agent output is unavailable'));
});

test('empty data hides HUD, display height limits reserve and hidden agents remain selectable', () => {
  const controller = new hud.HudController();
  controller.update(packet('root', [])); assert.equal(controller.height(24), 0);
  const update = packet(); update.all_rows = update.rows.concat(Array.from({ length: 20 }, (_, i) => row(`agent ${i}`, String(i))));
  controller.update(update); assert.equal(controller.height(24), 3);
  controller.enterHud(); assert.equal(controller.height(8), 4);
  for (let i = 0; i < 22; i++) controller.input(input(`${CSI}B`, 'down'), false);
  assert.deepEqual(controller.selected, ['native', '19']);
  assert.ok(controller.hudLines(4, 80).some(line => line.includes(`${CSI}0;1;7magent 19`)));
});

test('auxiliary bindings and prefix fallback never lose CLI keys', () => {
  const controller = new hud.HudController();
  controller.input(input('\x02', 'prefix'), false);
  assert.deepEqual(controller.input(input(`${CSI}B`, 'down'), false), { consumed: false, prepend: '\x02' });
  controller.input(input('\x02', 'prefix'), false);
  assert.deepEqual(controller.input(input('f', 'other'), false), { consumed: false, prepend: '\x02' });
  controller.update(packet());
  assert.equal(controller.input(input(`${CSI}1;3B`, 'altDown'), false).consumed, true);
});

test('renderer emits only differences and never hides cursor per background tick', () => {
  const output = [], renderer = new hud.DiffRenderer(data => output.push(data));
  const cursor = { x: 2, y: 3, visible: true };
  assert.equal(renderer.render(['CLI', 'HUD'], cursor), true);
  assert.equal(renderer.render(['CLI', 'HUD'], cursor), false);
  renderer.render(['CLI', 'HUD update'], cursor);
  assert.equal(output.length, 2);
  assert.ok(!output[1].includes('CLI'));
  assert.ok(!output[1].includes(`${CSI}?25l`));
  assert.ok(output[1].startsWith(`${CSI}?2026h`));
  assert.ok(output[1].endsWith(`${CSI}?2026l`));
});

test('VT synchronized frames spanning data events paint only the complete frame', async () => {
  let paints = 0;
  const screen = new hud.VirtualScreen(Terminal, 80, 24, () => paints++);
  try {
    screen.write(`${CSI}?2026h${CSI}?25l${CSI}1;1Hpartial`);
    await until(() => !screen.pendingWrites, 'first VT fragment parsed');
    assert.equal(paints, 0);
    screen.write(`${CSI}1;1Hcomplete${CSI}?25h${CSI}?2026l`);
    await until(() => paints === 1, 'complete VT frame painted');
    assert.equal(screen.cursorVisible, true);
    assert.ok(screen.lines()[0].includes('complete'));
  } finally { screen.dispose(); }
});

test('parsed callbacks observe an empty composer only after the write queue drains', async () => {
  const screen = new hud.VirtualScreen(Terminal, 80, 24);
  const observed = [];
  try {
    screen.write(prompt, () => observed.push(screen.emptyComposer()));
    screen.write(prompt, () => observed.push(screen.emptyComposer()));
    await until(() => observed.length === 2, 'queued frames parsed');
    assert.deepEqual(observed, [false, true]);
  } finally { screen.dispose(); }
});

test('configuration menu exposes the focus preset on native terminals', () => {
  const requests = [], controller = new hud.HudController(message => requests.push(message));
  controller.update(packet());
  controller.input(input('', 'config'), false);
  for (let i = 0; i < 4; i++) controller.input(input('', 'down'), false);
  controller.input(input('\r', 'enter'), false);
  assert.equal(controller.configIndex, 3);
  assert.equal(requests.at(-1).changes.preset, 'focus');
});

test('pending draft requires a composer fingerprint change; Working output cannot clear it', async () => {
  const screen = new hud.VirtualScreen(Terminal, 80, 24);
  const guard = new hud.ComposerGuard(screen);
  try {
    await write(screen.terminal, prompt);
    assert.equal(guard.empty(), true);
    guard.forwarded(input('x', 'other'));
    assert.equal(guard.empty(), false);
    await write(screen.terminal, `${CSI}1;1HWorking…${CSI}10;3H`);
    guard.observe(); assert.equal(guard.empty(), false, 'background output does not acknowledge draft');
    await write(screen.terminal, `${CSI}10;1H${CSI}2K› x${CSI}10;4H`);
    guard.observe(); assert.equal(guard.pending, null); assert.equal(guard.empty(), false);
    await write(screen.terminal, prompt);
    guard.forwarded(input(`${CSI}B`, 'down')); assert.equal(guard.empty(), true, 'no-op arrows never strand the composer guard');
    guard.forwarded(input('\x7f', 'other')); guard.forwarded(input('\r', 'enter'));
    guard.forwarded(input(`${CSI}200~`, 'paste')); guard.forwarded(input(`${CSI}201~`, 'paste'));
    assert.equal(guard.empty(), true, 'empty backspace, Enter and paste are no-ops');
  } finally { screen.dispose(); }
});

test('external termination restores terminal and stops only owned PTY and bridge', { timeout: 25000 }, async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-hud-native-signal-'));
  const runtime = path.join(base, 'runtime'), bridgePidFile = path.join(base, 'bridge.pid');
  fs.mkdirSync(runtime, { mode: 0o700 });
  const fakeCli = path.join(base, 'cli.cjs'), fakeBridge = path.join(base, 'bridge.cjs');
  fs.writeFileSync(fakeCli, 'process.stdin.resume();setInterval(()=>process.stdout.write("running\\r"),100);');
  fs.writeFileSync(fakeBridge, `require('node:fs').writeFileSync(${JSON.stringify(bridgePidFile)},String(process.pid));setInterval(()=>{},1000);`);
  fs.writeFileSync(path.join(runtime, 'spec.json'), JSON.stringify({ runtime, home: base, cwd: base, argv: [process.execPath, fakeCli],
    bridge_argv: [process.execPath, fakeBridge], hud_height: 6 }));
  const stdin = Object.assign(new EventEmitter(), { isTTY: true, isRaw: false, setEncoding() {}, resume() {}, pause() {}, setRawMode(value) { this.isRaw = value; } });
  let output = '';
  const stdout = Object.assign(new EventEmitter(), { isTTY: true, columns: 80, rows: 24, write(value) { output += value; } });
  let running, failure;
  try {
    running = hud.run(runtime, { stdin, stdout });
    await until(() => fs.existsSync(bridgePidFile) && fs.existsSync(path.join(runtime, 'runner.json')) && JSON.parse(fs.readFileSync(path.join(runtime, 'runner.json'), 'utf8')).pid > 0, 'owned children started with real ConPTY PID');
    const bridgePid = Number(fs.readFileSync(bridgePidFile, 'utf8'));
    const childPid = JSON.parse(fs.readFileSync(path.join(runtime, 'runner.json'), 'utf8')).pid;
    process.emit('SIGTERM');
    assert.equal(await running, 143);
    assert.equal(stdin.isRaw, false); assert.equal(fs.existsSync(runtime), false);
    await until(() => [bridgePid, childPid].every(pid => { try { process.kill(pid, 0); return false; } catch { return true; } }), 'owned children stopped');
    assert.ok(output.endsWith(`${CSI}?1049l`));
  } catch (error) {
    failure = error; throw diagnose(error, runtime, { output: output.slice(-6000) });
  } finally {
    if (fs.existsSync(runtime)) { process.emit('SIGTERM'); if (running) await running; }
    await removeFixture(base, failure);
  }
});

test('child environment is isolated from launcher session and preserves inherited variables', () => {
  const env = hud.childEnvironment('own-runtime', { CODEX_THREAD_ID: 'old-thread', CODEX_SESSION_ID: 'old-session', PATH: 'native-path', RUST_LOG: 'debug' });
  assert.equal(env.CODEX_THREAD_ID, undefined); assert.equal(env.CODEX_SESSION_ID, undefined);
  assert.equal(env.PATH, 'native-path'); assert.equal(env.CODEX_HUD_ACTIVE, '1');
  assert.equal(env.RUST_LOG, 'warn,codex_core::session=info');
});

test('dependency prefix has priority and settings.native_node resolves the installation', () => {
  const config = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-hud-node-config-'));
  const prefix = path.resolve(__dirname, '../native');
  try {
    fs.writeFileSync(path.join(config, 'settings.json'), '{broken');
    assert.equal(hud.loadDependencies({ CODEX_HUD_NODE_HOME: prefix, CODEX_HUD_CONFIG_HOME: config }).Terminal, Terminal);
    fs.writeFileSync(path.join(config, 'settings.json'), JSON.stringify({ native_node: prefix }));
    assert.equal(hud.loadDependencies({ CODEX_HUD_CONFIG_HOME: config }).Terminal, Terminal);
  } finally { fs.rmSync(config, { recursive: true, force: true }); }
});

test('real native PTY: selection, draft passthrough, live detail, root change, resize and /quit cleanup', { timeout: 45000 }, async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-hud-native-test-'));
  const runtime = path.join(base, 'runtime'), inputLog = path.join(base, 'input.jsonl'), bridgeLog = path.join(base, 'bridge.jsonl');
  fs.mkdirSync(runtime, { mode: 0o700 });
  const fakeCli = path.join(base, 'cli.cjs'), fakeBridge = path.join(base, 'bridge.cjs');
  fs.writeFileSync(fakeCli, `
const fs = require('node:fs');
const log = value => fs.appendFileSync(${JSON.stringify(inputLog)}, JSON.stringify(value) + '\\n');
log({env:{thread:process.env.CODEX_THREAD_ID,session:process.env.CODEX_SESSION_ID,active:process.env.CODEX_HUD_ACTIVE,log:process.env.RUST_LOG},pid:process.pid});
process.stdin.setRawMode(true); process.stdin.setEncoding('utf8'); let draft='';
function draw(){ process.stdout.write('\\x1b[?2026h\\x1b[2J\\x1b[10;1H› '+(draft || '\\x1b[2mAsk Codex to do anything\\x1b[0m')+'\\x1b[10;'+(3+draft.length)+'H\\x1b[?25h\\x1b[?2026l'); }
draw(); process.stdout.on('resize',()=>{ log({resize:[process.stdout.columns,process.stdout.rows]}); draw(); });
process.stdin.on('data',data=>{ log({input:data}); const text=data.replace(/\\x1b(?:\\[[0-?]*[ -/]*[@-~]|O[@-~])/g,''); for(const char of text){ if(char==='\\x7f') draft=draft.slice(0,-1); else if(char==='\\r'){ if(draft==='/quit') process.exit(0); draft=''; } else if(char>=' ')draft+=char; } draw(); });
`);
  fs.writeFileSync(fakeBridge, `
const fs=require('node:fs'); const readline=require('node:readline'); let tick=0;
const log=value=>fs.appendFileSync(${JSON.stringify(bridgeLog)},JSON.stringify(value)+'\\n'); log({pid:process.pid});
const emit=value=>process.stdout.write(JSON.stringify(value)+'\\n');
function update(){ const hidden=fs.existsSync(${JSON.stringify(path.join(base, 'hide'))}); const switched=fs.existsSync(${JSON.stringify(path.join(base, 'switch'))}); const skills=fs.existsSync(${JSON.stringify(path.join(base, 'skills'))}); const rows=hidden?[]:skills?[{spans:[['Skills demo','agent']],key:['skills','skill-turn']}]:${JSON.stringify(packet().rows)}; emit({type:'snapshot',snapshot:{root:{id:switched?'root-b':'root-a',skills:skills?['demo']:[],skill_turn_id:'skill-turn'},agents:[{id:'a',status:'running',turn_id:'test-turn',started:1}],display:{height:6,language:'en'}},rows,all_rows:rows}); }
readline.createInterface({input:process.stdin}).on('line',line=>{ const req=JSON.parse(line); log(req); if(req.type==='detail')emit({...req,lines:[req.key[0]==='skills'?'Skill source /fixture/SKILL.md':'Live agent output '+(++tick)]}); if(req.type==='action')emit({...req,draft:'HUD request for '+req.key[1]}); });
setTimeout(update,150); setInterval(update,150);
`);
  fs.writeFileSync(path.join(runtime, 'spec.json'), JSON.stringify({ runtime, home: base, cwd: base, argv: [process.execPath, fakeCli],
    bridge_argv: [process.execPath, fakeBridge], hud_height: 6, diagnostic: path.join(runtime, 'diagnostic.log') }));
  const outer = pty.spawn(process.execPath, [path.resolve(__dirname, '../native/terminal.cjs'), runtime], {
    name: 'xterm-256color', cols: 80, rows: 24, cwd: base,
    env: { ...process.env, CODEX_THREAD_ID: 'launcher-thread', CODEX_SESSION_ID: 'launcher-session', CODEX_HUD_NATIVE_DEBUG: '1' },
    ...(process.platform === 'win32' ? {} : { encoding: 'utf8' }), conptyInheritCursor: false
  });
  const terminal = new Terminal({ cols: 80, rows: 24, allowProposedApi: true });
  let exited = false, exitCode, rendered = '', replies = '', failure, lastOutput = Date.now();
  outer.onData(data => { lastOutput = Date.now(); rendered += data; terminal.write(data); });
  terminal.onData(data => { replies += data; outer.write(data); });
  outer.onExit(event => { exited = true; exitCode = event.exitCode; });
  const visible = () => Array.from({ length: terminal.rows }, (_, y) => terminal.buffer.active.getLine(terminal.buffer.active.viewportY + y)?.translateToString(true) || '').join('\n');
  const inputs = () => fs.existsSync(inputLog) ? fs.readFileSync(inputLog, 'utf8').trim().split('\n').map(JSON.parse).filter(entry => entry.input).map(entry => entry.input).join('') : '';
  const requests = () => fs.existsSync(bridgeLog) ? fs.readFileSync(bridgeLog, 'utf8').trim().split('\n').map(JSON.parse) : [];
  const selected = () => {
    const buffer = terminal.buffer.active;
    for (let y = 0; y < terminal.rows; y++) {
      const line = buffer.getLine(buffer.viewportY + y);
      if (line?.translateToString(true).includes('agent Alpha') && line.getCell(0).isInverse()) return true;
    }
    return false;
  };
  try {
    await until(() => visible().includes('agent Alpha') && visible().includes('› Ask Codex'), 'native HUD and official composer visible');
    assert.ok(fs.existsSync(path.join(runtime, 'runner.json')));
    const envRecord = JSON.parse(fs.readFileSync(inputLog, 'utf8').split('\n')[0]);
    assert.equal(envRecord.env.thread, undefined); assert.equal(envRecord.env.session, undefined);
    assert.equal(envRecord.env.active, '1'); assert.equal(envRecord.env.log, 'warn,codex_core::session=info');
    // ConPTY can show the old composer while the HUD's initial resize is
    // still reaching the CLI. Press only after that redraw has settled.
    await until(() => visible().includes('› Ask Codex') && Date.now() - lastOutput >= 100, 'resized empty composer settled');
    outer.write(`${CSI}B`);
    await until(selected, 'Down selected agent in the bottom HUD');
    assert.ok(!inputs().includes(`${CSI}B`), 'empty Down was not delivered to official CLI');
    outer.write('\r');
    await until(() => visible().includes('Live agent output'), 'Enter opened real bridge detail');
    await until(() => requests().filter(req => req.type === 'detail').length >= 2, 'detail refreshed from live snapshots');
    outer.write('s'); await until(() => visible().includes('prepare draft'), 'stop request confirmation visible');
    outer.write('\x1b'); await until(() => visible().includes('Live agent output'), 'cancel returns to task details');
    assert.equal(requests().filter(req => req.type === 'action').length, 0);
    outer.write('s'); await until(() => visible().includes('prepare draft'), 'confirm view reopened');
    const inputBefore = inputs().length;
    outer.write('\r'); await until(() => visible().includes('› HUD request for a'), 'request staged in native composer');
    assert.equal(inputs().slice(inputBefore), `${CSI}200~HUD request for a${CSI}201~`, 'draft paste has no submit key');
    outer.write('\x7f'.repeat('HUD request for a'.length)); await until(() => visible().includes('› Ask Codex'), 'request draft erased');
    fs.writeFileSync(path.join(base, 'skills'), '1'); await until(() => visible().includes('Skills demo'), 'skill row shown');
    outer.write(`${CSI}B\r`); await until(() => visible().includes('Skill source /fixture/SKILL.md'), 'skill details work through real PTY');
    outer.write('\x1b'); await wait(100); outer.write('\x1b'); await wait(100);
    fs.unlinkSync(path.join(base, 'skills')); await until(() => visible().includes('agent Alpha'), 'agent rows restored');
    outer.write(`${CSI}B\r`); await until(() => visible().includes('Live agent output'), 'agent details reopened');
    outer.write('\x1b'); await until(selected, 'first Esc returned to HUD selection');
    outer.write('\x1b'); await wait(100);
    outer.write(`z${CSI}B`);
    await until(() => inputs().includes(`z${CSI}B`) && visible().includes('› z'), 'same-chunk draft plus Down passed through');
    assert.equal(selected(), false, 'pending native edit cannot select HUD using stale empty screen');
    outer.write('\x7f'); await until(() => visible().includes('› Ask Codex'), 'rapid draft erased');
    outer.write('x'); await until(() => visible().includes('› x'), 'native draft preserved');
    outer.write(`${CSI}B`); await until(() => inputs().includes(`${CSI}B`), 'draft Down passed to official CLI');
    outer.write('\x7f'); await until(() => visible().includes('› Ask Codex'), 'backspace editing restored empty composer');
    outer.write(`${CSI}B`); await until(selected, 'agent selected again');
    fs.writeFileSync(path.join(base, 'switch'), '1'); await until(() => !selected(), 'root switch cleared HUD selection');
    outer.resize(90, 28); terminal.resize(90, 28);
    await until(() => requests().some(req => req.type === 'size' && req.cols === 90 && req.rows === 28), 'resize sent to bridge');
    fs.writeFileSync(path.join(base, 'hide'), '1'); await until(() => !visible().includes('agent Alpha'), 'empty HUD hid itself');
    await until(() => fs.readFileSync(inputLog, 'utf8').includes('90,28'), 'CLI reclaimed all rows');
    const bridgePid = requests().find(entry => entry.pid).pid;
    outer.write('/quit\r'); await until(() => exited, '/quit exited native UI', 10000);
    assert.equal(exitCode, 0); assert.equal(fs.existsSync(runtime), false, 'owned runtime removed');
    await until(() => { try { process.kill(bridgePid, 0); return false; } catch { return true; } }, 'bridge process stopped');
    assert.ok(rendered.includes(`${CSI}?1049l`), 'outer terminal mode restored');
  } catch (error) {
    failure = error; throw diagnose(error, runtime, { outerPid: outer.pid, exited, exitCode, replies,
      visible: visible(), vt: rendered.slice(-6000), input: readFixture(inputLog), bridge: readFixture(bridgeLog) });
  } finally {
    await stopFixture(outer, () => exited, runtime, failure);
    terminal.dispose(); await removeFixture(base, failure);
  }
});

test('real PTY and Python bridge read SQLite-correlated Chinese agent output without model requests', { timeout: 45000 }, async () => {
  const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const repository = path.resolve(__dirname, '..');
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-hud-python-'));
  const work = path.join(base, '中文目录'), runtime = path.join(work, '原生运行'), home = path.join(work, '私有Codex');
  const config = path.join(work, '私有配置'), diagnostic = path.join(runtime, '诊断.log');
  const childLog = path.join(home, 'sessions', '子会话.jsonl');
  const nickname = '中文核验助手', answer = '实际子会话返回：中文路径和真实 bridge 已核验成功。';
  const root = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', child = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  fs.mkdirSync(runtime, { recursive: true, mode: 0o700 });
  fs.mkdirSync(config, { recursive: true, mode: 0o700 });
  // This transcript-inspection fixture intentionally opts into completed history.
  fs.writeFileSync(path.join(config, 'display.json'), JSON.stringify({ agents_show_completed: true }));
  const environment = { ...process.env, CODEX_HOME: home, CODEX_HUD_CONFIG_HOME: config,
    CODEX_HUD_NODE_HOME: path.join(repository, 'native'), CODEX_X_CODEX_HOME: path.join(work, '私有分流'),
    PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', CODEX_HUD_NATIVE_DEBUG: '1' };
  const setup = spawnSync(python, ['-c', `
import json, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path
home, work, diagnostic = map(Path, sys.argv[1:4])
root, child, nickname, answer = sys.argv[4:8]
(home/'sessions').mkdir(parents=True)
stamp = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
paths = {root: home/'sessions'/'根会话.jsonl', child: home/'sessions'/'子会话.jsonl'}
for identity, log in paths.items():
 records = [{'type':'session_meta','timestamp':stamp,'payload':{'id':identity,'cwd':str(work),'timestamp':stamp}},
            {'type':'turn_context','payload':{'model':'fixture-model','cwd':str(work)}}]
 if identity == child:
  records += [{'type':'response_item','timestamp':stamp,'payload':{'type':'agent_message','content':[{'type':'input_text','text':'Message Type: NEW_TASK\\nPayload:\\n核验中文路径'}]}},
              {'type':'response_item','timestamp':stamp,'payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':answer}]}},
              {'type':'event_msg','timestamp':stamp,'payload':{'type':'task_complete','duration_ms':250}}]
 log.write_text(''.join(json.dumps(record,ensure_ascii=False)+'\\n' for record in records),encoding='utf-8')
state=sqlite3.connect(home/'state_5.sqlite')
state.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,model TEXT,agent_path TEXT,agent_nickname TEXT,title TEXT,first_user_message TEXT,source TEXT);CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);')
for identity, log in paths.items():
 state.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)',(identity,str(log),str(work),'fixture-model','/root/核验' if identity==child else '/root',nickname if identity==child else None,'','', 'cli' if identity==root else 'subagent'))
state.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)',(root,child,'closed'))
state.commit();state.close()
diagnostic.write_text(f'{stamp} INFO thread_spawn{{thread_id={root}}}:session_init:session_init.thread_persistence{{session_init.ephemeral=false}}: codex_core::thread_manager: new\\n{stamp} INFO session_loop{{thread_id={root}}}: codex_core::session: new\\n',encoding='utf-8')
`, home, work, diagnostic, root, child, nickname, answer], { env: environment, cwd: work, encoding: 'utf8' });
  if (setup.error) { fs.rmSync(base, { recursive: true, force: true }); throw setup.error; }
  assert.equal(setup.status, 0, `Python fixture creation failed: ${setup.stderr}`);
  const beforeLog = fs.readFileSync(childLog);
  const fakeCli = path.join(work, '官方界面替身.cjs');
  fs.writeFileSync(fakeCli, `
process.stdin.setRawMode(true);process.stdin.setEncoding('utf8');let draft='';
function draw(){process.stdout.write('\\x1b[?2026h\\x1b[2J\\x1b[Hfixture-ready '+process.stdout.columns+'x'+process.stdout.rows+'\\x1b[3;1HWorking (4m 51s)\\x1b[10;1H› '+(draft||'\\x1b[2mAsk Codex to do anything\\x1b[0m')+'\\x1b[10;'+(3+draft.length)+'H\\x1b[?25h\\x1b[?2026l');}
draw();process.stdout.on('resize',draw);
process.stdin.on('data',data=>{const text=data.replace(/\\x1b(?:\\[[0-?]*[ -/]*[@-~]|O[@-~])/g,'');for(const char of text){if(char==='\\r'){if(draft==='/quit')process.exit(0);draft='';}else if(char==='\\x7f')draft=draft.slice(0,-1);else if(char>=' ')draft+=char;}draw();});
`);
  fs.writeFileSync(path.join(runtime, 'spec.json'), JSON.stringify({ runtime, home, cwd: work, argv: [process.execPath, fakeCli],
    bridge_argv: [python, path.join(repository, 'codex-hud'), '_native_bridge', runtime], hud_height: 6, diagnostic }));
  const outer = pty.spawn(process.execPath, [path.join(repository, 'native/terminal.cjs'), runtime], {
    name: 'xterm-256color', cols: 90, rows: 28, cwd: work, env: environment,
    ...(process.platform === 'win32' ? {} : { encoding: 'utf8' }), conptyInheritCursor: false
  });
  const terminal = new Terminal({ cols: 90, rows: 28, allowProposedApi: true });
  let exited = false, exitCode, output = '', replies = '', failure, pendingOuterWrites = 0;
  outer.onData(data => { pendingOuterWrites++; output += data; terminal.write(data, () => { pendingOuterWrites--; }); });
  terminal.onData(data => { replies += data; outer.write(data); });
  outer.onExit(event => { exited = true; exitCode = event.exitCode; });
  const visible = () => Array.from({ length: terminal.rows }, (_, y) => terminal.buffer.active.getLine(terminal.buffer.active.viewportY + y)?.translateToString(true) || '').join('\n');
  const selected = () => {
    const buffer = terminal.buffer.active;
    for (let y = 0; y < terminal.rows; y++) {
      const line = buffer.getLine(buffer.viewportY + y);
      if (line?.translateToString(true).includes(nickname) && line.getCell(0).isInverse()) return true;
    }
    return false;
  };
  try {
    await until(() => visible().includes(nickname) && visible().includes('› Ask Codex'), 'actual Python snapshot and Chinese agent visible');
    const runner = JSON.parse(fs.readFileSync(path.join(runtime, 'runner.json'), 'utf8'));
    const snapshot = JSON.parse(fs.readFileSync(path.join(runtime, 'snapshot.json'), 'utf8'));
    assert.equal(snapshot.root.id, root); assert.equal(snapshot.agents[0].id, child);
    assert.equal(snapshot.agents[0].name, nickname); assert.ok(runner.bridge_pid > 0);
    // A quiet old frame can precede ConPTY's delayed resize event. Require
    // the child to acknowledge its new height in a fully parsed frame.
    const outerComposerReady = () => {
      const buffer = terminal.buffer.active, line = buffer.getLine(buffer.baseY + buffer.cursorY);
      // The outer renderer pads rows with literal spaces; trim that display
      // padding while retaining the dim-text and cursor guards.
      return buffer.cursorX === 2 && buffer.cursorY === 9 && line?.translateToString(true).trimEnd() === '› Ask Codex to do anything' &&
        Array.from({length: 23}, (_, i) => line.getCell(i+2)?.isDim()).every(Boolean);
    };
    await until(() => visible().includes('fixture-ready 90x25') && pendingOuterWrites === 0 &&
      !terminal.modes.synchronizedOutputMode && outerComposerReady(), 'real bridge resized composer acknowledged');
    outer.write(`${CSI}B`); await until(selected, 'plain Down selected actual correlated agent');
    outer.write('\r'); await until(() => visible().includes(answer), 'real Python details returned actual child answer');
    assert.deepEqual(fs.readFileSync(childLog), beforeLog, 'viewing details never changes or resumes the child session');
    outer.write('\x1b'); await until(selected, 'first Esc returned to actual HUD');
    outer.write('\x1b'); await wait(100);
    await until(() => visible().includes('Working (4m 51s)') && !selected(), 'returned to the frozen official fixture');
    const history = spawnSync(python, ['-c', `
import sqlite3,sys,time
from pathlib import Path
db=sqlite3.connect(Path(sys.argv[1])/'thread_history_1.sqlite')
db.executescript('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,started_at INTEGER,completed_at INTEGER,duration_ms INTEGER);CREATE TABLE thread_items(thread_id TEXT,item_type TEXT,item_json TEXT,rollout_ordinal INTEGER,updated_at_ordinal INTEGER);')
db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)',(sys.argv[2],'clock-turn',1,'inProgress',int(time.time())+1,None,None))
db.commit();db.close()
`, home, root], { env: environment, encoding: 'utf8' });
    assert.equal(history.status, 0, history.stderr);
    await until(() => /本轮 \d+s/u.test(visible()), 'independent turn clock became visible');
    const firstSeconds = Number(visible().match(/本轮 (\d+)s/u)[1]);
    const dataEvents = () => (readFixture(path.join(runtime, 'native.log')).match(/cli data bytes=/g) || []).length;
    const beforeEvents = dataEvents();
    await until(() => Number(visible().match(/本轮 (\d+)s/u)?.[1] ?? -1) >= firstSeconds + 2, 'HUD clock ticked without official redraw or key input');
    assert.ok(visible().includes('Working (4m 51s)'), 'official fixture remained frozen');
    assert.equal(dataEvents(), beforeEvents, 'no CLI bytes were required for HUD clock updates');
    outer.write('saved-draft');
    await until(() => visible().includes('› saved-draft'), 'draft exists before configuration');
    outer.write(`${CSI}19~`);
    await until(() => visible().includes('Codex HUD · configuration'), 'F8 opened configuration');
    outer.write(`${CSI}B${CSI}B${CSI}B`);
    await until(() => visible().includes('› focus'), 'focus selected');
    outer.write('\r');
    await until(() => !visible().includes('Codex HUD · configuration') && visible().includes('› saved-draft'), 'successful save closed menu and preserved draft');
    const saved = JSON.parse(fs.readFileSync(path.join(config, 'display.json'), 'utf8'));
    assert.equal(saved.layout, 'compact'); assert.equal(saved.cache_style, 'text');
    assert.equal(saved.show.model, false); assert.equal(saved.show.agents, true);
    assert.deepEqual(saved.merge_groups, [['context', 'quota'], ['tokens', 'cache'], ['header', 'skills']]);
    const afterSave = JSON.parse(fs.readFileSync(path.join(runtime, 'runner.json'), 'utf8'));
    assert.equal(afterSave.pid, runner.pid); assert.ok(hud.processAlive(runner.pid));
    assert.equal(selected(), false);
    outer.write('-continued');
    await until(() => visible().includes('› saved-draft-continued'), 'same CLI continues editing preserved draft');
    outer.write('\x7f'.repeat('saved-draft-continued'.length));
    await until(() => visible().includes('› Ask Codex'), 'fixture draft cleared before quit');
    outer.write('/quit\r'); await until(() => exited, 'quit closed real bridge integration', 10000);
    assert.equal(exitCode, 0); assert.equal(fs.existsSync(runtime), false);
    await until(() => [runner.pid, runner.bridge_pid].every(pid => { try { process.kill(pid, 0); return false; } catch { return true; } }), 'official fixture and Python bridge stopped');
    assert.deepEqual(fs.readFileSync(childLog), beforeLog);
    assert.ok(output.includes(`${CSI}?1049l`));
  } catch (error) {
    const buffer = terminal.buffer.active, line = buffer.getLine(buffer.baseY + buffer.cursorY);
    failure = error; throw diagnose(error, runtime, { outerPid: outer.pid, exited, exitCode, replies, visible: visible(), vt: output.slice(-6000),
      readyState: { pendingOuterWrites, x: buffer.cursorX, y: buffer.cursorY, text: line?.translateToString(true), dim: Array.from({length: 23}, (_, i) => line?.getCell(i+2)?.isDim()), sync: terminal.modes.synchronizedOutputMode } });
  } finally {
    await stopFixture(outer, () => exited, runtime, failure);
    terminal.dispose(); await removeFixture(base, failure);
  }
});
