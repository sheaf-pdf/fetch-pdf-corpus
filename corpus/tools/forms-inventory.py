#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pypdf>=6", "cryptography"]
# ///
"""Inventory the form features of every PDF under a directory, one TSV row each.

Usage: tools/forms-inventory.py DIR OUT.tsv [--procs N]

Columns: field counts by type (comb and multiline are text-field flags),
fields carrying calculate / format / keystroke / validate JavaScript
(custom_calc: calculate scripts that don't use AFSimple_Calculate), the
calculation order length (co), document-level scripts, XFA and the
<script> elements in it, and the Acrobat AF* functions called, with counts.
Each file gets 60 s; a timeout or parse error is recorded, not fatal.
"""
import argparse
import collections
import logging
import multiprocessing
import os
import re
import signal
import sys

from pypdf import PdfReader
from pypdf.generic import IndirectObject

logging.disable(logging.CRITICAL)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ["file", "status", "pages", "encrypted", "xfa", "xfa_scripts", "fields", "text",
        "check", "radio", "choice", "push", "sig", "comb", "multiline", "calc", "custom_calc",
        "format", "keystroke", "validate", "co", "doc_js", "need_appearances", "af", "error"]


class Timeout(Exception):
    pass


def on_alarm(*_):
    raise Timeout()


def res(o):
    return o.get_object() if isinstance(o, IndirectObject) else o


def js_text(action):
    try:
        action = res(action)
        js = res(action.get("/JS")) if action is not None else None
    except Exception:
        return ""
    if js is None:
        return ""
    if hasattr(js, "get_data"):
        return js.get_data().decode("latin-1", "replace")
    return str(js)


def field_type(f):
    ft, ff = f.get("/FT"), int(f.get("/Ff", 0) or 0)
    if ft == "/Btn":
        return "push" if ff & (1 << 16) else "radio" if ff & (1 << 15) else "check"
    return {"/Tx": "text", "/Ch": "choice", "/Sig": "sig"}.get(ft, "other")


def walk(node, inherited, out, depth=0):
    """Collect terminal fields, carrying inherited /FT and /Ff down."""
    node = res(node)
    if depth > 50 or not hasattr(node, "get"):
        return
    attrs = dict(inherited)
    for k in ("/FT", "/Ff"):
        if k in node:
            attrs[k] = node[k]
    kids = node.get("/Kids")
    kids = res(kids) if kids is not None else None
    # A field whose kids are all widgets (no /T) is terminal.
    if kids and any("/T" in res(k) for k in kids if hasattr(res(k), "get")):
        for k in kids:
            walk(k, attrs, out, depth + 1)
        return
    merged = dict(node)
    for k, v in attrs.items():
        merged.setdefault(k, v)
    out.append(merged)


def inventory(path):
    rec = {"status": "ok"}
    r = PdfReader(path, strict=False)
    rec["encrypted"] = int(r.is_encrypted)
    if r.is_encrypted:
        r.decrypt("")
    root = r.trailer["/Root"]
    rec["pages"] = len(r.pages)
    acro = res(root.get("/AcroForm"))
    js_all = []
    names = res(root.get("/Names"))
    rec["doc_js"] = 0
    if names and "/JavaScript" in names:
        arr = res(res(names["/JavaScript"]).get("/Names", []))
        for i in range(1, len(arr), 2):
            rec["doc_js"] += 1
            js_all.append(js_text(arr[i]))
    types, scripts = collections.Counter(), collections.Counter()
    fields = []
    if acro:
        xfa = acro.get("/XFA")
        rec["xfa"] = int(xfa is not None)
        if xfa is not None:
            xfa = res(xfa)
            parts = [xfa] if hasattr(xfa, "get_data") else [res(xfa[i]) for i in range(1, len(xfa), 2)]
            rec["xfa_scripts"] = sum(len(re.findall(rb"<script\b", p.get_data()))
                                     for p in parts if hasattr(p, "get_data"))
        for f in res(acro.get("/Fields", [])) or []:
            walk(f, {}, fields)
        rec["co"] = len(res(acro.get("/CO", [])) or [])
        rec["need_appearances"] = int(bool(acro.get("/NeedAppearances")))
    for f in fields:
        t = field_type(f)
        types[t] += 1
        ff = int(f.get("/Ff", 0) or 0)
        if t == "text" and ff & (1 << 24):
            types["comb"] += 1
        if t == "text" and ff & (1 << 12):
            types["multiline"] += 1
        aa = res(f.get("/AA"))
        if not aa or not hasattr(aa, "get"):
            continue
        for key, name in (("/C", "calc"), ("/F", "format"), ("/K", "keystroke"), ("/V", "validate")):
            if key in aa:
                text = js_text(aa[key])
                if text.strip():
                    scripts[name] += 1
                    js_all.append(text)
                    if name == "calc" and "AFSimple_Calculate" not in text:
                        scripts["custom_calc"] += 1
    rec["fields"] = len(fields)
    rec.update(types)
    rec.update(scripts)
    af = collections.Counter()
    for text in js_all:
        af.update(re.findall(r"\b(AF\w+)\s*\(", text))
    rec["af"] = ";".join(f"{k}:{v}" for k, v in af.most_common())
    return rec


def work(path):
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(60)
    try:
        rec = inventory(path)
    except Timeout:
        rec = {"status": "timeout"}
    except RecursionError:
        rec = {"status": "error", "error": "RecursionError"}
    except Exception as e:
        rec = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        signal.alarm(0)
    rec["file"] = os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else path
    return "\t".join(str(rec.get(c, "")).replace("\t", " ").replace("\n", " ") for c in COLS)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dir")
    ap.add_argument("out")
    ap.add_argument("--procs", type=int, default=os.cpu_count())
    args = ap.parse_args()
    top = os.path.abspath(args.dir)
    paths = sorted(os.path.join(d, f) for d, _, fs in os.walk(top) for f in fs if f.endswith(".pdf"))
    sys.setrecursionlimit(5000)
    with open(args.out, "w") as fh, multiprocessing.Pool(args.procs, maxtasksperchild=200) as pool:
        fh.write("\t".join(COLS) + "\n")
        for i, line in enumerate(pool.imap_unordered(work, paths, chunksize=4), 1):
            fh.write(line + "\n")
            if i % 5000 == 0:
                print(f"{i}/{len(paths)}", flush=True)
    print(f"done: {len(paths)} files", flush=True)


if __name__ == "__main__":
    main()
