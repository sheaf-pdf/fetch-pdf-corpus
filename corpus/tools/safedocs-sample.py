#!/usr/bin/env python3
"""Pick a varied sample of the SafeDocs pool into forms/safedocs/.

Usage: tools/safedocs-sample.py INVENTORY.tsv [--budget BYTES] [--dry-run]

INVENTORY.tsv is tools/forms-inventory.py's output for pool/safedocs. The sample
fills buckets in order, each up to a cap: scripted forms first (custom and
AFSimple calculations, each AF* function, validate, document JavaScript),
then XFA, field types, encryption, languages, producers, federal hosts and
files the inventory couldn't parse. Within a bucket files go in sha256 order,
so reruns pick the same files. A file counts once, for its first bucket.
Selection stops at the byte budget (default 2.5 GB).

Copies the picks to forms/safedocs/ (removing files no longer picked) and
rewrites the forms/safedocs rows of forms/manifest.tsv.
"""
import argparse
import collections
import csv
import gzip
import os
import re
import shutil

csv.field_size_limit(1 << 30)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_FILE = 1_500_000  # per-file cap, except in the rare buckets below
MAX_RARE = 3_000_000
RARE = {"custom-calc", "simple-calc", "calc-order", "xfa-only", "validate"}


def iv(r, k):
    try:
        return int(r.get(k) or 0)
    except ValueError:
        return 0


def producer_family(p):
    p = re.sub(r"\(.*?\)|;.*", " ", p.lower())
    p = re.sub(r"[^a-zà-ÿ ]+", " ", p)
    return " ".join(p.split()[:3]) or "(none)"


def buckets(rows):
    """Yield (name, cap, predicate) in priority order."""
    yield "custom-calc", 600, lambda r: iv(r, "custom_calc") > 0
    yield "simple-calc", 200, lambda r: iv(r, "calc") > iv(r, "custom_calc")
    yield "calc-order", 50, lambda r: iv(r, "co") > 0
    yield "validate", 150, lambda r: iv(r, "validate") > 0
    af = collections.Counter(k for r in rows for k in r["af_set"])
    for fn, _ in af.most_common():
        yield f"af:{fn}", 25, lambda r, fn=fn: fn in r["af_set"]
    yield "doc-js", 100, lambda r: iv(r, "doc_js") > 0
    yield "xfa-only", 300, lambda r: iv(r, "xfa") and iv(r, "fields") == 0
    yield "xfa-hybrid", 100, lambda r: iv(r, "xfa") and iv(r, "fields") > 0
    for t, cap in (("sig", 100), ("comb", 100), ("radio", 100), ("choice", 100),
                   ("push", 75), ("multiline", 75), ("need_appearances", 100)):
        yield t, cap, lambda r, t=t: iv(r, t) > 0
    yield "many-fields", 50, lambda r: iv(r, "fields") >= 500
    yield "encrypted", 150, lambda r: iv(r, "encrypted") > 0
    yield "unparsed", 50, lambda r: r["status"] != "ok"
    langs = collections.Counter(r["lang"] for r in rows)
    for lang, _ in langs.most_common(40):
        yield f"lang:{lang}", 20, lambda r, lang=lang: r["lang"] == lang
    fams = collections.Counter(r["family"] for r in rows)
    for fam, _ in fams.most_common(60):
        yield f"producer:{fam}", 8, lambda r, fam=fam: r["family"] == fam
    yield "federal", 200, lambda r: r["federal"] == "1"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("inventory")
    ap.add_argument("--budget", type=int, default=2_500_000_000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = {}
    with gzip.open(os.path.join(ROOT, "index", "safedocs-forms.tsv.gz"), "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            index[r["file_name"]] = r
    rows = []
    with open(args.inventory, newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            meta = index[os.path.basename(r["file"])]
            r.update({k: meta[k] for k in ("file_name", "length", "digest", "lang", "federal", "url",
                                           "host")})
            r["family"] = producer_family(meta["producer"])
            r["af_set"] = {kv.rsplit(":", 1)[0] for kv in r["af"].split(";") if kv}
            rows.append(r)
    rows.sort(key=lambda r: r["digest"])

    picked, used, report = {}, 0, []
    for name, cap, pred in buckets(rows):
        limit = MAX_RARE if name in RARE else MAX_FILE
        n = 0
        for r in rows:
            if n >= cap or used >= args.budget:
                break
            if r["file_name"] in picked or int(r["length"]) > limit or not pred(r):
                continue
            picked[r["file_name"]] = (r, name)
            used += int(r["length"])
            n += 1
        report.append((name, n))
    width = max(len(n) for n, _ in report)
    for name, n in report:
        print(f"  {name:{width}} {n}")
    print(f"picked {len(picked)} files, {used / 1e9:.2f} GB")
    if args.dry_run:
        return

    dest = os.path.join(ROOT, "forms", "safedocs")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if f not in picked:
            os.remove(os.path.join(dest, f))
    for name, (r, _) in picked.items():
        target = os.path.join(dest, name)
        if not os.path.exists(target):
            shutil.copyfile(os.path.join(ROOT, r["file"]), target)
    write_manifest(picked)


def write_manifest(picked):
    path = os.path.join(ROOT, "forms", "manifest.tsv")
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        cols = reader.fieldnames
        keep = [r for r in reader if not r["path"].startswith("forms/safedocs/")]
    for name, (r, _) in sorted(picked.items()):
        types = ",".join(f"{t}:{iv(r, t)}" for t in ("check", "choice", "push", "radio", "sig", "text")
                         if iv(r, t))
        scripts = ",".join(f"{label}:{iv(r, k)}" for label, k in
                           (("calculate", "calc"), ("format", "format"), ("keystroke", "keystroke"),
                            ("validate", "validate")) if iv(r, k))
        kind = "xfa-only" if iv(r, "xfa") and not iv(r, "fields") else "hybrid" if iv(r, "xfa") else "acroform"
        keep.append({
            "path": f"forms/safedocs/{name}", "form": "", "title": "",
            "pages": r["pages"], "kind": kind if r["status"] == "ok" else "unparsed",
            "fields": r["fields"], "field_types": types or "-", "field_scripts": scripts or "-",
            "source": r["url"], "license": "unknown", "sha256": r["digest"],
        })
    keep.sort(key=lambda r: r["path"])
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(keep)


main()
