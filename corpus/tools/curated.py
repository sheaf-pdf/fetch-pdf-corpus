#!/usr/bin/env python3
"""Copy the curated test sets, at their pinned versions, into their categories.

Usage: tools/curated.py [--refetch]

Each set belongs to a category (parsing/, conformance/, signatures/). It is
fetched whole into pool/<name>/<version>/ (gitignored) once, from a commit
or a DOI's archive, and the files it lists are copied to <category>/<name>/
under upstream's own paths, less a prefix where noted. With them come
upstream's licenses and whatever says what each test file should do:
READMEs, qpdf's .out files and .test scripts, JHOVE's spreadsheet, BFO's
description.txt. PDFium's .in templates are expanded with its own
fixup_pdf_template.py, at the same commit, into parsing/pdfium/generated/;
one that comes out the same as the PDF checked in beside it is left out, as
is any file a set lists to skip (those ClamAV flags), with the reason in its
SOURCE.md.

Then rewrites these sets' rows of <category>/manifest.tsv, a row per test
file (the PDFs, and qpdf's .fuzz inputs), and writes <category>/<name>/
SOURCE.md. Columns this tool doesn't own are kept, matched by path:

  source    the file at its pinned version
  license   SPDX, from the set's license
  sha256    of the copy
  expect    valid: upstream says it's correct, so a reader reads all of it;
            invalid: upstream says it breaks the spec, so a reader repairs
            or refuses it, but never crashes, hangs or runs out of memory;
            empty: upstream says neither
  password  the password it's encrypted with, as text, unnormalised, when
            upstream says; a reader encodes it as the revision has it
            (PDFDocEncoding to revision 4, UTF-8 from 5)
  note      what upstream says about it
  profile, clause  (conformance) the profile and clause a file tests, from
            its path; expect is then pass or fail against that profile
"""
import argparse
import base64
import csv
import fnmatch
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = ["path", "source", "license", "sha256", "pages"]
OWN = ["source", "license", "sha256", "expect", "password", "note"]


def github(repo):
    return lambda commit, path: (f"https://github.com/{repo}/blob/{commit}/"
                                 + urllib.parse.quote(path))


PDFIUM = "https://pdfium.googlesource.com/pdfium"

SOURCES = [
    {
        "name": "pdfa-safedocs", "title": "PDF Association, SafeDocs targeted test files",
        "fetch": ("github", "pdf-association/safedocs"),
        "version": "68f730ab8c39204844112c499c5707b79878533b",
        "url": github("pdf-association/safedocs"), "license": "Apache-2.0",
        "license_note": "NOTICE.txt licenses the repository's software under Apache-2.0 and "
                        "its other documentation under CC-BY-4.0, without saying which the "
                        "PDFs are; the manifest says Apache-2.0, the repository's LICENSE.",
        "take": ["LICENSE", "NOTICE.txt", "README.md", "CompactedSyntax/*", "Dialects/*",
                 "Inline Image Abbreviations/*", "Miscellaneous Targeted Test PDFs/*",
                 "Unicode passwords/*"],
        "tests": ["*.pdf"],
    },
    {
        "name": "pdf-differences", "title": "PDF Association, PDF Differences (parsing folders)",
        "fetch": ("github", "pdf-association/pdf-differences"),
        "version": "907fe96e52b73e491489eee545c47b119bf9989b",
        "url": github("pdf-association/pdf-differences"), "license": "CC-BY-4.0",
        "license_note": "README.md: the PDFs are CC-BY-4.0, the code Apache-2.0. The two "
                        "UnknownFilter files named by sha256 are real-world files, under "
                        "their owners' rights.",
        "take": ["LICENSE", "README.md", "UnknownFilter/*", "PDF-version/*"],
        "tests": ["*.pdf"],
    },
    {
        "name": "pdf20examples", "title": "PDF Association, PDF 2.0 examples",
        "fetch": ("github", "pdf-association/pdf20examples"),
        "version": "c20f2c17bfcc4baab7cfe62e70fae64caf14d5fa",
        "url": github("pdf-association/pdf20examples"), "license": "CC-BY-SA-4.0",
        "take": ["LICENSE.md", "README.md", "*.pdf"],
        "tests": ["*.pdf"],
    },
    {
        "name": "jhove", "title": "Lindlar, Synthetic PDF Testset for File Format Validation "
                                  "(TIB, 2017), DOI 10.22000/53",
        "fetch": ("archive", "https://www.radar-service.eu/radar-backend/archives/"
                             "JtlOdwQquZWDqQdq/versions/1/content",
                  "33b2b4510f70aae3f4c606ca087292fc506759bb917e3b9bf9663d74ffca4f43"),
        "version": "10.22000-53",
        "url": lambda _, path: "https://doi.org/10.22000/53#" + urllib.parse.quote(
            path.removeprefix("10.22000-53/data/dataset/")),
        "license": "CC-BY-SA-4.0", "strip": "10.22000-53/data/dataset/",
        "take": ["10.22000-53/data/dataset/*"],
        "tests": ["*.pdf"],
    },
    {
        "name": "hayro", "title": "hayro's load tests: crashes its fuzzers found",
        "fetch": ("github", "LaurenzV/hayro"),
        "version": "643fe553e7b379047d9832183fecb42f0980f5f3",
        "url": github("LaurenzV/hayro"), "license": "MIT OR Apache-2.0",
        "strip": "hayro-tests/pdfs/",
        "take": ["LICENSE-MIT", "LICENSE-APACHE", "hayro-tests/pdfs/load/*.pdf"],
        "tests": ["*.pdf"],
    },
    {
        "name": "qpdf", "title": "qpdf's test suite and OSS-Fuzz reproducers",
        "fetch": ("github", "qpdf/qpdf"),
        "version": "4eba95899886e851cc41d76886483b347612f2a8",
        "url": github("qpdf/qpdf"), "license": "Apache-2.0", "strip": "qpdf/",
        "take": ["LICENSE.txt", "qpdf/qtest/*.test", "qpdf/qtest/qpdf/*.pdf",
                 "qpdf/qtest/qpdf/*.out", "fuzz/qpdf_extra/*.fuzz"],
        "tests": ["*.pdf", "*.fuzz"],
    },
    {
        "name": "pdfium", "title": "PDFium's test resources",
        "fetch": ("gitiles", PDFIUM, ["testing/resources", "testing/tools"], ["LICENSE"]),
        "version": "8fdce8e1b14def5d34f217c5099936ddcebe8b81",
        "url": lambda commit, path: f"{PDFIUM}/+/{commit}/" + urllib.parse.quote(
            re.sub(r"^generated/(.*)\.pdf$", r"testing/resources/\1.in", path)),
        "license": "BSD-3-Clause", "strip": "testing/",
        "take": ["LICENSE", "testing/resources/*.pdf", "testing/resources/*.in",
                 "testing/resources/*.fragment", "generated/*.pdf"],
        "tests": ["*.pdf"],
        "skip": {
            "resources/pixel/bug_867501.pdf":
                "ClamAV 1.5.4/28160 flags it: Pdf.Exploit.TALOS_2018_0639-6680787-0",
            "generated/js.pdf": "ClamAV 1.5.4/28160 flags it: Pdf.Dropper.Agent-7158272-0",
        },
    },
    {
        "name": "verapdf", "category": "conformance",
        "title": "veraPDF's test corpus for PDF/A and PDF/UA, less Isartor",
        "fetch": ("github", "veraPDF/veraPDF-corpus"),
        "version": "bb75f4f0073d9350dfd058c0162a367e6fadf25e",
        "url": github("veraPDF/veraPDF-corpus"), "license": "CC-BY-4.0",
        "license_note": "README.md. Its Isartor test files are left out: Isartor's own "
                        "terms forbid redistributing them.",
        "take": ["README.md", "PDF_A-*/*", "PDF_UA-*/*", "ISO 32000-*/*",
                 "TWG test files/*", "Undefined/*"],
        "tests": ["*.pdf"],
    },
    {
        "name": "bfo", "category": "conformance", "title": "BFO's PDF/A test suite",
        "fetch": ("github", "bfocom/pdfa-testsuite"),
        "version": "c22d80260c7ec975a511f9bfcc6590b7ceb1cf18",
        "url": github("bfocom/pdfa-testsuite"), "license": "CC-BY-3.0",
        "take": ["README.md", "description.txt", "*.pdf"],
        "tests": ["*.pdf"],
    },
    {
        "name": "pyhanko", "category": "signatures",
        "title": "pyHanko's test PDFs: signed files, and the inputs its signing tests start from",
        "fetch": ("github", "MatthiasValvekens/pyHanko"),
        "version": "2be0a1b4618bfd2ec95011d574f2e12da58344ba",
        "url": github("MatthiasValvekens/pyHanko"), "license": "MIT",
        "strip": "internal/common-test-utils/src/pyhanko_testing_commons/test_data/data/",
        "take": ["LICENSE",
                 "internal/common-test-utils/src/pyhanko_testing_commons/test_data/data/pdf/*.pdf"],
        "tests": ["*.pdf"],
        "skip": {"pdf/safedocs/CompactedPDFSyntaxTest.pdf":
                 "A copy of parsing/pdfa-safedocs/CompactedSyntax/CompactedPDFSyntaxTest.pdf"},
    },
]


def get(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return r.read()


def untar(data, dest, strip=0):
    """Extract a tar's regular files and directories under dest, without its
    first `strip` path components."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            parts = m.name.split("/")[strip:]
            if not parts or not parts[0] or not (m.isfile() or m.isdir()):
                continue
            tf.extract(m.replace(name="/".join(parts), deep=False), dest, filter="data")


def fetch(src, refetch):
    """pool/<name>/<version>/, fetched if it isn't complete."""
    dest = os.path.join(ROOT, "pool", src["name"], src["version"])
    done = os.path.join(dest, ".fetched")
    if os.path.exists(done) and not refetch:
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    kind, *args = src["fetch"]
    commit = src["version"]
    if kind == "github":
        untar(get(f"https://codeload.github.com/{args[0]}/tar.gz/{commit}"), dest, strip=1)
    elif kind == "gitiles":
        base, dirs, files = args
        for d in dirs:
            untar(get(f"{base}/+archive/{commit}/{d}.tar.gz"), os.path.join(dest, d))
        for f in files:
            with open(os.path.join(dest, f), "wb") as fh:
                fh.write(base64.b64decode(get(f"{base}/+/{commit}/{f}?format=TEXT")))
    elif kind == "archive":
        url, digest = args
        data = get(url)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit(f"{src['name']}: {url} isn't sha256 {digest}")
        untar(data, dest)
    if src["name"] == "pdfium":
        expand_templates(dest)
    open(done, "w").close()
    return dest


def expand_templates(dest):
    """Each testing/resources/**/x.in to generated/**/x.pdf, by PDFium's own
    script; dropped when it equals the x.pdf checked in beside the .in."""
    res, gen = os.path.join(dest, "testing", "resources"), os.path.join(dest, "generated")
    script = os.path.join(dest, "testing", "tools", "fixup_pdf_template.py")
    dirs = sorted({d for d, _, fs in os.walk(res) for f in fs if f.endswith(".in")})
    for d in dirs:
        out = os.path.join(gen, os.path.relpath(d, res))
        os.makedirs(out, exist_ok=True)
        ins = sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".in"))
        subprocess.run(["python3", "-I", script, "--output-dir", out, *ins], check=True,
                       cwd=dest)
    for d, _, fs in os.walk(gen):
        for f in fs:
            path = os.path.join(d, f)
            twin = os.path.join(res, os.path.relpath(path, gen))
            if os.path.exists(twin) and same(path, twin):
                os.remove(path)


def same(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def listing(top):
    for d, dirs, fs in os.walk(top):
        dirs.sort()
        for f in sorted(fs):
            yield os.path.relpath(os.path.join(d, f), top).replace(os.sep, "/")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# What upstream says about each file: (expect, password, note), by its path
# under parsing/<name>/. Sets read from upstream's own files are below.
PDFA_MISC = "Miscellaneous Targeted Test PDFs/"
PDFA_PW = "Unicode passwords/"
SAID = {
    "pdfa-safedocs": {
        "CompactedSyntax/CompactedPDFSyntaxTest.pdf": (
            "valid", "", "Every permitted pairing of adjacent tokens, with as little white-space "
                         "as the syntax allows, in the body and in a content stream"),
        "CompactedSyntax/CompactedPDFSyntaxMatrix.pdf": (
            "valid", "", "The 121 token pairings CompactedPDFSyntaxTest.pdf covers"),
        "Dialects/Dialect-ContentStreams.pdf": (
            "valid", "", "PASS: direct dictionaries everywhere content stream syntax allows one"),
        "Dialects/Dialect-ContentStreamsViaResourceNames.pdf": (
            "valid", "", "PASS: as Dialect-ContentStreams.pdf, also through resource names"),
        "Dialects/Dialect-ContentStreamsWithIndirectRefs.pdf": (
            "invalid", "", "FAIL: indirect references in a content stream, which ISO 32000 "
                           "7.8.2 forbids"),
        "Dialects/Dialect-DictIsStream.pdf": (
            "invalid", "", "A stream where a dictionary belongs"),
        "Dialects/Dialect-StreamIsDict.pdf": (
            "invalid", "", "A dictionary, no stream or endstream, where a stream belongs"),
        "Inline Image Abbreviations/InlineAbbreviations.pdf": (
            "valid", "", "8 inline images with both full and abbreviated keys, which differ; the "
                         "abbreviation wins (ISO 32000-2 8.9.7). Correct: the same image 8 times, "
                         "no green showing"),
        PDFA_MISC + "FontinsideType3insideType3.pdf": (
            "valid", "", "Fonts: 2 Type 3 and Helvetica, nested"),
        PDFA_MISC + "PatternTextInsideText.pdf": (
            "valid", "", "Fonts: Helvetica and Times-Italic, one inside a shading pattern"),
        PDFA_MISC + "ContentStreamCycleType3insideType3.pdf": (
            "invalid", "", "Type 3 glyphs that draw each other in a cycle: for crashes, "
                           "deadlock and running out of memory"),
        PDFA_MISC + "ContentStreamNoCycleType3insideType3.pdf": (
            "valid", "", "The cycle case without the cycle. Fonts: 2 Type 3 and Helvetica"),
        PDFA_MISC + "PDF-NoPageContents.pdf": (
            "valid", "", "A page without /Contents: an empty page"),
        PDFA_MISC + "LinkAnnot-appearances.pdf": (
            "valid", "", "A link with an appearance stream and 3 URIs chained by /Next"),
        PDFA_MISC + "uri-ismap-test.pdf": ("valid", "", "A URI action with /IsMap"),
        PDFA_MISC + "list-of-uri-actions.pdf": (
            "valid", "", "URI actions chained in a list and in a tree"),
        PDFA_MISC + "list-of-uri-actions-with-base.pdf": (
            "valid", "", "As list-of-uri-actions.pdf, with the catalog's /URI /Base"),
        PDFA_MISC + "Dual-startxref.pdf": (
            "valid", "", "Two startxrefs before %%EOF: the last is the file's. Correct: "
                         "\"Second startxref\" in red; \"First startxref\" in blue is wrong"),
        PDFA_MISC + "utf16LE-test.pdf": (
            "invalid", "", "UTF-16LE text strings, which ISO 32000 doesn't allow; readers "
                           "often take them"),
        PDFA_PW + "baseline/unicode-test.pdf": (
            "valid", "", "PDF 2.0, not encrypted; U+32FF in a ToUnicode map"),
        PDFA_PW + "baseline/unicode-test-actualtext.pdf": (
            "valid", "", "PDF 2.0, not encrypted; U+32FF in ActualText"),
        PDFA_PW + "corrigendum4/unicode-test-U2F874-correct.pdf": (
            "valid", "Password\u5F33!",
            "Encrypted with Password<U+2F874>! SASLprep'd as Unicode 3.2 has it, "
            "Password<U+5F33>!; a reader that SASLpreps opens it with Password<U+2F874>! too"),
        PDFA_PW + "corrigendum4/unicode-test-U2F874-wrong.pdf": (
            "valid", "Password\u5F53!",
            "Encrypted with Password<U+2F874>! normalised by a later Unicode, "
            "Password<U+5F53>!; Password<U+2F874>! must not open it"),
        PDFA_PW + "corrigendum5/unicode-corrigendum5-fixed.pdf": (
            "valid", "\u1100\u0300\u1161\u0323",
            "Encrypted with this password unnormalised; whether it opens the file in a reader "
            "that SASLpreps is undefined (corrigendum5.md)"),
        PDFA_PW + "corrigendum5/unicode-corrigendum5-unicode32-once.pdf": (
            "valid", "\uAC00\u0300\u0323",
            "Encrypted with this password; a reader that SASLpreps must not open it with it, "
            "and may with <U+1100><U+0300><U+1161><U+0323> (corrigendum5.md)"),
        PDFA_PW + "corrigendum5/unicode-corrigendum5-unicode32-twice.pdf": (
            "valid", "\uAC00\u0323\u0300",
            "Encrypted with this password, already normalised; <U+AC00><U+0300><U+0323> "
            "opens it in a reader that SASLpreps, <U+1100><U+0300><U+1161><U+0323> must not"),
    },
    "pdf-differences": {
        "UnknownFilter/UnknownFilter-xrefstm.pdf": (
            "invalid", "", "%PDF-3.x; the unknown /XXXDecode on the cross-reference stream: "
                           "unreadable without recovery"),
        "UnknownFilter/UnknownFilter-objstm.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on the object stream: its objects can't be read"),
        "UnknownFilter/UnknownFilter-Linearized.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on the linearization data only: ignoring it, "
                           "the file reads in full"),
        "UnknownFilter/UnknownFilter-PageContentStream.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on the page's content: pages count and size, "
                           "but don't draw"),
        "UnknownFilter/UnknownFilter-Font.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on the embedded font only"),
        "UnknownFilter/UnknownFilter-ImageXObject.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on the image XObject only"),
        "UnknownFilter/UnknownFilter-FormXObject.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on the form XObject only"),
        "UnknownFilter/UnknownFilter-ICC.pdf": (
            "invalid", "", "%PDF-3.x; /XXXDecode on an ICC profile (/N 4, no /Alternate) only"),
        "UnknownFilter/UnknownFilter-OutlineObjStm.pdf": (
            "invalid", "", "%PDF-3.x; a hybrid file whose update's object stream, the outline's "
                           "3 objects, has /XXXDecode: the rest reads"),
        "UnknownFilter/382252277877c00a92f3bb54daef1413bafe5557e2f5f7c32c23e800cd9bdcd4.pdf": (
            "invalid", "", "Real-world: an image that can't be drawn; upstream keeps the cause "
                           "a puzzle"),
        "UnknownFilter/4387ba480c6f935b8e97749d40b3c001ad584b7bda66ca2c040be67337669497.pdf": (
            "invalid", "", "Real-world: an image that can't be drawn; upstream keeps the cause "
                           "a puzzle"),
        "PDF-version/PDF-versions1.pdf": (
            "valid", "", "Header 1.4, catalog /Version 1.5, an update's catalog (generation 1) "
                         "/Version 1.6: the version is 1.6"),
        "PDF-version/PDF-versions2.pdf": (
            "valid", "", "As PDF-versions1.pdf, the update's catalog a new object: the version "
                         "is 1.6"),
        "PDF-version/PDF-versions3.pdf": (
            "valid", "", "As PDF-versions2.pdf, also using PDF 2.0's UseBlackPtComp: the "
                         "version is 1.6"),
    },
    "pdf20examples": {
        "PDF 2.0 with offset start.pdf": (
            "valid", "", "Text before %PDF-2.0; offsets count from the header"),
        "PDF 2.0 via incremental save.pdf": (
            "valid", "", "A 1.7 file made 2.0 by an incremental update's catalog /Version"),
        "PDF 2.0 UTF-8 string and annotation.pdf": (
            "valid", "", "UTF-8 text strings (Thai) in an annotation"),
        "pdf20-utf8-test.pdf": (
            "valid", "", "UTF-8 strings in outlines, layer names, alternate text and Info"),
        "Simple PDF 2.0 file.pdf": ("valid", "", "Content commented operator by operator"),
        "PDF 2.0 image with BPC.pdf": (
            "valid", "", "Black point compensation; two CalRGB spaces over one image"),
        "PDF 2.0 with page level output intent.pdf": (
            "valid", "", "A page-level output intent beside the file's PDF/X one"),
    },
}
# Passwords, from the upstream tests that open these files (qpdf's qtest
# scripts, PDFium's *_embeddertest.cpp), each checked to open the file in
# qpdf and PDFium: (password, expect, note). The user password where it's
# known, else the owner's.
PDFIUM_TESTS = "PDFium's embedder tests"
QPDF_TESTS = "qpdf's qtest scripts"
HOTEL = ("hôtel", "", "User password hôtel, owner âge (" + PDFIUM_TESTS + ")")
PASSWORDS = {
    "pdfium": {
        "resources/bug_644.pdf": ("a", "", "User password a, owner b (" + PDFIUM_TESTS + ")"),
        "generated/bug_644.pdf": ("a", "", "User password a, owner b (" + PDFIUM_TESTS + ")"),
        "resources/bug_1124998.pdf": ("test", "", "Owner password test (" + PDFIUM_TESTS + ")"),
        "resources/encrypted.pdf": ("1234", "", "User password 1234, owner 5678 ("
                                    + PDFIUM_TESTS + ")"),
        "resources/encrypted_hello_world_r2.pdf": HOTEL,
        "resources/encrypted_hello_world_r3.pdf": HOTEL,
        "resources/encrypted_hello_world_r5.pdf": HOTEL,
        "resources/encrypted_hello_world_r6.pdf": HOTEL,
        "resources/encrypted_hello_world_r2_bad_okey.pdf": (
            "", "invalid", "A bad owner key: PDFium's test opens it with password a, expecting "
                           "failure and no crash (crbug.com/42270437)"),
        "resources/encrypted_hello_world_r3_bad_okey.pdf": (
            "", "invalid", "A bad owner key: PDFium's test opens it with password a, expecting "
                           "failure and no crash (crbug.com/42270437)"),
        "resources/bug_424613308.pdf": ("", "invalid", "PDFium's test expects it not to open"),
    },
    "qpdf": {
        "qtest/qpdf/20-pages.pdf": ("user", "", "User password user, owner owner"),
        "qtest/qpdf/pages-copy-encryption.pdf": ("user", "", "User password user, owner owner"),
        "qtest/qpdf/bad-encryption-length.pdf": (
            "", "", "qpdf opens it with an empty user password (owner o), with warnings; "
                    "PDFium won't open it"),
        "qtest/qpdf/c-r2.pdf": ("user1", "", "User password user1"),
        "qtest/qpdf/c-r3.pdf": ("owner2", "", "Owner password owner2"),
        "qtest/qpdf/c-r4.pdf": ("owner2", "", "Owner password owner2"),
        "qtest/qpdf/c-r5-in.pdf": ("user3", "", "User password user3, owner owner3"),
        "qtest/qpdf/c-r6-in.pdf": ("owner4", "", "Owner password owner4"),
        "qtest/qpdf/enc-R2,V1,U=view,O=master.pdf": ("view", "", "User password view, owner master"),
        "qtest/qpdf/enc-R2,V1,U=view,O=view.pdf": ("view", "", "User and owner password view"),
        "qtest/qpdf/enc-R3,V2,U=view,O=master.pdf": ("view", "", "User password view, owner master"),
        "qtest/qpdf/enc-R3,V2,U=view,O=view.pdf": ("view", "", "User and owner password view"),
        "qtest/qpdf/enc-XI-R6,V5,U=view,O=master.pdf": ("view", "", "User password view, owner master"),
        "qtest/qpdf/enc-XI-R6,V5,U=view,attachments,cleartext-metadata.pdf": (
            "view", "", "Password view"),
        "qtest/qpdf/enc-XI-R6,V5,U=wwwww,O=wwwww.pdf": ("wwwww", "", "User and owner password wwwww"),
        "qtest/qpdf/enc-XI-R6,V5,U=attachment,encrypted-attachments.pdf": (
            "attachment", "", "Password attachment; PDFium opens it with any password or none"),
        "qtest/qpdf/nontrivial-crypt-filter.pdf": (
            "asdfqwer", "", "Password asdfqwer; PDFium opens it with any password or none"),
        "qtest/qpdf/unfilterable-with-crypt.pdf": (
            "attachment", "", "Password attachment; PDFium opens it with any password or none"),
        "qtest/qpdf/enc-XI-long-password.pdf": (
            "qwertyuiopasdfghjklzxcvbnm" * 4 + "qwertyuiopasdfghjklzxcv", "",
            "A 127-byte password, all AES-256 keeps"),
        "qtest/qpdf/enc-long-password.pdf": (
            "asdf asdf asdf asdf asdf asdf qw", "",
            "A 32-byte password, all RC4 keeps: longer ones starting so open it too"),
        "qtest/qpdf/encrypted-40-bit-R3.pdf": ("623", "", "User password 623"),
        "qtest/qpdf/job-json-encrypt-128.pdf": ("u", "", "User password u, owner o"),
        "qtest/qpdf/job-json-encrypt-40.pdf": ("u", "", "User password u, owner o"),
        "qtest/qpdf/short-O-U.pdf": ("19723102477", "", "Owner password 19723102477; /O and /U "
                                                       "shorter than 32 bytes (qpdf issue 96)"),
    },
}

UNKNOWN_LICENSE = {
    "pdf-differences/UnknownFilter/382252277877c00a92f3bb54daef1413bafe5557e2f5f7c32c23e800cd9bdcd4.pdf",
    "pdf-differences/UnknownFilter/4387ba480c6f935b8e97749d40b3c001ad584b7bda66ca2c040be67337669497.pdf",
}


def jhove_said(top):
    """Each test file's row of JHOVE's spreadsheet, as (expect, password, note)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(os.path.join(
        top, "PDF_Validation_Test_corpus_description_and_outcome.xlsx"))
    shared = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{{{ns['m']}}}row"):
        cells = []
        for c in row.findall("m:c", ns):
            v = c.find("m:v", ns)
            text = "" if v is None else v.text
            cells.append(shared[int(text)] if c.get("t") == "s" and text else text)
        rows.append([" ".join(c.split()) for c in cells])
    said = {}
    for r in rows[1:]:
        if len(r) < 7 or not r[3].endswith(".pdf"):
            continue
        said["Test_Corpus/" + r[3]] = (
            "invalid", "", f"{r[1]}: {r[2]}. JHOVE 1.16.5: {r[4].rstrip('.')}"
                           f"{' (' + r[5] + ')' if r[5] not in ('', '-') else ''}. "
                           f"Acrobat XI opens it: {r[6]}")
    return said


def qpdf_said(top):
    """qpdf's own words on its broken files and bug reproducers."""
    said = {}
    text = open(os.path.join(top, "qtest", "error-condition.test")).read()
    block = text[text.index("my @badfiles"):]
    block = block[:block.index(");")]
    fails = re.search(r"for \(([\d, .]+)\)\s*\{\s*\$recover_failures", text).group(1)
    no_recover = {int(n) for n in re.findall(r"\d+", fails)}
    for desc, n in re.findall(r'"([^"]+)",\s*#\s*(\d+)', block):
        n = int(n)
        how = "qpdf can't recover it" if n in no_recover else "qpdf recovers it"
        said[f"qtest/qpdf/bad{n}.pdf"] = (
            "invalid", "", f"qpdf bad{n}: {desc}; {how} (bad{n}.out, bad{n}-recover.out)")
    text = open(os.path.join(top, "qtest", "specific-bugs.test")).read()
    for n, desc, status in re.findall(r'\["([^"]+)",\s*"([^"]+)",\s*(\d)', text):
        name = f"issue-{n}" if os.path.exists(
            os.path.join(top, "qtest", "qpdf", f"issue-{n}.pdf")) else n
        what = "errors" if status == "2" else "warnings"
        ref = f"qpdf issue {n}" if not n.startswith("fuzz") else f"qpdf {n}"
        said[f"qtest/qpdf/{name}.pdf"] = (
            "invalid", "", f"{ref}: {desc}; qpdf exits with {what} ({name}.out)")
    for f in os.listdir(os.path.join(top, "fuzz", "qpdf_extra")):
        said[f"fuzz/qpdf_extra/{f}"] = (
            "invalid", "", f"OSS-Fuzz reproducer {f.split('.')[0]}: no crash, no hang")
    return said


def pdfium_said(path):
    stem = os.path.basename(path).rsplit(".", 1)[0]
    notes = []
    if path.startswith("generated/"):
        notes.append(f"Expanded from {stem}.in by testing/tools/fixup_pdf_template.py")
    if re.fullmatch(r"bug_\d+(_\d+)?|\d{6,}", stem):
        notes.append("PDFium regression test")
    return ("", "", "; ".join(notes))


def verapdf_said(rel):
    """A veraPDF corpus file's profile, clause and outcome, from its path."""
    parts = rel.split("/")
    m = re.search(r"-(pass|fail)-", parts[-1])
    profile = parts[0]
    if profile == "TWG test files":
        t = re.search(r"-(pdfa|pdfua)(\d)-", parts[-1])
        profile = f"PDF_{'A' if t.group(1) == 'pdfa' else 'UA'}-{t.group(2)}" if t else profile
    return {"expect": m.group(1) if m else "", "profile": profile,
            "clause": "/".join(parts[1:-1]), "note": ""}


def bfo_said(out):
    """BFO's files' profile, clause, outcome and reason, from description.txt."""
    said = {}
    for line in open(os.path.join(out, "description.txt"), encoding="utf-8", errors="replace"):
        name, _, why = line.strip().partition("\t")
        m = re.match(r"pdfa(\d)-([\d-]+?)-bfo-t\d+-(pass|fail)\.pdf$", name)
        if m:
            said[name] = {"expect": m.group(3), "profile": f"PDF_A-{m.group(1)}",
                          "clause": m.group(2).replace("-", "."), "note": why.strip()}
    return said


def copy_set(src, top):
    """<category>/<name>/ as the set's take lists, and its rows."""
    cat = src.get("category", "parsing")
    out = os.path.join(ROOT, cat, src["name"])
    strip = src.get("strip", "")
    wanted = {}
    for path in listing(top):
        if path == ".fetched" or not any(fnmatch.fnmatch(path, p) for p in src["take"]):
            continue
        rel = path[len(strip):] if path.startswith(strip) else path
        if rel not in src.get("skip", {}):
            wanted[rel] = path
    if os.path.isdir(out):
        for rel in listing(out):
            if rel not in wanted and rel != "SOURCE.md":
                os.remove(os.path.join(out, rel))
    for rel, up in wanted.items():
        target = os.path.join(out, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not os.path.exists(target) or not same(target, os.path.join(top, up)):
            shutil.copyfile(os.path.join(top, up), target)
    for d, dirs, fs in os.walk(out, topdown=False):
        if not dirs and not fs:
            os.rmdir(d)

    name = src["name"]
    said = dict(SAID.get(name, {}))
    if name == "jhove":
        said.update(jhove_said(out))
    elif name == "qpdf":
        said.update(qpdf_said(out))
    bfo = bfo_said(out) if name == "bfo" else {}
    rows = {}
    for rel, up in wanted.items():
        if not any(fnmatch.fnmatch(rel, p) for p in src["tests"]):
            continue
        path = f"{cat}/{name}/{rel}"
        expect, password, note = (pdfium_said(rel) if name == "pdfium"
                                  else said.get(rel, ("", "", "")))
        if name == "hayro":
            note = "hayro load test: it loads without crashing"
        if rel in PASSWORDS.get(name, {}):
            password, said_expect, said_note = PASSWORDS[name][rel]
            expect = said_expect or expect
            note = "; ".join(n for n in (note, said_note) if n)
        if name == "pyhanko" and rel.startswith("pdf/coverage-anomalies/"):
            note = "A signature whose /ByteRange covers the file oddly"
        row = {
            "source": src["url"](src["version"], up),
            "license": "unknown" if f"{name}/{rel}" in UNKNOWN_LICENSE else src["license"],
            "sha256": sha256(os.path.join(out, rel)),
            "expect": expect, "password": password, "note": note,
        }
        if name == "verapdf":
            row.update(verapdf_said(rel))
        elif name == "bfo":
            row.update(bfo.get(rel, {"expect": "", "profile": "", "clause": "", "note": ""}))
        rows[path] = row
    write_source_md(src, cat, out, len(rows))
    return rows


def write_source_md(src, cat, out, n):
    kind, *args = src["fetch"]
    if kind == "archive":
        where = f"{args[0]} (sha256 {args[1]})"
    elif kind == "gitiles":
        where = f"{args[0]} at {src['version']}"
    else:
        where = f"https://github.com/{args[0]} at {src['version']}"
    lines = [f"# {src['name']}", "", src["title"] + ".", "",
             f"- From: {where}",
             f"- Taken: {', '.join('`' + p + '`' for p in src['take'])}"
             + (f", less the prefix `{src['strip']}`" if src.get("strip") else ""),
             f"- Test files: {n}, a row each in `{cat}/manifest.tsv`",
             f"- License: {src['license']}" + (f". {src['license_note']}"
                                                if src.get("license_note") else ""),
             *(f"- Left out: `{rel}`. {why}" for rel, why in src.get("skip", {}).items()),
             "", "Written by `tools/curated.py`; rerun it rather than edit this.", ""]
    with open(os.path.join(out, "SOURCE.md"), "w") as fh:
        fh.write("\n".join(lines))


def write_manifest(cat, sets):
    manifest = os.path.join(ROOT, cat, "manifest.tsv")
    old, cols = {}, CORE + ["expect", "password", "note"]
    for set_rows in sets.values():
        for row in set_rows.values():
            cols += [c for c in row if c not in cols]
    if os.path.exists(manifest):
        with open(manifest, newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            cols += [c for c in reader.fieldnames if c not in cols]
            old = {r["path"]: r for r in reader}
    ours = tuple(f"{cat}/{name}/" for name in sets)
    rows = {p: r for p, r in old.items() if not p.startswith(ours)}
    for set_rows in sets.values():
        for path, new in set_rows.items():
            row = dict(old.get(path, {}))
            row.update(new, path=path)
            rows[path] = row
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        for path in sorted(rows):
            w.writerow(rows[path])
    return os.path.relpath(manifest, ROOT)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refetch", action="store_true", help="fetch every set again")
    args = ap.parse_args()
    by_cat = {}
    for src in SOURCES:
        cat = src.get("category", "parsing")
        top = fetch(src, args.refetch)
        rows = by_cat.setdefault(cat, {})[src["name"]] = copy_set(src, top)
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _, fs in os.walk(os.path.join(ROOT, cat, src["name"])) for f in fs)
        print(f"  {cat + '/' + src['name']:28} {len(rows):5} test files  {size / 1e6:6.1f} MB")
    for cat, sets in by_cat.items():
        print(f"wrote {write_manifest(cat, sets)}: {sum(map(len, sets.values()))} curated rows")


main()
