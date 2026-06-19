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

setwd(WORKTREE)

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

rmds <- sort(list.files(file.path(WORKTREE, "vignettes/articles"),
                        pattern = "\\.Rmd$", full.names = TRUE))
if (!length(rmds)) {
  cat("no vignettes found under vignettes/articles/; nothing to validate\n")
  quit(status = 0L, save = "no")
}

cat(sprintf("--- rendering %d vignettes on %d cores (timeout %ds per vignette) ---\n",
            length(rmds), JOBS, TIMEOUT))
file.create(RESULTS)

render_one <- function(rmd) {
  t0 <- Sys.time()
  out <- tryCatch({
    callr::r(
      function(rmd) {
        suppressPackageStartupMessages({
          library(nlmixr2lib)
          library(rxode2)
        })
        outfile <- tempfile(fileext = ".html")
        rmarkdown::render(rmd, output_file = outfile,
                          output_format = "html_document",
                          quiet = TRUE, envir = new.env())
        list(ok = TRUE, msg = "", outfile = outfile)
      },
      args = list(rmd = rmd),
      timeout = TIMEOUT,
      show = FALSE
    )
  }, error = function(e) {
    list(ok = FALSE, msg = conditionMessage(e), outfile = NA_character_)
  })
  dt <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  out$file <- basename(rmd)
  out$secs <- dt
  cat(jsonlite::toJSON(out, auto_unbox = TRUE), "\n",
      sep = "", file = RESULTS, append = TRUE)
  if (out$ok) {
    cat(sprintf("[OK   %5.1fs] %s\n", dt, out$file))
  } else {
    cat(sprintf("[FAIL %5.1fs] %s  --  %s\n",
                dt, out$file, substr(out$msg, 1, 200)))
  }
  invisible(out)
}

invisible(mclapply(rmds, render_one, mc.cores = JOBS, mc.preschedule = FALSE))

res <- lapply(readLines(RESULTS), jsonlite::fromJSON)
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
