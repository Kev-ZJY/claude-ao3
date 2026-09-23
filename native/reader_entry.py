"""Frozen console entry point; the native macOS shell provides its PTY."""

from multiprocessing import freeze_support

from mini_notes.cli import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
