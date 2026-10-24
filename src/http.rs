//! HTTP with retries: whole bodies, byte ranges and streams. Bodies are
//! the server's bytes, never transparently decompressed.

use std::io::Read;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use ureq::http::StatusCode;

const TRIES: u32 = 5;

#[derive(Clone)]
pub struct Http {
    agent: ureq::Agent,
}

impl Http {
    pub fn new() -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_connect(Some(Duration::from_secs(30)))
            .timeout_recv_response(Some(Duration::from_secs(120)))
            .user_agent(concat!(
                "fetch-pdf-corpus/",
                env!("CARGO_PKG_VERSION"),
                " (+https://github.com/sheaf-pdf/fetch-pdf-corpus)"
            ))
            .build();
        Http {
            agent: config.into(),
        }
    }

    /// `url`'s body.
    pub fn get(&self, url: &str) -> Result<Vec<u8>> {
        self.retry(url, || {
            let mut bytes = Vec::new();
            (self.agent.get(url).call()?.into_body().into_reader()).read_to_end(&mut bytes)?;
            Ok(bytes)
        })
    }

    /// `length` bytes of `url` from `start`.
    pub fn range(&self, url: &str, start: u64, length: u64) -> Result<Vec<u8>> {
        if length == 0 {
            return Ok(Vec::new());
        }
        self.retry(url, || {
            let response = self
                .agent
                .get(url)
                .header("Range", format!("bytes={start}-{}", start + length - 1))
                .call()?;
            // A server that ignores the range would send the whole file.
            if response.status() != StatusCode::PARTIAL_CONTENT {
                bail!("asked for a range, got {}", response.status());
            }
            let mut bytes = Vec::with_capacity(length as usize);
            response
                .into_body()
                .into_reader()
                .take(length + 1)
                .read_to_end(&mut bytes)?;
            if bytes.len() as u64 != length {
                bail!("asked for {length} bytes from {start}, got {}", bytes.len());
            }
            Ok(bytes)
        })
    }

    /// `url`'s size, from a one-byte range's `Content-Range`.
    pub fn size(&self, url: &str) -> Result<u64> {
        self.retry(url, || {
            let response = self.agent.get(url).header("Range", "bytes=0-0").call()?;
            let range = response
                .headers()
                .get("content-range")
                .context("no Content-Range")?
                .to_str()?;
            let total = range
                .rsplit('/')
                .next()
                .context("no total in Content-Range")?;
            Ok(total.parse()?)
        })
    }

    /// `url`'s body as a stream. A failure part way isn't retried here.
    pub fn open(&self, url: &str) -> Result<impl Read + use<>> {
        self.retry(url, || {
            Ok(self.agent.get(url).call()?.into_body().into_reader())
        })
    }

    fn retry<T>(&self, url: &str, mut attempt: impl FnMut() -> Result<T>) -> Result<T> {
        let mut tries = 0;
        loop {
            tries += 1;
            match attempt() {
                Ok(value) => return Ok(value),
                Err(e) if tries < TRIES && transient(&e) => {
                    std::thread::sleep(Duration::from_secs(1 << (tries - 1)));
                }
                Err(e) => return Err(e.context(format!("GET {url}"))),
            }
        }
    }
}

/// Whether trying again might work: not a 4xx other than 408 or 429.
fn transient(e: &anyhow::Error) -> bool {
    match e.downcast_ref::<ureq::Error>() {
        Some(ureq::Error::StatusCode(code)) => *code >= 500 || *code == 408 || *code == 429,
        _ => true,
    }
}
