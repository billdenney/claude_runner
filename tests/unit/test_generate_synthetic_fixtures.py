"""``scripts/generate_synthetic_fixtures.py`` still writes the committed fixtures.

The parser tests read ``tests/fixtures/usage/synthetic_*``, and this script is
the only record of how those bytes were made. The script writes next to its own
location (``<script dir>/../tests/fixtures/usage``) and ignores its arguments,
so even ``--help`` would overwrite the fixtures. It therefore runs from a copy
in a temporary directory, never in place.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "generate_synthetic_fixtures.py"
COMMITTED = REPO_ROOT / "tests" / "fixtures" / "usage"


def test_regenerating_reproduces_the_committed_fixtures_byte_for_byte(tmp_path: Path) -> None:
    copy = tmp_path / "scripts" / SCRIPT.name
    copy.parent.mkdir()
    shutil.copy2(SCRIPT, copy)
    proc = subprocess.run(
        [sys.executable, str(copy)],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (proc.returncode, proc.stderr) == (0, "")

    written = tmp_path / "tests" / "fixtures" / "usage"
    names = sorted(path.name for path in written.iterdir())
    assert names == sorted(path.name for path in COMMITTED.glob("synthetic_*"))
    changed = [n for n in names if (written / n).read_bytes() != (COMMITTED / n).read_bytes()]
    assert changed == []
    assert sorted(proc.stdout.splitlines()) == sorted(
        f"wrote {cap.name} ({cap.stat().st_size} bytes) and {cap.stem}.expected.json"
        for cap in COMMITTED.glob("synthetic_*.cap")
    )
