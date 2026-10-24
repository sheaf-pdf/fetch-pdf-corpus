#!/usr/bin/env python3
"""Fill pool/safedocs/ with the PDFs an index lists.

SafeDocs packs its PDFs into ~1.3 GB zips of 1,000 on S3. This reads each
zip's central directory and then pulls just the wanted members with HTTP
range requests, checking each file's sha256 against the index.

Usage: tools/safedocs-fetch.py [--index INDEX.tsv.gz] [--max-size BYTES] [--workers N]

The index is index/safedocs-forms.tsv.gz unless given. When it has a pool
column, only its rows with pool 1 are fetched. Files land at pool/safedocs/<zip number>/<file name>. Ones already there at
the right size are skipped, so rerunning resumes. Progress prints every 30 s.
"""
import argparse
import collections
import concurrent.futures
import csv
import gzip
import hashlib
import io
import os
import struct
import threading
import time
import urllib.request
import zipfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://digitalcorpora.s3.amazonaws.com/corpora/files/CC-MAIN-2021-31-PDF-UNTRUNCATED/zipfiles"


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
            self.buf, _ = get(self.url, self.pos, min(self.size, self.pos + max(n, 1 << 18)) - 1)
        off = self.pos - self.buf_start
        self.pos += n
        return self.buf[off:off + n]


def fetch_zip(zipno, items, out, stats):
    group = zipno // 1000 * 1000
    url = f"{BASE}/{group:04d}-{group + 999:04d}/{zipno:04d}.zip"
    dest = os.path.join(out, f"{zipno:04d}")
    os.makedirs(dest, exist_ok=True)
    todo = [it for it in items
            if not (os.path.exists(os.path.join(dest, it[0]))
                    and os.path.getsize(os.path.join(dest, it[0])) == it[1])]
    stats["skipped"] += len(items) - len(todo)
    if not todo:
        return
    try:
        infos = {i.filename.rsplit("/", 1)[-1]: i for i in zipfile.ZipFile(RangeFile(url)).infolist()}
    except Exception as e:
        stats["zip_errors"] += 1
        print(f"zip {zipno:04d}: {e!r}", flush=True)
        return
    for name, length, digest in todo:
        info = infos.get(name)
        if info is None:
            stats["missing"] += 1
            continue
        try:
            head, _ = get(url, info.header_offset, info.header_offset + 29)
            nlen, xlen = struct.unpack("<HH", head[26:30])
            start = info.header_offset + 30 + nlen + xlen
            raw, _ = get(url, start, start + info.compress_size - 1)
            if info.compress_type == zipfile.ZIP_DEFLATED:
                raw = zlib.decompress(raw, -15)
            elif info.compress_type != zipfile.ZIP_STORED:
                raise ValueError(f"compression {info.compress_type}")
            if digest and hashlib.sha256(raw).hexdigest() != digest:
                stats["bad_digest"] += 1
                print(f"{name}: sha256 mismatch", flush=True)
                continue
            tmp = os.path.join(dest, name + ".part")
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, os.path.join(dest, name))
            stats["fetched"] += 1
            stats["bytes"] += len(raw)
        except Exception as e:
            stats["errors"] += 1
            print(f"{name}: {e!r}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-size", type=int, default=5_000_000,
                    help="skip files larger than this many bytes (default 5000000)")
    ap.add_argument("--workers", type=int, default=64, help="zips fetched at once (default 64)")
    ap.add_argument("--index", default=os.path.join(ROOT, "index", "safedocs-forms.tsv.gz"))
    args = ap.parse_args()
    out = os.path.join(ROOT, "pool", "safedocs")
    by_zip = collections.defaultdict(list)
    with gzip.open(args.index, "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            if r.get("pool", "1") != "1":
                continue
            if r["length"] and int(r["length"]) <= args.max_size:
                by_zip[int(r["file_name"][:-4]) // 1000].append(
                    (r["file_name"], int(r["length"]), r["digest"]))
    total = sum(map(len, by_zip.values()))
    stats = collections.Counter()
    t0 = time.time()
    done = threading.Event()

    def report():
        while not done.wait(30):
            n = stats["fetched"] + stats["skipped"]
            rest = {k: v for k, v in stats.items() if k not in ("fetched", "skipped", "bytes")}
            print(f"[{time.time() - t0:6.0f}s] {n}/{total} files, "
                  f"{stats['bytes'] / 1e9:.1f} GB fetched {rest}", flush=True)

    reporter = threading.Thread(target=report)
    reporter.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(args.workers) as ex:
            list(ex.map(lambda kv: fetch_zip(kv[0], kv[1], out, stats), sorted(by_zip.items())))
    finally:
        done.set()
        reporter.join()
    print(f"done in {time.time() - t0:.0f}s: {dict(stats)}", flush=True)


main()
