#!/usr/bin/env python3
"""Build the SafeDocs indexes for the conformance, scans and signatures categories.

Usage: tools/safedocs-feature-index.py META_DIR

META_DIR holds the pdfinfo, tika and cc-provenance tables, as for
tools/safedocs-parse-index.py. One pass over them writes, in index/:

  safedocs-conformance.tsv.gz  files whose XMP claims PDF/A, PDF/UA or PDF/X
                               (Tika's pdfa_version, pdfuaid_part,
                               pdfx_version), a bucket per claim; and tagged
                               files (pdfinfo), listed only where pooled
  safedocs-scans.tsv.gz        files a scanner or OCR program made (by
                               producer or creator), split by words per page
                               (Tika: under 10, or 50 and over); and files of
                               under 10 words a page from anything else,
                               listed only where pooled
  safedocs-signatures.tsv.gz   files Tika finds a signature in, and those of
                               them with 2 or more incremental updates

Each row has the file's URL, size, sha256, version, producer, creator tool,
page count, words per page, Tika's claims and flags, its buckets, and a pool
column for tools/safedocs-fetch.py: per bucket, the first files by file
number (sha256 order), up to its cap, of 5 MB or less (10 MB for scans).
"""
import array
import collections
import csv
import gzip
import os
import re
import sys

csv.field_size_limit(1 << 30)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N = 7_940_000
SCANNER = re.compile(
    r"\b(canon|ricoh|konica|minolta|bizhub|kyocera|taskalfa|xerox|sharp mx|toshiba|e-studio|"
    r"brother|epson|lexmark|hp digital sending|hp scan|imagerunner|scansnap|paperstream|"
    r"fujitsu|kodak|kofax|capture perfect|naps2|paper capture|scanner|scan to pdf)", re.I)
OCR = re.compile(r"(abbyy|finereader|omnipage|readiris|tesseract|ocrmypdf|clearscan|"
                 r"paper capture|nuance)", re.I)
CATEGORIES = {
    # bucket: (cap, listed when not pooled)
    "conformance": {"claim": (300, True), "tagged": (1000, False)},
    "scans": {"scanner-no-text": (2000, True), "scanner-text": (2000, True),
              "ocr-program": (2000, True), "no-text": (2000, False)},
    "signatures": {"signed": (4000, True), "signed-updated": (4000, True)},
}
MAX_POOL = {"conformance": 5_000_000, "scans": 10_000_000, "signatures": 5_000_000}
COLS = ["file_name", "url_id", "length", "digest", "url", "version", "producer",
        "creator_tool", "pages", "words_per_page", "lang", "pdfa", "pdfua", "pdfx", "tagged",
        "signature", "incremental_updates", "buckets", "pool"]


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


def main():
    meta = sys.argv[1]
    length = array.array("q", [0]) * N
    tagged = bytearray(N)
    makers = {}
    for r in rows(meta, "cc-provenance-20230303.csv.gz"):
        n = number(r["file_name"])
        if n is not None:
            length[n] = max(length[n], num(r["fetched_length"]))
    for r in rows(meta, "pdfinfo-20230315.csv.gz"):
        n = number(r["file_name"])
        if n is None:
            continue
        if r["tagged"] == "yes":
            tagged[n] = 1
        who = f"{r['producer']} {r['creator']}"
        if SCANNER.search(who) or OCR.search(who):
            makers[n] = who
    print("pdfinfo read", flush=True)

    # Per file, its buckets in each category, from Tika's row (its first URL's).
    buckets = collections.defaultdict(dict)
    tika = {}
    for r in rows(meta, "tika-20230714.csv.gz"):
        n = number(r["file_name"])
        if n is None or n in tika:
            continue
        who = f"{makers.get(n, '')} {r['pdf_producer']} {r['xmp_creator_tool']}"
        pages, words = num(r["num_pages"]), num(r["tika_eval_num_tokens"])
        wpp = words / pages if pages > 0 and words >= 0 and r["parse_status"] == "OK" else -1
        conf = []
        for col, kind in (("pdfa_version", "pdfa"), ("pdfuaid_part", "pdfua"),
                          ("pdfx_version", "pdfx"), ("pdfx_conformance", "pdfx")):
            if r[col]:
                conf.append(f"claim:{kind}:{clean(r[col], 20)}")
        if tagged[n]:
            conf.append("tagged")
        scan = []
        if SCANNER.search(who):
            if 0 <= wpp < 10:
                scan.append("scanner-no-text")
            elif wpp >= 50:
                scan.append("scanner-text")
        if OCR.search(who):
            scan.append("ocr-program")
        if not scan and 0 <= wpp < 10:
            scan.append("no-text")
        sig = []
        if r["has_signature"] == "t":
            sig.append("signed")
            if num(r["pdf_incremental_updates"]) >= 2:
                sig.append("signed-updated")
        for cat, b in (("conformance", conf), ("scans", scan), ("signatures", sig)):
            if b:
                buckets[cat][n] = b
        if conf or scan or sig:
            tika[n] = {"version": r["pdf_version"], "producer": clean(r["pdf_producer"], 100),
                       "creator_tool": clean(r["xmp_creator_tool"], 100), "pages": r["num_pages"],
                       "words_per_page": f"{wpp:.1f}" if wpp >= 0 else "", "lang": r["tika_eval_lang"],
                       "pdfa": r["pdfa_version"], "pdfua": r["pdfuaid_part"],
                       "pdfx": r["pdfx_version"] or r["pdfx_conformance"], "tagged": tagged[n],
                       "signature": int(r["has_signature"] == "t"),
                       "incremental_updates": r["pdf_incremental_updates"]}
    print("tika read", flush=True)

    chosen = {}
    for cat, per_file in buckets.items():
        caps = CATEGORIES[cat]
        taken, pool = collections.Counter(), set()
        for n in sorted(per_file):
            if not 0 < length[n] <= MAX_POOL[cat]:
                continue
            for b in per_file[n]:
                cap = caps["claim" if b.startswith("claim:") else b][0]
                if taken[b] < cap:
                    taken[b] += 1
                    pool.add(n)
        listed = {n for n, bs in per_file.items()
                  if n in pool or any(caps["claim" if b.startswith("claim:") else b][1] for b in bs)}
        chosen[cat] = (listed, pool)

    want = set().union(*(listed for listed, _ in chosen.values()))
    prov = {}
    for r in rows(meta, "cc-provenance-20230303.csv.gz"):
        n = number(r["file_name"])
        if n in want and n not in prov:
            prov[n] = {"file_name": f"{n:07d}.pdf", "url_id": r["url_id"], "length": length[n],
                       "digest": r["fetched_digest"], "url": clean(r["url"], 2000)}
    for cat, (listed, pool) in chosen.items():
        out = os.path.join(ROOT, "index", f"safedocs-{cat}.tsv.gz")
        counts = collections.Counter()
        with gzip.open(out, "wt", newline="", compresslevel=9) as fh:
            w = csv.DictWriter(fh, COLS, delimiter="\t", lineterminator="\n", restval="")
            w.writeheader()
            for n in sorted(listed):
                if n not in prov:
                    continue
                bs = buckets[cat][n]
                for b in bs:
                    counts[b, n in pool] += 1
                w.writerow({**prov[n], **tika[n], "buckets": ",".join(bs), "pool": int(n in pool)})
        size = sum(length[n] for n in pool)
        print(f"wrote {out}: {len(listed)} files, {len(pool)} to pool, {size / 1e9:.1f} GB")
        for b in sorted({b for b, _ in counts}):
            print(f"  {b:28} {counts[b, True] + counts[b, False]:8,} listed  "
                  f"{counts[b, True]:6,} pooled")


main()
