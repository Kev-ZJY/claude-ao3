#!/usr/bin/env python3
"""Exercise the installed application through an OS PTY, without publishing remote prose.

Run with .venv/bin/python scripts/pty_acceptance.py. Private ANSI captures are
0600; the JSON artifact records only metrics, controls and matching outcomes.
"""

from __future__ import annotations

import codecs
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import struct
import termios
import time

import pyte

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "artifacts" / "private"
STATE = PRIVATE / "pty-state"
RUN = PRIVATE / ("pty-" + time.strftime("%Y%m%d-%H%M%S"))
REPORT = ROOT / "artifacts" / "pty-acceptance.json"
ORIGIN = "https://archiveofourown.org"
URL = ORIGIN + "/works/88323841/chapters/234237836"
DB = STATE / "sources" / hashlib.sha256(ORIGIN.encode()).hexdigest()[:16] / "reader.sqlite3"
DRAFT = "pty-draft-20260921-A"


def compact(value):
    return "".join(value.split())


def saved_state():
    if not DB.exists():
        return {}
    try:
        with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
            value = db.execute("SELECT value FROM state WHERE key='session'").fetchone()
            return json.loads(value[0]) if value else {}
    except (sqlite3.Error, ValueError):
        return {}


class Session:
    def __init__(self, name, columns, lines, theme, *, open_url=False):
        self.name = name
        self.columns, self.lines = columns, lines
        self.screen = pyte.Screen(columns, lines)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.raw = bytearray()
        self.paragraphs = []
        self.status = None
        self.started = time.perf_counter()
        self.record = {"name": name, "dimensions": [columns, lines], "theme": theme, "actions": []}
        self.record["code_sha256"] = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [ROOT / "mini-notes", *sorted((ROOT / "src" / "mini_notes").glob("*.py"))]
        }
        self.command = [
            "./mini-notes",
            "--data-dir",
            str(STATE.relative_to(ROOT)),
            "--theme",
            theme,
        ]
        if open_url:
            self.command += ["--open", URL]
        self.record["command"] = self.command
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(ROOT)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
            env = os.environ.copy()
            env.update(TERM="xterm-256color", COLORTERM="truecolor", PYTHONUNBUFFERED="1")
            # The tool runner exports NO_COLOR=1. Exercise the explicitly chosen
            # color themes without changing the user's shell environment.
            env.pop("NO_COLOR", None)
            os.execve(str(ROOT / "mini-notes"), self.command, env)
        os.set_blocking(self.fd, False)
        self.capture = RUN / (name + ".ansi")
        self.raw_file = os.fdopen(
            os.open(self.capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        )

    def pump(self, duration=0.15):
        end = time.perf_counter() + duration
        while time.perf_counter() < end:
            readable, _, _ = select.select(
                [self.fd], [], [], min(0.05, max(0, end - time.perf_counter()))
            )
            if readable:
                try:
                    data = os.read(self.fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                self.raw += data
                self.raw_file.write(data)
                self.raw_file.flush()
                self.stream.feed(self.decoder.decode(data))
            if self.status is None:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    self.status = status
            if self.status is not None and not readable:
                break
        # macOS reports EIO when the final slave fd closes. Reap even when that
        # branch ran before waitpid inside the loop, so clean exits stay clean.
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = status

    def wait(self, predicate, timeout=3):
        started = time.perf_counter()
        while time.perf_counter() - started < timeout:
            self.pump(0.08)
            if predicate():
                self.pump(0.12)
                return True, round((time.perf_counter() - started) * 1000, 1)
            if self.status is not None:
                return False, round((time.perf_counter() - started) * 1000, 1)
        return False, round((time.perf_counter() - started) * 1000, 1)

    def text(self):
        return "\n".join(self.screen.display)

    def body(self):
        return "\n".join(self.screen.display[: max(0, self.lines - 6)])

    def send(self, value):
        os.write(self.fd, value if isinstance(value, bytes) else value.encode())

    def load_paragraphs(self):
        current = saved_state().get("current") or {}
        if current.get("id") == "234237836":
            self.paragraphs = current.get("paragraphs", [])
        return bool(self.paragraphs)

    def visible_body_matches(self):
        shown = compact(self.body())
        hits = 0
        for paragraph in self.paragraphs:
            value = compact(paragraph)
            # Multiple short substrings establish that actual stored body text,
            # rather than a title or a loading message, reached the PTY screen.
            for start in range(0, max(0, len(value) - 23), 16):
                if value[start : start + 24] in shown:
                    hits += 1
        return hits

    def first_visible_anchor(self):
        paragraphs = [compact(p) for p in self.paragraphs]
        for line in self.screen.display[: max(0, self.lines - 6)]:
            fragment = compact(line)
            if len(fragment) < 18:
                continue
            probe = fragment[: min(32, len(fragment))]
            for index, paragraph in enumerate(paragraphs):
                offset = paragraph.find(probe)
                if offset >= 0:
                    return {"paragraph": index, "compact_offset": offset}
        return None

    def add(self, name, ok, **values):
        self.record["actions"].append({"name": name, "ok": bool(ok), **values})
        return bool(ok)

    def palette_present(self, color):
        return any(
            cell.bg.lower() == color.lower()
            for row in self.screen.buffer.values()
            for cell in row.values()
        )

    def wait_for_body(self, timeout=55):
        def ready():
            return (
                self.load_paragraphs()
                and self.visible_body_matches() >= 2
                and "cols" in self.text()
            )

        started = time.perf_counter()
        while time.perf_counter() - started < timeout:
            self.pump(0.1)
            if ready():
                self.pump(0.2)
                return True
            text = self.text()
            if "无法获取内容" in text or "书源拒绝访问" in text or "书源暂时限流" in text:
                self.record["source_error"] = {
                    "http_status": re.findall(r"HTTP\s+(\d{3})", text),
                    "rate_limited": "限流" in text,
                    "forbidden": "拒绝" in text,
                    "timeout_or_network": "连接" in text or "超时" in text or "失败" in text,
                }
                return False
            if self.status is not None:
                return False
        return False

    def resize(self, columns, lines):
        self.columns, self.lines = columns, lines
        self.screen.resize(lines=lines, columns=columns)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def finish(self):
        if self.status is None:
            self.send(b"\x03")
            self.wait(lambda: self.status is not None, 8)
        if self.status is None:
            os.kill(self.pid, signal.SIGTERM)
            self.wait(lambda: self.status is not None, 2)
        self.pump(0.2)
        if self.status is None:
            _, self.status = os.waitpid(self.pid, 0)
        self.raw_file.close()
        os.close(self.fd)
        code = os.waitstatus_to_exitcode(self.status)
        leave = self.raw.rfind(b"\x1b[?1049l") > self.raw.rfind(b"\x1b[?1049h") >= 0
        cursor = self.raw.rfind(b"\x1b[?25h") > self.raw.rfind(b"\x1b[?25l") >= 0
        self.add(
            "ctrl_c_exit_and_terminal_restore",
            code == 0 and leave and cursor,
            exit_code=code,
            entered_alternate_buffer=b"\x1b[?1049h" in self.raw,
            left_alternate_buffer=leave,
            cursor_show_emitted=cursor,
        )
        self.record["elapsed_seconds"] = round(time.perf_counter() - self.started, 3)
        self.record["raw_bytes"] = len(self.raw)
        self.record["private_capture"] = str(self.capture.relative_to(ROOT))
        self.record["capture_mode"] = oct(self.capture.stat().st_mode & 0o777)
        self.record["all_actions_passed"] = all(a["ok"] for a in self.record["actions"])
        return self.record


def exercise_dark(session):
    ready = session.wait_for_body()
    session.add(
        "real_body_visible",
        ready,
        milliseconds=round((time.perf_counter() - session.started) * 1000, 1),
        matching_fragments=session.visible_body_matches(),
    )
    if not ready:
        return False
    session.add("dark_background", session.palette_present("222834"))
    original = session.body()
    session.send(b"\x1b[6~")
    changed, elapsed = session.wait(
        lambda: session.body() != original and session.visible_body_matches() >= 2
    )
    session.add("page_down", changed, milliseconds=elapsed)
    session.send(b"\x1b[5~")
    restored, elapsed = session.wait(lambda: session.body() == original)
    session.add("page_up_restores_body", restored, milliseconds=elapsed)
    session.send(b"\x1b[B")
    changed, elapsed = session.wait(lambda: session.body() != original)
    session.add("arrow_down", changed, milliseconds=elapsed)
    session.send(b"\x1b[A")
    restored, elapsed = session.wait(lambda: session.body() == original)
    session.add("arrow_up_restores_body", restored, milliseconds=elapsed)
    session.send(b"\x1b[6~")
    session.wait(lambda: session.body() != original)
    reading = session.body()
    anchor = session.first_visible_anchor()
    session.send(b"\x07")
    hidden, elapsed = session.wait(
        lambda: "ResizeObserver" in session.text() and session.visible_body_matches() == 0
    )
    session.add(
        "reader_ctrl_g_hides_body",
        hidden,
        milliseconds=elapsed,
        novel_fragments_visible=session.visible_body_matches(),
    )
    session.send(b"\x07")
    restored, elapsed = session.wait(lambda: session.body() == reading)
    session.add(
        "reader_ctrl_g_restores_exact_view",
        restored,
        milliseconds=elapsed,
        anchor=session.first_visible_anchor(),
    )
    # Key events in one OS write may all target the previously focused widget.
    # Type the focus-changing slash separately, as an interactive user does.
    session.send("/")
    command_focused, _ = session.wait(lambda: "/" in session.screen.display[-4])
    session.add("slash_focuses_command", command_focused)
    session.send("search")
    command_entered, _ = session.wait(lambda: "/search" in session.screen.display[-4])
    session.add("search_command_entered", command_entered)
    session.send("\r")
    search, _ = session.wait(
        lambda: (
            "Search" in session.text()
            and "Warnings" in session.text()
            and "Categories" in session.text()
        )
    )
    session.add("search_command_opens_form", search)
    session.send(DRAFT)
    drafted, _ = session.wait(lambda: DRAFT in session.text())
    session.add("search_draft_entered", search and drafted)
    session.send(b"\x07")
    hidden, elapsed = session.wait(
        lambda: (
            "ResizeObserver" in session.text()
            and DRAFT not in session.text()
            and "Categories" not in session.text()
        )
    )
    session.add("search_ctrl_g_hides_draft_and_filters", hidden, milliseconds=elapsed)
    session.send(b"\x07")
    restored, elapsed = session.wait(
        lambda: DRAFT in session.text() and "Categories" in session.text()
    )
    session.add("search_ctrl_g_restores_draft", restored, milliseconds=elapsed)
    session.send("-ok")
    focused, _ = session.wait(lambda: DRAFT + "-ok" in session.text())
    session.add("search_restores_input_focus", focused)
    session.send(b"\x1b")
    restored, elapsed = session.wait(lambda: session.body() == reading)
    session.add("escape_restores_reading_position", restored, milliseconds=elapsed)
    session.record["last_visible_anchor"] = anchor
    return True


def exercise_light(session, previous):
    ready = session.wait_for_body(8)
    session.add(
        "saved_real_body_visible",
        ready,
        milliseconds=round((time.perf_counter() - session.started) * 1000, 1),
        matching_fragments=session.visible_body_matches(),
    )
    if not ready:
        return
    session.add("light_background", session.palette_present("f6f7fa"))
    current = saved_state()
    session.add(
        "reopened_same_chapter_and_saved_anchor",
        current.get("current", {}).get("id") == previous.get("current", {}).get("id")
        and current.get("position") == previous.get("position"),
        saved_anchor=current.get("position"),
    )
    before = session.first_visible_anchor()
    anchor = previous.get("position", {})
    index = anchor.get("paragraph", 0)
    expected_offset = (
        len(compact(session.paragraphs[index][: anchor.get("offset", 0)]))
        if 0 <= index < len(session.paragraphs)
        else -1
    )
    restored_near_anchor = bool(
        before
        and before["paragraph"] == index
        and abs(before["compact_offset"] - expected_offset) <= 48
    )
    session.add(
        "saved_anchor_visible_after_reflow",
        restored_near_anchor,
        expected_paragraph=index,
        expected_compact_offset=expected_offset,
        visible_anchor=before,
    )
    session.add(
        "small_terminal_footer_and_body",
        "48 cols" in session.text()
        and "Workspace / local" in session.text()
        and session.visible_body_matches() >= 2,
        dimensions=[48, 15],
        first_visible_anchor=before,
    )
    session.resize(80, 24)
    resized, elapsed = session.wait(
        lambda: "80 cols" in session.text() and session.visible_body_matches() >= 2
    )
    after = session.first_visible_anchor()
    near = bool(
        before
        and after
        and before["paragraph"] == after["paragraph"]
        and abs(before["compact_offset"] - after["compact_offset"]) <= 80
    )
    session.add(
        "sigwinch_resize_preserves_paragraph",
        resized and near,
        milliseconds=elapsed,
        dimensions=[80, 24],
        before=before,
        after=after,
    )
    session.resize(48, 15)
    resized, elapsed = session.wait(
        lambda: "48 cols" in session.text() and session.visible_body_matches() >= 2
    )
    session.add("resize_back_to_48x15", resized, milliseconds=elapsed)


def main():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(mode=0o700)
    previous_runs = []
    if REPORT.exists():
        previous = json.loads(REPORT.read_text())
        previous_runs = previous.pop("previous_runs", []) + [previous]
    report = {
        "method": "OS pty.fork; installed ./mini-notes; pyte screen decoder",
        "term": "xterm-256color",
        "child_environment": {"COLORTERM": "truecolor", "NO_COLOR": "unset only for child"},
        "state_preexisted": STATE.exists(),
        "network": "real anonymous AO3 through installed CLI; persisted content may be reused; automatic one-successor prefetch may run",
        "copyright_prose_in_report": False,
        "sessions": [],
        "scope": "This PTY and emulator run only; not a guarantee for all terminal hosts or IMEs.",
        "previous_runs": previous_runs,
    }
    complete = False
    for attempt in range(1, 3):
        session = Session(f"dark-100x30-attempt-{attempt}", 100, 30, "dark", open_url=True)
        try:
            complete = exercise_dark(session)
        except Exception as exc:
            session.add("harness_exception", False, exception_type=type(exc).__name__)
        finally:
            report["sessions"].append(session.finish())
        if complete:
            break
        error = session.record.get("source_error", {})
        if error.get("rate_limited") or error.get("forbidden"):
            break
    before = saved_state()
    if complete:
        session = Session("light-48x15-restart", 48, 15, "light")
        try:
            exercise_light(session, before)
        except Exception as exc:
            session.add("harness_exception", False, exception_type=type(exc).__name__)
        finally:
            report["sessions"].append(session.finish())
    report["all_actions_passed"] = bool(complete) and all(
        s["all_actions_passed"] for s in report["sessions"]
    )
    report["latest_interaction_sessions_passed"] = bool(complete) and all(
        s["all_actions_passed"] for s in report["sessions"][-2:]
    )
    report["saved_work_id"] = before.get("current", {}).get("work", {}).get("id")
    report["saved_chapter_id"] = before.get("current", {}).get("id")
    report["saved_position"] = before.get("position")
    report["timing_scope"] = (
        "Script observation includes polling and a 120ms settling interval; these are not isolated display-latency measurements."
    )
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
