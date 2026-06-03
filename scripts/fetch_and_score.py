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
            elif t in ("description", "summary", "abstract"):
                summary = clean_text(child.text)
            elif t in ("pubDate", "published", "updated", "date"):
                date = clean_text(child.text)
        if title:
            out.append({
                "title": title,
                "link": link,
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

SCORING_SYSTEM = """You are a sales intelligence analyst for Advanced Cell Diagnostics (ACD), a Bio-Techne brand. ACD sells RNAscope in situ hybridization and spatial biology products to academic researchers in the United States.

You score one news or research item at a time for how relevant it is to an ACD field sales rep. Reps care about, in rough priority order:
1. Funded labs (a newly funded grant on spatial biology, in situ hybridization, or RNAscope is a strong buying signal. The PI may need probes or instruments soon).
2. Competitor moves (product launches, platform updates, or positioning from 10x Genomics, Bruker/NanoString, Vizgen, Akoya/Quanterix and similar. Reps need to know what they are up against).
3. Research and methods (new papers using or comparing spatial transcriptomics, smFISH, multiplexed imaging, or RNAscope itself. Useful for talking tracks and finding active labs).
4. Own/Bio-Techne news (ACD or Bio-Techne announcements the rep should be aware of).

Score 0 to 100:
- 80 to 100: directly actionable. A funded spatial/ISH lab, a major competitor launch, a high-profile paper using a relevant method.
- 50 to 79: relevant context worth a glance.
- 20 to 49: tangential. Adjacent biology but not clearly spatial or ISH.
- 0 to 19: not relevant.

Assign exactly one category from: Competitor Move, Research/Methods, Funded Lab, Own/Bio-Techne.

Write a one-line "why it matters" of at most 22 words, plain and specific, telling the rep what to do or note. Do not use em dashes.

Respond with ONLY a JSON object, no preamble, no markdown fences:
{"score": <int>, "category": "<one of the four>", "why": "<one line>"}"""


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
        return {"score": max(0, min(100, score)), "category": category, "why": why}
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

    # gather raw items from all sources
    raw = []
    for src in sources.get("rss_sources", []):
        raw.extend(fetch_rss(src))
    if "pubmed" in sources:
        raw.extend(fetch_pubmed(sources["pubmed"]))
    if "nih_reporter" in sources:
        raw.extend(fetch_nih_reporter(sources["nih_reporter"]))

    # dedupe and keep only new
    new_items = []
    for it in raw:
        iid = item_id(it.get("link", ""), it.get("title", ""))
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        it["id"] = iid
        new_items.append(it)

    log(f"{len(new_items)} new items to score (of {len(raw)} fetched)")

    # score new items
    scored_new = []
    for i, it in enumerate(new_items, 1):
        s = score_item(it)
        it.update(s)
        it["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        scored_new.append(it)
        if i % 10 == 0:
            log(f"scored {i}/{len(new_items)}")
        time.sleep(0.2)  # gentle pacing

    # merge, sort, cap
    all_items = scored_new + existing.get("items", [])
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
