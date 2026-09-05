"""SQLite persistence layer. Everything goes through the Database class."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import utils

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    trading_name TEXT,
    town TEXT,
    address TEXT,
    category TEXT,
    industry_searched TEXT,
    phone TEXT,
    phone_normalised TEXT,
    lat REAL,
    lon REAL,
    dedupe_key TEXT,
    is_limited_company INTEGER,
    companies_house_number TEXT,
    website_status TEXT,
    website_score INTEGER,
    lead_score INTEGER,
    lead_reasons TEXT,
    first_seen TEXT,
    last_seen TEXT,
    last_checked TEXT
);

CREATE INDEX IF NOT EXISTS idx_businesses_dedupe ON businesses(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_businesses_town ON businesses(town);

CREATE TABLE IF NOT EXISTS websites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    url TEXT,
    domain TEXT,
    status TEXT,          -- VERIFIED / GUESSED / UNREACHABLE / NO_WEBSITE_FOUND
    confidence INTEGER,
    verification_notes TEXT,
    discovered_via TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS website_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    website_id INTEGER NOT NULL REFERENCES websites(id),
    scanned_at TEXT,
    http_status INTEGER,
    https INTEGER,
    https_redirect_ok INTEGER,
    title TEXT,
    meta_description TEXT,
    has_viewport_meta INTEGER,
    mobile_friendly INTEGER,
    mobile_overflow INTEGER,
    load_time_seconds REAL,
    page_size_bytes INTEGER,
    num_requests INTEGER,
    broken_internal_links INTEGER,
    broken_social_links INTEGER,
    has_favicon INTEGER,
    has_contact_form INTEGER,
    has_tel_link INTEGER,
    has_email_link INTEGER,
    has_cta INTEGER,
    copyright_year INTEGER,
    mixed_content INTEGER,
    large_images_count INTEGER,
    cms_detected TEXT,
    robots_txt_present INTEGER,
    sitemap_present INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    contact_type TEXT,     -- EMAIL / PHONE / CONTACT_PAGE
    value TEXT,
    email_classification TEXT,
    source_url TEXT,
    first_seen TEXT,
    UNIQUE(business_id, contact_type, value)
);

CREATE TABLE IF NOT EXISTS companies_house_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    company_name TEXT,
    company_number TEXT,
    company_status TEXT,
    registered_office TEXT,
    sic_codes TEXT,
    match_confidence REAL,
    matched_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    source_name TEXT,
    source_url TEXT,
    retrieved_at TEXT
);

CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    desktop_path TEXT,
    mobile_path TEXT,
    taken_at TEXT
);

CREATE TABLE IF NOT EXISTS do_not_contact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name TEXT,
    domain TEXT,
    email TEXT,
    reason TEXT,
    date_added TEXT
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


@dataclass
class BusinessRecord:
    """In-memory representation used while processing a single business."""
    name: str
    trading_name: Optional[str] = None
    town: Optional[str] = None
    address: Optional[str] = None
    category: Optional[str] = None
    industry_searched: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    source_name: str = ""
    source_url: str = ""
    is_limited_company: Optional[bool] = None
    id: Optional[int] = None
    raw_tags: dict = field(default_factory=dict)


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

# Dedupe / upsert
    @staticmethod
    def make_dedupe_key(name: str, town: str, phone: Optional[str], domain: Optional[str]) -> str:
        parts = [utils.normalise_name(name), (town or "").strip().lower()]
        if domain:
            parts.append(domain)
        elif phone:
            parts.append(utils.normalise_phone(phone))
        return "|".join(p for p in parts if p)

    def find_existing_business(self, name: str, town: str, phone: Optional[str],
                                domain: Optional[str]) -> Optional[sqlite3.Row]:
        """Try exact dedupe key first, then fuzzy name+town match."""
        key = self.make_dedupe_key(name, town, phone, domain)
        row = self.conn.execute(
            "SELECT * FROM businesses WHERE dedupe_key = ?", (key,)
        ).fetchone()
        if row:
            return row

        # Fuzzy fallback: same town, similar name, or matching normalised phone.
        candidates = self.conn.execute(
            "SELECT * FROM businesses WHERE town = ? COLLATE NOCASE", (town,)
        ).fetchall()
        norm_phone = utils.normalise_phone(phone) if phone else None
        for cand in candidates:
            if norm_phone and cand["phone_normalised"] and cand["phone_normalised"] == norm_phone:
                return cand
            if utils.name_similarity(name, cand["name"]) >= 0.88:
                return cand
        return None

    def upsert_business(self, record: BusinessRecord) -> tuple[int, bool]:
        """Insert or update a business. Returns (business_id, is_new)."""
        domain = utils.extract_domain(record.website) if record.website else None
        domain = utils.registrable_domain(domain) if domain else None
        existing = self.find_existing_business(record.name, record.town or "", record.phone, domain)
        now = _now()

        if existing:
            business_id = existing["id"]
            with self.conn:
                self.conn.execute(
                    """UPDATE businesses SET last_seen = ?,
                       address = COALESCE(NULLIF(?, ''), address),
                       phone = COALESCE(NULLIF(?, ''), phone),
                       phone_normalised = COALESCE(NULLIF(?, ''), phone_normalised),
                       category = COALESCE(NULLIF(?, ''), category)
                       WHERE id = ?""",
                    (now, record.address or "", record.phone or "",
                     utils.normalise_phone(record.phone) if record.phone else "",
                     record.category or "", business_id),
                )
            return business_id, False

        dedupe_key = self.make_dedupe_key(record.name, record.town or "", record.phone, domain)
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO businesses
                   (name, trading_name, town, address, category, industry_searched,
                    phone, phone_normalised, lat, lon, dedupe_key, is_limited_company,
                    first_seen, last_seen, last_checked)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (record.name, record.trading_name, record.town, record.address,
                 record.category, record.industry_searched, record.phone,
                 utils.normalise_phone(record.phone) if record.phone else None,
                 record.lat, record.lon, dedupe_key,
                 int(record.is_limited_company) if record.is_limited_company is not None else None,
                 now, now),
            )
            business_id = cur.lastrowid
        if record.source_name:
            self.add_source(business_id, record.source_name, record.source_url)
        return business_id, True

# Refresh interval logic
    def needs_refresh(self, business_id: int, rescan_interval_days: int, force: bool = False) -> bool:
        if force:
            return True
        row = self.conn.execute(
            "SELECT last_checked FROM businesses WHERE id = ?", (business_id,)
        ).fetchone()
        if not row or not row["last_checked"]:
            return True
        try:
            last_checked = datetime.fromisoformat(row["last_checked"])
        except ValueError:
            return True
        return datetime.utcnow() - last_checked >= timedelta(days=rescan_interval_days)

    def mark_checked(self, business_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE businesses SET last_checked = ? WHERE id = ?", (_now(), business_id)
            )

# Websites
    def save_website(self, business_id: int, url: Optional[str], status: str,
                      confidence: int, notes: str, discovered_via: str) -> int:
        domain = utils.extract_domain(url) if url else None
        now = _now()
        existing = self.conn.execute(
            "SELECT id FROM websites WHERE business_id = ?", (business_id,)
        ).fetchone()
        if existing:
            with self.conn:
                self.conn.execute(
                    """UPDATE websites SET url=?, domain=?, status=?, confidence=?,
                       verification_notes=?, discovered_via=?, updated_at=?
                       WHERE id=?""",
                    (url, domain, status, confidence, notes, discovered_via, now, existing["id"]),
                )
            website_id = existing["id"]
        else:
            with self.conn:
                cur = self.conn.execute(
                    """INSERT INTO websites
                       (business_id, url, domain, status, confidence, verification_notes,
                        discovered_via, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (business_id, url, domain, status, confidence, notes, discovered_via, now, now),
                )
                website_id = cur.lastrowid
        with self.conn:
            self.conn.execute(
                "UPDATE businesses SET website_status = ? WHERE id = ?", (status, business_id)
            )
        return website_id

    def get_website(self, business_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM websites WHERE business_id = ?", (business_id,)
        ).fetchone()

# Website scans
    def save_scan(self, website_id: int, scan: dict[str, Any]) -> int:
        scan = dict(scan)
        scan["website_id"] = website_id
        scan["scanned_at"] = _now()
        columns = ", ".join(scan.keys())
        placeholders = ", ".join(["?"] * len(scan))
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO website_scans ({columns}) VALUES ({placeholders})",
                tuple(scan.values()),
            )
        return cur.lastrowid

    def get_latest_scan(self, website_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM website_scans WHERE website_id = ? ORDER BY id DESC LIMIT 1",
            (website_id,),
        ).fetchone()

# Contacts
    def add_contact(self, business_id: int, contact_type: str, value: str,
                     email_classification: Optional[str], source_url: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO contacts
                   (business_id, contact_type, value, email_classification, source_url, first_seen)
                   VALUES (?,?,?,?,?,?)""",
                (business_id, contact_type, value, email_classification, source_url, _now()),
            )

    def get_contacts(self, business_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contacts WHERE business_id = ?", (business_id,)
        ).fetchall()

# Companies House
    def save_ch_match(self, business_id: int, company_name: str, company_number: str,
                       company_status: str, registered_office: str, sic_codes: list[str],
                       match_confidence: float) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO companies_house_matches
                   (business_id, company_name, company_number, company_status,
                    registered_office, sic_codes, match_confidence, matched_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (business_id, company_name, company_number, company_status,
                 registered_office, json.dumps(sic_codes), match_confidence, _now()),
            )
            self.conn.execute(
                """UPDATE businesses SET is_limited_company = 1, companies_house_number = ?
                   WHERE id = ?""",
                (company_number, business_id),
            )

    def get_ch_match(self, business_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM companies_house_matches WHERE business_id = ? ORDER BY id DESC LIMIT 1",
            (business_id,),
        ).fetchone()

# Sources
    def add_source(self, business_id: int, source_name: str, source_url: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO sources (business_id, source_name, source_url, retrieved_at)
                   VALUES (?,?,?,?)""",
                (business_id, source_name, source_url, _now()),
            )

    def get_primary_source(self, business_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sources WHERE business_id = ? ORDER BY id ASC LIMIT 1",
            (business_id,),
        ).fetchone()

# Screenshots
    def save_screenshot(self, business_id: int, desktop_path: Optional[str],
                         mobile_path: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO screenshots (business_id, desktop_path, mobile_path, taken_at)
                   VALUES (?,?,?,?)""",
                (business_id, desktop_path, mobile_path, _now()),
            )

    def get_latest_screenshot(self, business_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM screenshots WHERE business_id = ? ORDER BY id DESC LIMIT 1",
            (business_id,),
        ).fetchone()

# Scoring persistence
    def save_scores(self, business_id: int, website_score: Optional[int],
                     lead_score: int, reasons: list[str]) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE businesses SET website_score = ?, lead_score = ?, lead_reasons = ?
                   WHERE id = ?""",
                (website_score, lead_score, json.dumps(reasons), business_id),
            )

# Do-not-contact
    def add_do_not_contact(self, business_name: Optional[str], domain: Optional[str],
                            email: Optional[str], reason: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO do_not_contact (business_name, domain, email, reason, date_added)
                   VALUES (?,?,?,?,?)""",
                (business_name, domain, email, reason, _now()),
            )

    def list_do_not_contact(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM do_not_contact ORDER BY id").fetchall()

    def is_suppressed(self, business_name: str, domain: Optional[str], emails: list[str]) -> bool:
        rows = self.list_do_not_contact()
        norm_name = utils.normalise_name(business_name)
        for row in rows:
            if row["domain"] and domain and row["domain"].lower() == domain.lower():
                return True
            if row["email"] and row["email"].lower() in [e.lower() for e in emails]:
                return True
            if row["business_name"] and utils.normalise_name(row["business_name"]) == norm_name:
                return True
        return False

# Bulk reads for CLI filtering / export
    def iter_businesses(self, location: Optional[str] = None,
                         industry: Optional[str] = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM businesses WHERE 1=1"
        params: list[Any] = []
        if location:
            query += " AND town = ? COLLATE NOCASE"
            params.append(location)
        if industry:
            query += " AND industry_searched = ? COLLATE NOCASE"
            params.append(industry)
        query += " ORDER BY lead_score DESC NULLS LAST, id"
        try:
            return self.conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            # SQLite < 3.30 lacks NULLS LAST
            query = query.replace(" NULLS LAST", "")
            return self.conn.execute(query, params).fetchall()

    def get_business(self, business_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM businesses WHERE id = ?", (business_id,)
        ).fetchone()
