---
name: hud
description: Configure automatic Codex HUD display, enable or disable it, change panel height, check installation, or remove its launcher. Use when the user invokes HUD configuration or asks to manage this Codex HUD plugin.
---

# Codex HUD

Use this plugin's deterministic setup command. The plugin root is two levels above this SKILL.md. Do not assume a developer's checkout path or change the official CLI binary.

- Initial setup, enabling automatic display, or invoking this skill without another action: `python3 <plugin-root>/codex-hud setup --enable`.
- Disable automatic display: `python3 <plugin-root>/codex-hud setup --disable`.
- Change height: add `--height N` to setup; valid heights are 6–30.
- Read current settings: `python3 <plugin-root>/codex-hud setup --status`.
- Remove only this plugin's launcher integration when requested: `python3 <plugin-root>/codex-hud setup --uninstall`. Native plugin removal is managed by `/plugins`.

Setup creates a transparent `codex` launcher in the user's local bin directory and adds one marked PATH block to the appropriate shell rc. Existing shell functions, proxy settings, authentication and CLI configuration are preserved. A pre-existing unrelated launcher is never overwritten. Read the returned result; do not claim installation succeeded from a manifest alone.

After one-time setup, ordinary interactive `codex`, `codex resume` and `codex fork` automatically open with the fixed HUD. Non-interactive commands, redirected output, help/version queries and remote sessions use the original CLI. The CLI's approvals and network policy still apply.

A shell and CLI already running before setup need one reload/new tab to pick up the launcher. Explain this once; do not ask the user to run a tmux attach command on every launch. An existing CLI outside tmux cannot gain a pane in place. For sessions already launched with HUD, `Ctrl-b H` expands details.

Agent counts come from session relations and turn events. Unknown usage stays unknown. Encrypted task instructions are represented by their recorded task names. Do not read credentials or route HUD configuration through relay workers.
