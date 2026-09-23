#!/usr/bin/env python3
"""Real OS PTY SIGHUP acceptance using only an original, offline resume fixture.

No source request is needed: the fixture's recent queue is explicitly exhausted.
Run: .venv/bin/python scripts/lifecycle_acceptance.py
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import tomllib

from mini_notes.models import Chapter, SearchFilters, WorkSummary
from mini_notes.storage import SessionInUseError, SessionLock, StateStore

import pty_acceptance as harness


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "artifacts" / "lifecycle-v4.json"
ORIGIN = "https://reader.invalid"
DRAFT = "lifecycle-unsaved-draft-v4"


def main():
    run = ROOT / "artifacts" / "private" / ("lifecycle-v4-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True, mode=0o700)
    state_dir = run / "state"
    namespace = hashlib.sha256(ORIGIN.encode()).hexdigest()[:16]
    db_dir = state_dir / "sources" / namespace
    filters = asdict(SearchFilters(language="zh"))
    work = WorkSummary(
        "fixture-lifecycle",
        ORIGIN + "/works/999999999",
        "原创生命周期样本",
        language="中文-普通话 國語",
        language_id="zh",
        chapter_count=1,
    )
    paragraphs = [
        "".join(f"原创样本{i:03d}-{j:02d}，检查终端滚动位置和关闭后的恢复。" for j in range(5))
        for i in range(80)
    ]
    chapter = Chapter("fixture-chapter", work.url, "原创测试章节", paragraphs, work)
    settings = StateStore(state_dir)
    settings.set_preferences({"source": ORIGIN, "language": "zh", "theme": "auto"})
    settings.close()
    store = StateStore(db_dir)
    store.save(
        {
            "schema_version": 1,
            "current": asdict(chapter),
            "position": {},
            "filters": filters,
            "search_draft": {"query": "", "language": "zh"},
            "flow": {
                "filters": filters,
                "active_kind": "recent",
                "recent": {
                    "kind": "recent",
                    "filters": filters,
                    "items": [],
                    "index": 0,
                    "url": ORIGIN + "/works",
                    "next_url": None,
                },
            },
            "history": [],
            "history_index": -1,
            "seen": [],
            "finished": {},
        }
    )
    store.cache_put("abandoned-speculation", {"paragraphs": ["原创旧预取样本"]})
    store.close()
    harness.STATE, harness.DB, harness.RUN = state_dir, db_dir / "reader.sqlite3", run
    session = harness.Session("sighup-original-fixture", 100, 30, "auto")
    session.paragraphs = paragraphs
    try:
        ready, milliseconds = session.wait(lambda: session.visible_body_matches() >= 2, 8)
        session.add(
            "original_fixture_body_visible",
            ready,
            milliseconds=milliseconds,
            matching_fragments=session.visible_body_matches(),
        )
        blocked = False
        try:
            SessionLock(state_dir).close()
        except SessionInUseError:
            blocked = True
        session.add("second_window_lock_blocked", blocked)
        with StateStoreView(db_dir) as view:
            session.add(
                "startup_purged_old_speculation", view.cache_get("abandoned-speculation") is None
            )
        for _ in range(2):
            session.send(b"\x1b[6~")
            session.pump(0.15)
        visible_anchor = session.first_visible_anchor()
        session.add(
            "scroll_changed_visible_anchor",
            bool(visible_anchor and visible_anchor["paragraph"] > 0),
            visible_anchor=visible_anchor,
        )
        session.send("/")
        session.pump(0.15)
        session.send("search")
        session.pump(0.15)
        session.send("\r")
        search_open, _ = session.wait(lambda: "关键词" in session.text(), 3)
        session.add("search_form_opened", search_open)
        before = harness.saved_state()
        draft_started = time.perf_counter()
        session.send(DRAFT)
        draft_visible, _ = session.wait(lambda: DRAFT in session.text(), 1)
        session.add("unsubmitted_draft_visible", draft_visible)
        before_signal = harness.saved_state()
        os.kill(session.pid, signal.SIGHUP)
        signal_after_ms = round((time.perf_counter() - draft_started) * 1000, 1)
        exited, exit_ms = session.wait(lambda: session.status is not None, 8)
        session.add(
            "sighup_completed",
            exited,
            milliseconds=exit_ms,
            signal_after_draft_ms=signal_after_ms,
            draft_was_not_yet_saved=before_signal.get("search_draft", {}).get("query") != DRAFT,
        )
        record = session.finish()
        record["actions"][-1]["name"] = "sighup_exit_and_terminal_restore"
        with StateStoreView(db_dir) as view:
            saved = view.load()
            cache = view.cache_info()
        lock = SessionLock(state_dir)
        lock.close()
        position = saved.get("position", {})
        checks = {
            "current_body_preserved": saved.get("current", {}).get("id") == chapter.id,
            "current_body_characters": sum(
                len(p) for p in saved.get("current", {}).get("paragraphs", [])
            ),
            "position_saved": position,
            "position_changed_from_initial": bool(
                position.get("paragraph", 0) or position.get("offset", 0)
            ),
            "position_matches_visible_paragraph": bool(
                visible_anchor and position.get("paragraph") == visible_anchor["paragraph"]
            ),
            "draft_preserved": saved.get("search_draft", {}).get("query") == DRAFT,
            "draft_changed_from_before_typing": before.get("search_draft", {}).get("query")
            != DRAFT,
            "cache_entries": cache["entries"],
            "cache_empty": cache["entries"] == 0,
            "history_entries": len(saved.get("history", [])),
            "session_lock_released": True,
        }
        record["all_actions_passed"] = all(action["ok"] for action in record["actions"])
        report = {
            "schema_version": 1,
            "version": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
            "code_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT / "src/mini_notes").glob("*.py"))
            },
            "at_local": time.strftime("%Y-%m-%d %H:%M:%S%z"),
            "scope": "真实 OS PTY + 安装入口 + 原创离线恢复状态；发送 SIGHUP，未关闭 GUI 宿主窗口。",
            "remote_content_or_credentials_used": False,
            "fixture_discovery_queue": "exhausted; no source request is needed",
            "session": record,
            "sqlite_after_exit": checks,
            "ok": record["all_actions_passed"]
            and all(
                checks[key]
                for key in (
                    "current_body_preserved",
                    "position_changed_from_initial",
                    "position_matches_visible_paragraph",
                    "draft_preserved",
                    "cache_empty",
                    "session_lock_released",
                )
            ),
        }
        if REPORT.exists():
            old = json.loads(REPORT.read_text())
            report["previous_runs"] = old.pop("previous_runs", []) + [old]
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "ok": report["ok"],
                    "actions": len(record["actions"]),
                    "cache_entries": cache["entries"],
                    "report": str(REPORT),
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["ok"] else 1
    finally:
        if session.status is None:
            session.finish()


class StateStoreView:
    def __init__(self, path):
        self.store = StateStore(path)

    def __enter__(self):
        return self.store

    def __exit__(self, *args):
        self.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
