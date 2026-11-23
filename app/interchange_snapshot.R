# Capture typed interchange bytes before visualization changes shared values.
capture_interchange <- function(plan, model_env, runner_dir, run_id = "") {
  if (is.null(plan$serialize_params) || !length(plan$serialize_params)) return(list(files = list(), warnings = character()))
  tryCatch({
    # Parent = the search path *below* the global environment: model variables are not visible
    # to the serializer, but attached base packages are. A bare baseenv() hid utils/stats, and
    # on R 3.6 utils::write.csv re-dispatches to write.table from the caller's frame, which
    # silently dropped every data-frame (OBJECT) output.
    module <- new.env(parent = parent.env(globalenv()))
    source(file.path(runner_dir, "interchange.R"), local = module)
    specs <- plan$serialize_params
    count <- if (is.data.frame(specs)) nrow(specs) else length(specs)
    if (count > 256L) stop("Interchange parameter limit exceeded")
    parameters <- list(); warnings <- character()
    for (i in seq_len(count)) {
      spec <- if (is.data.frame(specs)) as.list(specs[i, , drop = FALSE]) else specs[[i]]
      # jsonlite turns a missing name/unit into NA (or NULL); omit those fields, as the
      # serializer expects, instead of writing "name": null into the bundle.
      spec <- spec[vapply(spec, function(v) !(is.null(v) || (length(v) == 1L && is.na(v))), logical(1))]
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
    model_id <- plan$model_id
    if (!is.character(model_id) || length(model_id) != 1L || is.na(model_id)) model_id <- ""
    result <- module$ic_write_bundle(parameters, temporary, "R", list(tool = "FSKX Runner", modelId = model_id, runId = run_id))
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
