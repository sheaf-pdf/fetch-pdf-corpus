#!/usr/bin/env python3
"""Build index/safedocs-forms.tsv.gz: every SafeDocs PDF with form fields or XFA.

SafeDocs (CC-MAIN-2021-31-PDF-UNTRUNCATED) is ~7.9M PDFs from Common Crawl.
Its metadata tables say, per URL, whether Tika found AcroForm fields or XFA,
so the form PDFs can be picked out without downloading the corpus.

Usage: tools/safedocs-index.py META_DIR

META_DIR needs these files (about 2.3 GB):
  https://digitalcorpora.s3.amazonaws.com/corpora/files/CC-MAIN-2021-31-PDF-UNTRUNCATED/metadata/
    tika-20230714.csv.gz  cc-hosts-20230303.csv.gz
    pdfinfo-20230315.csv.gz  cc-provenance-20230303.csv.gz
  https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-federal.csv

The metadata is per URL; the same file can come from several URLs, so the
index keeps one row per file, preferring a federal URL. "federal" means a
host under a domain in CISA's federal .gov list, or any .mil or .fed.us host.
"""
import collections
import csv
import gzip
import os
import sys

csv.field_size_limit(1 << 30)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ["file_name", "url_id", "length", "pages", "acroform_fields", "xfa", "javascript",
        "signature", "encrypted", "pdfinfo_form", "federal", "host", "tld", "country", "lang",
        "pdf_version", "producer", "creator_tool", "creator", "digest", "url"]


def rows(meta, name):
    with gzip.open(os.path.join(meta, name), "rt", encoding="utf-8-sig", errors="replace",
                   newline="") as fh:
        yield from csv.DictReader(fh)


def clean(s):
    return (s or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def main():
    meta = sys.argv[1]
    fed = set()
    with open(os.path.join(meta, "current-federal.csv"), encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            fed.add(r["Domain name"].lower())

    def is_federal(host):
        parts = host.lower().rstrip(".").split(".")
        if parts[-1] == "mil" or parts[-2:] == ["fed", "us"]:
            return True
        return any(".".join(parts[i:]) in fed for i in range(len(parts) - 1))

    cand = {}
    for r in rows(meta, "tika-20230714.csv.gz"):
        if r["pdf_has_acroform_fields"] == "t" or r["has_xfa"] == "t":
            cand[r["url_id"]] = {
                "file_name": r["file_name"],
                "acroform_fields": r["pdf_has_acroform_fields"] == "t",
                "xfa": r["has_xfa"] == "t",
                "signature": r["has_signature"] == "t",
                "encrypted": r["encrypted"] == "t",
                "lang": r["tika_eval_lang"],
                "pages": r["num_pages"],
                "producer": clean(r["pdf_producer"]),
                "creator_tool": clean(r["xmp_creator_tool"]),
                "pdf_version": r["pdf_version"],
            }
    print(f"URLs with fields or XFA: {len(cand)}", flush=True)
    for r in rows(meta, "cc-hosts-20230303.csv.gz"):
        c = cand.get(r["url_id"])
        if c:
            c["host"], c["tld"], c["country"] = r["host"], r["tld"], r["country"]
    for r in rows(meta, "pdfinfo-20230315.csv.gz"):
        c = cand.get(r["url_id"])
        if c:
            c["pdfinfo_form"], c["javascript"] = r["form"], r["javascript"]
            c["creator"] = clean(r["creator"])
    for r in rows(meta, "cc-provenance-20230303.csv.gz"):
        c = cand.get(r["url_id"])
        if c:
            c["url"], c["length"], c["digest"] = clean(r["url"]), r["fetched_length"], r["fetched_digest"]

    by_file = {}
    for uid, c in sorted(cand.items(), key=lambda kv: int(kv[0])):
        c["url_id"] = uid
        c["federal"] = is_federal(c.get("host") or "x.invalid")
        prev = by_file.get(c["file_name"])
        if prev is None or (c["federal"] and not prev["federal"]):
            by_file[c["file_name"]] = c

    out = os.path.join(ROOT, "index", "safedocs-forms.tsv.gz")
    with gzip.open(out, "wt", newline="", compresslevel=9) as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(COLS)
        for name in sorted(by_file):
            c = by_file[name]
            w.writerow([int(v) if isinstance(v, bool) else v
                        for v in (c.get(k, "") for k in COLS)])
    total = sum(int(c.get("length") or 0) for c in by_file.values())
    print(f"wrote {out}: {len(by_file)} files, {total / 1e9:.1f} GB")
    print("languages:", collections.Counter(c.get("lang", "") for c in by_file.values()).most_common(10))


main()
