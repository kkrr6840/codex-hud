# Codex HUD

[English](README.en.md) | [简体中文](README.md)

A persistent bottom panel for the official Codex CLI. Configure it once, then keep using `codex`, `codex resume`, or `codex fork` as usual. No separate HUD command or manual tmux attachment is needed on each launch.

![Codex plugin](https://img.shields.io/badge/Codex-plugin-blue) ![License](https://img.shields.io/badge/license-MIT-green)

![HUD preview with example data and Chinese display labels](assets/preview.png)

## Features

- **Live model selection:** the current session's model and reasoning effort refresh about once a second, including changes made before the next request.
- **Context and quotas:** colored progress bars for context usage and available 5-hour, weekly, or other quota windows. Missing data stays hidden.
- **Token usage:** main-thread input, cached input, output, and reasoning tokens, with a separate cache ratio. Agent usage is shown separately.
- **Session activity:** native agents, recent tools, recorded plans, and session-scoped codex-x-codex relay jobs.
- **Read skills:** names of SKILL.md instructions successfully read by the main thread in the current or most recent turn. The list resets on the next turn and excludes child/relay reads. Installed metadata is not counted as loaded instructions; encrypted or unsupported read events remain unavailable.
- **Git and environment:** branch, working-tree changes, optional ahead/behind counts, directory, session information, and other display items.
- **Live customization:** visibility, presets, compact or expanded layout, colors, ordering, and English or Chinese labels. Preferences take effect without restarting an active HUD.
- **Clean startup:** the panel stays hidden until data is available and adjusts its height to the content.

v0.7 adds the **focus** preset: hide model/effort/path already shown by the native CLI, keep context/quota bars and activity, and merge token totals with a compact cache percentage. Ask the hud skill to use focus, choose 4 in the Mac settings menu, or select focus in the Windows menu. Unrelated preferences stay intact. Idle Mac HUD frames no longer clear and repaint repeatedly; scrolling, selection, resize, and color changes still refresh.

v0.7.1 makes the text cache percentage and token/cache merge the defaults, including existing configurations without explicit cache preferences. Explicit `cache_style="bar"`, merge groups, and manual overrides are retained.

v0.7.2 adds an independent running-turn clock that updates each second from the main thread's recorded start time, even when the official CLI stops repainting with animations disabled. It includes tool and approval waits, unlike the official pause-aware clock. Missing start times remain hidden, and the clock leaves the HUD when the turn ends or is interrupted. Hide it with `config --hide turn_duration`. This does not repair or rewrite the official CLI's frozen status row; Windows backend upgrades apply on the next normal Codex launch.

v0.7.4 fixes missing text after full, nearly full, or empty bars in the macOS panel. Quota percentages and reset times remain visible at those boundaries.

v0.7.9 reduces overhead in long sessions and multiple windows. Tool history uses separate indexed ranges for new and updated records instead of repeatedly scanning old items. The macOS panel recomputes layout only after snapshot, size or input changes and batches geometry queries into one tmux call. Preferences and turn clocks still update about once a second; active Mac observers can refresh in place. These changes do not establish a terminal scrolling frame-rate guarantee.

Windows v0.7.1 recognizes the official npm platform-package aliases and records the exact Python interpreter used for setup. The launcher uses that executable directly instead of PATH's `python`, `py`, batch shims, or application aliases. If it becomes unavailable, the launcher warns before falling back to the official CLI. Existing Windows installations must run the hud skill's automatic setup once after upgrading to replace their generated `codex.cmd`.

## Requirements

Supports macOS and native Windows PowerShell / Windows Terminal. Git is required for Git status.

| Platform | Dependencies and backend |
| --- | --- |
| macOS | Python 3.11+, tmux 3.2+, `lsof`; tested on macOS 15 / Apple Silicon / Codex CLI 0.162.1 |
| Windows | Python 3.11+, Node.js 20+ with npm, official Codex CLI; native ConPTY, no WSL or tmux |

On macOS, install missing dependencies with:

```sh
brew install python tmux
```

## Install

Add this repository using Codex's plugin manager:

```sh
codex plugin marketplace add https://github.com/kkrr6840/codex-hud
codex plugin add codex-hud@codex-hud
```

You can then manage the plugin from `/plugins` in Codex.

Use the same installation commands on Windows. The first setup downloads pinned terminal dependencies with prebuilt native binaries.

## Configure once

In Codex, open `/skills`, select **hud**, and ask:

> Enable automatic HUD and use English labels.

You can also invoke the skill explicitly with `$hud`. It runs the setup commands for you. After setup, ordinary interactive Codex launches automatically include the panel.

Setup installs a transparent launcher at `~/.local/bin/codex` and adds a marked PATH block to your shell configuration. Existing proxy functions and CLI settings are preserved.

On Windows, setup creates `~/.local/bin/codex.cmd` and updates the installed Windows PowerShell / PowerShell 7 profiles. Open a new terminal tab once after setup; future launches use ordinary `codex`. Unicode paths, arguments, proxy variables, and exit codes are preserved. Setup does not change execution policy or overwrite npm's `.ps1` / `.cmd` entries.

Starting with **v0.4.1**, setup also recognizes an owned, writable npm CLI symlink verified against the official `@openai/codex` package. It connects that existing entry to the launcher, so terminals that cached the old path work **without `rehash`**. The original link is recorded for restoration; official package files are unchanged. If npm later recreates the link, ask the hud skill to enable automatic HUD again.

For installations without a compatible symlink, open a new terminal tab to load the PATH change. An already running CLI outside tmux needs to be exited and resumed once to gain the panel. Direct calls to another absolute CLI path may bypass the launcher.

## Customize the panel

On macOS, press **F10** to open the settings menu, or keep using **Ctrl-b** followed by uppercase **C**. Use **fn+F10** if the function row controls system audio. F8/F9 remain available for Codex voice. An existing unrelated tmux F10 binding is preserved. In the macOS menu:

- Arrow keys: select an item.
- Space: toggle visibility.
- `1` / `2` / `3`: minimal / standard / full preset.
- `L`: switch layout; `G`: switch language.
- Esc: close the menu. Changes save immediately.

On Windows, use Up/Down to select a preset and Enter to apply it. **F8** also opens this menu. Starting with v0.7.6, a successful save returns to the CLI automatically; errors keep the menu open for retry, and Esc can leave while saving. The hud skill supports all display settings on both platforms, with live updates.

Or use the hud skill in plain language:

> Show only the model, context, and quotas. Use English and a compact layout.

For direct configuration:

```sh
codex-hud config --language en --layout compact
codex-hud config --show quota_5h,quota_weekly --hide tokens,cache
codex-hud config --set 'height=8'
codex-hud config --describe
```

The `codex-hud` command above requires the standalone entry on your PATH. With only the plugin installed, use the hud skill; it locates and runs the installed entry for you.

Preferences are stored in `~/.config/codex-hud/display.json`. An optional `override.json` takes precedence and is preserved during normal configuration. On macOS, **Ctrl-b H** expands or collapses the panel; arrows or `j` / `k` scroll it. Quit normally with `/quit` on either platform.

Starting with v0.7.10, normal macOS launches end when their last terminal connection closes: closing a window or iTerm2 tab, quitting iTerm2, or using `Ctrl-b d` stops that HUD's CLI and panel, including ordinary tool processes in the same pane process group. Saved conversations remain available through `codex resume`. Another terminal connected to the same HUD keeps it alive. Explicit `codex-hud --detach` launches retain their background lifetime. The hud skill can apply this policy to an existing Mac session without restarting its CLI; display refreshes preserve the CLI and draft.

After an upgrade, the hud skill can refresh the macOS observer while keeping the main CLI running. Starting with v0.6.3, open macOS sessions keep their panel, arrow navigation, and menus working even when the plugin manager removes the old version's cache. No session restart is needed. Windows backend upgrades apply on the next normal launch; display preferences update live on both platforms.

## Select agents directly (v0.6)

Press **Down** from the default empty composer to highlight the first agent in the HUD. **Up/Down** selects, **Enter** opens details, and **Esc** returns to the HUD and then to the input. Up from the first entry also returns to the input. With a draft, a native menu, or no selectable agents, Down passes through to Codex.

| Action | macOS | Windows |
| --- | --- | --- |
| Select from empty input | Down | Down |
| Move / inspect / return | Up/Down / Enter / Esc | Up/Down / Enter / Esc |
| Auxiliary entry | Option (⌥)+Down, or Ctrl-b then Down | Alt+Down, or Ctrl-b then Down |
| Settings | F10 (or fn+F10), or Ctrl-b then uppercase C | Ctrl-b then uppercase C, or F8 |

If your Mac terminal reserves Option, use plain Down or Ctrl-b then Down. Auxiliary shortcuts open a list on macOS and select a HUD row on Windows. Use them when the composer has a different placeholder.

Details show live task, model, status, elapsed time, reported usage, recent tools, and readable assistant output for native agents and correlated relay jobs. Up/Down scrolls long output. The main CLI keeps running throughout.

Starting with v0.6.1, completed agents and relay jobs leave the bottom list automatically, including their counts and overflow rows. An open detail view remains available for reading the final result. To show completed history again, ask the hud skill to set `agents_show_completed=true`; no session restart is needed.

Starting with v0.6.4, failed agents and relay jobs also leave the bottom list and counts by default. Failure records remain available to the parent conversation and historical inspection. Set `agents_show_failed=true` to display failed history again. Unfinished unknown states and active-task data-source errors remain visible.

v0.7.3 fixes missing live relay rows when a relay runs alongside other commands in one parallel tool call. Indexed results correlate each literal command and subsequent poll without borrowing another command's status or usage. Ended jobs without a returned result leave the list with completed history; their outcome and usage remain unknown. Set `agents_show_completed=true` to inspect them. Missing-log warnings for hidden completed/failed agents also leave the main HUD; active-agent and main-thread read errors remain visible.

The viewer reads existing records without resuming an agent or acquiring its writer. It reads the last five assistant messages available in a 128 KB transcript tail and excludes inherited messages dated before the child session was created. Relay jobs can display their returned answer even when their worker transcript was not saved. Output outside the read window, encrypted records, and results not yet produced stay unavailable. Unknown usage stays unknown. For the complete native subagent conversation and continuing its work, use Codex's **`/subagents`** command.

## Data accuracy and limitations

Each panel is bound to its own thread using invocation-specific diagnostics and official local session records. Agent counts come from thread relations and turn events, not process counts. Other sessions' activity is excluded.

The model label follows the current thread selection; tokens and context capacity reflect recorded requests. Changing a global default does not necessarily change the selected model in every running session. Context usage is request input divided by the reported runtime context window; cumulative token totals can be much larger because requests repeatedly include history.

Quota bars use official rate-limit snapshots received by the current thread. They are not estimated from tokens. Unknown values stay unknown, and unavailable quota windows are hidden. Costs, API tokens per second, cache expiry, and active skill execution do not have reliable telemetry in this implementation and are not invented.

Codex HUD uses a private tmux session on macOS and a native ConPTY terminal host on Windows. Both launch the official CLI with `--no-daemon` and a private diagnostic directory. Non-interactive commands, help/version queries, redirected output, and remote sessions pass through to the official CLI. CLI approval and network policies still apply.

Starting with v0.4.2, the private server enables synchronized updates for an outer tmux terminal and uses an explicit steady cursor in the CLI pane. Starting with v0.7.5, refresh replaces an old nested display connection when its cached capabilities still lack synchronized updates. The CLI, session, typed draft and cursor position remain intact; the screen may briefly redraw once without reopening the conversation. Already synchronized, read-only, control-mode and suspended clients are not reconnected automatically.

Starting with v0.7.6, refresh can connect a visible terminal directly to the current session when it was nested inside another private HUD owned by this plugin. Starting with v0.7.10, this requires a persistent parent HUD so both CLIs can remain alive; a parent that closes with its terminal keeps its existing connection. Saved history, personal tmux servers, other windows and protected clients are preserved. Existing Windows sessions load the new menu code on the next normal launch; Esc still returns from the current menu.

## Disable or remove

Ask the hud skill to **disable automatic HUD** or **remove its launcher integration**. Disabling or removing the native plugin makes the launcher pass through to the official CLI. Removing launcher integration also restores its managed npm link and removes its marked PATH block; an entry changed by you or a package update is preserved.

## Development

```sh
python3 -m unittest discover -s tests -v
npm ci --prefix native --include=optional
node --test tests/native_terminal.test.cjs
```

The suite covers session isolation, model changes, quotas, usage accounting, argument and proxy forwarding, install/restore behavior, and real tmux/PTY and ConPTY navigation, bridge data, Unicode paths, and cleanup. [Windows CI](https://github.com/kkrr6840/codex-hud/actions/workflows/windows-native.yml) uses local fixtures without credentials or model requests.

MIT licensed. See [LICENSE](LICENSE).
