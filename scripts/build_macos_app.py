#!/usr/bin/env python3
"""Bundle the native terminal and standalone reader into an arm64 .app and DMG."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.kevzjy.claude-ao3"


def run(command, **kwargs):
    print("+", " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def reset_owned_app(path: Path):
    if not path.exists():
        return
    info = path / "Contents/Info.plist"
    if path.is_symlink() or not info.is_file():
        raise RuntimeError(f"Refusing to replace an unknown path: {path}")
    with info.open("rb") as stream:
        if plistlib.load(stream).get("CFBundleIdentifier") != BUNDLE_ID:
            raise RuntimeError("Existing app belongs to a different product")
    shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-dir", type=Path, default=ROOT / "output/macos-build-work/reader")
    parser.add_argument("--swift-bin", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "dist/macos")
    parser.add_argument("--sign-identity", default="-")
    parser.add_argument("--no-dmg", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        parser.error("This build target currently requires an Apple Silicon Mac")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    reader = args.reader_dir.resolve()
    if not (reader / "claude-ao3").is_file():
        parser.error("Build scripts/build_standalone.py first; standalone reader is missing")
    text = subprocess.check_output([str(reader / "claude-ao3"), "--version"], text=True)
    if version not in text:
        raise RuntimeError("Standalone reader version does not match this source")
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = ROOT / "output/macos-build-work"
    work.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["CLANG_MODULE_CACHE_PATH"] = str(work / "clang-cache")
    if args.swift_bin:
        executable = args.swift_bin.resolve()
    else:
        command = ["swift", "build", "--package-path", ROOT / "native/macos",
                   "--scratch-path", work / "swift-build", "--disable-sandbox", "-c", "release",
                   "--triple", "arm64-apple-macosx13.0"]
        run(command, env=environment)
        binary_dir = subprocess.check_output(list(map(str, command + ["--show-bin-path"])),
                                             env=environment, text=True).strip()
        executable = Path(binary_dir) / "ClaudeAO3Mac"
    if not executable.is_file():
        raise RuntimeError(f"Native app executable is missing: {executable}")
    # Assemble separately so a failed build cannot remove the last valid app.
    assembly = Path(tempfile.mkdtemp(prefix="app-assembly-", dir=work))
    app = assembly / "Claude 凹3.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(executable, contents / "MacOS/ClaudeAO3Mac")
    terminal_resources = executable.parent / "SwiftTerm_SwiftTerm.bundle"
    if not terminal_resources.is_dir():
        raise RuntimeError("SwiftTerm resource bundle is missing beside the native executable")
    shutil.copytree(terminal_resources, resources / terminal_resources.name)
    shutil.copytree(reader, resources / "reader", symlinks=True)
    iconset = work / "ClaudeAO3.iconset"
    run(["swift", "-module-cache-path", work / "icon-module-cache",
         ROOT / "native/branding/DrawIcon.swift", iconset], env=environment)
    run(["iconutil", "-c", "icns", iconset, "-o", resources / "ClaudeAO3.icns"])
    info = {
        "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "Claude 凹3",
        "CFBundleDisplayName": "Claude 凹3", "CFBundleExecutable": "ClaudeAO3Mac",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version,
        "CFBundleVersion": version, "CFBundleIconFile": "ClaudeAO3",
        "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication", "LSApplicationCategoryType": "public.app-category.books",
    }
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    # Include the license of the linked native terminal component.
    candidates = list((work / "swift-build/checkouts").glob("SwiftTerm/LICENSE*"))
    candidates += list((ROOT / "native/macos/.build/checkouts").glob("SwiftTerm/LICENSE*"))
    if not candidates:
        raise RuntimeError("SwiftTerm license must be included with the application")
    shutil.copy2(candidates[0], resources / "SwiftTerm-LICENSE.txt")
    signing = ["codesign", "--force", "--sign", args.sign_identity]
    if args.sign_identity != "-":
        signing += ["--options", "runtime", "--timestamp"]
        # Freeze the reader using this same identity before invoking this builder.
    run(signing + [contents / "MacOS/ClaudeAO3Mac"])
    run(signing + [app])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])
    destination = output / app.name
    reset_owned_app(destination)
    app.rename(destination)
    assembly.rmdir()
    app = destination
    print(f"Application: {app}", flush=True)
    if not args.no_dmg:
        with tempfile.TemporaryDirectory(prefix="claude-ao3-dmg-", dir=work) as temporary:
            staging = Path(temporary)
            shutil.copytree(app, staging / app.name, symlinks=True)
            (staging / "Applications").symlink_to("/Applications")
            dmg = output / f"Claude-AO3-{version}-macos-arm64.dmg"
            run(["hdiutil", "create", "-ov", "-volname", "Claude 凹3", "-srcfolder",
                 staging, "-format", "UDZO", dmg])
            run(["hdiutil", "verify", dmg])
            print(f"DMG: {dmg}\nSHA256: {hashlib.sha256(dmg.read_bytes()).hexdigest()}")
    if args.sign_identity == "-":
        print("Distribution status: ad-hoc signed preview; not Apple-notarized.")


if __name__ == "__main__":
    main()
