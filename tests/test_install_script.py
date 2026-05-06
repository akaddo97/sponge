"""The Whisper install script must exist + be executable + have a shebang."""
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install_whisper.sh"


def test_install_script_exists():
    assert SCRIPT.is_file()


def test_install_script_is_executable():
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/install_whisper.sh must be executable (chmod +x)"


def test_install_script_has_bash_shebang():
    first_line = SCRIPT.read_text().splitlines()[0]
    assert first_line.startswith("#!/bin/bash") or first_line.startswith("#!/usr/bin/env bash")
