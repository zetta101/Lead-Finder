Lead Finder

Finds small UK businesses in towns you pick, checks whether they have a website
(and how good it is), pulls whatever contact details are publicly listed, and
scores each one as a potential web design / hosting lead. Runs locally on
Windows, writes everything to SQLite, and spits out an Excel workbook that's easy to navigate.

No paid APIs, locally ran and no accounts to sign
up for. It doesn't email anyone either — it just builds you a list to look
through yourself.

## What it actually does

1. Reads a list of towns + trade/industry keywords from `config.yaml`
2. Looks up businesses matching those in OpenStreetMap
3. Skips anything it's already got in the database recently
4. Tries to find/verify each business's real website (not working amazing - needs work)
5. Pulls public email addresses, phone numbers, contact page links (again doesn't work amazing - needs work)
6. Loads the site in a headless browser and checks the basics — HTTPS, mobile
   layout, load time, broken links, contact form, that sort of thing
7. Scores it twice: how good the *website* is, and how good a *lead* it is
   (a bad website = a good lead)
8. Optionally cross-checks Companies House if you've set an API key (buggy - needs work)
9. Saves everything, exports to `output/leads.xlsx`, screenshots the
   interesting ones, prints a summary (need to add ability to accept cookies etc before screenshot)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Edit `config.yaml` first if you want different towns/industries than the
default (it ships pointed at Torquay with a few trades as a quick test).

Then just:

```powershell
python run.py
```

### Companies House (optional)

Only matters if you want company numbers/status pulled in. Get a free key at
the [Companies House developer hub](https://developer.company-information.service.gov.uk/),
then:

```powershell
$env:COMPANIES_HOUSE_API_KEY = "your-key-here"
```

Leave it unset and those columns just stay blank — nothing else changes.

## Command-line options

```powershell
python run.py                       # everything in config.yaml
python run.py --location London     # just one city/town
python run.py --industry plumber    # just one trade
python run.py --limit 20            # cap results per search
python run.py --force-refresh       # re-scan sites even if recently checked
python run.py --export-only         # rebuild the Excel file, skip scanning
python run.py --no-screenshots
```

## A couple of things worth knowing

OpenStreetMap's tagging for trades (plumbers, electricians, builders...) is
patchy in a lot of towns — a search can come back with zero results for a
specific trade even though the tool is working fine, because not every
tradesperson has put themselves on the map. Cafes, restaurants,
hairdressers and other shopfront businesses tend to be much better mapped.
If you want a fuller test run, try `--industry cafe` or similar.

The public Overpass API also throws the occasional timeout under load — the
tool retries automatically, but a search returning nothing is worth a check
in `logs/leadfinder.log` before assuming there's genuinely nothing there.

Website guessing (used when no site is listed anywhere) tries a few likely
domain names based on the business name and only accepts a match if the page
content actually mentions the business — it won't find every real site, and
that's by design: better to say "no website found" than guess wrong. (Could do with some work)

## Managing a do-not-contact list

No UI for it yet, just:

```powershell
python -c "from leadfinder.database import Database; from pathlib import Path; db = Database(Path('data/leads.db')); db.add_do_not_contact('Example Ltd', 'example.co.uk', None, 'asked not to be contacted'); db.close()"
```

Anything on that list gets skipped on future runs and is greyed out / clearly
marked in the spreadsheet rather than showing up as a lead.

## Before you contact anyone

This only pulls contact info that's already publicly displayed for business
use, and it doesn't send anything on its own — you're meant to look through
the spreadsheet and decide who's worth reaching out to. That said, scraping
together a list of businesses and their emails is still something UK GDPR and
PECR have opinions about, especially if any of these turn out to be sole
traders using a personal email address rather than a limited company. Worth
reading up on before you start emailing people at scale, and honour any
opt-out requests via the do-not-contact list above. Not legal advice, just a
heads up.

## Project layout

```
run.py                     entry point / CLI
config.yaml                towns, industries, scoring weights, etc.
leadfinder/
  database.py               SQLite schema + queries
  discovery.py              finds businesses (OpenStreetMap for now)
  website_finder.py         verifies/guesses the business website
  contact_extractor.py      pulls emails/phones off the site
  website_checker.py        Playwright audit + screenshots
  company_checker.py        optional Companies House lookup
  scorer.py                 website score + lead score
  exporter.py                Excel export
  utils.py                  logging, normalisation, rate limiting
data/leads.db               created on first run
output/leads.xlsx           overwritten every run
screenshots/
logs/leadfinder.log
```

## Adding another data source

`discovery.py` has a small `DiscoverySource` base class — OpenStreetMap is the
only one implemented right now, but adding another just means writing a new
class with a `discover(location, industry, limit)` method and registering it
in `DiscoveryManager`. Everything downstream (dedup, scoring, export) works
off the same `RawBusiness` shape regardless of where it came from.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Activate the venv, `pip install -r requirements.txt` |
| Playwright complains it can't find Chromium | Run `playwright install chromium` |
| No results for a town | Check spelling, try a bigger nearby town, or bump `search_radius_metres` |
| Companies House fields always empty | Expected without an API key |
| `database is locked` | Don't run two copies of `run.py` at once |
| Excel export fails | Close `output/leads.xlsx` if you've got it open |
