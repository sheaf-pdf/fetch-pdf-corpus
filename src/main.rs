//! Rebuilds a PDF test corpus from public sources, byte for byte: each
//! file comes from where it was first published, or from this
//! repository, and is checked against its sha256 before it's written.

mod archive;
mod fetch;
mod http;
mod lock;
mod make_lock;
mod pdfium;
mod sets;
mod source;
mod zip;

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Result, bail};
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// This repository: its `locks/` and `corpus/`.
    #[arg(long, global = true, default_value = env!("CARGO_MANIFEST_DIR"))]
    data: PathBuf,
    /// Files fetched or checked at once.
    #[arg(long, global = true, default_value_t = 16)]
    jobs: usize,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Fetch collections into a folder. Files already there and right
    /// are kept, so a rerun resumes. With no collections, all but `pool`.
    Fetch {
        /// The corpus folder to fill.
        #[arg(long)]
        out: PathBuf,
        collections: Vec<String>,
    },
    /// Check a folder against the locks: every file there and right,
    /// and nothing else.
    Verify {
        #[arg(long)]
        dir: PathBuf,
        collections: Vec<String>,
    },
    /// Rebuild the locks and `corpus/` from a local copy of the corpus,
    /// checking every file against its source. For the corpus's keepers.
    Lock {
        /// The local corpus.
        #[arg(long)]
        from: PathBuf,
        /// The pool files to lock, a path a line.
        #[arg(long)]
        pool_list: Option<PathBuf>,
        collections: Vec<String>,
    },
}

/// The named collections, or every one a plain fetch takes.
fn collections(named: Vec<String>) -> Result<Vec<String>> {
    if named.is_empty() {
        return Ok(lock::COLLECTIONS
            .iter()
            .filter(|(_, default)| *default)
            .map(|(c, _)| c.to_string())
            .collect());
    }
    for c in &named {
        if !lock::COLLECTIONS.iter().any(|(known, _)| known == c) {
            let known: Vec<_> = lock::COLLECTIONS.iter().map(|(c, _)| *c).collect();
            bail!("no collection `{c}`; there are {}", known.join(", "));
        }
    }
    Ok(named)
}

fn run(cli: Cli) -> Result<bool> {
    let data: &Path = &cli.data;
    match cli.command {
        Command::Fetch {
            out,
            collections: named,
        } => fetch::fetch(data, &out, &collections(named)?, cli.jobs),
        Command::Verify {
            dir,
            collections: named,
        } => fetch::verify(data, &dir, &collections(named)?, cli.jobs),
        Command::Lock {
            from,
            pool_list,
            collections: named,
        } => make_lock::lock(
            data,
            &from,
            &collections(named)?,
            pool_list.as_deref(),
            cli.jobs,
        ),
    }
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::from(2)
        }
    }
}
