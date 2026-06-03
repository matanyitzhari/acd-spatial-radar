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

CATEGORIES = ["Competitor Move", "Research/Methods", "Funded Lab"]
USER_AGENT = "ACD-Spatial-Radar/1.0 (sales intelligence; contact rep)"
MAX_ITEMS_KEPT = 400  # cap the stored set so data.json stays small
MIN_SCORE = 30        # items scoring below this are dropped, never shown
DAYS_LOOKBACK = 120   # only keep items from the last N days (about 4 months)
TERRITORIES_PATH = os.path.join(ROOT, "territories.json")

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


def classify_territory(state, institution=""):
    """Return (territory_code, region) for a given state and institution.
    Region is 'East' or 'West'. Returns ('National', 'National') when no
    usable location is present (most research and competitor items).
    Applies the MA and MD account-level overrides by institution name."""
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
    published version (same title, different link) collapse into one."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9 ]", "", t)   # drop punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t[:120]


def is_recent(date_str, days=DAYS_LOOKBACK):
    """True if date_str parses to within the last `days`. If the date cannot
    be parsed, return True (keep it) so we never silently drop good items on a
    formatting quirk. Scoring + threshold still gate it."""
    if not date_str:
        return True
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    s = date_str.strip()
    fmts = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
            "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
            "%Y/%m/%d", "%d %b %Y", "%b %d, %Y", "%Y %b %d", "%Y"]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s[:len(fmt)+8], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= cutoff
        except Exception:
            continue
    # try a loose YYYY-MM-DD extraction
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return dt >= cutoff
        except Exception:
            pass
    return True  # unparseable: keep


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
            out.append({
                "title": title,
                "link": normalize_link(link, src["name"]),
                "summary": summary[:1200],
                "source": src["name"],
                "category_hint": src.get("category_hint", ""),
                "date": date,
            })
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
        cutoff = (now - timedelta(days=DAYS_LOOKBACK)).strftime("%Y-%m-%d")
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
            "include_fields": [
                "ProjectTitle", "AbstractText", "FiscalYear", "OrgName",
                "PrincipalInvestigators", "AwardAmount", "ApplId", "ProjectNum",
                "ProjectStartDate", "AwardNoticeDate", "OrgState", "OrgCity",
            ],
            "offset": 0,
            "limit": cfg.get("limit", 50),
            "sort_field": "award_notice_date",
            "sort_order": "desc",
        }
        time.sleep(1.0)  # NIH asks for <= 1 request/second
        resp = http_post_json(cfg["url"], payload)
        for r in resp.get("results", []):
            title = clean_text(r.get("project_title", ""))
            if not title:
                continue
            org_obj = r.get("organization") or {}
            org = org_obj.get("org_name", "") or r.get("org_name", "")
            org_state = org_obj.get("org_state", "") or r.get("org_state", "")
            org_city = org_obj.get("org_city", "") or r.get("org_city", "")
            pis = ", ".join(
                clean_text(pi.get("full_name", ""))
                for pi in (r.get("principal_investigators") or [])
            )
            amt = r.get("award_amount")
            item_date = clean_text(str(r.get("award_notice_date") or r.get("project_start_date", "")))
            summary = f"PI: {pis}. Institution: {org}. " \
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


def fetch_nsf(cfg):
    """NSF Awards API. GET with keyword + printFields + date range (mm/dd/yyyy).
    Response: {'response': {'award': [...]}}. Names PI and awardee institution
    with a state code, so these classify into territories like NIH items."""
    out = []
    try:
        from datetime import timedelta
        start = (datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)).strftime("%m/%d/%Y")
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
        start = (datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)).strftime("%Y-%m-%d")
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

You score one item at a time. Instead of one gut number, rate four axes from 0 to 10, then derive a headline score. Reason about each axis honestly.

AXES (0 to 10 each):
1. relevance: How squarely is this in the spatial biology / in situ hybridization / RNAscope world? 10 = directly about RNAscope, smFISH, spatial transcriptomics, or multiplexed in situ imaging. 5 = adjacent (single-cell, general genomics) with a plausible spatial angle. 0 = unrelated biology.
2. buying_signal: How strongly does this point to a near-term purchase or active lab? 10 = a newly funded grant naming a spatial/ISH aim, or a lab clearly standing up this capability. 5 = an active lab publishing relevant work. 0 = no commercial implication.
3. competitive_urgency: How much does the field team need to hear this now to defend deals? 10 = a direct competitor launching, repositioning, partnering, or getting funded. 0 = no competitive angle.
   Direct and emerging competitors to watch closely:
   - Molecular Instruments (HCR RNA-CISH and RNA-FISH; positioned head-to-head against RNAscope on speed and price. Treat any MI news as high urgency.)
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
   Adjacent ecosystem (score moderate urgency, useful context not direct threat): Visiopharm, Grundium, Indica Labs (HALO), and pathology/imaging analysis vendors.
4. recency: 10 = within the last week. 5 = within a month. 0 = older.

NOISE CONTROL: A generic preprint or paper with no clear spatial, in situ, or commercial hook should score LOW on relevance and buying_signal, which will pull its headline score below the display threshold. Do not inflate a routine single-cell or genomics paper just because it mentions tissue. Reserve high research scores for work that genuinely uses or compares RNAscope, smFISH, HCR, MERFISH, or multiplexed in situ imaging, or that reveals an active high-value lab.

HIGH-VALUE LAB PROFILES: ACD field reps target these 16 research personas. A grant or paper matching one of these is a strong RNAscope prospect EVEN IF it never says "spatial" or "RNAscope", because these labs need in situ RNA validation. Treat a clear match as high relevance and high buying_signal:
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

HEADLINE SCORE: compute as a weighted blend, scaled to 0 to 100:
score = round( (relevance*3 + buying_signal*2.5 + competitive_urgency*2.5 + recency*2) / 100 * 100 )
Then apply one tiebreak rule: when two items would score within 3 points of each other and one is a competitor move, nudge the competitor item up by 3. Competitor intelligence wins ties because a missed launch costs the team across every deal at once.

CATEGORY: assign exactly one from: Competitor Move, Research/Methods, Funded Lab.

WHY: write a one-line hook of at most 24 words. Be concrete, not vague. Name the specific lever where present: gene targets, tissue or disease, method, PI name, institution, or the competing product. Tell the rep what to note or do. Do not use em dashes.

Respond with ONLY a JSON object, no preamble, no markdown fences:
{"relevance": <int>, "buying_signal": <int>, "competitive_urgency": <int>, "recency": <int>, "score": <int>, "category": "<one of the four>", "why": "<one line>"}"""


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
        score = int(parsed.get("score", 0))
        category = parsed.get("category", "")
        if category not in CATEGORIES:
            category = item.get("category_hint") or "Research/Methods"
        why = clean_text(parsed.get("why", "")).replace("\u2014", "-")
        return {
            "score": max(0, min(100, score)),
            "category": category,
            "why": why,
            "subscores": {
                "relevance": parsed.get("relevance"),
                "buying_signal": parsed.get("buying_signal"),
                "competitive_urgency": parsed.get("competitive_urgency"),
                "recency": parsed.get("recency"),
            },
        }
    except Exception as e:
        warn(f"Scoring failed for '{item['title'][:60]}': {e}")
        return {"score": 40, "category": item.get("category_hint") or "Research/Methods",
                "why": "Could not score automatically, review manually."}


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


def main():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    existing = load_existing()
    seen_ids = {it["id"] for it in existing.get("items", [])}
    seen_titles = {title_key(it.get("title", "")) for it in existing.get("items", [])}

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

    # dedupe and keep only new (by id AND by normalized title), within lookback window
    new_items = []
    skipped_old = 0
    for it in raw:
        if not is_recent(it.get("date", "")):
            skipped_old += 1
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

    log(f"{len(new_items)} new items to score (of {len(raw)} fetched, {skipped_old} older than {DAYS_LOOKBACK} days)")

    # score new items
    scored_new = []
    dropped = 0
    for i, it in enumerate(new_items, 1):
        s = score_item(it)
        it.update(s)
        terr_code, region = classify_territory(it.get("state", ""), it.get("institution", ""))
        it["territory"] = terr_code
        it["region"] = region
        it["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if it.get("score", 0) < MIN_SCORE:
            dropped += 1
        else:
            scored_new.append(it)
        if i % 10 == 0:
            log(f"scored {i}/{len(new_items)}")
        time.sleep(0.2)  # gentle pacing

    log(f"kept {len(scored_new)} new items, dropped {dropped} below score {MIN_SCORE}")

    # merge, sort, cap. Re-filter stored items by score AND recency.
    kept_existing = [it for it in existing.get("items", [])
                     if it.get("score", 0) >= MIN_SCORE and is_recent(it.get("date", ""))]
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


if __name__ == "__main__":
    main()
