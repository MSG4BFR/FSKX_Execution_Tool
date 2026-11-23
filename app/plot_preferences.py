"""Checked plot-presentation preferences; never removes result artifacts.

The wrapper writes the receipt after collecting model files. It associates a
capture with a model export of the same tracked figure revision. These hashes
protect the presentation decision against later file changes; they are not a
sandbox or a scientific equivalence/approval claim.
"""
import hashlib
import json
from pathlib import Path
import re

HASH = re.compile(r"[0-9a-f]{64}\Z")


def contained(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        relative = path.relative_to(root)
        current = root
        if root.is_symlink(): return False
        for part in relative.parts:
            if part in {'.', '..'}: return False
            current = current / part
            if current.is_symlink(): return False
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024: return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def verified(path, digest, root):
    try:
        if not contained(root, path) or not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024 * 1024:
            return False
        actual, size = hashlib.sha256(), 0
        with path.open('rb') as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block: break
                size += len(block)
                if size > 64 * 1024 * 1024: return False
                actual.update(block)
        return actual.hexdigest() == digest
    except (OSError, ValueError):
        return False


def preferred_plot_exports(directory):
    """Hash-verified presentation choices; every underlying artifact is retained."""
    root = Path(directory)
    receipt = root / 'plot-selection.json'
    if not contained(root, receipt): return {}
    value = _json(receipt)
    pairs = value.get('pairs')
    if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('preference') != 'model_export'
            or not isinstance(pairs, list) or len(pairs) > 64): return {}
    result = {}
    automatic_names = {p.get('automatic') for p in pairs
                       if isinstance(p, dict) and isinstance(p.get('automatic'), str)}
    for pair in pairs:
        if not isinstance(pair, dict): continue
        automatic, exported = pair.get('automatic'), pair.get('export')
        if (not isinstance(automatic, str) or not re.fullmatch(r'automatic/plot_[1-9][0-9]*\.png', automatic)
                or not isinstance(exported, str) or exported in automatic_names
                or any(c in exported for c in ['\\', ':', '\x00'])
                or any(part in {'', '.', '..'} for part in exported.split('/'))
                or not exported.lower().endswith(('.png', '.svg', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.pdf'))): continue
        first, second = pair.get('automatic_sha256'), pair.get('export_sha256')
        if not isinstance(first, str) or not HASH.fullmatch(first) or not isinstance(second, str) or not HASH.fullmatch(second): continue
        if verified(root / automatic, first, root) and verified(root / exported, second, root):
            result[automatic] = exported
    return result
