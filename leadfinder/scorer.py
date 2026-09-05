"""Website scoring and lead scoring.

Two independent 0-100 scores plus a list of plain-English reasons.
All weights come from config.yaml (`scoring:` section) so they can be
tuned without touching code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import website_finder

NO_WEBSITE = website_finder.STATUS_NO_WEBSITE_FOUND
UNREACHABLE = website_finder.STATUS_UNREACHABLE


@dataclass
class ScoreResult:
    website_score: Optional[int]
    lead_score: int
    reasons: list[str] = field(default_factory=list)


def score_website(scan: dict, weights: dict, performance: dict) -> tuple[int, list[str]]:
    """Returns (website_score 0-100, list of issue reasons)."""
    score = 100
    reasons: list[str] = []

    if scan.get("error"):
        return 0, [f"Website could not be loaded ({scan['error']})"]

    if not scan.get("https"):
        score -= weights["no_https_penalty"]
        reasons.append("Website does not use HTTPS")

    if scan.get("https_redirect_ok") == 0 and scan.get("http_status") is not None:
        # Only penalise if we actually attempted an http:// URL and it didn't
        # upgrade to https - see website_checker for when this is set.
        pass  # https_redirect_ok stays 0 by default when not applicable; no extra penalty here

    if not scan.get("title"):
        score -= weights["missing_title_penalty"]
        reasons.append("Missing or empty page title")

    if not scan.get("meta_description"):
        score -= weights["missing_meta_description_penalty"]
        reasons.append("Missing meta description")

    if not scan.get("has_viewport_meta"):
        score -= weights["no_viewport_meta_penalty"]
        reasons.append("No mobile viewport meta tag")

    if scan.get("mobile_overflow"):
        score -= weights["mobile_overflow_penalty"]
        reasons.append("Mobile viewport has layout/overflow problems")

    load_time = scan.get("load_time_seconds")
    if load_time is not None:
        if load_time >= performance["very_slow_load_seconds"]:
            score -= weights["very_slow_load_penalty"]
            reasons.append(f"Website takes {load_time:.1f} seconds to load")
        elif load_time >= performance["slow_load_seconds"]:
            score -= weights["slow_load_penalty"]
            reasons.append(f"Website is slow to load ({load_time:.1f}s)")

    broken_internal = scan.get("broken_internal_links") or 0
    if broken_internal:
        penalty = min(broken_internal * weights["broken_internal_link_penalty_each"],
                      weights["broken_internal_link_penalty_max"])
        score -= penalty
        reasons.append(f"{broken_internal} broken internal link(s) found")

    broken_social = scan.get("broken_social_links") or 0
    if broken_social:
        penalty = min(broken_social * weights["broken_social_link_penalty_each"],
                      weights["broken_social_link_penalty_max"])
        score -= penalty
        reasons.append(f"{broken_social} broken social media link(s) found")

    if not scan.get("has_favicon"):
        score -= weights["no_favicon_penalty"]
        reasons.append("No favicon set")

    if not scan.get("has_contact_form"):
        score -= weights["no_contact_form_penalty"]
        reasons.append("No contact form found")

    if not scan.get("has_tel_link"):
        score -= weights["no_tel_link_penalty"]
        reasons.append("No click-to-call telephone link")

    if not scan.get("has_email_link"):
        score -= weights["no_email_link_penalty"]
        reasons.append("No mailto email link")

    if not scan.get("has_cta"):
        score -= weights["no_cta_penalty"]
        reasons.append("No obvious call-to-action")

    copyright_year = scan.get("copyright_year")
    if copyright_year:
        years_behind = datetime.utcnow().year - copyright_year
        if years_behind >= performance["outdated_copyright_years_behind"]:
            score -= weights["outdated_copyright_penalty"]
            reasons.append(f"Copyright still displays {copyright_year}")

    if scan.get("mixed_content"):
        score -= weights["mixed_content_penalty"]
        reasons.append("Mixed content (insecure resources on a secure page)")

    large_images = scan.get("large_images_count") or 0
    if large_images:
        penalty = min(large_images * weights["large_images_penalty_each"],
                      weights["large_images_penalty_max"])
        score -= penalty
        reasons.append(f"{large_images} excessively large image(s) found")

    if not scan.get("sitemap_present"):
        score -= weights["no_sitemap_penalty"]

    if not scan.get("robots_txt_present"):
        score -= weights["no_robots_penalty"]

    score = max(0, min(100, round(score)))
    return score, reasons


def score_lead(website_status: str, website_score: Optional[int], scan: Optional[dict],
                has_domain_email: bool, uses_free_email_provider: bool,
                weights: dict, performance: dict) -> tuple[int, list[str]]:
    """Returns (lead_score 0-100, list of plain-English reasons), highest
    score = strongest prospect for web design/hosting services."""
    score = 0
    reasons: list[str] = []

    if website_status == NO_WEBSITE:
        score += weights["no_website"]
        reasons.append("No website could be found for this business")
        if uses_free_email_provider:
            score += weights["no_website_free_email_bonus"]
            reasons.append("No website and uses a free email provider for business contact")
        return _finalise(score, reasons)

    if website_status == UNREACHABLE:
        score += weights["website_unavailable"]
        reasons.append("Website exists but is currently unreachable/broken")
        return _finalise(score, reasons)

    scan = scan or {}

    if scan.get("mobile_overflow") or scan.get("mobile_friendly") == 0:
        score += weights["very_poor_mobile"]
        reasons.append("Mobile viewport has layout problems")

    load_time = scan.get("load_time_seconds")
    if load_time is not None and load_time >= performance["very_slow_load_seconds"]:
        score += weights["very_slow_website"]
        reasons.append(f"Website takes {load_time:.1f} seconds to load")

    if not scan.get("https"):
        score += weights["no_https"]
        reasons.append("Website does not use HTTPS")

    broken_internal = scan.get("broken_internal_links") or 0
    if broken_internal:
        score += weights["broken_internal_links"]
        reasons.append(f"{broken_internal} broken link(s) found on the website")

    copyright_year = scan.get("copyright_year")
    if copyright_year and datetime.utcnow().year - copyright_year >= \
            performance["outdated_copyright_years_behind"]:
        score += weights["outdated_indicators"]
        reasons.append(f"Copyright still displays {copyright_year}")

    if not scan.get("has_contact_form"):
        score += weights["no_contact_form"]
        reasons.append("Website has no contact form")

    if not has_domain_email:
        score += weights["no_domain_email"]
        if uses_free_email_provider:
            reasons.append("Website exists but business email is hosted on a free email provider")
            score += weights["uses_free_email_provider"]
            score += weights["website_no_domain_email_bonus"]
        else:
            reasons.append("No domain-based business email found")

    if not scan.get("has_viewport_meta") or not scan.get("has_favicon"):
        score += weights["poor_accessibility"]
        reasons.append("Basic accessibility/technical indicators are missing")

    if not scan.get("title") or not scan.get("meta_description"):
        score += weights["missing_title_or_meta"]
        reasons.append("Missing page title or meta description")

    if website_score is not None and website_score >= 80 and has_domain_email:
        score += weights["professional_modern_website"]
        reasons.append("Website appears modern and professional")

    return _finalise(score, reasons)


def _finalise(score: int, reasons: list[str]) -> tuple[int, list[str]]:
    score = max(0, min(100, round(score)))
    if not reasons:
        reasons.append("No significant issues identified")
    return score, reasons


def classify_lead_reasons(email_classifications: list[str]) -> tuple[bool, bool]:
    """Returns (has_domain_email, uses_free_email_provider)."""
    has_domain_email = "DOMAIN_EMAIL" in email_classifications
    free_types = {"GMAIL", "OUTLOOK", "HOTMAIL", "YAHOO", "OTHER_FREE_EMAIL"}
    uses_free_email_provider = any(c in free_types for c in email_classifications)
    return has_domain_email, uses_free_email_provider
