"""Technical/UX website audit using Playwright + Chromium.

Returns a dict matching the website_scans table columns. These are all
best-effort heuristics - real websites are messy - but each measurement is
kept separate so you can see exactly what tripped a score.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Error as PlaywrightError, Page, sync_playwright

from . import utils

logger = utils.get_logger()

CTA_KEYWORDS = (
    "book now", "get a quote", "get quote", "call us", "contact us", "buy now",
    "order now", "enquire", "enquiry", "request a quote", "book online",
    "book an appointment", "free quote", "get in touch", "shop now",
)

SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "twitter.com", "x.com",
                   "linkedin.com", "tiktok.com", "youtube.com")

CMS_SIGNATURES = {
    "wp-content": "WordPress",
    "wp-includes": "WordPress",
    "cdn.shopify.com": "Shopify",
    "static.wixstatic.com": "Wix",
    "squarespace.com": "Squarespace",
    "webflow.io": "Webflow",
    "godaddysites.com": "GoDaddy Website Builder",
    "weebly.com": "Weebly",
}

_COPYRIGHT_RE = re.compile(r"(?:©|copyright)\s*\D{0,15}?(\d{4})", re.IGNORECASE)


class WebsiteChecker:
    def __init__(self, user_agent: str, timeout_seconds: int, rate_limiter: utils.RateLimiter,
                 max_internal_links_checked: int = 5, max_social_links_checked: int = 4,
                 large_image_bytes: int = 500_000):
        self.user_agent = user_agent
        self.timeout_ms = timeout_seconds * 1000
        self.rate_limiter = rate_limiter
        self.max_internal_links_checked = max_internal_links_checked
        self.max_social_links_checked = max_social_links_checked
        self.large_image_bytes = large_image_bytes
        self._playwright = None
        self.browser: Optional[Browser] = None
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": user_agent})

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=True)

    def stop(self) -> None:
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _check_link_batch(self, urls: list[str]) -> int:
        """Returns count of broken links (HEAD/GET failing or 4xx/5xx)."""
        broken = 0
        for url in urls:
            host = utils.extract_domain(url) or url
            self.rate_limiter.wait(host)
            try:
                resp = self._http.head(url, timeout=10, allow_redirects=True)
                if resp.status_code >= 400 or resp.status_code == 405:
                    # Some servers reject HEAD; retry with GET before counting broken.
                    resp = self._http.get(url, timeout=10, allow_redirects=True, stream=True)
                if resp.status_code >= 400:
                    broken += 1
            except requests.RequestException:
                broken += 1
        return broken

    def _check_simple_get(self, url: str) -> bool:
        try:
            self.rate_limiter.wait(utils.extract_domain(url) or url)
            resp = self._http.get(url, timeout=8)
            return resp.status_code < 400
        except requests.RequestException:
            return False

    def scan(self, url: str) -> dict:
        result = {
            "http_status": None, "https": 0, "https_redirect_ok": 0, "title": None,
            "meta_description": None, "has_viewport_meta": 0, "mobile_friendly": 0,
            "mobile_overflow": 0, "load_time_seconds": None, "page_size_bytes": None,
            "num_requests": None, "broken_internal_links": 0, "broken_social_links": 0,
            "has_favicon": 0, "has_contact_form": 0, "has_tel_link": 0,
            "has_email_link": 0, "has_cta": 0, "copyright_year": None,
            "mixed_content": 0, "large_images_count": 0, "cms_detected": None,
            "robots_txt_present": 0, "sitemap_present": 0, "error": None,
        }

        if not self.browser:
            raise RuntimeError("WebsiteChecker.start() must be called before scan()")

        network_requests: list[dict] = []
        page: Optional[Page] = None
        try:
            context = self.browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)

            def on_response(response):
                try:
                    headers = response.headers
                    length = headers.get("content-length")
                    network_requests.append({
                        "url": response.url,
                        "status": response.status,
                        "size": int(length) if length and length.isdigit() else None,
                        "content_type": headers.get("content-type", ""),
                    })
                except Exception:
                    pass

            page.on("response", on_response)

            start = time.perf_counter()
            response = page.goto(url, wait_until="load", timeout=self.timeout_ms)
            load_time = time.perf_counter() - start
            result["load_time_seconds"] = round(load_time, 2)

            if response is None:
                result["error"] = "No response from server"
                context.close()
                return result

            result["http_status"] = response.status
            final_url = page.url
            result["https"] = int(final_url.startswith("https://"))
            if url.startswith("http://") and not url.startswith("http://localhost"):
                result["https_redirect_ok"] = int(final_url.startswith("https://"))

            result["title"] = (page.title() or "").strip()[:300] or None

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            meta_desc = soup.find("meta", attrs={"name": "description"})
            result["meta_description"] = (meta_desc.get("content", "").strip()[:500]
                                           if meta_desc and meta_desc.get("content") else None)

            viewport_tag = soup.find("meta", attrs={"name": "viewport"})
            result["has_viewport_meta"] = int(viewport_tag is not None)

            # Mobile overflow check: resize viewport, re-measure scrollWidth.
            try:
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(300)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > window.innerWidth + 5"
                )
                result["mobile_overflow"] = int(bool(overflow))
            except PlaywrightError:
                pass
            result["mobile_friendly"] = int(
                result["has_viewport_meta"] == 1 and result["mobile_overflow"] == 0
            )

            result["has_tel_link"] = int(soup.find("a", href=re.compile(r"^tel:", re.I)) is not None)
            result["has_email_link"] = int(soup.find("a", href=re.compile(r"^mailto:", re.I)) is not None)

            form_found = False
            for form in soup.find_all("form"):
                form_text = str(form).lower()
                if "email" in form_text or "contact" in form_text or "message" in form_text or \
                   "enquiry" in form_text or "enquire" in form_text:
                    form_found = True
                    break
            result["has_contact_form"] = int(form_found)

            page_text_lower = soup.get_text(" ", strip=True).lower()
            result["has_cta"] = int(any(kw in page_text_lower for kw in CTA_KEYWORDS))

            copyright_match = _COPYRIGHT_RE.search(page_text_lower)
            if copyright_match:
                year = int(copyright_match.group(1))
                if 1995 <= year <= datetime.utcnow().year:
                    result["copyright_year"] = year

            for sig, cms in CMS_SIGNATURES.items():
                if sig in html.lower():
                    result["cms_detected"] = cms
                    break

            has_favicon = bool(soup.find("link", rel=re.compile("icon", re.I)))
            if not has_favicon:
                has_favicon = self._check_simple_get(urljoin(final_url, "/favicon.ico"))
            result["has_favicon"] = int(has_favicon)

            # Network-derived metrics.
            result["num_requests"] = len(network_requests) or None
            sizes = [r["size"] for r in network_requests if r["size"]]
            result["page_size_bytes"] = sum(sizes) if sizes else None
            large_images = sum(
                1 for r in network_requests
                if r.get("content_type", "").startswith("image/")
                and r.get("size") and r["size"] > self.large_image_bytes
            )
            result["large_images_count"] = large_images

            page_is_https = final_url.startswith("https://")
            mixed = any(
                r["url"].startswith("http://") and not r["url"].startswith("http://localhost")
                for r in network_requests
            )
            result["mixed_content"] = int(page_is_https and mixed)

            # Internal + social link checks (small, capped sample).
            base_domain = utils.extract_domain(final_url)
            internal_links, social_links = [], []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                full = urljoin(final_url, href)
                parsed = urlparse(full)
                if parsed.scheme not in ("http", "https"):
                    continue
                link_domain = utils.extract_domain(full)
                if link_domain == base_domain and full not in internal_links:
                    internal_links.append(full)
                elif any(sd in link_domain for sd in SOCIAL_DOMAINS if link_domain) and \
                        full not in social_links:
                    social_links.append(full)

            result["broken_internal_links"] = self._check_link_batch(
                internal_links[:self.max_internal_links_checked]
            )
            result["broken_social_links"] = self._check_link_batch(
                social_links[:self.max_social_links_checked]
            )

            result["robots_txt_present"] = int(self._check_simple_get(urljoin(final_url, "/robots.txt")))
            result["sitemap_present"] = int(self._check_simple_get(urljoin(final_url, "/sitemap.xml")))

            context.close()
        except PlaywrightError as exc:
            logger.warning("Website scan failed for %s: %s", url, exc)
            result["error"] = str(exc)[:500]
        except Exception as exc:
            logger.warning("Unexpected error scanning %s: %s", url, exc)
            result["error"] = str(exc)[:500]

        return result

    def screenshot(self, url: str, desktop_path: str, mobile_path: str,
                    desktop_size: tuple[int, int], mobile_size: tuple[int, int]) -> bool:
        if not self.browser:
            raise RuntimeError("WebsiteChecker.start() must be called before screenshot()")
        try:
            context = self.browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": desktop_size[0], "height": desktop_size[1]},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.goto(url, wait_until="load", timeout=self.timeout_ms)
            page.wait_for_timeout(1500)  # let lazy-loaded/animated content settle
            page.screenshot(path=desktop_path, full_page=False)

            page.set_viewport_size({"width": mobile_size[0], "height": mobile_size[1]})
            page.wait_for_timeout(300)
            page.screenshot(path=mobile_path, full_page=False)

            context.close()
            return True
        except Exception as exc:
            logger.warning("Screenshot failed for %s: %s", url, exc)
            return False
