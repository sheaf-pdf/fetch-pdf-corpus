//! Lock files: `locks/<collection>.tsv`, a row per file of the corpus
//! saying what it is (path, sha256, size) and where its bytes are.
//!
//! Columns: `path sha256 size kind url member offset length codec`.
//!
//! | kind | the file is |
//! |---|---|
//! | `repo` | `corpus/<path>` in this repository |
//! | `get` | `url`'s body; `codec` `base64` if it's base64 text of the file |
//! | `zip` | the `length` bytes at `offset` in `url`, a zip member's data, `codec` `stored` or `deflate`; `member` is its name, for reading |
//! | `tar` | `member` of the tar archive at `url`, gzipped or not |
//! | `pdfium-template` | PDFium's `fixup_pdf_template.py` run on `member` of the tar archive at `url` |
//!
//! A path on more than one row has more than one source, tried in order.

use std::fmt::Write as _;
use std::path::Path;

use anyhow::{Context, Result, bail};

#[derive(Clone, Debug, PartialEq)]
pub enum Source {
    Repo,
    Get {
        url: String,
        base64: bool,
    },
    Zip {
        url: String,
        member: String,
        offset: u64,
        length: u64,
        deflate: bool,
    },
    Tar {
        url: String,
        member: String,
    },
    PdfiumTemplate {
        url: String,
        member: String,
    },
}

#[derive(Clone, Debug)]
pub struct Row {
    pub path: String,
    pub sha256: String,
    pub size: u64,
    pub source: Source,
}

const HEADER: &str = "path\tsha256\tsize\tkind\turl\tmember\toffset\tlength\tcodec";

/// The collections, in fetch order, and whether a plain `fetch` takes them.
pub const COLLECTIONS: [(&str, bool); 7] = [
    ("meta", true),
    ("forms", true),
    ("scans", true),
    ("signatures", true),
    ("conformance", true),
    ("parsing", true),
    ("pool", false),
];

pub fn read(path: &Path) -> Result<Vec<Row>> {
    let text = std::fs::read_to_string(path).with_context(|| format!("{}", path.display()))?;
    let mut lines = text.lines();
    if lines.next() != Some(HEADER) {
        bail!(
            "{}: not a lock file (its header isn't `{HEADER}`)",
            path.display()
        );
    }
    lines
        .enumerate()
        .map(|(i, line)| parse(line).with_context(|| format!("{} line {}", path.display(), i + 2)))
        .collect()
}

fn parse(line: &str) -> Result<Row> {
    let f: Vec<&str> = line.split('\t').collect();
    let [path, sha256, size, kind, url, member, offset, length, codec] = f[..] else {
        bail!("{} columns, not 9", f.len());
    };
    let (url, member) = (url.to_string(), member.to_string());
    let source = match kind {
        "repo" => Source::Repo,
        "get" => Source::Get {
            url,
            base64: codec == "base64",
        },
        "zip" => Source::Zip {
            url,
            member,
            offset: offset.parse()?,
            length: length.parse()?,
            deflate: match codec {
                "deflate" => true,
                "stored" => false,
                _ => bail!("zip codec `{codec}`"),
            },
        },
        "tar" => Source::Tar { url, member },
        "pdfium-template" => Source::PdfiumTemplate { url, member },
        _ => bail!("kind `{kind}`"),
    };
    if sha256.len() != 64 || !sha256.bytes().all(|b| b.is_ascii_hexdigit()) {
        bail!("sha256 `{sha256}`");
    }
    Ok(Row {
        path: path.to_string(),
        sha256: sha256.to_string(),
        size: size.parse()?,
        source,
    })
}

pub fn write(path: &Path, rows: &[Row]) -> Result<()> {
    let mut text = String::from(HEADER);
    text.push('\n');
    for row in rows {
        let (kind, url, member, offset, length, codec) = match &row.source {
            Source::Repo => ("repo", "", "", String::new(), String::new(), ""),
            Source::Get { url, base64 } => (
                "get",
                url.as_str(),
                "",
                String::new(),
                String::new(),
                if *base64 { "base64" } else { "" },
            ),
            Source::Zip {
                url,
                member,
                offset,
                length,
                deflate,
            } => (
                "zip",
                url.as_str(),
                member.as_str(),
                offset.to_string(),
                length.to_string(),
                if *deflate { "deflate" } else { "stored" },
            ),
            Source::Tar { url, member } => (
                "tar",
                url.as_str(),
                member.as_str(),
                String::new(),
                String::new(),
                "",
            ),
            Source::PdfiumTemplate { url, member } => (
                "pdfium-template",
                url.as_str(),
                member.as_str(),
                String::new(),
                String::new(),
                "",
            ),
        };
        for field in [row.path.as_str(), url, member] {
            if field.contains(['\t', '\n', '\r']) {
                bail!("{field:?} can't go in a TSV field");
            }
        }
        writeln!(
            text,
            "{}\t{}\t{}\t{kind}\t{url}\t{member}\t{offset}\t{length}\t{codec}",
            row.path, row.sha256, row.size
        )?;
    }
    std::fs::write(path, text).with_context(|| format!("{}", path.display()))
}
