"""Extracts publicly displayed contact details from a business's website.

Only ever looks at a handful of normal public pages (home + an obvious
contact/about page found by following on-page links) and respects
robots.txt. No login, no CAPTCHA bypass, no aggressive crawling.
"""
from __future__ import annotations

import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import utils

logger = utils.get_logger()

CONTACT_PAGE_HINTS = ("contact", "get-in-touch", "about", "find-us", "location")


@dataclass
class ContactBundle:
    emails: list[tuple[str, str, str]] = field(default_factory=list)  # (email, classification, source_url)
    phones: list[tuple[str, str]] = field(default_factory=list)  # (phone, source_url)
    contact_page_url: Optional[str] = None


class RobotsCache:
    def __init__(self, user_agent: str, timeout: int):
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, robotparser.RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._cache.get(base)
        if rp is None:
            rp = robotparser.RobotFileParser()
            rp.set_url(urljoin(base, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                # If robots.txt can't be read, default to allow (it's a
                # normal public page fetch, not bulk crawling).
                rp = None
            self._cache[base] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True


class ContactExtractor:
    def __init__(self, user_agent: str, timeout: int, rate_limiter: utils.RateLimiter,
                 max_pages: int = 3):
        self.timeout = timeout
        self.rate_limiter = rate_limiter
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.robots = RobotsCache(user_agent, timeout)

    def _get(self, url: str) -> Optional[requests.Response]:
        if not self.robots.can_fetch(url):
            logger.debug("robots.txt disallows fetching %s - skipping", url)
            return None
        host = utils.extract_domain(url) or url
        self.rate_limiter.wait(host)
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code >= 400:
                return None
            return resp
        except requests.RequestException as exc:
            logger.debug("Failed to fetch %s: %s", url, exc)
            return None

    def _find_contact_link(self, homepage_url: str, soup: BeautifulSoup) -> Optional[str]:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = (a.get_text() or "").lower()
            if any(hint in href.lower() for hint in CONTACT_PAGE_HINTS) or \
               any(hint in text for hint in CONTACT_PAGE_HINTS):
                full_url = urljoin(homepage_url, href)
                if utils.extract_domain(full_url) == utils.extract_domain(homepage_url):
                    return full_url
        return None

    def _extract_from_page(self, url: str, html: str, bundle: ContactBundle) -> None:
        soup = BeautifulSoup(html, "html.parser")

        # mailto: / tel: links (highest-confidence signals - explicitly
        # published for contact purposes)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                email = href[7:].split("?")[0].strip()
                if utils.is_valid_email(email):
                    bundle.emails.append((email, utils.classify_email(email), url))
            elif href.lower().startswith("tel:"):
                phone = href[4:].strip()
                normalised = utils.normalise_phone(phone)
                if len(normalised) >= 10:
                    bundle.phones.append((normalised, url))

        # Visible text (covers sites that display "info@example.co.uk"
        # without a mailto: link, and JSON-LD structured data)
        text = soup.get_text(" ", strip=True)
        for email in utils.extract_emails(text):
            bundle.emails.append((email, utils.classify_email(email), url))
        for phone in utils.extract_phone_numbers(text):
            bundle.phones.append((phone, url))

        # JSON-LD structured data (schema.org LocalBusiness etc.)
        for script in soup.find_all("script", type="application/ld+json"):
            content = script.string or ""
            for email in utils.extract_emails(content):
                bundle.emails.append((email, utils.classify_email(email), url))
            for phone in utils.extract_phone_numbers(content):
                bundle.phones.append((phone, url))

    def extract(self, website_url: str) -> ContactBundle:
        bundle = ContactBundle()
        resp = self._get(website_url)
        if resp is None:
            return bundle

        self._extract_from_page(str(resp.url), resp.text, bundle)
        soup = BeautifulSoup(resp.text, "html.parser")

        contact_url = self._find_contact_link(str(resp.url), soup)
        pages_visited = 1
        if contact_url and contact_url != str(resp.url) and pages_visited < self.max_pages:
            bundle.contact_page_url = contact_url
            contact_resp = self._get(contact_url)
            if contact_resp is not None:
                self._extract_from_page(str(contact_resp.url), contact_resp.text, bundle)
                pages_visited += 1

        # De-duplicate while preserving first-seen source URL.
        seen_emails: dict[str, tuple[str, str, str]] = {}
        for email, classification, source in bundle.emails:
            key = email.lower()
            if key not in seen_emails:
                seen_emails[key] = (email, classification, source)
        bundle.emails = list(seen_emails.values())

        seen_phones: dict[str, tuple[str, str]] = {}
        for phone, source in bundle.phones:
            if phone not in seen_phones:
                seen_phones[phone] = (phone, source)
        bundle.phones = list(seen_phones.values())

        return bundle
