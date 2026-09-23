# Claude 凹3

一个 Claude Code 风格的 AO3 阅读器。搜索作品、前后切章、自动保存阅读位置；按 **Ctrl+G** 收起阅读，切到本地工作便笺，再按一次回到原处。独立于 Claude Code，不需要模型、账号或 API Key。

## Mac 应用

[下载 v0.6.0 预览版](https://github.com/Kev-ZJY/claude-ao3/releases/download/v0.6.0/Claude-AO3-0.6.0-macos-arm64.dmg)，打开 DMG，把 **Claude 凹3** 拖入「应用程序」，然后打开。应用内是真实终端阅读器，已包含 Python 和依赖。

支持 Apple Silicon，构建目标为 macOS 13+；本次实际运行验证环境为 macOS 26.3。预览版使用临时签名，**尚未完成 Apple 公证**，首次打开可能需要在「系统设置 → 隐私与安全性」中允许，见 [Apple 说明](https://support.apple.com/zh-cn/102445)。没有 Intel 预编译包。

## 终端一键安装

在 Apple Silicon Mac 的终端执行：

```sh
curl -fsSL https://github.com/Kev-ZJY/claude-ao3/releases/download/v0.6.0/install.sh | sh -s -- --launch
```

安装器校验 SHA-256，将独立运行环境安装到 `~/.local/share/claude-ao3/releases/0.6.0/`，命令安装到 `~/.local/bin/claude-ao3`；必要时为 zsh/bash 添加 PATH。安装后立即打开阅读器，以后新开终端输入 `claude-ao3`。无交互终端时仅安装。不改动 `claude`，也不更改 macOS 安全设置。

自定义目录可下载并检查 `install.sh` 后执行 `sh install.sh --install-root DIR --bin-dir DIR --no-path`；省略 `--launch` 可只安装。Mac 应用与终端使用同一默认数据目录，不应同时打开同一份进度。

## 阅读

| 操作 | 作用 |
|---|---|
| `/search` | 搜索关键词、警告和关系类别；新搜索固定中文 |
| `n` / `p` | 下一章 / 上一章；自然延续系列阅读 |
| ↑↓、j/k、Space、PgUp/PgDn | 滚动、翻页 |
| Ctrl+G | 收起到本地便笺 / 恢复阅读及草稿 |
| Esc | 取消等待或返回 |
| `/retry` | 重试失败操作 |
| Ctrl+C / `/quit` | 保存退出；Mac 关闭窗口也会等待保存 |

默认书源为第三方 `https://aoya.moe`，默认 direct 网络方式；可达性取决于书源及当前网络，失败不会自动切换来源。内容提示需手动确认。支持 `--open URL`、`--network-info`、`--doctor`、`--data-dir PATH`，其余参数见 `--help`。

为兼容旧版本，macOS 进度保存在 `~/Library/Application Support/mini-notes/`，按来源隔离。退出保留当前章节和位置；Ctrl+G 只切换本应用界面。

## 源码与构建

Python 3.11+、系统 curl。Intel Mac / Linux 可尝试源码安装；这次预编译发行仅支持 Apple Silicon。

```sh
sh scripts/install.sh
claude-ao3
```

源码安装需保留项目目录；优先使用 uv 锁文件，未安装 uv 时使用 venv/pip。开发与检查：

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/build_release.py
.venv/bin/python scripts/package_acceptance.py
```

在 Apple Silicon Mac 上安装 Xcode Command Line Tools 和 uv 后，构建独立应用：

```sh
.venv/bin/python scripts/build_standalone.py
.venv/bin/python scripts/build_macos_app.py
```

前者下载独立 Python、冻结锁定依赖并输出 CLI 归档；后者编译 AppKit/SwiftTerm 外壳、生成图标和 `dist/macos/` 内的应用及 DMG。未提供 Developer ID 时只生成临时签名预览版。第三方许可随二进制包含。

`web/` 是静态介绍页，可运行 `python3 -m http.server 5179 --directory web` 预览。页面视频为真实终端操作录屏，正文和书源响应使用自拟示例；不代表当前实网可用性。

项目与 Anthropic、AO3 无隶属关系。
