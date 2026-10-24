//! The curated sets, as the corpus's `tools/curated.py` took them: each
//! from an archive at a pinned version, into `<category>/<name>/` under
//! upstream's own paths less `strip`. Only `lock` reads this table; the
//! lock rows it writes name every archive member.

pub struct Set {
    pub category: &'static str,
    pub name: &'static str,
    /// The prefix `curated.py` took off upstream paths that have it.
    pub strip: &'static str,
    pub archives: &'static [Archive],
    /// Files fetched one at a time, by upstream path.
    pub files: &'static [File],
}

pub struct Archive {
    pub url: &'static str,
    /// Where the archive's files sit among upstream's paths.
    pub mount: &'static str,
    /// Leading path components of each member that aren't upstream's.
    pub strip_components: usize,
}

pub struct File {
    pub path: &'static str,
    pub url: &'static str,
    pub base64: bool,
}

/// A GitHub repository at a commit: codeload's tarball, which puts
/// everything under one `<repo>-<commit>/`.
macro_rules! github {
    ($repo:literal, $commit:literal) => {
        &[Archive {
            url: concat!("https://codeload.github.com/", $repo, "/tar.gz/", $commit),
            mount: "",
            strip_components: 1,
        }]
    };
}

#[cfg(test)]
const PDFIUM_COMMIT: &str = "8fdce8e1b14def5d34f217c5099936ddcebe8b81";

pub const SETS: &[Set] = &[
    Set {
        category: "parsing",
        name: "pdfa-safedocs",
        strip: "",
        archives: github!(
            "pdf-association/safedocs",
            "68f730ab8c39204844112c499c5707b79878533b"
        ),
        files: &[],
    },
    Set {
        category: "parsing",
        name: "pdf-differences",
        strip: "",
        archives: github!(
            "pdf-association/pdf-differences",
            "907fe96e52b73e491489eee545c47b119bf9989b"
        ),
        files: &[],
    },
    Set {
        category: "parsing",
        name: "pdf20examples",
        strip: "",
        archives: github!(
            "pdf-association/pdf20examples",
            "c20f2c17bfcc4baab7cfe62e70fae64caf14d5fa"
        ),
        files: &[],
    },
    Set {
        category: "parsing",
        name: "jhove",
        strip: "10.22000-53/data/dataset/",
        // DOI 10.22000/53's archive at TIB's RADAR.
        archives: &[Archive {
            url: "https://www.radar-service.eu/radar-backend/archives/JtlOdwQquZWDqQdq/versions/1/content",
            mount: "",
            strip_components: 0,
        }],
        files: &[],
    },
    Set {
        category: "parsing",
        name: "hayro",
        strip: "hayro-tests/pdfs/",
        archives: github!("LaurenzV/hayro", "643fe553e7b379047d9832183fecb42f0980f5f3"),
        files: &[],
    },
    Set {
        category: "parsing",
        name: "qpdf",
        strip: "qpdf/",
        archives: github!("qpdf/qpdf", "4eba95899886e851cc41d76886483b347612f2a8"),
        files: &[],
    },
    Set {
        category: "parsing",
        name: "pdfium",
        strip: "testing/",
        archives: &[Archive {
            url: "https://pdfium.googlesource.com/pdfium/+archive/8fdce8e1b14def5d34f217c5099936ddcebe8b81/testing/resources.tar.gz",
            mount: "testing/resources/",
            strip_components: 0,
        }],
        files: &[File {
            path: "LICENSE",
            url: "https://pdfium.googlesource.com/pdfium/+/8fdce8e1b14def5d34f217c5099936ddcebe8b81/LICENSE?format=TEXT",
            base64: true,
        }],
    },
    Set {
        category: "conformance",
        name: "verapdf",
        strip: "",
        archives: github!(
            "veraPDF/veraPDF-corpus",
            "bb75f4f0073d9350dfd058c0162a367e6fadf25e"
        ),
        files: &[],
    },
    Set {
        category: "conformance",
        name: "bfo",
        strip: "",
        archives: github!(
            "bfocom/pdfa-testsuite",
            "c22d80260c7ec975a511f9bfcc6590b7ceb1cf18"
        ),
        files: &[],
    },
    Set {
        category: "signatures",
        name: "pyhanko",
        strip: "internal/common-test-utils/src/pyhanko_testing_commons/test_data/data/",
        archives: github!(
            "MatthiasValvekens/pyHanko",
            "2be0a1b4618bfd2ec95011d574f2e12da58344ba"
        ),
        files: &[],
    },
];

pub fn find(category: &str, name: &str) -> Option<&'static Set> {
    SETS.iter()
        .find(|s| s.category == category && s.name == name)
}

/// The path under `parsing/pdfium/` of the template that a generated
/// PDF expands, in upstream's paths: `generated/a/b.pdf` is
/// `testing/resources/a/b.in`.
pub fn pdfium_template(rel: &str) -> Option<String> {
    let stem = rel.strip_prefix("generated/")?.strip_suffix(".pdf")?;
    Some(format!("testing/resources/{stem}.in"))
}

#[test]
fn pdfium_commit_is_the_urls() {
    let pdfium = find("parsing", "pdfium").unwrap();
    assert!(pdfium.archives[0].url.contains(PDFIUM_COMMIT));
    assert!(pdfium.files[0].url.contains(PDFIUM_COMMIT));
}
