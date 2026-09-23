#!/usr/bin/env python3
"""Real production source/engine acceptance; retain only metadata, never remote prose.

Explicitly run: PYTHONPATH=src .venv/bin/python scripts/live_network_v41.py
This performs bounded anonymous AO3 GET requests using the actual default transport.
"""
from __future__ import annotations
import asyncio
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from mini_notes.cli import VERSION
from mini_notes.app import MiniNotesApp
from mini_notes.engine import ReaderEngine
from mini_notes.models import SearchFilters, matches_filters
from mini_notes.source import AO3Source, SourceError
from mini_notes.network_diagnostics import network_configuration

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts' / ('live-network-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')

class EphemeralStore:
    def __init__(self):
        self.state, self.cache = {}, {}
    def load(self):
        return copy.deepcopy(self.state)
    def save(self, value):
        self.state = copy.deepcopy(value)
    def cache_get(self, key):
        return copy.deepcopy(self.cache.get(key))
    def cache_put(self, key, value, ttl=86400):
        self.cache[key] = copy.deepcopy(value)
    def clear_cache(self):
        self.cache.clear()
    def close(self):
        self.state.clear()
        self.cache.clear()

async def main(network="direct"):
    report = {'version': VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Real AO3 Chinese public search and engine navigation; body never persisted. Does not independently verify country or VPN/TUN status.',
              'network_configuration': network_configuration(network),
              'mainland_direct_verified': False,
              'budget': {'max_http_requests_per_run': 18, 'operation_seconds': 45,
                         'source_retries': 0, 'app_retry_attempts': 3, 'queries': 2}, 'runs': []}
    report['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / 'src/mini_notes').glob('*.py'))}
    def save():
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    for query in ('温暖', '旅行'):
        row = {'query': query, 'steps': [], 'requests': []}
        report['runs'].append(row)
        source = AO3Source(max_retries=0, network=network)
        row['transport'] = source.transport_name
        began = 0.0
        attempts = 0
        async def request(req):
            nonlocal began, attempts
            attempts += 1
            if attempts > 18:
                raise SourceError('acceptance_budget', '验收请求数达到预算。')
            began = time.perf_counter()
        async def response(res):
            row['requests'].append({'path': res.request.url.path, 'status': res.status_code,
                                    'protocol': res.http_version,
                                    'milliseconds': round((time.perf_counter()-began)*1000, 1)})
            save()
        source._client.event_hooks.update(request=[request], response=[response])
        store = EphemeralStore()
        engine = ReaderEngine(source, store)
        ui = MiniNotesApp(engine)
        filters = SearchFilters(query=query, language='zh', warnings=['16'], categories=['21'], rating='10')
        async def step(name, operation):
            started = time.perf_counter()
            value = None
            async def capture():
                nonlocal value
                value = await operation()
            await ui._run_with_retries(capture)  # Exactly the production App recovery policy.
            detail = {'step': name, 'milliseconds': round((time.perf_counter()-started)*1000, 1)}
            detail['app_attempt'] = ui._progress['attempt']
            if hasattr(value, 'items'):
                detail.update(items=len(value.items))
            elif hasattr(value, 'paragraphs'):
                assert value.paragraphs and matches_filters(value.work, filters)
                detail.update(work_id=value.work.id, chapter_id=value.id,
                              characters=sum(map(len, value.paragraphs)), chapter=value.position)
            row['steps'].append(detail)
            print(json.dumps({'query': query, **detail}, ensure_ascii=False), flush=True)
            save()
            return value
        try:
            results = await step('search', lambda: engine.search(filters))
            assert results.items
            selected = next((i for i,w in enumerate(results.items) if (w.chapter_count or 0) > 1), 0)
            first = await step('open_result', lambda: engine.open_result(selected))
            original = (first.work.id, first.id)
            second = await step('next', engine.next)
            assert (second.work.id, second.id) != original
            restored = await step('previous', engine.previous)
            assert (restored.work.id, restored.id) == original
            # At least one real background cycle and then a cached transition.
            # The source enforces a single in-flight request and all same filters.
            prep = asyncio.create_task(engine.prefetch())
            await asyncio.sleep(3)
            await step('next_during_prefetch', engine.next)
            skipped = await step('skip_work', engine.skip_work)
            assert skipped.work.id != original[0]
            engine.pause_prefetch()
            await prep
            row['ok'] = True
        except Exception as exc:
            row['ok'] = False
            row['error'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
                            'status': getattr(exc, 'http_status', None),
                            'failure_kind': getattr(exc, 'failure_kind', None)}
        finally:
            await engine.close()
            row['health'] = source.health
            store.close()
            save()
    report['ok'] = all(row['ok'] for row in report['runs'])
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({'ok': report['ok'], 'runs': len(report['runs']), 'output': str(OUTPUT)}), flush=True)
    return 0 if report['ok'] else 2

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', choices=('direct', 'system'), default='direct')
    raise SystemExit(asyncio.run(main(parser.parse_args().network)))
