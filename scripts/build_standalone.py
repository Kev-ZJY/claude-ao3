#!/usr/bin/env python3
"""Freeze the existing reader as a macOS arm64 onedir application.

Run with .venv/bin/python scripts/build_standalone.py. Product dependencies are
installed from uv.lock in an isolated build environment. PyInstaller 6.22.3
supports Python 3.14; its official changelog records support beginning in 6.15.
The output needs no separately installed Python. It remains a console program;
the SwiftTerm application wraps this directory without changing the reader.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK = ROOT / "output/macos-build-work"
BUILD_TOOLS = (
    "pyinstaller==6.22.3", "pyinstaller-hooks-contrib==2026.7", "altgraph==0.17.5",
    "macholib==1.16.4", "packaging==26.3", "setuptools==84.0.0",
)
RUNTIME_VERSION = "3.14.5"
RUNTIME_ARCHIVE_SHA256 = "3a0373cc39fefd494754ef555267f245c720cddbaaabf63a7c9a4269f1e56532"
RUNTIME_RELEASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/tag/20260602"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, report) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(argv, cwd, env, report, run_dir, label, timeout=300):
    began = time.monotonic()
    process = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=timeout)
    log = run_dir / f"{label}.log"
    log.write_text(process.stdout + process.stderr, encoding="utf-8")
    report["steps"].append({"label": label, "command": [str(arg) for arg in argv],
                            "exit_code": process.returncode,
                            "seconds": round(time.monotonic() - began, 3), "log": str(log)})
    save(run_dir / "build-report.json", report)
    print(f"{label}: exit {process.returncode}", flush=True)
    if process.returncode:
        raise RuntimeError(f"{label} failed; see {log}")
    return process


def collect_notices(output: Path, requirements: Path) -> None:
    """Run under the build interpreter so notices match installed dependencies."""
    import importlib.metadata as metadata
    import sysconfig
    from packaging.requirements import Requirement

    names = {"pyinstaller"}
    for line in requirements.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate():
                names.add(requirement.name)
    sections = [
        "Claude 凹3 — third-party notices\n"
        "This distribution embeds Python and the dependencies listed below.\n"
        "PyInstaller's bootloader uses its published distribution exception.\n"
        "These notices do not imply endorsement by their authors.\n",
    ]
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python's installed LICENSE.txt was not found")
    sections.append(f"Python {platform.python_version()}\n" + python_license.read_text())
    missing = []
    for name in sorted(names):
        distribution = metadata.distribution(name)
        meta = distribution.metadata
        block = [f"{meta['Name']} {distribution.version}",
                 "License: " + (meta.get("License-Expression") or meta.get("License") or "see text"),
                 "Homepage: " + (meta.get("Home-page") or meta.get("Project-URL") or "see package metadata")]
        found = []
        for entry in distribution.files or []:
            filename = Path(str(entry)).name.lower()
            if filename.startswith(("license", "copying", "notice")):
                path = Path(distribution.locate_file(entry))
                if path.is_file() and path.stat().st_size < 1_000_000:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if text not in found:
                        found.append(text)
        if not found:
            missing.append(name)
        sections.append("\n".join(block + found))
    if missing:
        raise RuntimeError("Dependency license text missing: " + ", ".join(missing))
    # python-build-standalone statically links several native dependencies.
    # Include its exact release license texts even when no separate dylib is
    # present. Never substitute unrelated licenses from the builder's Homebrew.
    runtime_notices = ROOT / "native/python-runtime-notices.txt"
    sections.append(runtime_notices.read_text(encoding="utf-8"))
    (output / "THIRD_PARTY_NOTICES.txt").write_text(
        ("\n\n" + "=" * 76 + "\n\n").join(sections), encoding="utf-8")


def validate_symlinks(output: Path) -> None:
    root = output.resolve()
    for path in output.rglob("*"):
        if path.is_symlink():
            target = path.resolve(strict=True)
            if target != root and root not in target.parents:
                raise RuntimeError(f"Runtime symlink escapes the distribution: {path}")


def verify_deployment_targets(output: Path, report, run_dir, env) -> None:
    magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe",
             b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    native = set()
    for path in output.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                if handle.read(4) in magic:
                    native.add(path.resolve())
    results = []
    for index, path in enumerate(sorted(native)):
        checked = command(["/usr/bin/otool", "-l", path], ROOT, env, report, run_dir,
                          f"deployment-target-{index:02d}")
        versions = re.findall(r"\bminos (\d+(?:\.\d+){0,2})", checked.stdout)
        versions += re.findall(r"LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+){0,2})",
                               checked.stdout)
        if not versions:
            raise RuntimeError(f"Cannot establish native deployment target: {path.name}")
        if any(tuple(map(int, version.split("."))) > (13, 0, 0) for version in versions):
            raise RuntimeError(f"Native binary requires macOS newer than 13: {path.name}: {versions}")
        linked = command(["/usr/bin/otool", "-L", path], ROOT, env, report, run_dir,
                         f"native-linkage-{index:02d}")
        dependencies = [line.strip().split(" (", 1)[0] for line in linked.stdout.splitlines()[1:]]
        if any(not dependency.startswith(("@", "/usr/lib/", "/System/Library/"))
               for dependency in dependencies):
            raise RuntimeError(f"Native binary links to a nonportable library: {path.name}: {dependencies}")
        results.append({"file": path.relative_to(output).as_posix(), "minos": versions,
                        "dependencies": dependencies})
    report["native_deployment_targets"] = results
    report["deployment_target_check"] = "All bundled Mach-O binaries declare minimum macOS <=13.0; older OS runtime tests are separate"


def make_archive(output: Path, target: Path) -> None:
    temporary = target.with_suffix(".tmp")
    def public_metadata(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT, dereference=True) as archive:
        # The native app preserves framework symlinks. The independently
        # installable CLI archive materializes their contents so its POSIX
        # installer can reject every symlink/hardlink archive entry.
        archive.add(output, arcname="claude-ao3", recursive=True, filter=public_metadata)
    with tarfile.open(temporary) as archive:
        if any(not (member.isfile() or member.isdir())
               for member in archive.getmembers()):
            raise RuntimeError("Unsupported member type in standalone archive")
    os.replace(temporary, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_WORK / "reader")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--codesign-identity", help="Apple Developer ID; default is ad-hoc signing")
    parser.add_argument("--python-runtime", type=Path, help="Explicit redistributable Python interpreter; default downloads pinned CPython into the work directory")
    parser.add_argument("--skip-bootstrap", action="store_true", help="Reuse an already prepared isolated build venv")
    parser.add_argument("--notices-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-requirements", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.notices_output:
        collect_notices(args.notices_output, args.runtime_requirements)
        return 0
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("This build target requires native macOS arm64; it is not universal2 or cross-compiled")
    output, work = args.output.expanduser().absolute(), args.work_dir.expanduser().absolute()
    if output == ROOT or output == work or output in ROOT.parents:
        parser.error("Choose a dedicated output directory")
    work.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="standalone-run-", dir=work))
    build_venv = work / "build-venv"
    python = build_venv / "bin/python"
    env = dict(os.environ)
    env.update(UV_CACHE_DIR=str(work / "uv-cache"),
               UV_PROJECT_ENVIRONMENT=str(build_venv),
               PYINSTALLER_CONFIG_DIR=str(work / "pyinstaller-cache"))
    report = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "architecture": "arm64", "platform": platform.platform(),
              "python": platform.python_version(), "output": str(output),
              "codesign_identity": args.codesign_identity or "ad-hoc",
              "notarized": False, "steps": [],
              "pyinstaller_reference": "https://pyinstaller.org/en/stable/CHANGES.html"}
    try:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is required to install frozen product dependencies")
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        report["version"] = version
        sources = sorted((ROOT / "src/mini_notes").glob("*.py"))
        source_hashes = {p.relative_to(ROOT).as_posix(): sha(p) for p in sources}
        report["source_sha256"] = source_hashes
        build_inputs = [ROOT / path for path in ("pyproject.toml", "uv.lock",
                        "native/reader_entry.py", "native/python-runtime-notices.txt",
                        "scripts/build_standalone.py")]
        report["build_inputs_sha256"] = {p.relative_to(ROOT).as_posix(): sha(p) for p in build_inputs}
        report["uv_lock_sha256"] = sha(ROOT / "uv.lock")
        requirements = run_dir / "runtime-requirements.txt"
        command([uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                 "--format", "requirements-txt", "--output-file", requirements],
                ROOT, env, report, run_dir, "export-frozen-runtime")
        if not args.skip_bootstrap:
            runtime_root = work / "python-runtimes"
            if args.python_runtime:
                runtime = args.python_runtime.expanduser().absolute()
            else:
                command([uv, "python", "install", RUNTIME_VERSION, "--install-dir", runtime_root,
                         "--no-bin"], ROOT, env, report, run_dir, "download-isolated-python")
                runtime = runtime_root / f"cpython-{RUNTIME_VERSION}-macos-aarch64-none/bin/python3.14"
            command([uv, "venv", "--clear", "--python", runtime, build_venv],
                    ROOT, env, report, run_dir, "create-isolated-build-venv")
            command([uv, "sync", "--frozen", "--no-dev", "--inexact", "--no-python-downloads",
                     "--python", runtime], ROOT, env, report, run_dir, "sync-frozen-runtime")
            command([uv, "pip", "install", "--python", python, *BUILD_TOOLS],
                    ROOT, env, report, run_dir, "install-build-tools")
        packages = command([uv, "pip", "list", "--python", python, "--format", "json"],
                           ROOT, env, report, run_dir, "build-environment-packages")
        report["build_environment"] = json.loads(packages.stdout)
        runtime_info = command([python, "-c", "import json,sys; print(json.dumps({'version':sys.version,'base_prefix':sys.base_prefix}))"],
                               ROOT, env, report, run_dir, "python-runtime-info")
        report["python_runtime"] = json.loads(runtime_info.stdout)
        runtime_library = Path(report["python_runtime"]["base_prefix"]) / "lib/libpython3.14.dylib"
        # uv relocates this dylib's install name to the chosen runtime folder,
        # so the installed library digest legitimately differs by build path.
        if not runtime_library.is_file() or not report["python_runtime"]["version"].startswith(
                RUNTIME_VERSION + " (main, Jun  2 2026"):
            raise RuntimeError("Use the audited python-build-standalone CPython 3.14.5 arm64 "
                               "release 20260602 runtime; updating Python also requires a license review")
        report["python_runtime"].update(release=RUNTIME_RELEASE_URL,
                                        upstream_install_archive_sha256=RUNTIME_ARCHIVE_SHA256,
                                        library_sha256=sha(runtime_library))
        distpath = run_dir / "dist"
        argv = [python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
                "--name", "claude-ao3", "--target-architecture", "arm64",
                "--distpath", distpath, "--workpath", run_dir / "analysis",
                "--specpath", run_dir, "--paths", ROOT / "src", "--contents-directory", "_internal"]
        for package in ("textual", "rich", "pygments", "certifi", "platformdirs"):
            argv.extend(("--collect-all", package))
        for package in ("httpx", "httpcore", "anyio"):
            argv.extend(("--collect-submodules", package))
        for package in ("textual", "rich", "httpx", "httpcore", "anyio", "platformdirs", "beautifulsoup4"):
            argv.extend(("--copy-metadata", package))
        for package in ("pytest", "tkinter", "IPython", "textual_dev"):
            argv.extend(("--exclude-module", package))
        # The existing --doctor reports hashes by reading its own .py files.
        # Include only the 13 product modules, never tests/fixtures/build helpers.
        for path in sources:
            argv.extend(("--add-data", str(path) + ":mini_notes"))
        if args.codesign_identity:
            argv.extend(("--codesign-identity", args.codesign_identity))
        argv.append(ROOT / "native/reader_entry.py")
        command(argv, ROOT, env, report, run_dir, "pyinstaller-freeze", timeout=600)
        built = distpath / "claude-ao3"
        command([python, Path(__file__).resolve(), "--notices-output", built,
                 "--runtime-requirements", requirements], ROOT, env, report, run_dir, "license-notices")
        validate_symlinks(built)
        verify_deployment_targets(built, report, run_dir, env)
        # PyInstaller signs the collected framework binary individually. Seal
        # the reconstructed framework resources as a bundle before strict
        # validation, preserving the ordinary macOS signing model.
        frameworks = sorted(built.rglob("*.framework"), key=lambda p: len(p.parts), reverse=True)
        for index, framework in enumerate(frameworks):
            signing = ["/usr/bin/codesign", "--force", "--sign", args.codesign_identity or "-"]
            signing += ["--options", "runtime"] if args.codesign_identity else ["--timestamp=none"]
            command([*signing, framework], ROOT, env, report, run_dir, f"seal-framework-{index:02d}")
        executable = built / "claude-ao3"
        architecture = command(["/usr/bin/file", executable], ROOT, env, report, run_dir, "binary-architecture")
        if "arm64" not in architecture.stdout or "universal" in architecture.stdout:
            raise RuntimeError("Unexpected executable architecture")
        command(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", executable],
                ROOT, env, report, run_dir, "verify-executable-signature")
        dylibs = sorted({p.resolve() for p in built.rglob("*") if p.is_file()
                         and (p.suffix in {".dylib", ".so"} or p.name == "Python")})
        for index, path in enumerate(dylibs):
            command(["/usr/bin/codesign", "--verify", "--strict", path],
                    ROOT, env, report, run_dir, f"verify-runtime-signature-{index:02d}")
        unrelated = run_dir / "unrelated-cwd"
        unrelated.mkdir()
        runtime_env = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "LANG": "en_US.UTF-8"}
        for flag in ("--version", "--help", "--network-info"):
            result = command([executable, flag], unrelated, runtime_env, report, run_dir,
                             "standalone-" + flag.removeprefix("--"), timeout=20)
            if flag == "--version" and version not in result.stdout:
                raise RuntimeError("Frozen version does not match project version")
            if flag == "--network-info":
                info = json.loads(result.stdout)
                if info["selected_client"] != "curl" or info["network"] != "direct":
                    raise RuntimeError("Unexpected standalone default network configuration")
        if {p.relative_to(ROOT).as_posix(): sha(p) for p in sources} != source_hashes:
            raise RuntimeError("Reader source changed during the build; rerun")
        if {p.relative_to(ROOT).as_posix(): sha(p) for p in build_inputs} != report["build_inputs_sha256"]:
            raise RuntimeError("Build inputs changed during the build; rerun")
        if output.exists():
            if not (output / "claude-ao3").is_file() or not (output / "_internal").is_dir():
                raise RuntimeError("Refusing to replace an unrecognized output directory")
            shutil.move(str(output), str(run_dir / "previous-reader"))
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(built), str(output))
        archive = work / f"claude-ao3-{version}-macos-arm64.tar.gz"
        make_archive(output, archive)
        report.update(completed=True, reader_executable_sha256=sha(output / "claude-ao3"),
                      archive=str(archive), archive_sha256=sha(archive),
                      archive_bytes=archive.stat().st_size,
                      source_unchanged=True,
                      pty_acceptance="Run separately with a synthetic session; not established by CLI checks")
        save(run_dir / "build-report.json", report)
        print(json.dumps({"reader": str(output), "archive": str(archive),
                          "bytes": archive.stat().st_size, "sha256": sha(archive),
                          "report": str(run_dir / "build-report.json")}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        report.update(completed=False, error=type(exc).__name__ + ": " + str(exc))
        save(run_dir / "build-report.json", report)
        print(report["error"], file=sys.stderr)
        print("Build report: " + str(run_dir / "build-report.json"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
