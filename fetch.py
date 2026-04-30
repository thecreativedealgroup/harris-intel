"""
Harris County, TX — Motivated Seller Lead Scraper
Clerk Portal: https://www.cclerk.hctx.net/PublicRecords.aspx
Runs daily via GitHub Actions, outputs records.json + GHL CSV
"""

import asyncio
import json
import csv
import io
import os
import re
import sys
import time
import zipfile
import logging
import hashlib
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Optional dbfread ──────────────────────────────────────────────────────────
try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hc-scraper")

# ── Config ────────────────────────────────────────────────────────────────────
CLERK_URL   = "https://www.cclerk.hctx.net/PublicRecords.aspx"
APPR_BASE   = "https://hcad.org"          # Harris County Appraisal District
APPR_BULK   = "https://hcad.org/hcad-resources/hcad-appraisal-codes-and-data/hcad-data-and-records/"
COUNTY      = "Harris"
STATE       = "TX"
LOOKBACK    = 7   # days

DOC_TYPES = {
    "LP":       ("LP",      "Lis Pendens"),
    "NOFC":     ("FC",      "Notice of Foreclosure"),
    "TAXDEED":  ("TAXDEED", "Tax Deed"),
    "JUD":      ("JUD",     "Judgment"),
    "CCJ":      ("JUD",     "Certified Judgment"),
    "DRJUD":    ("JUD",     "Domestic Judgment"),
    "LNCORPTX": ("TAXLIEN", "Corp Tax Lien"),
    "LNIRS":    ("TAXLIEN", "IRS Lien"),
    "LNFED":    ("TAXLIEN", "Federal Lien"),
    "LN":       ("LIEN",    "Lien"),
    "LNMECH":   ("LIEN",    "Mechanic Lien"),
    "LNHOA":    ("LIEN",    "HOA Lien"),
    "MEDLN":    ("LIEN",    "Medicaid Lien"),
    "PRO":      ("PRO",     "Probate"),
    "NOC":      ("NOC",     "Notice of Commencement"),
    "RELLP":    ("RELLP",   "Release Lis Pendens"),
}

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 5  # seconds

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).resolve().parent.parent
DASH_DIR  = ROOT / "dashboard"
DATA_DIR  = ROOT / "data"
CACHE_DIR = ROOT / ".cache"
for d in (DASH_DIR, DATA_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# PARCEL LOOKUP (HCAD bulk data)
# ─────────────────────────────────────────────────────────────────────────────

class ParcelLookup:
    """
    Downloads the HCAD bulk parcel export (DBF or CSV), builds an
    owner-name → address lookup with multiple name variants.
    """

    PARCEL_CACHE = CACHE_DIR / "parcels.json"
    # Cache is valid for 24 h so daily runs don't re-download
    CACHE_TTL    = 86_400

    def __init__(self):
        self.index: dict[str, dict] = {}  # normalised_name → record

    # ── public ────────────────────────────────────────────────────────────────

    def load(self):
        if self._cache_fresh():
            log.info("Parcel cache is fresh — loading from disk")
            self._load_cache()
            return
        log.info("Fetching HCAD bulk parcel data …")
        try:
            self._download_and_index()
            self._save_cache()
        except Exception as exc:
            log.warning("Parcel download failed (%s); trying cache", exc)
            if self.PARCEL_CACHE.exists():
                self._load_cache()

    def lookup(self, owner_name: str) -> dict | None:
        """Return address dict for owner name, trying multiple variants."""
        if not owner_name:
            return None
        for variant in self._name_variants(owner_name):
            key = self._normalise(variant)
            if key in self.index:
                return self.index[key]
        return None

    # ── private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise(name: str) -> str:
        return re.sub(r"\s+", " ", name.upper().strip())

    @staticmethod
    def _name_variants(name: str) -> list[str]:
        """Generate 'FIRST LAST', 'LAST FIRST', 'LAST, FIRST' variants."""
        name = name.strip()
        variants = [name]
        # If comma present: "LAST, FIRST" → also try "FIRST LAST"
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            if len(parts) == 2:
                variants.append(f"{parts[1]} {parts[0]}")
                variants.append(f"{parts[0]} {parts[1]}")
        else:
            tokens = name.split()
            if len(tokens) >= 2:
                first, *rest = tokens
                last = rest[-1]
                variants.append(f"{last} {first}")
                variants.append(f"{last}, {first}")
        return variants

    def _cache_fresh(self) -> bool:
        if not self.PARCEL_CACHE.exists():
            return False
        age = time.time() - self.PARCEL_CACHE.stat().st_mtime
        return age < self.CACHE_TTL

    def _load_cache(self):
        with open(self.PARCEL_CACHE) as f:
            self.index = json.load(f)
        log.info("Loaded %d parcel records from cache", len(self.index))

    def _save_cache(self):
        with open(self.PARCEL_CACHE, "w") as f:
            json.dump(self.index, f, separators=(",", ":"))
        log.info("Saved %d parcel records to cache", len(self.index))

    def _download_and_index(self):
        """
        HCAD publishes a zip of DBF/CSV files at hcad.org.
        We try several known URL patterns and fall back gracefully.
        """
        year = datetime.now().year
        candidate_urls = [
            f"https://pdata.hcad.org/download/{year}/comm_bldg_section.zip",
            f"https://pdata.hcad.org/download/{year}/real_acct_owner.zip",
            f"https://pdata.hcad.org/data/Acct/real_acct.zip",
            f"https://pdata.hcad.org/Desc/real_acct.zip",
        ]
        # Try account/owner CSV which is the most reliable public file
        acct_urls = [
            f"https://pdata.hcad.org/download/{year}/real_acct_owner.zip",
            f"https://pdata.hcad.org/Desc/real_acct.zip",
        ]
        for url in acct_urls:
            try:
                self._fetch_zip_and_index(url)
                if self.index:
                    return
            except Exception as exc:
                log.debug("URL %s failed: %s", url, exc)

        # Fallback: scrape the HCAD data page for any downloadable zip
        self._scrape_hcad_page_for_zip()

    def _fetch_zip_and_index(self, url: str):
        log.info("Downloading parcel zip: %s", url)
        resp = _get_with_retry(url, stream=True)
        raw = resp.content
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            log.debug("Zip contents: %s", names)
            # Prefer files with 'acct' or 'owner' in the name
            chosen = next(
                (n for n in names if any(k in n.lower() for k in ("acct", "owner", "real"))),
                names[0] if names else None,
            )
            if not chosen:
                raise ValueError("Empty zip")
            data = zf.read(chosen)

        if chosen.lower().endswith(".dbf"):
            self._index_dbf(data)
        else:
            self._index_csv(data)

    def _index_dbf(self, raw: bytes):
        if not HAS_DBF:
            log.warning("dbfread not installed; skipping DBF parcel data")
            return
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".dbf", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            table = DBF(tmp_path, load=True, ignore_missing_memofile=True)
            cols  = {f.name.upper() for f in table.fields}
            self._index_records(table, cols)
        finally:
            os.unlink(tmp_path)

    def _index_csv(self, raw: bytes):
        text   = raw.decode("latin-1", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        cols   = set(reader.fieldnames or [])
        self._index_records(reader, cols)

    def _index_records(self, records, cols):
        """Map column names to canonical keys, index by owner name variants."""
        def col(*candidates):
            for c in candidates:
                if c.upper() in {x.upper() for x in cols}:
                    return c
            return None

        owner_col   = col("OWNER", "OWN1", "OWNER_NAME")
        site_addr   = col("SITE_ADDR", "SITEADDR", "SITUS_ADDR", "PROP_ADDR")
        site_city   = col("SITE_CITY", "SITECITY", "SITUS_CITY")
        site_zip    = col("SITE_ZIP",  "SITEZIP",  "SITUS_ZIP")
        mail_addr   = col("ADDR_1", "MAILADR1", "MAIL_ADDR", "MAIL_ADDR1")
        mail_city   = col("CITY",   "MAILCITY", "MAIL_CITY")
        mail_state  = col("STATE",  "MAILSTATE","MAIL_STATE")
        mail_zip    = col("ZIP",    "MAILZIP",  "MAIL_ZIP")

        count = 0
        for row in records:
            try:
                def g(c):
                    if not c:
                        return ""
                    v = row[c] if isinstance(row, dict) else getattr(row, c, "")
                    return str(v).strip() if v else ""

                owner = g(owner_col)
                if not owner:
                    continue
                rec = {
                    "prop_address": g(site_addr),
                    "prop_city":    g(site_city) or COUNTY,
                    "prop_state":   STATE,
                    "prop_zip":     g(site_zip),
                    "mail_address": g(mail_addr),
                    "mail_city":    g(mail_city),
                    "mail_state":   g(mail_state) or STATE,
                    "mail_zip":     g(mail_zip),
                }
                for variant in ParcelLookup._name_variants(owner):
                    key = ParcelLookup._normalise(variant)
                    if key not in self.index:
                        self.index[key] = rec
                count += 1
            except Exception:
                pass
        log.info("Indexed %d parcel owner records", count)

    def _scrape_hcad_page_for_zip(self):
        log.info("Scraping HCAD data page for bulk zip …")
        try:
            resp = _get_with_retry(APPR_BULK)
            soup = BeautifulSoup(resp.text, "lxml")
            links = soup.find_all("a", href=re.compile(r"\.zip", re.I))
            for link in links:
                href = link.get("href", "")
                if not href.startswith("http"):
                    href = APPR_BASE + href
                try:
                    self._fetch_zip_and_index(href)
                    if self.index:
                        return
                except Exception as exc:
                    log.debug("Bulk zip %s failed: %s", href, exc)
        except Exception as exc:
            log.warning("HCAD page scrape failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# CLERK SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

class ClerkScraper:
    """
    Uses Playwright to drive the Harris County Clerk public records portal.
    Searches each doc type code for the last LOOKBACK days.
    """

    BASE = CLERK_URL

    def __init__(self, start_date: str, end_date: str):
        self.start = start_date   # MM/DD/YYYY
        self.end   = end_date
        self.results: list[dict] = []

    async def run(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx     = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = await ctx.new_page()
            await page.goto(self.BASE, wait_until="domcontentloaded", timeout=60_000)

            for code in DOC_TYPES:
                for attempt in range(1, RETRY_ATTEMPTS + 1):
                    try:
                        rows = await self._search_doc_type(page, code)
                        self.results.extend(rows)
                        log.info("  %-10s → %d records", code, len(rows))
                        break
                    except PWTimeout:
                        log.warning("  %-10s timeout (attempt %d/%d)", code, attempt, RETRY_ATTEMPTS)
                        if attempt < RETRY_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAY)
                    except Exception as exc:
                        log.warning("  %-10s error: %s (attempt %d/%d)", code, exc, attempt, RETRY_ATTEMPTS)
                        if attempt < RETRY_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAY)

            await browser.close()

    async def _search_doc_type(self, page, code: str) -> list[dict]:
        """Fill the search form, submit, collect all result pages."""
        await page.goto(self.BASE, wait_until="domcontentloaded", timeout=45_000)

        # ── Fill form fields (field names inferred from portal inspection) ──
        # The portal uses ASP.NET WebForms with various input IDs.
        # We try to fill whichever inputs are present.
        await self._try_fill(page, ["#txtDocType", "[name*='DocType']", "[id*='DocType']"], code)
        await self._try_fill(page, ["#txtDateFrom","[name*='DateFrom']","[id*='DateFrom']"], self.start)
        await self._try_fill(page, ["#txtDateTo",  "[name*='DateTo']",  "[id*='DateTo']"],   self.end)

        # Submit — try search button by various selectors
        for sel in ["#btnSearch", "input[value='Search']", "button:has-text('Search')", "[id*='Search']"]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    break
            except Exception:
                pass

        await page.wait_for_load_state("domcontentloaded", timeout=30_000)

        rows = []
        while True:
            page_rows = await self._parse_results_page(page, code)
            rows.extend(page_rows)

            # Pagination — look for "Next" link
            next_sel = "a:has-text('Next'), input[value='Next >'], [id*='Next']"
            try:
                nxt = page.locator(next_sel).first
                if await nxt.is_visible(timeout=2_000):
                    await nxt.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                else:
                    break
            except Exception:
                break

        return rows

    async def _try_fill(self, page, selectors: list[str], value: str):
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_500):
                    await el.triple_click()
                    await el.fill(value)
                    return
            except Exception:
                pass

    async def _parse_results_page(self, page, code: str) -> list[dict]:
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")
        rows = []

        # Results table — look for <table> rows with recognisable columns
        tables = soup.find_all("table")
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(k in " ".join(headers) for k in ("doc", "date", "grantor", "type")):
                continue
            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all(["td", "th"])
                if len(cells) < 3:
                    continue
                try:
                    row = self._extract_row(cells, headers, code, page.url)
                    if row:
                        rows.append(row)
                except Exception:
                    pass
        return rows

    def _extract_row(self, cells, headers: list[str], code: str, page_url: str) -> dict | None:
        def cell(idx: int) -> str:
            if idx < len(cells):
                return cells[idx].get_text(separator=" ", strip=True)
            return ""

        def find_col(*keywords) -> str:
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h:
                        return cell(i)
            return ""

        doc_num  = find_col("doc", "instrument", "num")
        doc_type = find_col("type") or code
        filed    = find_col("date", "filed", "record")
        grantor  = find_col("grantor", "owner", "name")
        grantee  = find_col("grantee", "party")
        legal    = find_col("legal", "desc")
        amount_s = find_col("amount", "debt", "value")

        if not doc_num and not filed:
            return None

        # Extract href for direct link
        link = ""
        for a in cells[0].find_all("a") if cells else []:
            href = a.get("href", "")
            if href:
                if href.startswith("http"):
                    link = href
                else:
                    from urllib.parse import urljoin
                    link = urljoin(CLERK_URL, href)
                break

        amount = _parse_amount(amount_s)
        cat, cat_label = DOC_TYPES.get(code, (code, code))

        return {
            "doc_num":   doc_num,
            "doc_type":  code,
            "filed":     _normalise_date(filed),
            "cat":       cat,
            "cat_label": cat_label,
            "owner":     grantor,
            "grantee":   grantee,
            "amount":    amount,
            "legal":     legal,
            "clerk_url": link or page_url,
            # address fields filled later by parcel lookup
            "prop_address": "", "prop_city": "", "prop_state": STATE, "prop_zip": "",
            "mail_address": "", "mail_city": "", "mail_state": STATE, "mail_zip": "",
        }


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def build_flags(rec: dict, week_cutoff: str) -> list[str]:
    flags = []
    code  = rec.get("doc_type", "")
    cat   = rec.get("cat", "")
    owner = rec.get("owner", "").upper()

    if code in ("LP",):                             flags.append("Lis pendens")
    if code in ("NOFC", "TAXDEED"):                 flags.append("Pre-foreclosure")
    if cat == "JUD":                                flags.append("Judgment lien")
    if cat == "TAXLIEN":                            flags.append("Tax lien")
    if code in ("LNMECH",):                         flags.append("Mechanic lien")
    if code in ("PRO",):                            flags.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|LLP)\b", owner):
        flags.append("LLC / corp owner")
    if rec.get("filed", "") >= week_cutoff:         flags.append("New this week")
    return flags


def score_record(rec: dict, flags: list[str], week_cutoff: str) -> int:
    s = 30
    s += len(flags) * 10

    # LP + foreclosure combo
    types = {rec.get("doc_type",""), rec.get("cat","")}
    if "LP" in types and ("FC" in types or "NOFC" in types):
        s += 20

    amount = rec.get("amount") or 0
    if amount > 100_000: s += 15
    elif amount > 50_000: s += 10

    if rec.get("filed","") >= week_cutoff: s += 5
    if rec.get("prop_address") or rec.get("mail_address"): s += 5

    return min(s, 100)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_with_retry(url: str, **kwargs) -> requests.Response:
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    raise last_exc


def _parse_amount(s: str) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _normalise_date(s: str) -> str:
    """Return YYYY-MM-DD or empty string."""
    if not s:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s.strip()


def _split_name(full: str) -> tuple[str, str]:
    """Best-effort split of 'LAST, FIRST' or 'FIRST LAST' → (first, last)."""
    if not full:
        return "", ""
    if "," in full:
        parts = [p.strip() for p in full.split(",", 1)]
        return parts[1], parts[0]
    tokens = full.split()
    if len(tokens) == 1:
        return "", tokens[0]
    return tokens[0].title(), " ".join(tokens[1:]).title()


def _dedup(records: list[dict]) -> list[dict]:
    seen = set()
    out  = []
    for r in records:
        key = (r.get("doc_num",""), r.get("doc_type",""), r.get("filed",""))
        h   = hashlib.md5(str(key).encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# GHL CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────

GHL_COLUMNS = [
    "First Name", "Last Name", "Mailing Address", "Mailing City",
    "Mailing State", "Mailing Zip", "Property Address", "Property City",
    "Property State", "Property Zip", "Lead Type", "Document Type",
    "Date Filed", "Document Number", "Amount/Debt Owed", "Seller Score",
    "Motivated Seller Flags", "Source", "Public Records URL",
]


def export_ghl_csv(records: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_COLUMNS)
        writer.writeheader()
        for r in records:
            first, last = _split_name(r.get("owner",""))
            writer.writerow({
                "First Name":           first,
                "Last Name":            last,
                "Mailing Address":      r.get("mail_address",""),
                "Mailing City":         r.get("mail_city",""),
                "Mailing State":        r.get("mail_state", STATE),
                "Mailing Zip":          r.get("mail_zip",""),
                "Property Address":     r.get("prop_address",""),
                "Property City":        r.get("prop_city",""),
                "Property State":       r.get("prop_state", STATE),
                "Property Zip":         r.get("prop_zip",""),
                "Lead Type":            r.get("cat_label",""),
                "Document Type":        r.get("doc_type",""),
                "Date Filed":           r.get("filed",""),
                "Document Number":      r.get("doc_num",""),
                "Amount/Debt Owed":     r.get("amount",""),
                "Seller Score":         r.get("score",""),
                "Motivated Seller Flags": " | ".join(r.get("flags",[])),
                "Source":               f"{COUNTY} County Clerk",
                "Public Records URL":   r.get("clerk_url",""),
            })
    log.info("GHL CSV saved → %s", path)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    now        = datetime.now(timezone.utc)
    end_dt     = now
    start_dt   = now - timedelta(days=LOOKBACK)
    date_fmt   = "%m/%d/%Y"
    start_str  = start_dt.strftime(date_fmt)
    end_str    = end_dt.strftime(date_fmt)
    week_cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    log.info("=" * 60)
    log.info("Harris County Lead Scraper — %s", now.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("Date range: %s → %s", start_str, end_str)
    log.info("=" * 60)

    # 1 — Parcel lookup
    parcel = ParcelLookup()
    parcel.load()

    # 2 — Clerk scrape
    log.info("Scraping clerk portal for %d doc types …", len(DOC_TYPES))
    scraper = ClerkScraper(start_str, end_str)
    await scraper.run()
    raw = _dedup(scraper.results)
    log.info("Raw records (deduped): %d", len(raw))

    # 3 — Enrich with parcel data + scoring
    enriched = []
    with_addr = 0
    for rec in raw:
        try:
            addr = parcel.lookup(rec.get("owner",""))
            if addr:
                rec.update(addr)
                with_addr += 1
            flags       = build_flags(rec, week_cutoff)
            rec["flags"] = flags
            rec["score"] = score_record(rec, flags, week_cutoff)
            enriched.append(rec)
        except Exception as exc:
            log.debug("Enrich error: %s", exc)
            rec["flags"] = []
            rec["score"] = 30
            enriched.append(rec)

    # Sort by score desc
    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    # 4 — Build output payload
    payload = {
        "fetched_at":   now.isoformat(),
        "source":       f"{COUNTY} County Clerk",
        "date_range":   {"start": start_str, "end": end_str},
        "total":        len(enriched),
        "with_address": with_addr,
        "records":      enriched,
    }

    # 5 — Save JSON to both locations
    for dest in (DASH_DIR / "records.json", DATA_DIR / "records.json"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        log.info("Saved JSON → %s", dest)

    # 6 — GHL CSV
    today_str = now.strftime("%Y-%m-%d")
    export_ghl_csv(enriched, DATA_DIR / f"ghl_export_{today_str}.csv")

    log.info("=" * 60)
    log.info("Done. Total: %d  |  With address: %d", len(enriched), with_addr)
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
