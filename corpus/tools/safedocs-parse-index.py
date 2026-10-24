#!/usr/bin/env python3
"""Build index/safedocs-parse.tsv.gz: the SafeDocs PDFs worth a reader's attention.

Usage: tools/safedocs-parse-index.py META_DIR

META_DIR needs the tables tools/safedocs-index.py reads (pdfinfo, tika and
cc-provenance; about 2 GB) and pdfa-eng-wds's key list, saved as
pdfa-eng-wds-78af41b7-minimal.json:
  https://huggingface.co/datasets/pixparse/pdfa-eng-wds/resolve/
  78af41b722c098baef2a87fab70a15dcf8d77ab7/_pdfa-english-train-info-minimal.json
(sha256 6dd8490feb7e9eb37dd42913afb67948f74bcb07bf98f41f922c34dab1bf204e).

A file is listed when pdfinfo's or Tika's reading of it puts it in a bucket:

  damage    pdfinfo-error (exits non-zero, a wrong password aside); xref,
            lexer, stream, structure, not-pdf (pdfinfo's stderr says so;
            exit 99 is structure); tika-error (parse_status not OK);
            damaged-font; page-disagree (pdfinfo and Tika count pages
            differently); timeout
  features  password (pdfinfo needs one); encrypted; updates (10 or more
            incremental updates, by Tika's count); hint-tables (pdfinfo
            complains of linearization hints); linearized; many-pages (over
            1,000); pdf-2.0; odd-version (a header version other than 1.0-1.7
            and 2.0); html (Tika reads text or HTML); illustrator; known-good
            (pdfa-eng-wds took it: it opened, rendered under 500 ms a page
            and had English text; file numbers below 31,000 only, about one
            in 256 of them)

Linearization alone, which 28% of files have, lists only the pooled ones.
The metadata is per URL; a file's flags are those of all its URLs, its other
columns its first URL's. The pool column is what tools/safedocs-fetch.py
fetches: files of 5 MB or less in a damage bucket, or among the first 5,000
of a feature bucket by file number (sha256 order), or known-good.
"""
import array
import csv
import gzip
import hashlib
import json
import os
import re
import sys

csv.field_size_limit(1 << 30)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N = 7_940_000
PDFA_SHA256 = "6dd8490feb7e9eb37dd42913afb67948f74bcb07bf98f41f922c34dab1bf204e"
MAX_POOL, FEATURE_CAP, KNOWN_GOOD_BELOW = 5_000_000, 5_000, 31_000
DAMAGE = ["pdfinfo-error", "xref", "lexer", "stream", "structure", "not-pdf", "tika-error",
          "damaged-font", "page-disagree", "timeout"]
FEATURES = ["password", "encrypted", "updates", "hint-tables", "linearized", "many-pages",
            "pdf-2.0", "odd-version", "html", "illustrator", "known-good"]
BIT = {b: 1 << i for i, b in enumerate(DAMAGE + FEATURES)}
DAMAGED = sum(BIT[b] for b in DAMAGE)
COLS = ["file_name", "url_id", "length", "digest", "url", "version", "producer", "pages",
        "tika_pages", "pdfinfo_exit", "pdfinfo_error", "tika_status", "tika_error", "mime",
        "lang", "buckets", "pool"]
VERSIONS = {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "2.0"}
STDERR = [
    ("xref", re.compile(r"xref|XRef|trailer|reconstruct", re.I)),
    ("lexer", re.compile(r"Illegal character|Unterminated string|End of file inside|"
                         r"Command token too long|Dictionary key must be a name")),
    ("stream", re.compile(r"flate stream|stream length|endstream|Unknown filter|"
                          r"Unexpected end of file", re.I)),
    ("structure", re.compile(r"Catalog object is wrong type|Couldn't read page catalog|"
                             r"Top-level pages object|Kid object", re.I)),
    ("not-pdf", re.compile(r"May not be a PDF")),
    ("hint-tables", re.compile(r"hint", re.I)),
]


def rows(meta, name):
    with gzip.open(os.path.join(meta, name), "rt", encoding="utf-8-sig", errors="replace",
                   newline="") as fh:
        yield from csv.DictReader(fh)


def number(name):
    stem = (name or "").split(".", 1)[0]
    return int(stem) if stem.isdigit() and int(stem) < N else None


def num(s):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return -1


def clean(s, n=150):
    return " ".join((s or "").split())[:n]


def pdfinfo_flags(r):
    err, f = r["stderr"] or "", 0
    password = "Incorrect password" in err
    if password:
        f |= BIT["password"]
    if r["exit_value"] not in ("0", "") and not password:
        f |= BIT["pdfinfo-error"]
    if r["exit_value"] == "99":
        f |= BIT["structure"]
    if r["timeout"] in ("t", "true"):
        f |= BIT["timeout"]
    for bucket, pattern in STDERR:
        if pattern.search(err):
            f |= BIT[bucket]
    v = (r["pdf_version"] or "").strip()
    if v == "2.0":
        f |= BIT["pdf-2.0"]
    elif v and v not in VERSIONS:
        f |= BIT["odd-version"]
    if num(r["pages"]) > 1000:
        f |= BIT["many-pages"]
    if (r["optimized"] or "").lower() == "yes":
        f |= BIT["linearized"]
    return f


def tika_flags(r):
    f = 0
    if r["parse_status"] and r["parse_status"] != "OK":
        f |= BIT["tika-error"]
        if r["parse_status"] in ("TIMEOUT", "OOM", "UNSPECIFIED_CRASH"):
            f |= BIT["timeout"]
    if r["encrypted"] == "t":
        f |= BIT["encrypted"]
    if num(r["pdf_incremental_updates"]) >= 10:
        f |= BIT["updates"]
    if r["pdf_contains_damaged_font"] == "t":
        f |= BIT["damaged-font"]
    mime = r["mime"] or ""
    if mime.startswith(("text/", "application/xhtml")):
        f |= BIT["html"]
    elif mime.startswith("application/illustrator"):
        f |= BIT["illustrator"]
    return f


def first_error(stderr):
    for line in (stderr or "").splitlines():
        if "Error" in line or "error" in line:
            return clean(line)
    return clean(stderr)


def main():
    meta = sys.argv[1]
    flags = array.array("I", [0]) * N
    length = array.array("q", [0]) * N
    pages = array.array("l", [-1]) * N
    with open(os.path.join(meta, "pdfa-eng-wds-78af41b7-minimal.json"), "rb") as fh:
        data = fh.read()
    if hashlib.sha256(data).hexdigest() != PDFA_SHA256:
        raise SystemExit("pdfa-eng-wds-78af41b7-minimal.json isn't the pinned revision's")
    good = {int(k) for shard in json.loads(data).values() for k in shard["samples"]}
    print(f"pdfa-eng-wds keys: {len(good)}", flush=True)
    for n in good:
        if n < KNOWN_GOOD_BELOW:
            flags[n] |= BIT["known-good"]

    for r in rows(meta, "cc-provenance-20230303.csv.gz"):
        n = number(r["file_name"])
        if n is not None:
            length[n] = max(length[n], num(r["fetched_length"]))
    for r in rows(meta, "pdfinfo-20230315.csv.gz"):
        n = number(r["file_name"])
        if n is not None:
            flags[n] |= pdfinfo_flags(r)
            pages[n] = max(pages[n], num(r["pages"]))
    for r in rows(meta, "tika-20230714.csv.gz"):
        n = number(r["file_name"])
        if n is not None:
            f = tika_flags(r)
            tp = num(r["num_pages"])
            if tp >= 0 and pages[n] >= 0 and tp != pages[n]:
                f |= BIT["page-disagree"]
            flags[n] |= f
    print("flags read", flush=True)

    pool, taken = bytearray(N), dict.fromkeys(FEATURES, 0)
    for n in range(N):
        f = flags[n]
        if not f or not 0 < length[n] <= MAX_POOL:
            continue
        if f & (DAMAGED | BIT["known-good"]):
            pool[n] = 1
        for b in FEATURES:
            if f & BIT[b] and taken[b] < FEATURE_CAP:
                taken[b] += 1
                pool[n] = 1

    listed = {n for n in range(N) if flags[n] & ~BIT["linearized"] or pool[n]}
    detail = {}
    for r in rows(meta, "cc-provenance-20230303.csv.gz"):
        n = number(r["file_name"])
        if n in listed and n not in detail:
            detail[n] = {"file_name": f"{n:07d}.pdf", "url_id": r["url_id"],
                         "length": length[n], "digest": r["fetched_digest"],
                         "url": clean(r["url"], 2000)}
    for r in rows(meta, "pdfinfo-20230315.csv.gz"):
        d = detail.get(number(r["file_name"]))
        if d is not None and "pdfinfo_exit" not in d:
            d.update(version=(r["pdf_version"] or "").strip(), pages=r["pages"],
                     pdfinfo_exit=r["exit_value"], pdfinfo_error=first_error(r["stderr"]))
    for r in rows(meta, "tika-20230714.csv.gz"):
        d = detail.get(number(r["file_name"]))
        if d is not None and "tika_status" not in d:
            exc = (r["container_exception"] or "").strip().splitlines()
            d.update(producer=clean(r["pdf_producer"], 100), tika_pages=r["num_pages"],
                     tika_status=r["parse_status"], tika_error=clean(exc[0] if exc else ""),
                     mime=clean(r["mime"], 60), lang=r["tika_eval_lang"])

    out = os.path.join(ROOT, "index", "safedocs-parse.tsv.gz")
    counts = {b: [0, 0, 0] for b in DAMAGE + FEATURES}
    with gzip.open(out, "wt", newline="", compresslevel=9) as fh:
        w = csv.DictWriter(fh, COLS, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        for n in sorted(detail):
            d = detail[n]
            names = [b for b in DAMAGE + FEATURES if flags[n] & BIT[b]]
            for b in names:
                counts[b][0] += 1
                counts[b][1] += pool[n]
                counts[b][2] += length[n] if pool[n] else 0
            d.update(buckets=",".join(names), pool=pool[n])
            w.writerow(d)
    print(f"wrote {out}: {len(detail)} files, {sum(pool)} to pool, "
          f"{sum(length[n] for n in detail if pool[n]) / 1e9:.1f} GB")
    for b, (c, p, size) in counts.items():
        print(f"  {b:14} {c:8,} listed  {p:7,} pooled  {size / 1e9:6.1f} GB")


main()
