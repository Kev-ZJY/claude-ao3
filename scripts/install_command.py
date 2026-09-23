#!/usr/bin/env python3
"""Install an owned, reversible user command; never modify the claude executable."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = '# Claude-ao3 managed command v1'
BEGIN = '# >>> Claude-ao3 PATH >>>'
END = '# <<< Claude-ao3 PATH <<<'


def owned(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open('rb') as stream:
        return stream.read(128).startswith(('#!/bin/sh\n' + MARKER + '\n').encode())


def write_atomic(path: Path, content: str, mode: int) -> None:
    # Preserve a user's dotfile symlink, if present.
    path = path.resolve() if path.is_symlink() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content.encode())
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def rc_paths() -> list[Path]:
    home = Path.home()
    shell = Path(os.environ.get('SHELL', '')).name
    if shell == 'zsh':
        return [Path(os.environ.get('ZDOTDIR', home)) / '.zshrc']
    if shell == 'bash':
        login = next((home / name for name in ('.bash_profile', '.bash_login', '.profile') if (home/name).exists()), home/'.bash_profile')
        return [home/'.bashrc', login]
    return []


def remove_block(text: str, bin_dir: Path | None = None) -> str:
    start = text.find(BEGIN)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        raise ValueError('PATH 配置块不完整，请先检查 shell 配置。')
    end += len(END)
    if bin_dir is not None:
        expected = f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"'
        if expected not in text[start:end].splitlines():
            return text
    if text[end:end+1] == '\n':
        end += 1
    return text[:start] + text[end:]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='安装或移除 claude-ao3 用户命令')
    parser.add_argument('--entry', type=Path, default=ROOT/'.venv/bin/claude-ao3')
    parser.add_argument('--bin-dir', type=Path, default=Path.home()/'.local/bin')
    parser.add_argument('--no-path', action='store_true', help='不更新 shell PATH 配置')
    parser.add_argument('--local-only', action='store_true', help='只用仓库内启动脚本')
    parser.add_argument('--uninstall', action='store_true', help='只移除命令及自有 PATH 配置块，保留进度和源码')
    args = parser.parse_args(argv)
    if args.local_only:
        print('本地安装完成：./claude-ao3')
        return 0
    bin_dir = args.bin_dir.expanduser().absolute()
    target = bin_dir/'claude-ao3'
    try:
        if os.path.lexists(target) and not owned(target):
            raise ValueError(f'拒绝覆盖或移除非本产品管理的命令：{target}')
        if args.uninstall:
            target.unlink(missing_ok=True)
            for rc in ([] if args.no_path else rc_paths()):
                if rc.exists():
                    text = rc.read_text()
                    cleaned = remove_block(text, bin_dir)
                    if cleaned != text:
                        write_atomic(rc, cleaned, stat.S_IMODE(rc.stat().st_mode))
            print('已移除 claude-ao3 命令；源码、环境和阅读进度保留。')
            return 0
        entry = args.entry.expanduser().absolute()
        if not entry.is_file() or not os.access(entry, os.X_OK) or entry.resolve() == target.resolve():
            raise ValueError('缺少可执行入口，或入口与目标相同；请先运行 scripts/install.sh。')
        script = f'#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(entry))} "$@"\n'
        write_atomic(target, script, 0o755)
        present = any(Path(p or '.').absolute() == bin_dir for p in os.get_exec_path())
        if not present and not args.no_path:
            paths = rc_paths()
            for rc in paths:
                text = rc.read_text() if rc.exists() else ''
                # Appending does not read, execute or display unrelated settings.
                clean = remove_block(text)
                block = f'{BEGIN}\nexport PATH={shlex.quote(str(bin_dir))}:"$PATH"\n{END}\n'
                updated = clean + ('' if not clean or clean.endswith('\n') else '\n') + block
                if updated != text:
                    write_atomic(rc, updated, stat.S_IMODE(rc.stat().st_mode) if rc.exists() else 0o600)
            if paths:
                print('已配置用户 PATH；新开终端后生效。当前终端也可执行：')
            else:
                print('请将命令目录加入当前 shell 的 PATH：')
            print(f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"')
        elif not present:
            print(f'测试/自定义目录；需配置 PATH={shlex.quote(str(bin_dir))}:"$PATH"')
        print(f'Claude 凹3 已安装：{target}\n任意目录启动：claude-ao3')
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
