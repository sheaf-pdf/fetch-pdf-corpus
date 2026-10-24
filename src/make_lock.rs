//! `lock`: the locks, and `corpus/`, made from a local copy of the
//! corpus. Every file's source is found and checked against the local
//! file before it's written down: zip members by size and CRC-32 from
//! the zip's directory, everything else by downloading it and comparing
//! sha256s.

use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::sync::Mutex;

use anyhow::{Context, Result, bail};
use rayon::prelude::*;

use crate::fetch::walk;
use crate::http::Http;
use crate::lock::{self, Row, Source};
use crate::sets::{self, Set};
use crate::source::{self, sha256};
use crate::{archive, pdfium, zip};

const SAFEDOCS: &str = "https://digitalcorpora.s3.amazonaws.com/corpora/files/CC-MAIN-2021-31-PDF-UNTRUNCATED/zipfiles";

/// The zip holding SafeDocs file `number`.
fn safedocs_zip(number: u64) -> String {
    let zip = number / 1000;
    let group = zip / 1000 * 1000;
    format!("{SAFEDOCS}/{group:04}-{:04}/{zip:04}.zip", group + 999)
}

/// The local file's facts.
struct Local {
    sha256: String,
    size: u64,
    crc32: u32,
}

/// Where a file is to be looked for.
enum Plan {
    Repo,
    /// A member of a zip: SafeDocs' by file name, UNSAFE-DOCS' by path.
    Zip {
        url: String,
        member: String,
        by_name: bool,
    },
    Get {
        url: String,
    },
    Curated {
        set: &'static Set,
        rel: String,
    },
}

/// A file's source, or why there's none.
type Found = std::result::Result<Source, String>;

pub fn lock(
    data: &Path,
    from: &Path,
    collections: &[String],
    pool_list: Option<&Path>,
    jobs: usize,
) -> Result<bool> {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(jobs).build()?;
    let http = Http::new();
    let mut all_found = true;
    for collection in collections {
        let files = list(from, collection, pool_list)?;
        eprintln!("{collection}: {} files", files.len());
        let locals: HashMap<String, Local> = pool.install(|| {
            files
                .par_iter()
                .map(|path| {
                    let bytes = std::fs::read(from.join(path)).with_context(|| path.clone())?;
                    let local = Local {
                        sha256: sha256(&bytes),
                        size: bytes.len() as u64,
                        crc32: crc32fast::hash(&bytes),
                    };
                    Ok((path.clone(), local))
                })
                .collect::<Result<_>>()
        })?;
        let sources = manifest_sources(from, collection)?;
        let plans: Vec<(String, Result<Plan>)> = files
            .iter()
            .map(|path| (path.clone(), plan(collection, path, &sources)))
            .collect();
        let found = pool.install(|| resolve(&http, plans, &locals));

        let mut rows = Vec::new();
        let mut unresolved = Vec::new();
        for (path, found) in found {
            match found {
                Ok(source) => {
                    let local = &locals[&path];
                    rows.push(Row {
                        path,
                        sha256: local.sha256.clone(),
                        size: local.size,
                        source,
                    });
                }
                Err(why) => unresolved.push((path, why)),
            }
        }
        rows.sort_by(|a, b| a.path.cmp(&b.path));
        copy_repo_files(data, from, collection, &rows)?;
        std::fs::create_dir_all(data.join("locks"))?;
        lock::write(&data.join("locks").join(format!("{collection}.tsv")), &rows)?;
        let kinds = rows
            .iter()
            .fold(BTreeMap::<&str, usize>::new(), |mut kinds, row| {
                *kinds.entry(kind(&row.source)).or_default() += 1;
                kinds
            });
        eprintln!(
            "{collection}: {} rows {kinds:?}; {} without a source",
            rows.len(),
            unresolved.len()
        );
        for (path, why) in &unresolved {
            eprintln!("  {path}: {why}");
        }
        all_found &= unresolved.is_empty();
    }
    Ok(all_found)
}

fn kind(source: &Source) -> &'static str {
    match source {
        Source::Repo => "repo",
        Source::Get { .. } => "get",
        Source::Zip { .. } => "zip",
        Source::Tar { .. } => "tar",
        Source::PdfiumTemplate { .. } => "pdfium-template",
    }
}

/// The collection's files, relative to the corpus root.
fn list(from: &Path, collection: &str, pool_list: Option<&Path>) -> Result<Vec<String>> {
    let mut files = Vec::new();
    match collection {
        "meta" => {
            files.extend(["README.md".to_string(), ".gitignore".to_string()]);
            for dir in ["tools", "index"] {
                walk(from, &from.join(dir), &mut |rel| {
                    files.push(rel.to_string())
                })?;
            }
        }
        "pool" => {
            let list = pool_list.context("the pool's lock needs --pool-list")?;
            let text = std::fs::read_to_string(list)?;
            files.extend(
                text.lines()
                    .map(str::trim)
                    .filter(|l| !l.is_empty())
                    .map(String::from),
            );
        }
        "forms" | "scans" | "signatures" | "conformance" | "parsing" => {
            walk(from, &from.join(collection), &mut |rel| {
                files.push(rel.to_string())
            })?
        }
        _ => bail!("no collection `{collection}`"),
    }
    files.sort();
    files.dedup();
    Ok(files)
}

/// The `source` column of the collection's manifest, by path.
fn manifest_sources(from: &Path, collection: &str) -> Result<HashMap<String, String>> {
    let path = from.join(collection).join("manifest.tsv");
    if !path.is_file() {
        return Ok(HashMap::new());
    }
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(b'\t')
        .from_path(&path)?;
    let headers = reader.headers()?.clone();
    let column = |name: &str| headers.iter().position(|h| h == name);
    let (Some(path_at), Some(source_at)) = (column("path"), column("source")) else {
        bail!("{}: no path or source column", path.display());
    };
    let mut sources = HashMap::new();
    for record in reader.records() {
        let record = record?;
        sources.insert(record[path_at].to_string(), record[source_at].to_string());
    }
    Ok(sources)
}

fn plan(collection: &str, path: &str, sources: &HashMap<String, String>) -> Result<Plan> {
    if collection == "meta" || path == format!("{collection}/manifest.tsv") {
        return Ok(Plan::Repo);
    }
    let parts: Vec<&str> = path.splitn(3, '/').collect();
    let file_name = path.rsplit('/').next().unwrap_or(path);
    let safedocs = |file_name: &str| -> Result<Plan> {
        let number = file_name
            .strip_suffix(".pdf")
            .filter(|n| n.len() == 7 && n.bytes().all(|b| b.is_ascii_digit()))
            .with_context(|| format!("{file_name} isn't a SafeDocs file name"))?;
        Ok(Plan::Zip {
            url: safedocs_zip(number.parse()?),
            member: file_name.to_string(),
            by_name: true,
        })
    };
    match parts[..] {
        ["pool", "safedocs", _] => safedocs(file_name),
        [_, "safedocs", _] => safedocs(file_name),
        ["parsing", "unsafe", _] => {
            let source = sources.get(path).context("no manifest row")?;
            let (url, member) = source
                .split_once('#')
                .context("its source isn't a zip#member")?;
            Ok(Plan::Zip {
                url: url.to_string(),
                member: member.to_string(),
                by_name: false,
            })
        }
        [category, name, rel] if sets::find(category, name).is_some() => {
            if rel == "SOURCE.md" {
                Ok(Plan::Repo)
            } else {
                Ok(Plan::Curated {
                    set: sets::find(category, name).unwrap(),
                    rel: rel.to_string(),
                })
            }
        }
        ["forms", _, _] => Ok(Plan::Get {
            url: sources.get(path).context("no manifest row")?.clone(),
        }),
        _ => bail!("nothing says where it comes from"),
    }
}

/// Each planned file's source, checked against `locals`.
fn resolve(
    http: &Http,
    plans: Vec<(String, Result<Plan>)>,
    locals: &HashMap<String, Local>,
) -> Vec<(String, Found)> {
    let found: Mutex<Vec<(String, Found)>> = Mutex::new(Vec::new());
    let settle = |path: &str, result: Found| found.lock().unwrap().push((path.to_string(), result));
    let mut zips: BTreeMap<String, Vec<(String, String, bool)>> = BTreeMap::new();
    let mut gets = Vec::new();
    // A set's files: (path, path under the set's folder).
    type SetFiles = (&'static Set, Vec<(String, String)>);
    let mut curated: BTreeMap<&'static str, SetFiles> = BTreeMap::new();
    for (path, plan) in plans {
        match plan {
            Err(e) => settle(&path, Err(format!("{e:#}"))),
            Ok(Plan::Repo) => settle(&path, Ok(Source::Repo)),
            Ok(Plan::Zip {
                url,
                member,
                by_name,
            }) => zips.entry(url).or_default().push((path, member, by_name)),
            Ok(Plan::Get { url }) => gets.push((path, url)),
            Ok(Plan::Curated { set, rel }) => curated
                .entry(set.name)
                .or_insert((set, Vec::new()))
                .1
                .push((path, rel)),
        }
    }
    rayon::scope(|scope| {
        scope.spawn(|_| {
            zips.par_iter().for_each(|(url, wanted)| {
                for (path, result) in resolve_zip(http, url, wanted, locals) {
                    settle(&path, result);
                }
            })
        });
        scope.spawn(|_| {
            gets.par_iter().for_each(|(path, url)| {
                let result = http
                    .get(url)
                    .map_err(|e| format!("{e:#}"))
                    .and_then(|bytes| {
                        same(&locals[path], &bytes).map(|()| Source::Get {
                            url: url.clone(),
                            base64: false,
                        })
                    });
                settle(path, result);
            })
        });
        scope.spawn(|_| {
            curated.par_iter().for_each(|(_, (set, files))| {
                for (path, result) in resolve_set(http, set, files, locals) {
                    settle(&path, result);
                }
            })
        });
    });
    let mut found = found.into_inner().unwrap();
    found.sort_by(|a, b| a.0.cmp(&b.0));
    found
}

/// Whether `bytes` are the local file.
fn same(local: &Local, bytes: &[u8]) -> std::result::Result<(), String> {
    if bytes.len() as u64 == local.size && sha256(bytes) == local.sha256 {
        Ok(())
    } else {
        Err(format!(
            "upstream's file differs ({} bytes, sha256 {})",
            bytes.len(),
            sha256(bytes)
        ))
    }
}

fn resolve_zip(
    http: &Http,
    url: &str,
    wanted: &[(String, String, bool)],
    locals: &HashMap<String, Local>,
) -> Vec<(String, Found)> {
    let directory = match zip::directory(http, url) {
        Ok(directory) => directory,
        Err(e) => {
            return wanted
                .iter()
                .map(|(path, ..)| (path.clone(), Err(format!("{e:#}"))))
                .collect();
        }
    };
    let by_name: HashMap<&str, &zip::Member> = directory
        .values()
        .map(|m| (m.name.rsplit('/').next().unwrap_or(&m.name), m))
        .collect();
    wanted
        .iter()
        .map(|(path, member, is_name)| {
            let local = &locals[path];
            let found = if *is_name {
                by_name.get(member.as_str()).copied()
            } else {
                directory.get(member)
            };
            let result = (|| {
                let m = found.ok_or_else(|| format!("{url} has no {member}"))?;
                if m.size != local.size || m.crc32 != local.crc32 {
                    return Err(format!(
                        "{url}'s {} differs (size {}, CRC-32 {:08x})",
                        m.name, m.size, m.crc32
                    ));
                }
                if m.method != zip::STORED && m.method != zip::DEFLATED {
                    return Err(format!("{}: compression method {}", m.name, m.method));
                }
                let offset = zip::data_offset(http, url, m).map_err(|e| format!("{e:#}"))?;
                Ok(Source::Zip {
                    url: url.to_string(),
                    member: m.name.clone(),
                    offset,
                    length: m.compressed,
                    deflate: m.method == zip::DEFLATED,
                })
            })();
            (path.clone(), result)
        })
        .collect()
}

/// An archive member: its name, sha256 and size.
struct Upstream {
    url: &'static str,
    member: String,
    sha256: String,
    size: u64,
}

fn resolve_set(
    http: &Http,
    set: &Set,
    files: &[(String, String)],
    locals: &HashMap<String, Local>,
) -> Vec<(String, Found)> {
    // Every archive member, by upstream path; and, for PDFium's
    // templates, the archive's files themselves.
    let mut upstream: HashMap<String, Upstream> = HashMap::new();
    let mut held: HashMap<&'static str, HashMap<String, Vec<u8>>> = HashMap::new();
    let templates = files
        .iter()
        .any(|(_, rel)| sets::pdfium_template(rel).is_some());
    for archive in set.archives {
        let mut contents = HashMap::new();
        let read = archive::each(http, archive.url, |name, data| {
            let mut bytes = Vec::new();
            data.read_to_end(&mut bytes)?;
            let parts: Vec<&str> = name.split('/').collect();
            if parts.len() > archive.strip_components {
                let path = format!(
                    "{}{}",
                    archive.mount,
                    parts[archive.strip_components..].join("/")
                );
                upstream.insert(
                    pdfium::normpath(&path),
                    Upstream {
                        url: archive.url,
                        member: name.to_string(),
                        sha256: sha256(&bytes),
                        size: bytes.len() as u64,
                    },
                );
            }
            if templates {
                contents.insert(name.to_string(), bytes);
            }
            Ok(())
        });
        if let Err(e) = read {
            let why = format!("{e:#}");
            return files
                .iter()
                .map(|(path, _)| (path.clone(), Err(why.clone())))
                .collect();
        }
        held.insert(archive.url, source::normalized(contents));
    }
    files
        .iter()
        .map(|(path, rel)| {
            let local = &locals[path];
            let result = if let Some(template) = sets::pdfium_template(rel) {
                (|| {
                    let up = upstream
                        .get(&template)
                        .ok_or_else(|| format!("no {template} upstream"))?;
                    let member = pdfium::normpath(&up.member);
                    let bytes =
                        pdfium::expand(&member, &held[up.url]).map_err(|e| format!("{e:#}"))?;
                    same(local, &bytes)?;
                    Ok(Source::PdfiumTemplate {
                        url: up.url.to_string(),
                        member,
                    })
                })()
            } else {
                let candidates = [format!("{}{rel}", set.strip), rel.clone()];
                let matched = candidates
                    .iter()
                    .filter_map(|c| upstream.get(c))
                    .find(|up| up.size == local.size && up.sha256 == local.sha256);
                if let Some(up) = matched {
                    Ok(Source::Tar {
                        url: up.url.to_string(),
                        member: up.member.clone(),
                    })
                } else if let Some(file) = set
                    .files
                    .iter()
                    .find(|f| candidates.iter().any(|c| c == f.path))
                {
                    http.get(file.url)
                        .map_err(|e| format!("{e:#}"))
                        .and_then(|body| {
                            if file.base64 {
                                source::decode_base64(&body).map_err(|e| format!("{e:#}"))
                            } else {
                                Ok(body)
                            }
                        })
                        .and_then(|bytes| same(local, &bytes))
                        .map(|()| Source::Get {
                            url: file.url.to_string(),
                            base64: file.base64,
                        })
                } else if candidates.iter().any(|c| upstream.contains_key(c)) {
                    Err("upstream's file differs".to_string())
                } else {
                    Err(format!("not in {}'s archives", set.name))
                }
            };
            (path.clone(), result)
        })
        .collect()
}

/// `corpus/` for the collection: its `repo` rows' files, copied, and
/// nothing else of the collection's.
fn copy_repo_files(data: &Path, from: &Path, collection: &str, rows: &[Row]) -> Result<()> {
    let corpus = data.join("corpus");
    let ours = |path: &str| match collection {
        "meta" => {
            path == "README.md"
                || path == ".gitignore"
                || path.starts_with("tools/")
                || path.starts_with("index/")
        }
        _ => path.starts_with(&format!("{collection}/")),
    };
    if corpus.is_dir() {
        let mut stale = Vec::new();
        walk(&corpus, &corpus, &mut |rel| {
            if ours(rel) {
                stale.push(rel.to_string());
            }
        })?;
        for rel in stale {
            std::fs::remove_file(corpus.join(rel))?;
        }
    }
    for row in rows.iter().filter(|r| r.source == Source::Repo) {
        let target = corpus.join(&row.path);
        std::fs::create_dir_all(target.parent().unwrap())?;
        std::fs::copy(from.join(&row.path), &target).with_context(|| row.path.clone())?;
    }
    Ok(())
}
