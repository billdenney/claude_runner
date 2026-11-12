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
# * in a model file the merge added or changed (--models), what stops
#   addFileToModelDb(): a file it cannot load (a parse error, a first
#   expression that is no assignment, a model named unlike its file, an
#   nlmixr() failure), and an error-severity checkModelConventions() issue.
#   Each model is loaded and checked with the calls addFileToModelDb() makes,
#   so the model name the checks see, and so their verdict, is the build's.
#   An error raised inside checkModelConventions() itself is a warning here,
#   since addFileToModelDb() ignores it too.
#
# The package is loaded from the worktree with pkgload::load_all(), so the
# checks and the registers are the branch's, not the installed copy's. A
# package without buildModelDb() has nothing to check. One with
# buildModelDb() but without .referenceDuplicateIssues() or
# checkModelConventions() cannot be checked as the build checks it, which is an
# error: a rename upstream must not switch the pre-check off.
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
#   1  a duplicate canonical, a model that cannot be loaded, or a convention
#      error (listed)
#   2  it could not run: bad arguments, the package could not be loaded, a
#      check function or package it needs is missing, or it crashed

one_line <- function(text) gsub("\\s+", " ", trimws(text))

# The value of flag `name`, or NULL when it is not given exactly once with one.
get_arg <- function(args, name) {
  i <- which(args == name)
  if (length(i) != 1L || length(args) < i + 1L) {
    return(NULL)
  }
  args[[i + 1L]]
}

has_function <- function(ns, name) {
  exists(name, envir = ns, inherits = FALSE) && is.function(get(name, envir = ns))
}

# The calls addFileToModelDb() makes to load one model file; an error here
# stops the build. `parsedFile` keeps the build's name: nlmixr() names the
# model after the expression it is given, and the covariate checks read that
# name.
load_model <- function(path) {
  parsedFile <- parse(file = path)
  if (!identical(parsedFile[[1]][[1]], as.name("<-"))) {
    stop("the file does not start with an assignment of the model function")
  }
  modelName <- as.character(parsedFile[[1]][[2]])
  if (modelName != tools::file_path_sans_ext(basename(path))) {
    stop(sprintf("the model is named %s, the file %s", modelName, basename(path)))
  }
  suppressWarnings(suppressMessages(nlmixr2est::nlmixr(eval(parsedFile))))
}

# A check's error-severity rows; NULL or a result without them has none.
error_rows <- function(found) {
  if (!is.data.frame(found) || !nrow(found) || !"severity" %in% names(found)) {
    return(NULL)
  }
  bad <- found[found$severity %in% "error", , drop = FALSE]
  if (nrow(bad)) bad else NULL
}

check_registers <- function(dups_check) {
  registers <- sort(Sys.glob(file.path("inst", "references", "*.md")))
  dups <- error_rows(dups_check(registers))
  if (is.null(dups)) {
    cat(sprintf("    (precheck) no canonical is registered twice in %d register(s)\n", length(registers)))
    return(0L)
  }
  cat(sprintf("ERROR: (precheck) %d canonical(s) registered twice:\n", nrow(dups)))
  for (i in seq_len(nrow(dups))) cat(sprintf("    %s\n", one_line(dups$message[[i]])))
  nrow(dups)
}

check_models <- function(models, conventions_check) {
  present <- models[file.exists(models)]
  if (!length(present)) {
    cat("    (precheck) the merge added or changed no model file\n")
    return(0L)
  }
  cat(sprintf("    (precheck) checking the conventions of %d model(s) the merge added or changed\n", length(present)))
  errors <- 0L
  for (path in present) {
    mod <- tryCatch(load_model(path), error = function(e) e)
    if (inherits(mod, "error")) {
      errors <- errors + 1L
      cat(sprintf("ERROR: (precheck) %s cannot be loaded, so buildModelDb() would stop: %s\n",
                  path, one_line(conditionMessage(mod))))
      next
    }
    found <- tryCatch(suppressWarnings(conventions_check(mod, verbose = FALSE)), error = function(e) e)
    if (inherits(found, "error")) {
      cat(sprintf("WARNING: (precheck) %s: checkModelConventions() failed, which buildModelDb() ignores too: %s\n",
                  path, one_line(conditionMessage(found))))
      next
    }
    bad <- error_rows(found)
    if (!is.null(bad)) {
      errors <- errors + nrow(bad)
      cat(sprintf("ERROR: (precheck) %s:\n", path))
      for (i in seq_len(nrow(bad))) {
        cat(sprintf("    %s (%s): %s\n", bad$name[[i]], bad$category[[i]], one_line(bad$message[[i]])))
      }
    }
  }
  if (!errors) cat("    (precheck) no convention error in the models the merge added or changed\n")
  errors
}

main <- function(args) {
  for (flag in c("--worktree", "--models")) {
    if (is.null(get_arg(args, flag))) {
      cat(sprintf("ERROR: (precheck) %s needs exactly one value\n", flag))
      return(2L)
    }
  }
  worktree <- normalizePath(get_arg(args, "--worktree"), mustWork = FALSE)
  models_file <- get_arg(args, "--models")
  if (!dir.exists(worktree) || !file.exists(file.path(worktree, "DESCRIPTION"))) {
    cat(sprintf("ERROR: (precheck) %s is not a package worktree\n", worktree))
    return(2L)
  }
  if (!file.exists(models_file)) {
    cat(sprintf("ERROR: (precheck) --models %s does not exist\n", models_file))
    return(2L)
  }
  models <- readLines(models_file, warn = FALSE)
  models <- models[nzchar(models)]
  setwd(worktree)
  pkg <- read.dcf("DESCRIPTION", fields = "Package")[[1L]]

  if (!requireNamespace("pkgload", quietly = TRUE)) {
    cat("ERROR: (precheck) the pkgload package is not installed\n")
    return(2L)
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
  if (!loaded) return(2L)

  ns <- asNamespace(pkg)
  if (!has_function(ns, "buildModelDb")) {
    cat(sprintf("    (precheck) %s has no buildModelDb(); nothing to check\n", pkg))
    return(0L)
  }
  for (needed in c(".referenceDuplicateIssues", "checkModelConventions")) {
    if (!has_function(ns, needed)) {
      cat(sprintf("ERROR: (precheck) %s has buildModelDb() but no %s(), so the pre-check cannot run the build's checks\n",
                  pkg, needed))
      return(2L)
    }
  }
  if (length(models[file.exists(models)]) && !requireNamespace("nlmixr2est", quietly = TRUE)) {
    cat("ERROR: (precheck) the nlmixr2est package is not installed\n")
    return(2L)
  }

  errors <- check_registers(get(".referenceDuplicateIssues", envir = ns))
  errors <- errors + check_models(models, get("checkModelConventions", envir = ns))
  if (errors) {
    cat(sprintf("\nSUMMARY: %d error(s); buildModelDb() would stop on them\n", errors))
    return(1L)
  }
  0L
}

status <- tryCatch(
  main(commandArgs(trailingOnly = TRUE)),
  error = function(e) {
    cat(sprintf("ERROR: (precheck) crashed: %s\n", one_line(conditionMessage(e))))
    2L
  }
)
quit(status = status, save = "no")
