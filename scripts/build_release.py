#!/usr/bin/env python3
"""Build the current wheel and source bundle from a reviewed file allowlist."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {
    'private', '待删除', '.venv', '.cache', '__pycache__', '.pytest_cache', '.ruff_cache',
    '.build', 'artifacts', 'reports', 'userdata', 'docs', 'research', 'output',
}
REQUIRED_FILES = (
    'README.md', 'pyproject.toml', 'uv.lock', 'claude-ao3', 'mini-notes', 'install.sh',
    '.gitignore', 'scripts/install.sh', 'scripts/install_command.py',
    'scripts/build_release.py', 'scripts/package_acceptance.py',
    'scripts/build_standalone.py', 'scripts/build_macos_app.py',
    # Imported by a synthetic regression test; no historical reports are shipped.
    'scripts/live_network_v41.py', 'scripts/lifecycle_acceptance.py',
    'scripts/pty_acceptance.py',
    'native/reader_entry.py', 'native/python-runtime-notices.txt',
    'native/macos/Package.swift', 'native/macos/Package.resolved',
    'native/macos/Sources/ClaudeAO3/main.swift', 'native/branding/DrawIcon.swift',
    'web/index.html', 'web/app.js', 'web/style.css', 'web/media/favicon.svg',
    'web/media/app-icon.png', 'web/media/poster.png', 'web/media/terminal-demo.mp4',
    'web/media/captions.vtt',
    'tests/test_app.py', 'tests/test_cli.py', 'tests/test_engine.py',
    'tests/test_binary_installer.py', 'tests/fixtures/ao3_chapter.html',
    'tests/fixtures/ao3_single_work.html', 'tests/fixtures/ao3_listing.html',
    'tests/fixtures/ao3_series.html',
)
SOURCE_MODULES = (
    '__init__', '__main__', 'app', 'cli', 'curl_transport', 'engine', 'models',
    'network_diagnostics', 'rendering', 'source', 'storage', 'storage_policy',
    'terminal_theme',
)


def release_files() -> list[Path]:
    files = {ROOT / name for name in REQUIRED_FILES}
    files.update(ROOT / f'src/mini_notes/{name}.py' for name in SOURCE_MODULES)
    for pattern in ('src/mini_notes/*.py', 'tests/test_*.py', 'tests/fixtures/*.html',
                    'native/macos/Sources/**/*.swift', 'native/macos/Tests/**/*.swift'):
        files.update(path for path in ROOT.glob(pattern) if path.is_file() or path.is_symlink())
    for path in files:
        relative = path.relative_to(ROOT)
        if FORBIDDEN_PARTS.intersection(relative.parts):
            raise RuntimeError(f'Private or generated file in release: {relative}')
        if not path.is_file() or any(part.is_symlink() for part in (path, *path.parents)
                                      if part != ROOT and ROOT in part.parents):
            raise RuntimeError(f'Missing or unsafe release file: {relative}')
    return sorted(files)


def main() -> None:
    project = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
    name, version = project['name'], project['version']
    files = release_files()  # Fail before building when the required source is incomplete.
    subprocess.run([sys.executable, '-m', 'hatchling', 'build', '-t', 'wheel'], cwd=ROOT, check=True)
    target = ROOT/'dist'/f'{name}-{version}-source.zip'
    temporary = target.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(ROOT)
            archive.write(path, f'{name}-{version}/{relative.as_posix()}')
    temporary.replace(target)
    wheel = ROOT/'dist'/f'{name.replace("-", "_")}-{version}-py3-none-any.whl'
    (ROOT/'dist/SHA256SUMS.txt').write_text(''.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in (wheel, target)
    ))
    print(f'{target.name}: {len(files)} files, {target.stat().st_size:,} bytes')


if __name__ == '__main__':
    main()
