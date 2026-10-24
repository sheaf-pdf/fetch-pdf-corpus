# pdf-corpus

Real-world PDFs for testing [sheaf](https://github.com/gechelberger/sheaf),
kept out of the sheaf repo so its history stays small.

Files here are frozen. Nothing is fetched from an issuer's current link:
issuers replace their forms every year, and a test corpus that drifts can't
be measured against. To take a newer revision, add it as a new file with
its own manifest row. The one bulk download, the SafeDocs pool, comes from a
fixed 2021 archive and is checked against its sha256s.

## Layout

```
forms/             form PDFs, in git; a row each in forms/manifest.tsv
parsing/           PDFs for testing a reader; a row each in parsing/manifest.tsv
conformance/       PDF/A, PDF/UA and PDF/X files, claimed and tested
scans/             scanned documents, with and without OCR text
signatures/        signed documents
index/             what each pool holds, and inventories of it
tools/             scripts that build the indexes, the pools and the samples
pool/              downloads, gitignored, a folder per source; see "SafeDocs"
```

Every other top-level folder is a category: PDFs gathered for one
purpose: `forms/`, `parsing/`, `conformance/`, `scans/` and `signatures/`
so far. Each has a `manifest.tsv` with a
row for each test file under it and nothing else, paths relative to this
repo; the other files there are upstream's licenses and documentation. A
file two categories want is copied into both; git keeps one copy. Pools
are by source, shared by the categories that sample them. A category
holds at most 1 GB in git, samples and curated files together; `forms/`,
at 2.1 GB, is older than the cap.

Under `forms/`, one folder per issuer, with files named as the issuer names
them, plus the SafeDocs sample:

```
irs/         IRS tax and employment-tax forms
uscis/       Form I-9
va/          Veterans Affairs
uscourts/    US bankruptcy official forms
ny-tax/      New York State Department of Taxation and Finance
ca-ftb/      California Franchise Tax Board
ca-courts/   Judicial Council of California
safedocs/    4,316 forms from the web, named by SafeDocs file number
```

## forms/manifest.tsv

| Column | Meaning |
|---|---|
| `path` | File, relative to this repo |
| `form`, `title` | From the PDF's Title and Subject, filled in from the first page where those are thin; `form` carries the tax year or revision. Empty for SafeDocs files |
| `pages` | Page count |
| `kind` | `acroform`; `hybrid` (AcroForm fields plus an XFA copy of the form); `xfa-only` (XFA with no AcroForm fields); `unparsed` (pypdf can't read it) |
| `fields` | Terminal AcroForm fields |
| `field_types` | `text`, `check`, `radio`, `choice`, `push`, `sig` counts |
| `field_scripts` | Fields with calculate / format / keystroke / validate JavaScript |
| `source` | Where it came from. Year-pinned URLs where the issuer has one (IRS `irs-prior/`, NY and California tax); otherwise the issuer's current link, which moves on to the next revision. For SafeDocs files, the URL it was crawled from in 2021 |
| `license` | Whether it may be redistributed; see below |
| `sha256` | Of the file as retrieved |

Government forms were retrieved 2026-10-14. SafeDocs files were crawled in
July/August 2021 and copied from its archive 2026-10-14.

## parsing/

Files for testing how a reader copes with what's in them: correct files
that are easy to get wrong, and broken ones. Curated sets, each taken
whole from upstream at a pinned version with the licenses and the
documents that say what each file should do, keeping upstream's paths;
and a sample of the web:

```
pdfa-safedocs/     26  PDF Association's SafeDocs targeted files: token syntax, dialects, Unicode passwords
pdf-differences/   14  PDF Association: unknown filters, PDF version from header and catalog
pdf20examples/      7  PDF Association's PDF 2.0 examples
jhove/             88  one broken rule each, with JHOVE's and Acrobat's verdicts (DOI 10.22000/53)
hayro/             88  crashes hayro's fuzzers found
qpdf/             706  qpdf's test files, its .out expectations and .test scripts, and OSS-Fuzz reproducers
pdfium/           802  PDFium's test resources, and the PDFs its .in templates expand to
safedocs/       2,242  a sample of SafeDocs' parse pool, named by file number; see "SafeDocs"
unsafe/         1,359  a sample of UNSAFE-DOCS: Kudu's malformed files and bug-tracker attachments; see "UNSAFE-DOCS"
```

Each curated set's `SOURCE.md` says where it came from, what was taken,
and its license. `tools/curated.py` fetches the sets into `pool/`
and copies them here; it runs PDFium's own `fixup_pdf_template.py` on the
templates. The curated sets come to 61 MB, the SafeDocs sample to 445 MB,
the UNSAFE-DOCS sample to 251 MB: 0.78 GB of the category's 1 GB.
ClamAV (1.5.4, signatures 28160) passes every file in every category;
the two PDFium files it flagged are left out, as its `SOURCE.md` says.

`parsing/manifest.tsv`:

| Column | Meaning |
|---|---|
| `path` | File, relative to this repo |
| `source` | The file upstream, at the pinned commit (or in the DOI's dataset); for SafeDocs files the URL crawled; for UNSAFE-DOCS files the zip at its S3 version, `#` the file's path in it |
| `license` | SPDX, from the set's license; `unknown` for files upstream took from the web |
| `sha256` | Of the file here |
| `expect` | `valid`: upstream says it's correct, so a reader reads all of it. `invalid`: upstream says it breaks the spec, so a reader repairs or refuses it, but never crashes, hangs or runs out of memory. Empty: upstream says neither |
| `password` | The password it's encrypted with, when upstream's documents or tests say: the user password where known, else the owner's, which `note` names. It's text, unnormalised; a reader encodes it as the file's revision has it, PDFDocEncoding to revision 4 and UTF-8 from 5. PDFium and qpdf don't SASLprep a password; `note` says what a reader that does should do |
| `note` | What upstream says about it; for SafeDocs files, pdfinfo's and Tika's errors; for UNSAFE-DOCS files, the malformation or tracker |
| `bucket` | For sampled files, the bucket that took it |
| `pages` | PDFium's page count, else qpdf's |
| `size` … `objstms` | What the bytes show: header version and offset, bytes after the last `%%EOF`, `%%EOF` count, what the last `startxref` points at (`table`, `hybrid`, `stream`, `broken`, `none`), linearization, object streams |
| `filters`, `encryption`, `objects`, `fonts`, `unembedded` | As qpdf reads them: stream filters, encryption method and revision, object count, font dictionaries by subtype, fonts without a font file |
| `qpdf`, `qpdf_pages`, `qpdf_message` | qpdf 12.4.2: `ok`, `warned`, `repaired` (rebuilt the cross-reference table), `password`, `failed`, `crashed`, `timeout`; its first warning or error |
| `pdfium`, `pdfium_pages` | PDFium 156.0.8076.0: `ok`, `password`, its load error (`format`, `file`, `security`, `unknown`), `crashed`, `timeout` |

`tools/inventory.py --manifest` fills the columns from `pages` on,
reading each file in a process of its own so a crash or hang is recorded;
`tools/inventory.py DIR OUT.tsv` does the same for a pool. Its
docstring has the details. qpdf and PDFium are the reference: sheaf's
reader is checked against PDFium, and qpdf repairs the most and says what
it did. On the curated sets they disagree about whether 78 files open and
how many pages 14 have; neither crashes on any.

## conformance/

For checking and making archival and accessible PDFs: PDF/A, PDF/UA,
PDF/X.

```
verapdf/      2,701  veraPDF's test corpus: a folder per profile and clause, -pass- or -fail- in each name
bfo/             33  BFO's PDF/A test suite, with description.txt's reason for each
safedocs/       790  SafeDocs files that claim PDF/A, PDF/UA or PDF/X, or are tagged; see "SafeDocs"
```

veraPDF's Isartor files are left out: Isartor's terms forbid
redistributing them. `conformance/manifest.tsv` has the parsing columns
(`expect` is `pass` or `fail` against `profile`, at `clause`), and
`tools/inventory.py --manifest conformance` adds what each file claims and
holds: `claims` (its XMP's PDF/A, PDF/UA and PDF/X identification),
`marked`, `struct_tree`, `lang`, `output_intents`, `embedded_files`. None of
it is a validator's verdict on the SafeDocs files, only their claims; with
Java installed, veraPDF could add one. It comes to 0.50 GB.

## scans/

For OCR, scan cleanup and finding blank pages: documents scanners made,
with and without a text layer.

```
safedocs/       709  SafeDocs files a scanner or OCR program made, or with almost no text; see "SafeDocs"
```

`tools/inventory.py --manifest scans` adds what PDFium finds in the first
50 pages: `kind` (`scan`, half the pages or more covered by an image;
`scan-ocr`, with invisible text, an OCR layer; `scan-text`, with visible
text; or `text` or `no-text` for files that only looked like scans),
`chars_per_page`, `images_per_page`, `full_page_images`,
`invisible_text_pages`, `image_filters`, `dpi` and `bits`. It comes to
0.59 GB.

## signatures/

For signing and checking signatures.

```
pyhanko/     131  pyHanko's test PDFs: 32 signed (PAdES, timestamps, coverage anomalies, shadow-attack form updates), and the files its signing tests start from
safedocs/    400  signed SafeDocs files; see "SafeDocs"
```

`tools/inventory.py --manifest signatures` adds what pyHanko 0.37.0 makes
of each, with nothing fetched and no trust roots, so `valid` is about the
signature's value, not whether its certificate is trusted: `signatures`,
`subfilters`, `timestamps`, `certified` (the DocMDP level), `dss`,
`intact` (signatures whose digest matches the bytes they cover), `valid`,
`last_coverage` (how much of the file the last one covers),
`empty_fields`. It comes to 0.15 GB.

## Licenses

| `license` | Files | Basis |
|---|---|---|
| `public-domain` | IRS, USCIS, VA, US Courts | Works of the US federal government (17 U.S.C. § 105) |
| `restricted` | NY Tax Department, California courts | NY's [copyright notice](https://www.tax.ny.gov/help/tech/copyright.htm) forbids reproduction without written authorization. The California courts' [terms of use](https://www.courts.ca.gov/11529.htm) allow personal, non-commercial use and forbid redistribution |
| `unstated` | California Franchise Tax Board | No published reuse terms found |
| `unknown` | SafeDocs; UNSAFE-DOCS; two real-world files in `parsing/pdf-differences/` | Web documents, each under its owner's rights; not checked |
| SPDX names | The curated sets in `parsing/`, `conformance/`, `signatures/` | Each set's upstream license, in its folder: Apache-2.0, BSD-3-Clause, CC-BY-3.0, CC-BY-4.0, CC-BY-SA-4.0, MIT, MIT OR Apache-2.0. The CC licenses ask for attribution, which the set's `SOURCE.md` gives |

This repo is private. Before it goes anywhere public, drop the `restricted`
and `unknown` files and decide about the `unstated` one.

## What the forms exercise

Notes for sheaf's M10 (Forms) work.

### Government forms

- **No calculations.** None has a calculate script or a calculation order
  (`/CO`), including two state income tax returns and a bankruptcy "means
  test calculation".
- **Format and keystroke scripts are the common case.** NY IT-201, CA 540,
  bankruptcy form 122A-2 and the I-9 have hundreds between them, all
  Acrobat built-ins: `AFNumber_Format`/`_Keystroke`, `AFDate_FormatEx`/
  `_KeystrokeEx` and `AFSpecial_Format`/`_Keystroke`/`_KeystrokeEx`. The
  I-9 adds 14 validate scripts.
- **Hybrids need care when filling.** Every IRS form but 1040-ES, VA
  21-526EZ and CA FL-100 are LiveCycle Designer output with an AcroForm and
  an XFA copy of the same fields. Acrobat prefers the XFA data when both
  are present, so a filler must either update the XFA `datasets` or drop
  `/XFA` when it writes field values, or Acrobat shows the old values.
- **Hybrids carry scripts sheaf won't run.** IRS forms have three
  document-level `!ADBE::…VersChk` scripts, Adobe's "upgrade your viewer"
  check, which a viewer should not run. Their XFA side has scripts in 41
  forms (738 in all), but nearly all are one leftover checkbox handler that
  calls a `MainCalculation()` no form defines; the rest are input checks
  (Form 2848's required-field prompts, 990-EZ's length limits). VA's XFA
  scripts clear sibling checkboxes; FL-100's open related forms by URL.
- **Field types.** IRS forms have no radio buttons: mutually exclusive
  choices are separate checkboxes. Radio groups are in CA 540 and VA
  21-526EZ, signature fields in VA 21-526EZ, combo boxes in the I-9 and
  122A-2.

### Forms on the web (the SafeDocs pool)

Of the 156,030 pool files pypdf can read:

- **13.5% carry JavaScript** in a field or at document level.
- **2.0% calculate** (3,101 files). 1,749 call `AFSimple_Calculate` and
  1,894 have custom calculate scripts; some have both.
- **Formatting is the bulk of it.** Format scripts are on 9.6% of files,
  keystroke 9.5%, validate 1.2%, document-level JavaScript 4.7%. By files
  using each: `AFDate_FormatEx` 10,376, `AFNumber_Format` 7,712,
  `AFSpecial_Format` 3,684, `AFSimple_Calculate` 1,749, `AFMakeNumber` 967,
  `AFRange_Validate` 891, `AFSpecial_KeystrokeEx` 886, `AFMergeChange` 358,
  `AFTime_*` about 280, `AFParseDateEx` 230, `AFPercent_*` about 175. About
  20 more, mostly date-parsing helpers called from custom scripts, appear
  in under 35 files each.
- **Field types.** Half the files (49.6%) have a signature field. Radio
  groups are in 10.7%, combo or list boxes 4.4%, comb fields 3.1%.
- **XFA is rare.** 2.8% have XFA; 835 files (0.5%) are XFA-only.
- **5.2% are encrypted**, all openable with an empty user password.
- **3.4% set `NeedAppearances`**, asking the viewer to build the field
  appearances itself.

## SafeDocs

[SafeDocs](https://digitalcorpora.org/cc-main-2021-31-pdf-untruncated/) is
about 7.9 million PDFs from Common Crawl's July/August 2021 crawl, refetched
untruncated by NASA JPL for DARPA's SafeDocs program and hosted by Digital
Corpora. Its metadata says, per file, what Apache Tika and poppler's
pdfinfo made of it: whether it has AcroForm fields or XFA, whether either
tool failed and why, encryption, incremental updates. So the form PDFs, and
the ones a reader might trip on, can be picked out without downloading it.
Both kinds share `pool/safedocs/`, which now holds 281,476 files, 164 GB.

Forms:

- `index/safedocs-forms.tsv.gz` lists all of them: 163,352 files, 215 GB.
  Each row has the source URL and host, whether that host is federal,
  language, producer, page count, Tika and pdfinfo flags (fields, XFA,
  JavaScript, signature, encryption), size and sha256.
- `pool/safedocs/<zip>/<file>.pdf` holds the 156,074 of them that are
  5 MB or smaller, 96 GB. It's gitignored.
- `index/safedocs-forms-inventory.tsv.gz` is `tools/forms-inventory.py`
  run over the pool: per file, field counts by type, scripts, calculation order, XFA,
  encryption and the `AF*` functions called. Join it to the index on the
  file name.
- `forms/safedocs/` is a 2.1 GB sample of the pool, in git. It fills
  buckets in order, each capped: custom and `AFSimple_Calculate`
  calculations, calculation order, validate scripts, each `AF*` function,
  document JavaScript, XFA-only and hybrid, each field type,
  `NeedAppearances`, 500+ fields, encryption, files pypdf can't parse, 40
  languages, 60 producers, and federal hosts. Files over 1.5 MB are skipped
  (3 MB for calculation, validate and XFA-only), and each bucket takes
  files in sha256 order, so a rerun picks the same ones.

Parsing:

- `index/safedocs-parse.tsv.gz` lists 362,306 files pdfinfo's or Tika's
  reading puts in a bucket: damage (pdfinfo or Tika failed; pdfinfo's
  stderr names an xref, lexer, stream or structure error, or "May not be a
  PDF file"; a damaged font; the two count pages differently) and features
  (a password, encryption, 10+ updates, linearization and its hints, over
  1,000 pages, PDF 2.0 or an odd version, HTML or Illustrator files). A
  `known-good` bucket takes the files below number 31,000 that
  [pdfa-eng-wds](https://huggingface.co/datasets/pixparse/pdfa-eng-wds)
  kept, at a pinned revision: they opened, rendered fast and had English
  text. Each row has the URL, size, sha256, version, producer, both
  tools' page counts, status and first error, and its buckets.
- Its `pool` column picks 127,919 of them, 80.9 GB, for
  `pool/safedocs/`: every damaged file of 5 MB or less, the first 5,000
  of each feature by file number, and the known-good ones.
- `index/safedocs-parse-inventory.tsv.gz` is `tools/inventory.py`
  run over those: what qpdf and PDFium make of each (see `parsing/`).
  Neither crashes or hangs on any; they disagree about whether 4,356 open
  and how many pages 72 have.
- `parsing/safedocs/` is a 445 MB sample of them, in git. It fills buckets
  in order, each capped: where qpdf and PDFium disagree, both fail, or qpdf
  repairs or warns; the index's damage buckets; each cross-reference
  kind, bytes before the header or after `%%EOF`, 10+ updates,
  linearization, object streams; each encryption method, stream filter,
  font type and header version; the index's rarer features; and 6
  known-good files from each of 40 producers. Files over 500 KB are
  skipped (2 MB for the rarest buckets), in sha256 order as above. It
  leaves the rest of the category's 1 GB for the UNSAFE-DOCS sample.

Conformance, scans and signatures:

- `tools/safedocs-feature-index.py` writes an index for each from the
  same tables. `index/safedocs-conformance.tsv.gz`: files whose XMP claims
  PDF/A (55,097 claim A-1b), PDF/UA (42,540) or PDF/X (75,296 claim
  X-1:2001), a bucket per claim, and tagged files.
  `index/safedocs-scans.tsv.gz`: files a scanner or OCR program made, by
  producer or creator, split by words per page, and others with almost
  no text. `index/safedocs-signatures.tsv.gz`: the 64,891 files Tika finds
  a signature in. Each pools the first files of each bucket by file
  number, up to a cap: 4,440, 7,008 (up to 10 MB a file) and 6,892 files,
  17 GB together.
- `index/safedocs-{conformance,scans,signatures}-inventory.tsv.gz` are
  `tools/inventory.py` over each pool with its category's `--feature`.
  6,282 of the 6,892 pooled signature files are signed: 2,452 PAdES,
  475 document timestamps, 1,361 certified, 1,682 with `/DSS`, about
  1,750 changed after their last signature, and 751 with a signature
  whose digest doesn't match or that pyHanko can't check.
- `tools/safedocs-category-sample.py CATEGORY` samples each into its
  folder, bucket by bucket as for the other samples; its docstring has
  the buckets. Budgets (400 MB, 600 MB, 300 MB) are ceilings, not
  targets.

The tools rebuild each piece:

```sh
tools/safedocs-index.py META_DIR                 # regenerate the forms index (see its docstring)
tools/safedocs-fetch.py                          # fill or resume pool/safedocs (~2.5 h at 10 MB/s)
tools/forms-inventory.py DIR OUT.tsv             # form features of every PDF under DIR
tools/safedocs-sample.py INVENTORY.tsv           # re-pick forms/safedocs and its manifest rows

tools/safedocs-parse-index.py META_DIR           # regenerate the parse index (~7 min)
tools/safedocs-fetch.py --index index/safedocs-parse.tsv.gz      # its pool (~2.4 h)
tools/inventory.py --index index/safedocs-parse.tsv.gz \
    index/safedocs-parse-inventory.tsv.gz        # qpdf and PDFium on it (~17 min)
tools/safedocs-parse-sample.py index/safedocs-parse-inventory.tsv.gz  # re-pick parsing/safedocs

tools/safedocs-feature-index.py META_DIR         # the conformance, scans and signatures indexes (~8 min)
tools/safedocs-fetch.py --index index/safedocs-signatures.tsv.gz       # and conformance;
tools/safedocs-fetch.py --index index/safedocs-scans.tsv.gz --max-size 10000000
tools/inventory.py --index index/safedocs-scans.tsv.gz \
    index/safedocs-scans-inventory.tsv.gz --feature scans               # and the others
tools/safedocs-category-sample.py scans index/safedocs-scans-inventory.tsv.gz
```

`forms-inventory.py` and `inventory.py` run under
[uv](https://docs.astral.sh/uv/), which installs pypdf, and pikepdf,
pypdfium2 and pyHanko, for them; the others need only Python 3. Common Crawl's
[terms](https://commoncrawl.org/terms-of-use) grant nothing for the content
itself.

GitHub rejects pushes over 2 GB, so push `forms/safedocs/` in more than one
go.

## UNSAFE-DOCS

[UNSAFE-DOCS](https://digitalcorpora.org/corpora/file-corpora/unsafe-docs-cc-main-2021-31-unsafe/)
(CC-MAIN-2021-31-UNSAFE) is Digital Corpora's companion to SafeDocs: files
DARPA SafeDocs' performers used to test parsers, in versioned zips on S3.
Two parts of it are here:

- `TA3/`: Kudu Dynamics' test files. `MPG/` has 94 folders, each named
  for the malformation (`malform_xref_delete`) or modification
  (`modifier_pdf_marker_prepend_random_bytes`) made to copies of 6 seed
  files, which are in `MPG/initial_files/`; `26-samples/` has a file per
  feature (each filter, encryption, incremental updates, Type 3 fonts, a
  launch action).
- `issue-trackers/`: 28,406 files attached to 13 projects' bug reports
  (Ghostscript, LibreOffice and OpenOffice, Mozilla and pdf.js, PDFBox,
  PDFium, poppler, qpdf, SumatraPDF, Tika, OCRmyPDF, OpenPDF, POI), with
  a catalog of their libmagic types and sha256s. It's an earlier copy of
  the PDF Association's Stressful PDF Corpus, which adds more trackers but
  ships as whole 2-5 GB tarballs.

The pieces:

- `index/unsafe.tsv.gz` lists the 37,215 files under those two folders:
  each one's zip, the zip's S3 version, its place, size and CRC in the
  zip, and the catalog's type and sha256. Its `pool` column marks MPG/,
  26-samples/ and the bug-tracker files libmagic calls PDF: 30,254 files,
  24 GB, in `pool/unsafe/`, each checked against its CRC and sha256.
- `index/unsafe-clamav.txt` is ClamAV over the pool. It flags 362 files:
  361 Kudu files, every one in 4 folders (hex-encoded names, which its
  obfuscation heuristic dislikes, and oversized trailer `/Size`s, which
  match a CVE-2016-8715 signature) and some in others; and
  `PDFIUM-1198-0.pdf`, a real Adobe Reader exploit (CVE-2009-0927).
- `index/unsafe-inventory.tsv.gz` is `tools/inventory.py` over the
  pool. PDFium never crashes or hangs; qpdf runs past 60 s on one 17 MB
  file. They disagree about whether 274 open and how many pages 197 have.
- `parsing/unsafe/` is a 251 MB sample, in git: every feature sample and
  seed, 4 files from each MPG folder (1 where all of a folder's files are
  over 500 KB), then from the bug trackers what qpdf and PDFium disagree
  about, fail on or repair, 25 from each tracker, and the same feature
  buckets as the SafeDocs sample. Nothing ClamAV flags is taken, nor
  anything without `%PDF-` in its first 1 KB, and the picks are scanned
  again before they're copied. The 4 folders ClamAV flags whole aren't
  sampled.

```sh
tools/unsafe-pool.py index                       # regenerate index/unsafe.tsv.gz (~1 min)
tools/unsafe-pool.py fetch                       # fill or resume pool/unsafe (~25 min)
clamscan -r -i --no-summary pool/unsafe > index/unsafe-clamav.txt
tools/inventory.py pool/unsafe index/unsafe-inventory.tsv.gz   # (~12 min)
tools/unsafe-sample.py index/unsafe-inventory.tsv.gz                   # re-pick parsing/unsafe
```

The corpus says no file in it is known to be malicious, but the PDF
Association warns otherwise, and ClamAV agrees about one. Treat the pool
as hostile: the tools read it as bytes, and the inventory opens files only
in the processes it starts for them.

## Using it from sheaf

sheaf's corpus tests (`sheaf_doc::forms::corpus`) look in `$SHEAF_CORPUS`,
then `../pdf-corpus`, for `forms/manifest.tsv`. They skip when neither
folder exists, and fail when one exists without it. Nothing in sheaf reads
the other categories' manifests yet.
