"""Verifies a candidate website against a business, or - if none was found
during discovery - tries a handful of likely domain guesses (basically what
a person would type into an address bar) and checks the page content for a
name/town/phone match. No search engines involved, so it won't find
everything; it's built to fail safe (NO_WEBSITE_FOUND) rather than guess wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

from . import utils

logger = utils.get_logger()

STATUS_VERIFIED = "VERIFIED"
STATUS_GUESSED = "GUESSED"
STATUS_UNREACHABLE = "UNREACHABLE"
STATUS_NO_WEBSITE_FOUND = "NO_WEBSITE_FOUND"

_STOPWORDS = {"the", "and", "of", "ltd", "limited", "llp", "plc", "co", "company"}


@dataclass
class WebsiteResult:
    url: Optional[str]
    status: str
    confidence: int  # 0-100
    notes: str
    discovered_via: str


def _name_tokens(name: str) -> list[str]:
    normalised = utils.normalise_name(name)
    return [t for t in normalised.split() if t and t not in _STOPWORDS]


def _candidate_slugs(name: str) -> list[str]:
    tokens = _name_tokens(name)
    if not tokens:
        return []
    joined = "".join(tokens)
    hyphenated = "-".join(tokens)
    slugs = [joined, hyphenated]
    if len(tokens) > 1:
        slugs.append("".join(tokens[:2]))  # first two words, e.g. long trading names
    # de-dupe while preserving order
    seen = set()
    out = []
    for s in slugs:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _candidate_domains(name: str) -> list[str]:
    tlds = [".co.uk", ".com", ".uk"]
    domains = []
    for slug in _candidate_slugs(name):
        for tld in tlds:
            domains.append(f"{slug}{tld}")
    return domains


class WebsiteFinder:
    def __init__(self, user_agent: str, timeout: int, rate_limiter: utils.RateLimiter,
                 enable_domain_guessing: bool = True):
        self.timeout = timeout
        self.rate_limiter = rate_limiter
        self.enable_domain_guessing = enable_domain_guessing
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _fetch(self, url: str) -> Optional[requests.Response]:
        host = utils.extract_domain(url) or url
        self.rate_limiter.wait(host)
        try:
            return self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            logger.debug("Fetch failed for %s: %s", url, exc)
            return None

    def _match_confidence(self, html: str, name: str, town: str, phone: Optional[str]) -> int:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
        score = 0
        name_tokens = _name_tokens(name)
        if name_tokens:
            hits = sum(1 for t in name_tokens if len(t) > 2 and t in text)
            score += int(60 * hits / len(name_tokens))
        if town and town.lower() in text:
            score += 20
        if phone:
            digits = re.sub(r"\D", "", phone)
            if digits and digits[-9:] in re.sub(r"\D", "", text):
                score += 20
        return min(score, 100)

    def verify_supplied_website(self, url: str, name: str, town: str,
                                 phone: Optional[str]) -> WebsiteResult:
        normalised = utils.normalise_url(url)
        if not normalised:
            return WebsiteResult(None, STATUS_NO_WEBSITE_FOUND, 0, "Invalid URL supplied", "none")

        resp = self._fetch(normalised)
        if resp is None or resp.status_code >= 400:
            return WebsiteResult(normalised, STATUS_UNREACHABLE, 10,
                                  f"Supplied website did not respond successfully "
                                  f"(status={getattr(resp, 'status_code', 'no response')})",
                                  "supplied")

        confidence = self._match_confidence(resp.text, name, town, phone)
        status = STATUS_VERIFIED if confidence >= 30 else STATUS_GUESSED
        notes = f"Supplied website matched with confidence {confidence}"
        return WebsiteResult(str(resp.url), status, confidence, notes, "supplied")

    def attempt_domain_guessing(self, name: str, town: str,
                                 phone: Optional[str]) -> WebsiteResult:
        if not self.enable_domain_guessing:
            return WebsiteResult(None, STATUS_NO_WEBSITE_FOUND, 0,
                                  "Domain guessing disabled", "none")

        best: Optional[WebsiteResult] = None
        for domain in _candidate_domains(name):
            for scheme in ("https://", "http://"):
                url = scheme + domain
                resp = self._fetch(url)
                if resp is None or resp.status_code >= 400:
                    continue
                confidence = self._match_confidence(resp.text, name, town, phone)
                if confidence >= 45 and (best is None or confidence > best.confidence):
                    best = WebsiteResult(
                        str(resp.url), STATUS_GUESSED, confidence,
                        f"Found by trying likely domain '{domain}', "
                        f"content match confidence {confidence}",
                        "domain_guess",
                    )
                break  # https worked or failed cleanly; don't also try http for same domain
            if best and best.confidence >= 75:
                break  # good enough, stop trying more guesses

        if best:
            return best
        return WebsiteResult(None, STATUS_NO_WEBSITE_FOUND, 0,
                              "No supplied website and no confident domain guess found",
                              "domain_guess")

    def find(self, name: str, town: str, phone: Optional[str],
              supplied_url: Optional[str]) -> WebsiteResult:
        if supplied_url:
            result = self.verify_supplied_website(supplied_url, name, town, phone)
            if result.status in (STATUS_VERIFIED, STATUS_GUESSED):
                return result
            # Supplied URL was unreachable - fall through to guessing as a
            # last resort rather than giving up immediately.
            guessed = self.attempt_domain_guessing(name, town, phone)
            if guessed.status != STATUS_NO_WEBSITE_FOUND:
                return guessed
            return result

        return self.attempt_domain_guessing(name, town, phone)
