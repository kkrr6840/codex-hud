# Codex HUD

为官方 Codex CLI 添加固定底部 HUD。安装并配置一次后，照常运行 `codex`，HUD 自动出现，无需每次运行 `codex-hud` 或手动接入 tmux。

![Codex plugin](https://img.shields.io/badge/Codex-plugin-blue) ![License](https://img.shields.io/badge/license-MIT-green)

![HUD 样式预览，图中为示例数据](assets/preview.png)

## 安装

已在 macOS 15、arm64、Codex CLI 0.162.1 上实测。需要 Python 3.11+、tmux 3.2+（设置弹窗）和 lsof；Git 状态显示还需要 Git。macOS 缺少依赖时可以安装：

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

如果是在安装前就打开的 zsh 终端，退出 Codex 后先运行一次 `rehash`，清除旧命令路径缓存，再正常进入。仅退出并重新进入 Codex 不会自动清除 shell 的缓存。

配置 skill 也支持“关闭自动 HUD”“最大高度设为 8 行”“查看状态”“移除启动包装”。在 `/plugins` 中禁用或卸载插件后，启动包装会退回官方 CLI。需要删除包装和 PATH 标记时，通过 hud skill 执行“移除启动包装”。

## 选择显示内容（v0.4）

**按 Ctrl-b，松开后按大写 C** 打开设置菜单，方向键选择、空格开关，`1/2/3` 选择精简/标准/完整预设，`L` 切换紧凑/展开布局，`G` 切换语言，`Esc` 关闭。修改即时保存，约一秒后刷新，不用重新进入会话。

也可以在 `/skills` 选择 **hud**，直接说“只保留模型、上下文和额度”“隐藏分流”“显示 Git 领先落后”“用英文紧凑布局”。高级配置同样可通过这个 skill 修改，无需手改配置文件。

设置保存在 `~/.config/codex-hud/display.json`。可选 `override.json` 为手动覆盖层；嵌套对象逐项合并，数组替换，覆盖层优先，常规配置不会改写它。没有数据的元素不显示。`codex-hud config --describe` 列出所有有效设置及数据限制。

| 设置 | 可选内容 |
| --- | --- |
| 显示项目 | 模型、力度、状态、目录、额外目录、Git、提供商、会话名、版本、订阅计划、上下文、5 小时/周/其他额度、Token、缓存、工具、MCP、子 agent、计划、会话时间、开始时间、最近响应、压缩次数、配置数量、RAM、分流、自定义文字 |
| 布局 | `layout`: expanded/compact；`order` 区域顺序、`header_order` 首行顺序、`merge_groups` 合并行、`right_align` 合并行右对齐、`separators` 分隔线、`max_width` 最大宽度、`height` 最大行数 |
| 上下文 | `context_value`: percent/tokens/remaining/both；`context_bar`；`context_window` 可选手动分母（显示星号）；警告/严重阈值 |
| 额度 | `quota_value`: used/remaining；`quota_style`: bar/text/compact；`reset_format`: relative/absolute/both/elapsed/elapsed_absolute；时制、秒、显示阈值、消耗速度提示 |
| Git | dirty、ahead_behind、file_stats、worktree、wrap、push_warning、push_critical |
| 样式 | 简体/繁体/英文、路径层数、模型简称/覆盖、力度文字/符号、提供商标签、自定义文字、颜色名/256 色/#rrggbb、单列宽进度条字符 |
| 活动 | `tools_limit`、`agents_limit`、`tool_name_length`；工具显示最近至多 200 条记录的活动，不冒充全部历史 |

开发者也可使用 `codex-hud config --show quota_5h,quota_weekly --hide tokens`，或 `codex-hud config --set 'git.ahead_behind=true'`。预设只修改显示开关和布局，保留自定义颜色等其他设置。

## 显示内容

- 当前会话的模型、推理档位、请求输入与实际上下文窗口、累计输入/缓存/输出/推理 token。
- 5 小时和每周额度的剩余百分比、进度条及本地时间的重置日期。读取当前会话收到的官方额度快照，按实际窗口长度识别，不把 primary 固定当作 5 小时；官方未返回的窗口隐藏，过期快照提示等待更新。额度随 Codex 的事件更新，不根据 token 数估算，也不额外读取登录凭据。
- 当前会话目录的 Git 分支与变更状态。
- 最近工具活动、明确的 `update_plan` 计划事件、MCP 工具名、会话名称、CLI 版本、时长和压缩次数；可选显示本地配置数量、系统物理 RAM。
- 原生子 agent 的名称、任务、状态和耗时；来自父子线程关系和轮次事件，不以进程数代替 agent 数。
- codex-x-codex 的开关状态，以及本会话及其子 agent 关联的分流状态和完成用量；不把全局记录混入当前会话。

初次进入或切换到无数据的新会话时，面板完全隐藏；有实际数据后自动出现。缺少的字段、空 agent 和空分流区域不显示，面板高度随内容收缩。真实读取错误和已有任务的未知用量仍会明确提示。

上下文使用彩色进度条：低于 70% 为绿色，70% 起为黄色，90% 起为红色；缓存占比单独使用蓝色进度条。累计 token 是计数，不会用上下文容量作为它的错误分母。

运行、待命、完成、失败和未知状态分开显示；加密的详细任务指令显示实际任务名。上下文数字是包含系统内容的请求输入，和原生 TUI 的 token 显示口径可能不同。

主面板的“主会话 Token”优先采用 `token_usage_record.thread_token_usage`，必须匹配当前线程 ID，之后不让其他汇总事件覆盖它。原生子 agent 和分流用量仅出现在自己的活动行，不相加到主会话上下文或累计值。累计输入会重复计算各次请求送入的历史内容，所以可远大于上下文窗口；缓存属于输入的一部分。失败的分流任务未回传用量时只显示一次“未返回用量”，不填成零。

`Ctrl-b H` 展开或收起详情；在 HUD 中使用方向键或 `j/k` 滚动，`Esc` 回到 CLI。正常输入 `/quit` 退出，临时诊断日志、HUD 进程和私有 tmux 会话会自动清理。

插件升级后，可让 hud skill 执行 `codex-hud refresh --session <当前会话 ID>`，只替换观察面板，保持 CLI 进程和对话继续。`codex-hud watch --session <ID>` 可在独立终端只读监视指定会话；它固定监视该 ID，不自动跟随另一个客户端的 `/new`。

## 与 Claude HUD 的功能对照

本次对照了本机 [claude-hud](https://github.com/jarrodwatts/claude-hud) 0.11.2 的配置和渲染功能，采用独立 Python 实现。终端外观与配置方式尽量对应，数据始终来自 Codex。

| Claude HUD 功能 | Codex HUD 对应与边界 |
| --- | --- |
| 显示开关、预设、布局、排序/合并、语言/颜色、Git 详情 | 已实现；通过设置弹窗或 hud skill 修改，实时刷新 |
| 模型、路径/额外目录、力度、提供商、会话名、版本、认证计划 | 使用当前 Codex 元数据、线程数据库和返回的 plan_type；不读取凭据或用户名 |
| 上下文、额度、剩余/已用、重置时间、消耗速度提示、缓存命中率 | 已实现；按官方窗口长度识别 5 小时/7 天，其他窗口保留真实名称和分钟数；未返回则隐藏 |
| 工具、MCP、子 agent、待办、时间、压缩、RAM、配置数量 | 已实现；MCP 仅依据明确的 mcp__ 工具事件，计划仅依据明确 update_plan 事件，配置计数不等于实际加载数量 |
| 会话/每日/每周费用、路由费用、纯 API 输出速度、prompt cache 到期 | 当前 Codex 数据不完整，暂不支持；不以 token 估算真实账单，不把工具等待算成 API 速度，不猜缓存 TTL |
| 活动 Skill、advisor、输出风格、登录用户名 | 无可靠对应事件或属于 Claude 专有字段；不把读取 SKILL.md 当成技能执行 |
| jj、外部额度文件/写出、额外状态命令、余额来源合并 | 本版未实现；不会拿 Claude 的账号窗口补 Codex 缺失额度 |
| 原生 statusLine 回调 | Codex HUD 使用 tmux 面板，未修改官方 CLI；已有面板支持配置热更新 |

恢复长会话时读取日志末尾约 2 MB；工具历史和压缩计数可能不完整，压缩计数会标记 `≥`。官方累计 Token 由记录中的累计字段提供，不将日志尾部相加冒充总量。

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
