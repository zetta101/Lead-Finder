"""Logging, text normalisation, validation and rate limiting helpers."""
from __future__ import annotations

import logging
import re
import string
import sys
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)

    # windows consoles default to cp1252 and choke on accented business names
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logger = logging.getLogger("leadfinder")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger  # already configured (e.g. re-imported)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_dir / "leadfinder.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.setLevel(level)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("leadfinder")


class RateLimiter:
    """Per-host politeness delay. Not thread safe - the app runs single
    threaded on purpose so we never hit one site from two directions."""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = max(0.0, delay_seconds)
        self._last_request: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_request.get(host)
        if last is not None:
            elapsed = now - last
            remaining = self.delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()


_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalise_name(name: str) -> str:
    """For dedupe matching, not display - strips punctuation/accents/legal suffixes."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower().translate(_PUNCT_TABLE)
    name = re.sub(r"\b(ltd|limited|llp|plc|the|and|co)\b", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def name_similarity(a: str, b: str) -> float:
    a_n, b_n = normalise_name(a), normalise_name(b)
    if not a_n or not b_n:
        return 0.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "business"


def sanitise_filename(text: str) -> str:
    slug = slugify(text)
    return slug[:80] if slug else "business"


_PHONE_RE = re.compile(
    r"(?:\+44\s?|0)(?:\d[\s\-\.]?){9,10}\d"
)


def extract_phone_numbers(text: str) -> list[str]:
    if not text:
        return []
    candidates = _PHONE_RE.findall(text)
    return [normalise_phone(c) for c in candidates if len(re.sub(r"\D", "", c)) >= 10]


def normalise_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    elif raw.strip().startswith("+44"):
        digits = "0" + digits.lstrip("44")
    return digits


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

FREE_EMAIL_PROVIDERS = {
    "gmail.com": "GMAIL",
    "googlemail.com": "GMAIL",
    "outlook.com": "OUTLOOK",
    "hotmail.com": "HOTMAIL",
    "hotmail.co.uk": "HOTMAIL",
    "outlook.co.uk": "OUTLOOK",
    "live.co.uk": "OUTLOOK",
    "live.com": "OUTLOOK",
    "yahoo.com": "YAHOO",
    "yahoo.co.uk": "YAHOO",
    "icloud.com": "OTHER_FREE_EMAIL",
    "aol.com": "OTHER_FREE_EMAIL",
    "btinternet.com": "OTHER_FREE_EMAIL",
    "talktalk.net": "OTHER_FREE_EMAIL",
    "sky.com": "OTHER_FREE_EMAIL",
    "mail.com": "OTHER_FREE_EMAIL",
    "protonmail.com": "OTHER_FREE_EMAIL",
    "zoho.com": "OTHER_FREE_EMAIL",
}

GENERIC_MAILBOX_PREFIXES = {
    "info", "hello", "enquiries", "enquiry", "sales", "office", "contact",
    "admin", "bookings", "booking", "reception", "mail",
}

# template/tracker junk, not real business addresses
EMAIL_IGNORE_SUBSTRINGS = (
    "example.com", "sentry.io", "wixpress.com", "godaddy.com",
    "yourdomain", "domain.com", "@2x", "schema.org", "w3.org",
)


def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    if not EMAIL_RE.fullmatch(email):
        return False
    lowered = email.lower()
    return not any(bad in lowered for bad in EMAIL_IGNORE_SUBSTRINGS)


def classify_email(email: str) -> str:
    try:
        domain = email.lower().split("@", 1)[1]
    except IndexError:
        return "UNKNOWN"
    return FREE_EMAIL_PROVIDERS.get(domain, "DOMAIN_EMAIL")


def extract_emails(text: str) -> list[str]:
    if not text:
        return []
    found = {m.group(0) for m in EMAIL_RE.finditer(text)}
    return [e for e in found if is_valid_email(e)]


# not exhaustive, but covers most UK small-business domains without pulling
# in the full public suffix list as a dependency
_UK_SECOND_LEVEL = {
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
    "nhs.uk", "police.uk", "gov.uk", "ac.uk",
}


def normalise_url(url: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def extract_domain(url_or_email: str) -> Optional[str]:
    if not url_or_email:
        return None
    if "@" in url_or_email and "://" not in url_or_email:
        host = url_or_email.split("@", 1)[1].lower()
    else:
        normalised = normalise_url(url_or_email)
        if not normalised:
            return None
        host = urlparse(normalised).netloc.lower()
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def registrable_domain(host: str) -> str:
    """e.g. 'shop.example.co.uk' -> 'example.co.uk'."""
    if not host:
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    last_three = ".".join(parts[-3:])
    if last_two in _UK_SECOND_LEVEL and len(parts) >= 3:
        return last_three
    return last_two


@dataclass
class RetryConfig:
    attempts: int = 3
    backoff_seconds: float = 1.5


def retry(fn, *, attempts: int = 3, backoff_seconds: float = 1.5, exceptions=(Exception,),
          logger: Optional[logging.Logger] = None, what: str = "operation"):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if logger:
                logger.debug("Attempt %s/%s failed for %s: %s", attempt, attempts, what, exc)
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)
    raise last_exc  # type: ignore[misc]


def chunked(seq: Iterable, size: int):
    buf = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
