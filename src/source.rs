//! A file's bytes from one of its sources, and checking them.

use std::path::Path;

use anyhow::{Context, Result, bail};
use base64::Engine as _;
use sha2::{Digest, Sha256};

use crate::http::Http;
use crate::lock::{Row, Source};
use crate::{archive, pdfium, zip};

pub fn sha256(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// The bytes `row`'s source gives. An archive is read whole for its one
/// member; `fetch` batches a set's members to read each archive once.
pub fn bytes(http: &Http, data: &Path, row: &Row) -> Result<Vec<u8>> {
    match &row.source {
        Source::Repo => {
            let path = data.join("corpus").join(&row.path);
            std::fs::read(&path).with_context(|| format!("{}", path.display()))
        }
        Source::Get { url, base64 } => {
            let body = http.get(url)?;
            if *base64 {
                decode_base64(&body)
            } else {
                Ok(body)
            }
        }
        Source::Zip {
            url,
            offset,
            length,
            deflate,
            ..
        } => zip::unpack(http.range(url, *offset, *length)?, *deflate, row.size),
        Source::Tar { url, member } => archive::member(http, url, member),
        Source::PdfiumTemplate { url, member } => {
            let files = normalized(archive::all(http, url)?);
            pdfium::expand(member, &files)
        }
    }
}

/// gitiles' `?format=TEXT`: base64, maybe wrapped.
pub fn decode_base64(text: &[u8]) -> Result<Vec<u8>> {
    let text: Vec<u8> = text
        .iter()
        .copied()
        .filter(|b| !b.is_ascii_whitespace())
        .collect();
    Ok(base64::engine::general_purpose::STANDARD.decode(text)?)
}

/// An archive's files keyed by normalized path, as templates include them.
pub fn normalized(
    files: std::collections::HashMap<String, Vec<u8>>,
) -> std::collections::HashMap<String, Vec<u8>> {
    files
        .into_iter()
        .map(|(name, bytes)| (pdfium::normpath(&name), bytes))
        .collect()
}

/// That `bytes` are `row`'s file.
pub fn check(row: &Row, bytes: &[u8]) -> Result<()> {
    if bytes.len() as u64 != row.size {
        bail!(
            "{} bytes, not {}: the source has changed",
            bytes.len(),
            row.size
        );
    }
    let got = sha256(bytes);
    if got != row.sha256 {
        bail!("sha256 {got}, not {}: the source has changed", row.sha256);
    }
    Ok(())
}
