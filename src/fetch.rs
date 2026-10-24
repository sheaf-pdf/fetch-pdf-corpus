//! `fetch` and `verify`: a folder made, or checked, from the locks.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;

use crate::http::Http;
use crate::lock::{self, Row, Source};
use crate::source::{self, sha256};
use crate::{archive, pdfium};

/// The rows of `collections`' locks, grouped by path, sources in order.
pub fn load(data: &Path, collections: &[String]) -> Result<BTreeMap<String, Vec<Row>>> {
    let mut by_path: BTreeMap<String, Vec<Row>> = BTreeMap::new();
    for collection in collections {
        for row in lock::read(&data.join("locks").join(format!("{collection}.tsv")))? {
            by_path.entry(row.path.clone()).or_default().push(row);
        }
    }
    for (path, rows) in &by_path {
        if rows
            .iter()
            .any(|r| r.sha256 != rows[0].sha256 || r.size != rows[0].size)
        {
            bail!("{path}: its rows disagree about what the file is");
        }
    }
    Ok(by_path)
}

/// Whether `dir/path` is already the file `row` describes.
fn present(dir: &Path, row: &Row) -> bool {
    let path = dir.join(&row.path);
    match std::fs::metadata(&path) {
        Ok(meta) if meta.len() == row.size => {
            std::fs::read(&path).is_ok_and(|bytes| sha256(&bytes) == row.sha256)
        }
        _ => false,
    }
}

/// One file's content, wanted at one or more paths.
struct Content {
    paths: Vec<String>,
    /// Every source of every path, first ones first.
    sources: Vec<Row>,
}

impl Content {
    fn row(&self) -> &Row {
        &self.sources[0]
    }
}

struct Progress {
    files: AtomicU64,
    bytes: AtomicU64,
    failed: Mutex<Vec<(String, String)>>,
}

fn write(out: &Path, path: &str, bytes: &[u8]) -> Result<()> {
    let target = out.join(path);
    let parent = target.parent().context("a path with no folder")?;
    std::fs::create_dir_all(parent)?;
    let part = PathBuf::from(format!("{}.part", target.display()));
    std::fs::write(&part, bytes)?;
    std::fs::rename(&part, &target)?;
    Ok(())
}

/// Checks `bytes` against `content` and writes them at each of its paths.
fn deliver(out: &Path, content: &Content, bytes: &[u8], progress: &Progress) -> Result<()> {
    source::check(content.row(), bytes)?;
    for path in &content.paths {
        write(out, path, bytes).with_context(|| format!("writing {path}"))?;
    }
    progress
        .files
        .fetch_add(content.paths.len() as u64, Ordering::Relaxed);
    progress
        .bytes
        .fetch_add(bytes.len() as u64, Ordering::Relaxed);
    Ok(())
}

/// `content` from its sources after the first, which failed with `first`.
fn fall_back(
    http: &Http,
    data: &Path,
    out: &Path,
    content: &Content,
    first: anyhow::Error,
    progress: &Progress,
) {
    let mut errors = vec![format!("{first:#}")];
    for row in &content.sources[1..] {
        match source::bytes(http, data, row).and_then(|b| deliver(out, content, &b, progress)) {
            Ok(()) => return,
            Err(e) => errors.push(format!("{e:#}")),
        }
    }
    let mut failed = progress.failed.lock().unwrap();
    for path in &content.paths {
        failed.push((path.clone(), errors.join("; then ")));
    }
}

pub fn fetch(data: &Path, out: &Path, collections: &[String], jobs: usize) -> Result<bool> {
    let by_path = load(data, collections)?;
    let total = by_path.len();
    let pool = rayon::ThreadPoolBuilder::new().num_threads(jobs).build()?;
    let todo: Vec<&Vec<Row>> = pool.install(|| {
        by_path
            .par_iter()
            .filter(|(_, rows)| !present(out, &rows[0]))
            .map(|(_, rows)| rows)
            .collect()
    });
    eprintln!(
        "{total} files in {}; {} already there; fetching {}",
        collections.join(", "),
        total - todo.len(),
        todo.len()
    );

    // One fetch for each content, however many paths want it.
    let mut contents: BTreeMap<&str, Content> = BTreeMap::new();
    for rows in todo {
        let content = contents
            .entry(rows[0].sha256.as_str())
            .or_insert_with(|| Content {
                paths: Vec::new(),
                sources: Vec::new(),
            });
        content.paths.push(rows[0].path.clone());
        for row in rows {
            if !content.sources.iter().any(|s| s.source == row.source) {
                content.sources.push(row.clone());
            }
        }
    }
    // Archive members by archive, to read each archive once.
    let mut archives: BTreeMap<&str, Vec<&Content>> = BTreeMap::new();
    let mut singles: Vec<&Content> = Vec::new();
    for content in contents.values() {
        match &content.row().source {
            Source::Tar { url, .. } | Source::PdfiumTemplate { url, .. } => {
                archives.entry(url.as_str()).or_default().push(content)
            }
            _ => singles.push(content),
        }
    }

    let http = Http::new();
    let progress = Progress {
        files: AtomicU64::new(0),
        bytes: AtomicU64::new(0),
        failed: Mutex::new(Vec::new()),
    };
    let started = Instant::now();
    let fetching = todo_count(&contents);
    let done = std::sync::atomic::AtomicBool::new(false);
    std::thread::scope(|scope| {
        scope.spawn(|| {
            let mut last = Instant::now();
            while !done.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(200));
                if last.elapsed() >= Duration::from_secs(15) {
                    last = Instant::now();
                    eprintln!(
                        "[{:5.0}s] {}/{fetching} files, {:.2} GB, {} failed",
                        started.elapsed().as_secs_f64(),
                        progress.files.load(Ordering::Relaxed),
                        progress.bytes.load(Ordering::Relaxed) as f64 / 1e9,
                        progress.failed.lock().unwrap().len()
                    );
                }
            }
        });
        pool.install(|| {
            rayon::join(
                || {
                    archives.par_iter().for_each(|(url, members)| {
                        fetch_archive(&http, data, out, url, members, &progress)
                    })
                },
                || {
                    singles.par_iter().for_each(|content| {
                        match source::bytes(&http, data, content.row())
                            .and_then(|bytes| deliver(out, content, &bytes, &progress))
                        {
                            Ok(()) => {}
                            Err(e) => fall_back(&http, data, out, content, e, &progress),
                        }
                    })
                },
            )
        });
        done.store(true, Ordering::Relaxed);
    });

    let failed = progress.failed.into_inner().unwrap();
    eprintln!(
        "fetched {} files, {:.2} GB, in {:.0} s; {} failed",
        progress.files.load(Ordering::Relaxed),
        progress.bytes.load(Ordering::Relaxed) as f64 / 1e9,
        started.elapsed().as_secs_f64(),
        failed.len()
    );
    for (path, error) in &failed {
        eprintln!("  {path}: {error}");
    }
    Ok(failed.is_empty())
}

fn todo_count(contents: &BTreeMap<&str, Content>) -> usize {
    contents.values().map(|c| c.paths.len()).sum()
}

/// The members `members` want from the archive at `url`, read once.
fn fetch_archive(
    http: &Http,
    data: &Path,
    out: &Path,
    url: &str,
    members: &[&Content],
    progress: &Progress,
) {
    let templates = members
        .iter()
        .any(|c| matches!(c.row().source, Source::PdfiumTemplate { .. }));
    let mut wanted: HashMap<&str, Vec<&Content>> = HashMap::new();
    for content in members {
        if let Source::Tar { member, .. } = &content.row().source {
            wanted.entry(member.as_str()).or_default().push(content);
        }
    }
    let mut delivered: BTreeSet<*const Content> = BTreeSet::new();
    let mut failures: Vec<(&Content, anyhow::Error)> = Vec::new();
    let read = if templates {
        // Templates include other files: hold the whole archive.
        archive::all(http, url).map(|files| {
            for (name, bytes) in &files {
                for content in wanted.get(name.as_str()).into_iter().flatten() {
                    match deliver(out, content, bytes, progress) {
                        Ok(()) => drop(delivered.insert(*content as *const _)),
                        Err(e) => failures.push((content, e)),
                    }
                }
            }
            let files = source::normalized(files);
            for content in members {
                if let Source::PdfiumTemplate { member, .. } = &content.row().source {
                    match pdfium::expand(member, &files)
                        .and_then(|b| deliver(out, content, &b, progress))
                    {
                        Ok(()) => drop(delivered.insert(*content as *const _)),
                        Err(e) => failures.push((content, e)),
                    }
                }
            }
        })
    } else {
        archive::each(http, url, |name, data| {
            if let Some(contents) = wanted.get(name) {
                let mut bytes = Vec::new();
                data.read_to_end(&mut bytes)?;
                for content in contents {
                    match deliver(out, content, &bytes, progress) {
                        Ok(()) => drop(delivered.insert(*content as *const _)),
                        Err(e) => failures.push((content, e)),
                    }
                }
            }
            Ok(())
        })
    };
    let read_error = read.err().map(|e| format!("{e:#}"));
    let failed: BTreeSet<*const Content> = failures.iter().map(|(c, _)| *c as *const _).collect();
    for (content, e) in failures {
        fall_back(http, data, out, content, e, progress);
    }
    for content in members {
        let ptr = *content as *const Content;
        if !delivered.contains(&ptr) && !failed.contains(&ptr) {
            let why = read_error
                .clone()
                .unwrap_or_else(|| "not in the archive".into());
            fall_back(
                http,
                data,
                out,
                content,
                anyhow::anyhow!("{url}: {why}"),
                progress,
            );
        }
    }
}

/// Checks `dir` against `collections`' locks: every file there and
/// right, and none the locks don't name (outside `.git` and, unless the
/// pool is among them, `pool/`).
pub fn verify(data: &Path, dir: &Path, collections: &[String], jobs: usize) -> Result<bool> {
    let by_path = load(data, collections)?;
    let pool = rayon::ThreadPoolBuilder::new().num_threads(jobs).build()?;
    let wrong: Vec<(String, &'static str)> = pool.install(|| {
        by_path
            .par_iter()
            .filter_map(|(path, rows)| {
                let full = dir.join(path);
                if !full.is_file() {
                    Some((path.clone(), "missing"))
                } else if !present(dir, &rows[0]) {
                    Some((path.clone(), "differs"))
                } else {
                    None
                }
            })
            .collect()
    });
    let with_pool = collections.iter().any(|c| c == "pool");
    let mut extra = Vec::new();
    walk(dir, dir, &mut |rel| {
        let skipped = rel.starts_with(".git/") || (!with_pool && rel.starts_with("pool/"));
        if !skipped && !by_path.contains_key(rel) {
            extra.push(rel.to_string());
        }
    })?;
    eprintln!(
        "{} files checked in {}: {} missing or different, {} not in the locks",
        by_path.len(),
        dir.display(),
        wrong.len(),
        extra.len()
    );
    for (path, why) in wrong.iter().take(50) {
        eprintln!("  {why}: {path}");
    }
    for path in extra.iter().take(50) {
        eprintln!("  extra: {path}");
    }
    Ok(wrong.is_empty() && extra.is_empty())
}

/// Each regular file under `dir`, relative to `root`, `/`-separated.
pub fn walk(root: &Path, dir: &Path, each: &mut dyn FnMut(&str)) -> Result<()> {
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .with_context(|| format!("{}", dir.display()))?
        .collect::<Result<_, _>>()?;
    entries.sort_by_key(|e| e.file_name());
    for entry in entries {
        let kind = entry.file_type()?;
        let path = entry.path();
        if kind.is_dir() {
            walk(root, &path, each)?;
        } else if kind.is_file() {
            let rel = path
                .strip_prefix(root)?
                .to_string_lossy()
                .replace('\\', "/");
            each(&rel);
        }
    }
    Ok(())
}
