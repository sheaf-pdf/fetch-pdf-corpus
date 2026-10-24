#!/usr/bin/env python3
"""Index and fill pool/unsafe/ from Digital Corpora's CC-MAIN-2021-31-UNSAFE.

Usage: tools/unsafe-pool.py index [--workers N]
       tools/unsafe-pool.py fetch [--workers N]

UNSAFE-DOCS packs its files into zips on S3, versioned. `index` reads the
central directory of every corpora-pdf zip's current version, by HTTP range
requests, and writes index/unsafe.tsv.gz: a row per file under TA3/ (Kudu
Dynamics' test files for DARPA SafeDocs: MPG/ in folders that name the
malformation or modification made, 26-samples/ a file per feature) or
issue-trackers/ (files attached to 13 projects' bug trackers), with its zip,
the zip's S3 version, where the file is in it, its size and CRC, and from
issue-trackers/catalog.csv its libmagic type and sha256. The pool column
marks what `fetch` takes: TA3's MPG/ and 26-samples/, and the
issue-tracker files libmagic calls PDF.

`fetch` puts each at pool/unsafe/<its path in the zip, less corpora-pdf/>,
checking its CRC and, where the catalog has one, its sha256, and skips
files already there at the right size, so rerunning resumes.

The corpus may hold malicious files: nothing here opens them.
"""
import argparse
import collections
import concurrent.futures
import csv
import gzip
import hashlib
import io
import os
import re
import struct
import threading
import time
import urllib.request
from urllib.parse import quote
import xml.etree.ElementTree as ET
import zipfile
import zlib

csv.field_size_limit(1 << 30)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUCKET = "https://digitalcorpora.s3.amazonaws.com"
PREFIX = "corpora/files/CC-MAIN-2021-31-UNSAFE/corpora-pdf/"
INDEX = os.path.join(ROOT, "index", "unsafe.tsv.gz")
POOL = os.path.join(ROOT, "pool", "unsafe")
KEEP = ("corpora-pdf/TA3/", "corpora-pdf/issue-trackers/")
COLS = ["path", "zip", "zip_version", "offset", "csize", "size", "method", "crc", "set",
        "label", "mime", "sha256", "pool"]


def get(url, start=None, end=None, tries=5):
    for attempt in range(tries):
        req = urllib.request.Request(url)
        if start is not None:
            req.add_header("Range", f"bytes={start}-{end}")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read(), r.headers
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


class RangeFile(io.RawIOBase):
    """Just enough of a seekable file for zipfile to read the central directory."""

    def __init__(self, url):
        _, h = get(url, 0, 0)
        self.url, self.pos = url, 0
        self.size = int(h["Content-Range"].rsplit("/", 1)[1])
        self.buf_start, self.buf = 0, b""

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        lo, hi = self.buf_start, self.buf_start + len(self.buf)
        if not (lo <= self.pos and self.pos + n <= hi):
            self.buf_start = self.pos
            self.buf, _ = get(self.url, self.pos, min(self.size, self.pos + max(n, 1 << 20)) - 1)
        off = self.pos - self.buf_start
        self.pos += n
        return self.buf[off:off + n]


def zips():
    """(key, version) of each corpora-pdf zip's current version."""
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    out, marker = [], ""
    while True:
        xml, _ = get(f"{BUCKET}/?versions&prefix={PREFIX}&max-keys=1000" + marker)
        root = ET.fromstring(xml)
        for v in root.findall("s3:Version", ns):
            key = v.findtext("s3:Key", namespaces=ns)
            if v.findtext("s3:IsLatest", namespaces=ns) == "true" and re.search(
                    r"/corpora-pdf-x\d+\.zip$", key):
                out.append((key, v.findtext("s3:VersionId", namespaces=ns)))
        if root.findtext("s3:IsTruncated", namespaces=ns) != "true":
            return sorted(out)
        marker = ("&key-marker=" + quote(root.findtext("s3:NextKeyMarker", namespaces=ns))
                  + "&version-id-marker="
                  + quote(root.findtext("s3:NextVersionIdMarker", namespaces=ns)))


def url_of(key, version):
    return f"{BUCKET}/{key}?versionId={version}"


def member(key, version, info):
    """A member's bytes, read by range and checked against its CRC."""
    url = url_of(key, version)
    head, _ = get(url, info["offset"], info["offset"] + 29)
    nlen, xlen = struct.unpack("<HH", head[26:30])
    start = info["offset"] + 30 + nlen + xlen
    raw, _ = get(url, start, start + info["csize"] - 1) if info["csize"] else (b"", None)
    if info["method"] == zipfile.ZIP_DEFLATED:
        raw = zlib.decompress(raw, -15)
    elif info["method"] != zipfile.ZIP_STORED:
        raise ValueError(f"compression {info['method']}")
    if zlib.crc32(raw) != info["crc"]:
        raise ValueError("CRC mismatch")
    return raw


def label(path):
    parts = path.split("/")
    if parts[1] == "TA3":
        return parts[1], "/".join(parts[2:-1])
    return parts[1], parts[2] if len(parts) > 3 else ""


def index(workers):
    found = zips()
    print(f"{len(found)} zips", flush=True)
    rows, lock = [], threading.Lock()

    def listing(kv):
        key, version = kv
        for i in zipfile.ZipFile(RangeFile(url_of(key, version))).infolist():
            if i.filename.startswith(KEEP) and not i.is_dir():
                s, lab = label(i.filename)
                row = {"path": i.filename, "zip": key.rsplit("/", 1)[1], "zip_version": version,
                       "offset": i.header_offset, "csize": i.compress_size, "size": i.file_size,
                       "method": i.compress_type, "crc": i.CRC, "set": s, "label": lab}
                with lock:
                    rows.append(row)

    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        list(ex.map(listing, found))
    by_path = {r["path"]: r for r in rows}
    catalog = by_path.get("corpora-pdf/issue-trackers/catalog.csv")
    if catalog:
        raw = member(f"{PREFIX}{catalog['zip']}", catalog["zip_version"], catalog)
        for c in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")),
                                skipinitialspace=True):
            # The catalog's folders have _ where the zips' have - (mozilla_general).
            folder, _, name = c["relative_path"].partition("/")
            r = by_path.get(f"corpora-pdf/issue-trackers/{folder}/{name}") or by_path.get(
                f"corpora-pdf/issue-trackers/{folder.replace('_', '-')}/{name}")
            if r:
                r["mime"], r["sha256"] = c["mimetype"], c["sha256"]
    for r in rows:
        if r["set"] == "TA3":
            r["pool"] = int(r["label"].startswith(("MPG/", "26-samples")))
        else:
            r["pool"] = int(r.get("mime", "").startswith("application/pdf"))
    rows.sort(key=lambda r: r["path"])
    with gzip.open(INDEX, "wt", newline="", compresslevel=9) as fh:
        w = csv.DictWriter(fh, COLS, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(rows)
    pooled = [r for r in rows if r["pool"]]
    print(f"wrote {os.path.relpath(INDEX, ROOT)}: {len(rows)} files, {len(pooled)} to pool, "
          f"{sum(r['size'] for r in pooled) / 1e9:.1f} GB")
    for (s, lab), n in sorted(collections.Counter(
            (r["set"], r["label"].split("/")[0]) for r in rows).items()):
        print(f"  {s}/{lab}: {n}")


def fetch(workers):
    with gzip.open(INDEX, "rt", newline="") as fh:
        todo = [r for r in csv.DictReader(fh, delimiter="\t") if r["pool"] == "1"]
    stats, t0 = collections.Counter(), time.time()

    def one(r):
        dest = os.path.join(POOL, r["path"].removeprefix("corpora-pdf/"))
        if os.path.exists(dest) and os.path.getsize(dest) == int(r["size"]):
            stats["skipped"] += 1
            return
        info = {k: int(r[k]) for k in ("offset", "csize", "method", "crc")}
        try:
            raw = member(PREFIX + r["zip"], r["zip_version"], info)
            if r["sha256"] and hashlib.sha256(raw).hexdigest() != r["sha256"]:
                raise ValueError("sha256 mismatch")
        except Exception as e:
            stats["errors"] += 1
            print(f"{r['path']}: {e!r}", flush=True)
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".part", "wb") as fh:
            fh.write(raw)
        os.replace(dest + ".part", dest)
        stats["fetched"] += 1
        stats["bytes"] += len(raw)

    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for i, _ in enumerate(ex.map(one, todo), 1):
            if i % 2000 == 0:
                print(f"[{time.time() - t0:6.0f}s] {i}/{len(todo)} {dict(stats)}", flush=True)
    print(f"done in {time.time() - t0:.0f}s: {dict(stats)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["index", "fetch"])
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args()
    (index if args.command == "index" else fetch)(args.workers)


main()
