# pdfium

PDFium's test resources.

- From: https://pdfium.googlesource.com/pdfium at 8fdce8e1b14def5d34f217c5099936ddcebe8b81
- Taken: `LICENSE`, `testing/resources/*.pdf`, `testing/resources/*.in`, `testing/resources/*.fragment`, `generated/*.pdf`, less the prefix `testing/`
- Test files: 802, a row each in `parsing/manifest.tsv`
- License: BSD-3-Clause
- Left out: `resources/pixel/bug_867501.pdf`. ClamAV 1.5.4/28160 flags it: Pdf.Exploit.TALOS_2018_0639-6680787-0
- Left out: `generated/js.pdf`. ClamAV 1.5.4/28160 flags it: Pdf.Dropper.Agent-7158272-0

Written by `tools/curated.py`; rerun it rather than edit this.
