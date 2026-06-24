# Executed INSIDE a model's micromamba environment (Rscript).
#
# Reproduces FSK-Lab execution semantics: parameter script, model script and the
# (optional) visualization script are sourced into ONE session, in that order.
# Plots are captured to PNG; top-level objects are serialized to results.json and the
# full session to workspace.RData.
#
# Usage: Rscript run_r_model.R <workdir> <outdir>

args <- commandArgs(trailingOnly = TRUE)
workdir <- normalizePath(args[1])
outdir  <- args[2]
dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
outdir <- normalizePath(outdir)

# Directory this wrapper lives in (so we can source interchange.R beside it, in both the
# APP_DIR and the synced /work/_runner/ docker location).
.allargs <- commandArgs(trailingOnly = FALSE)
.file_arg <- sub("^--file=", "", .allargs[grepl("^--file=", .allargs)])
runner_dir <- if (length(.file_arg)) dirname(normalizePath(.file_arg[1])) else getwd()

setwd(workdir)

# The engine resolves the actual script names (roles come from metadata.rdf, so they
# need NOT be model.r / visualization.r) and writes them here. Fall back to the
# conventional names if the plan is absent (older extractions).
plan <- list()
plan_path <- file.path(workdir, "_run_plan.json")
if (file.exists(plan_path) && requireNamespace("jsonlite", quietly = TRUE)) {
  plan <- tryCatch(jsonlite::fromJSON(plan_path), error = function(e) list())
}
param_script <- if (!is.null(plan$param_script)) plan$param_script else "params.R"
model_script <- if (!is.null(plan$model_script)) plan$model_script else "model.r"
viz_script   <- if (!is.null(plan$visualization_script)) plan$visualization_script else "visualization.r"

# Snapshot modification times (RECURSIVELY) so we capture files created OR overwritten
# during the run, wherever the model writes them — models may save outputs into
# subfolders, not just the work-dir root. Names are paths relative to workdir.
script_files <- c(basename(param_script), basename(model_script),
                  basename(viz_script), "workspace.RData", "_run_plan.json")
.before_files <- list.files(workdir, recursive = TRUE, all.files = FALSE)
.before_mtimes <- setNames(file.info(file.path(workdir, .before_files))$mtime,
                           .before_files)

status <- list(ok = TRUE, error = NULL, plots = character(0), files = character(0))

# Resolve a script name case-insensitively (archives vary: model.r vs model.R).
resolve_ci <- function(name) {
  if (file.exists(name)) return(name)
  hits <- list.files(".", pattern = paste0("^", gsub("\\.", "\\\\.", name), "$"),
                     ignore.case = TRUE)
  if (length(hits) > 0) return(hits[1])
  NA_character_
}

# `print_eval = TRUE` makes top-level visible values autoprint, the way R does at the
# REPL / under Rscript. Visualization scripts commonly END on a bare plot object
# (e.g. `ggplot(...) + geom_line(...)`) and rely on that autoprint to render it — without
# it the ggplot/lattice object is built but never drawn, so the device stays empty.
# (Base-graphics calls draw as side effects, so they don't need this.)
run_one <- function(name, print_eval = FALSE) {
  f <- resolve_ci(name)
  if (!is.na(f)) source(f, local = FALSE, echo = FALSE, print.eval = print_eval)
}

# Open a PNG device BEFORE visualization so base-graphics plots are captured.
png_pattern <- file.path(outdir, "plot_%03d.png")
grDevices::png(filename = png_pattern, width = 1100, height = 750, res = 130)
dev_opened <- TRUE

# Snapshot existing names so we only serialize objects the MODEL creates (params,
# intermediate and output variables), not this runner's own internals.
.initial_objs <- ls()

# Params + model are the core: a failure here is fatal.
result <- tryCatch({
  run_one(param_script)        # generated parameter overrides
  run_one(model_script)        # the model itself
  TRUE
}, error = function(e) {
  status$ok <<- FALSE
  status$error <<- paste(conditionMessage(e), collapse = "\n")
  FALSE
})

# Visualization is best-effort: a partial plot is still captured by the open device.
if (isTRUE(result) && !is.null(viz_script) && nzchar(viz_script)) {
  tryCatch(run_one(viz_script, print_eval = TRUE), error = function(e) {
    status$warnings <<- paste(viz_script, "error:", conditionMessage(e))
  })
}

if (dev_opened) try(grDevices::dev.off(), silent = TRUE)

# Names of objects created by the model (computed before we add more locals below).
.model_objs <- setdiff(ls(), c(.initial_objs, ".initial_objs", "result"))

# List captured plots.
plots <- list.files(outdir, pattern = "^plot_\\d+\\.png$")
status$plots <- plots

# Save the full session.
try(save.image(file.path(outdir, "workspace.RData")), silent = TRUE)

# Copy files the model created or overwrote during the run (CSV outputs, images written
# into subfolders, …), preserving their relative paths under outdir.
for (nf in list.files(workdir, recursive = TRUE, all.files = FALSE)) {
  if (basename(nf) %in% script_files || startsWith(nf, ".")) next
  src <- file.path(workdir, nf)
  if (!file.exists(src) || dir.exists(src)) next
  # `.before_mtimes` is an atomic POSIXct vector: `[[` on an absent name errors
  # ("subscript out of bounds") rather than returning NULL, so check membership
  # first. A name not present means the file is NEW → keep it (don't skip).
  prev <- if (nf %in% names(.before_mtimes)) .before_mtimes[[nf]] else NA
  if (!is.na(prev) && file.info(src)$mtime == prev) next  # pre-existing & untouched
  dest <- file.path(outdir, nf)
  dir.create(dirname(dest), showWarnings = FALSE, recursive = TRUE)
  ok <- tryCatch({ file.copy(src, dest, overwrite = TRUE); TRUE },
                 error = function(e) FALSE)
  if (ok) status$files <- c(status$files, nf)
}

# Serialize top-level objects: scalars / short vectors as values, matrices and
# data frames to CSV.
res <- list()
for (obj in .model_objs) {
  v <- tryCatch(get(obj), error = function(e) NULL)
  if (is.null(v) || is.function(v)) next
  if (is.matrix(v) || is.data.frame(v)) {
    csv_name <- paste0(obj, ".csv")
    try(write.csv(v, file.path(outdir, csv_name)), silent = TRUE)
    res[[obj]] <- paste0("<", class(v)[1], " dim=",
                         paste(dim(v), collapse = "x"), " -> ", csv_name, ">")
    status$files <- c(status$files, csv_name)
  } else if ((is.numeric(v) || is.character(v) || is.logical(v)) && length(v) <= 1000) {
    res[[obj]] <- v
  } else {
    res[[obj]] <- paste0("<", class(v)[1], " length=", length(v), ">")
  }
}

if (requireNamespace("jsonlite", quietly = TRUE)) {
  writeLines(jsonlite::toJSON(res, auto_unbox = TRUE, force = TRUE, null = "null",
                              digits = 8),
             file.path(outdir, "results.json"))
}

# Emit the typed interchange bundle (model-joining Phase 1): the model's declared
# parameters (INPUT/CONSTANT/OUTPUT, each tagged with classification), serialized in the
# language-neutral format. Best-effort — failures become warnings, never a failed run.
if (isTRUE(result) && !is.null(plan$serialize_params) && length(plan$serialize_params)) {
  ic_ok <- tryCatch({ source(file.path(runner_dir, "interchange.R"), local = FALSE); TRUE },
                    error = function(e) FALSE)
  if (ic_ok) {
    op <- plan$serialize_params  # jsonlite simplifies an array of objects to a data.frame
    n_out <- if (is.data.frame(op)) nrow(op) else length(op)
    params <- list()
    for (i in seq_len(n_out)) {
      if (is.data.frame(op)) {
        pid <- as.character(op$id[i])
        dt  <- if (!is.null(op$dataType)) as.character(op$dataType[i]) else ""
        cls <- if (!is.null(op$classification)) as.character(op$classification[i]) else ""
        nm  <- if (!is.null(op$name) && !is.na(op$name[i])) op$name[i] else NULL
        un  <- if (!is.null(op$unit) && !is.na(op$unit[i])) op$unit[i] else NULL
      } else {
        row <- op[[i]]; pid <- row$id; dt <- row$dataType; cls <- row$classification
        nm <- row$name; un <- row$unit
      }
      if (exists(pid, envir = .GlobalEnv, inherits = TRUE)) {
        val <- tryCatch(get(pid, envir = .GlobalEnv, inherits = TRUE),
                        error = function(e) NULL)
        params[[length(params) + 1]] <- list(id = pid, dataType = dt, classification = cls,
                                              name = nm, unit = un, value = val)
      } else if (identical(toupper(cls), "OUTPUT")) {
        status$warnings <- c(status$warnings,
                             paste0("declared OUTPUT '", pid, "' not found; skipped"))
      }
    }
    gen <- list(tool = "FSKX Runner",
                modelId = if (!is.null(plan$model_id)) plan$model_id else "",
                runId = basename(outdir))
    wb <- tryCatch(ic_write_bundle(params, outdir, "R", gen),
                   error = function(e) {
                     status$warnings <<- c(status$warnings,
                       paste("interchange bundle not written:", conditionMessage(e)))
                     NULL
                   })
    if (!is.null(wb)) status$warnings <- c(status$warnings, wb$warnings)
  } else {
    status$warnings <- c(status$warnings, "interchange.R could not be sourced; outputs.json skipped")
  }
}

if (requireNamespace("jsonlite", quietly = TRUE)) {
  writeLines(jsonlite::toJSON(status, auto_unbox = TRUE, force = TRUE, null = "null"),
             file.path(outdir, "status.json"))
}

if (!isTRUE(result)) {
  message(status$error)
  quit(status = 1)
}
