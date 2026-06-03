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

CATEGORIES = ["Competitor Move", "Research/Methods", "Funded Lab", "Own/Bio-Techne"]
USER_AGENT = "ACD-Spatial-Radar/1.0 (sales intelligence; contact rep)"
MAX_ITEMS_KEPT = 400  # cap the stored set so data.json stays small
MIN_SCORE = 30        # items scoring below this are dropped, never shown


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
        payload = {
            "criteria": {
                "advanced_text_search": {
                    "operator": "and",
                    "search_field": "projecttitle,abstracttext,terms",
                    "search_text": cfg["advanced_text_search"],
                },
                "newly_added_projects_only": cfg.get("newly_added_projects_only", True),
            },
            "include_fields": [
                "ProjectTitle", "AbstractText", "FiscalYear", "Organization",
                "PrincipalInvestigators", "AwardAmount", "ProjectNumLink",
                "ProjectStartDate", "AgencyIcAdmin",
            ],
            "offset": 0,
            "limit": cfg.get("limit", 50),
            "sort_field": "fiscal_year",
            "sort_order": "desc",
        }
        time.sleep(1.0)  # NIH asks for <= 1 request/second
        resp = http_post_json(cfg["url"], payload)
        for r in resp.get("results", []):
            title = clean_text(r.get("project_title", ""))
            if not title:
                continue
            org = (r.get("organization") or {}).get("org_name", "")
            pis = ", ".join(
                clean_text(pi.get("full_name", ""))
                for pi in (r.get("principal_investigators") or [])
            )
            amt = r.get("award_amount")
            summary = f"PI: {pis}. Institution: {org}. " \
                      f"Award: {('$' + format(amt, ',')) if amt else 'n/a'}. " \
                      + clean_text(r.get("abstract_text", ""))[:800]
            out.append({
                "title": title,
                "link": r.get("project_num_link") or "https://reporter.nih.gov/",
                "summary": summary,
                "source": "NIH RePORTER",
                "category_hint": cfg.get("category_hint", "Funded Lab"),
                "date": clean_text(str(r.get("project_start_date", ""))),
                "institution": org,
                "pi": pis,
            })
    except Exception as e:
        warn(f"NIH RePORTER failed: {e}. Skipping.")
    log(f"NIH RePORTER: {len(out)} items")
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
   - Leica Biosystems (BOND RX automation, often partnering with the above)
   Adjacent ecosystem (score moderate urgency, useful context not direct threat): Visiopharm, Grundium, Indica Labs (HALO), and pathology/imaging analysis vendors.
4. recency: 10 = within the last week. 5 = within a month. 0 = older.

NOISE CONTROL: A generic preprint or paper with no clear spatial, in situ, or commercial hook should score LOW on relevance and buying_signal, which will pull its headline score below the display threshold. Do not inflate a routine single-cell or genomics paper just because it mentions tissue. Reserve high research scores for work that genuinely uses or compares RNAscope, smFISH, HCR, MERFISH, or multiplexed in situ imaging, or that reveals an active high-value lab.

HEADLINE SCORE: compute as a weighted blend, scaled to 0 to 100:
score = round( (relevance*3 + buying_signal*2.5 + competitive_urgency*2.5 + recency*2) / 100 * 100 )
Then apply one tiebreak rule: when two items would score within 3 points of each other and one is a competitor move, nudge the competitor item up by 3. Competitor intelligence wins ties because a missed launch costs the team across every deal at once.

CATEGORY: assign exactly one from: Competitor Move, Research/Methods, Funded Lab, Own/Bio-Techne.

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

    # dedupe and keep only new (by id AND by normalized title)
    new_items = []
    for it in raw:
        iid = item_id(it.get("link", ""), it.get("title", ""))
        tkey = title_key(it.get("title", ""))
        if iid in seen_ids or (tkey and tkey in seen_titles):
            continue
        seen_ids.add(iid)
        if tkey:
            seen_titles.add(tkey)
        it["id"] = iid
        new_items.append(it)

    log(f"{len(new_items)} new items to score (of {len(raw)} fetched)")

    # score new items
    scored_new = []
    dropped = 0
    for i, it in enumerate(new_items, 1):
        s = score_item(it)
        it.update(s)
        it["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if it.get("score", 0) < MIN_SCORE:
            dropped += 1
        else:
            scored_new.append(it)
        if i % 10 == 0:
            log(f"scored {i}/{len(new_items)}")
        time.sleep(0.2)  # gentle pacing

    log(f"kept {len(scored_new)} new items, dropped {dropped} below score {MIN_SCORE}")

    # merge, sort, cap. Also re-filter any previously stored items under threshold.
    all_items = scored_new + [it for it in existing.get("items", []) if it.get("score", 0) >= MIN_SCORE]
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
