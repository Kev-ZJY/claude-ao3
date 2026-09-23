#!/usr/bin/env python3
"""Install the built source archive and wheel into fresh environments (requires uv)."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
import tomllib

from build_release import release_files

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
work = Path(tempfile.mkdtemp(prefix='claude-ao3-release-'))
environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'UV_NO_CACHE': '1'}
for setting in ('VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT', 'PYTHONPATH', 'PYTHONHOME'):
    environment.pop(setting, None)
report = {'version': version, 'generated_at': datetime.now(timezone.utc).isoformat(),
          'dependency_cache': 'uv download cache disabled; all installed environments are temporary. Existing Python interpreter is shared and excluded from installed footprint.', 'checks': [], 'all_passed': False}

def footprint(directory):
    files = [p for p in directory.rglob('*') if p.is_file() and not p.is_symlink()]
    unique = {(p.stat().st_dev, p.stat().st_ino): p.stat() for p in files}
    return {'files': len(files), 'logical_bytes': sum(p.stat().st_size for p in files),
            'allocated_unique_bytes': sum(s.st_blocks * 512 for s in unique.values())}

def run(name, args, cwd=root, timeout=100):
    t = time.perf_counter()
    try:
        p = subprocess.run([str(x) for x in args], cwd=cwd, env=environment,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = ''.join(value.decode(errors='replace') if isinstance(value, bytes) else (value or '') for value in (exc.stdout, exc.stderr))
        report['checks'].append({'name': name, 'command': [str(x).replace(str(root), '<workspace>') for x in args],
            'exit_code': None, 'timeout_seconds': timeout, 'output': output.replace(str(root), '<workspace>')})
        raise
    report['checks'].append({'name': name, 'command': [str(x).replace(str(root), '<workspace>') for x in args],
        'exit_code': p.returncode, 'seconds': round(time.perf_counter()-t,3),
        'output': (p.stdout+p.stderr).replace(str(root), '<workspace>')})
    print(name, p.returncode, flush=True)
    if p.returncode:
        raise RuntimeError((p.stdout+p.stderr)[-3000:])
    return p.stdout

try:
    archive = root / 'dist' / f'claude-ao3-{version}-source.zip'
    with zipfile.ZipFile(archive) as z:
        expected = {f'claude-ao3-{version}/{path.relative_to(root).as_posix()}': path
                    for path in release_files()}
        assert len(z.namelist()) == len(expected), 'Duplicate or missing source archive entries'
        assert set(z.namelist()) == set(expected), 'Source archive does not match the current allowlist'
        for name, path in expected.items():
            assert z.read(name) == path.read_bytes(), f'Source archive differs from current file: {name}'
        z.extractall(work)
    source = work / f'claude-ao3-{version}'
    for name in ['claude-ao3', 'mini-notes', 'install.sh', 'scripts/install.sh']:
        (source/name).chmod(0o755)
    run('source_install_script', ['./scripts/install.sh', '--bin-dir', str(work/'bin'), '--no-path'], source)
    assert not (source/'.cache/uv').exists(), 'Installer must not retain download cache'
    assert run('source_command', [work/'bin/claude-ao3', '--version'], work).strip() == f'Claude 凹3 (claude-ao3) {version}'
    venv = work / 'wheel-env'
    run('fresh_wheel_environment', ['uv', '--no-cache', 'venv', venv])
    wheel = root / 'dist' / f'claude_ao3-{version}-py3-none-any.whl'
    report['package_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (archive, wheel)}
    run('wheel_install', ['uv', '--no-cache', 'pip', 'install', '--python', venv/'bin/python', wheel])
    assert run('wheel_command', [venv/'bin/claude-ao3', '--version'], work).strip() == f'Claude 凹3 (claude-ao3) {version}'
    smoke = work/'smoke.py'
    smoke.write_text('''import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
from mini_notes.app import MiniNotesApp
from mini_notes.engine import ReaderEngine
from mini_notes.models import Chapter, SearchFilters, WorkSummary, Page
from mini_notes.storage import StateStore
from importlib.metadata import version

class Source:
    base_url = 'https://reader.invalid'
    async def recent(self, **kwargs):
        return Page([], self.base_url+'/works')
    async def aclose(self):
        pass

async def main():
    assert version('claude-ao3') == '__PACKAGE_VERSION__'
    with tempfile.TemporaryDirectory() as temp:
        store = StateStore(Path(temp))
        work = WorkSummary('fixture', 'https://reader.invalid/works/1', 'Original smoke fixture')
        chapter = Chapter('fixture', work.url, 'Original', ['原创安装验证文本，检查原生布局。']*30, work)
        store.save({'schema_version':1, 'current':asdict(chapter), 'filters':asdict(SearchFilters()),
                    'flow':{'active_kind':'recent','recent':{'kind':'recent','filters':{},'items':[],
                            'index':0,'url':'https://reader.invalid/works','next_url':None}},
                    'position':{},'history':[],'history_index':-1})
        engine = ReaderEngine(Source(), store)
        app = MiniNotesApp(engine)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            assert engine.current and app.reader.paragraphs
            await pilot.press('ctrl+g')
            assert app.masked
            await pilot.press('ctrl+g')
            assert not app.masked
            await pilot.press('ctrl+c')
            assert engine._closed
        store.close()
    print('Installed wheel mounts original Chinese body, masks/restores and exits; no external requests')
asyncio.run(main())
'''.replace('__PACKAGE_VERSION__', version))
    run('installed_wheel_tui_smoke', [venv/'bin/python', smoke], work)
    report['installed_footprint'] = {
        'scope': 'Fresh no-dev environments; no symlink traversal into shared Python; includes bytecode produced by smoke run; not a bound for all operating systems.',
        'source_environment': footprint(source/'.venv'),
        'source_directory_including_environment': footprint(source),
        'wheel_environment': footprint(venv),
        'source_download_cache_retained': False,
    }
    report['code_sha256'] = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'src/mini_notes').glob('*.py'))}
    with zipfile.ZipFile(wheel) as z:
        report['wheel_files'] = z.namelist()
        for p in (root/'src/mini_notes').glob('*.py'):
            assert z.read('mini_notes/'+p.name) == p.read_bytes()
    with zipfile.ZipFile(archive) as z:
        report['source_file_count'] = len(z.namelist())
        for name in z.namelist():
            relative = Path(name).relative_to(f'claude-ao3-{version}')
            assert not {'private', '待删除', '.venv', '.cache', '__pycache__'}.intersection(relative.parts)
            assert z.read(name) == (root/relative).read_bytes(), name
    # The shipped tests must remain runnable without old research/acceptance files.
    run('source_dev_dependencies', ['uv', '--no-cache', 'sync', '--frozen', '--no-python-downloads'], source)
    run('source_full_tests', [source/'.venv/bin/python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider'], source, timeout=300)
    run('source_lint', [source/'.venv/bin/python', '-m', 'ruff', 'check', '--no-cache', 'src', 'tests', 'scripts'], source)
    run('source_lifecycle_pty', [source/'.venv/bin/python', 'scripts/lifecycle_acceptance.py'], source)
    lifecycle = json.loads((source/'artifacts/lifecycle-v4.json').read_text())
    report['lifecycle'] = lifecycle
    assert lifecycle['ok'] is True
    report['tui_scope'] = 'Installed wheel uses a headless Textual fixture; installed source also runs an original offline OS PTY/SIGHUP fixture. Native app window and frozen CLI checks are separate; any pytest skips remain visible in source_full_tests output.'
    report['all_passed'] = True
except Exception as exc:
    report['error'] = {'type': type(exc).__name__, 'message': str(exc).replace(str(root), '<workspace>')}
    raise
finally:
    (root/'dist').mkdir(exist_ok=True)
    (root/'dist/package-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    shutil.rmtree(work)
