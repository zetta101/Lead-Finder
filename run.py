#!/usr/bin/env python
"""Lead Finder - entry point.

Usage (from the project folder, inside the venv):

    python run.py
    python run.py --location Torquay
    python run.py --industry plumber
    python run.py --limit 20
    python run.py --force-refresh
    python run.py --export-only
    python run.py --no-screenshots

See README.md for full setup and usage instructions.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from leadfinder import company_checker, contact_extractor, database, discovery
from leadfinder import exporter, scorer, utils, website_checker, website_finder

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_PATH = BASE_DIR / "data" / "leads.db"
OUTPUT_PATH = BASE_DIR / "output" / "leads.xlsx"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
LOGS_DIR = BASE_DIR / "logs"

DEFAULT_SCORING_LEAD = {
    "no_website": 70, "website_unavailable": 65, "very_poor_mobile": 25,
    "very_slow_website": 20, "no_https": 20, "broken_internal_links": 15,
    "outdated_indicators": 15, "no_contact_form": 10, "no_domain_email": 10,
    "poor_accessibility": 10, "missing_title_or_meta": 5,
    "uses_free_email_provider": 15, "website_no_domain_email_bonus": 10,
    "no_website_free_email_bonus": 10, "professional_modern_website": -40,
}
DEFAULT_SCORING_WEBSITE = {
    "no_https_penalty": 20, "https_redirect_broken_penalty": 10,
    "missing_title_penalty": 8, "missing_meta_description_penalty": 6,
    "no_viewport_meta_penalty": 12, "mobile_overflow_penalty": 12,
    "slow_load_penalty": 15, "very_slow_load_penalty": 10,
    "broken_internal_link_penalty_each": 4, "broken_internal_link_penalty_max": 20,
    "broken_social_link_penalty_each": 3, "broken_social_link_penalty_max": 9,
    "no_favicon_penalty": 4, "no_contact_form_penalty": 8, "no_tel_link_penalty": 5,
    "no_email_link_penalty": 5, "no_cta_penalty": 6, "outdated_copyright_penalty": 8,
    "mixed_content_penalty": 10, "large_images_penalty_each": 3,
    "large_images_penalty_max": 9, "no_sitemap_penalty": 3, "no_robots_penalty": 2,
}
DEFAULT_PERFORMANCE = {
    "slow_load_seconds": 4.0, "very_slow_load_seconds": 7.0,
    "large_image_bytes": 500_000, "outdated_copyright_years_behind": 3,
}
DEFAULT_SCREENSHOT_SIZES = {
    "desktop_width": 1440, "desktop_height": 900, "mobile_width": 390, "mobile_height": 844,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: config.yaml not found at {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    config.setdefault("locations", [])
    config.setdefault("industries", [])
    config.setdefault("max_businesses_per_search", 25)
    config.setdefault("minimum_lead_score_for_screenshot", 60)
    config.setdefault("request_delay_seconds", 3)
    config.setdefault("website_timeout_seconds", 20)
    config.setdefault("website_rescan_interval_days", 14)
    config.setdefault("search_radius_metres", 6000)
    config.setdefault("max_pages_per_website", 3)
    config.setdefault("national_chain_keywords", [])
    config.setdefault("user_agent", "AlffiLeadFinder/1.0")
    config.setdefault("companies_house", {"enabled": True, "min_name_match_score": 0.72})

    scoring = config.setdefault("scoring", {})
    lead_weights = {**DEFAULT_SCORING_LEAD, **(scoring.get("lead") or {})}
    website_weights = {**DEFAULT_SCORING_WEBSITE, **(scoring.get("website") or {})}
    scoring["lead"] = lead_weights
    scoring["website"] = website_weights

    config["performance"] = {**DEFAULT_PERFORMANCE, **(config.get("performance") or {})}
    config["screenshots"] = {**DEFAULT_SCREENSHOT_SIZES, **(config.get("screenshots") or {})}
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alffi Lead Finder - discover and score potential web-design/hosting leads."
    )
    parser.add_argument("--location", help="Only process this location (overrides config.yaml list)")
    parser.add_argument("--industry", help="Only process this industry (overrides config.yaml list)")
    parser.add_argument("--limit", type=int, help="Max businesses per location/industry search")
    parser.add_argument("--force-refresh", action="store_true",
                         help="Re-analyse websites even if recently checked")
    parser.add_argument("--export-only", action="store_true",
                         help="Skip discovery/analysis - just export the current database to Excel")
    parser.add_argument("--no-screenshots", action="store_true", help="Never take screenshots")
    return parser.parse_args()


def process_business(raw: discovery.RawBusiness, db: database.Database,
                      finder: website_finder.WebsiteFinder,
                      extractor: contact_extractor.ContactExtractor,
                      checker: website_checker.WebsiteChecker,
                      ch_checker: company_checker.CompanyChecker,
                      config: dict, force_refresh: bool, no_screenshots: bool,
                      position: str, logger) -> dict:
    """Processes a single discovered business end to end. Returns a small
    stats dict for the run summary. Never raises - all failures are caught
    and logged so one bad website can't kill an overnight run."""
    stats = {"new": False, "leads_scored": False, "screenshot": False, "error": None}

    logger.info("%s Checking %s - %s", position, raw.name, raw.town)

    record = database.BusinessRecord(
        name=raw.name, town=raw.town, address=raw.address, category=raw.category,
        industry_searched=raw.industry_searched, phone=raw.phone, website=raw.website,
        email=raw.email, lat=raw.lat, lon=raw.lon,
        source_name=raw.source_name, source_url=raw.source_url,
    )

    try:
        business_id, is_new = db.upsert_business(record)
        stats["new"] = is_new
    except Exception as exc:
        logger.error("Failed to save business %s: %s", raw.name, exc)
        stats["error"] = str(exc)
        return stats

    if db.is_suppressed(raw.name, None, [raw.email] if raw.email else []):
        logger.info("  Suppressed (do-not-contact) - skipping analysis")
        db.mark_checked(business_id)
        return stats

    if not db.needs_refresh(business_id, config["website_rescan_interval_days"], force=force_refresh):
        logger.info("  Recently checked - skipping re-analysis")
        return stats

    try:
        website_result = finder.find(raw.name, raw.town, raw.phone, raw.website)
        website_id = db.save_website(
            business_id, website_result.url, website_result.status,
            website_result.confidence, website_result.notes, website_result.discovered_via,
        )

        if website_result.url:
            logger.info("  Website: %s (%s, confidence %d)",
                        website_result.url, website_result.status, website_result.confidence)
        else:
            logger.info("  No website found")

        domain = utils.registrable_domain(utils.extract_domain(website_result.url)) \
            if website_result.url else None

        analysable = website_result.status in (website_finder.STATUS_VERIFIED, website_finder.STATUS_GUESSED)

        # Re-check suppression now that we may know the domain.
        if analysable and db.is_suppressed(raw.name, domain, []):
            logger.info("  Suppressed (do-not-contact) - skipping contact/website analysis")
            db.mark_checked(business_id)
            return stats

        if raw.email and utils.is_valid_email(raw.email):
            db.add_contact(business_id, "EMAIL", raw.email, utils.classify_email(raw.email), raw.source_url)
        if raw.phone:
            db.add_contact(business_id, "PHONE", utils.normalise_phone(raw.phone), None, raw.source_url)

        scan = None
        if analysable:
            bundle = extractor.extract(website_result.url)
            for email, classification, source_url in bundle.emails:
                db.add_contact(business_id, "EMAIL", email, classification, source_url)
                logger.info("  Email found: %s", email)
            for phone, source_url in bundle.phones:
                db.add_contact(business_id, "PHONE", phone, None, source_url)
            if bundle.contact_page_url:
                db.add_contact(business_id, "CONTACT_PAGE", bundle.contact_page_url, None, website_result.url)

            scan = checker.scan(website_result.url)
            db.save_scan(website_id, scan)

        if config["companies_house"].get("enabled", True):
            match = ch_checker.lookup(raw.name, raw.town)
            if match and match.confidence >= config["companies_house"].get("min_name_match_score", 0.72):
                db.save_ch_match(business_id, match.company_name, match.company_number,
                                  match.company_status, match.registered_office,
                                  match.sic_codes, match.confidence)
                logger.info("  Companies House match: %s (%s)", match.company_name, match.company_number)

        contacts = db.get_contacts(business_id)
        classifications = [c["email_classification"] for c in contacts
                            if c["contact_type"] == "EMAIL" and c["email_classification"]]
        has_domain_email, uses_free_email = scorer.classify_lead_reasons(classifications)

        website_score = None
        if scan:
            website_score, _ = scorer.score_website(
                scan, config["scoring"]["website"], config["performance"]
            )

        lead_score, lead_reasons = scorer.score_lead(
            website_result.status, website_score, scan, has_domain_email, uses_free_email,
            config["scoring"]["lead"], config["performance"],
        )
        db.save_scores(business_id, website_score, lead_score, lead_reasons)
        stats["leads_scored"] = True

        logger.info("  Website score: %s", website_score if website_score is not None else "N/A")
        logger.info("  Lead score: %s", lead_score)

        db.mark_checked(business_id)

        if not no_screenshots and analysable and website_result.url:
            should_screenshot = (
                lead_score >= config["minimum_lead_score_for_screenshot"]
                or (scan and (scan.get("mobile_overflow") or scan.get("broken_internal_links", 0) > 0
                               or scan.get("error")))
            )
            if should_screenshot:
                folder_name = utils.sanitise_filename(domain or raw.name)
                folder = SCREENSHOTS_DIR / folder_name
                folder.mkdir(parents=True, exist_ok=True)
                desktop_path = folder / "desktop.png"
                mobile_path = folder / "mobile.png"
                shots = config["screenshots"]
                ok = checker.screenshot(
                    website_result.url, str(desktop_path), str(mobile_path),
                    (shots["desktop_width"], shots["desktop_height"]),
                    (shots["mobile_width"], shots["mobile_height"]),
                )
                if ok:
                    db.save_screenshot(business_id, str(desktop_path), str(mobile_path))
                    stats["screenshot"] = True
                    logger.info("  Screenshots saved: %s", folder)

    except Exception as exc:
        logger.error("  Error processing %s: %s", raw.name, exc)
        stats["error"] = str(exc)
        db.mark_checked(business_id)

    return stats


def run_discovery_and_analysis(config: dict, args: argparse.Namespace, db: database.Database, logger) -> dict:
    rate_limiter = utils.RateLimiter(config["request_delay_seconds"])
    discovery_manager = discovery.DiscoveryManager(config, rate_limiter)
    finder = website_finder.WebsiteFinder(
        user_agent=config["user_agent"], timeout=config["website_timeout_seconds"],
        rate_limiter=rate_limiter,
    )
    extractor = contact_extractor.ContactExtractor(
        user_agent=config["user_agent"], timeout=config["website_timeout_seconds"],
        rate_limiter=rate_limiter, max_pages=config["max_pages_per_website"],
    )
    ch_checker = company_checker.CompanyChecker(
        timeout=config["website_timeout_seconds"], rate_limiter=rate_limiter,
        min_name_match_score=config["companies_house"].get("min_name_match_score", 0.72),
        enabled=config["companies_house"].get("enabled", True),
    )

    locations = [args.location] if args.location else config["locations"]
    industries = [args.industry] if args.industry else config["industries"]
    limit = args.limit or config["max_businesses_per_search"]

    if not locations or not industries:
        logger.error("No locations/industries configured. Edit config.yaml or pass --location/--industry.")
        return {"total": 0, "new": 0, "scored": 0, "screenshots": 0, "errors": 0}

    totals = {"total": 0, "new": 0, "scored": 0, "screenshots": 0, "errors": 0}

    with website_checker.WebsiteChecker(
        user_agent=config["user_agent"], timeout_seconds=config["website_timeout_seconds"],
        rate_limiter=rate_limiter,
        large_image_bytes=config["performance"]["large_image_bytes"],
    ) as checker:
        for location in locations:
            for industry in industries:
                logger.info("")
                logger.info("=== %s / %s ===", location, industry)
                try:
                    candidates = discovery_manager.discover(location, industry, limit)
                except Exception as exc:
                    logger.error("Discovery failed for %s/%s: %s", location, industry, exc)
                    continue

                for idx, raw in enumerate(candidates, start=1):
                    position = f"[{idx}/{len(candidates)}]"
                    stats = process_business(
                        raw, db, finder, extractor, checker, ch_checker, config,
                        args.force_refresh, args.no_screenshots, position, logger,
                    )
                    totals["total"] += 1
                    totals["new"] += int(stats["new"])
                    totals["scored"] += int(stats["leads_scored"])
                    totals["screenshots"] += int(stats["screenshot"])
                    if stats["error"]:
                        totals["errors"] += 1

    return totals


def print_summary(totals: dict, db: database.Database, output_path: Path, logger) -> None:
    all_rows = db.iter_businesses()
    best = [r for r in all_rows if (r["lead_score"] or 0) >= 60]
    no_website = [r for r in all_rows if r["website_status"] == "NO_WEBSITE_FOUND"]

    logger.info("")
    logger.info("================ RUN SUMMARY ================")
    logger.info("Businesses checked this run : %d", totals["total"])
    logger.info("New businesses discovered   : %d", totals["new"])
    logger.info("Websites/leads scored       : %d", totals["scored"])
    logger.info("Screenshots taken           : %d", totals["screenshots"])
    logger.info("Errors (logged, non-fatal)  : %d", totals["errors"])
    logger.info("-----------------------------------------------")
    logger.info("Total businesses in database: %d", len(all_rows))
    logger.info("Leads with score >= 60      : %d", len(best))
    logger.info("Businesses with no website  : %d", len(no_website))
    logger.info("-----------------------------------------------")
    logger.info("Excel workbook: %s", output_path)
    logger.info("Log file      : %s", LOGS_DIR / "leadfinder.log")
    logger.info("Review the workbook manually before contacting anyone.")
    logger.info("=================================================")


def main() -> None:
    args = parse_args()
    config = load_config()
    logger = utils.setup_logging(LOGS_DIR)

    db = database.Database(DB_PATH)
    start_time = time.time()

    try:
        if args.export_only:
            logger.info("Export-only mode - skipping discovery/analysis.")
            totals = {"total": 0, "new": 0, "scored": 0, "screenshots": 0, "errors": 0}
        else:
            totals = run_discovery_and_analysis(config, args, db, logger)

        exporter.export_to_excel(
            db, OUTPUT_PATH,
            best_lead_threshold=config["minimum_lead_score_for_screenshot"],
            poor_website_threshold=50,
        )
        print_summary(totals, db, OUTPUT_PATH, logger)
    finally:
        db.close()

    elapsed = time.time() - start_time
    logger.info("Done in %.1f seconds.", elapsed)


if __name__ == "__main__":
    main()
