"""Freeze optional upstream interchange files BEFORE visualization (Python 3.4 syntax)."""
import os
import tempfile


def capture(plan, namespace, run_id=""):
    if not plan.get("serialize_params"):
        return {}, []
    try:
        import interchange
        specs = plan["serialize_params"]
        if len(specs) > 256:
            raise ValueError("Interchange parameter limit exceeded")
        parameters = []
        warnings = []
        for spec in specs:
            pid = spec.get("id")
            if pid in namespace:
                item = dict(spec)
                item["value"] = namespace[pid]
                parameters.append(item)
            elif spec.get("classification") == "OUTPUT":
                warnings.append("Declared output '{}' is unavailable".format(pid))
        payloads, total = {}, 0
        with tempfile.TemporaryDirectory(prefix="fskx-interchange-") as temporary:
            _, notes = interchange.write_bundle(parameters, temporary, generator_language="Python",
                generated_by={"tool": "FSKX Runner", "modelId": plan.get("model_id") or "", "runId": run_id})
            warnings.extend(notes)
            for directory, dirs, files in os.walk(temporary):
                for name in files:
                    source = os.path.join(directory, name)
                    if os.path.islink(source):
                        raise ValueError("Interchange links are forbidden")
                    size = os.path.getsize(source)
                    total += size
                    if size > 8 * 1024 * 1024 or total > 16 * 1024 * 1024 or len(payloads) >= 256:
                        raise ValueError("Interchange file limit exceeded")
                    with open(source, "rb") as stream:
                        payloads[os.path.relpath(source, temporary)] = stream.read()
        return payloads, warnings
    except Exception as exc:
        return {}, ["Interchange unavailable; original results preserved: " + str(exc)[:500]]


def publish(payloads, outdir):
    for name, raw in payloads.items():
        path = os.path.join(outdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(raw)
