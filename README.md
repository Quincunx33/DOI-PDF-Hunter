# DOI-PDF-Hunter

<div align="center">

  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Playwright-Browser-2E8B57?style=for-the-badge&logo=playwright&logoColor=white" alt="Playwright" />
  <img src="https://img.shields.io/badge/Mode-Dynamic%20PDF%20Discovery-FF7F50?style=for-the-badge" alt="Dynamic PDF discovery" />

  <h3>Advanced DOI and Sci-Hub PDF downloader with smart link discovery.</h3>

</div>

A powerful Python utility that hunts down DOI-based paper links, discovers Sci-Hub PDF endpoints dynamically, follows browser-generated blob/data PDF URLs, and saves cleanly organized PDF files with duplicate protection and retry logic.

## Highlights

- DOI and URL extraction from plain text files
- Sci-Hub page traversal with browser automation
- Dynamic PDF discovery from DOM, blobs, and network responses
- Automatic PDF detection from `blob:` and `data:` URLs
- Duplicate filtering using hash-based tracking
- Retry handling for flaky network conditions
- Clean terminal progress output with colorized status
- Writes logs for invalid links, skips, and retries

## Why this project?

Academic research often begins with a DOI, a PDF link, or a messy list of references. This tool helps turn those raw inputs into downloadable PDF files with minimum manual work.

It is especially useful when:

- you have a `.txt` file of DOIs or links
- the site hides the final PDF behind JavaScript or blob URLs
- you want to avoid downloading the same file twice
- you need a resilient downloader for research workflows

## Features at a glance

- Extracts valid DOI identifiers from text
- Accepts raw URLs or DOI strings in `links.txt`
- Detects PDFs created by JavaScript without visible anchors
- Checks browser response media types and PDF hints
- Stores downloaded file hashes to prevent duplicates
- Saves progress and failure logs to local files
- Works with persistent browser sessions for better continuity

## Project structure

```text
DOI-PDF-Hunter/
├── xtract_doi.py         # Main downloader script
├── links.txt             # Add DOI/URL entries here
├── downloads/            # Downloaded PDFs
├── done.txt              # Completed links
├── skipped.txt           # Missed / skipped / failed links
├── invalid_links.txt     # Parsing issues
├── file_hashes.json      # Hash index for duplicate detection
├── download_errors.log   # Runtime and network errors
├── assets/               # Optional repo assets
├── README.md             # Project documentation
└── requirements.txt      # If present in your setup
```

## Installation

1. Clone the repository:

```bash
git clone https://github.com/Quincunx33/DOI-PDF-Hunter.git
cd DOI-PDF-Hunter
```

2. Create and activate a virtual environment (recommended):

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows
```

3. Install dependencies:

```bash
pip install playwright
python -m playwright install chromium
```

If you are on a system without the Chromium browser available, Playwright will prompt you to install the required browser binary.

## Usage

1. Prepare a text file named `links.txt` in the project root.
2. Add one DOI or URL per line, for example:

```text
10.1038/s41586-020-00773-7
https://example.org/paper
https://doi.org/10.1109/ICCV.2019.00027
```

3. Run the downloader:

```bash
python xtract_doi.py
```

4. The script will:

- detect valid DOI/URL entries
- filter already completed items
- open a browser session
- navigate potential PDF providers
- discover final PDF URLs dynamically
- download matching files into `downloads/`

## Output behavior

The tool writes these files automatically:

- `downloads/` — saved PDF files
- `done.txt` — successfully processed links
- `skipped.txt` — items marked as missing or skipped
- `invalid_links.txt` — malformed or unusable entries
- `download_errors.log` — detailed error output
- `file_hashes.json` — SHA-256 index for deduplication

## Notes

- The project is tuned for research and academic PDF gathering workflows.
- Some publishers and mirrors may block automated access or require manual CAPTCHA solving.
- The script includes temporary retry paths for transient network failures.
- You can enable verbose trace output with:

```bash
PDF_VERBOSE=1 python xtract_doi.py
```

## Example terminal output

```text
PDF Downloader
TXT: links.txt (selected)
Queue: 12 pending | This run: 12 | Retries/link: 2
[1/12] SAVED  paper_1.pdf | 820 KB | network response
[2/12] SKIP   duplicate of paper_2.pdf
[3/12] MISS   not available on this page
```

## License

This project currently does not declare a license in the repository metadata. If you plan to distribute or reuse it publicly, add an appropriate open-source license.

## Contributing

Pull requests, bug reports, and improvements are welcome. If you improve the discovery logic or add smarter validation, feel free to open an issue or contribute a patch.

## Acknowledgements

This project relies on:

- Python
- Playwright
- browser automation for dynamic PDF discovery

---

Made for fast academic PDF harvesting with a little automation magic.
