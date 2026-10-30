import base64
import binascii
from datetime import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote_to_bytes, urljoin, urlsplit, urlunsplit
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

BASE = "https://sci-hub.st/"
LINKS_FILE = "links.txt"
DOWNLOAD_DIR = "downloads"
SKIP_LOG = "skipped.txt"
DONE_LOG = "done.txt"
INVALID_LOG = "invalid_links.txt"
HASH_INDEX_FILE = "file_hashes.json"
CHANNEL = "chrome"
DELAY_BETWEEN_LINKS = 15
PAGE_TIMEOUT = 45000
PDF_SETTLE_MS = 600  # Catch responses and DOM updates after a link first appears.
PDF_FALLBACK_WAIT_MS = 3500  # Look for late candidates after a failed URL.
MAX_INLINE_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_LINK_ATTEMPTS = 3
MAX_PAGE_ATTEMPTS = 3
ERROR_LOG = "download_errors.log"
VERBOSE = os.environ.get("PDF_VERBOSE", "").strip().lower() in {"1", "yes", "true"}
BATCH_SIZE = 999

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
URL_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]{1,15}://(?:(?![A-Za-z][A-Za-z0-9+.-]{1,15}://)[^\s<>'\"])+",
    re.IGNORECASE,
)
LINK_MARKER_RE = re.compile(
    r"\b(?:https?|www\.|doi:|10\.|[A-Za-z][A-Za-z0-9+.-]{1,15}://)",
    re.IGNORECASE,
)
NOT_FOUND_HINTS = ("not found", "not available", "no result", "did not find")
SCRIPT_DIR = Path(__file__).resolve().parent


def terminal_supports_color():
    """Enable Windows VT processing before emitting ANSI escape sequences.

    The legacy Windows console prints literal '[96m' text unless virtual
    terminal processing is enabled. If enabling fails, output plain text.
    """
    if "NO_COLOR" in os.environ:
        return False
    if not (sys.stdout.isatty() or "FORCE_COLOR" in os.environ):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        ))
    except (AttributeError, OSError, ValueError):
        return False


COLOR_ENABLED = terminal_supports_color()


def paint(text, code):
    value = str(text)
    return f"\033[{code}m{value}\033[0m" if COLOR_ENABLED else value


def short_error(exc, limit=140):
    """Show the useful first line of a Playwright error, not its call log."""
    message = str(exc).strip().splitlines()
    first = message[0] if message else type(exc).__name__
    first = re.sub(r"^(?:Page|Frame|APIRequestContext|BrowserType)\.[^:]+:\s*", "", first)
    return first[:limit - 1] + "…" if len(first) > limit else first


def record_error(stage, link, exc):
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as log:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            log.write(f"[{stamp}] {stage} | {link} | {type(exc).__name__}: {exc}\n")
    except OSError:
        # A bad log destination should not bring down the download.
        pass


def compact_link(link, limit=52):
    if len(link) <= limit:
        return link
    return link[:limit - 1] + "…"


def show_item_result(position, total, link, status, detail=""):
    styles = {"ok": ("SAVED", "32"), "skip": ("SKIP", "33"),
              "missing": ("MISS", "33"), "error": ("ERROR", "31")}
    name, color = styles.get(status, (status.upper(), "36"))
    extra = f"  {detail}" if detail else ""
    print(f"[{position:>{len(str(total))}}/{total}] "
          f"{paint(name.ljust(5), color)}  {compact_link(link)}{extra}", flush=True)


def verbose(message):
    if VERBOSE:
        print(paint("  · " + message, "90"), flush=True)


def read_lines(path):
    f = Path(path)
    if not f.exists():
        return []
    return [
        line.strip()
        for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if line.strip()
    ]


def parse_link_line(raw_line):
    line = raw_line.strip().lstrip("\ufeff")
    if not line or line.startswith("#"):
        return [], []
    doi_matches = []
    for match in DOI_RE.finditer(line):
        doi = match.group(0).strip(".,;:!?\"'`<>")
        while doi.endswith(")") and doi.count("(") < doi.count(")"):
            doi = doi[:-1]
        if doi:
            doi_matches.append(doi)
    if doi_matches:
        return list(dict.fromkeys(doi_matches)), []
    urls = URL_RE.findall(line)
    if not urls:
        urls = re.findall(r"\bwww\.[^\s<>'\"]+", line, re.IGNORECASE)
    links = []
    errors = []
    for raw_url in urls:
        candidate = raw_url.strip("<>{}[]\"'`").rstrip(".,;:!?")
        while candidate.endswith((")", "]")):
            closing = candidate[-1]
            opening = "(" if closing == ")" else "["
            if candidate.count(opening) >= candidate.count(closing):
                break
            candidate = candidate[:-1]
        if candidate.lower().startswith("www."):
            candidate = "https://" + candidate
        try:
            parsed = urlsplit(candidate)
            hostname = parsed.hostname
            parsed.port
        except ValueError:
            hostname = None
            parsed = None
        if (
            parsed is None
            or parsed.scheme.lower() not in ("http", "https")
            or not hostname
            or any(character in hostname for character in ",;<>\"'")
            or any(character.isspace() for character in candidate)
        ):
            errors.append(f"Malformed URL: {raw_url}")
            continue
        normalized = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
        links.append(normalized)
    if links:
        return list(dict.fromkeys(links)), errors
    if errors:
        return [], errors
    return [], ["No DOI or HTTP(S) URL found"]


def link_identity(link):
    """Canonical comparison key: DOI case-insensitive; other URL paths stay case-sensitive.

    Works for bare DOIs and DOI URLs alike, including variants in done.txt.
    """
    value = str(link).strip()
    doi_match = DOI_RE.search(value)
    if doi_match:
        doi = doi_match.group(0).strip(".,;:!?\"'`<>")
        while doi.endswith(")") and doi.count("(") < doi.count(")"):
            doi = doi[:-1]
        return "doi:" + doi.casefold()

    # Unlike DOIs, ordinary URL paths and query strings can be case-sensitive.
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname:
            return "url:" + urlunsplit((
                parsed.scheme.lower(), parsed.netloc.lower(),
                parsed.path, parsed.query, parsed.fragment,
            ))
    except ValueError:
        pass
    return "raw:" + value


def discover_link_files(directory="."):
    """Find TXT files containing mostly usable links, not license/notice text.

    A lone link-like substring in an arbitrary text file is not enough to
    classify that file as a download queue.
    """
    excluded = {
        Path(SKIP_LOG).name.casefold(),
        Path(DONE_LOG).name.casefold(),
        Path(INVALID_LOG).name.casefold(),
    }
    non_link_prefixes = (
        "thirdpartynotices", "license", "licence", "notice", "copying",
        "readme", "changelog", "requirements",
    )
    found = []
    for path in sorted(Path(directory).glob("*.txt")):
        if (path.name.casefold() in excluded
                or path.stem.casefold().startswith(non_link_prefixes)):
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        content_lines = [line for line in lines
                         if line.strip() and not line.lstrip().startswith("#")]
        if not content_lines:
            continue
        valid_lines = sum(bool(parse_link_line(line)[0]) for line in content_lines)
        if valid_lines and valid_lines * 2 >= len(content_lines):
            found.append(path)
    return found


def select_link_files(directory="."):
    candidates = discover_link_files(directory)
    if not candidates:
        sys.exit(
            f"{paint('No DOI/URL TXT files found.', '31')}\n"
            "Put links.txt beside download.py."
        )
    if len(candidates) == 1:
        print(f"TXT: {candidates[0].name} (selected)")
        return candidates

    print(f"\nTXT files ({len(candidates)}):")
    for number, path in enumerate(candidates, start=1):
        print(f" {number}) {path.name}")
    default = next(
        (number for number, path in enumerate(candidates, start=1)
         if path.name.casefold() == LINKS_FILE.casefold()),
        None,
    )
    prompt = f"Select (number/all, Enter={default}): " if default else "Select (number/all): "
    while True:
        choice = input(prompt).strip().lower()
        if not choice:
            if default is not None:
                return [candidates[default - 1]]
            print("Enter a number or 'all'.")
            continue
        if choice == "all":
            return candidates
        try:
            numbers = []
            for part in re.split(r"[\s,]+", choice):
                if "-" in part:
                    first, last = (int(value) for value in part.split("-", 1))
                    if first > last:
                        raise ValueError
                    numbers.extend(range(first, last + 1))
                else:
                    numbers.append(int(part))
            numbers = list(dict.fromkeys(numbers))
            if not numbers or any(number < 1 or number > len(candidates) for number in numbers):
                raise ValueError
            return [candidates[number - 1] for number in numbers]
        except ValueError:
            print("Use a file number (e.g. 1,2) or 'all'.")


def load_links(paths):
    if isinstance(paths, (str, Path)):
        paths = [Path(paths)]
    done = set()
    for old_link in read_lines(DONE_LOG):
        normalized, _ = parse_link_line(old_link)
        done.update(link_identity(item) for item in (normalized or [old_link]))
    pending = []
    seen = set()
    duplicate_count = 0
    completed_count = 0
    invalid = []
    for path in paths:
        try:
            source_lines = Path(path).read_text(
                encoding="utf-8-sig", errors="replace"
            ).splitlines()
        except OSError as exc:
            invalid.append((str(path), 0, "", f"Could not read file: {exc}"))
            continue
        for line_number, raw_line in enumerate(source_lines, start=1):
            if not raw_line.strip() or raw_line.strip().startswith("#"):
                continue
            parsed, errors = parse_link_line(raw_line)
            invalid.extend(
                (str(path), line_number, raw_line, error) for error in errors
            )
            for link in parsed:
                key = link_identity(link)
                if key in seen:
                    duplicate_count += 1
                    continue
                seen.add(key)
                if key in done:
                    completed_count += 1
                    continue
                pending.append(link)
    if duplicate_count or completed_count:
        print(f"Links filtered: {duplicate_count} repeated | "
              f"{completed_count} already in {DONE_LOG}")
    if invalid:
        with open(INVALID_LOG, "a", encoding="utf-8") as fh:
            for source, line_number, raw_line, error in invalid:
                fh.write(f"{source}:{line_number} | {error} | {raw_line!r}\n")
        print(
            paint(
                f"Warning: {len(invalid)} parsing issue(s) logged to {INVALID_LOG}.",
                "33",
            )
        )
    if not pending:
        if invalid:
            sys.exit(paint(f"No valid links found. See {INVALID_LOG} for details.", "31"))
        sys.exit(paint(f"Nothing left to do. Links are already in {DONE_LOG}.", "33"))
    return pending


def show_progress(current, total):
    width = 28
    ratio = current / total if total else 1
    filled = int(width * ratio)
    bar = f"{paint('█' * filled, '92')}{paint('░' * (width - filled), '90')}"
    print(f"{paint('Progress', '1;36')}  {bar}  {paint(f'{ratio * 100:5.1f}%', '1;37')}  ({current}/{total})", flush=True)


def internet_available():
    try:
        with urlopen("https://example.com/", timeout=5) as response:
            return response.status < 500
    except Exception:
        return False


def wait_for_internet(max_wait_seconds=25):
    """Check connectivity for a bounded time. Never hang forever offline."""
    deadline = time.monotonic() + max_wait_seconds
    while True:
        if internet_available():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(5, remaining))


def is_transient_network_error(exc):
    message = str(exc).lower()
    network_hints = (
        "net::err_",
        "internet",
        "connection",
        "network",
        "dns",
        "socket",
        "temporarily unavailable",
        "timed out",
        "timeout",
    )
    if any(hint in message for hint in network_hints):
        if "waiting for selector" in message or "waiting for locator" in message:
            return any(hint in message for hint in network_hints[:9]) and "net::err_" in message
        return True
    return bool(re.search(r"\bhttp (?:408|429|5\d\d)\b", message))


def goto_with_network_retry(page, url):
    for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
        try:
            return page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        except Exception as exc:
            if not is_transient_network_error(exc) or attempt == MAX_PAGE_ATTEMPTS:
                raise
            verbose(f"Page retry {attempt}/{MAX_PAGE_ATTEMPTS - 1}: {short_error(exc)}")
            wait_for_internet(10)
            time.sleep(min(3 * attempt, 8))


def mark_done(link):
    with open(DONE_LOG, "a", encoding="utf-8") as fh:
        fh.write(link + "\n")


def to_target(link):
    m = DOI_RE.search(link)
    return m.group(0).rstrip(".") if m else link


def wait_for_ddos_guard(page):
    for _ in range(60):
        try:
            if "ddos-guard" not in (page.title() or "").lower():
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def human_pause(message):
    print(f"\n{paint('ACTION REQUIRED', '1;33')}  {message}", flush=True)
    input("Fix it in the browser, then press Enter to continue: ")


def clean_name(doi, index):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", doi).strip("._")
    return (name or f"paper_{index}") + ".pdf"


def load_hash_index(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_hash_index(hash_index, path):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(hash_index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temp_path.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def index_existing_pdfs(outdir, hash_index, index_path):
    changed = False
    for pdf_path in outdir.glob("*.pdf"):
        try:
            digest = sha256_file(pdf_path)
            record = hash_index.get(digest)
            indexed_file = record.get("file") if isinstance(record, dict) else None
            if not indexed_file or not (outdir / indexed_file).is_file():
                hash_index[digest] = {
                    "file": pdf_path.name,
                    "size_bytes": pdf_path.stat().st_size,
                    "source": "existing file",
                }
                changed = True
        except OSError:
            continue
    if changed:
        save_hash_index(hash_index, index_path)
    return hash_index


PDF_URL_HINT_RE = re.compile(
    r"\.pdf(?:[?#]|$)|[?&](?:format|type|output|download)=pdf(?:[&#]|$)",
    re.IGNORECASE,
)

# Installed before navigation. Catches PDF blobs even when JavaScript never
# puts the object URL into a visible anchor/iframe, including revoked URLs.
# It records only PDF-like blobs and keeps a bounded in-page cache.
PDF_BLOB_WATCH_JS = r"""(() => {
  try {
    if (window.__pdfBlobWatch || !URL.createObjectURL) return;
    const nativeCreate = URL.createObjectURL;
    const watched = new Map();
    const MAX_TOTAL = 80 * 1024 * 1024;
    const MAX_SINGLE = 80 * 1024 * 1024;
    const MAX_COUNT = 12;
    let retained = 0;
    function remember(url, blob) {
      if (watched.has(url) || blob.size > MAX_SINGLE) return;
      while (watched.size && (watched.size >= MAX_COUNT || retained + blob.size > MAX_TOTAL)) {
        const oldest = watched.keys().next().value;
        retained -= watched.get(oldest).size;
        watched.delete(oldest);
      }
      watched.set(url, blob);
      retained += blob.size;
    }
    URL.createObjectURL = function(object) {
      const url = nativeCreate.apply(this, arguments);
      if (object instanceof Blob && object.size <= MAX_SINGLE) {
        if (/(?:^|\/)pdf(?:$|[+;])/i.test(object.type)) {
          remember(url, object);
        } else if (!object.type || /octet-stream/i.test(object.type)) {
          object.slice(0, 5).text().then(head => {
            if (head === '%PDF-') remember(url, object);
          }).catch(() => {});
        }
      }
      return url;
    };
    Object.defineProperty(window, '__pdfBlobWatch', {
      value: {list: () => [...watched.keys()], get: url => watched.get(url)},
      configurable: false
    });
  } catch (_) { /* Some sites restrict instrumentation; DOM/network still work. */ }
})()"""


def install_pdf_blob_watch(context):
    context.add_init_script(script=PDF_BLOB_WATCH_JS)


# Runs in every accessible frame. Includes elements injected later by JavaScript,
# and walks open shadow roots (where document.querySelectorAll alone cannot look).
PDF_DOM_SCAN_JS = r"""() => {
  const found = [];
  const seen = new Set();
  const selector = [
    'embed', 'iframe', 'object', 'a[href]', 'source', 'param',
    '[data-pdf]', '[data-pdf-url]', '[data-src]', '[data-url]',
    '[data-file]', '[data-download]'
  ].join(',');
  const pdfAddress = v =>
    /\.pdf(?:[?#]|$)|[?&](?:format|type|output|download)=pdf(?:[&#]|$)/i.test(v) ||
    /^blob:/i.test(v) || /^data:application\/(?:x-)?pdf[;,]/i.test(v);
  const add = (value, kind, strong) => {
    if (typeof value !== 'string') return;
    const url = value.trim();
    if (!url || /^(?:javascript:|#|about:|mailto:)/i.test(url)) return;
    if (seen.has(url)) return;
    seen.add(url);
    found.push({url, kind, strong});
  };
  const walk = root => {
    for (const el of root.querySelectorAll(selector)) {
      const tag = el.tagName.toLowerCase();
      const title = [el.id, el.getAttribute('type'), el.getAttribute('title'),
                     el.getAttribute('aria-label'), el.getAttribute('download'),
                     tag === 'a' ? el.textContent.slice(0, 100) : '']
                    .filter(Boolean).join(' ');
      const hasPdfLabel = /(?:^|\W)pdf(?:\W|$)/i.test(title);
      const isPdfEmbed = ['embed', 'iframe', 'object'].includes(tag) && hasPdfLabel;
      for (const attr of ['src', 'data', 'href', 'value', 'data-src', 'data-url',
                          'data-file', 'data-pdf', 'data-pdf-url', 'data-download']) {
        if (!el.hasAttribute(attr)) continue;
        const value = el.getAttribute(attr);
        if (!value) continue;
        // Don't follow generic iframes, generic anchors or arbitrary JS values.
        if (pdfAddress(value) || (hasPdfLabel &&
            ['src','data','href','data-src','data-url','data-file',
             'data-pdf','data-pdf-url','data-download'].includes(attr))) {
          add(value, tag + ':' + attr, isPdfEmbed || hasPdfLabel);
        }
      }
    }
    // Open shadow DOM is visible to scripts; closed shadow roots are not.
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  // Also discover blobs constructed by JavaScript without any DOM link.
  try {
    for (const url of (window.__pdfBlobWatch?.list() || [])) {
      add(url, 'created-blob', true);
    }
  } catch (_) {}
  return found;
}"""


def normalize_pdf_url(raw_url, base_url):
    """Preserve blob/data URLs and safely resolve HTTP(S) relative addresses."""
    if not isinstance(raw_url, str):
        return None
    raw_url = raw_url.strip()
    if not raw_url:
        return None
    if raw_url.lower().startswith("blob:"):
        return raw_url
    if re.match(r"^data:application/(?:x-)?pdf(?:;|,)", raw_url, re.I):
        return raw_url
    url = urljoin(base_url, raw_url)
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
    except ValueError:
        return None
    return url


class PDFDiscovery:
    """Collect observed PDF network replies and dynamic links from all frames."""

    def __init__(self, page):
        self.page = page
        self._candidates = {}
        self._listening = False

    def start(self):
        if not self._listening:
            self.page.on("response", self._on_response)
            self._listening = True

    def stop(self):
        if self._listening:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass  # The page or browser may already have closed.
            self._listening = False

    def add(self, url, *, source, frame=None, response=None, priority=0):
        base_url = (frame.url if frame is not None else self.page.url) or BASE
        if urlsplit(base_url).scheme.lower() not in ("http", "https"):
            base_url = self.page.url or BASE  # about:srcdoc inherits its parent URL.
        normalized = normalize_pdf_url(url, base_url)
        if normalized is None:
            return
        previous = self._candidates.get(normalized)
        if previous is None or priority > previous["priority"]:
            self._candidates[normalized] = {
                "url": normalized,
                "source": source,
                "frame": frame,
                "response": response,
                "priority": priority,
            }
        elif response is not None and previous["response"] is None:
            previous["response"] = response

    def _on_response(self, response):
        try:
            if not 200 <= response.status < 300:
                return
            headers = response.headers
            mime = headers.get("content-type", "").lower()
            disposition = headers.get("content-disposition", "").lower()
            url = response.url
            is_pdf = "application/pdf" in mime or "application/x-pdf" in mime
            pdf_filename = ".pdf" in disposition
            hinted_url = bool(PDF_URL_HINT_RE.search(url))
            if is_pdf or pdf_filename or (hinted_url and "text/html" not in mime):
                self.add(
                    url, source="network response", response=response,
                    priority=50 if is_pdf else 40,
                )
        except Exception:
            # A frame may detach while the browser emits a response event.
            return

    def scan_dom(self):
        for frame in list(self.page.frames):
            try:
                entries = frame.evaluate(PDF_DOM_SCAN_JS)
                for entry in entries:
                    self.add(
                        entry["url"],
                        source="DOM " + entry.get("kind", "element"),
                        frame=frame,
                        priority=30 if entry.get("strong") else 20,
                    )
            except Exception:
                # One inaccessible/navigating frame must not hide other links.
                continue

    def candidates(self):
        return sorted(self._candidates.values(), key=lambda item: -item["priority"])


def find_pdf_url(page):
    """Backwards-compatible helper: scan dynamic DOM, including child frames."""
    discovery = PDFDiscovery(page)
    discovery.scan_dom()
    candidates = discovery.candidates()
    return candidates[0]["url"] if candidates else None


def wait_for_pdf_candidates(page, discovery, timeout_ms=PAGE_TIMEOUT):
    """Poll DOM updates while Playwright captures MIME-identified PDF responses."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        discovery.scan_dom()
        if discovery.candidates():
            page.wait_for_timeout(PDF_SETTLE_MS)
            discovery.scan_dom()
            return discovery.candidates()
        if time.monotonic() >= deadline:
            return []
        page.wait_for_timeout(min(350, max(1, int((deadline - time.monotonic()) * 1000))))


def is_pdf_body(body):
    return isinstance(body, bytes) and b"%PDF-" in body[:1024]


def decode_inline_pdf(pdf_url, frame):
    """Read PDF bytes from a blob: URL in the frame that owns the element."""
    if pdf_url.lower().startswith("data:"):
        header, separator, value = pdf_url.partition(",")
        if not separator:
            raise ValueError("Malformed inline PDF URL")
        is_b64 = ";base64" in header.lower()
        max_encoded = MAX_INLINE_PDF_BYTES * (4 if is_b64 else 3) // (3 if is_b64 else 1) + 1024
        if len(value) > max_encoded:
            raise ValueError("Inline PDF exceeds size limit")
        try:
            raw = unquote_to_bytes(value)
            body = base64.b64decode(raw, validate=True) if is_b64 else raw
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid inline PDF data") from exc
        return body
    if frame is None:
        raise ValueError("Cannot read blob URL without its owning frame")
    encoded = frame.evaluate(
        """async ({url, maxSize}) => {
          // Prefer the original Blob saved before URL.revokeObjectURL() runs.
          const saved = window.__pdfBlobWatch?.get(url);
          let blob = saved;
          if (!blob) {
            const response = await fetch(url);
            if (!response.ok) throw Error('Blob HTTP ' + response.status);
            blob = await response.blob();
          }
          if (blob.size > maxSize) throw Error('Blob PDF exceeds size limit');
          const bytes = new Uint8Array(await blob.arrayBuffer());
          const chunks = [];
          for (let i = 0; i < bytes.length; i += 32768) {
            chunks.push(String.fromCharCode(...bytes.subarray(i, i + 32768)));
          }
          return btoa(chunks.join(''));
        }""",
        {"url": pdf_url, "maxSize": MAX_INLINE_PDF_BYTES},
    )
    return base64.b64decode(encoded, validate=True)


def save_pdf_body(body, path, source, pdf_url, hash_index, index_path, started):
    if not is_pdf_body(body):
        raise RuntimeError("The discovered link did not return a PDF file")
    if len(body) > MAX_PDF_BYTES:
        raise RuntimeError(f"PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)} MB limit")
    digest = hashlib.sha256(body).hexdigest()
    record = hash_index.get(digest)
    if isinstance(record, dict):
        existing_name = record.get("file")
        if existing_name and (path.parent / existing_name).is_file():
            elapsed = max(time.perf_counter() - started, 0.001)
            return len(body), True, existing_name, len(body) * 8 / elapsed / 1_000_000
    # Write atomically: a failed download cannot leave a truncated *.pdf.
    temp_path = path.with_name(path.name + ".part")
    try:
        temp_path.write_bytes(body)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    hash_index[digest] = {
        "file": path.name,
        "size_bytes": len(body),
        "source": source,
        "url": pdf_url if not pdf_url.lower().startswith(("blob:", "data:")) else "inline PDF",
    }
    save_hash_index(hash_index, index_path)
    elapsed = max(time.perf_counter() - started, 0.001)
    return len(body), False, path.name, len(body) * 8 / elapsed / 1_000_000


def grab_pdf(context, pdf_url, path, source, hash_index, index_path,
             response=None, frame=None):
    started = time.perf_counter()
    if pdf_url.lower().startswith(("blob:", "data:")):
        body = decode_inline_pdf(pdf_url, frame)
    else:
        body = None
        # A JS fetch may use request headers not replayable through APIRequest.
        # Its already-observed response is the most reliable source of PDF bytes.
        if response is not None:
            try:
                declared_length = response.headers.get("content-length", "0")
                declared_size = int(declared_length) if declared_length.isdecimal() else 0
                if declared_size > MAX_PDF_BYTES:
                    raise ValueError("Network PDF exceeds configured size limit")
                observed_body = response.body()
                if response.status == 200 and is_pdf_body(observed_body):
                    body = observed_body
            except ValueError:
                raise
            except Exception as exc:
                verbose(f"Could not reuse browser response: {short_error(exc)}")
        if body is None:
            request_args = {"timeout": 120000}
            if context.pages and context.pages[0].url.startswith(("https://", "http://")):
                request_args["headers"] = {"Referer": context.pages[0].url}
            req = context.request.get(pdf_url, **request_args)
            body = req.body()
            if req.status != 200 or not is_pdf_body(body):
                # Some sites need a real browser navigation before a PDF GET works.
                probe = context.new_page()
                try:
                    probe.goto(pdf_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                    probe.wait_for_timeout(500)
                    req = context.request.get(pdf_url, timeout=120000)
                    body = req.body()
                finally:
                    probe.close()
            if req.status != 200:
                raise RuntimeError(f"PDF request returned HTTP {req.status}")
    return save_pdf_body(body, path, source, pdf_url, hash_index, index_path, started)

def handle(context, page, link, index, outdir, hash_index, index_path):
    """Return (status, short detail); keep expected misses out of error logs."""
    target = to_target(link)
    dest = outdir / clean_name(target, index)
    if dest.exists() and dest.stat().st_size > 1000:
        return "skip", "already downloaded"
    discovery = PDFDiscovery(page)
    discovery.start()  # Network listener MUST be active before navigation.
    try:
        page.goto(BASE + target, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if not wait_for_ddos_guard(page):
            human_pause("DDoS-Guard is stuck. Solve it in the browser window.")
        if page.query_selector("iframe[src*='captcha'], img[src*='captcha']"):
            human_pause("A CAPTCHA is showing. Solve it, then press Enter.")
        body = (page.content() or "").lower()
        if any(
            hint in body for hint in (
                "err_internet_disconnected", "err_name_not_resolved",
                "err_connection_reset", "err_connection_refused",
            )
        ):
            return "retry", "page reports a connection issue"
        if page.url.rstrip("/") == BASE.rstrip("/") or any(h in body for h in NOT_FOUND_HINTS):
            return "missing", "not available on this page"

        candidates = wait_for_pdf_candidates(page, discovery)
        if not candidates and page.query_selector("iframe[src*='captcha'], img[src*='captcha']"):
            human_pause("CAPTCHA again. Solve it, then press Enter.")
            candidates = wait_for_pdf_candidates(page, discovery)
        if not candidates:
            return "missing", "no PDF in DOM, blobs or network responses"

        tried = set()
        errors = []
        late_deadline = None
        while True:
            discovery.scan_dom()
            untried = [c for c in discovery.candidates() if c["url"] not in tried]
            if not untried:
                if late_deadline is not None and time.monotonic() < late_deadline:
                    page.wait_for_timeout(350)
                    continue
                break
            for candidate in untried:
                pdf_url = candidate["url"]
                tried.add(pdf_url)
                try:
                    size, duplicate, stored_name, speed_mbps = grab_pdf(
                        context, pdf_url, dest, link, hash_index, index_path,
                        response=candidate["response"], frame=candidate["frame"],
                    )
                    if duplicate:
                        return "skip", f"duplicate of {stored_name}"
                    return "ok", f"{dest.name} | {size / 1024:.0f} KB | {candidate['source']}"
                except Exception as exc:
                    errors.append(exc)
                    record_error("candidate: " + candidate["source"], link, exc)
                    verbose(f"Trying next candidate: {short_error(exc)}")
                    if late_deadline is None:
                        late_deadline = time.monotonic() + PDF_FALLBACK_WAIT_MS / 1000
            page.wait_for_timeout(PDF_SETTLE_MS)
        if errors and all(is_transient_network_error(e) for e in errors):
            return "retry", f"{len(tried)} candidate(s) had network errors"
        return "error", f"{len(tried)} candidate(s) failed; details in {ERROR_LOG}"
    finally:
        discovery.stop()


def main():
    # Opening a .py by double-click may start Python from its installation
    # folder. Always search beside this script instead of WindowsApps.
    os.chdir(SCRIPT_DIR)
    print(paint(r"""                    _.:._
                  ."\ | /".
.,__              "=.\:/.="              __,.
 "=.`"=._            /^\            _.="`.="
   ".'.'."=.=.=.=.-,/   \,-.=.=.=.=".'.'."
     `~.`.`.`.`.`.`.     .'.'.'.'.'.'.~`
        `~.`` ` `.`.\   /.'.' ' ''.~`
            `=.-~~-._ ) ( _.-~~-.=`
                    `\ /`
                     ( )
                      Y""", "96"))
    print(paint("PDF Downloader", "1;36"))
    selected_files = select_link_files()
    links = load_links(selected_files)
    outdir = Path(DOWNLOAD_DIR)
    outdir.mkdir(exist_ok=True)
    hash_index_path = Path(HASH_INDEX_FILE)
    hash_index = index_existing_pdfs(outdir, load_hash_index(hash_index_path), hash_index_path)
    todo = links[:BATCH_SIZE]
    print(f"Files: {', '.join(path.name for path in selected_files)}")
    print(f"Queue: {len(links)} pending | This run: {len(todo)} | Retries/link: {MAX_LINK_ATTEMPTS - 1}")
    print(paint("─" * 65, "90"))

    saved = 0
    skipped, missing, failed = [], [], []
    interrupted = False
    context = None
    try:
        with sync_playwright() as p:
            kwargs = dict(
                user_data_dir="browser_profile", headless=False,
                accept_downloads=True, viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            if CHANNEL:
                kwargs["channel"] = CHANNEL
            try:
                context = p.chromium.launch_persistent_context(**kwargs)
            except Exception as exc:
                record_error("Chrome startup", "browser", exc)
                verbose(f"Chrome unavailable, trying Playwright Chromium: {short_error(exc)}")
                kwargs.pop("channel", None)
                context = p.chromium.launch_persistent_context(**kwargs)
            install_pdf_blob_watch(context)
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_network_retry(page, BASE)
            if not wait_for_ddos_guard(page):
                human_pause("Open the site in this window and let it finish loading.")
            for i, link in enumerate(todo, start=1):
                result, detail = "error", "unexpected error"
                for attempt in range(1, MAX_LINK_ATTEMPTS + 1):
                    try:
                        result, detail = handle(
                            context, page, link, i, outdir, hash_index, hash_index_path
                        )
                    except Exception as exc:
                        record_error(f"link attempt {attempt}", link, exc)
                        result = "retry" if is_transient_network_error(exc) else "error"
                        detail = short_error(exc)
                    if result != "retry":
                        break
                    if attempt == MAX_LINK_ATTEMPTS:
                        record_error("retry limit", link, RuntimeError(detail))
                        result, detail = "error", "retry limit reached; see error log"
                        break
                    verbose(f"Retrying {compact_link(link)} (attempt {attempt + 1}/{MAX_LINK_ATTEMPTS})")
                    wait_for_internet(10)
                    time.sleep(min(3 * 2 ** (attempt - 1), 12))
                show_item_result(i, len(todo), link, result, detail)
                if result in ("ok", "skip"):
                    mark_done(link)
                if result == "ok":
                    saved += 1
                elif result == "skip":
                    skipped.append(link)
                elif result == "missing":
                    missing.append(link)
                else:
                    failed.append(link)
                if i != len(todo):
                    time.sleep(DELAY_BETWEEN_LINKS)
    except KeyboardInterrupt:
        interrupted = True
        print("\n" + paint("Stopped by user. Completed downloads are retained.", "33"))
    except Exception as exc:
        record_error("startup/runtime", BASE, exc)
        print(paint(f"Browser error: {short_error(exc)}", "31"))
        print(f"Details saved to {ERROR_LOG}")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if missing or failed:
            try:
                with open(SKIP_LOG, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(missing + failed) + "\n")
            except OSError as exc:
                record_error("skip log", SKIP_LOG, exc)
        print(paint("─" * 65, "90"))
        print(f"Summary: {paint(str(saved) + ' saved', '32')} | "
              f"{len(skipped)} already present/duplicate | "
              f"{len(missing)} missing | {paint(str(len(failed)) + ' failed', '31')}")
        if interrupted:
            print("Run interrupted; remaining links can be retried next time.")
        if missing or failed:
            print(f"Missed/failed links: {SKIP_LOG}")
        if failed:
            print(f"Error details: {ERROR_LOG}")


if __name__ == "__main__":
    main()
