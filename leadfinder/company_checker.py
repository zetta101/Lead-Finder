"""Optional Companies House lookup - only runs if COMPANIES_HOUSE_API_KEY is
set (free key: https://developer.company-information.service.gov.uk/).
Name similarity alone isn't enough to accept a match; we also need the town
to show up in the registered address, otherwise it's skipped.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

from . import utils

logger = utils.get_logger()

CH_SEARCH_URL = "https://api.company-information.service.gov.uk/search/companies"


@dataclass
class CompanyMatch:
    company_name: str
    company_number: str
    company_status: str
    registered_office: str
    sic_codes: list[str]
    confidence: float


class CompanyChecker:
    def __init__(self, timeout: int, rate_limiter: utils.RateLimiter,
                 min_name_match_score: float = 0.72, enabled: bool = True):
        self.timeout = timeout
        self.rate_limiter = rate_limiter
        self.min_name_match_score = min_name_match_score
        self.api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
        self.enabled = enabled and bool(self.api_key)
        if enabled and not self.api_key:
            logger.info(
                "COMPANIES_HOUSE_API_KEY not set - Companies House matching disabled "
                "(this is optional; the program works fine without it)."
            )
        self.session = requests.Session()

    def lookup(self, name: str, town: str) -> Optional[CompanyMatch]:
        if not self.enabled:
            return None

        self.rate_limiter.wait("api.company-information.service.gov.uk")
        try:
            resp = self.session.get(
                CH_SEARCH_URL,
                params={"q": name, "items_per_page": 10},
                auth=(self.api_key, ""),
                timeout=self.timeout,
            )
            if resp.status_code == 401:
                logger.warning("Companies House API key rejected (401) - disabling for this run.")
                self.enabled = False
                return None
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except requests.RequestException as exc:
            logger.debug("Companies House lookup failed for %s: %s", name, exc)
            return None

        best: Optional[CompanyMatch] = None
        best_score = 0.0
        for item in items:
            candidate_name = item.get("title", "")
            name_score = utils.name_similarity(name, candidate_name)
            if name_score < self.min_name_match_score:
                continue

            address_snippet = item.get("address_snippet", "") or ""
            town_match = bool(town) and town.lower() in address_snippet.lower()

            # Require the name similarity AND a town match to accept -
            # name similarity alone is not sufficient corroboration.
            if not town_match:
                continue

            confidence = round(min(1.0, name_score + 0.1), 2)
            if confidence > best_score:
                best_score = confidence
                best = CompanyMatch(
                    company_name=candidate_name,
                    company_number=item.get("company_number", ""),
                    company_status=item.get("company_status", ""),
                    registered_office=address_snippet,
                    sic_codes=item.get("sic_codes", []) or [],
                    confidence=confidence,
                )

        return best
