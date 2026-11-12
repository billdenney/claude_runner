"""precheck_regen.R: what buildModelDb() stops on, found before the regeneration.

A cold buildModelDb() takes about an hour and raises the convention errors it
collected only at the end. The pre-check runs the package's own duplicate
check over every register and its convention check over the models the merge
added or changed, so merge_branches.sh stops in minutes instead.

The script runs against small packages built here, so the cases need R, and
pkgload, nlmixr2est and rxode2 where a model is checked; CI installs no R. The
orchestrator's side runs everywhere, with a stub Rscript.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from ._git_world import git
from ._merge_skill_repo import SKILL_DIR, new_repo, push_task_branch, run

PRECHECK = SKILL_DIR / "precheck_regen.R"
MERGE_BRANCHES = SKILL_DIR / "merge_branches.sh"

FAKE_R = """\
buildModelDb <- function() NULL

.referenceDuplicateIssues <- function(paths) {
  out <- data.frame(
    category = character(), severity = character(), name = character(),
    message = character(), suggestion = character(), stringsAsFactors = FALSE
  )
  for (p in paths) {
    if (any(grepl("DUPLICATE", readLines(p)))) {
      out <- rbind(out, data.frame(
        category = "reference_duplicate", severity = "error", name = "x",
        message = sprintf("%s has 2 `### x` entries\\nsharing a Type.", basename(p)),
        suggestion = NA_character_, stringsAsFactors = FALSE
      ))
    }
  }
  out
}

checkModelConventions <- function(model, verbose = TRUE) {
  bad <- "lbad" %in% model$iniDf$name
  data.frame(
    model = "m", category = "covariates", severity = if (bad) "error" else "warning",
    name = "OCC", message = "Covariate 'OCC' has no entry in covariateData.",
    suggestion = NA_character_, stringsAsFactors = FALSE
  )[if (bad) 1L else 0L, , drop = FALSE]
}
"""

MODEL = """\
{name} <- function() {{
  ini({{
    {theta} <- 0.45
    propSd <- 0.1
  }})
  model({{
    ka <- exp({theta})
    d/dt(depot) <- -ka * depot
    Cc <- depot
    Cc ~ prop(propSd)
  }})
}}
"""


@pytest.fixture
def rscript() -> str:
    found = shutil.which("Rscript")
    if found is None:
        pytest.skip("Rscript is not on PATH (CI installs no R), so the R script is not run")
    probe = run(found, "-e", "quit(status = if (requireNamespace('pkgload')) 0L else 1L)")
    if probe.returncode != 0:
        pytest.skip("pkgload is not installed, so the script cannot load a package")
    return found


@pytest.fixture
def rscript_with_nlmixr2(rscript: str) -> str:
    probe = run(
        rscript,
        "-e",
        "quit(status = if (requireNamespace('nlmixr2est') && requireNamespace('rxode2')) 0L"
        " else 1L)",
    )
    if probe.returncode != 0:
        pytest.skip("nlmixr2est or rxode2 is not installed, so no model can be loaded")
    return rscript


def package(
    root: Path,
    r_code: str = FAKE_R,
    registers: dict[str, str] | None = None,
    namespace: str = "export(checkModelConventions)\n",
) -> Path:
    files = {
        "DESCRIPTION": "Package: fakepkg\nVersion: 0.0.1\nTitle: Fake\nDescription: Fake.\n"
        "License: MIT\n",
        "NAMESPACE": namespace,
        "R/fake.R": r_code,
        **{
            f"inst/references/{name}": text
            for name, text in (registers or {"a.md": "ok\n"}).items()
        },
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def precheck(rscript: str, wt: Path, models: list[str], tmp_path: Path) -> tuple[int, str]:
    listing = tmp_path / "models.txt"
    listing.write_text("".join(f"{m}\n" for m in models))
    proc = run(rscript, PRECHECK, "--worktree", wt, "--models", listing)
    return proc.returncode, proc.stdout + proc.stderr


class TestPrecheckScript:
    def test_a_canonical_registered_twice_stops_it(self, rscript: str, tmp_path: Path) -> None:
        wt = package(tmp_path / "pkg", registers={"a.md": "ok\n", "b.md": "DUPLICATE\n"})
        code, out = precheck(rscript, wt, [], tmp_path)
        assert code == 1, out
        assert out == (
            "ERROR: (precheck) 1 canonical(s) registered twice:\n"
            "    b.md has 2 `### x` entries sharing a Type.\n"
            "    (precheck) the merge added or changed no model file\n"
            "\n"
            "SUMMARY: 1 error(s); buildModelDb() would stop on them\n"
        )

    def test_clean_registers_and_no_models_pass(self, rscript: str, tmp_path: Path) -> None:
        wt = package(tmp_path / "pkg")
        assert precheck(rscript, wt, ["inst/modeldb/Gone_2020_x.R"], tmp_path) == (
            0,
            "    (precheck) no canonical is registered twice in 1 register(s)\n"
            "    (precheck) the merge added or changed no model file\n",
        )

    def test_a_convention_error_in_a_changed_model_stops_it(
        self, rscript_with_nlmixr2: str, tmp_path: Path
    ) -> None:
        """Only an error stops it; the clean model's warning is buildModelDb's to print."""
        wt = package(tmp_path / "pkg")
        for name, theta in (("Bad_2020_x", "lbad"), ("Good_2021_y", "lka")):
            path = wt / "inst" / "modeldb" / f"{name}.R"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(MODEL.format(name=name, theta=theta))
        code, out = precheck(
            rscript_with_nlmixr2,
            wt,
            ["inst/modeldb/Bad_2020_x.R", "inst/modeldb/Good_2021_y.R"],
            tmp_path,
        )
        assert code == 1, out
        assert out == (
            "    (precheck) no canonical is registered twice in 1 register(s)\n"
            "    (precheck) checking the conventions of 2 model(s) the merge added or changed\n"
            "ERROR: (precheck) inst/modeldb/Bad_2020_x.R:\n"
            "    OCC (covariates): Covariate 'OCC' has no entry in covariateData.\n"
            "\n"
            "SUMMARY: 1 error(s); buildModelDb() would stop on them\n"
        )

    def test_a_model_named_unlike_its_file_stops_it(
        self, rscript_with_nlmixr2: str, tmp_path: Path
    ) -> None:
        """buildModelDb() stops on it too, before the conventions."""
        wt = package(tmp_path / "pkg")
        path = wt / "inst" / "modeldb" / "Name_2020_x.R"
        path.parent.mkdir(parents=True)
        path.write_text(MODEL.format(name="Other_2020_x", theta="lka"))
        code, out = precheck(rscript_with_nlmixr2, wt, ["inst/modeldb/Name_2020_x.R"], tmp_path)
        assert code == 1, out
        assert (
            "ERROR: (precheck) inst/modeldb/Name_2020_x.R cannot be loaded, so buildModelDb()"
            " would stop: the model is named Other_2020_x, the file Name_2020_x.R\n"
        ) in out

    def test_a_package_without_buildmodeldb_has_nothing_to_check(
        self, rscript: str, tmp_path: Path
    ) -> None:
        wt = package(tmp_path / "pkg", r_code="checkModelConventions <- function(...) NULL\n")
        assert precheck(rscript, wt, [], tmp_path) == (
            0,
            "    (precheck) fakepkg has no buildModelDb(); nothing to check\n",
        )

    @pytest.mark.parametrize("missing", [".referenceDuplicateIssues", "checkModelConventions"])
    def test_a_check_the_package_lacks_stops_it(
        self, rscript: str, tmp_path: Path, missing: str
    ) -> None:
        """A rename upstream must not switch the pre-check off silently."""
        code = FAKE_R.replace(f"{missing} <- function", "renamed_away <- function")
        exported = "" if missing == "checkModelConventions" else "export(checkModelConventions)\n"
        wt = package(tmp_path / "pkg", r_code=code, namespace=exported)
        assert precheck(rscript, wt, [], tmp_path) == (
            2,
            f"ERROR: (precheck) fakepkg has buildModelDb() but no {missing}(), so the pre-check"
            " cannot run the build's checks\n",
        )

    @pytest.mark.parametrize(
        ("body", "out"),
        [
            (
                'stop("no ui")',
                "WARNING: (precheck) inst/modeldb/Good_2021_y.R: checkModelConventions() failed,"
                " which buildModelDb() ignores too: no ui\n",
            ),
            ("NULL", ""),
        ],
        ids=["raises", "returns-null"],
    )
    def test_a_convention_check_that_fails_or_finds_nothing_does_not_stop_it(
        self, rscript_with_nlmixr2: str, tmp_path: Path, body: str, out: str
    ) -> None:
        """addFileToModelDb() ignores an error raised inside the check."""
        code = FAKE_R.split("checkModelConventions <- function")[0]
        code += f"checkModelConventions <- function(model, verbose = TRUE) {body}\n"
        wt = package(tmp_path / "pkg", r_code=code)
        path = wt / "inst" / "modeldb" / "Good_2021_y.R"
        path.parent.mkdir(parents=True)
        path.write_text(MODEL.format(name="Good_2021_y", theta="lka"))
        code_, printed = precheck(
            rscript_with_nlmixr2, wt, ["inst/modeldb/Good_2021_y.R"], tmp_path
        )
        assert (code_, printed) == (
            0,
            "    (precheck) no canonical is registered twice in 1 register(s)\n"
            "    (precheck) checking the conventions of 1 model(s) the merge added or changed\n"
            f"{out}"
            "    (precheck) no convention error in the models the merge added or changed\n",
        )

    def test_a_crash_is_not_a_verdict(self, rscript: str, tmp_path: Path) -> None:
        """Exit 1 means "buildModelDb would stop"; a crash must not read as that."""
        code = FAKE_R.replace(
            ".referenceDuplicateIssues <- function(paths) {",
            '.referenceDuplicateIssues <- function(paths) {\n  stop("register parser broke")',
        )
        wt = package(tmp_path / "pkg", r_code=code)
        assert precheck(rscript, wt, [], tmp_path) == (
            2,
            "ERROR: (precheck) crashed: register parser broke\n",
        )

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--models", "x"], "ERROR: (precheck) --worktree needs exactly one value\n"),
            (
                ["--worktree", "{wt}", "--models", "{tmp}/nope.txt"],
                "ERROR: (precheck) --models {tmp}/nope.txt does not exist\n",
            ),
            (
                ["--worktree", "{tmp}", "--models", "{tmp}/nope.txt"],
                "ERROR: (precheck) {tmp} is not a package worktree\n",
            ),
        ],
        ids=["no-worktree", "no-models-file", "not-a-package"],
    )
    def test_inputs_that_cannot_be_right_exit_2(
        self, rscript: str, tmp_path: Path, args: list[str], message: str
    ) -> None:
        wt = package(tmp_path / "pkg")
        given = [arg.format(wt=wt, tmp=tmp_path) for arg in args]
        proc = run(rscript, PRECHECK, *given)
        assert (proc.returncode, proc.stdout) == (2, message.format(wt=wt, tmp=tmp_path))


def _stub_rscript(log: Path, fail: Path) -> str:
    """Logs the pre-check with its model list, and the regeneration; the
    pre-check fails while ``fail`` exists."""
    out = shlex.quote(str(log))
    flag = shlex.quote(str(fail))
    return f"""#!/bin/bash
case "$*" in
  *precheck_regen.R*)
    while [[ $# -gt 0 ]]; do [[ "$1" == --models ]] && models="$2"; shift; done
    {{ echo precheck; sed 's/^/  /' "$models"; }} >> {out}
    if [[ -e {flag} ]]; then echo "ERROR: (precheck) 1 canonical(s) registered twice:"; exit 1; fi ;;
  *buildModelDb*)
    echo regen >> {out} ;;
esac
"""


def test_merge_branches_stops_before_the_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check gets the model files the merge added or changed, and
    its error stops the run (exit 5) before buildModelDb() starts. --finish,
    once it is fixed, runs the pre-check again and then the regeneration."""
    repo = new_repo(
        tmp_path, monkeypatch, {"inst/modeldb/Old_2019_o.R": "# old\n", "README.md": "x\n"}
    )
    push_task_branch(
        repo,
        "claude/a",
        {"inst/modeldb/A_2020_a.R": "# A\n", "inst/modeldb/Old_2019_o.R": "# old, fixed\n"},
        "Add A",
    )
    push_task_branch(repo, "claude/b", {"inst/modeldb/sub/B_2021_b.R": "# B\n"}, "Add B")
    log, fail = tmp_path / "r.log", tmp_path / "fail-precheck"
    fail.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "Rscript").write_text(_stub_rscript(log, fail))
    (bin_dir / "Rscript").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    proc = run(
        "bash",
        MERGE_BRANCHES,
        "--repo",
        repo,
        "--yes",
        "--skip-check",
        "--skip-vignettes",
        "--skip-push",
        "--branch-name",
        "consolidation",
        env=env,
    )
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert log.read_text() == (
        "precheck\n  inst/modeldb/A_2020_a.R\n  inst/modeldb/Old_2019_o.R\n"
        "  inst/modeldb/sub/B_2021_b.R\n"
    )
    assert "==> Pre-checking the registers and the 3 model(s) the merge added or changed\n" in (
        proc.stdout
    )
    wt = repo / ".worktrees" / "consolidation"
    finish = (
        f"bash {shlex.quote(str(MERGE_BRANCHES))} --repo {shlex.quote(str(repo))}"
        " --branch-name consolidation --finish"
    )
    assert proc.stderr.endswith(
        "ERROR: the pre-check found what buildModelDb would stop on (listed above): a\n"
        "  canonical registered twice, or a convention error in a model the merge added\n"
        f"  or changed. Fix it in {wt} and commit, then run\n"
        f"     {finish}\n"
        "  which runs the pre-check and the regeneration again.\n"
    )
    fail.unlink()
    log.unlink()
    done = run(*shlex.split(finish), env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert log.read_text().splitlines()[0] == "precheck"
    assert log.read_text().splitlines()[-1] == "regen"
    assert git(wt, "status", "--porcelain") == ""
