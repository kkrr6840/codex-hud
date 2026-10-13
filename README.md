# Codex HUD

为官方 Codex CLI 添加固定底部 HUD。安装并配置一次后，照常运行 `codex`，HUD 自动出现，无需每次运行 `codex-hud` 或手动接入 tmux。

![Codex plugin](https://img.shields.io/badge/Codex-plugin-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## 安装

已在 macOS 15、arm64、Codex CLI 0.162.1 上实测。需要 Python 3.11+、tmux 和 lsof；Git 状态显示还需要 Git。macOS 缺少依赖时可以安装：

```sh
brew install python tmux
```

通过 Codex 自带的插件管理器添加本仓库并安装：

```sh
codex plugin marketplace add https://github.com/kkrr6840/codex-hud
codex plugin add codex-hud@codex-hud
```

添加市场后，也可以在 Codex 的 `/plugins` 中管理这个插件。

## 配置一次，之后自动显示

安装后重新进入 Codex，在 `/skills` 中选择 **hud**，发送“开启自动 HUD”。也可以显式调用 `$hud`。配置 skill 会运行本插件的安装器，无需自己输入启动或 tmux 命令。

配置会创建用户目录下的 `~/.local/bin/codex` 启动包装，并向 shell rc 添加一段带标记的 PATH 设置。原来的 Codex 程序、登录信息、模型配置和已有 shell 函数保持原样。已有代理函数调用外部 `codex` 时，代理环境会继续传递。已有其他同名本地启动器时会停止，避免覆盖。

如果已有 alias 或函数直接调用 Codex 的绝对路径，它会绕过 PATH 集成；需要让该入口调用 PATH 中的 `codex`。安装器会保留这些定义。插件升级或回滚时，启动器以原生插件管理器报告的已安装版本为准。

新开一次终端标签页以加载设置；此后正常运行 `codex`、`codex resume` 或 `codex fork` 都自动带 HUD。已在运行、且不在 tmux 中的 CLI 需要重新进入一次。

配置 skill 也支持“关闭自动 HUD”“高度设为 8 行”“查看状态”“移除启动包装”。在 `/plugins` 中禁用或卸载插件后，启动包装会退回官方 CLI。需要删除包装和 PATH 标记时，通过 hud skill 执行“移除启动包装”。

## 显示内容

- 当前会话的模型、推理档位、请求输入与实际上下文窗口、累计输入/缓存/输出/推理 token。
- 当前会话目录的 Git 分支与变更状态。
- 原生子 agent 的名称、任务、状态和耗时；来自父子线程关系和轮次事件，不以进程数代替 agent 数。
- codex-x-codex 的开关状态，以及本会话及其子 agent 关联的分流状态和完成用量；不把全局记录混入当前会话。

运行、待命、完成、失败和未知状态分开显示。无法读取的数据保留为未知；加密的详细任务指令显示实际任务名。上下文数字是包含系统内容的请求输入，和原生 TUI 的 token 显示口径可能不同。

`Ctrl-b H` 展开或收起详情；在 HUD 中使用方向键或 `j/k` 滚动，`Esc` 回到 CLI。正常输入 `/quit` 退出，临时诊断日志、HUD 进程和私有 tmux 会话会自动清理。

## 运行方式与边界

插件使用独立 tmux 会话运行官方 CLI，为本次调用加入 `--no-daemon` 和私有临时诊断目录。CLI 配置文件中的模型、账号和权限策略继续生效；不会修改用户的 tmux 配置。每个 HUD 按其自己的诊断事件和日志归属绑定线程，包括 `/new` 尚未发消息的阶段。

`exec`、`review`、`debug`、`login`、`plugin` 等非交互命令，帮助/版本查询、管道或重定向输出、远程会话继续使用原 CLI。未知参数语法会保守透传。HUD 不改变审批或网络政策。

这是一份可通过 Codex 插件管理器安装的 skill 插件，加上一次性的本地启动集成。Codex 的[原生状态栏](https://learn.chatgpt.com/docs/cli/slash-commands#configure-footer-items-with-statusline)通过 `/statusline` 配置内建字段；本插件的 agent 和分流面板由 tmux 渲染，未替换或修改官方 CLI。

## 开发与检查

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖会话隔离、`/new` 切换、状态及未知用量、透明参数转发、已有启动器保护，以及真实 tmux/PTY 下的自动启动、刷新、窗口尺寸变化和退出清理。

许可证：MIT。
