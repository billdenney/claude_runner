# The checks that stop buildModelDb(), run before it, on what the merge changed.
#
# A cold buildModelDb() parses every model in the library, about an hour, and
# only then raises the convention errors it collected. Two kinds of error are
# found in minutes instead:
#
# * a canonical registered twice under one `- **Type:**` anywhere in
#   inst/references/*.md, the package's own .referenceDuplicateIssues(), which
#   buildModelDb() runs on the same files. It reads each register whole, so it
#   sees a canonical filed under two `##` sections, which the merge's
#   per-section dedup of compartment-names.md and parameter-names.md cannot;
# * an error-severity checkModelConventions() issue, or a parse or load
#   failure, in a model file the merge added or changed (--models). Each model
#   is loaded and checked with the same calls addFileToModelDb() makes, so the
#   model name the checks see, and so their verdict, is the build's.
#
# The package is loaded from the worktree with pkgload::load_all(), so the
# checks and the registers are the branch's, not the installed copy's. A
# package without buildModelDb() has nothing to check. A check function the
# package lacks is skipped with a warning: buildModelDb() still enforces
# whatever the package has.
#
# Invocation:
#
#   Rscript precheck_regen.R --worktree <path> --models <file>
#
# --models names a file with one repo-relative model path per line; a path the
# worktree lacks is skipped.
#
# Exit codes:
#   0  no error found, or nothing to check
#   1  a duplicate canonical, or a convention, parse or load error (listed)
#   2  bad arguments, or the package could not be loaded

args <- commandArgs(trailingOnly = TRUE)

get_arg <- function(name) {
  i <- which(args == name)
  if (length(i) != 1L || length(args) < i + 1L) {
    cat(sprintf("ERROR: (precheck) %s needs exactly one value\n", name))
    quit(status = 2L, save = "no")
  }
  args[[i + 1L]]
}

# The calls addFileToModelDb() makes for one model file, ending in its
# convention issues. `parsedFile` keeps the build's name: nlmixr() names the
# model after the expression it is given, and the covariate checks read that
# name.
model_issues <- function(path, check) {
  parsedFile <- parse(file = path)
  if (!identical(parsedFile[[1]][[1]], as.name("<-"))) {
    stop("the file does not start with an assignment of the model function")
  }
  modelName <- as.character(parsedFile[[1]][[2]])
  if (modelName != tools::file_path_sans_ext(basename(path))) {
    stop(sprintf("the model is named %s, the file %s", modelName, basename(path)))
  }
  mod <- suppressWarnings(suppressMessages(nlmixr2est::nlmixr(eval(parsedFile))))
  suppressWarnings(check(mod, verbose = FALSE))
}

one_line <- function(text) gsub("\\s+", " ", trimws(text))

worktree <- normalizePath(get_arg("--worktree"), mustWork = FALSE)
models_file <- get_arg("--models")
if (!dir.exists(worktree) || !file.exists(file.path(worktree, "DESCRIPTION"))) {
  cat(sprintf("ERROR: (precheck) %s is not a package worktree\n", worktree))
  quit(status = 2L, save = "no")
}
if (!file.exists(models_file)) {
  cat(sprintf("ERROR: (precheck) --models %s does not exist\n", models_file))
  quit(status = 2L, save = "no")
}
models <- readLines(models_file, warn = FALSE)
models <- models[nzchar(models)]
setwd(worktree)
pkg <- read.dcf("DESCRIPTION", fields = "Package")[[1L]]

if (!requireNamespace("pkgload", quietly = TRUE)) {
  cat("ERROR: (precheck) the pkgload package is not installed\n")
  quit(status = 2L, save = "no")
}
loaded <- tryCatch(
  {
    suppressMessages(pkgload::load_all(".", quiet = TRUE, helpers = FALSE))
    TRUE
  },
  error = function(e) {
    cat(sprintf("ERROR: (precheck) loading %s failed: %s\n", pkg, one_line(conditionMessage(e))))
    FALSE
  }
)
if (!loaded) quit(status = 2L, save = "no")

ns <- asNamespace(pkg)
has <- function(name) exists(name, envir = ns, inherits = FALSE) && is.function(get(name, envir = ns))
if (!has("buildModelDb")) {
  cat(sprintf("    (precheck) %s has no buildModelDb(); nothing to check\n", pkg))
  quit(status = 0L, save = "no")
}

errors <- 0L

if (has(".referenceDuplicateIssues")) {
  registers <- sort(Sys.glob(file.path("inst", "references", "*.md")))
  dups <- get(".referenceDuplicateIssues", envir = ns)(registers)
  dups <- dups[dups$severity == "error", , drop = FALSE]
  if (nrow(dups)) {
    errors <- errors + nrow(dups)
    cat(sprintf("ERROR: (precheck) %d canonical(s) registered twice:\n", nrow(dups)))
    for (i in seq_len(nrow(dups))) cat(sprintf("    %s\n", one_line(dups$message[[i]])))
  } else {
    cat(sprintf("    (precheck) no canonical is registered twice in %d register(s)\n", length(registers)))
  }
} else {
  cat(sprintf("WARNING: (precheck) %s has no .referenceDuplicateIssues(); duplicates are left to buildModelDb()\n", pkg))
}

present <- models[file.exists(models)]
if (!length(present)) {
  cat("    (precheck) the merge added or changed no model file\n")
} else if (!has("checkModelConventions")) {
  cat(sprintf("WARNING: (precheck) %s has no checkModelConventions(); conventions are left to buildModelDb()\n", pkg))
} else if (!requireNamespace("nlmixr2est", quietly = TRUE)) {
  cat("ERROR: (precheck) the nlmixr2est package is not installed\n")
  quit(status = 2L, save = "no")
} else {
  check <- get("checkModelConventions", envir = ns)
  cat(sprintf("    (precheck) checking the conventions of %d model(s) the merge added or changed\n", length(present)))
  failed <- 0L
  for (path in present) {
    found <- tryCatch(
      model_issues(path, check),
      error = function(e) {
        data.frame(
          category = "load", severity = "error", name = "(model)",
          message = one_line(conditionMessage(e)), stringsAsFactors = FALSE
        )
      }
    )
    bad <- found[found$severity == "error", , drop = FALSE]
    if (nrow(bad)) {
      failed <- failed + 1L
      errors <- errors + nrow(bad)
      cat(sprintf("ERROR: (precheck) %s:\n", path))
      for (i in seq_len(nrow(bad))) {
        cat(sprintf("    %s (%s): %s\n", bad$name[[i]], bad$category[[i]], one_line(bad$message[[i]])))
      }
    }
  }
  if (!failed) cat("    (precheck) no convention error in the models the merge added or changed\n")
}

if (errors) {
  cat(sprintf("\nSUMMARY: %d error(s); buildModelDb() would stop on them\n", errors))
  quit(status = 1L, save = "no")
}
quit(status = 0L, save = "no")
