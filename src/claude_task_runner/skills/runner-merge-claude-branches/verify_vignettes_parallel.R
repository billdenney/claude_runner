# Parallel vignette validator for the runner-merge-claude-branches skill.
#
# Renders every vignette under vignettes/articles/ in a callr subprocess
# so a single failure does not poison the others. Continues on failure
# and writes a JSON-lines report so the orchestrator can summarise.
#
# Why this lives in the merge skill:
# pkgdown's CI vignette build runs sequentially and ABORTS on the FIRST
# failure. After a 130-branch merge that can leave 14+ latent failures
# undiscovered — each surfaced one at a time across many CI cycles.
# Catching them in one local parallel pass cuts the loop from days to
# minutes and keeps the PR review process short.
#
# Invocation (from the consolidation worktree):
#
#   Rscript .../verify_vignettes_parallel.R \
#     --worktree <abs-worktree-path>        # default: $PWD
#     --jobs <int>                          # default: max(1, parallel::detectCores() - 2)
#     --timeout <secs>                      # per-vignette ceiling; default: 900
#     --rx-threads <int>                    # rxode2 and data.table threads in each worker;
#                                           # default 2, what both give on a 4-vCPU CI
#                                           # runner; 0 leaves each package's own default
#     --results <path>                      # JSONL output; default: /tmp/vignette_results.jsonl
#     --log <path>                          # human-readable log; default: stdout (no -- arg)
#     --full-lib                            # opt OUT of the DESCRIPTION-only library path
#                                           # (see build_description_libpath below); the gate
#                                           # then sees every package installed on the machine,
#                                           # which can hide a missing CI dependency.
#     --only a,b,c                          # restrict to these vignettes (see the note at the
#                                           # call site); unknown names exit 2
#     --skip-install                        # do not install the worktree's package; the
#                                           # workers load whatever copy the library has
#
# The package is installed from the worktree into a private library inside the
# gate's own temporary directory, never into the user's library: an install
# there would swap the package under every other R session on the host, and
# the copy there may be one remotes cannot reinstall over (RemoteType git).
# The workers find it first through their library path and R_LIBS, which a
# process a vignette starts inherits. Each worker renders with its own
# temporary directory as rmarkdown's intermediates directory, so the gate
# writes nothing into the worktree: a concurrent R CMD build of the same
# worktree reads a tree the gate leaves as it found it.
#
# Exit codes:
#   0  all vignettes rendered successfully
#   1  at least one vignette failed (details in --results and --log)
#   2  bad args / setup failure
suppressPackageStartupMessages({
  library(parallel)
  library(callr)
})

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(name, default = NULL) {
  i <- which(args == name)
  if (!length(i)) return(default)
  if (length(args) < i + 1L) stop(sprintf("missing value for %s", name))
  args[[i + 1L]]
}
has_flag <- function(name) name %in% args

WORKTREE <- normalizePath(get_arg("--worktree", getwd()), mustWork = TRUE)
JOBS     <- as.integer(get_arg("--jobs", max(1L, parallel::detectCores() - 2L)))
# rxode2 solver threads and data.table threads INSIDE each worker (not the
# number of workers). Both packages default to half the machine's cores, which
# is 2 on the 4-vCPU ubuntu-latest runners CI renders on, and 16 here on 32
# cores: the default of 2 renders as CI does, and keeps --jobs workers at 2
# threads each instead of 16. See the long note in the worker. Pass
# --rx-threads 0 to leave both at their own defaults.
RXTHREADS <- as.integer(get_arg("--rx-threads", "2"))
if (is.na(RXTHREADS) || RXTHREADS < 1L) RXTHREADS <- NULL
TIMEOUT  <- as.integer(get_arg("--timeout", 900L))
RESULTS  <- get_arg("--results", "/tmp/vignette_results.jsonl")
SKIP_INSTALL <- has_flag("--skip-install")
# DESCRIPTION-only library path: ON by default. --full-lib opts out.
DESC_LIB <- !has_flag("--full-lib")

setwd(WORKTREE)

# Everything the gate writes: the private library, the DESCRIPTION-only
# library, the package tarball.
GATE_DIR <- tempfile("vignette-gate-")
dir.create(GATE_DIR, recursive = TRUE)
PRIVATE_LIB <- file.path(GATE_DIR, "lib")
dir.create(PRIVATE_LIB)

# Build the worktree's package into GATE_DIR, then install the tarball into
# PRIVATE_LIB. R CMD build works on a copy of the source, so the worktree is
# not written to. Returns NULL on success, else the failing step's output.
install_private <- function(worktree, gate_dir, lib) {
  r <- file.path(R.home("bin"), "R")
  built <- in_dir(gate_dir, system2(
    r, c("CMD", "build", "--no-build-vignettes", "--no-manual", shQuote(worktree)),
    stdout = TRUE, stderr = TRUE
  ))
  tarball <- list.files(gate_dir, pattern = "\\.tar\\.gz$", full.names = TRUE)
  if (!is.null(attr(built, "status")) || length(tarball) != 1L) {
    return(c("R CMD build failed:", built))
  }
  installed <- system2(
    r, c("CMD", "INSTALL", "-l", shQuote(lib), shQuote(tarball)),
    stdout = TRUE, stderr = TRUE
  )
  if (!is.null(attr(installed, "status"))) {
    return(c("R CMD INSTALL failed:", installed))
  }
  NULL
}

# Evaluate `code` with `dir` as the working directory, then return to the old one.
in_dir <- function(dir, code) {
  old <- setwd(dir)
  on.exit(setwd(old))
  code
}

# ---------------------------------------------------------------------------
# Build a library path containing ONLY what DESCRIPTION declares (plus the
# render harness and the transitive closure of both).
#
# Why: a vignette that uses a package the project does not DECLARE renders
# fine locally -- the developer happens to have it installed -- and then dies
# on the CI runner, which installs only DESCRIPTION's dependencies. That is
# not a hypothetical: pkgdown failed on `Fu_2022_atenolol_qsp` with "there is
# no package called 'units'" AFTER this gate had passed all 1215 vignettes,
# because `units` was present on the dev box and absent in CI. A gate that
# cannot go red for the thing CI goes red for is not a gate.
# ---------------------------------------------------------------------------
build_description_libpath <- function(worktree, gate_dir) {
  descfile <- file.path(worktree, "DESCRIPTION")
  if (!file.exists(descfile)) return(NULL)
  desc <- read.dcf(descfile)
  field <- function(f) {
    if (!f %in% colnames(desc)) return(character())
    v <- desc[1L, f]
    if (is.na(v)) return(character())
    v <- gsub("\\([^)]*\\)", "", v)          # drop version constraints
    v <- trimws(strsplit(v, ",")[[1]])
    setdiff(v[nzchar(v)], "R")
  }
  declared <- unique(unlist(lapply(c("Depends", "Imports", "Suggests", "LinkingTo"), field)))
  pkg      <- desc[1L, "Package"]
  # Build-time tooling: present in CI's pkgdown environment regardless of
  # DESCRIPTION, and required for a vignette to render at all.
  harness  <- c("rmarkdown", "knitr", "callr", "evaluate", "jsonlite",
                "yaml", "xfun", "digest", "htmltools", "processx", "ps")
  wanted   <- unique(c(declared, harness, pkg))

  ip      <- utils::installed.packages()
  present <- intersect(wanted, rownames(ip))
  missing <- setdiff(wanted, rownames(ip))
  deps <- tryCatch(
    unique(unlist(tools::package_dependencies(present, db = ip, recursive = TRUE))),
    error = function(e) character()
  )
  keep <- unique(c(present, deps))
  # base + recommended live in .Library and are always visible; don't relink.
  base_pkgs <- rownames(utils::installed.packages(lib.loc = .Library,
                                                  priority = c("base", "recommended")))
  keep <- setdiff(keep, base_pkgs)

  lib <- file.path(gate_dir, "desc-lib")
  dir.create(lib, recursive = TRUE, showWarnings = FALSE)
  linked <- 0L
  for (p in keep) {
    src <- suppressWarnings(find.package(p, quiet = TRUE))
    if (!length(src)) next
    dest <- file.path(lib, p)
    if (!file.exists(dest) && file.symlink(src[[1L]], dest)) linked <- linked + 1L
  }
  list(libpath = c(lib, .Library), linked = linked,
       declared = length(declared), missing = missing)
}

if (!SKIP_INSTALL) {
  cat(sprintf("--- installing the worktree's package into the gate's private library %s ---\n",
              PRIVATE_LIB))
  failed <- install_private(WORKTREE, GATE_DIR, PRIVATE_LIB)
  if (!is.null(failed)) {
    cat("INSTALL FAILED:", utils::tail(failed, 20L), sep = "\n")
    quit(status = 2L, save = "no")
  }
  # First on the path, so the DESCRIPTION-only library below links this copy.
  .libPaths(c(PRIVATE_LIB, .libPaths()))
}

LIBPATH <- .libPaths()
if (DESC_LIB) {
  info <- build_description_libpath(WORKTREE, GATE_DIR)
  if (is.null(info)) {
    cat("--- no DESCRIPTION found; falling back to the full library path ---\n")
  } else {
    LIBPATH <- info$libpath
    cat(sprintf(paste0("--- DESCRIPTION-only library path: %d packages linked ",
                       "(%d declared in DESCRIPTION + harness + transitive deps) ---\n"),
                info$linked, info$declared))
    if (length(info$missing)) {
      cat(sprintf("    WARNING: declared but NOT installed here: %s\n",
                  paste(info$missing, collapse = ", ")))
    }
    cat("    Vignettes that need an undeclared package will now FAIL here,\n")
    cat("    exactly as they would on the CI runner. Pass --full-lib to opt out.\n")
  }
}

# callr sets the workers' library path but not R_LIBS, which a process a
# vignette starts (an R subprocess, R CMD SHLIB) reads to find packages; the
# user library is left off too, as the DESCRIPTION-only path leaves it off.
WORKER_ENV <- c(
  callr::rcmd_safe_env(),
  R_LIBS = paste(LIBPATH, collapse = .Platform$path.sep),
  R_LIBS_USER = LIBPATH[[1L]]
)

rmds <- sort(list.files(file.path(WORKTREE, "vignettes/articles"),
                        pattern = "\\.Rmd$", full.names = TRUE))

# --only restricts the run to named vignettes (comma-separated, with or without
# the .Rmd suffix). Use it ONLY when the change under test cannot affect the
# rest: the registers are inputs to every render, so a branch that renames or
# retires a canonical, or edits R/conventions.R, still needs the full set. An
# incremental round that merely ADDS models (no removed register lines, no
# modified shared files) is the case this is for. Unknown names are a hard
# error rather than a silent no-op -- a typo must not look like a pass.
ONLY <- get_arg("--only", "")
if (nzchar(ONLY)) {
  want <- trimws(strsplit(ONLY, ",")[[1]])
  want <- sub("\\.Rmd$", "", want[nzchar(want)])
  have <- sub("\\.Rmd$", "", basename(rmds))
  missing <- setdiff(want, have)
  if (length(missing)) {
    cat(sprintf("--only names %d vignette(s) not present under vignettes/articles: %s\n",
                length(missing), paste(missing, collapse = ", ")))
    quit(status = 2L, save = "no")
  }
  rmds <- rmds[have %in% want]
  cat(sprintf("--- --only: restricted to %d of %d vignettes ---\n", length(rmds), length(have)))
}
if (!length(rmds)) {
  cat("no vignettes found under vignettes/articles/; nothing to validate\n")
  quit(status = 0L, save = "no")
}

cat(sprintf("--- rendering %d vignettes on %d cores (timeout %ds per vignette) ---\n",
            length(rmds), JOBS, TIMEOUT))
invisible(file.create(RESULTS))

render_one <- function(rmd) {
  t0 <- Sys.time()
  out <- tryCatch({
    callr::r(
      function(rmd, rxthreads) {
        suppressPackageStartupMessages({
          library(nlmixr2lib)
          library(rxode2)
        })
        # PIN THE THREAD COUNT. rxSetSeed() makes an rxode2 simulation
        # reproducible only for a GIVEN number of threads: the parallel RNG
        # streams are partitioned per thread, so the same seed on a different
        # thread count draws different numbers. A dev box solving on 16 threads
        # and a CI runner on 2 therefore produce different simulated cohorts
        # from identical source, and any vignette whose stopifnot() is tight
        # enough will pass here and fail there.
        #
        # That is not hypothetical: on the 2026-08-31 consolidation this gate
        # reported 1680/1682 green and CI then failed four vignettes
        # (Bardhi_2026_ampicillin_foal, Chatterjee_2016_pembrolizumab,
        # Lin_2024_adverseEvent_markov, Riccobene_2017_ceftaroline) on their own
        # assertions. All four pass at 16 threads and fail at 1, 2 and 4. A gate
        # that runs at the dev box's default thread count cannot see the class
        # of bug CI trips on, so pin low and match the runner.
        if (!is.null(rxthreads) && exists("setRxThreads", asNamespace("rxode2"))) {
          try(rxode2::setRxThreads(as.integer(rxthreads)), silent = TRUE)
        }
        # data.table, which rxode2 and the vignettes use, takes half the
        # cores too; at its default every worker would run 16 threads.
        if (!is.null(rxthreads) && requireNamespace("data.table", quietly = TRUE)) {
          data.table::setDTthreads(as.integer(rxthreads))
        }
        # FORCE error = FALSE. knitr's default renders a chunk error inline and
        # returns success, so a broken vignette silently "passes" -- the exact
        # false negative that shipped latent-broken vignettes to main before.
        knitr::opts_chunk$set(error = FALSE)
        outfile <- tempfile(fileext = ".html")
        # The worker's own temporary directory takes the knitted markdown
        # and the figures, which would otherwise land beside the Rmd.
        rmarkdown::render(rmd, output_file = outfile,
                          output_format = "html_document",
                          intermediates_dir = tempdir(),
                          quiet = TRUE, envir = new.env())
        list(ok = TRUE, msg = "", outfile = outfile)
      },
      args = list(rmd = rmd, rxthreads = RXTHREADS),
      libpath = LIBPATH,
      env = WORKER_ENV,
      timeout = TIMEOUT,
      show = FALSE
    )
  }, error = function(e) {
    list(ok = FALSE, msg = conditionMessage(e), outfile = NA_character_)
  })
  dt <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  out$file <- basename(rmd)
  out$secs <- dt
  # ONE write() call. `cat(json, "\n", ...)` emits the JSON and the newline as
  # two separate writes, so a second forked worker can append its record between
  # them -- producing `...}{"ok":true,...` on one line, which the per-line
  # fromJSON() below then rejects with "trailing garbage", failing the gate
  # after every vignette rendered fine. Observed 2026-08-20: 1516/1516 rendered
  # OK, gate exited non-zero. Pasting the newline in first makes it a single
  # O_APPEND write, which Linux serialises between processes.
  cat(paste0(jsonlite::toJSON(out, auto_unbox = TRUE), "\n"),
      file = RESULTS, append = TRUE)
  if (out$ok) {
    cat(sprintf("[OK   %5.1fs] %s\n", dt, out$file))
  } else {
    cat(sprintf("[FAIL %5.1fs] %s  --  %s\n",
                dt, out$file, substr(out$msg, 1, 200)))
  }
  invisible(out)
}

invisible(mclapply(rmds, render_one, mc.cores = JOBS, mc.preschedule = FALSE))

# Belt-and-braces against a torn append: split any line carrying more than one
# record before parsing, so an interleaved write degrades to a cosmetic blip
# rather than failing the gate.
.split_records <- function(line) {
  parts <- strsplit(gsub("\\}\\{", "}\n{", line), "\n", fixed = TRUE)[[1]]
  parts[nzchar(parts)]
}
res <- lapply(unlist(lapply(readLines(RESULTS), .split_records)), jsonlite::fromJSON)
ok_n   <- sum(vapply(res, function(x) isTRUE(x$ok), logical(1L)))
fail_n <- length(res) - ok_n
cat(sprintf("\nSUMMARY: %d ok / %d failed / %d total\n", ok_n, fail_n, length(res)))
if (fail_n > 0L) {
  cat("\nFAILURES:\n")
  for (r in res) if (!isTRUE(r$ok)) {
    cat(sprintf("  - %s\n      %s\n",
                r$file, substr(r$msg, 1, 400)))
  }
  quit(status = 1L, save = "no")
}
quit(status = 0L, save = "no")
