#!/usr/bin/env python3
"""Pick a varied sample of a SafeDocs pool into a category: conformance, scans or signatures.

Usage: tools/safedocs-category-sample.py CATEGORY INVENTORY.tsv.gz [--budget BYTES] [--dry-run]

INVENTORY is tools/inventory.py's output, with the category's --feature, for
the files index/safedocs-<CATEGORY>.tsv.gz pools. The sample fills buckets
in order, each up to a cap; within a bucket files go in sha256 order, so
reruns pick the same files, and a file counts once, for its first bucket.
Files over the category's size cap are skipped, and selection stops at the
budget, which with the category's curated sets keeps it under 1 GB:

  conformance  each claim the XMP makes; claimed files qpdf or PDFium find
               damaged; marked or tagged files that claim nothing; each
               output intent type; files with attachments; then 4 from each
               of 40 producers. 1 MB a file, 400 MB.
  scans        each kind (a scan, one with an OCR layer, one with visible
               text over it, and files that only looked like scans); each
               image filter; 1-, 8- and 24-bit images; four resolution
               bands; 10 and 50 pages or more; then 4 from each of 40
               producers. 3 MB a file, 600 MB.
  signatures   each /SubFilter; each certification level; document
               timestamps; /DSS; 2 and 4 or more signatures; files changed
               after their last signature; signatures whose digest doesn't
               match, or whose value doesn't check out; files pyHanko can't
               read; signature fields with nothing signed; then 3 signed
               files from each of 40 producers. 1 MB a file, 300 MB.

Copies the picks to <CATEGORY>/safedocs/ (removing files no longer picked)
and rewrites the <CATEGORY>/safedocs rows of <CATEGORY>/manifest.tsv, with
the inventory's columns and the bucket that took each.
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
CATEGORY_CAP = 1_000_000_000
SETTINGS = {"conformance": (1_000_000, 400_000_000), "scans": (3_000_000, 600_000_000),
            "signatures": (1_000_000, 300_000_000)}
OPENS = ("ok", "warned", "repaired")


def iv(r, k):
    try:
        return int(float(r.get(k) or 0))
    except ValueError:
        return 0


def producer_family(p):
    p = re.sub(r"\(.*?\)|;.*", " ", p.lower())
    p = re.sub(r"[^a-zà-ÿ ]+", " ", p)
    return " ".join(p.split()[:3]) or "(none)"


def spread(rows, pred, n, families=40):
    for fam, _ in collections.Counter(r["family"] for r in rows if pred(r)).most_common(families):
        yield f"producer:{fam}", n, lambda r, fam=fam: pred(r) and r["family"] == fam


def conformance(rows):
    for c, _ in collections.Counter(r["claims"] for r in rows if r["claims"]).most_common(30):
        yield f"claim:{c}", 25, lambda r, c=c: r["claims"] == c
    yield "claimed-but-damaged", 40, lambda r: r["claims"] and (
        r["qpdf"] not in ("ok",) or r["pdfium"] != "ok")
    yield "marked-unclaimed", 25, lambda r: not r["claims"] and r["marked"] == "1"
    yield "struct-tree-unclaimed", 25, lambda r: not r["claims"] and r["struct_tree"] == "1"
    intents = collections.Counter(i for r in rows for i in r["output_intents"].split(",") if i)
    for i, _ in intents.most_common(10):
        yield f"output-intent:{i}", 20, lambda r, i=i: i in r["output_intents"].split(",")
    yield "attachments", 20, lambda r: iv(r, "embedded_files") > 0
    yield "no-lang", 15, lambda r: r["claims"] and not r["lang"]
    yield from spread(rows, lambda r: True, 4)


def scans(rows):
    for k, cap in (("scan", 60), ("scan-ocr", 60), ("scan-text", 30), ("no-text", 30),
                   ("text", 15)):
        yield f"kind:{k}", cap, lambda r, k=k: r["kind"] == k
    scanned = lambda r: r["kind"].startswith("scan")
    for f, _ in collections.Counter(f for r in rows if scanned(r)
                                    for f in r["image_filters"].split(",") if f).most_common(8):
        yield f"filter:{f}", 25, lambda r, f=f: scanned(r) and f in r["image_filters"].split(",")
    for b in ("1", "8", "24"):
        yield f"bits:{b}", 25, lambda r, b=b: scanned(r) and b in r["bits"].split(",")
    for lo, hi in ((0, 150), (150, 250), (250, 350), (350, 10_000)):
        yield f"dpi:{lo}-{hi}", 20, lambda r, lo=lo, hi=hi: scanned(r) and r["dpi"] and \
            lo <= iv(r, "dpi") < hi
    yield "pages:10+", 30, lambda r: scanned(r) and iv(r, "pages") >= 10
    yield "pages:50+", 15, lambda r: scanned(r) and iv(r, "pages") >= 50
    yield from spread(rows, scanned, 4)


def signatures(rows):
    signed = lambda r: iv(r, "signatures") > 0
    subfilters = collections.Counter(s.split(":")[0] for r in rows
                                     for s in r["subfilters"].split(",") if s)
    for s, _ in subfilters.most_common(10):
        yield f"subfilter:{s}", 25, lambda r, s=s: s in [x.split(":")[0]
                                                        for x in r["subfilters"].split(",")]
    for p in ("1", "2", "3"):
        yield f"certified:P{p}", 15, lambda r, p=p: r["certified"] == p
    yield "timestamps", 20, lambda r: iv(r, "timestamps") > 0
    yield "dss", 20, lambda r: r["dss"] == "1"
    yield "signatures:4+", 10, lambda r: iv(r, "signatures") >= 4
    yield "signatures:2+", 25, lambda r: iv(r, "signatures") >= 2
    yield "changed-after-signing", 30, lambda r: signed(r) and r["last_coverage"] not in (
        "ENTIRE_FILE", "")
    yield "digest-mismatch", 25, lambda r: signed(r) and iv(r, "intact") < iv(r, "signatures")
    yield "value-fails", 15, lambda r: signed(r) and iv(r, "valid") < iv(r, "intact")
    yield "unreadable", 15, lambda r: r["signatures_read"] not in ("ok", "")
    yield "empty-fields", 20, lambda r: not signed(r) and iv(r, "empty_fields") > 0
    yield from spread(rows, signed, 3)


BUCKETS = {"conformance": conformance, "scans": scans, "signatures": signatures}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("category", choices=sorted(BUCKETS))
    ap.add_argument("inventory")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cat = args.category
    max_file, budget = SETTINGS[cat]
    budget = args.budget or budget

    index = {}
    with gzip.open(os.path.join(ROOT, "index", f"safedocs-{cat}.tsv.gz"), "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            if r["pool"] == "1":
                index[r["file_name"]] = r
    rows = []
    with gzip.open(args.inventory, "rt", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        inventory_cols = [c for c in reader.fieldnames if c != "file"]
        for r in reader:
            meta = index.get(os.path.basename(r["file"]))
            if meta is None:
                continue
            r.update({k: meta[k] for k in ("file_name", "length", "digest", "url", "producer",
                                           "buckets")})
            r["pages"] = r["pdfium_pages"] or r["qpdf_pages"] or meta["pages"]
            r["family"] = producer_family(meta["producer"] or meta["creator_tool"])
            rows.append(r)
    rows.sort(key=lambda r: r["digest"])

    picked, used, report = {}, 0, []
    for name, cap, pred in BUCKETS[cat](rows):
        n, start = 0, used
        for r in rows:
            if n >= cap or used >= budget:
                break
            if r["file_name"] in picked or int(r["length"]) > max_file or not pred(r):
                continue
            picked[r["file_name"]] = (r, name)
            used += int(r["length"])
            n += 1
        report.append((name, n, used - start))
    width = max(len(n) for n, _, _ in report)
    for name, n, size in report:
        print(f"  {name:{width}} {n:4} {size / 1e6:6.1f} MB")
    dest = os.path.join(ROOT, cat, "safedocs")
    others = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(os.path.join(ROOT, cat))
                 if not d.startswith(dest) for f in fs)
    print(f"picked {len(picked)} files, {used / 1e9:.2f} GB; {cat}/ comes to "
          f"{(used + others) / 1e9:.2f} GB of its {CATEGORY_CAP / 1e9:.0f} GB")
    if args.dry_run:
        return
    if used + others > CATEGORY_CAP:
        raise SystemExit("over the category's cap; lower --budget")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if f not in picked:
            os.remove(os.path.join(dest, f))
    for name, (r, _) in picked.items():
        target = os.path.join(dest, name)
        if not os.path.exists(target):
            shutil.copyfile(os.path.join(ROOT, r["file"]), target)
    write_manifest(cat, picked, inventory_cols)


def write_manifest(cat, picked, inventory_cols):
    manifest = os.path.join(ROOT, cat, "manifest.tsv")
    cols, keep = ["path", "source", "license", "sha256", "pages", "expect", "password", "note"], []
    if os.path.exists(manifest):
        with open(manifest, newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            cols = reader.fieldnames
            keep = [r for r in reader if not r["path"].startswith(f"{cat}/safedocs/")]
    cols += [c for c in ["bucket"] + inventory_cols if c not in cols]
    for name, (r, bucket) in sorted(picked.items()):
        row = {c: r.get(c, "") for c in inventory_cols}
        row.update({"path": f"{cat}/safedocs/{name}", "source": r["url"], "license": "unknown",
                    "sha256": r["digest"], "pages": r["pages"], "expect": "", "password": "",
                    "note": "", "bucket": bucket})
        keep.append(row)
    keep.sort(key=lambda r: r["path"])
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(keep)


main()
