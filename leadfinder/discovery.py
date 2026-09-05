"""Finds candidate businesses. `DiscoverySource` is the plugin interface -
add a new one and register it in `DiscoveryManager` to bring in another
data source later without touching the rest of the pipeline.

Currently just OpenStreetMap (Nominatim for geocoding, Overpass for the
actual tag search) - free, no key needed, no scraping involved.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import utils

logger = utils.get_logger()

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Maps an industry keyword (as used in config.yaml) to a list of OSM
# (key, value) tag pairs that plausibly represent it. A search tries each
# pair and merges results. Extend this dict to support more industries.
INDUSTRY_TAG_MAP: dict[str, list[tuple[str, str]]] = {
    "plumber": [("craft", "plumber"), ("shop", "trade")],
    "electrician": [("craft", "electrician")],
    "builder": [("craft", "builder"), ("office", "construction_company")],
    "roofer": [("craft", "roofer")],
    "landscaper": [("craft", "gardener"), ("shop", "garden_centre")],
    "garage": [("shop", "car_repair"), ("shop", "tyres")],
    "mechanic": [("shop", "car_repair")],
    "accountant": [("office", "accountant")],
    "solicitor": [("office", "lawyer")],
    "estate agent": [("office", "estate_agent")],
    "cafe": [("amenity", "cafe")],
    "restaurant": [("amenity", "restaurant")],
    "takeaway": [("amenity", "fast_food")],
    "pub": [("amenity", "pub")],
    "hairdresser": [("shop", "hairdresser")],
    "beauty salon": [("shop", "beauty")],
    "charity": [("office", "charity"), ("amenity", "social_facility")],
    "cleaner": [("craft", "cleaning")],
    "carpenter": [("craft", "carpenter")],
    "painter": [("craft", "painter")],
    "florist": [("shop", "florist")],
    "bakery": [("shop", "bakery")],
    "dentist": [("amenity", "dentist")],
    "vet": [("amenity", "veterinary")],
    "gym": [("leisure", "fitness_centre")],
}


@dataclass
class RawBusiness:
    name: str
    town: str = ""
    address: str = ""
    category: str = ""
    industry_searched: str = ""
    phone: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    source_name: str = ""
    source_url: str = ""
    raw_tags: dict = field(default_factory=dict)


class DiscoverySource(ABC):
    name: str = "unknown"

    @abstractmethod
    def discover(self, location: str, industry: str, limit: int) -> list[RawBusiness]:
        ...


class OverpassDiscoverySource(DiscoverySource):
    """Finds businesses via OpenStreetMap using free Nominatim + Overpass."""

    name = "OpenStreetMap"

    def __init__(self, user_agent: str, request_delay_seconds: float,
                 timeout: int, radius_metres: int, rate_limiter: utils.RateLimiter):
        self.user_agent = user_agent
        self.timeout = timeout
        self.radius = radius_metres
        self.rate_limiter = rate_limiter
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self._geocode_cache: dict[str, tuple[float, float]] = {}

    # geocoding

    def _geocode(self, location: str) -> Optional[tuple[float, float]]:
        if location in self._geocode_cache:
            return self._geocode_cache[location]

        self.rate_limiter.wait("nominatim.openstreetmap.org")
        try:
            resp = utils.retry(
                lambda: self.session.get(
                    NOMINATIM_URL,
                    params={"q": f"{location}, UK", "format": "json", "limit": 1,
                            "countrycodes": "gb"},
                    timeout=self.timeout,
                ),
                attempts=3, backoff_seconds=2, exceptions=(requests.RequestException,),
                logger=logger, what=f"geocode {location}",
            )
            resp.raise_for_status()
            results = resp.json()
        except Exception as exc:
            logger.warning("Geocoding failed for %s: %s", location, exc)
            return None

        if not results:
            logger.warning("No geocoding result for location: %s", location)
            return None

        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
        self._geocode_cache[location] = (lat, lon)
        return lat, lon

    # overpass

    def _build_query(self, tag_pairs: list[tuple[str, str]], lat: float, lon: float) -> str:
        clauses = []
        for key, value in tag_pairs:
            for elem in ("node", "way"):
                clauses.append(f'{elem}["{key}"="{value}"](around:{self.radius},{lat},{lon});')
        body = "\n  ".join(clauses)
        return f"[out:json][timeout:30];\n(\n  {body}\n);\nout center tags;"

    def _post_overpass(self, query: str):
        resp = self.session.post(OVERPASS_URL, data={"data": query}, timeout=self.timeout + 20)
        # The free public Overpass instance frequently returns transient 504s
        # under load - raise here so utils.retry actually retries on them,
        # rather than treating a bad HTTP status as a successful call.
        resp.raise_for_status()
        return resp

    def _run_overpass(self, query: str) -> list[dict]:
        self.rate_limiter.wait("overpass-api.de")
        try:
            resp = utils.retry(
                lambda: self._post_overpass(query),
                attempts=3, backoff_seconds=5, exceptions=(requests.RequestException,),
                logger=logger, what="overpass query",
            )
            return resp.json().get("elements", [])
        except Exception as exc:
            logger.warning("Overpass query failed: %s", exc)
            return []

    # public API

    def discover(self, location: str, industry: str, limit: int) -> list[RawBusiness]:
        tag_pairs = INDUSTRY_TAG_MAP.get(industry.lower().strip())
        if not tag_pairs:
            logger.warning(
                "No OpenStreetMap tag mapping for industry '%s' - skipping. "
                "Add it to discovery.INDUSTRY_TAG_MAP to support it.", industry
            )
            return []

        coords = self._geocode(location)
        if not coords:
            return []
        lat, lon = coords

        query = self._build_query(tag_pairs, lat, lon)
        elements = self._run_overpass(query)

        results: list[RawBusiness] = []
        seen_osm_ids = set()
        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name")
            if not name:
                continue
            osm_id = f'{el.get("type")}/{el.get("id")}'
            if osm_id in seen_osm_ids:
                continue
            seen_osm_ids.add(osm_id)

            if el.get("type") == "way" and "center" in el:
                el_lat, el_lon = el["center"]["lat"], el["center"]["lon"]
            else:
                el_lat, el_lon = el.get("lat"), el.get("lon")

            address_parts = [
                tags.get("addr:housenumber", ""),
                tags.get("addr:street", ""),
            ]
            street = " ".join(p for p in address_parts if p).strip()
            town = tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:suburb") or location
            postcode = tags.get("addr:postcode", "")
            full_address = ", ".join(p for p in [street, town, postcode] if p)

            website = tags.get("website") or tags.get("contact:website")
            phone = tags.get("phone") or tags.get("contact:phone")
            email = tags.get("email") or tags.get("contact:email")
            category = tags.get("craft") or tags.get("shop") or tags.get("office") or \
                tags.get("amenity") or tags.get("leisure") or industry

            source_url = f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}"

            results.append(RawBusiness(
                name=name.strip(),
                town=town.strip() if town else location,
                address=full_address,
                category=category,
                industry_searched=industry,
                phone=phone,
                website=website,
                email=email,
                lat=el_lat,
                lon=el_lon,
                source_name=self.name,
                source_url=source_url,
                raw_tags=tags,
            ))
            if len(results) >= limit:
                break

        return results


def is_national_chain(name: str, chain_keywords: list[str]) -> bool:
    lowered = name.lower()
    return any(keyword.lower() in lowered for keyword in chain_keywords)


def deduplicate_batch(businesses: list[RawBusiness]) -> list[RawBusiness]:
    """Remove near-duplicates within a single discovery batch (e.g. a node
    and a way representing the same premises)."""
    unique: list[RawBusiness] = []
    for biz in businesses:
        biz_domain = utils.registrable_domain(utils.extract_domain(biz.website)) if biz.website else None
        is_dup = False
        for existing in unique:
            existing_domain = utils.registrable_domain(utils.extract_domain(existing.website)) \
                if existing.website else None
            same_domain = biz_domain and existing_domain and biz_domain == existing_domain
            same_phone = (biz.phone and existing.phone and
                          utils.normalise_phone(biz.phone) == utils.normalise_phone(existing.phone))
            similar_name = utils.name_similarity(biz.name, existing.name) >= 0.9
            if same_domain or same_phone or (similar_name and biz.town == existing.town):
                is_dup = True
                break
        if not is_dup:
            unique.append(biz)
    return unique


class DiscoveryManager:
    def __init__(self, config: dict, rate_limiter: utils.RateLimiter):
        self.config = config
        self.sources: list[DiscoverySource] = [
            OverpassDiscoverySource(
                user_agent=config["user_agent"],
                request_delay_seconds=config["request_delay_seconds"],
                timeout=config["website_timeout_seconds"],
                radius_metres=config.get("search_radius_metres", 6000),
                rate_limiter=rate_limiter,
            )
        ]
        self.chain_keywords = config.get("national_chain_keywords", [])

    def discover(self, location: str, industry: str, limit: int) -> list[RawBusiness]:
        all_results: list[RawBusiness] = []
        for source in self.sources:
            try:
                results = source.discover(location, industry, limit)
                logger.info("  %s found %d candidate(s) for %s in %s",
                            source.name, len(results), industry, location)
                all_results.extend(results)
            except Exception as exc:
                logger.error("Discovery source %s failed for %s/%s: %s",
                             source.name, location, industry, exc)

        all_results = deduplicate_batch(all_results)
        before = len(all_results)
        all_results = [b for b in all_results if not is_national_chain(b.name, self.chain_keywords)]
        skipped = before - len(all_results)
        if skipped:
            logger.info("  Skipped %d likely national chain(s)", skipped)

        return all_results[:limit]
