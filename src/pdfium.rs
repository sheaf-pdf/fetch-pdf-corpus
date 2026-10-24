//! PDFium's `testing/tools/fixup_pdf_template.py`, ported: a hand-written
//! template (`.in`) expanded into a PDF, byte for byte as the script
//! writes it. The corpus's `parsing/pdfium/generated/` was made by the
//! script at the pinned commit; `lock` checks this port against every one.

use std::collections::{HashMap, HashSet};

use anyhow::{Context, Result, bail};

/// Included files whose CRLF line endings become LF.
const OVERRIDE_LINE_ENDINGS: [&str; 4] = [".js", ".fragment", ".in", ".xml"];

const HEADER: &[u8] = b"{{header}}";
const HEADER_REPLACEMENT: &[u8] = b"%PDF-1.7\n%\xa0\xf2\xa4\xf4";
const XREF: &[u8] = b"{{xref}}";
const TRAILER: &[u8] = b"{{trailer}}";
const TRAILERSIZE: &[u8] = b"{{trailersize}}";
const STARTXREF: &[u8] = b"{{startxref}}";
const STREAMLEN: &[u8] = b"{{streamlen}}";

/// The template at `path`, expanded. `files` holds the template and
/// everything it includes, by `/`-separated path from one root.
pub fn expand(path: &str, files: &HashMap<String, Vec<u8>>) -> Result<Vec<u8>> {
    let mut intermediate = Vec::new();
    insert_includes(path, files, &mut intermediate, &mut HashSet::new())?;
    Processor::default().expand(&intermediate)
}

/// Python's iteration over a binary file: lines split after each `\n`.
fn lines(bytes: &[u8]) -> impl Iterator<Item = &[u8]> {
    bytes.split_inclusive(|&b| b == b'\n')
}

/// Python's `\s` in a bytes pattern.
fn is_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

/// `bytes.rstrip()`.
fn rstrip(line: &[u8]) -> &[u8] {
    let end = line
        .iter()
        .rposition(|&b| !is_space(b))
        .map_or(0, |i| i + 1);
    &line[..end]
}

fn insert_includes(
    path: &str,
    files: &HashMap<String, Vec<u8>>,
    out: &mut Vec<u8>,
    visited: &mut HashSet<String>,
) -> Result<()> {
    let path = normpath(path);
    if !visited.insert(path.clone()) {
        eprintln!("Circular inclusion {path}, ignoring");
        return Ok(());
    }
    let name = path.rsplit('/').next().unwrap_or(&path);
    let ext = match name.rfind('.') {
        Some(i) if name[..i].bytes().any(|b| b != b'.') => &name[i..],
        _ => "",
    };
    let override_line_endings = OVERRIDE_LINE_ENDINGS.contains(&ext);
    let data = files
        .get(&path)
        .with_context(|| format!("failed to include {path}"))?;
    let mut end_of_file_line_ending = false;
    for line in lines(data) {
        if let Some(included) = include_target(line) {
            let dir = path.rfind('/').map_or("", |i| &path[..i]);
            let joined = if included.starts_with('/') || dir.is_empty() {
                included
            } else {
                format!("{dir}/{included}")
            };
            insert_includes(&joined, files, out, visited)?;
        } else if override_line_endings && line.ends_with(b"\r\n") {
            out.extend_from_slice(&line[..line.len() - 2]);
            out.push(b'\n');
            end_of_file_line_ending = true;
        } else {
            if override_line_endings {
                end_of_file_line_ending = line.ends_with(b"\n");
            }
            out.extend_from_slice(line);
        }
    }
    if !end_of_file_line_ending {
        out.push(b'\n');
    }
    visited.remove(&path);
    Ok(())
}

/// `re.match(rb'\s*{{include\s+(.+)}}', line)`'s group, decoded.
fn include_target(line: &[u8]) -> Option<String> {
    let start = line.iter().position(|&b| !is_space(b))?;
    let rest = line[start..].strip_prefix(b"{{include")?;
    // `.` stops at a newline, and `(.+)}}` can't reach past one.
    let rest = &rest[..rest.iter().position(|&b| b == b'\n').unwrap_or(rest.len())];
    let spaces = rest.iter().take_while(|&&b| is_space(b)).count();
    // `\s+` is greedy and gives back what `(.+)}}` needs.
    for skip in (1..=spaces).rev() {
        let tail = &rest[skip..];
        if let Some(end) = tail.windows(2).rposition(|w| w == b"}}")
            && end >= 1
        {
            return Some(String::from_utf8_lossy(&tail[..end]).into_owned());
        }
    }
    None
}

/// `os.path.normpath` for a relative `/`-separated path.
pub(crate) fn normpath(path: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|p| *p != "..") => {
                parts.pop();
            }
            _ => parts.push(part),
        }
    }
    let joined = parts.join("/");
    if path.starts_with('/') {
        format!("/{joined}")
    } else if joined.is_empty() {
        ".".into()
    } else {
        joined
    }
}

#[derive(Clone, Copy, PartialEq, Default)]
enum StreamLen {
    #[default]
    Start,
    FindStream,
    FindEndstream,
}

#[derive(Default)]
struct Processor {
    state: StreamLen,
    streamlens: Vec<i64>,
    offset: u64,
    xref_offset: u64,
    max_object_number: u64,
    objects: HashMap<u64, (u64, u64)>,
}

impl Processor {
    fn expand(mut self, intermediate: &[u8]) -> Result<Vec<u8>> {
        for line in lines(intermediate) {
            self.preprocess_line(line)?;
        }
        self.streamlens.reverse();
        let mut out = Vec::new();
        for line in lines(intermediate) {
            out.extend(self.process_line(line)?);
        }
        Ok(out)
    }

    fn preprocess_line(&mut self, line: &[u8]) -> Result<()> {
        if contains(line, STREAMLEN) {
            if self.state != StreamLen::Start {
                bail!("{{{{streamlen}}}} inside a stream");
            }
            self.state = StreamLen::FindStream;
            self.streamlens.push(0);
            return Ok(());
        }
        if self.state == StreamLen::FindStream && rstrip(line) == b"stream" {
            self.state = StreamLen::FindEndstream;
            return Ok(());
        }
        if self.state == StreamLen::FindEndstream {
            let last = self.streamlens.last_mut().expect("a stream was started");
            if rstrip(line) == b"endstream" {
                self.state = StreamLen::Start;
                // The final newline isn't the stream's.
                *last -= 1;
            } else {
                *last += line.len() as i64;
            }
        }
        Ok(())
    }

    fn generate_xref_table(&self) -> Vec<u8> {
        let mut table = format!("xref\n0 {}\n", self.max_object_number + 1).into_bytes();
        for i in 0..=self.max_object_number {
            match self.objects.get(&i) {
                Some((offset, generation)) => {
                    table.extend(format!("{offset:010} {generation:05} n \n").bytes())
                }
                None => table.extend_from_slice(b"0000000000 65535 f \n"),
            }
        }
        table
    }

    fn process_line(&mut self, line: &[u8]) -> Result<Vec<u8>> {
        let mut line = line.to_vec();
        if contains(&line, HEADER) {
            line = replace(&line, HEADER, HEADER_REPLACEMENT);
        }
        if contains(&line, STREAMLEN) {
            let length = self
                .streamlens
                .pop()
                .context("more {{streamlen}}s than streams")?;
            line = replace(&line, STREAMLEN, format!("/Length {length}").as_bytes());
        }
        if contains(&line, XREF) {
            self.xref_offset = self.offset;
            line = self.generate_xref_table();
        }
        if contains(&line, TRAILER) {
            let trailer = format!(
                "trailer <<\n  /Root 1 0 R\n  /Size {}\n>>",
                self.max_object_number + 1
            );
            line = replace(&line, TRAILER, trailer.as_bytes());
        }
        if contains(&line, TRAILERSIZE) {
            let size = format!("/Size {}", self.max_object_number + 1);
            line = replace(&line, TRAILERSIZE, size.as_bytes());
        }
        if contains(&line, STARTXREF) {
            let startxref = format!("startxref\n{}", self.xref_offset);
            line = replace(&line, STARTXREF, startxref.as_bytes());
        }
        if let Some(found) = two_numbers(&line, 0, b"{{object") {
            self.objects
                .insert(found.numbers[0], (self.offset, found.numbers[1]));
            self.max_object_number = self.max_object_number.max(found.numbers[0]);
            // `\g<1> \g<2> obj`: the digits as written, at every match.
            line = replace_numbered(&line, b"{{object", |digits| {
                let mut with = digits[0].to_vec();
                with.push(b' ');
                with.extend_from_slice(digits[1]);
                with.extend_from_slice(b" obj");
                with
            });
        }
        if let Some(found) = two_numbers(&line, 0, b"{{startxrefobj") {
            let [number, generation] = found.numbers;
            let &(offset, recorded) = self
                .objects
                .get(&number)
                .with_context(|| format!("startxrefobj of object {number}, never declared"))?;
            if recorded != generation {
                bail!("startxrefobj {number} {generation}: the object's generation is {recorded}");
            }
            let with = format!("startxref\n{offset}").into_bytes();
            line = replace_numbered(&line, b"{{startxrefobj", |_| with.clone());
        }
        self.offset += line.len() as u64;
        Ok(line)
    }
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

fn replace(hay: &[u8], needle: &[u8], with: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(hay.len());
    let mut i = 0;
    while i < hay.len() {
        if hay[i..].starts_with(needle) {
            out.extend_from_slice(with);
            i += needle.len();
        } else {
            out.push(hay[i]);
            i += 1;
        }
    }
    out
}

/// A match of `{{<name>\s+(\d+)\s+(\d+)}}`.
struct Numbered<'a> {
    end: usize,
    digits: [&'a [u8]; 2],
    numbers: [u64; 2],
}

/// `{{<name>\s+(\d+)\s+(\d+)}}` matched at `at`.
fn two_numbers<'a>(line: &'a [u8], at: usize, name: &[u8]) -> Option<Numbered<'a>> {
    if !line[at..].starts_with(name) {
        return None;
    }
    let mut i = at + name.len();
    let mut digits: [&[u8]; 2] = [b""; 2];
    let mut numbers = [0u64; 2];
    for k in 0..2 {
        let spaces = line[i..].iter().take_while(|&&b| is_space(b)).count();
        let count = line[i + spaces..]
            .iter()
            .take_while(|b| b.is_ascii_digit())
            .count();
        if spaces == 0 || count == 0 {
            return None;
        }
        digits[k] = &line[i + spaces..i + spaces + count];
        numbers[k] = std::str::from_utf8(digits[k]).ok()?.parse().ok()?;
        i += spaces + count;
    }
    line[i..].starts_with(b"}}").then_some(Numbered {
        end: i + 2,
        digits,
        numbers,
    })
}

/// `re.sub` of the `{{<name> x y}}` pattern, every match in the line.
fn replace_numbered(line: &[u8], name: &[u8], with: impl Fn([&[u8]; 2]) -> Vec<u8>) -> Vec<u8> {
    let mut out = Vec::with_capacity(line.len());
    let mut i = 0;
    while i < line.len() {
        if let Some(found) = two_numbers(line, i, name) {
            out.extend(with(found.digits));
            i = found.end;
        } else {
            out.push(line[i]);
            i += 1;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn expand_one(template: &str) -> Vec<u8> {
        let files = HashMap::from([("t.in".to_string(), template.as_bytes().to_vec())]);
        expand("t.in", &files).unwrap()
    }

    fn find(hay: &[u8], needle: &str) -> Option<usize> {
        hay.windows(needle.len())
            .position(|w| w == needle.as_bytes())
    }

    #[test]
    fn offsets_and_lengths_are_filled_in() {
        let pdf = expand_one(
            "{{header}}\n{{object 1 0}} <<>>\nendobj\n{{object 02 0}} <<\n{{streamlen}}\n>>\nstream\nabc\nendstream\nendobj\n{{xref}}\n{{trailer}}\n{{startxref}}\n%%EOF\n",
        );
        let header_len = HEADER_REPLACEMENT.len() + 1;
        assert!(pdf.starts_with(HEADER_REPLACEMENT));
        assert!(find(&pdf, "1 0 obj <<>>").is_some());
        // The digits as written.
        assert!(find(&pdf, "02 0 obj <<").is_some());
        assert!(find(&pdf, "/Length 3\n").is_some());
        assert!(find(&pdf, &format!("{header_len:010} 00000 n \n")).is_some());
        assert!(find(&pdf, "trailer <<\n  /Root 1 0 R\n  /Size 3\n>>").is_some());
        let xref_at = find(&pdf, "xref\n0 3\n").unwrap();
        assert!(pdf.ends_with(format!("startxref\n{xref_at}\n%%EOF\n").as_bytes()));
    }

    #[test]
    fn includes_are_spliced_in_on_their_own_lines() {
        let files = HashMap::from([
            (
                "a/t.in".to_string(),
                b"x\r\n{{include ../f.fragment}}\ny".to_vec(),
            ),
            ("f.fragment".to_string(), b"inc".to_vec()),
        ]);
        let mut out = Vec::new();
        insert_includes("a/t.in", &files, &mut out, &mut HashSet::new()).unwrap();
        assert_eq!(out, b"x\ninc\ny\n");
    }
}
