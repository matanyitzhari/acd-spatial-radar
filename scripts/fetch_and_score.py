#!/usr/bin/env python3
"""
ACD Spatial Radar - fetch, dedupe, and score news for relevance to
Advanced Cell Diagnostics / Bio-Techne Spatial field sales.

Runs in GitHub Actions on a cron timer. Pulls RSS feeds, PubMed, and
NIH RePORTER, scores each new item via the Claude API, and writes the
results to data.json which the dashboard reads.

Design rules:
- A broken or missing source logs a warning and is skipped. It never
  stops the whole run.
- Items already in data.json are not re-scored (dedupe by link/id).
- No em dashes in any output.
"""

import json
import os
import re
import sys
import time
import html
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "sources.json")
DATA_PATH = os.path.join(ROOT, "data.json")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"  # cheap and fast for per-item scoring

# Email digest delivery (Resend). Secrets come from the environment, never the repo.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_URL = "https://api.resend.com/emails"
DIGEST_TO = os.environ.get("DIGEST_TO", "")  # recipient; with the resend.dev sender this must be your Resend signup email

CATEGORIES = ["Competitor Move", "Research/Methods", "Funded Lab"]
USER_AGENT = "ACD-Spatial-Radar/1.0 (sales intelligence; contact rep)"
MAX_ITEMS_KEPT = 400  # cap the stored set so data.json stays small
DEFAULT_MAX_AGE_DAYS = 365  # hard cutoff: items older than this are never shown (about 1 year). Tunable in scoring_config.json.
TERRITORIES_PATH = os.path.join(ROOT, "territories.json")
SCORING_CONFIG_PATH = os.path.join(ROOT, "scoring_config.json")

# Built-in scoring defaults. scoring_config.json overrides these when present,
# so the per-category thresholds and the recency decay can be tuned without
# editing this file. If that JSON is missing or malformed, we warn and use these.
DEFAULT_MIN_SCORE = {"Competitor Move": 25, "Funded Lab": 30, "Research/Methods": 45}
DEFAULT_MIN_SCORE_DEFAULT = 30
DEFAULT_RECENCY = {
    "tiers": [
        {"max_age_days": 7, "factor": 1.0},
        {"max_age_days": 21, "factor": 0.9},
        {"max_age_days": 45, "factor": 0.75},
        {"max_age_days": 90, "factor": 0.55},
    ],
    "older_factor": 0.4,
    "unknown_date_factor": 0.7,
}
DEFAULT_DIGEST = {
    "enabled": True,
    "min_score": 55,      # only items at or above this go in the email (>= a display threshold)
    "max_items": 12,      # cap the email length
    "subject_prefix": "Spatial Radar",
    "from": "Spatial Radar <onboarding@resend.dev>",  # change to a verified-domain sender to mail anyone but yourself
}

_SCORING_CFG = None


def load_scoring_config():
    """Load scoring_config.json once, falling back to the built-in defaults for
    anything missing or malformed. A broken config logs a warning and is ignored,
    it never stops the run."""
    global _SCORING_CFG
    if _SCORING_CFG is not None:
        return _SCORING_CFG
    cfg = {
        "min_score": dict(DEFAULT_MIN_SCORE),
        "min_score_default": DEFAULT_MIN_SCORE_DEFAULT,
        "recency": json.loads(json.dumps(DEFAULT_RECENCY)),  # deep copy
        "digest": dict(DEFAULT_DIGEST),
        "max_age_days": DEFAULT_MAX_AGE_DAYS,
    }
    try:
        with open(SCORING_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        ms = raw.get("min_score", {})
        if isinstance(ms, dict):
            for k, v in ms.items():
                if k == "_default":
                    cfg["min_score_default"] = int(v)
                elif not str(k).startswith("_") and isinstance(v, (int, float)):
                    cfg["min_score"][k] = int(v)
        rd = raw.get("recency_decay", {})
        if isinstance(rd, dict):
            tiers = rd.get("tiers")
            if isinstance(tiers, list):
                clean = []
                for t in tiers:
                    try:
                        clean.append({"max_age_days": int(t["max_age_days"]),
                                      "factor": float(t["factor"])})
                    except Exception:
                        continue
                if clean:
                    clean.sort(key=lambda t: t["max_age_days"])
                    cfg["recency"]["tiers"] = clean
            if "older_factor" in rd:
                cfg["recency"]["older_factor"] = float(rd["older_factor"])
            if "unknown_date_factor" in rd:
                cfg["recency"]["unknown_date_factor"] = float(rd["unknown_date_factor"])
        dg = raw.get("digest", {})
        if isinstance(dg, dict):
            if "enabled" in dg:
                cfg["digest"]["enabled"] = bool(dg["enabled"])
            for k in ("min_score", "max_items"):
                if k in dg and isinstance(dg[k], (int, float)):
                    cfg["digest"][k] = int(dg[k])
            for k in ("subject_prefix", "from"):
                if k in dg and isinstance(dg[k], str) and dg[k].strip():
                    cfg["digest"][k] = dg[k]
        if isinstance(raw.get("max_age_days"), (int, float)) and raw["max_age_days"] > 0:
            cfg["max_age_days"] = int(raw["max_age_days"])
        log(f"Loaded scoring_config.json: min_score={cfg['min_score']}, default={cfg['min_score_default']}")
    except FileNotFoundError:
        warn("scoring_config.json not found, using built-in scoring defaults.")
    except Exception as e:
        warn(f"Could not parse scoring_config.json: {e}. Using built-in scoring defaults.")
    _SCORING_CFG = cfg
    return _SCORING_CFG


def min_score_for(category):
    cfg = load_scoring_config()
    return cfg["min_score"].get(category, cfg["min_score_default"])


def max_age_days():
    return load_scoring_config().get("max_age_days", DEFAULT_MAX_AGE_DAYS)

# Keywords used to pre-filter broad newswire feeds (rss_filtered) before scoring,
# so we don't waste API calls on unrelated pharma news. An item passes if its
# title or summary contains any of these (spatial method terms OR a competitor name).
FILTER_KEYWORDS = [
    "spatial", "in situ", "rnascope", "rna-ish", "rna ish", "smfish", "merfish",
    "hcr ", "hybridization chain reaction", "multiplexed imaging", "spatial proteomics",
    "spatial transcriptomics", "spatial biology", "in situ hybridization",
    "molecular instruments", "10x genomics", "xenium", "visium", "nanostring",
    "cosmx", "geomx", "vizgen", "merscope", "akoya", "phenocycler", "codex",
    "navinci", "naveni", "resolve bioscience", "molecular cartography",
    "standard biotools", "hyperion", "imaging mass cytometry", "singular genomics",
    "quanterix", "bio-techne", "advanced cell diagnostics",
]

# Loaded once at startup
_TERR = None


def load_territories():
    global _TERR
    if _TERR is not None:
        return _TERR
    try:
        with open(TERRITORIES_PATH, "r", encoding="utf-8") as f:
            _TERR = json.load(f)
    except Exception as e:
        warn(f"Could not load territories.json: {e}. Territory tagging disabled.")
        _TERR = {}
    return _TERR


def classify_territory(state, institution="", city=""):
    """Return (territory_code, region) for a given state, institution, and city.
    Region is 'East' or 'West'. Returns ('National', 'National') when no
    usable location is present (most research and competitor items).
    Applies the MA and MD account-level overrides by institution name, and the
    NY split (NYA = NYC 5 boroughs, CEA = upstate NY + Canada east) by city."""
    terr = load_territories()
    if not terr or not state:
        return ("National", "National")
    state = state.upper().strip()
    presets = terr.get("territory_presets", {})
    east = terr.get("east_region", {})
    east_states = set()
    for grp in east.values():
        east_states.update(grp)
    region = "East" if state in east_states else "West"

    inst = (institution or "").lower()
    cty = (city or "").lower().strip()

    # Account-level overrides where one state maps to two territories.
    if state == "MA":
        if any(k in inst for k in ["harvard", "boston university", "dana-farber", "brigham", "mass general", "massachusetts general", "beth israel", "children's hospital boston"]):
            return ("BSA", region)
        if any(k in inst for k in ["broad", "mit", "massachusetts institute", "umass", "university of massachusetts"]):
            return ("NEA", region)
        # ambiguous MA account: leave specific code unset, mark region
        return ("MA (BSA/NEA)", region)
    if state == "MD":
        if "nih" in inst or "national institutes of health" in inst or "bethesda" in inst:
            return ("NIH", region)
        if any(k in inst for k in ["johns hopkins", "hopkins", "university of maryland", "umd"]):
            return ("MDA", region)
        return ("MD (NIH/MDA)", region)
    if state == "NY":
        # NYA = NYC 5 boroughs; CEA = upstate NY + Canada east.
        boroughs = ["new york", "manhattan", "bronx", "brooklyn", "queens",
                    "staten island"]
        if any(b in cty for b in boroughs):
            return ("NYA", region)
        # institution-name hints for NYC even if city field is odd
        if any(k in inst for k in ["mount sinai", "memorial sloan", "weill cornell",
                                   "rockefeller", "nyu", "new york university",
                                   "columbia", "einstein", "city university of new york"]):
            return ("NYA", region)
        # default upstate
        return ("CEA", region)

    # Single-territory states: find the first preset that owns this state.
    # Prefer East-coast territories when a state appears in multiple presets.
    east_coast = set(terr.get("east_coast_territories", []))
    matches = [code for code, states in presets.items() if state in states]
    if not matches:
        return ("Unmapped", region)
    # prefer an East-coast territory match if one exists
    east_matches = [m for m in matches if m in east_coast]
    chosen = east_matches[0] if east_matches else matches[0]
    return (chosen, region)


def owner_for(territory_code):
    """Map a resolved territory code to its rep owner, or '' when there is no
    single owner (National, Unmapped, or an unresolved account-level split)."""
    return load_territories().get("rep_owners", {}).get(territory_code, "")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def warn(msg):
    print(f"[WARN] {msg}", file=sys.stderr, flush=True)


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def clean_text(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)        # strip tags
    s = re.sub(r"\s+", " ", s).strip()
    return s


def item_id(link, title):
    basis = (link or "") + "|" + (title or "")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def title_key(title):
    """Normalized title for meaning-based dedupe so a preprint and its
    published version (same title, different link) collapse into one. Also
    strips the ' - Outlet' suffix Google News appends, so the same story from
    several outlets collapses to one key."""
    t = (title or "")
    # drop a trailing " - Source" that Google News tacks on (last occurrence)
    if " - " in t:
        t = t.rsplit(" - ", 1)[0]
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]", "", t)   # drop punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t[:120]


# Stock-trading noise that the Google News competitor feeds drag in. Publishers
# are ticker and analyst aggregators; phrases are trading chatter. Both are
# overridable from sources.json (blocked_publishers, blocked_title_phrases).
DEFAULT_BLOCKED_PUBLISHERS = [
    "tipranks", "gurufocus", "simplywall", "moomoo", "yahoo finance", "marketbeat",
    "benzinga", "zacks", "stocktitan", "stock titan", "motley fool", "insider monkey",
    "investing.com", "barchart", "seeking alpha", "nasdaq", "defense world",
    "markets insider", "tickerreport", "americanbankingnews", "etf daily news",
    "stocktwits", "wallstreetzen", "the globe and mail", "stocknews",
]
DEFAULT_BLOCKED_PHRASES = [
    "buy rating", "sell rating", "strong buy", "hold rating", "price target",
    "target price", "stock forecast", "analyst rating", "% surge", "stock soars",
    "stock jumps", "txg.us", "stock split", "market cap of", "stock to buy",
    "shares sold", "shares purchased", "shares bought", "raises stake", "lowers stake",
]


def is_market_noise(item, publishers, phrases):
    """True if an item looks like stock-trading noise (analyst ratings, price
    targets, ticker chatter) rather than a real competitor move. Keyed on the
    Google News outlet suffix and on trading phrases in the title."""
    title = (item.get("title") or "").lower()
    outlet = title.rsplit(" - ", 1)[-1] if " - " in title else ""
    if outlet and any(p in outlet for p in publishers):
        return True
    return any(ph in title for ph in phrases)


def parse_date(date_str):
    """Best-effort parse of a feed date string to an aware UTC datetime, or None.
    Handles ISO (with or without time, offset, or trailing Z), RFC822 RSS dates,
    and a few common variants, then a loose YYYY-MM-DD extraction as a fallback."""
    if not date_str:
        return None
    s = str(date_str).strip()
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    fmts = ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M %z", "%a, %d %b %Y %H:%M:%S",
            "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d"]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def is_recent(date_str, days=None):
    """True only if the date parses to within the last `days` (the configurable
    one-year cutoff by default). Items with a missing or unparseable date are
    treated as NOT recent and excluded, so nothing ancient or dateless leaks in."""
    if days is None:
        days = max_age_days()
    from datetime import timedelta
    dt = parse_date(date_str)
    if dt is None:
        return False
    return dt >= (datetime.now(timezone.utc) - timedelta(days=days))


def normalize_link(link, source_name=""):
    """Fix links that are not directly clickable. bioRxiv Atom feeds put a
    DOI-style id in the link field, which 404s on its own. A bioRxiv DOI
    (10.1101/...) resolves to the live article via doi.org regardless of
    version, so rewrite it."""
    link = (link or "").strip()
    if not link:
        return link
    # already a full content URL to the article
    if link.startswith("http") and "10.1101" in link and "biorxiv" in link:
        return link
    # bioRxiv cgi short-form URLs carry the DOI tail; convert to doi.org
    m_cgi = re.search(r"/cgi/content/short/([0-9.]+v?\d*)", link)
    if m_cgi:
        return "https://doi.org/10.1101/" + m_cgi.group(1)
    # extract a bioRxiv/medRxiv DOI if present anywhere in the string
    m = re.search(r"(10\.1101/[^\s\"'<>]+)", link)
    if m:
        return "https://doi.org/" + m.group(1)
    # bare doi: prefix
    if link.lower().startswith("doi:"):
        return "https://doi.org/" + link.split(":", 1)[1].strip()
    return link


# ----------------------------------------------------------------------
# fetchers (each returns a list of raw dicts: title, link, summary, source, category_hint, date)
# ----------------------------------------------------------------------

def strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def fetch_rss(src):
    out = []
    try:
        raw = http_get(src["url"])
        root = ET.fromstring(raw)
    except Exception as e:
        warn(f"RSS source '{src['name']}' failed: {e}. Skipping.")
        return out

    # handle both RSS <item> and Atom <entry>
    nodes = []
    for el in root.iter():
        if strip_ns(el.tag) in ("item", "entry"):
            nodes.append(el)

    for n in nodes:
        title = link = summary = date = ""
        for child in n:
            t = strip_ns(child.tag)
            if t == "title":
                title = clean_text(child.text)
            elif t == "link":
                # Atom uses href attribute, RSS uses text
                link = child.get("href") or clean_text(child.text) or link
            elif t in ("id", "guid"):
                # bioRxiv Atom often carries the DOI here
                idval = clean_text(child.text)
                if not link or "10.1101" in idval:
                    link = link or idval
                    if "10.1101" in idval:
                        link = idval
            elif t in ("description", "summary", "abstract"):
                summary = clean_text(child.text)
            elif t in ("pubDate", "published", "updated", "date"):
                date = clean_text(child.text)
        if title:
            item = {
                "title": title,
                "link": normalize_link(link, src["name"]),
                "summary": summary[:1200],
                "source": src["name"],
                "category_hint": src.get("category_hint", ""),
                "date": date,
            }
            # For broad newswire feeds (rss_filtered), keep only items that mention
            # a spatial/ISH/competitor keyword, so we don't score hundreds of
            # unrelated pharma releases. Match against title + summary.
            if src.get("type") == "rss_filtered":
                hay = (title + " " + summary).lower()
                kws = src.get("_filter_keywords") or FILTER_KEYWORDS
                if not any(k in hay for k in kws):
                    continue
            out.append(item)
    log(f"RSS '{src['name']}': {len(out)} items")
    return out


def fetch_pubmed(cfg):
    out = []
    try:
        params = {
            "db": "pubmed",
            "term": cfg["query"],
            "retmode": "json",
            "retmax": str(cfg.get("retmax", 40)),
            "sort": "date",
        }
        url = cfg["esearch_base"] + "?" + urllib.parse.urlencode(params)
        ids = json.loads(http_get(url)).get("esearchresult", {}).get("idlist", [])
        if not ids:
            log("PubMed: 0 ids")
            return out
        time.sleep(0.4)
        sparams = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
        surl = cfg["esummary_base"] + "?" + urllib.parse.urlencode(sparams)
        result = json.loads(http_get(surl)).get("result", {})
        for pid in result.get("uids", []):
            rec = result.get(pid, {})
            title = clean_text(rec.get("title", ""))
            if not title:
                continue
            out.append({
                "title": title,
                "link": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                "summary": clean_text(rec.get("source", "")) + ". " + clean_text(rec.get("fulljournalname", "")),
                "source": "PubMed",
                "category_hint": cfg.get("category_hint", "Research/Methods"),
                "date": rec.get("pubdate", ""),
            })
    except Exception as e:
        warn(f"PubMed failed: {e}. Skipping.")
    log(f"PubMed: {len(out)} items")
    return out


def fetch_nih_reporter(cfg):
    out = []
    try:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=max_age_days())).strftime("%Y-%m-%d")
        # NIH fiscal year runs Oct 1 to Sep 30. Include current and prior FY so a
        # 4-month window never falls in a gap around the Oct boundary.
        fy = now.year if now.month >= 10 else now.year
        fiscal_years = sorted({fy, fy - 1, (now.year if now.month < 10 else now.year + 1)})
        payload = {
            "criteria": {
                "advanced_text_search": {
                    "operator": "or",
                    "search_field": "projecttitle,abstracttext,terms",
                    "search_text": cfg["advanced_text_search"],
                },
                "fiscal_years": fiscal_years,
                "award_notice_date": {"from_date": cutoff, "to_date": ""},
            },
            "offset": 0,
            "limit": cfg.get("limit", 50),
            "sort_field": "award_notice_date",
            "sort_order": "desc",
        }
        time.sleep(1.0)  # NIH asks for <= 1 request/second
        resp = http_post_json(cfg["url"], payload)
        missing_org_logged = False
        for r in resp.get("results", []):
            title = clean_text(r.get("project_title", ""))
            if not title:
                continue
            # Organization can come back nested under 'organization' (standard) or
            # occasionally as flat keys. Try every plausible shape.
            org_obj = r.get("organization") or {}
            org = (org_obj.get("org_name") or r.get("org_name")
                   or org_obj.get("orgName") or "")
            org_state = (org_obj.get("org_state") or r.get("org_state")
                         or org_obj.get("orgState") or "")
            org_city = (org_obj.get("org_city") or r.get("org_city")
                        or org_obj.get("orgCity") or "")
            # Diagnostic: if org is missing, log the keys actually returned for the
            # first such record so we can see the real response shape in the run log.
            if not org and not missing_org_logged:
                warn(f"NIH org missing for '{title[:40]}'. Top-level keys: {list(r.keys())}. "
                     f"organization keys: {list(org_obj.keys()) if isinstance(org_obj, dict) else type(org_obj)}")
                missing_org_logged = True
            pis = ", ".join(
                clean_text(pi.get("full_name", ""))
                for pi in (r.get("principal_investigators") or [])
            )
            amt = r.get("award_amount")
            item_date = clean_text(str(r.get("award_notice_date") or r.get("project_start_date", "")))
            summary = f"PI: {pis}. Institution: {org or 'n/a'}. " \
                      f"Award: {('$' + format(amt, ',')) if amt else 'n/a'}. " \
                      + clean_text(r.get("abstract_text", ""))[:800]
            # Build a reliable per-grant link. appl_id is always returned and
            # the project-details page format is stable. Prefer it over any
            # optional URL field which may be absent.
            appl_id = r.get("appl_id") or r.get("applId")
            if appl_id:
                link = f"https://reporter.nih.gov/project-details/{appl_id}"
            elif r.get("project_num"):
                link = "https://reporter.nih.gov/search/?projectNum=" + urllib.parse.quote(str(r.get("project_num")))
            else:
                link = "https://reporter.nih.gov/"
            out.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": "NIH RePORTER",
                "category_hint": cfg.get("category_hint", "Funded Lab"),
                "date": item_date,
                "institution": org,
                "pi": pis,
                "state": (org_state or "").upper().strip(),
                "city": org_city,
            })
    except Exception as e:
        warn(f"NIH RePORTER failed: {e}. Skipping.")
    log(f"NIH RePORTER: {len(out)} items")
    return out


def fetch_html_news(cfg):
    """Scrape a press-release listing page that has no working RSS feed (e.g.
    Molecular Instruments, a Wix site). Pulls the page HTML, finds outbound
    newswire links (Business Wire, PR Newswire, etc.) and the headline text and
    date near each one. Emits one item per release linking to the newswire
    article, which is the stable canonical URL. Resilient: any parse miss is
    skipped, a fetch failure logs a warning and returns nothing."""
    out = []
    name = cfg.get("name", "HTML News")
    try:
        raw = http_get(cfg["url"])
        # Find anchors pointing at known newswire/press domains.
        wire_re = re.compile(
            r'href="(https?://(?:www\.)?(?:businesswire|prnewswire|accesswire|einpresswire|globenewswire|prweb)\.com/[^"]+)"',
            re.I)
        # Split the HTML into chunks around each newswire link so we can grab the
        # nearby headline and date. We look at a window of text before each link.
        text = re.sub(r'<[^>]+>', '\n', raw)        # strip tags to lines
        text = html.unescape(text)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        # Collect newswire URLs in order of appearance.
        wire_urls = wire_re.findall(raw)
        if not wire_urls:
            log(f"{name}: no newswire links found on page")
            return out
        # For headline+date, walk the de-tagged lines: a release is a longish
        # title line, a source word, then a date line like 'February 17, 2026'.
        date_re = re.compile(r'^[A-Z][a-z]+ \d{1,2}, \d{4}$')
        title_buf = None
        wire_idx = 0
        for i, ln in enumerate(lines):
            if date_re.match(ln) and title_buf and wire_idx < len(wire_urls):
                # found a release block: title_buf is the headline, ln is the date
                link = wire_urls[wire_idx]
                wire_idx += 1
                out.append({
                    "title": clean_text(title_buf),
                    "link": link,
                    "summary": f"{name} press release via newswire.",
                    "source": name,
                    "category_hint": cfg.get("category_hint", "Competitor Move"),
                    "date": ln,
                })
                title_buf = None
            elif len(ln) > 40 and not ln.startswith(("http", "[", "meta-", "©", "Copyright")):
                # candidate headline: a long content line
                title_buf = ln
        log(f"{name}: {len(out)} items")
    except Exception as e:
        warn(f"{name} (HTML scrape) failed: {e}. Skipping.")
    return out


def fetch_nsf(cfg):
    """NSF Awards API. GET with keyword + printFields + date range (mm/dd/yyyy).
    Response: {'response': {'award': [...]}}. Names PI and awardee institution
    with a state code, so these classify into territories like NIH items."""
    out = []
    try:
        from datetime import timedelta
        start = (datetime.now(timezone.utc) - timedelta(days=max_age_days())).strftime("%m/%d/%Y")
        end = datetime.now(timezone.utc).strftime("%m/%d/%Y")
        params = {
            "keyword": cfg.get("keyword_query", "spatial biology"),
            "printFields": "id,title,awardeeName,awardeeStateCode,awardeeCity,pdPIName,startDate,fundsObligatedAmt,abstractText",
            "dateStart": start,
            "dateEnd": end,
            "rpp": str(cfg.get("limit", 50)),
            "offset": "1",
        }
        url = cfg["url"] + "?" + urllib.parse.urlencode(params)
        time.sleep(1.0)  # NSF courtesy: ~1 req/sec
        resp = json.loads(http_get(url))
        awards = (resp.get("response") or {}).get("award", [])
        for a in awards:
            title = clean_text(a.get("title", ""))
            if not title:
                continue
            aid = a.get("id", "")
            pi = clean_text(a.get("pdPIName", ""))
            org = clean_text(a.get("awardeeName", ""))
            state = (a.get("awardeeStateCode") or "").upper().strip()
            amt = a.get("fundsObligatedAmt")
            link = f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={aid}" if aid else "https://www.nsf.gov/awardsearch/"
            summary = f"PI: {pi}. Institution: {org}. " \
                      f"Award: {('$' + str(amt)) if amt else 'n/a'}. " \
                      + clean_text(a.get("abstractText", ""))[:800]
            out.append({
                "title": title, "link": link, "summary": summary,
                "source": "NSF Awards", "category_hint": cfg.get("category_hint", "Funded Lab"),
                "date": clean_text(a.get("startDate", "")), "institution": org,
                "pi": pi, "state": state, "city": clean_text(a.get("awardeeCity", "")),
            })
    except Exception as e:
        warn(f"NSF Awards failed: {e}. Skipping.")
    log(f"NSF Awards: {len(out)} items")
    return out


def fetch_sec_edgar(cfg):
    """SEC EDGAR full-text search. GET efts.sec.gov/LATEST/search-index with
    q, forms, dateRange, ciks. Requires a descriptive User-Agent with contact
    email per SEC fair-access policy. Response: {'hits':{'hits':[{'_source':...,'_id':'accession:file'}]}}."""
    out = []
    try:
        from datetime import timedelta
        start = (datetime.now(timezone.utc) - timedelta(days=max_age_days())).strftime("%Y-%m-%d")
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ciks = [v for v in (cfg.get("ciks_of_interest") or {}).values()
                if v and not v.startswith("VERIFY")]
        params = {
            "q": cfg.get("query", "spatial biology"),
            "forms": ",".join(cfg.get("forms", ["8-K"])),
            "dateRange": "custom", "startdt": start, "enddt": end,
        }
        if ciks:
            params["ciks"] = ",".join(ciks)
        ua = cfg.get("user_agent", USER_AGENT)
        headers = {"User-Agent": ua, "Accept": "application/json"}
        url = cfg["url"] + "?" + urllib.parse.urlencode(params)
        time.sleep(0.5)  # SEC: stay well under 10 req/s
        resp = json.loads(http_get(url, headers=headers))
        hits = (resp.get("hits") or {}).get("hits", [])
        cik_to_name = {v.lstrip("0"): k for k, v in (cfg.get("ciks_of_interest") or {}).items() if v and not v.startswith("VERIFY")}
        for h in hits:
            src = h.get("_source", {})
            form = src.get("file_type") or src.get("form") or ""
            display = src.get("display_names", [])
            entity = display[0] if display else ""
            title = f"{form}: {entity}".strip(": ").strip()
            if not title:
                continue
            # build filing URL from _id = "accession:filename"
            _id = h.get("_id", "")
            link = "https://www.sec.gov/cgi-bin/browse-edgar"
            cik_list = src.get("cik", [])
            cik0 = (cik_list[0] if isinstance(cik_list, list) and cik_list else cik_list) or ""
            if _id and ":" in _id and cik0:
                acc, fname = _id.split(":", 1)
                acc_nodash = acc.replace("-", "")
                link = f"https://www.sec.gov/Archives/edgar/data/{str(cik0).lstrip('0')}/{acc_nodash}/{fname}"
            summary = f"{entity} filed a {form}. Mentions: {cfg.get('query','')[:80]}."
            out.append({
                "title": title, "link": link, "summary": summary,
                "source": "SEC EDGAR", "category_hint": cfg.get("category_hint", "Competitor Move"),
                "date": src.get("file_date", ""), "institution": entity,
            })
    except Exception as e:
        warn(f"SEC EDGAR failed: {e}. Skipping.")
    log(f"SEC EDGAR: {len(out)} items")
    return out


# ----------------------------------------------------------------------
# scoring via Claude
# ----------------------------------------------------------------------

SCORING_SYSTEM = """You are a sales intelligence analyst for Advanced Cell Diagnostics (ACD), a Bio-Techne brand. ACD sells RNAscope in situ hybridization and spatial biology products to academic researchers in the United States. This radar is an early-warning system first and a prospecting feed second.

You score one item at a time on two axes from 0 to 10. Reason about each honestly. Do not compute a final score, that is handled in code outside this prompt.

AXES (0 to 10 each):
1. relevance: How squarely is this in the spatial biology / in situ hybridization / RNAscope world? This is the gate. 10 = directly about RNAscope, smFISH, spatial transcriptomics, HCR, MERFISH, or multiplexed in situ imaging. 5 = adjacent (single-cell, general genomics) with a plausible spatial or in situ angle. 0 = unrelated biology. A routine single-cell or genomics paper does not earn high relevance just because it mentions tissue.

2. importance: How much should a field sales manager care, read through the lens of the item's category.
   - For a Funded Lab: the strength of the buying signal. 10 = a newly funded grant whose aims imply in situ RNA validation, or a clear match to a high-value persona below. 5 = an active lab in a relevant area. 0 = no commercial implication. A grant or paper that matches a persona is a strong RNAscope prospect EVEN IF it never says "spatial" or "RNAscope", because these labs need in situ RNA validation.
   - For a Competitor Move: competitive threat or urgency to ACD. 10 = a direct competitor launching, repositioning, partnering, or getting funded. Treat any Molecular Instruments news as high importance (HCR is positioned head-to-head against RNAscope on speed and price). 0 = no competitive angle.
   - For Research/Methods: how useful for positioning or objection handling, not as a lead. 10 = work that directly uses or compares RNAscope, smFISH, HCR, or MERFISH and hands the rep a talking point. Lower for general interest with no clear lever.

DIRECT AND EMERGING COMPETITORS to weigh when scoring a Competitor Move:
   - Molecular Instruments (HCR RNA-CISH and RNA-FISH; head-to-head against RNAscope on speed and price. High importance.)
   - 10x Genomics (Xenium, Visium)
   - Bruker / NanoString (CosMx, GeoMx)
   - Vizgen (MERSCOPE)
   - Akoya Biosciences / Quanterix (PhenoCycler, CODEX)
   - Navinci (Naveni in situ proximity ligation, spatial proteomics; competes with ProximityScope)
   - Resolve Biosciences (Molecular Cartography, ISH-based spatial)
   - Standard BioTools (Hyperion imaging mass cytometry, spatial proteomics)
   - Singular Genomics (G4X emerging spatial entrant)
   - Rebus Biosystems (Esper), Curio Bioscience (emerging entrants)
   - Leica Biosystems (BOND RX automation, often partnering with the above)
   Adjacent ecosystem (moderate importance, useful context not direct threat): Visiopharm, Grundium, Indica Labs (HALO), and pathology/imaging analysis vendors.

NOISE CONTROL: A generic preprint or paper with no clear spatial, in situ, or commercial hook should score LOW on relevance, which gates its final score below the display threshold. Do not inflate a routine single-cell or genomics paper just because it mentions tissue. Reserve high research scores for work that genuinely uses or compares RNAscope, smFISH, HCR, MERFISH, or multiplexed in situ imaging, or that reveals an active high-value lab.

HIGH-VALUE LAB PROFILES: ACD field reps target these 16 research personas. A grant or paper matching one of these is a strong RNAscope prospect EVEN IF it never says "spatial" or "RNAscope", because these labs need in situ RNA validation. Treat a clear match as high relevance and high importance:
1. Neuro-psychiatric: bulk or single-cell RNA-seq on brain tissue in psychiatric cohorts (depression, PTSD, schizophrenia, autism, bipolar, addiction). Needs subtype marker validation in situ.
2. HIV / SIV: viral reservoir labs in lymph node, gut, or brain tissue (HIV-1, SIV, SHIV). No antibodies for viral RNA, so RNAscope is the only option.
3. Gene therapy: AAV or lentiviral biodistribution and transgene expression (vector, transgene, AAV, RNAi, siRNA).
4. Neuroscience: scRNA-seq brain labs validating cell types or circuits (receptors, ligands, neural circuits, synaptic, glia).
5. Comparative oncology: vet or NCI canine cancer programs (dog, canine, tumor microenvironment) where antibodies are scarce.
6. Poor-antibody labs: IHC-heavy stem cell or developmental work frustrated by antibody specificity (LGR5, SOX2, SOX9, marker validation).
7. Cytokine detection: localizing cytokines in tissue (IFNG, IL6, TNFA, GZMB, CXCL9/10) where antibodies are weak.
8. Immuno-oncology and TME: tumor microenvironment spatial profiling (PD-1, PD-L1, CTLA-4, TILs, myeloid, exhaustion, CD8 T cells).
9. Neurodegeneration and glial biology: AD, PD, ALS in human brain (microglia, astrocytes, amyloid, tau, P2RY12, TMEM119, GFAP).
10. Organoids and 3D models: confirming cell identity in organoids (patient-derived, brain, intestinal, spheroids).
11. Fibrosis, NASH, tissue remodeling: fibrotic niche profiling (NASH, IPF, cirrhosis, hepatic stellate cells, COL1A1, ACTA2, myofibroblasts).
12. Infectious disease non-HIV: pathogen RNA localization (SARS-CoV-2, influenza, RSV, tuberculosis, parasite, viral tropism).
13. CRISPR and gene-editing validation: on/off-target and exon-level (base editing, prime editing, gRNA, splice variant, knock-in/out).
14. Non-coding RNA: lncRNA and miRNA spatial localization (lncRNA, microRNA, antisense, ncRNA).
15. Stem cell and iPSC differentiation: lineage marker validation (iPSC, OCT4, NANOG, neural differentiation, cardiomyocytes).
16. Barrier-organ inflammation: lung, GI, kidney epithelia (IBD, Crohn's, ulcerative colitis, asthma, COPD, glomerulonephritis, podocytes).

CATEGORY: assign exactly one from: Competitor Move, Research/Methods, Funded Lab.

WHY: write a one-line hook of at most 24 words. Be concrete, not vague. Name the specific lever where present: gene targets, tissue or disease, method, PI name, institution, or the competing product. Tell the rep what to note or do. Do not use em dashes.

Respond with ONLY a JSON object, no preamble, no markdown fences:
{"relevance": <int>, "importance": <int>, "category": "<one of the three>", "why": "<one line>"}"""


def recency_factor(date_str):
    """Freshness decay applied to the score in code, not by the model. Fresh
    items count full, older ones taper, nothing drops below the floor on age
    alone. An unparseable date gets a middling factor so a formatting quirk
    never zeroes a good item. Cutoffs and floor live in scoring_config.json."""
    rcfg = load_scoring_config()["recency"]
    parsed = parse_date(date_str)
    if parsed is None:
        return rcfg["unknown_date_factor"]
    age = (datetime.now(timezone.utc) - parsed).days
    for tier in rcfg["tiers"]:
        if age <= tier["max_age_days"]:
            return tier["factor"]
    return rcfg["older_factor"]


def score_item(item):
    if not ANTHROPIC_API_KEY:
        # no key: pass through with a neutral score so the pipeline still works
        return {"score": 50, "category": item.get("category_hint") or "Research/Methods",
                "why": "Scored neutrally (no API key set)."}
    user = (
        f"Source: {item['source']}\n"
        f"Category hint: {item.get('category_hint','')}\n"
        f"Title: {item['title']}\n"
        f"Summary: {item.get('summary','')[:1000]}"
    )
    payload = {
        "model": MODEL,
        "max_tokens": 200,
        "system": SCORING_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    try:
        resp = http_post_json(ANTHROPIC_URL, payload, headers=headers, timeout=60)
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        text = text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        rel = max(0, min(10, int(parsed.get("relevance", 0) or 0)))
        imp = max(0, min(10, int(parsed.get("importance", 0) or 0)))
        category = parsed.get("category", "")
        if category not in CATEGORIES:
            category = item.get("category_hint") or "Research/Methods"
        rf = recency_factor(item.get("date", ""))
        # Composite: multiply the two axes (both must be true to rank high),
        # then decay by age. relevance*importance is 0..100; rf is 0.4..1.0.
        score = round(rel * imp * rf)
        why = clean_text(parsed.get("why", "")).replace("\u2014", "-")
        return {
            "score": max(0, min(100, score)),
            "category": category,
            "why": why,
            "subscores": {"relevance": rel, "importance": imp},
            "recency_factor": round(rf, 2),
        }
    except Exception as e:
        warn(f"Scoring failed for '{item['title'][:60]}': {e}")
        return {"score": 40, "category": item.get("category_hint") or "Research/Methods",
                "why": "Could not score automatically, review manually."}


IMPACT_BRIEF_SYSTEM = """You are a competitive intelligence analyst for Advanced Cell Diagnostics (ACD), a Bio-Techne brand that sells RNAscope in situ hybridization and spatial biology products. You write a short "Impact Brief" about a competitor's news, for a mixed internal audience: product managers, marketing, and sales leadership. The goal is shared understanding of what a competitor move means for ACD, not a field sales script.

Ground everything in the provided item. If the item is a thin headline with little detail, keep the analysis appropriately high-level and do not invent specifics (no made-up deal terms, dollar figures, or product names).

Be honest and balanced. Name real risks plainly; do not write reassurance. The "holding ground" points must be genuine ACD strengths relevant to this specific move, not generic boilerplate.

ACD context for your analysis: RNAscope's strengths are single-molecule sensitivity and specificity, deep validation across tissue types, and platform-agnostic flexibility (it does not lock a customer into one company's instrument). ACD's exposure is generally to platform bundling and ecosystem lock-in by larger competitors (10x Genomics, Bruker/NanoString, Akoya/Quanterix, etc.), and to emerging head-to-head ISH rivals like Molecular Instruments (HCR).

Do not use em dashes. Use commas, periods, parentheses, or "and"/"but" instead.

Respond with ONLY a JSON object, no preamble, no markdown fences:
{
  "what_happened": "<2 to 3 sentences, plain language>",
  "why_it_matters": "<2 to 3 sentences on the strategic meaning for ACD>",
  "at_risk": ["<short point>", "<short point>"],
  "holding_ground": ["<short point>", "<short point>"]
}
Each at_risk and holding_ground point is one scannable sentence or phrase. Provide 1 to 3 points each."""


def fetch_article_text(url, max_chars=4000):
    """Fetch an article page and return a cleaned text excerpt for deeper
    analysis. Best-effort: returns empty string on any failure (paywall,
    timeout, JS-only page). Strips tags, scripts, and boilerplate."""
    if not url or not url.startswith("http"):
        return ""
    try:
        raw = http_get(url, timeout=20)
        # drop script/style blocks entirely
        raw = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def generate_impact_brief(item):
    """Generate a structured Impact Brief for a competitor item. Returns a dict
    or None. Only called for Competitor Move items. Costs one extra API call per
    competitor item, so it is gated by category upstream."""
    if not ANTHROPIC_API_KEY:
        return None
    # Fetch the underlying article for deeper grounding (catches platform names,
    # framing, deal detail the feed summary misses). Falls back to summary.
    article = fetch_article_text(item.get("link", ""))
    body = article if len(article) > len(item.get("summary", "")) else item.get("summary", "")
    user = (
        f"Source: {item.get('source','')}\n"
        f"Title: {item.get('title','')}\n"
        f"Article text: {body[:4000]}\n"
        f"One-line take: {item.get('why','')}"
    )
    payload = {
        "model": MODEL,
        "max_tokens": 800,
        "system": IMPACT_BRIEF_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}
    try:
        resp = http_post_json(ANTHROPIC_URL, payload, headers=headers, timeout=60)
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        text = text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        clean = lambda s: clean_text(s).replace("\u2014", "-")
        return {
            "what_happened": clean(parsed.get("what_happened", "")),
            "why_it_matters": clean(parsed.get("why_it_matters", "")),
            "at_risk": [clean(x) for x in (parsed.get("at_risk") or []) if x][:3],
            "holding_ground": [clean(x) for x in (parsed.get("holding_ground") or []) if x][:3],
        }
    except Exception as e:
        warn(f"Impact brief failed for '{item.get('title','')[:50]}': {e}")
        return None


OUTREACH_ANGLE_SYSTEM = """You are a sales enablement analyst for Advanced Cell Diagnostics (ACD), a Bio-Techne brand that sells RNAscope in situ hybridization probes and spatial biology products to academic researchers. You write a short "Outreach Angle" for a newly funded lab (an NIH or NSF grant), to help a field sales rep turn this lead into a conversation.

Ground everything in the provided grant text. If the abstract is thin, keep it high-level and do not invent specifics about the lab's work.

ACD targets 16 researcher personas. A funded lab is a strong RNAscope prospect when its work implies a need for in situ RNA validation, even if the grant never says "RNAscope" or "spatial." Common needs: validating cell-type markers in tissue, localizing low-abundance transcripts, confirming single-cell or sequencing findings in situ, detecting targets with no good antibody (cytokines, viral RNA, lncRNA), or mapping expression across tissue regions.

Write the following, each concrete and specific to THIS grant. Map the lab's research signals (disease, tissue, gene targets, technique) to the RNAscope need the way the MeSH signal-to-angle playbook does:
1. hook: what about this lab's funded work makes them a RNAscope prospect. Name the persona fit and the specific assay need their work implies.
2. way_in: the concrete validation problem or product fit to lead a conversation with. What pain does RNAscope solve for them.
3. routing: a brief cue on who should own this, referencing the territory if provided. Keep it short.
4. draft_subject: a short, specific email subject line (under 10 words) that references their work, not a generic pitch.
5. draft_body: a ready-to-send cold outreach email of 3 to 5 sentences from an ACD rep to the PI. Open by referencing their specific funded work, connect it to the in situ RNA validation need, and end with a low-friction ask (a short call, a relevant probe set, or sample data). Professional and concise, no hype. Do not invent personal details, lab members, or results not in the abstract. Leave the signature as a placeholder line "[Rep name], Advanced Cell Diagnostics (Bio-Techne)". Do not use em dashes.

Do not use em dashes anywhere. Use commas, periods, parentheses, or "and"/"but" instead.

Respond with ONLY a JSON object, no preamble, no markdown fences:
{"hook": "<2 sentences>", "way_in": "<2 sentences>", "routing": "<one short line>", "draft_subject": "<short subject>", "draft_body": "<3 to 5 sentence email>"}"""


def generate_outreach_angle(item):
    """Generate an outreach angle for a funded-lab item. Returns a dict or None.
    Only called for Funded Lab items. Works from the stored grant abstract
    (NIH/NSF summaries are already rich), so no web fetch needed."""
    if not ANTHROPIC_API_KEY:
        return None
    user = (
        f"Source: {item.get('source','')}\n"
        f"Grant title: {item.get('title','')}\n"
        f"PI: {item.get('pi','')}\n"
        f"Institution: {item.get('institution','')}\n"
        f"Territory: {item.get('territory','')} ({item.get('region','')})\n"
        f"Grant abstract: {item.get('summary','')[:1800]}\n"
        f"One-line take: {item.get('why','')}"
    )
    payload = {
        "model": MODEL,
        "max_tokens": 800,
        "system": OUTREACH_ANGLE_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}
    try:
        resp = http_post_json(ANTHROPIC_URL, payload, headers=headers, timeout=60)
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        text = text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        clean = lambda s: clean_text(s).replace("\u2014", "-")
        return {
            "hook": clean(parsed.get("hook", "")),
            "way_in": clean(parsed.get("way_in", "")),
            "routing": clean(parsed.get("routing", "")),
            "draft_subject": clean(parsed.get("draft_subject", "")),
            "draft_body": clean(parsed.get("draft_body", "")),
        }
    except Exception as e:
        warn(f"Outreach angle failed for '{item.get('title','')[:50]}': {e}")
        return None


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def load_existing():
    if not os.path.exists(DATA_PATH):
        return {"updated": "", "items": []}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        warn(f"Could not read existing data.json: {e}. Starting fresh.")
        return {"updated": "", "items": []}


def compose_digest_html(items):
    """Render the picked items into a light, email-safe HTML body. Inline styles
    and table layout so it survives Gmail and Outlook. No em dashes."""
    cat_colors = {
        "Competitor Move": ("#FAECE7", "#993C1D"),
        "Funded Lab": ("#EAF3DE", "#27500A"),
        "Research/Methods": ("#E6F1FB", "#0C447C"),
    }
    def card(it):
        title = html.escape(it.get("title", ""))
        link = html.escape(it.get("link", "") or "#", quote=True)
        cat = it.get("category", "")
        bg, fg = cat_colors.get(cat, ("#F1EFE8", "#444441"))
        score = it.get("score", 0)
        meta_bits = [html.escape(it.get("source", ""))]
        if it.get("date"):
            meta_bits.append(html.escape(it.get("date", "")))
        terr = it.get("territory", "")
        if terr and terr not in ("National", "Unmapped", ""):
            meta_bits.append("Territory: " + html.escape(terr))
        if it.get("owner"):
            meta_bits.append("Owner: " + html.escape(it["owner"]))
        meta = " &nbsp;&middot;&nbsp; ".join(b for b in meta_bits if b)
        why = html.escape(it.get("why", ""))
        extra = ""
        if cat == "Competitor Move" and it.get("impact_brief", {}).get("why_it_matters"):
            extra = ('<div style="margin-top:8px;font-size:13px;color:#5F5E5A;line-height:1.45;">'
                     '<b style="color:#993C1D;">Why it matters.</b> '
                     + html.escape(it["impact_brief"]["why_it_matters"]) + '</div>')
        elif cat == "Funded Lab" and it.get("outreach_angle", {}).get("hook"):
            extra = ('<div style="margin-top:8px;font-size:13px;color:#5F5E5A;line-height:1.45;">'
                     '<b style="color:#27500A;">Angle.</b> '
                     + html.escape(it["outreach_angle"]["hook"]) + '</div>')
        draft = ""
        ang = it.get("outreach_angle") or {}
        if cat == "Funded Lab" and ang.get("draft_body"):
            subj = html.escape(ang.get("draft_subject", ""))
            body_txt = html.escape(ang.get("draft_body", "")).replace("\n", "<br>")
            draft = (
                '<div style="margin-top:10px;background:#F3F8F2;border:1px solid #CFE6D4;'
                'border-radius:10px;padding:12px 14px;">'
                '<div style="font-family:Menlo,Consolas,monospace;font-size:11px;font-weight:700;'
                'color:#27500A;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;">Draft outreach</div>'
                + (('<div style="font-family:Menlo,Consolas,monospace;font-size:12px;color:#27500A;'
                    'font-weight:700;margin-bottom:6px;">Subject: ' + subj + '</div>') if subj else '')
                + '<div style="font-size:13px;color:#2C2C2A;line-height:1.55;">' + body_txt + '</div></div>'
            )
        return (
            '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            'style="border:1px solid #E5E3DC;border-radius:10px;margin:0 0 12px;background:#ffffff;">'
            '<tr>'
            '<td width="64" valign="top" style="padding:14px 0 14px 14px;">'
            '<div style="width:50px;height:50px;border-radius:8px;background:#F4F2EC;text-align:center;'
            'line-height:50px;font-size:20px;font-weight:700;color:#2C2C2A;">' + str(score) + '</div>'
            '</td>'
            '<td valign="top" style="padding:14px 16px;">'
            '<span style="display:inline-block;font-size:12px;font-weight:600;color:' + fg + ';background:' + bg + ';'
            'padding:2px 9px;border-radius:6px;">' + html.escape(cat) + '</span>'
            '<div style="margin:8px 0 3px;font-size:15px;font-weight:600;line-height:1.35;">'
            '<a href="' + link + '" style="color:#1B3A8C;text-decoration:none;">' + title + '</a></div>'
            '<div style="font-size:12px;color:#888780;font-family:Menlo,Consolas,monospace;">' + meta + '</div>'
            '<div style="margin-top:6px;font-size:13px;color:#444441;line-height:1.45;">' + why + '</div>'
            + extra + draft +
            '</td></tr></table>'
        )

    # group by category, in priority order; anything unrecognized goes last
    order = [("Competitor Move", "Competitor Moves"),
             ("Funded Lab", "Funded Labs"),
             ("Research/Methods", "Research and Methods")]
    known = {c for c, _ in order}
    sections = []
    for cat, label in order:
        group = [it for it in items if it.get("category") == cat]
        if not group:
            continue
        _, fg = cat_colors.get(cat, ("", "#444441"))
        sections.append(
            '<div style="font-family:Menlo,Consolas,monospace;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:0.08em;color:' + fg + ';margin:18px 0 10px;">'
            + html.escape(label) + ' (' + str(len(group)) + ')</div>'
            + "".join(card(it) for it in group)
        )
    leftovers = [it for it in items if it.get("category") not in known]
    if leftovers:
        sections.append(
            '<div style="font-family:Menlo,Consolas,monospace;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:0.08em;color:#444441;margin:18px 0 10px;">Other</div>'
            + "".join(card(it) for it in leftovers)
        )
    body = "\n".join(sections)
    today = datetime.now(timezone.utc).strftime("%A, %b %d")
    dash = os.environ.get("DASHBOARD_URL", "")
    dash_link = ('<a href="' + html.escape(dash, quote=True) + '" style="color:#1B3A8C;">Open the full radar</a>'
                 if dash else "the dashboard")
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:#F4F2EC;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="background:#F4F2EC;padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" cellpadding="0" cellspacing="0" width="640" '
        'style="max-width:640px;width:100%;background:#F4F2EC;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">'
        '<tr><td style="padding:0 4px 16px;">'
        '<div style="font-size:22px;font-weight:700;color:#1B3A8C;letter-spacing:-0.01em;">Spatial Radar</div>'
        '<div style="font-size:13px;color:#5F5E5A;margin-top:2px;">' + today + ' &nbsp;&middot;&nbsp; '
        + str(len(items)) + ' new item(s) worth a look</div></td></tr>'
        '<tr><td>' + body + '</td></tr>'
        '<tr><td style="padding:8px 4px 0;font-size:12px;color:#888780;line-height:1.5;">'
        'Items new since the last run, scored at or above your digest bar. '
        'Tune thresholds in scoring_config.json. ' + dash_link + '.</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def send_digest(new_items):
    """Email the run's new items that clear the digest bar. Sends only when there
    is something new, so an every-few-hours cron does not spam. Never raises."""
    dcfg = load_scoring_config().get("digest", DEFAULT_DIGEST)
    if not dcfg.get("enabled", True):
        log("Digest disabled in config, skipping email.")
        return
    if not RESEND_API_KEY:
        warn("RESEND_API_KEY not set, skipping digest email.")
        return
    if not DIGEST_TO:
        warn("DIGEST_TO not set, skipping digest email.")
        return
    bar = dcfg.get("min_score", 55)
    cap = dcfg.get("max_items", 12)
    picks = sorted((it for it in new_items if it.get("score", 0) >= bar),
                   key=lambda x: x.get("score", 0), reverse=True)[:cap]
    if not picks:
        log(f"No new items at or above digest bar {bar}, no email sent.")
        return
    comp = sum(1 for it in picks if it.get("category") == "Competitor Move")
    labs = sum(1 for it in picks if it.get("category") == "Funded Lab")
    bits = []
    if comp:
        bits.append(f"{comp} competitor")
    if labs:
        bits.append(f"{labs} lab" + ("s" if labs != 1 else ""))
    breakdown = (" (" + ", ".join(bits) + ")") if bits else ""
    date_str = datetime.now(timezone.utc).strftime("%b %d")
    subject = f"{dcfg.get('subject_prefix', 'Spatial Radar')}: {len(picks)} new{breakdown}, {date_str}"
    payload = {
        "from": dcfg.get("from", "Spatial Radar <onboarding@resend.dev>"),
        "to": [DIGEST_TO],
        "subject": subject,
        "html": compose_digest_html(picks),
    }
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}
    try:
        resp = http_post_json(RESEND_URL, payload, headers=headers, timeout=30)
        log(f"Digest sent to {DIGEST_TO}: {len(picks)} items, id={resp.get('id', '?')}")
    except Exception as e:
        detail = ""
        try:
            if hasattr(e, "read"):
                detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        code = getattr(e, "code", "")
        warn(f"Digest send failed: HTTP {code}. Resend said: {detail or e}")


def main():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    existing = load_existing()
    seen_ids = {it["id"] for it in existing.get("items", [])}
    seen_titles = {title_key(it.get("title", "")) for it in existing.get("items", [])}
    blocked_pubs = [p.lower() for p in (sources.get("blocked_publishers") or DEFAULT_BLOCKED_PUBLISHERS)]
    blocked_phrases = [p.lower() for p in (sources.get("blocked_title_phrases") or DEFAULT_BLOCKED_PHRASES)]

    # gather raw items from all sources
    raw = []
    for src in sources.get("rss_sources", []):
        raw.extend(fetch_rss(src))
    if "pubmed" in sources:
        raw.extend(fetch_pubmed(sources["pubmed"]))
    if "nih_reporter" in sources:
        raw.extend(fetch_nih_reporter(sources["nih_reporter"]))
    if "nsf_awards" in sources:
        raw.extend(fetch_nsf(sources["nsf_awards"]))
    if "mi_news" in sources:
        raw.extend(fetch_html_news(sources["mi_news"]))

    # dedupe and keep only new (by id AND by normalized title), within lookback window
    new_items = []
    skipped_old = 0
    skipped_noise = 0
    for it in raw:
        if not is_recent(it.get("date", "")):
            skipped_old += 1
            continue
        if is_market_noise(it, blocked_pubs, blocked_phrases):
            skipped_noise += 1
            continue
        iid = item_id(it.get("link", ""), it.get("title", ""))
        tkey = title_key(it.get("title", ""))
        if iid in seen_ids or (tkey and tkey in seen_titles):
            continue
        seen_ids.add(iid)
        if tkey:
            seen_titles.add(tkey)
        it["id"] = iid
        new_items.append(it)

    log(f"{len(new_items)} new items to score (of {len(raw)} fetched, "
        f"{skipped_old} older than {max_age_days()} days, {skipped_noise} stock-noise)")

    # score new items
    scored_new = []
    dropped = 0
    for i, it in enumerate(new_items, 1):
        s = score_item(it)
        it.update(s)
        terr_code, region = classify_territory(it.get("state", ""), it.get("institution", ""), it.get("city", ""))
        it["territory"] = terr_code
        it["region"] = region
        it["owner"] = owner_for(terr_code)
        # Impact Brief: extra analysis layer, competitor items only (one more API call each).
        if it.get("category") == "Competitor Move":
            brief = generate_impact_brief(it)
            if brief:
                it["impact_brief"] = brief
            time.sleep(0.2)
        # Outreach Angle: prospecting layer, funded-lab items only.
        elif it.get("category") == "Funded Lab":
            angle = generate_outreach_angle(it)
            if angle:
                it["outreach_angle"] = angle
            time.sleep(0.2)
        it["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if it.get("score", 0) < min_score_for(it.get("category", "")):
            dropped += 1
        else:
            scored_new.append(it)
        if i % 10 == 0:
            log(f"scored {i}/{len(new_items)}")
        time.sleep(0.2)  # gentle pacing

    log(f"kept {len(scored_new)} new items, dropped {dropped} below category thresholds")

    # merge, sort, cap. Re-filter stored items by score AND recency.
    kept_existing = [it for it in existing.get("items", [])
                     if it.get("score", 0) >= min_score_for(it.get("category", ""))
                     and is_recent(it.get("date", ""))
                     and not is_market_noise(it, blocked_pubs, blocked_phrases)]
    all_items = scored_new + kept_existing
    all_items.sort(key=lambda x: (x.get("score", 0), x.get("fetched", "")), reverse=True)
    all_items = all_items[:MAX_ITEMS_KEPT]

    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(all_items),
        "new_this_run": len(scored_new),
        "categories": CATEGORIES,
        "items": all_items,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log(f"Wrote {len(all_items)} items to data.json ({len(scored_new)} new)")

    # Email digest of this run's new items (sends only if any clear the bar).
    send_digest(scored_new)


if __name__ == "__main__":
    main()
