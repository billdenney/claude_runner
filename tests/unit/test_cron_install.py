"""Tests for cron.install — managed-block crontab planner."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from claude_task_runner.clock import FakeClock
from claude_task_runner.cron.install import (
    BEGIN_MARKER,
    END_MARKER,
    backup_crontab,
    build_block,
    build_install_plan,
    build_uninstall_plan,
    crontab_l,
    remove_block,
    replace_block,
)

WATCHDOG = Path("/usr/local/lib/claude/watchdog.sh")


class TestBuildBlock:
    def test_default_schedule(self) -> None:
        out = build_block(WATCHDOG)
        assert BEGIN_MARKER in out
        assert END_MARKER in out
        assert "* * * * * /usr/local/lib/claude/watchdog.sh" in out

    def test_custom_schedule(self) -> None:
        out = build_block(WATCHDOG, schedule="*/5 * * * *")
        assert "*/5 * * * * /usr/local/lib/claude/watchdog.sh" in out


class TestReplaceBlock:
    def test_appends_when_no_existing_block(self) -> None:
        existing = "0 3 * * * /usr/local/bin/backup\n"
        new_block = build_block(WATCHDOG)
        out, existed = replace_block(existing, new_block)
        assert existed is False
        assert "0 3 * * * /usr/local/bin/backup" in out
        assert BEGIN_MARKER in out
        assert out.endswith("\n")

    def test_replaces_existing_block_in_place(self) -> None:
        existing_block = build_block(Path("/old/path/watchdog.sh"))
        existing = f"0 3 * * * /usr/local/bin/backup\n{existing_block}MAILTO=ops\n"
        new_block = build_block(WATCHDOG)
        out, existed = replace_block(existing, new_block)
        assert existed is True
        assert "/old/path/watchdog.sh" not in out
        assert "/usr/local/lib/claude/watchdog.sh" in out
        # Lines outside the block survive.
        assert "0 3 * * * /usr/local/bin/backup" in out
        assert "MAILTO=ops" in out

    def test_empty_existing(self) -> None:
        out, existed = replace_block("", build_block(WATCHDOG))
        assert existed is False
        assert out.startswith(BEGIN_MARKER)
        assert out.endswith("\n")


class TestRemoveBlock:
    def test_removes_block_only(self) -> None:
        existing = f"0 3 * * * /usr/local/bin/backup\n{build_block(WATCHDOG)}MAILTO=ops\n"
        out, existed = remove_block(existing)
        assert existed is True
        assert BEGIN_MARKER not in out
        assert END_MARKER not in out
        assert "0 3 * * * /usr/local/bin/backup" in out
        assert "MAILTO=ops" in out

    def test_no_op_when_no_block(self) -> None:
        existing = "0 3 * * * /usr/local/bin/backup\n"
        out, existed = remove_block(existing)
        assert existed is False
        assert out == existing


class TestCrontabSubprocess:
    """Exercise the ``crontab(1)`` shell-out via a fake binary."""

    def _make_fake_crontab(self, tmp_path: Path, mode: str) -> Path:
        """Create an executable shim and return its path.

        ``mode``:
        * ``"empty"`` — exits 1 with "no crontab" stderr (as Linux ``crontab -l`` does).
        * ``"populated"`` — prints a sample crontab on -l; for ``-`` reads stdin
          (success).
        * ``"broken"`` — exits 5 with arbitrary stderr.
        """
        if mode == "empty":
            body = '#!/usr/bin/env bash\nif [ "$1" = "-l" ]; then echo "no crontab for user" 1>&2; exit 1; fi\nexit 0\n'
        elif mode == "populated":
            body = (
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "-l" ]; then\n'
                '  printf "0 3 * * * /usr/local/bin/backup\\nMAILTO=ops\\n"; exit 0;\n'
                "fi\n"
                'if [ "$1" = "-" ]; then\n'
                "  cat > /dev/null; exit 0;\n"
                "fi\n"
                "exit 0\n"
            )
        elif mode == "broken":
            body = '#!/usr/bin/env bash\necho "kaboom" 1>&2\nexit 5\n'
        else:
            raise ValueError(mode)
        path = tmp_path / "crontab"
        path.write_text(body)
        path.chmod(0o755)
        return path

    def test_crontab_l_empty(self, tmp_path: Path) -> None:
        binary = self._make_fake_crontab(tmp_path, "empty")
        assert crontab_l(crontab_executable=str(binary)) == ""

    def test_crontab_l_populated(self, tmp_path: Path) -> None:
        binary = self._make_fake_crontab(tmp_path, "populated")
        out = crontab_l(crontab_executable=str(binary))
        assert "0 3 * * * /usr/local/bin/backup" in out
        assert "MAILTO=ops" in out

    def test_build_install_plan_diff(self, tmp_path: Path) -> None:
        binary = self._make_fake_crontab(tmp_path, "populated")
        plan = build_install_plan(
            watchdog_path=WATCHDOG,
            crontab_executable=str(binary),
        )
        assert plan.block_existed is False
        diff = "\n".join(plan.diff_lines)
        assert "+" in diff
        assert BEGIN_MARKER in diff

    def test_build_uninstall_plan_no_block(self, tmp_path: Path) -> None:
        binary = self._make_fake_crontab(tmp_path, "populated")
        plan = build_uninstall_plan(crontab_executable=str(binary))
        assert plan.block_existed is False
        assert plan.new_text == plan.existing_text


class TestBackupCrontab:
    def test_writes_timestamped_file(self, tmp_path: Path) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 30, 45, tzinfo=UTC))
        path = backup_crontab(
            "0 3 * * * /usr/local/bin/backup\n",
            clock=clock,
            dest_dir=tmp_path,
        )
        assert path.parent == tmp_path
        assert "20260504T123045Z" in path.name
        assert path.read_text() == "0 3 * * * /usr/local/bin/backup\n"
