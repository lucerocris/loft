"""Files attached to a new task: pasted screenshots, dropped or picked files.

The app uploads each file (POST /api/uploads?name=<file name>, raw body) before the task is
created; it lands in <data>/attachments/<8 hex>/<name> and the task keeps the paths. When the
agent starts, the paths are listed under its prompt and the attachments folder is added with
--add-dir, so it can Read them (images included) without asking.
"""
import os
import re
import uuid
from pathlib import Path

from . import paths

MAX_BYTES = 50_000_000
MAX_FILES = 20


def root():
    return paths.data_dir() / "attachments"


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f/]", "", name).strip(". ") or "file"
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        name = stem[:120 - len(ext[:16])] + ext[:16]
    return name


def save(name, data):
    """Store one upload. Returns its record {path, name, size}."""
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"Files up to {MAX_BYTES // 1_000_000} MB.")
    d = root() / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = d / safe_name(name)
    p.write_bytes(data)
    return {"path": str(p), "name": p.name, "size": len(data)}


def clean(files):
    """Keep only paths that are real uploads (inside the attachments folder)."""
    out, base = [], os.path.realpath(root())
    for f in (files or [])[:MAX_FILES]:
        if not isinstance(f, str):
            continue
        rp = os.path.realpath(os.path.expanduser(f))
        if rp.startswith(base + os.sep) and os.path.isfile(rp) and rp not in out:
            out.append(rp)
    return out


def prompt_with(prompt, files):
    files = [f for f in files or [] if os.path.isfile(f)]
    if not files:
        return prompt
    lines = "\n".join(f"- {f}" for f in files)
    return (f"{prompt}\n\nAttached file{'s' if len(files) > 1 else ''} "
            f"(open {'them' if len(files) > 1 else 'it'} with the Read tool; images show as images):\n{lines}")


def add_dir_args(files):
    return ["--add-dir", str(root())] if any(os.path.isfile(f) for f in files or []) else []
