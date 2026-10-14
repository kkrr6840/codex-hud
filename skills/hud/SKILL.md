---
name: hud
description: Configure Codex HUD display items, quotas, layouts, colors and presets with live updates, or manage its automatic launcher. Use when the user asks to configure, customize, show, hide or manage this HUD.
---

# Codex HUD

Use this plugin's deterministic setup command. The plugin root is two levels above this SKILL.md. Do not assume a developer's checkout path or change the official CLI binary.

## Display configuration

Run `python3 <plugin-root>/codex-hud config --describe` to inspect the effective preferences, valid display items, and unavailable data. For an explicit request, apply only the requested keys without asking for another confirmation. Preserve unrelated settings and manual overrides. Configuration is read every second; an active HUD does not need a new Codex session.

- Show/hide items: `config --show quota_5h,quota_weekly --hide tokens,cache`.
- Presets: `config --preset minimal`, `standard`, or `full`.
- Layout/language: `config --layout compact --language zh-Hans` (also expanded, en, zh-Hant).
- Advanced values: `config --set 'git.ahead_behind=true' --set 'reset_format="both"' --set 'height=8'`.
- Other keys, colors, ordering, merge groups and thresholds are documented by `config --describe` and the README. Pass values as properly quoted JSON; never execute user text as a shell command.
- The native terminal menu opens with **Ctrl-b C**: release Ctrl-b, then press uppercase C. Arrows select an item; Space toggles it; 1/2/3 select presets; L changes layout; G changes language; Esc closes it. Changes save immediately.
- If the user invokes general customization without specifying preferences, briefly list the current active items and ask which to change, or explain the menu. Do not arbitrarily overwrite a preset.

Preferences live in `~/.config/codex-hud/display.json`. Optional `override.json` in the same directory wins; never modify it on a routine configuration request. The configuration command reports whether that file is present; explain any override that prevents the requested effective change. Unknown existing settings survive updates.

After a plugin update, an active HUD may still run the old code. `python3 <plugin-root>/codex-hud refresh --session <current-thread-id>` replaces only its observer and bindings. Check the returned runtime and retained CLI PID; do not restart or stop the main conversation.

Quotas come from official current-thread rate-limit snapshots, not estimates from tokens. The 5-hour or weekly row stays hidden when its window is absent. Main-thread usage never adds native agent or relay usage. Relay failures without a returned usage record show unavailable, not zero. Cost totals, API tok/s, cache expiry, and active Skill executions lack reliable Codex telemetry; do not invent these values or read credentials to fill gaps.

## Automatic launch integration

- Initial setup, enabling automatic display, or invoking this skill without another action: `python3 <plugin-root>/codex-hud setup --enable`.
- Disable automatic display: `python3 <plugin-root>/codex-hud setup --disable`.
- Change maximum panel height without reinstalling: `config --set 'height=8'`; valid limits are 1–30. The panel shrinks to fit content and stays hidden when it has no data.
- Read current settings: `python3 <plugin-root>/codex-hud setup --status`.
- Remove only this plugin's launcher integration when requested: `python3 <plugin-root>/codex-hud setup --uninstall`. Native plugin removal is managed by `/plugins`.

Setup creates a transparent `codex` launcher in the user's local bin directory and adds one marked PATH block to the appropriate shell rc. For an owned, writable symlink verified as the official npm package's `bin.codex`, setup also bridges that existing entry to the launcher, preserving its original link for uninstall. Official package files, existing shell functions, proxy settings, authentication and CLI configuration are preserved. A pre-existing unrelated launcher is never overwritten. Read the returned `entrypoint_bridge` and effect; do not claim installation succeeded from a manifest alone.

After one-time setup, ordinary interactive `codex`, `codex resume` and `codex fork` automatically open with the fixed HUD. Non-interactive commands, redirected output, help/version queries and remote sessions use the original CLI. The CLI's approvals and network policy still apply. A bridged entry works even in old shells with cached paths: users do not need `rehash`. If a package update recreates its bin link, rerun setup to bridge the updated entry.

When no compatible entry could be bridged, a new terminal tab loads the PATH integration. Do not ask the user to run a tmux attach command on every launch. An existing CLI outside tmux must be reentered to gain a pane; a bridged entry requires no extra shell command. For sessions already launched with HUD, `Ctrl-b H` expands details. Model and effort follow the current thread selection on each one-second refresh; global default configuration must not override an independently selected session model.

Agent counts come from session relations and turn events. Unknown usage stays unknown. Encrypted task instructions are represented by their recorded task names. Do not read credentials or route HUD configuration through relay workers.
