# Model exports and automatic plot capture

Python figures are captured before `pyplot.close` and after visualization for
figures left open. Each figure object is captured at most once; reused figure
numbers do not discard the next figure. Automatic PNGs are retained under
`automatic/` with the other results.

When a model successfully calls `Figure.savefig`, the wrapper records the figure
object, the ordinary artist-mutation revision, the local export path and its hash.
After collecting files, a checked `plot-selection.json` receipt links an automatic
capture to a valid model export of the same tracked revision. The result view
then displays the model export and offers the automatic copy as a download.
The original export's resolution and styling are preserved. No additional
rendering pass, model-code change or numerical transformation is introduced.

Separate figures, changed figures and intentional additional model exports remain
visible. Deleted/overwritten exports, failed saves, outside or file-like export
destinations and lost mutation tracking keep the automatic fallback. If an
optional backup fails but the model export is valid, the export stays usable and
a warning records the unavailable backup. If no usable export or capture exists,
the capture failure remains visible in warnings as before.

The receipt reader checks relative paths, symbolic links, file sizes and both
hashes before suppressing a preview. Missing, invalid or stale metadata never
suppresses an image. This is a presentation policy, not a sandbox against model
code or a scientific equivalence/approval check. Author-written metadata named
`plot-selection.json` is retained under `model-files/` rather than overwritten.

Older saved results without the receipt remain unchanged. Their images are not
deleted or matched by filename/visual similarity. R capture and the existing
shared model/visualization namespace are unchanged. Numerical interchange
snapshotting is handled separately from this plotting change.

Run the neutral regressions from the repository root:

```sh
python tests/test_closed_python_figures.py
python tests/test_plot_export_preference.py
```

They require Matplotlib and use only `signal=2*x` with `x=2`. The original direct
PNG-reference comparisons remain, including the downloadable captures. Existing
pipeline, archive, workflow-state and interchange tests also remain applicable.
