"""Keyboard display settings, opened without leaving the active Codex session."""
import curses
import sys

from .config import ELEMENTS, load, preset, save
from .display import clip


def run():
    if not sys.stdin.isatty():
        print("交互设置需要终端；也可以通过 hud skill 修改显示选项", file=sys.stderr)
        return 2
    def ui(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        selected, notice = 0, ""
        keys = list(ELEMENTS)
        while True:
            cfg, error = load()
            en = cfg["language"] == "en"
            screen.erase()
            height, width = screen.getmaxyx()
            def line(y, text, attr=0):
                if 0 <= y < height:
                    try:
                        screen.addstr(y, 0, clip(text, width-1), attr)
                    except curses.error:
                        pass
            line(0, " Codex HUD · Display settings" if en else " Codex HUD · 显示设置", curses.A_BOLD)
            line(1, " ↑↓ select · Space toggle · 1/2/3 presets · L layout · G language" if en else " ↑↓ 选择 · 空格 开关 · 1/2/3 预设 · L 布局 · G 语言")
            line(2, f" {cfg['layout']} · {cfg['language']} · Esc/q close · saved live" if en else f" {cfg['layout']} · {cfg['language']} · Esc/q 关闭 · 修改立即保存生效")
            room = max(1, height-5)
            offset = max(0, selected-room+1)
            for i, key in enumerate(keys[offset:offset+room], offset):
                marker = "[✓]" if cfg["show"][key] else "[ ]"
                line(4+i-offset, f" {marker} {key if en else ELEMENTS[key]}", curses.A_REVERSE if i == selected else 0)
            line(height-1, " " + (error or notice or ("1 minimal · 2 standard · 3 full" if en else "1 精简 · 2 标准 · 3 完整；高级选项通过 hud skill 修改")))
            screen.refresh()
            key = screen.getch()
            if key in (27, ord("q")):
                return
            if key in (curses.KEY_DOWN, ord("j")):
                selected = (selected+1) % len(keys)
            elif key in (curses.KEY_UP, ord("k")):
                selected = (selected-1) % len(keys)
            else:
                patch = None
                if key in (ord(" "), 10, 13):
                    patch = {"show": {keys[selected]: not cfg["show"][keys[selected]]}}
                elif key in (ord("1"), ord("2"), ord("3")):
                    patch = preset({ord("1"): "minimal", ord("2"): "standard", ord("3"): "full"}[key])
                elif key in (ord("l"), ord("L")):
                    patch = {"layout": "compact" if cfg["layout"] == "expanded" else "expanded"}
                elif key in (ord("g"), ord("G")):
                    langs = ["zh-Hans", "zh-Hant", "en"]
                    patch = {"language": langs[(langs.index(cfg["language"])+1) % 3]}
                if patch:
                    try:
                        save(patch)
                        notice = "Saved" if en else "已保存"
                    except (OSError, ValueError) as err:
                        notice = str(err)
    curses.wrapper(ui)
    return 0
