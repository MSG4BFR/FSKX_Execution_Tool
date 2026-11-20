# Capture typed interchange bytes before visualization changes shared values.
capture_interchange <- function(plan, model_env, runner_dir, run_id = "") {
  if (is.null(plan$serialize_params) || !length(plan$serialize_params)) return(list(files = list(), warnings = character()))
  tryCatch({
    module <- new.env(parent = baseenv())
    source(file.path(runner_dir, "interchange.R"), local = module)
    specs <- plan$serialize_params
    count <- if (is.data.frame(specs)) nrow(specs) else length(specs)
    if (count > 256L) stop("Interchange parameter limit exceeded")
    parameters <- list(); warnings <- character()
    for (i in seq_len(count)) {
      spec <- if (is.data.frame(specs)) as.list(specs[i, , drop = FALSE]) else specs[[i]]
      pid <- as.character(spec$id)
      if (exists(pid, envir = model_env, inherits = FALSE)) {
        spec$value <- get(pid, envir = model_env, inherits = FALSE)
        parameters[[length(parameters) + 1L]] <- spec
      } else if (identical(spec$classification, "OUTPUT")) {
        warnings <- c(warnings, paste("Declared output unavailable:", pid))
      }
    }
    temporary <- tempfile("fskx-interchange-"); dir.create(temporary)
    on.exit(unlink(temporary, recursive = TRUE), add = TRUE)
    result <- module$ic_write_bundle(parameters, temporary, "R", list(tool = "FSKX Runner", modelId = plan$model_id, runId = run_id))
    names <- list.files(temporary, recursive = TRUE)
    sizes <- file.info(file.path(temporary, names))$size
    if (length(names) > 256L || any(sizes > 8*1024*1024) || sum(sizes) > 16*1024*1024) stop("Interchange file limit exceeded")
    payloads <- lapply(seq_along(names), function(i) readBin(file.path(temporary, names[i]), "raw", sizes[i]))
    names(payloads) <- names
    list(files = payloads, warnings = c(warnings, result$warnings))
  }, error = function(error) list(files = list(), warnings = paste("Interchange unavailable; original results preserved:", conditionMessage(error))))
}

publish_interchange <- function(receipt, outdir) {
  for (name in names(receipt$files)) {
    target <- file.path(outdir, name)
    dir.create(dirname(target), recursive = TRUE, showWarnings = FALSE)
    writeBin(receipt$files[[name]], target)
  }
}
