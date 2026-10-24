//! What a remote zip holds, read with range requests: its central
//! directory, and where each member's data starts. Only `lock` reads
//! these; a lock row already says which bytes to fetch.

use std::collections::HashMap;
use std::io::Read;

use anyhow::{Context, Result, bail};

use crate::http::Http;

pub struct Member {
    pub name: String,
    pub crc32: u32,
    pub compressed: u64,
    pub size: u64,
    pub method: u16,
    pub header_offset: u64,
}

pub const STORED: u16 = 0;
pub const DEFLATED: u16 = 8;

fn u16_at(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}

fn u32_at(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

fn u64_at(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

/// Every member of the zip at `url`, by name.
pub fn directory(http: &Http, url: &str) -> Result<HashMap<String, Member>> {
    let size = http.size(url)?;
    // The end record, its comment, and a Zip64 locator and record before it.
    let tail_len = size.min(22 + 65535 + 20 + 56);
    let tail_start = size - tail_len;
    let tail = http.range(url, tail_start, tail_len)?;
    let eocd = (0..=tail.len() - 22)
        .rev()
        .find(|&i| tail[i..i + 4] == [0x50, 0x4b, 0x05, 0x06])
        .context("no end of central directory record")?;
    let mut entries = u64::from(u16_at(&tail, eocd + 10));
    let mut cd_size = u64::from(u32_at(&tail, eocd + 12));
    let mut cd_offset = u64::from(u32_at(&tail, eocd + 16));
    if entries == 0xffff || cd_size == 0xffff_ffff || cd_offset == 0xffff_ffff {
        let locator = eocd.checked_sub(20).context("no Zip64 locator")?;
        if tail[locator..locator + 4] != [0x50, 0x4b, 0x06, 0x07] {
            bail!("no Zip64 locator");
        }
        let record_at = u64_at(&tail, locator + 8);
        let record = http.range(url, record_at, 56)?;
        if record[..4] != [0x50, 0x4b, 0x06, 0x06] {
            bail!("no Zip64 end of central directory record");
        }
        entries = u64_at(&record, 32);
        cd_size = u64_at(&record, 40);
        cd_offset = u64_at(&record, 48);
    }
    let cd = http.range(url, cd_offset, cd_size)?;
    let mut members = HashMap::new();
    let mut at = 0;
    for _ in 0..entries {
        if cd.get(at..at + 4) != Some(&[0x50, 0x4b, 0x01, 0x02]) {
            bail!("a central directory entry without its signature at {at}");
        }
        let name_len = usize::from(u16_at(&cd, at + 28));
        let extra_len = usize::from(u16_at(&cd, at + 30));
        let comment_len = usize::from(u16_at(&cd, at + 32));
        let name_bytes = &cd[at + 46..at + 46 + name_len];
        let extra = &cd[at + 46 + name_len..at + 46 + name_len + extra_len];
        let mut member = Member {
            name: String::from_utf8_lossy(name_bytes).into_owned(),
            crc32: u32_at(&cd, at + 16),
            compressed: u64::from(u32_at(&cd, at + 20)),
            size: u64::from(u32_at(&cd, at + 24)),
            method: u16_at(&cd, at + 10),
            header_offset: u64::from(u32_at(&cd, at + 42)),
        };
        zip64(&mut member, extra);
        members.insert(member.name.clone(), member);
        at += 46 + name_len + extra_len + comment_len;
    }
    Ok(members)
}

/// The Zip64 extra field's values, for those the entry marks as in it.
fn zip64(member: &mut Member, mut extra: &[u8]) {
    while extra.len() >= 4 {
        let (id, len) = (u16_at(extra, 0), usize::from(u16_at(extra, 2)));
        let Some(mut data) = extra.get(4..4 + len) else {
            return;
        };
        if id == 1 {
            for field in [
                &mut member.size,
                &mut member.compressed,
                &mut member.header_offset,
            ] {
                if *field == 0xffff_ffff && data.len() >= 8 {
                    *field = u64_at(data, 0);
                    data = &data[8..];
                }
            }
            return;
        }
        extra = &extra[4 + len..];
    }
}

/// Where `member`'s data starts: past its local header, whose name and
/// extra field needn't be the central directory's.
pub fn data_offset(http: &Http, url: &str, member: &Member) -> Result<u64> {
    let header = http.range(url, member.header_offset, 30)?;
    if header[..4] != [0x50, 0x4b, 0x03, 0x04] {
        bail!(
            "{}: no local header at {}",
            member.name,
            member.header_offset
        );
    }
    let name_len = u64::from(u16_at(&header, 26));
    let extra_len = u64::from(u16_at(&header, 28));
    Ok(member.header_offset + 30 + name_len + extra_len)
}

/// A member's data as stored, made the file it holds.
pub fn unpack(raw: Vec<u8>, deflated: bool, size: u64) -> Result<Vec<u8>> {
    if !deflated {
        return Ok(raw);
    }
    let mut out = Vec::with_capacity(size as usize);
    flate2::read::DeflateDecoder::new(&raw[..])
        .take(size + 1)
        .read_to_end(&mut out)?;
    Ok(out)
}
