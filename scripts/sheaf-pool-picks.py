#!/usr/bin/env python3
"""The pool files sheaf's corpus tests read: lists/sheaf-pool.txt.

Usage: scripts/sheaf-pool-picks.py CORPUS SHEAF/bench/pool-sample.txt > lists/sheaf-pool.txt

sheaf's realize_corpus test (crates/sheaf-ops/tests/realize_corpus.rs,
`files`) reads the pool sample; the inventory's first 75 encrypted rows;
and a top-up of 50 files with bytes before `%PDF-` and 50 whose
`startxref` leads nowhere, walking the inventory in order. The lexer test
(crates/sheaf-render/tests/content_lexer.rs) reads the sample only. This
runs the same picks over a full pool and prints the files they take. A
file that doesn't count toward a top-up can be missing without changing
what the walk picks, so a pool of just these gives sheaf the same files.

Needs only Python 3. Run it with -I: it reads the pool as bytes.
"""
import gzip
import os
import re
import sys

WS = b" \t\n\x0c\r"  # Rust's is_ascii_whitespace
OBJ = re.compile(rb"^[0-9]+[ \t\n\x0b\x0c\r]+[0-9]+[ \t\n\x0b\x0c\r]+obj")


def head(path):
    try:
        with open(path, "rb") as f:
            return f.read(1024)
    except OSError:
        return None


def leads_nowhere(path):
    """realize_corpus.rs's `leads_nowhere`."""
    try:
        with open(path, "rb") as f:
            b = f.read()
    except OSError:
        return False
    i = b[:1024].find(b"%PDF-")
    header = i if i >= 0 else 0
    tail = max(0, len(b) - 2048)
    j = b[tail:].rfind(b"startxref")
    if j < 0:
        return True
    k = tail + j + 9
    while k < len(b) and b[k] in WS:
        k += 1
    m = k
    while m < len(b) and 48 <= b[m] <= 57:
        m += 1
    if m == k or int(b[k:m]) >= 2**64:
        return True
    start = int(b[k:m])

    def section(pos):
        if pos > len(b):
            return False
        rest = b[pos:pos + 64]
        return rest.startswith(b"xref") or OBJ.match(rest) is not None

    return not section(start) and not section(start + header)


def main():
    root, sample_list = sys.argv[1], sys.argv[2]
    sample = [line.strip() for line in open(sample_list) if line.strip()]
    sampled = set(sample)
    with gzip.open(os.path.join(root, "index/safedocs-forms-inventory.tsv.gz"), "rt") as f:
        cols = f.readline().rstrip("\n").split("\t")
        path_at, encrypted_at = cols.index("file"), cols.index("encrypted")
        rows = [(c[path_at], c[encrypted_at] == "1")
                for c in (line.rstrip("\n").split("\t") for line in f)]
    picks = {p for p in sample if p.startswith("pool/")}
    # Every one of the first 75 encrypted rows: which are taken depends
    # on what each one's encryption turns out to be.
    picks.update([p for p, encrypted in rows if encrypted][:75])
    junk = damaged = 0
    for p, _ in rows:
        if junk >= 50 and damaged >= 50:
            break
        if p in sampled:
            continue
        at = os.path.join(root, p)
        h = head(at)
        if h is None:
            continue
        if junk < 50 and h.find(b"%PDF-") > 0:
            junk += 1
            picks.add(p)
        if damaged < 50 and leads_nowhere(at):
            damaged += 1
            picks.add(p)
    for p in sorted(picks):
        print(p)


main()
