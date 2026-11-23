# Optional rejection of conflicting workflow imports

Existing callers retain their behavior. Importing a `.fskxp` workflow creates a new
pipeline and restores missing model/result files. Existing local files are kept,
even when an incoming file with the same destination has different contents.
This remains the default and never overwrites a local file.

To reject such conflicts, check **Reject conflicting imports** beside the workflow
import controls. It starts unchecked when the page loads and is not persisted in
settings. The import then compares existing models and results with the incoming
files before copying any file or saving a pipeline. Equal bytes are accepted,
regardless of timestamps. Different bytes, a directory where a file is required,
or a file blocking a required parent directory stop the import. The message lists
the conflicting archive paths. Use a different destination or resolve the local
conflicts before retrying; there is no implicit overwrite or fallback to keep-local.

For example, an archive supplies `results/A/run-1/outputs.json` containing
`signal=9`, while the same local path contains `signal=4`:

| Policy | Local result | Import outcome |
| --- | --- | --- |
| `keep_local` (default) | Still `signal=4` | Success; missing files are restored and a new pipeline is saved |
| `reject` (opt in) | Still `signal=4` | Conflict error; no files restored and no pipeline saved |

The default can mix existing local artifacts with restored files. Choose `reject`
when the workflow must not silently pick up different local contents. This policy
does not validate scientific results, model versions or result provenance beyond
the compared bytes. Old archives, the FSKX format and existing results are unchanged.

## Python and HTTP interfaces

```python
from pipeline_store import import_archive, ArchiveConflictError

record = import_archive(path)  # unchanged default, same return shape
record = import_archive(path, conflict_policy="keep_local")  # explicit default
try:
    record = import_archive(path, conflict_policy="reject")
except ArchiveConflictError as exc:  # also a ValueError for existing handlers
    print(exc.paths)  # sorted archive-relative paths, not absolute host paths
```

`conflict_policy` is keyword-only. Existing three-argument calls remain valid.
Other values (including booleans and `None`) raise `ValueError` before importing.

`POST /api/pipelines/import` accepts multipart fields `file` and the optional
`conflict_policy=keep_local|reject`. Omission preserves the existing default.
Success remains HTTP 200 with `id` and `name`. Reject-mode content conflicts return
HTTP 409 with `error` and `conflicts`. Invalid policy values and malformed archives
return HTTP 400; the caller must not retry a conflict under another policy without
an explicit user choice. Temporary uploaded files are cleaned up on success and
failure. The UI displays the error and leaves the current workflow selected.

Existing archive size, path, link and duplicate-entry checks apply in both modes.
Export uses the same limits (`MAX_ARCHIVE_*` in `pipeline_store.py`), so an archive the tool
writes can always be imported again; one over the limits fails at export with a clear message
(HTTP 400 from the export endpoint) and leaves no temporary file. An import resolves and
contains every target before it restores anything, so a failure in either policy leaves the
destination unchanged.
Reject mode additionally refuses distinct entries that resolve to the same target,
so normalized path aliases cannot bypass preflight. Comparisons stream in 64 KiB
chunks. This is a preflight for conflicts in a stable filesystem, not a transaction
or lock: later disk failures and concurrent filesystem modifications can still
cause a partial import and are not rolled back. Executing model code is outside
the scope of these checks.

## Verification

```sh
python tests/test_pipeline_archive_boundaries.py
python tests/test_pipeline_import_conflicts.py
python tests/test_pipeline_import_api.py
node tests/test_pipeline_import_ui.js
```

The API tests require Flask and the Runner's usual dependencies. The UI test uses
Node's standard library and a small DOM/fetch stub; it does not replace a browser
layout test. All fixtures contain only temporary neutral files, not real models.
