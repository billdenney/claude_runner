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
#     --results <path>                      # JSONL output; default: /tmp/vignette_results.jsonl
#     --log <path>                          # human-readable log; default: stdout (no -- arg)
#     --full-lib                            # opt OUT of the DESCRIPTION-only library path
#                                           # (see build_description_libpath below); the gate
#                                           # then sees every package installed on the machine,
#                                           # which can hide a missing CI dependency.
#     --skip-install                        # skip remotes::install_local (worker process loads
#                                           # via library(); needed when modeldb has changed)
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
TIMEOUT  <- as.integer(get_arg("--timeout", 900L))
RESULTS  <- get_arg("--results", "/tmp/vignette_results.jsonl")
SKIP_INSTALL <- has_flag("--skip-install")
# DESCRIPTION-only library path: ON by default. --full-lib opts out.
DESC_LIB <- !has_flag("--full-lib")

setwd(WORKTREE)

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
build_description_libpath <- function(worktree) {
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

  lib <- file.path(tempdir(), "vignette-gate-desc-lib")
  unlink(lib, recursive = TRUE); dir.create(lib, recursive = TRUE, showWarnings = FALSE)
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
  cat("--- installing local package (so callr workers see the worktree version) ---\n")
  ok <- tryCatch({
    remotes::install_local(WORKTREE, upgrade = "never", force = TRUE, quiet = TRUE)
    TRUE
  }, error = function(e) {
    cat("INSTALL FAILED:", conditionMessage(e), "\n")
    FALSE
  })
  if (!ok) quit(status = 2L, save = "no")
}

LIBPATH <- .libPaths()
if (DESC_LIB) {
  info <- build_description_libpath(WORKTREE)
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

rmds <- sort(list.files(file.path(WORKTREE, "vignettes/articles"),
                        pattern = "\\.Rmd$", full.names = TRUE))
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
      function(rmd) {
        suppressPackageStartupMessages({
          library(nlmixr2lib)
          library(rxode2)
        })
        # FORCE error = FALSE. knitr's default renders a chunk error inline and
        # returns success, so a broken vignette silently "passes" -- the exact
        # false negative that shipped latent-broken vignettes to main before.
        knitr::opts_chunk$set(error = FALSE)
        outfile <- tempfile(fileext = ".html")
        rmarkdown::render(rmd, output_file = outfile,
                          output_format = "html_document",
                          quiet = TRUE, envir = new.env())
        list(ok = TRUE, msg = "", outfile = outfile)
      },
      args = list(rmd = rmd),
      libpath = LIBPATH,
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
