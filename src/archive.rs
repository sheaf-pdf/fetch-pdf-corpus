//! Tar archives, gzipped or not, read as they download.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read};

use anyhow::{Context, Result};

use crate::http::Http;

/// Each regular file of the archive at `url`, with its member name.
pub fn each(
    http: &Http,
    url: &str,
    mut each: impl FnMut(&str, &mut dyn Read) -> Result<()>,
) -> Result<()> {
    let mut reader = BufReader::with_capacity(1 << 20, http.open(url)?);
    let gzipped = reader.fill_buf()?.starts_with(&[0x1f, 0x8b]);
    let inner: Box<dyn Read> = if gzipped {
        Box::new(flate2::read::GzDecoder::new(reader))
    } else {
        Box::new(reader)
    };
    let mut archive = tar::Archive::new(inner);
    for entry in archive
        .entries()
        .with_context(|| format!("reading {url}"))?
    {
        let mut entry = entry.with_context(|| format!("reading {url}"))?;
        if !entry.header().entry_type().is_file() {
            continue;
        }
        let name = entry.path()?.to_string_lossy().into_owned();
        each(&name, &mut entry).with_context(|| format!("{url}: {name}"))?;
    }
    Ok(())
}

/// Every regular file of the archive at `url`, by member name.
pub fn all(http: &Http, url: &str) -> Result<HashMap<String, Vec<u8>>> {
    let mut files = HashMap::new();
    each(http, url, |name, data| {
        let mut bytes = Vec::new();
        data.read_to_end(&mut bytes)?;
        files.insert(name.to_string(), bytes);
        Ok(())
    })?;
    Ok(files)
}

/// The member of the archive at `url` named `member`.
pub fn member(http: &Http, url: &str, member: &str) -> Result<Vec<u8>> {
    let mut found = None;
    each(http, url, |name, data| {
        if found.is_none() && name == member {
            let mut bytes = Vec::new();
            data.read_to_end(&mut bytes)?;
            found = Some(bytes);
        }
        Ok(())
    })?;
    found.with_context(|| format!("{url} has no {member}"))
}
