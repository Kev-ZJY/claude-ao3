"""Execute the POSIX installer against tiny invented archives; no network/user files."""
import hashlib
import errno
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tarfile
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
VERSION = "0.6.0"
ARCHIVE = f"claude-ao3-{VERSION}-macos-arm64.tar.gz"


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    home, mocks, download = [tmp_path / n for n in ("home", "mocks", "download")]
    for directory in (home, mocks, download):
        directory.mkdir()
    executable(mocks / "uname", '#!/bin/sh\ncase "$1" in -s) echo "${TEST_OS:-Darwin}";; -m) echo "${TEST_ARCH:-arm64}";; esac\n')
    executable(mocks / "sw_vers", '#!/bin/sh\necho "${TEST_MACOS:-13.0}"\n')
    executable(mocks / "sysctl", '#!/bin/sh\necho "${TEST_TRANSLATED:-0}"\n')
    executable(mocks / "curl", f"#!{sys.executable}\n" + '''import json, os, pathlib, shutil, sys
args=sys.argv[1:]
with open(os.environ['TEST_CURL_LOG'], 'a') as f: f.write(json.dumps(args)+'\\n')
if os.environ.get('TEST_DOWNLOAD_FAIL') == '1': sys.exit(22)
url=args[-1]
assert url.startswith('https://github.com/Kev-ZJY/claude-ao3/releases/download/v0.6.0/')
target=args[args.index('--output')+1]
shutil.copyfile(pathlib.Path(os.environ['TEST_DOWNLOAD'])/url.rsplit('/',1)[1],target)
''')
    env = {**os.environ, "HOME": str(home), "ZDOTDIR": str(home), "SHELL": "/bin/zsh",
           "PATH": str(mocks) + ":/usr/bin:/bin:/usr/sbin:/sbin", "TEST_DOWNLOAD": str(download),
           "TEST_CURL_LOG": str(tmp_path / "curl.jsonl"), "TMPDIR": str(tmp_path)}
    return {"root": tmp_path, "home": home, "download": download, "env": env,
            "install": tmp_path / "install", "bin": tmp_path / "bin"}


def archive(s, *, bad_path=None, symlink=False, hardlink=False, binary=None):
    target = s["download"] / ARCHIVE
    with tarfile.open(target, "w:gz") as tar:
        for name, content, mode in (
            ("claude-ao3/claude-ao3", binary or b'#!/bin/sh\nprintf "<%s>\\n" "$@"\n', 0o755),
            ("claude-ao3/_internal/fixture.txt", b"self-authored fixture\n", 0o644),
        ):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(content), mode
            tar.addfile(member, io.BytesIO(content))
        if bad_path:
            member = tarfile.TarInfo(bad_path)
            member.size = 6
            tar.addfile(member, io.BytesIO(b"unsafe"))
        if symlink:
            member = tarfile.TarInfo("claude-ao3/_internal/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "fixture.txt"
            tar.addfile(member)
        if hardlink:
            member = tarfile.TarInfo("claude-ao3/_internal/hardlink")
            member.type = tarfile.LNKTYPE
            member.linkname = "claude-ao3/_internal/fixture.txt"
            tar.addfile(member)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (s["download"] / "SHA256SUMS.txt").write_text(f"{digest}  {ARCHIVE}\n")
    return digest


def run(s, *options, default_paths=False):
    command = ["/bin/sh", str(SCRIPT)]
    if not default_paths:
        command += ["--install-root", str(s["install"]), "--bin-dir", str(s["bin"])]
    return subprocess.run(command + list(options), env=s["env"], cwd=s["root"],
                          capture_output=True, text=True, timeout=15)


def command_path(s):
    return s["bin"] / "claude-ao3"


def old_managed_command(s):
    s["bin"].mkdir(exist_ok=True)
    executable(command_path(s), '#!/bin/sh\n# Claude-ao3 managed command v1\nprintf "old-version\\n"\n')
    return command_path(s).read_bytes()


def test_installs_and_forwards_literal_arguments_from_another_directory(sandbox):
    s = sandbox
    s["install"] = s["root"] / "安装目录 with ' quotes"
    s["bin"] = s["root"] / "command path 'quoted'"
    archive(s)
    result = run(s, "--no-path")
    assert result.returncode == 0, result.stderr
    binary = s["install"] / "releases" / VERSION / "claude-ao3"
    assert binary.is_file() and os.access(binary, os.X_OK)
    args = ["a b", "$(touch must-not-exist)", "中文", "single'quote", 'double"quote', ""]
    launched = subprocess.run([str(command_path(s)), *args], cwd=s["home"], env=s["env"],
                              capture_output=True, text=True)
    assert launched.returncode == 0
    assert launched.stdout == "".join(f"<{arg}>\n" for arg in args)
    assert not (s["home"] / "must-not-exist").exists()
    assert not (s["home"] / ".zshrc").exists()


def test_repeat_install_is_idempotent_and_preserves_unknown_claude(sandbox):
    s = sandbox
    archive(s)
    s["bin"].mkdir()
    (s["bin"] / "claude").write_text("unrelated Claude Code entry")
    assert run(s).returncode == 0
    first_command = command_path(s).read_bytes()
    first_profile = (s["home"] / ".zshrc").read_bytes()
    assert run(s).returncode == 0
    assert command_path(s).read_bytes() == first_command
    assert (s["home"] / ".zshrc").read_bytes() == first_profile
    assert (s["bin"] / "claude").read_text() == "unrelated Claude Code entry"


@pytest.mark.parametrize("failure", ["download", "sha", "malformed_sha", "duplicate_sha"])
def test_failed_download_or_checksum_preserves_existing_command(sandbox, failure):
    s = sandbox
    digest = archive(s)
    original = old_managed_command(s)
    checksum = s["download"] / "SHA256SUMS.txt"
    if failure == "download":
        s["env"]["TEST_DOWNLOAD_FAIL"] = "1"
    elif failure == "sha":
        checksum.write_text(f"{'0' * 64}  {ARCHIVE}\n")
    elif failure == "malformed_sha":
        checksum.write_text(f"not-a-sha  {ARCHIVE}\n")
    else:
        checksum.write_text(f"{digest}  {ARCHIVE}\n{digest}  {ARCHIVE}\n")
    result = run(s, "--no-path")
    assert result.returncode == 4, result.stderr
    assert command_path(s).read_bytes() == original
    assert not (s["install"] / "releases" / VERSION).exists()
    assert not (s["home"] / ".zshrc").exists()


def test_upgrade_existing_source_installer_owned_command(sandbox):
    s = sandbox
    archive(s)
    old_managed_command(s)
    assert run(s, "--no-path").returncode == 0
    assert b"old-version" not in command_path(s).read_bytes()
    assert b"# Claude-ao3 managed command v1" in command_path(s).read_bytes()


@pytest.mark.parametrize("collision", ["command", "command_symlink", "version", "version_symlink"])
def test_unknown_command_and_version_targets_are_never_overwritten(sandbox, collision):
    s = sandbox
    archive(s)
    untouched = s["root"] / "unrelated"
    untouched.write_text("preserve this")
    if collision.startswith("command"):
        s["bin"].mkdir()
        if collision.endswith("symlink"):
            command_path(s).symlink_to(untouched)
        else:
            command_path(s).write_text("someone else's command")
    else:
        target = s["install"] / "releases" / VERSION
        target.parent.mkdir(parents=True)
        if collision.endswith("symlink"):
            target.symlink_to(untouched)
        else:
            target.mkdir()
            (target / "personal.txt").write_text("unknown release directory")
    result = run(s, "--no-path")
    assert result.returncode == 5, result.stderr
    assert untouched.read_text() == "preserve this"
    if collision == "command":
        assert command_path(s).read_text() == "someone else's command"
    assert not Path(s["env"]["TEST_CURL_LOG"]).exists()


@pytest.mark.parametrize("bad_path", [
    "../escaped.txt", "/tmp/escaped.txt", "claude-ao3/../../escaped.txt", "other/file",
    "claude-ao3/_internal/fixture.txt", "claude-ao3/.claude-ao3-release",
])
def test_unsafe_archive_paths_rejected_before_install(sandbox, bad_path):
    s = sandbox
    archive(s, bad_path=bad_path)
    original = old_managed_command(s)
    assert run(s, "--no-path").returncode == 4
    assert command_path(s).read_bytes() == original
    assert not (s["install"] / "releases" / VERSION).exists()


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_archive_links_rejected_by_dereferenced_release_contract(sandbox, link):
    s = sandbox
    archive(s, **{link: True})
    assert run(s, "--no-path").returncode == 4
    assert not command_path(s).exists()


@pytest.mark.parametrize("values", [
    {"TEST_OS": "Linux"}, {"TEST_ARCH": "x86_64"}, {"TEST_MACOS": "12.7.6"},
])
def test_unsupported_system_is_explicit_and_download_not_attempted(sandbox, values):
    s = sandbox
    archive(s)
    s["env"].update(values)
    assert run(s, "--no-path").returncode == 3
    assert not Path(s["env"]["TEST_CURL_LOG"]).exists()


def test_rosetta_on_apple_silicon_is_supported(sandbox):
    s = sandbox
    archive(s)
    s["env"].update(TEST_ARCH="x86_64", TEST_TRANSLATED="1")
    assert run(s, "--no-path").returncode == 0


def test_zsh_path_update_only_replaces_own_block(sandbox):
    s = sandbox
    archive(s)
    profile = s["home"] / ".zshrc"
    profile.write_text('# user prefix\n# >>> Claude-ao3 PATH >>>\nexport PATH=/old/path:"$PATH"\n# <<< Claude-ao3 PATH <<<\nexport USER_SETTING=retained\n')
    profile.chmod(0o640)
    assert run(s).returncode == 0
    text = profile.read_text()
    assert text.startswith("# user prefix\n")
    assert "export USER_SETTING=retained\n" in text
    assert "/old/path" not in text
    assert text.count("# >>> Claude-ao3 PATH >>>") == 1
    assert profile.stat().st_mode & 0o777 == 0o640
    assert not (s["home"] / ".bashrc").exists()


def test_default_paths_and_no_path_do_not_write_profiles(sandbox):
    s = sandbox
    archive(s)
    profiles = [s["home"] / name for name in (".zshrc", ".bashrc", ".bash_profile")]
    for p in profiles:
        p.write_text("retain my own settings\n")
    assert run(s, "--no-path", default_paths=True).returncode == 0
    assert (s["home"] / ".local/bin/claude-ao3").is_file()
    assert (s["home"] / ".local/share/claude-ao3/releases/0.6.0/claude-ao3").is_file()
    assert all(p.read_text() == "retain my own settings\n" for p in profiles)


def test_download_enforces_https_and_bounded_curl(sandbox):
    s = sandbox
    archive(s)
    assert run(s, "--no-path").returncode == 0
    calls = [json.loads(line) for line in Path(s["env"]["TEST_CURL_LOG"]).read_text().splitlines()]
    assert len(calls) == 2
    for args in calls:
        assert args[0] == "-q"
        assert args[args.index("--proto") + 1] == "=https"
        assert args[args.index("--proto-redir") + 1] == "=https"
        assert "--tlsv1.2" in args and "--insecure" not in args and "-k" not in args
        assert int(args[args.index("--connect-timeout") + 1]) <= 15
        assert int(args[args.index("--max-time") + 1]) <= 180
        assert int(args[args.index("--retry") + 1]) <= 2


@pytest.mark.parametrize("profile_kind", ["symlink", "incomplete"])
def test_unusual_profile_is_preserved_without_invalidating_install(sandbox, profile_kind):
    s = sandbox
    archive(s)
    profile = s["home"] / ".zshrc"
    original = "# >>> Claude-ao3 PATH >>>\nexport KEEP=yes\n"
    if profile_kind == "symlink":
        external = s["root"] / "dotfile"
        external.write_text(original)
        profile.symlink_to(external)
    else:
        profile.write_text(original)
    assert run(s).returncode == 0
    assert profile.read_text() == original
    assert command_path(s).is_file()
    if profile_kind == "symlink":
        assert profile.is_symlink()


def test_bash_updates_only_selected_login_profile_and_rc(sandbox):
    s = sandbox
    archive(s)
    s["env"]["SHELL"] = "/bin/bash"
    login = s["home"] / ".bash_login"
    login.write_text("# login settings\n")
    assert run(s).returncode == 0
    assert login.read_text().startswith("# login settings\n")
    assert "Claude-ao3 PATH" in (s["home"] / ".bashrc").read_text()
    assert not (s["home"] / ".bash_profile").exists()
    assert not (s["home"] / ".zshrc").exists()


def test_launch_without_controlling_terminal_reports_installed(sandbox):
    s = sandbox
    archive(s)
    result = subprocess.run([
        "/bin/sh", str(SCRIPT), "--install-root", str(s["install"]),
        "--bin-dir", str(s["bin"]), "--launch", "--no-path",
    ], env=s["env"], capture_output=True, text=True, start_new_session=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "没有交互终端，未启动" in result.stdout
    assert command_path(s).is_file()


def test_missing_shell_variable_still_installs(sandbox):
    s = sandbox
    archive(s)
    s["env"].pop("SHELL", None)
    assert run(s).returncode == 0


def test_piped_installer_launches_with_three_terminal_streams_and_live_input(sandbox):
    s = sandbox
    archive(s, binary=b'''#!/bin/sh
[ -t 0 ] && [ -t 1 ] && [ -t 2 ] || exit 91
printf 'BINARY_TTY_READY\\n'
IFS= read -r answer
printf 'BINARY_INPUT=%s\\n' "$answer"
''')
    pid, fd = pty.fork()
    if pid == 0:
        try:
            tty = os.open("/dev/tty", os.O_RDWR)
            os.close(tty)
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                os._exit(77)  # Sandbox cannot open a terminal; not a product failure.
            raise
        os.execve("/bin/sh", ["/bin/sh", "-c",
                  'cat "$1" | /bin/sh -s -- --install-root "$2" --bin-dir "$3" --no-path --launch',
                  "installer-test", str(SCRIPT), str(s["install"]), str(s["bin"])], s["env"])
    received = bytearray()
    sent = False
    status = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                received.extend(data)
                if not sent and b"BINARY_TTY_READY" in received:
                    os.write(fd, b"literal keyboard input\n")
                    sent = True
            done, child_status = os.waitpid(pid, os.WNOHANG)
            if done:
                status = child_status
                break
        if status == 77 << 8:
            pytest.skip("environment denies opening /dev/tty; run PTY acceptance outside sandbox")
        assert status == 0, received.decode(errors="replace")
        assert b"BINARY_INPUT=literal keyboard input" in received
        assert not (s["install"] / ".install-lock").exists()
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
