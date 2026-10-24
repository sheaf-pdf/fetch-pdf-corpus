//! Where the locks and this repository's own files come from: a
//! checkout of it, or, for an installed binary, the locks built in and
//! the files from GitHub at this version's tag.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::http::Http;
use crate::lock::{self, Row};

/// `corpus/` at this version's tag. Each version's tag is the commit
/// whose locks it was built with, so the files match their rows.
const REPO_FILES: &str = concat!(
    "https://raw.githubusercontent.com/sheaf-pdf/fetch-pdf-corpus/v",
    env!("CARGO_PKG_VERSION"),
    "/corpus/"
);

const BUILT_IN: [(&str, &str); 7] = [
    ("meta", include_str!("../locks/meta.tsv")),
    ("forms", include_str!("../locks/forms.tsv")),
    ("scans", include_str!("../locks/scans.tsv")),
    ("signatures", include_str!("../locks/signatures.tsv")),
    ("conformance", include_str!("../locks/conformance.tsv")),
    ("parsing", include_str!("../locks/parsing.tsv")),
    ("pool", include_str!("../locks/pool.tsv")),
];

pub enum Data {
    /// A checkout of this repository: its `locks/` and `corpus/`.
    Checkout(PathBuf),
    /// The locks this binary was built with.
    BuiltIn,
}

impl Data {
    pub fn rows(&self, collection: &str) -> Result<Vec<Row>> {
        match self {
            Data::Checkout(dir) => {
                let path = dir.join("locks").join(format!("{collection}.tsv"));
                let text = std::fs::read_to_string(&path)
                    .with_context(|| format!("{}", path.display()))?;
                lock::parse_text(&path.display().to_string(), &text)
            }
            Data::BuiltIn => {
                let (_, text) = BUILT_IN
                    .iter()
                    .find(|(name, _)| *name == collection)
                    .with_context(|| format!("no built-in lock for {collection}"))?;
                lock::parse_text(&format!("the built-in {collection} lock"), text)
            }
        }
    }

    /// A `repo` row's file.
    pub fn repo_file(&self, http: &Http, path: &str) -> Result<Vec<u8>> {
        match self {
            Data::Checkout(dir) => {
                let file = dir.join("corpus").join(path);
                std::fs::read(&file).with_context(|| format!("{}", file.display()))
            }
            Data::BuiltIn => http.get(&format!("{REPO_FILES}{}", encode(path))),
        }
    }

    /// The checkout, for commands that write to it.
    pub fn checkout(&self) -> Option<&Path> {
        match self {
            Data::Checkout(dir) => Some(dir),
            Data::BuiltIn => None,
        }
    }
}

/// `path` for a URL: each byte but unreserved ones and `/` percent-encoded.
fn encode(path: &str) -> String {
    let mut out = String::new();
    for b in path.bytes() {
        if b.is_ascii_alphanumeric() || b"-._~/".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

#[test]
fn every_collection_has_a_built_in_lock() {
    for (collection, _) in lock::COLLECTIONS {
        assert!(Data::BuiltIn.rows(collection).is_ok(), "{collection}");
    }
    assert_eq!(encode("parsing/a b/SOURCE.md"), "parsing/a%20b/SOURCE.md");
}
