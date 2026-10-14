---
name: hud
description: Configure Codex HUD display items, quotas, layouts, colors and presets with live updates, or manage its automatic launcher. Use when the user asks to configure, customize, show, hide or manage this HUD.
---

# Codex HUD

Use this plugin's deterministic setup command. The plugin root is two levels above this SKILL.md. Do not assume a developer's checkout path or change the official CLI binary.

Commands below use `python3` on macOS/Linux. On native Windows use `python` (or `py -3`); Python 3.11+ and Node.js 20+ with npm are required. Setup installs the pinned ConPTY dependencies. Do not install tmux, curses, lsof or WSL for native Windows.

## Display configuration

Run `python3 <plugin-root>/codex-hud config --describe` to inspect the effective preferences, valid display items, and unavailable data. For an explicit request, apply only the requested keys without asking for another confirmation. Preserve unrelated settings and manual overrides. Configuration is read every second; an active HUD does not need a new Codex session.

- Show/hide items: `config --show quota_5h,quota_weekly --hide tokens,cache`.
- Presets: `config --preset minimal`, `standard`, `full`, or `focus`. Focus keeps context, quotas, token usage, a compact cache percentage, skills and activity; it hides the model/effort/path repeated by the native CLI. Preserve unrelated colors, thresholds and manual overrides.
- Skills: `config --show skills` displays the SKILL.md names successfully read by the main thread in its current or most recent turn. It does not show installed/discoverable catalog entries as loaded or claim a skill is continuously executing. Names reset on the next turn; child and relay reads do not appear in the main list. Missing, encrypted or unsupported read events remain unavailable. `skills_limit` controls name overflow, with 0 showing all.
- Layout/language: `config --layout compact --language zh-Hans` (also expanded, en, zh-Hant).
- Advanced values: `config --set 'git.ahead_behind=true' --set 'reset_format="both"' --set 'height=8'`.
- Completed and failed native agents and relay jobs are hidden from bottom rows and counts by default. `config --set 'agents_show_completed=true'` shows completed history; `config --set 'agents_show_failed=true'` shows failed history. False hides the corresponding history again. Unknown states remain visible. An open detail view may finish reading the final answer even after its row leaves the HUD. Only display filtering changes; failure records, telemetry and logs remain available to the parent conversation and explicit inspection.
- Other keys, colors, ordering, merge groups and thresholds are documented by `config --describe` and the README. Pass values as properly quoted JSON; never execute user text as a shell command.
- The terminal menu opens with **Ctrl-b C**: release Ctrl-b, then press uppercase C. On macOS, arrows select an item; Space toggles it; 1/2/3/4 select minimal/standard/full/focus; L changes layout; G changes language. On Windows, arrows select a preset and Enter applies it; F8 also opens the menu. Esc closes it. Use this skill for all other Windows display changes.
- Inspect agents by pressing **Down from the default empty composer**. It selects the first visible agent row inline in the HUD; Up/Down selects, Enter opens live task/status/usage/tool/output details, Esc returns to HUD and then main input. Up from the first row also returns to input. Drafts and native menus keep their official arrow behavior. Alternative: macOS **Option (⌥)+Down**, Windows **Alt+Down**, or Ctrl-b followed by Down on either platform. Alternatives open a list on macOS and select HUD rows on Windows. This viewer only reads the current snapshot and identified session logs. It never resumes, interrupts or sends a message to an agent. Encrypted or missing output stays unavailable. For native thread switching or continuing a native agent, use Codex's `/subagents` command.
- If the user invokes general customization without specifying preferences, briefly list the current active items and ask which to change, or explain the menu. Do not arbitrarily overwrite a preset.

Preferences live in `~/.config/codex-hud/display.json`. Optional `override.json` in the same directory wins; never modify it on a routine configuration request. The configuration command reports whether that file is present; explain any override that prevents the requested effective change. Unknown existing settings survive updates.

After a plugin update, an active HUD may still run the old code. `python3 <plugin-root>/codex-hud refresh --session <current-thread-id>` replaces only its observer and bindings. Check the returned runtime and retained CLI PID; do not restart or stop the main conversation.

That code-refresh command applies to macOS tmux HUDs. Native Windows backend upgrades take effect on the next normal launch. Display preferences refresh live on both platforms. Windows supports `watch --session <ID> --once` for read-only data verification, without a curses viewer.

Quotas come from official current-thread rate-limit snapshots, not estimates from tokens. The 5-hour or weekly row stays hidden when its window is absent. Main-thread usage never adds native agent or relay usage. Relay failures without a returned usage record show unavailable, not zero. Cost totals, API tok/s, cache expiry, and continuous Skill execution state lack reliable Codex telemetry; read skill instructions are shown separately, without inventing those values or reading credentials.

## Automatic launch integration

- Initial setup, enabling automatic display, or invoking this skill without another action: `python3 <plugin-root>/codex-hud setup --enable`.
- Disable automatic display: `python3 <plugin-root>/codex-hud setup --disable`.
- Change maximum panel height without reinstalling: `config --set 'height=8'`; valid limits are 1–30. The panel shrinks to fit content and stays hidden when it has no data.
- Read current settings: `python3 <plugin-root>/codex-hud setup --status`.
- Remove only this plugin's launcher integration when requested: `python3 <plugin-root>/codex-hud setup --uninstall`. Native plugin removal is managed by `/plugins`.

Setup creates a transparent `codex` launcher in the user's local bin directory and adds one marked PATH block to the appropriate shell rc. For an owned, writable symlink verified as the official npm package's `bin.codex`, setup also bridges that existing entry to the launcher, preserving its original link for uninstall. Official package files, existing shell functions, proxy settings, authentication and CLI configuration are preserved. A pre-existing unrelated launcher is never overwritten. Read the returned `entrypoint_bridge` and effect; do not claim installation succeeded from a manifest alone.

On Windows, setup creates a `codex.cmd` shim and marked PATH blocks in available PowerShell profiles. Read its returned `profile` / `profiles` and `native_node` paths. A new PowerShell / Windows Terminal tab loads these once; ordinary subsequent `codex` launches need no commands. Windows npm entry files and execution policy are preserved. Non-interactive passthrough waits for the official executable and returns its exit code.

After one-time setup, ordinary interactive `codex`, `codex resume` and `codex fork` automatically open with the fixed HUD. Non-interactive commands, redirected output, help/version queries and remote sessions use the original CLI. The CLI's approvals and network policy still apply. A bridged entry works even in old shells with cached paths: users do not need `rehash`. If a package update recreates its bin link, rerun setup to bridge the updated entry.

When no compatible entry could be bridged, a new terminal tab loads the PATH integration. Do not ask the user to run a tmux attach command on every launch. An existing CLI outside the HUD backend must be reentered once; a bridged macOS entry requires no extra shell command. On macOS, `Ctrl-b H` expands details. Model and effort follow the current thread selection on each one-second refresh; global default configuration must not override an independently selected session model.

Agent counts come from session relations and turn events. Unknown usage stays unknown. Encrypted task instructions are represented by their recorded task names. Do not read credentials or route HUD configuration through relay workers.
