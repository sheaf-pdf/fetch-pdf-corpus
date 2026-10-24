#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pikepdf==10.17.0", "pypdfium2==5.14.0", "pyhanko==0.37.0"]
# ///
"""Inventory how PDFs read, one TSV row each: what their bytes show, and
what qpdf (libqpdf 12.4.2, through pikepdf) and PDFium (156.0.8076.0,
through pypdfium2) make of them.

Usage: tools/inventory.py DIR OUT.tsv[.gz] [--feature F] [--procs N]
       tools/inventory.py --index INDEX.tsv.gz OUT.tsv[.gz] [--feature F] [--procs N]
       tools/inventory.py --manifest [CATEGORY] [--procs N]

The first form inventories every file under DIR. The second, the files a
SafeDocs index pools (its pool column 1), that are in pool/safedocs/. The
third inventories <CATEGORY>/manifest.tsv's files (parsing/ by default),
opening each with its password, and writes the columns into it by path,
with `pages` PDFium's count, else qpdf's; for conformance, scans and
signatures it adds that category's feature columns (below). --feature adds
them to the other forms.

Each file is read by each engine in a process of its own, within 60 s and
4 GB, so one that crashes or hangs a library is recorded, not fatal.
Columns:

  size           bytes
  version        the header's
  header_offset  bytes before %PDF-; -1 when it isn't in the first 1 KB
  after_eof      bytes after the last %%EOF, white-space aside; -1: no %%EOF
  eofs           %%EOF markers
  xref           what the last startxref points at: table, hybrid (a table
                 whose trailer has /XRefStm), stream; broken when it points
                 at neither, none when there's no startxref
  linearized     1 when the first object says /Linearized
  objstms        object streams
  filters        the stream filters named, as qpdf reads the streams; when it
                 can't, the names after /Filter a byte scan finds that end
                 in Decode, or are abbreviations or Crypt
  encryption     qpdf's reading: the streams' method, key bits and revision
                 (aes-256 R6; identity when only strings are encrypted);
                 password when it needs one qpdf wasn't given; ? when qpdf
                 can't open it and /Encrypt is in it
  qpdf           ok; warned (it opened with warnings, or a stream doesn't
                 decode); repaired (it rebuilt the cross-reference table);
                 password; failed; crashed; timeout
  qpdf_pages, objects
  fonts          font dictionaries by subtype (Type1:3,TrueType:1)
  unembedded     fonts other than Type0 and Type3 with no font file
  qpdf_message   qpdf's first warning or error
  pdfium         ok; password; format, file, security, unknown (its load
                 error); crashed; timeout
  pdfium_pages

Each feature's <feature>_read says how its reader did: ok, failed,
crashed, timeout.

conformance (qpdf's reading):
  claims         what the XMP claims: PDF/A-<part><level>, PDF/UA-<part>,
                 the PDF/X version
  marked, struct_tree  1 when /MarkInfo /Marked is true; when there's a
                 /StructTreeRoot
  lang           the catalog's /Lang
  output_intents their /S subtypes
  embedded_files entries in the EmbeddedFiles name tree

scans (PDFium's reading of the first 50 pages):
  kind           scan (half the pages or more are covered by an image),
                 scan-ocr (and have invisible text: an OCR layer),
                 scan-text (and visible text), text, or no-text
  pages_read, chars_per_page, images_per_page
  full_page_images   pages an image covers 90% or more of
  invisible_text_pages   pages with text in render mode 3
  image_filters, dpi (the median of the images' horizontal resolution),
  bits (the images' bits per pixel)

signatures (pyHanko 0.37.0's reading; nothing is fetched, no trust roots):
  signatures     signature dictionaries in signed fields
  subfilters     by /SubFilter (adbe.pkcs7.detached:2)
  timestamps     of them document timestamps
  certified      1 when one carries DocMDP; its /P
  dss            1 when the catalog has /DSS (long-term validation data)
  intact, valid  signatures whose digest matches the bytes they cover, and
                 whose signature value checks out against its certificate
  last_coverage  how much of the file the last signature covers (pyHanko's
                 ENTIRE_FILE, ENTIRE_REVISION, CONTIGUOUS_BLOCK_FROM_START,
                 UNCLEAR)
  empty_fields   signature fields with nothing signed in them
  signatures_message  pyHanko's first error
"""
import argparse
import collections
import concurrent.futures
import csv
import gzip
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ["size", "version", "header_offset", "after_eof", "eofs", "xref", "linearized",
        "objstms", "filters", "encryption", "qpdf", "qpdf_pages", "objects", "fonts",
        "unembedded", "qpdf_message", "pdfium", "pdfium_pages"]
FEATURE_COLS = {
    "conformance": ["conformance_read", "claims", "marked", "struct_tree", "lang", "output_intents",
                    "embedded_files"],
    "scans": ["scans_read", "kind", "pages_read", "chars_per_page", "images_per_page", "full_page_images",
              "invisible_text_pages", "image_filters", "dpi", "bits"],
    "signatures": ["signatures_read", "signatures", "subfilters", "timestamps", "certified", "dss", "intact",
                   "valid", "last_coverage", "empty_fields", "signatures_message"],
}
TIMEOUT, MEMORY = 60, 4 << 30
ABBREVIATED = {b"AHx", b"A85", b"LZW", b"Fl", b"RL", b"CCF", b"DCT", b"Crypt"}
PDFIUM_ERRORS = {1: "unknown", 2: "file", 3: "format", 4: "password", 5: "security"}


def scan(data):
    """What the bytes show, without parsing objects."""
    header = data.find(b"%PDF-", 0, 1024)
    m = re.match(rb"%PDF-(\d+\.\d+)", data[header:header + 12]) if header >= 0 else None
    eof = data.rfind(b"%%EOF")
    start = max(header, 0)
    names = set()
    for f in re.findall(rb"/Filter\s*(\[[^\]]*\]|/[\w.+-]+)", data):
        names.update(n.decode() for n in re.findall(rb"/([\w.+-]+)", f, re.ASCII)
                     if n.endswith(b"Decode") or n in ABBREVIATED)
    return {
        "size": len(data),
        "version": m.group(1).decode() if m else "",
        "header_offset": header,
        "after_eof": len(data[eof + 5:].strip()) if eof >= 0 else -1,
        "eofs": data.count(b"%%EOF"),
        "xref": xref_kind(data, header),
        "linearized": int(b"/Linearized" in data[start:start + 2048]),
        "objstms": len(re.findall(rb"/Type\s*/ObjStm\b", data)),
        "scan_filters": ",".join(sorted(names)),
        "encrypt_named": bool(re.search(rb"/Encrypt\b", data)),
    }


def xref_kind(data, header):
    k = data.rfind(b"startxref")
    if k < 0:
        return "none"
    m = re.match(rb"startxref\s+(\d+)", data[k:k + 40])
    if not m:
        return "broken"
    for base in {0, max(header, 0)}:
        at = int(m.group(1)) + base
        head = data[at:at + 64].lstrip()
        if head.startswith(b"xref"):
            t = data.find(b"trailer", at)
            return "hybrid" if t >= 0 and b"/XRefStm" in data[t:t + 4096] else "table"
        if re.match(rb"\d+\s+\d+\s+obj", head) and re.search(rb"/Type\s*/XRef\b",
                                                               data[at:at + 4096]):
            return "stream"
    return "broken"


def passwords(password):
    """A password's encodings to try: UTF-8 (revision 5 on), then
    PDFDocEncoding (before it), which for the Latin-1 letters is Latin-1."""
    tries = [password or ""]
    if password and not password.isascii():
        try:
            tries.append(password.encode("latin-1"))
        except UnicodeEncodeError:
            pass
    return tries


def open_qpdf(path, password):
    """pikepdf's Pdf, or the error (a PasswordError when it needs one)."""
    import pikepdf
    for pw in passwords(password):
        try:
            return pikepdf.open(path, password=pw)
        except pikepdf.PasswordError as e:
            err = e
        except Exception as e:
            return e
    return err


def read_qpdf(path, password):
    import pikepdf
    pdf = open_qpdf(path, password)
    if isinstance(pdf, pikepdf.PasswordError):
        return {"qpdf": "password"}
    if isinstance(pdf, Exception):
        return {"qpdf": "failed", "qpdf_message": unpathed(str(pdf), path)}
    out, problems = {}, []
    try:
        problems += [p.split(": ", 1)[-1] if p.startswith(("WARNING", "ERROR")) else p
                     for p in pdf.check_pdf_syntax()]
    except Exception as e:
        problems.append(str(e))
    try:
        out["qpdf_pages"] = len(pdf.pages)
    except Exception as e:
        problems.append(str(e))
    try:
        out["objects"] = len(pdf.objects)
        out.update(objects(pdf))
    except Exception as e:
        problems.append(str(e))
    if pdf.is_encrypted:
        e = pdf.encryption
        method = str(e.stream_method).rsplit(".", 1)[-1]
        method = "rc4" if e.R <= 3 else {"aesv3": "aes", "none": "identity"}.get(method, method)
        out["encryption"] = f"{method}-{e.bits} R{e.R}"
    repaired = any("reconstruct" in p for p in problems)
    out["qpdf"] = "repaired" if repaired else "warned" if problems else "ok"
    if problems:
        out["qpdf_message"] = unpathed(problems[0], path)
    return out


def objects(pdf):
    """The stream filters named, font dictionaries by subtype, and how many
    fonts lack a font file."""
    import pikepdf
    subtypes, unembedded, filters = collections.Counter(), 0, set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            f = obj.stream_dict.get("/Filter")
            for name in (f if isinstance(f, pikepdf.Array) else [f]):
                if isinstance(name, pikepdf.Name):
                    filters.add(str(name)[1:])
            continue
        if not isinstance(obj, pikepdf.Dictionary) or obj.get("/Type") != "/Font":
            continue
        sub = str(obj.get("/Subtype", "/?"))[1:]
        subtypes[sub] += 1
        if sub not in ("Type0", "Type3"):
            desc = obj.get("/FontDescriptor")
            if not isinstance(desc, pikepdf.Dictionary) or not any(
                    k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                unembedded += 1
    return {"fonts": ",".join(f"{k}:{v}" for k, v in sorted(subtypes.items())),
            "unembedded": unembedded, "filters": ",".join(sorted(filters))}


def unpathed(message, path):
    """qpdf's message without the file's path it starts with."""
    return message.replace(path, "").lstrip(" :")


def read_pdfium(path, password):
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(path, password=password or None)
    except pdfium.PdfiumError as e:
        return {"pdfium": PDFIUM_ERRORS.get(getattr(e, "err_code", 1), "unknown")}
    out = {"pdfium": "ok", "pdfium_pages": len(doc)}
    doc.close()
    return out


def read_conformance(path, password):
    import pikepdf
    pdf = open_qpdf(path, password)
    if isinstance(pdf, Exception):
        return {}
    root, out = pdf.Root, {}
    xmp = b""
    try:
        if "/Metadata" in root:
            xmp = root.Metadata.read_bytes()
    except Exception:
        pass

    def xmp_value(name):
        m = re.search(rb"%s\s*=\s*[\"']([^\"']*)|<%s>([^<]*)<" % (name, name), xmp)
        return (m.group(1) or m.group(2)).decode("utf-8", "replace").strip() if m else ""

    claims = []
    part, level = xmp_value(b"pdfaid:part"), xmp_value(b"pdfaid:conformance")
    if part:
        claims.append(f"PDF/A-{part}{level.lower()}")
    if ua := xmp_value(b"pdfuaid:part"):
        claims.append(f"PDF/UA-{ua}")
    if x := xmp_value(b"pdfx:GTS_PDFXVersion") or xmp_value(b"pdfxid:GTS_PDFXVersion"):
        claims.append(x)
    out["claims"] = ", ".join(claims)
    mark = root.get("/MarkInfo")
    out["marked"] = int(isinstance(mark, pikepdf.Dictionary) and bool(mark.get("/Marked")))
    out["struct_tree"] = int("/StructTreeRoot" in root)
    lang = root.get("/Lang")
    out["lang"] = str(lang)[:20] if isinstance(lang, pikepdf.String) else ""
    intents = root.get("/OutputIntents")
    if isinstance(intents, pikepdf.Array):
        out["output_intents"] = ",".join(sorted(
            str(oi.get("/S", "/?"))[1:] for oi in intents if isinstance(oi, pikepdf.Dictionary)))
    try:
        out["embedded_files"] = len(pdf.attachments)
    except Exception:
        pass
    return out


def read_scans(path, password):
    import statistics
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    try:
        doc = pdfium.PdfDocument(path, password=password or None)
    except pdfium.PdfiumError:
        return {}
    n = min(len(doc), 50)
    chars = images = full = invisible = 0
    filters, dpis, bits = set(), [], set()
    for i in range(n):
        page = doc[i]
        w, h = page.get_size()
        text = page.get_textpage()
        chars += text.count_chars()
        text.close()
        page_full = page_invisible = False
        for obj in page.get_objects(max_depth=4):
            if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                images += 1
                left, bottom, right, top = obj.get_bounds()
                page_full |= (right - left) * (top - bottom) >= 0.9 * w * h
                try:
                    filters.update(obj.get_filters())
                    m = obj.get_metadata()
                    if m.horizontal_dpi > 0:
                        dpis.append(m.horizontal_dpi)
                    bits.add(m.bits_per_pixel)
                except Exception:
                    pass
            elif obj.type == raw.FPDF_PAGEOBJ_TEXT:
                page_invisible |= (raw.FPDFTextObj_GetTextRenderMode(obj.raw)
                                   == raw.FPDF_TEXTRENDERMODE_INVISIBLE)
        full += page_full
        invisible += page_invisible
        page.close()
    doc.close()
    if not n:
        return {"pages_read": 0}
    scanned = full * 2 >= n
    kind = ("scan-ocr" if invisible else "scan-text" if chars else "scan") if scanned else \
        "text" if chars else "no-text"
    return {"kind": kind, "pages_read": n, "chars_per_page": round(chars / n, 1),
            "images_per_page": round(images / n, 2), "full_page_images": full,
            "invisible_text_pages": invisible, "image_filters": ",".join(sorted(filters)),
            "dpi": round(statistics.median(dpis)) if dpis else "",
            "bits": ",".join(str(b) for b in sorted(bits))}


def read_signatures(path, password):
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.fields import enumerate_sig_fields
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext
    out, subfilters, errors = {}, collections.Counter(), []
    with open(path, "rb") as fh:
        reader = PdfFileReader(fh, strict=False)
        if reader.encrypted:
            for pw in passwords(password):
                try:
                    reader.decrypt(pw.encode() if isinstance(pw, str) else pw)
                    break
                except Exception:
                    continue
        sigs = reader.embedded_signatures
        out["signatures"] = len(sigs)
        out["empty_fields"] = sum(1 for _, value, _ in enumerate_sig_fields(reader)
                                  if value is None)
        out["dss"] = int("/DSS" in reader.root)
        intact = valid = timestamps = 0
        certified, coverage = "", ""
        context = ValidationContext(allow_fetching=False)
        for sig in sorted(sigs, key=lambda s: s.signed_revision):
            d = sig.sig_object
            subfilters[str(d.get("/SubFilter", "/?"))[1:]] += 1
            timestamps += d.get("/Type") == "/DocTimeStamp"
            if sig.docmdp_level is not None:
                certified = str(sig.docmdp_level.value)
            try:
                status = validate_pdf_signature(sig, signer_validation_context=context,
                                                ts_validation_context=context, skip_diff=True)
                intact += bool(status.intact)
                valid += bool(status.valid)
                coverage = status.coverage.name
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
                coverage = "?"
        out.update({"subfilters": ",".join(f"{k}:{v}" for k, v in sorted(subfilters.items())),
                    "timestamps": timestamps, "certified": certified, "intact": intact,
                    "valid": valid, "last_coverage": coverage})
        if errors:
            out["signatures_message"] = " ".join(errors[0].split())[:200]
    return out


READERS = {"conformance": read_conformance, "scans": read_scans,
           "signatures": read_signatures}


def one(path, password, engine):
    """In a child: the byte scan and qpdf's reading, or PDFium's, a JSON line
    each, so a crash keeps what came before it."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))
    if engine == "qpdf":
        with open(path, "rb") as fh:
            print(json.dumps(scan(fh.read())), flush=True)
    read = {"qpdf": read_qpdf, "pdfium": read_pdfium, **READERS}[engine]
    status = f"{engine}_read" if engine in READERS else engine
    try:
        row = read(path, password)
        if engine in READERS:
            row[status] = "ok"
    except Exception as e:
        row = {status: "failed", f"{engine}_message": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(row, default=str), flush=True)


def inventory(path, password="", feature=None):
    """In the parent: run `one` on path in a child per engine, and make its row."""
    row = {}
    for engine in ("qpdf", "pdfium") + ((feature,) if feature else ()):
        try:
            r = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "--one", path,
                                "--password", password, "--engine", engine],
                               capture_output=True, timeout=TIMEOUT)
            out, lost = r.stdout, "crashed" if r.returncode < 0 else "failed"
        except subprocess.TimeoutExpired as e:
            out, lost = e.stdout or b"", "timeout"
        for line in out.decode("utf-8", "replace").splitlines():
            try:
                row.update(json.loads(line))
            except ValueError:
                pass
        row.setdefault(f"{engine}_read" if engine in READERS else engine, lost)
    row.pop("pdfium_message", None)
    for engine in ("conformance", "scans"):
        row.pop(f"{engine}_message", None)
    if row.get("qpdf") == "password":
        row["encryption"] = "password"
    elif "encryption" not in row and row.pop("encrypt_named", False) and row["qpdf"] != "ok":
        row["encryption"] = "?"
    row.pop("encrypt_named", None)
    scanned = row.pop("scan_filters", "")
    if "filters" not in row:
        row["filters"] = scanned
    if isinstance(row.get("qpdf_message"), str):
        row["qpdf_message"] = " ".join(row["qpdf_message"].split())[:200]
    return row


def run(jobs, procs, feature=None):
    """inventory() of each (path, password), on `procs` children at once."""
    rows, done = {}, 0
    with concurrent.futures.ThreadPoolExecutor(procs) as ex:
        futures = {ex.submit(inventory, p, pw, feature): p for p, pw in jobs}
        for f in concurrent.futures.as_completed(futures):
            rows[futures[f]] = f.result()
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(jobs)}", file=sys.stderr, flush=True)
    return rows


def summary(rows, feature=None):
    extra = {"conformance": ["claims"], "scans": ["kind"], "signatures": ["last_coverage"]}
    for col in ["xref", "qpdf", "pdfium"] + extra.get(feature, []):
        c = collections.Counter(r.get(col, "") for r in rows.values())
        print(f"  {col}: " + ", ".join(f"{k or '-'} {v}" for k, v in c.most_common()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dir", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--manifest", nargs="?", const="parsing", metavar="CATEGORY")
    ap.add_argument("--feature", choices=sorted(READERS))
    ap.add_argument("--index", help="a SafeDocs index; its pooled files")
    ap.add_argument("--procs", type=int, default=os.cpu_count())
    ap.add_argument("--one", help=argparse.SUPPRESS)
    ap.add_argument("--password", default="", help=argparse.SUPPRESS)
    ap.add_argument("--engine", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.one:
        return one(args.one, args.password, args.engine)
    if args.manifest:
        manifest = os.path.join(ROOT, args.manifest, "manifest.tsv")
        feature = args.feature or (args.manifest if args.manifest in READERS else None)
        want = COLS + FEATURE_COLS.get(feature, [])
        with open(manifest, newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            cols, entries = reader.fieldnames, list(reader)
        rows = run([(os.path.join(ROOT, e["path"]), e.get("password", "")) for e in entries],
                   args.procs, feature)
        cols += [c for c in want if c not in cols]
        for e in entries:
            row = rows[os.path.join(ROOT, e["path"])]
            e.update({c: row.get(c, "") for c in want})
            e["pages"] = row.get("pdfium_pages", row.get("qpdf_pages", ""))
        with open(manifest, "w", newline="") as fh:
            w = csv.DictWriter(fh, cols, delimiter="\t", lineterminator="\n", restval="")
            w.writeheader()
            w.writerows(entries)
        print(f"wrote {os.path.relpath(manifest, ROOT)}: {len(entries)} rows")
        return summary(rows, feature)
    if args.index:
        args.dir, args.out = None, args.dir
        paths = pooled(args.index)
    elif args.dir:
        paths = sorted(os.path.join(d, f) for d, _, fs in os.walk(args.dir) for f in fs)
    if not args.out:
        ap.error("DIR and OUT.tsv, --index INDEX OUT.tsv, or --manifest")
    rows = run([(p, "") for p in paths], args.procs, args.feature)
    cols = COLS + FEATURE_COLS.get(args.feature, [])
    with (gzip.open if args.out.endswith(".gz") else open)(args.out, "wt", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["file"] + cols)
        for p in paths:
            w.writerow([os.path.relpath(p, ROOT)] + [rows[p].get(c, "") for c in cols])
    print(f"wrote {args.out}: {len(paths)} files")
    summary(rows, args.feature)


def pooled(index):
    """The files in pool/safedocs/ that a SafeDocs index pools."""
    paths = []
    with gzip.open(index, "rt", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            name = r["file_name"]
            path = os.path.join(ROOT, "pool", "safedocs", name[:4], name)
            if r.get("pool", "1") == "1" and os.path.exists(path):
                paths.append(path)
    return paths


main()
