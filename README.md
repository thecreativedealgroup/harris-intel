# Harris County Motivated Seller Lead Scraper

Automated daily scraper for Harris County Clerk public records. Collects
motivated seller document types (Lis Pendens, Foreclosures, Liens, Judgments,
Probate, etc.) over the last 7 days, enriches with HCAD parcel data, scores
each lead 0–100, and publishes a live dashboard via GitHub Pages.

---

## File Structure

```
├── scraper/
│   ├── fetch.py            # Main scraper (Playwright + BeautifulSoup)
│   └── requirements.txt    # Python dependencies
├── dashboard/
│   ├── index.html          # GitHub Pages dashboard
│   └── records.json        # Latest lead data (committed by CI)
├── data/
│   ├── records.json        # Duplicate output (alternate path)
│   └── ghl_export.csv      # GoHighLevel-ready CSV
└── .github/
    └── workflows/
        └── scrape.yml      # Daily cron + Pages deploy
```

---

## Quick Start

### 1. Clone & configure repository

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

### 2. Enable GitHub Pages

Go to **Settings → Pages** → Source: `GitHub Actions`

### 3. Enable Actions write permissions

Go to **Settings → Actions → General → Workflow permissions** → select
"Read and write permissions".

### 4. Run locally

```bash
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
python scraper/fetch.py
```

Output files:
- `dashboard/records.json` — full lead data
- `data/ghl_export.csv` — GHL import ready

### 5. Trigger manually

Go to **Actions → Daily Lead Scrape → Run workflow**

---

## Scoring System

| Condition              | Points |
|------------------------|--------|
| Base score             | +30    |
| Each flag              | +10    |
| LP + Foreclosure combo | +20    |
| Amount > $100k         | +15    |
| Amount > $50k          | +10    |
| Filed this week        | +5     |
| Has property address   | +5     |
| Max score              | 100    |

**Flags**: Lis pendens, Pre-foreclosure, Judgment lien, Tax lien,
Mechanic lien, Probate / estate, LLC / corp owner, New this week

---

## Document Types Scraped

| Code       | Description            |
|------------|------------------------|
| LP         | Lis Pendens            |
| NOFC       | Notice of Foreclosure  |
| TAXDEED    | Tax Deed               |
| JUD        | Judgment               |
| CCJ        | Certified Judgment     |
| DRJUD      | Domestic Judgment      |
| LNCORPTX   | Corp Tax Lien          |
| LNIRS      | IRS Lien               |
| LNFED      | Federal Lien           |
| LN         | Lien                   |
| LNMECH     | Mechanic Lien          |
| LNHOA      | HOA Lien               |
| MEDLN      | Medicaid Lien          |
| PRO        | Probate                |
| NOC        | Notice of Commencement |
| RELLP      | Release Lis Pendens    |

---

## Notes

- The clerk portal at `cclerk.hctx.net` uses ASP.NET WebForms with
  `__doPostBack`. The Playwright scraper handles full JS rendering.
- HCAD parcel bulk data is downloaded from `pdata.hcad.org`. If
  unavailable, scraper continues without address enrichment.
- All errors are non-fatal — bad records are skipped, scraper never crashes.
- Dashboard auto-deploys to `https://YOUR_USERNAME.github.io/YOUR_REPO/`
