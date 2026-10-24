//! `pool-picks`: the pool files sheaf's corpus tests read, for
//! `lists/sheaf-pool.txt`.
//!
//! sheaf's realize_corpus test (`crates/sheaf-ops/tests/realize_corpus.rs`,
//! `files`) reads the pool sample; the inventory's first 75 encrypted
//! rows; and a top-up of 50 files with bytes before `%PDF-` and 50 whose
//! `startxref` leads nowhere, walking the inventory in order. The lexer
//! test (`crates/sheaf-render/tests/content_lexer.rs`) reads the sample
//! only. This makes the same picks over a full pool. A file that doesn't
//! count toward a top-up can be missing without changing what the walk
//! picks, so a pool of just these gives sheaf the same files. `head` and
//! `leads_nowhere` are realize_corpus's own.

use std::collections::{BTreeSet, HashSet};
use std::io::{BufRead, BufReader, Read};
use std::path::Path;

use anyhow::{Context, Result};

const ENCRYPTED_ROWS: usize = 75;
const TOP_UP: usize = 50;

/// The picks, sorted, from the corpus at `root` and sheaf's
/// `bench/pool-sample.txt`.
pub fn pool_picks(root: &Path, sample: &Path) -> Result<Vec<String>> {
    let sample: Vec<String> = std::fs::read_to_string(sample)
        .with_context(|| format!("{}", sample.display()))?
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(String::from)
        .collect();
    let rows = inventory(root)?;
    let mut picks: BTreeSet<String> = sample
        .iter()
        .filter(|p| p.starts_with("pool/"))
        .cloned()
        .collect();
    // All of the first encrypted rows: which the test takes depends on
    // each one's encryption, which it reads by opening the file.
    picks.extend(
        rows.iter()
            .filter(|(_, e)| *e)
            .take(ENCRYPTED_ROWS)
            .map(|(p, _)| p.clone()),
    );
    let sampled: HashSet<&String> = sample.iter().collect();
    let (mut junk, mut damaged) = (0, 0);
    for (path, _) in &rows {
        if junk >= TOP_UP && damaged >= TOP_UP {
            break;
        }
        if sampled.contains(path) {
            continue;
        }
        let at = root.join(path);
        let Some(head) = head(&at) else {
            continue;
        };
        if junk < TOP_UP && find(&head, b"%PDF-").is_some_and(|i| i > 0) {
            junk += 1;
            picks.insert(path.clone());
        }
        if damaged < TOP_UP && leads_nowhere(&at) {
            damaged += 1;
            picks.insert(path.clone());
        }
    }
    Ok(picks.into_iter().collect())
}

/// The pool's inventory, a row a file: its path and whether it's
/// encrypted, in order.
fn inventory(root: &Path) -> Result<Vec<(String, bool)>> {
    let path = root.join("index/safedocs-forms-inventory.tsv.gz");
    let file = std::fs::File::open(&path).with_context(|| format!("{}", path.display()))?;
    let mut lines = BufReader::new(flate2::read::GzDecoder::new(file)).lines();
    let header = lines.next().context("an empty inventory")??;
    let column = |name: &str| {
        header
            .split('\t')
            .position(|c| c == name)
            .with_context(|| format!("no {name} column"))
    };
    let (path_at, encrypted_at) = (column("file")?, column("encrypted")?);
    lines
        .map(|line| {
            let line = line?;
            let cols: Vec<&str> = line.split('\t').collect();
            Ok((cols[path_at].to_string(), cols[encrypted_at] == "1"))
        })
        .collect()
}

/// The first kilobyte of the file at `path`.
fn head(path: &Path) -> Option<Vec<u8>> {
    let mut head = Vec::new();
    let file = std::fs::File::open(path).ok()?;
    file.take(1024).read_to_end(&mut head).ok()?;
    Some(head)
}

/// Whether the file at `path`'s `startxref` leads to no cross-reference
/// section: neither `xref` nor an object is where it says, counted from
/// the file's start or its header's.
fn leads_nowhere(path: &Path) -> bool {
    let Ok(bytes) = std::fs::read(path) else {
        return false;
    };
    let header = find(&bytes[..bytes.len().min(1024)], b"%PDF-").unwrap_or(0);
    let tail = bytes.len().saturating_sub(2048);
    let Some(at) = rfind(&bytes[tail..], b"startxref").map(|i| tail + i + 9) else {
        return true;
    };
    let digits: String = (bytes[at..].iter())
        .skip_while(|b| b.is_ascii_whitespace())
        .take_while(|b| b.is_ascii_digit())
        .map(|&b| b as char)
        .collect();
    let Ok(start) = digits.parse::<usize>() else {
        return true;
    };
    let object = regex::bytes::Regex::new(r"^\d+\s+\d+\s+obj").unwrap();
    let section = |pos: usize| {
        let Some(rest) = bytes.get(pos..) else {
            return false;
        };
        let rest = &rest[..rest.len().min(64)];
        rest.starts_with(b"xref") || object.is_match(rest)
    };
    !section(start) && !section(start + header)
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

fn rfind(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).rposition(|w| w == needle)
}
