# interchange.R — FSKX Model-Interchange format, v1.0.0 (R side, WRITER only).
#
# Mirrors the WRITER in interchange.py so an R model run can emit outputs.json natively.
# The READER and RENDERER stay in Python: injecting a value always happens in the Python
# orchestration layer (it emits a source literal), so no R reader is needed.
#
# Requires jsonlite (already a wrapper dependency). All functions are prefixed `ic_`.
# JSON element indices in `specials` are 0-based to match the Python side.

ic_FORMAT_VERSION  <- "1.0.0"
ic_VECTOR_INLINE_MAX <- 1000
ic_MATRIX_INLINE_MAX <- 10000  # cells

ic_safe_name <- function(s) {
  s <- gsub("[^A-Za-z0-9._-]", "_", as.character(s))
  if (length(s) == 0 || !nzchar(s)) "value" else s
}

ic_finite_or_special <- function(x) {
  x <- suppressWarnings(as.numeric(x))
  if (length(x) == 0 || is.na(x) || is.nan(x)) return(list(value = NA, special = "NaN"))
  if (is.infinite(x)) return(list(value = NA, special = if (x > 0) "Infinity" else "-Infinity"))
  list(value = x, special = NULL)
}

ic_col_type <- function(col) {
  if (is.logical(col)) return("BOOLEAN")
  if (is.integer(col)) return("INTEGER")
  if (is.numeric(col)) return("DOUBLE")
  "STRING"
}

# --- per-dataType encoders -------------------------------------------------

ic_encode_number <- function(x, dt, outdir, base) {
  if (identical(dt, "INTEGER")) {
    iv <- suppressWarnings(as.integer(x))
    if (length(iv) == 1 && !is.na(iv)) {
      return(list(dataType = "INTEGER", encoding = "inline", value = iv))
    }
  }
  fs <- ic_finite_or_special(x)
  out <- list(dataType = if (nzchar(dt)) dt else "DOUBLE",
              encoding = "inline", value = fs$value)
  if (!is.null(fs$special)) out$special <- fs$special
  out
}

ic_encode_bool   <- function(x, ...) list(dataType = "BOOLEAN", encoding = "inline",
                                          value = as.logical(x)[1])
ic_encode_string <- function(x, ...) list(dataType = "STRING", encoding = "inline",
                                          value = as.character(x)[1])
ic_encode_date   <- function(x, ...) list(dataType = "DATE", encoding = "inline",
                                          value = format(as.Date(x), "%Y-%m-%d"))
ic_encode_file   <- function(x, ...) list(dataType = "FILE", encoding = "ref",
                                          ref = list(path = basename(as.character(x)[1]),
                                                     format = "raw",
                                                     mediaType = "application/octet-stream"))

ic_encode_vector <- function(x, outdir, base) {
  x <- suppressWarnings(as.numeric(x))
  n <- length(x)
  if (!is.null(outdir) && n > ic_VECTOR_INLINE_MAX) {
    path <- paste0(base, ".csv")
    utils::write.table(x, file.path(outdir, path), row.names = FALSE,
                       col.names = FALSE, sep = ",")
    return(list(dataType = "VECTOROFNUMBERS", encoding = "ref", shape = I(n),
                ref = list(path = path, format = "csv", mediaType = "text/csv",
                           header = FALSE)))
  }
  specials <- list()
  vals <- x
  for (i in which(!is.finite(x))) {
    xi <- x[i]
    specials[[as.character(i - 1)]] <-
      if (is.na(xi) || is.nan(xi)) "NaN" else if (xi > 0) "Infinity" else "-Infinity"
    vals[i] <- NA
  }
  out <- list(dataType = "VECTOROFNUMBERS", encoding = "inline",
              value = I(vals), shape = I(n))
  if (length(specials)) out$specials <- specials
  out
}

ic_encode_matrix <- function(x, outdir, base) {
  m <- as.matrix(x)
  nr <- nrow(m); nc <- ncol(m)
  if (!is.null(outdir) && nr * nc > ic_MATRIX_INLINE_MAX) {
    path <- paste0(base, ".csv")
    utils::write.table(m, file.path(outdir, path), row.names = FALSE,
                       col.names = FALSE, sep = ",")
    return(list(dataType = "MATRIXOFNUMBERS", encoding = "ref", shape = I(c(nr, nc)),
                ref = list(path = path, format = "csv", mediaType = "text/csv",
                           header = FALSE)))
  }
  specials <- list(); idx <- 0
  rows <- vector("list", nr)
  for (r in seq_len(nr)) {
    rowvals <- suppressWarnings(as.numeric(m[r, ]))
    for (c in seq_len(nc)) {
      xrc <- m[r, c]
      if (!is.finite(xrc)) {
        specials[[as.character(idx)]] <-
          if (is.na(xrc) || is.nan(xrc)) "NaN" else if (xrc > 0) "Infinity" else "-Infinity"
        rowvals[c] <- NA
      }
      idx <- idx + 1
    }
    rows[[r]] <- I(rowvals)
  }
  out <- list(dataType = "MATRIXOFNUMBERS", encoding = "inline",
              value = rows, shape = I(c(nr, nc)))
  if (length(specials)) out$specials <- specials
  out
}

ic_encode_object <- function(x, outdir, base) {
  # 1. dataframe-first.
  if (is.data.frame(x) && !is.null(outdir)) {
    path <- paste0(base, ".csv")
    utils::write.csv(x, file.path(outdir, path), row.names = FALSE)
    cols <- list()
    for (cn in names(x)) cols[[cn]] <- ic_col_type(x[[cn]])
    return(list(dataType = "OBJECT", encoding = "ref", shape = I(c(nrow(x), ncol(x))),
                columns = cols,
                ref = list(path = path, format = "csv", mediaType = "text/csv",
                           header = TRUE)))
  }
  # 2. JSON-serializable best-effort.
  ok <- tryCatch({ jsonlite::toJSON(x, auto_unbox = TRUE); TRUE },
                 error = function(e) FALSE)
  if (ok) return(list(dataType = "OBJECT", encoding = "inline", value = x))
  # 3. partial.
  list(dataType = "OBJECT", encoding = "inline", partial = TRUE,
       value = paste(utils::capture.output(utils::str(x)), collapse = "\n"))
}

ic_encode_value <- function(value, data_type, outdir = NULL, base = "value") {
  dt <- toupper(if (is.null(data_type)) "" else data_type)
  base <- ic_safe_name(base)
  switch(dt,
    NUMBER = , DOUBLE = , INTEGER = ic_encode_number(value, dt, outdir, base),
    BOOLEAN = ic_encode_bool(value),
    STRING  = ic_encode_string(value),
    DATE    = ic_encode_date(value),
    VECTOROFNUMBERS = ic_encode_vector(value, outdir, base),
    MATRIXOFNUMBERS = ic_encode_matrix(value, outdir, base),
    FILE    = ic_encode_file(value),
    OBJECT  = ic_encode_object(value, outdir, base),
    ic_encode_object(value, outdir, base)  # default: best-effort object
  )
}

# --- bundle writer ---------------------------------------------------------

ic_write_bundle <- function(parameters, outdir, generator_language = "R",
                            generated_by = NULL) {
  items <- list(); warnings <- character(0)
  model_id <- if (!is.null(generated_by$modelId)) generated_by$modelId else ""
  for (p in parameters) {
    data <- tryCatch(ic_encode_value(p$value, p$dataType, outdir, p$id),
                     error = function(e) {
                       warnings <<- c(warnings,
                         paste0("could not serialize output '", p$id, "': ",
                                conditionMessage(e)))
                       NULL
                     })
    if (is.null(data)) next
    if (isTRUE(data$partial)) {
      warnings <- c(warnings, paste0("output '", p$id,
                                     "' is a non-tabular OBJECT; serialized partially"))
    }
    dtp <- toupper(if (is.null(p$dataType)) "" else p$dataType)
    meta <- list(id = p$id, dataType = if (nzchar(dtp)) dtp else data$dataType)
    for (k in c("name", "unit", "classification", "description")) {
      if (!is.null(p[[k]])) meta[[k]] <- p[[k]]
    }
    items[[length(items) + 1]] <- list(metadata = meta, modelId = model_id, data = data)
  }
  bundle <- list(formatVersion = ic_FORMAT_VERSION,
                 generatorLanguage = generator_language, parameters = items)
  if (!is.null(generated_by)) bundle$generatedBy <- generated_by
  path <- file.path(outdir, "outputs.json")
  writeLines(jsonlite::toJSON(bundle, auto_unbox = TRUE, na = "null",
                              digits = NA, pretty = TRUE), path)
  list(path = path, warnings = warnings)
}
