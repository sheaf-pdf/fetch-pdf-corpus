# fetch-pdf-corpus

Rebuilds a corpus of about 16,000 real-world and test-suite PDFs, byte
for byte, from where each file was published. This repository holds no
PDFs: it holds where every file comes from, its size and its sha256, and
the corpus's own manifests, which say what each file is and what other
tools make of it (pypdf, pyHanko, qpdf, PDFium). A file whose bytes
don't match its sha256 isn't written.

It's the corpus [sheaf](https://github.com/sheaf-pdf/sheaf) tests
against: a fetched folder is what sheaf's corpus tests read from
`$SHEAF_CORPUS` or `../pdf-corpus`.

## Fetching

```sh
cargo install --locked fetch-pdf-corpus
# or from the tagged source:
cargo install --locked --git https://github.com/sheaf-pdf/fetch-pdf-corpus --tag v0.1.1

fetch-pdf-corpus fetch --out pdf-corpus             # every collection but pool
fetch-pdf-corpus fetch --out pdf-corpus forms scans # some of them
fetch-pdf-corpus verify --dir pdf-corpus            # check a folder
```

It needs Rust 1.88 or later. The locks are built into the binary;
the few files this repository holds itself (`corpus/`) come from GitHub
at the binary's version tag. Files already there and right are kept, so
a rerun resumes. `--jobs` sets how many files are fetched at once (16).
From a checkout, `cargo run --release -- --data . fetch …` uses its
`locks/` and `corpus/` instead.

For sheaf, fetch into `pdf-corpus` beside your sheaf checkout, or point
`$SHEAF_CORPUS` at it. `parsing/unsafe/` holds files from UNSAFE-DOCS,
made to break parsers: read them as bytes, or in a process you can lose.

| Collection | Files | Size | What |
|---|---:|---:|---|
| `meta` | 27 | 139 MB | The corpus's README, its curation tools, and `index/` (what each pool holds, and inventories of it) |
| `forms` | 4,393 | 2.1 GB | Government forms, and a 4,316-file sample of forms on the web |
| `scans` | 710 | 590 MB | Documents scanners and OCR programs made |
| `signatures` | 534 | 151 MB | Signed documents: pyHanko's test files and web documents |
| `conformance` | 3,530 | 498 MB | PDF/A, PDF/UA and PDF/X: veraPDF's and BFO's test suites, and files that claim them |
| `parsing` | 6,708 | 783 MB | For testing readers: curated suites (qpdf, PDFium, hayro, JHOVE, the PDF Association's) and broken files from the web |
| `pool` | 568 | 1.9 GB | Not fetched unless named: the files of the corpus's download pool that sheaf's tests read (`lists/sheaf-pool.txt`) |

The corpus's own `README.md` (in `meta`) says what's in each and how it
was picked.

## Where the files come from

| Source | Files | How |
|---|---:|---|
| [SafeDocs](https://digitalcorpora.org/cc-main-2021-31-pdf-untruncated/) (CC-MAIN-2021-31-PDF-UNTRUNCATED), Digital Corpora | 9,025 | One range request each, into SafeDocs' zips on S3 |
| [UNSAFE-DOCS](https://digitalcorpora.org/corpora/file-corpora/unsafe-docs-cc-main-2021-31-unsafe/), Digital Corpora | 1,359 | The same, into versioned zips |
| GitHub repositories at pinned commits | 4,495 | Each repository's tarball, read once |
| PDFium at a pinned commit | 923 | Its `testing/resources` tarball, and its `LICENSE` |
| PDFium's templates | 460 | `fixup_pdf_template.py`'s expansion of a `.in`, ported to Rust (`src/pdfium.rs`) |
| JHOVE's test set, DOI 10.22000/53 | 90 | Its archive at TIB's RADAR |
| The issuers (IRS, USCIS, VA, US Courts, NY, California) | 76 | Their URLs: year-pinned where the issuer has one |
| This repository's `corpus/` | 42 | Manifests, the curated sets' `SOURCE.md`, the curation tools, `index/` |

Government forms and web documents keep their owners' rights; the
corpus README's "Licenses" section says what's known about each.
Fetching them yourself redistributes nothing. An issuer that replaces a
form at the same URL breaks that file's fetch, and `fetch` says so: the
corpus is frozen, so a new revision is a new file.

## Locks

`locks/<collection>.tsv` has a row per file:

| Column | |
|---|---|
| `path` | Where it goes, relative to the corpus folder |
| `sha256`, `size` | What it is |
| `kind` | `repo`: `corpus/<path>` here. `get`: `url`'s body (`codec` `base64`: base64 text of it). `zip`: the `length` bytes at `offset` in `url`, a member's data, `codec` `stored` or `deflate`. `tar`: `member` of the tar archive at `url`, gzipped or not. `pdfium-template`: `member` of that archive, expanded |
| `url`, `member`, `offset`, `length`, `codec` | As `kind` says |

So a fetcher in any language is short: a range request, maybe inflate,
check the sha256. A path on more than one row has more than one source,
tried in order: room for mirrors.

## Rebuilding the locks

For the corpus's keepers, from a local copy of it:

```sh
cargo run --release -- --data . lock --from ../pdf-corpus \
    --pool-list lists/sheaf-pool.txt meta forms scans signatures conformance parsing pool
```

Each collection's files are listed, each one's source found (SafeDocs
files by number; UNSAFE-DOCS and government forms from the manifests'
`source`; the curated sets from `src/sets.rs`, which follows the
corpus's `tools/curated.py`), and checked against the local file before
its row is written: zip members by size and CRC-32 from the zip's
directory, everything else by downloading it. `corpus/` is rewritten
with the collection's `repo` files. A file with no source is reported
and left out.

`lists/sheaf-pool.txt` is what sheaf's tests pick from a full pool:

```sh
cargo run --release -- pool-picks --from ../pdf-corpus \
    --sample ../sheaf/bench/pool-sample.txt > lists/sheaf-pool.txt
```

## Releasing

An installed binary takes `corpus/` from GitHub at the tag of its own
version, so a change to `locks/` or `corpus/` needs a new version: bump
it in `Cargo.toml`, commit, tag the commit `v<version>`, and push the
tag before publishing.
