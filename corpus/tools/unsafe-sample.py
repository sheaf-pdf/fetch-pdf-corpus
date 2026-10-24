#!/usr/bin/env python3
"""Pick a varied sample of the UNSAFE pool into parsing/unsafe/.

Usage: tools/unsafe-sample.py INVENTORY.tsv.gz [--budget BYTES] [--dry-run]

INVENTORY is tools/inventory.py's output for pool/unsafe/. A file
can be picked only if it starts with %PDF- within its first 1 KB and ClamAV
didn't flag it (index/unsafe-clamav.txt, `clamscan -r -i --no-summary
pool/unsafe` over the pool); the picks are scanned again before they're
copied, and nothing is copied if ClamAV flags one.

The sample fills buckets in order, each up to a cap: all of TA3's feature
samples (26-samples/), MPG's 6 seed files, and 4 files from each of MPG's
folders, which name the malformation or modification made (1 where all of a
folder's files are over 500 KB); then, from the
bug trackers, where qpdf and PDFium disagree, crash, fail or repair, 25 from
each tracker, and each cross-reference kind, bytes before the header or
after %%EOF, 10+ updates, encryption method, stream filter, font type and
header version. Within a bucket files go in sha256 order, so reruns pick
the same files; a file counts once, for its first bucket. Files over 500 KB
are skipped (2 MB in the rare buckets, 10 MB for TA3's feature samples and
seeds), and selection stops at the byte budget (default 400 MB, which keeps
parsing/ under its 1 GB).

Copies the picks to parsing/unsafe/ under their paths in the zips (less
corpora-pdf/), removing files no longer picked, and rewrites the
parsing/unsafe rows of parsing/manifest.tsv.
"""
import argparse
import collections
import csv
import gzip
import hashlib
import os
import re
import shutil
import subprocess

csv.field_size_limit(1 << 30)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "parsing", "manifest.tsv")
CLAMAV = os.path.join(ROOT, "index", "unsafe-clamav.txt")
BUCKET = "https://digitalcorpora.s3.amazonaws.com/corpora/files/CC-MAIN-2021-31-UNSAFE/corpora-pdf/"
CATEGORY_CAP = 1_000_000_000
MAX_FILE, MAX_RARE, MAX_TA3 = 500_000, 2_000_000, 10_000_000
INVENTORY = ["size", "version", "header_offset", "after_eof", "eofs", "xref", "linearized",
             "objstms", "filters", "encryption", "qpdf", "qpdf_pages", "objects", "fonts",
             "unembedded", "qpdf_message", "pdfium", "pdfium_pages"]
OPENS = ("ok", "warned", "repaired")
FAILS = ("format", "file", "unknown", "security")
FILTERS = {"ASCIIHexDecode", "ASCII85Decode", "LZWDecode", "FlateDecode", "RunLengthDecode",
           "CCITTFaxDecode", "JBIG2Decode", "DCTDecode", "JPXDecode", "Crypt", "AHx", "A85",
           "LZW", "Fl", "RL", "CCF", "DCT"}
FONTS = {"Type0", "Type1", "MMType1", "Type3", "TrueType", "CIDFontType0", "CIDFontType2"}
VERSIONS = {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "2.0", ""}


def iv(r, k):
    try:
        return int(r.get(k) or 0)
    except ValueError:
        return 0


def buckets(rows):
    """Yield (name, cap, size limit, predicate) in priority order."""
    ta3 = lambda r: r["set"] == "TA3"
    bug = lambda r: r["set"] == "issue-trackers"
    yield "ta3:26-samples", 40, MAX_TA3, lambda r: ta3(r) and r["label"].startswith("26-samples")
    yield "ta3:MPG/initial_files", 6, MAX_TA3, lambda r: r["label"] == "MPG/initial_files"
    smallest = {}
    for r in rows:
        if ta3(r) and r["label"].startswith("MPG/") and r["label"] != "MPG/initial_files":
            smallest[r["label"]] = min(smallest.get(r["label"], 1 << 62), iv(r, "size"))
    for lab, least in sorted(smallest.items()):
        # A folder whose files were all made from the larger seeds gives one.
        cap, limit = (4, MAX_FILE) if least <= MAX_FILE else (1, MAX_TA3)
        yield f"ta3:{lab}", cap, limit, lambda r, lab=lab: r["label"] == lab
    yield "qpdf-fails-pdfium-opens", 60, MAX_RARE, lambda r: bug(r) and r["qpdf"] == "failed" \
        and r["pdfium"] == "ok"
    yield "pdfium-fails-qpdf-opens", 60, MAX_RARE, lambda r: bug(r) and r["pdfium"] in FAILS \
        and r["qpdf"] in OPENS
    yield "pages-differ", 40, MAX_RARE, lambda r: bug(r) and r["qpdf_pages"] and \
        r["pdfium_pages"] and r["qpdf_pages"] != r["pdfium_pages"]
    yield "crashed-or-hung", 50, MAX_RARE, lambda r: bug(r) and bool(
        {"crashed", "timeout"} & {r["qpdf"], r["pdfium"]})
    yield "both-fail", 40, MAX_FILE, lambda r: bug(r) and r["qpdf"] == "failed" and \
        r["pdfium"] in FAILS
    yield "qpdf-repaired", 60, MAX_FILE, lambda r: bug(r) and r["qpdf"] == "repaired"
    yield "qpdf-warned", 40, MAX_FILE, lambda r: bug(r) and r["qpdf"] == "warned"
    for t in sorted({r["label"] for r in rows if bug(r)}):
        yield f"tracker:{t}", 25, MAX_FILE, lambda r, t=t: bug(r) and r["label"] == t
    for x in ("broken", "none", "hybrid", "stream", "table"):
        yield f"xref:{x}", 10, MAX_FILE, lambda r, x=x: bug(r) and r["xref"] == x
    yield "header-offset", 15, MAX_FILE, lambda r: bug(r) and iv(r, "header_offset") > 0
    yield "after-eof", 15, MAX_FILE, lambda r: bug(r) and iv(r, "after_eof") > 0
    yield "updates", 15, MAX_FILE, lambda r: bug(r) and iv(r, "eofs") >= 10
    for e, _ in collections.Counter(r["encryption"] for r in rows
                                    if bug(r) and r["encryption"]).most_common(20):
        yield f"encryption:{e}", 10, MAX_FILE, lambda r, e=e: bug(r) and r["encryption"] == e
    for f in sorted(FILTERS):
        yield f"filter:{f}", 8, MAX_FILE, lambda r, f=f: bug(r) and f in r["filter_set"]
    yield "filter:unknown", 15, MAX_RARE, lambda r: bug(r) and bool(r["filter_set"] - FILTERS)
    for f in sorted(FONTS):
        yield f"font:{f}", 8, MAX_FILE, lambda r, f=f: bug(r) and f in r["font_set"]
    yield "font:other", 5, MAX_RARE, lambda r: bug(r) and bool(r["font_set"] - FONTS)
    for v in sorted(VERSIONS - {""}):
        yield f"version:{v}", 5, MAX_FILE, lambda r, v=v: bug(r) and r["version"] == v
    yield "version:other", 10, MAX_RARE, lambda r: bug(r) and r["version"] not in VERSIONS


def flagged():
    """Paths, relative to this repo, ClamAV flagged in the pool."""
    out = set()
    with open(CLAMAV) as fh:
        for line in fh:
            if line.rstrip().endswith(" FOUND"):
                path = line.rsplit(": ", 1)[0]
                out.add(os.path.relpath(os.path.abspath(path), ROOT))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("inventory")
    ap.add_argument("--budget", type=int, default=400_000_000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = {}
    with gzip.open(os.path.join(ROOT, "index", "unsafe.tsv.gz"), "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            if r["pool"] == "1":
                index["pool/unsafe/" + r["path"].removeprefix("corpora-pdf/")] = r
    bad = flagged()
    rows, dropped = [], collections.Counter()
    with gzip.open(args.inventory, "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            meta = index.get(r["file"])
            if meta is None:
                continue
            if r["file"] in bad:
                dropped["clamav"] += 1
                continue
            if iv(r, "header_offset") < 0:
                dropped["no %PDF-"] += 1
                continue
            r.update({k: meta[k] for k in ("path", "zip", "zip_version", "set", "label",
                                           "sha256")})
            r["filter_set"] = set(f for f in r["filters"].split(",") if f)
            r["font_set"] = {f.split(":")[0] for f in r["fonts"].split(",") if f}
            rows.append(r)
    print(f"{len(rows)} files can be picked; left out: {dict(dropped)}")
    emptied = sorted({m["label"] for m in index.values() if m["set"] == "TA3"}
                     - {r["label"] for r in rows if r["set"] == "TA3"})
    if emptied:
        print(f"TA3 folders ClamAV flags every file of: {', '.join(emptied)}")
    for r in rows:
        if not r["sha256"]:
            r["sha256"] = sha256(os.path.join(ROOT, r["file"]))
    rows.sort(key=lambda r: r["sha256"])

    picked, used, report = {}, 0, []
    for name, cap, limit, pred in buckets(rows):
        n, start = 0, used
        for r in rows:
            if n >= cap or used >= args.budget:
                break
            if r["file"] in picked or iv(r, "size") > limit or not pred(r):
                continue
            picked[r["file"]] = (r, name)
            used += iv(r, "size")
            n += 1
        report.append((name, n, used - start))
    width = max(len(n) for n, _, _ in report)
    for name, n, size in report:
        print(f"  {name:{width}} {n:4} {size / 1e6:6.1f} MB")
    others = sum(os.path.getsize(os.path.join(d, f))
                 for d, _, fs in os.walk(os.path.join(ROOT, "parsing"))
                 if not d.startswith(os.path.join(ROOT, "parsing", "unsafe")) for f in fs)
    print(f"picked {len(picked)} files, {used / 1e9:.2f} GB; parsing/ comes to "
          f"{(used + others) / 1e9:.2f} GB of its {CATEGORY_CAP / 1e9:.0f} GB")
    if args.dry_run:
        return
    if used + others > CATEGORY_CAP:
        raise SystemExit("over the category's cap; lower --budget")
    rescan(sorted(picked))

    dest = os.path.join(ROOT, "parsing", "unsafe")
    wanted = {os.path.join(dest, r["path"].removeprefix("corpora-pdf/")): f
              for f, (r, _) in picked.items()}
    if os.path.isdir(dest):
        for d, _, fs in os.walk(dest):
            for f in fs:
                if os.path.join(d, f) not in wanted:
                    os.remove(os.path.join(d, f))
    for target, f in wanted.items():
        if not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(ROOT, f), target)
    for d, dirs, fs in os.walk(dest, topdown=False):
        if not dirs and not fs:
            os.rmdir(d)
    write_manifest(picked)


def rescan(files):
    """ClamAV over the picks; stop if it flags any."""
    r = subprocess.run(["clamscan", "--infected", "--no-summary", "-f", "/dev/stdin"],
                       input="\n".join(os.path.join(ROOT, f) for f in files), text=True,
                       capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"clamscan flagged picks (exit {r.returncode}):\n{r.stdout}{r.stderr}")
    print(f"clamscan passed all {len(files)} picks")


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def expect_note(r):
    lab = r["label"]
    if r["set"] == "issue-trackers":
        return "", f"Attached to a {lab} bug report"
    if lab == "MPG/initial_files":
        return "", "Kudu Dynamics (DARPA SafeDocs TA3): a seed the MPG files were made from"
    if lab.startswith("MPG/malform_"):
        return "invalid", f"Kudu Dynamics (DARPA SafeDocs TA3) malformation: {lab[4:]}"
    if lab.startswith("MPG/"):
        return "", f"Kudu Dynamics (DARPA SafeDocs TA3) modification: {lab[4:]}"
    feature = re.sub(r"_\d+\.pdf$", "", os.path.basename(r["path"])).replace("_", " ")
    return "", f"Kudu Dynamics (DARPA SafeDocs TA3) sample: {feature}"


def write_manifest(picked):
    with open(MANIFEST, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        cols = reader.fieldnames
        keep = [r for r in reader if not r["path"].startswith("parsing/unsafe/")]
    cols += [c for c in ["bucket"] + INVENTORY if c not in cols]
    for f, (r, bucket) in picked.items():
        expect, note = expect_note(r)
        row = {c: r.get(c, "") for c in INVENTORY}
        row.update({
            "path": "parsing/unsafe/" + r["path"].removeprefix("corpora-pdf/"),
            "source": f"{BUCKET}{r['zip']}?versionId={r['zip_version']}#{r['path']}",
            "license": "unknown", "sha256": r["sha256"],
            "pages": r["pdfium_pages"] or r["qpdf_pages"], "expect": expect, "password": "",
            "note": note, "bucket": bucket,
        })
        keep.append(row)
    keep.sort(key=lambda r: r["path"])
    with open(MANIFEST, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(keep)


main()
