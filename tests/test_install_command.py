"""Actual shell execution in temporary homes; never modifies the user's PATH files."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install_command.py'


def setup(tmp_path):
    entry = tmp_path / "目录 with ' quotes" / 'reader'
    entry.parent.mkdir()
    entry.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    entry.chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()
    bin_dir = home / '.local/bin'
    env = {**os.environ, 'HOME': str(home), 'ZDOTDIR': str(home), 'SHELL': '/bin/zsh', 'PATH': '/usr/bin:/bin'}
    return entry, home, bin_dir, env


def install(entry, bin_dir, env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), '--entry', str(entry), '--bin-dir', str(bin_dir), *args], env=env, capture_output=True, text=True)


def test_install_runs_from_unrelated_directory_preserves_args_and_is_idempotent(tmp_path):
    entry, home, bin_dir, env = setup(tmp_path)
    assert install(entry, bin_dir, env).returncode == 0
    before = (home / '.zshrc').read_bytes()
    assert install(entry, bin_dir, env).returncode == 0
    assert (home / '.zshrc').read_bytes() == before
    launched = subprocess.run([str(bin_dir/'claude-ao3'), 'a b', '$(touch not-created)', '中文'], cwd=home, env=env, capture_output=True, text=True)
    assert launched.returncode == 0 and launched.stdout.splitlines() == ['a b', '$(touch not-created)', '中文']
    assert not (home/'not-created').exists()
    if Path('/bin/zsh').exists():
        launched = subprocess.run(['/bin/zsh', '-ic', 'claude-ao3 from-anywhere'], cwd='/tmp', env=env, capture_output=True, text=True)
        assert launched.returncode == 0 and 'from-anywhere' in launched.stdout


def test_installer_preserves_claude_and_refuses_unowned_name_collision(tmp_path):
    entry, home, bin_dir, env = setup(tmp_path)
    bin_dir.mkdir(parents=True)
    original = bin_dir/'claude'
    original.write_text('existing Claude Code')
    collision = bin_dir/'claude-ao3'
    collision.write_text('some other user command')
    result = install(entry, bin_dir, env)
    assert result.returncode != 0 and '覆盖' in result.stderr
    assert collision.read_text() == 'some other user command'
    assert original.read_text() == 'existing Claude Code'
    assert not (home/'.zshrc').exists()


def test_uninstall_removes_only_own_command_and_path_block(tmp_path):
    entry, home, bin_dir, env = setup(tmp_path)
    rc = home/'.zshrc'
    rc.write_text('# user configuration\nexport USER_SETTING=retained\n')
    original = rc.read_bytes()
    assert install(entry, bin_dir, env).returncode == 0
    assert install(entry, bin_dir, env, '--uninstall').returncode == 0
    assert not (bin_dir/'claude-ao3').exists()
    assert rc.read_bytes() == original
    assert entry.exists()


def test_existing_path_needs_no_shell_change(tmp_path):
    entry, home, bin_dir, env = setup(tmp_path)
    env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']
    assert install(entry, bin_dir, env).returncode == 0
    assert not (home/'.zshrc').exists()


@pytest.mark.parametrize('uninstall_args', [[], ['--no-path']])
def test_uninstall_of_separate_command_keeps_main_installation_path(tmp_path, uninstall_args):
    entry, home, bin_dir, env = setup(tmp_path)
    assert install(entry, bin_dir, env).returncode == 0
    original = (home/'.zshrc').read_bytes()
    isolated = tmp_path/'isolated-bin'
    assert install(entry, isolated, env, '--no-path').returncode == 0
    assert install(entry, isolated, env, '--uninstall', *uninstall_args).returncode == 0
    assert (home/'.zshrc').read_bytes() == original
    assert (bin_dir/'claude-ao3').exists()
