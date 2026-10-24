#!/usr/bin/env python3
"""Pick a varied sample of the SafeDocs parse pool into parsing/safedocs/.

Usage: tools/safedocs-parse-sample.py INVENTORY.tsv.gz [--budget BYTES] [--dry-run]

INVENTORY is tools/inventory.py's output for the files
index/safedocs-parse.tsv.gz pools. The sample fills buckets in order, each
up to a cap: where qpdf and PDFium disagree, crash, fail or repair; then the
index's damage buckets (what pdfinfo and Tika said); then what the files
hold: cross-reference kinds, bytes before the header and after %%EOF, many
updates, linearization, object streams, each encryption method, stream
filter, font type and header version; then the index's rarer features; and
last, files pdfa-eng-wds took (known good) from each of 40 producers.
Within a bucket files go in sha256 order, so reruns pick the same files. A
file counts once, for its first bucket. Files over 500 KB are skipped (2 MB
in the rare buckets), and selection stops at the byte budget (default
600 MB, leaving the rest of the parsing category's 1 GB to the curated sets
and the UNSAFE sample).

Copies the picks to parsing/safedocs/ (removing files no longer picked) and
rewrites the parsing/safedocs rows of parsing/manifest.tsv, with the
inventory's columns and the bucket that took each.
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
MANIFEST = os.path.join(ROOT, "parsing", "manifest.tsv")
CATEGORY_CAP = 1_000_000_000
MAX_FILE, MAX_RARE = 500_000, 2_000_000
DAMAGE = ["pdfinfo-error", "xref", "lexer", "stream", "structure", "not-pdf", "tika-error",
          "damaged-font", "page-disagree", "timeout"]
INVENTORY = ["size", "version", "header_offset", "after_eof", "eofs", "xref", "linearized",
             "objstms", "filters", "encryption", "qpdf", "qpdf_pages", "objects", "fonts",
             "unembedded", "qpdf_message", "pdfium", "pdfium_pages"]
OPENS = ("ok", "warned", "repaired")
FILTERS = {"ASCIIHexDecode", "ASCII85Decode", "LZWDecode", "FlateDecode", "RunLengthDecode",
           "CCITTFaxDecode", "JBIG2Decode", "DCTDecode", "JPXDecode", "Crypt", "AHx", "A85",
           "LZW", "Fl", "RL", "CCF", "DCT"}
FONTS = {"Type0", "Type1", "MMType1", "Type3", "TrueType", "CIDFontType0", "CIDFontType2"}
VERSIONS = {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "2.0", ""}
FAILS = ("format", "file", "unknown", "security")


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
    """Yield (name, cap, rare, predicate) in priority order."""
    yield "qpdf-fails-pdfium-opens", 100, True, lambda r: r["qpdf"] == "failed" and r["pdfium"] == "ok"
    yield "pdfium-fails-qpdf-opens", 100, True, lambda r: r["pdfium"] in FAILS and r["qpdf"] in OPENS
    yield "pages-differ", 100, True, lambda r: (r["qpdf_pages"] and r["pdfium_pages"]
                                                and r["qpdf_pages"] != r["pdfium_pages"])
    yield "crashed-or-hung", 100, True, lambda r: "crashed" in (r["qpdf"], r["pdfium"]) or \
        "timeout" in (r["qpdf"], r["pdfium"])
    yield "both-fail", 60, False, lambda r: r["qpdf"] == "failed" and r["pdfium"] in FAILS
    yield "qpdf-repaired", 100, False, lambda r: r["qpdf"] == "repaired"
    yield "qpdf-warned", 60, False, lambda r: r["qpdf"] == "warned"
    for b in DAMAGE:
        yield f"meta:{b}", 40, b in ("page-disagree", "timeout"), lambda r, b=b: b in r["bucket_set"]
    for x in ("broken", "none", "hybrid", "stream", "table"):
        yield f"xref:{x}", 40, False, lambda r, x=x: r["xref"] == x
    yield "header-offset", 50, False, lambda r: iv(r, "header_offset") > 0
    yield "after-eof", 50, False, lambda r: iv(r, "after_eof") > 0
    yield "updates", 50, False, lambda r: iv(r, "eofs") >= 10
    yield "linearized", 40, False, lambda r: r["linearized"] == "1"
    yield "objstms", 40, False, lambda r: iv(r, "objstms") > 0
    for e, _ in collections.Counter(r["encryption"] for r in rows if r["encryption"]).most_common(20):
        yield f"encryption:{e}", 20, False, lambda r, e=e: r["encryption"] == e
    for f in sorted(FILTERS):
        yield f"filter:{f}", 15, False, lambda r, f=f: f in r["filter_set"]
    yield "filter:unknown", 25, True, lambda r: bool(r["filter_set"] - FILTERS)
    for f in sorted(FONTS):
        yield f"font:{f}", 15, False, lambda r, f=f: f in r["font_set"]
    yield "font:other", 10, True, lambda r: bool(r["font_set"] - FONTS)
    yield "unembedded", 20, False, lambda r: iv(r, "unembedded") > 0
    for v in sorted(VERSIONS - {""}):
        yield f"version:{v}", 10, False, lambda r, v=v: r["version"] == v
    yield "version:other", 15, True, lambda r: r["version"] not in VERSIONS
    for b in ("password", "updates", "many-pages", "pdf-2.0", "odd-version", "html",
              "illustrator", "hint-tables"):
        yield f"meta:{b}", 20, b == "many-pages", lambda r, b=b: b in r["bucket_set"]
    good = collections.Counter(r["family"] for r in rows if "known-good" in r["bucket_set"])
    for fam, _ in good.most_common(40):
        yield f"known-good:{fam}", 6, False, lambda r, fam=fam: (
            "known-good" in r["bucket_set"] and r["family"] == fam)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("inventory")
    ap.add_argument("--budget", type=int, default=600_000_000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = {}
    with gzip.open(os.path.join(ROOT, "index", "safedocs-parse.tsv.gz"), "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            if r["pool"] == "1":
                index[r["file_name"]] = r
    rows = []
    with gzip.open(args.inventory, "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            meta = index.get(os.path.basename(r["file"]))
            if meta is None:
                continue
            r.update({k: meta[k] for k in ("file_name", "length", "digest", "url", "producer",
                                           "pdfinfo_exit", "pdfinfo_error", "tika_status",
                                           "tika_error", "buckets")})
            r["bucket_set"] = set(meta["buckets"].split(","))
            r["filter_set"] = set(f for f in r["filters"].split(",") if f)
            r["font_set"] = {f.split(":")[0] for f in r["fonts"].split(",") if f}
            r["family"] = producer_family(meta["producer"])
            rows.append(r)
    rows.sort(key=lambda r: r["digest"])

    picked, used, report = {}, 0, []
    for name, cap, rare, pred in buckets(rows):
        limit = MAX_RARE if rare else MAX_FILE
        n, start = 0, used
        for r in rows:
            if n >= cap or used >= args.budget:
                break
            if r["file_name"] in picked or int(r["length"]) > limit or not pred(r):
                continue
            picked[r["file_name"]] = (r, name)
            used += int(r["length"])
            n += 1
        report.append((name, n, used - start))
    width = max(len(n) for n, _, _ in report)
    for name, n, size in report:
        print(f"  {name:{width}} {n:4} {size / 1e6:6.1f} MB")
    others = sum(os.path.getsize(os.path.join(d, f))
                 for d, _, fs in os.walk(os.path.join(ROOT, "parsing"))
                 if not d.startswith(os.path.join(ROOT, "parsing", "safedocs")) for f in fs)
    print(f"picked {len(picked)} files, {used / 1e9:.2f} GB; parsing/ comes to "
          f"{(used + others) / 1e9:.2f} GB of its {CATEGORY_CAP / 1e9:.0f} GB")
    if args.dry_run:
        return

    dest = os.path.join(ROOT, "parsing", "safedocs")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if f not in picked:
            os.remove(os.path.join(dest, f))
    for name, (r, _) in picked.items():
        target = os.path.join(dest, name)
        if not os.path.exists(target):
            shutil.copyfile(os.path.join(ROOT, r["file"]), target)
    write_manifest(picked)


def note(r):
    said = []
    if r["pdfinfo_exit"] not in ("", "0") or r["pdfinfo_error"]:
        said.append(f"pdfinfo exit {r['pdfinfo_exit'] or '?'}"
                    + (f": {r['pdfinfo_error']}" if r["pdfinfo_error"] else ""))
    if r["tika_status"] not in ("", "OK") or r["tika_error"]:
        said.append(f"Tika {r['tika_status'] or '?'}"
                    + (f": {r['tika_error']}" if r["tika_error"] else ""))
    return "; ".join(said)


def write_manifest(picked):
    with open(MANIFEST, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        cols = reader.fieldnames
        keep = [r for r in reader if not r["path"].startswith("parsing/safedocs/")]
    cols += [c for c in ["bucket"] + INVENTORY if c not in cols]
    for name, (r, bucket) in sorted(picked.items()):
        row = {c: r.get(c, "") for c in INVENTORY}
        row.update({
            "path": f"parsing/safedocs/{name}", "source": r["url"], "license": "unknown",
            "sha256": r["digest"], "pages": r["pdfium_pages"] or r["qpdf_pages"],
            "expect": "", "password": "", "note": note(r), "bucket": bucket,
        })
        keep.append(row)
    keep.sort(key=lambda r: r["path"])
    with open(MANIFEST, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(keep)


main()
