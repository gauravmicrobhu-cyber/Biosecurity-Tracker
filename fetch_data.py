#!/usr/bin/env python3
"""
fetch_data.py — automated data refresh for the CONTAINMENT biosecurity tracker.

Runs server-side (via GitHub Actions), not in the browser. This matters for two
separate reasons, not one:
  1. CORS: browsers block cross-origin JS fetches; a server-side script isn't
     subject to that restriction at all.
  2. IP reputation: GDELT's rate-limit/abuse response was reproduced even via
     direct browser navigation (which bypasses CORS entirely), meaning it was
     the requesting network's IP being flagged, not a CORS issue. GitHub Actions
     runners use Microsoft/GitHub IP ranges, which are very unlikely to already
     be caught up in that block.

WHAT THIS SCRIPT DOES:
  - GDELT news-volume z-scores for six tracked topics -> data.json "signals".
    A raw z-score is NOT trusted on its own (see "WHY HEADLINES GATE THE SPIKES").
  - CDC NWSS wastewater coverage snapshot and Europe PMC preprint-volume counts
    -> data.json "upstreamIndicators".
  - Optional ReliefWeb supplement (skipped unless RELIEFWEB_APPNAME is set).

WHY HEADLINES GATE THE SPIKES (added after live data showed 3 of 5 logged spikes
were noise):
  These topics match only a handful of articles a day, so baselines are ~0.000-0.005%
  and one extra headline can produce a z-score of 5-9. A z-score cannot tell one stray
  op-ed from a real cluster. So when a topic crosses the threshold, the script pulls the
  actual headlines and checks whether any of them mention the topic. If none do, the
  spike is DISMISSED as noise (status returns to "normal", the raw z-score and the
  reason stay visible in the note). If headlines cannot be retrieved at all, the spike
  is kept but flagged unverified. Queries were also tightened (English-language,
  health-context terms, "-COVID" for the lab topic), each with a legacy fallback query
  in case GDELT rejects the new syntax.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - It does NOT touch outbreaks, governance, AI-Bio, or synthesis-screening data
    beyond the optional ReliefWeb supplement. WHO's Disease Outbreak News is
    JavaScript-rendered from an undocumented API. That content stays on the manual
    "ask Claude to check" workflow.
  - It does NOT overwrite meta.contentUpdatedAt / meta.contentSource. Those record when
    the manually curated content was last refreshed. meta.updatedAt only means "the
    automated layer last ran", so the two are kept apart on purpose.

USAGE:
  python3 fetch_data.py                # updates ./data.json in place
  python3 fetch_data.py --dry-run      # prints what would change, writes nothing
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

DATA_PATH = "data.json"
GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
RELIEFWEB_ENDPOINT = "https://api.reliefweb.int/v2/reports"
CDC_NWSS_ENDPOINT = "https://data.cdc.gov/resource/2ew6-ywp6.json"  # NWSS Public SARS-CoV-2 Wastewater Metric Data (Socrata)
EUROPEPMC_ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Six topics tracked in the dashboard's Signal Detection module.
#   query          primary GDELT query (boolean OR groups must be wrapped in parentheses;
#                  multiple groups are ANDed; "-term" excludes; sourcelang: restricts language)
#   fallback_query legacy query, used only if GDELT rejects the primary as a syntax error
#   match_terms    lower-case words used for (a) the headline-relevance check and
#                  (b) corroboration against tracked outbreaks
SIGNAL_KEYWORDS = [
    {"id": "hemfever-drc", "label": "Hemorrhagic fever — Central Africa",
     "query": 'hemorrhagic fever (DRC OR Congo OR Uganda OR "South Sudan")',
     "match_terms": ["ebola", "marburg", "hemorrhagic", "haemorrhagic", "filovirus", "bundibugyo", "lassa", "crimean"]},
    {"id": "unknown-pneumonia", "label": "Unknown pneumonia / mystery illness cluster",
     "query": '("unknown pneumonia" OR "mystery illness" OR "undiagnosed pneumonia") outbreak sourcelang:english',
     "fallback_query": '("unknown pneumonia" OR "mystery illness") outbreak',
     "match_terms": ["pneumonia", "mystery illness", "unknown illness", "unexplained", "undiagnosed", "mysterious"]},
    {"id": "mass-illness-sasia", "label": "Mass illness — South Asia",
     "query": '("mass illness" OR "mysterious illness" OR "unknown disease") (India OR Pakistan OR Bangladesh) (hospitalized OR hospitalised OR outbreak OR "health officials") sourcelang:english',
     "fallback_query": '("mass illness" OR "unknown disease") (India OR Pakistan OR Bangladesh)',
     "match_terms": ["mass illness", "mysterious", "unknown disease", "mystery illness", "fall ill", "fell ill", "falls ill",
                     "food poisoning", "hospitalised", "hospitalized"]},
    {"id": "cholera-global", "label": "Cholera outbreak — Global",
     "query": "cholera outbreak",
     "match_terms": ["cholera"]},
    {"id": "avian-flu-human", "label": "Avian influenza — human cases",
     "query": '("avian influenza" OR "bird flu" OR H5N1) ("human case" OR "human cases" OR "human infection" OR "human infections") sourcelang:english',
     "fallback_query": '"avian influenza" human case',
     "match_terms": ["avian", "bird flu", "h5n1", "h5n5", "h9n2", "h5n6"]},
    {"id": "lab-biosafety", "label": "Lab biosafety incident / breach",
     "query": '("biosafety incident" OR "biosafety breach" OR "laboratory-acquired infection" OR "lab accident" OR "laboratory accident" OR biolab) -COVID sourcelang:english',
     "fallback_query": "laboratory biosafety (incident OR breach OR leak)",
     "match_terms": ["biosafety", "biolab", "laboratory", "lab leak", "lab accident", "lab worker", "lab-acquired", "lab exposure"]},
]

REQUEST_SPACING_SEC = 45  # widened after live testing: GitHub's shared cloud IP pool got HTTP 429
                           # (a real rate-limit response, not a ban) even at 6s spacing. This runs
                           # unattended, so the extra minutes cost nothing.


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", file=sys.stderr)


def fetch_gdelt_timeline(query, timespan="60d"):
    url = f"{GDELT_ENDPOINT}?query={urllib.parse.quote(query)}&mode=timelinevol&format=json&timespan={timespan}"
    req = urllib.request.Request(url, headers={"User-Agent": "prism-containment-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if "limit" in text.lower() or "5 second" in text.lower():
            raise RuntimeError("Rate limited by GDELT — even paced server-side requests hit this; back off further.")
        raise RuntimeError(f"GDELT returned non-JSON: {text[:150]}")
    raw = (data.get("timeline") or [{}])[0].get("data")
    if not raw:
        raise RuntimeError("Unexpected GDELT response shape (no timeline data)")
    series = []
    for point in raw:
        d = point.get("date", "")
        v = point.get("value", point.get("count", 0))
        series.append(float(v))
    return series


def compute_signal(values):
    if len(values) < 8:
        return {"status": "insufficient", "z": None}
    latest = values[-1]
    baseline = values[:-1]
    mean = sum(baseline) / len(baseline)
    variance = sum((x - mean) ** 2 for x in baseline) / len(baseline)
    sd = variance ** 0.5 or 0.0001
    z = (latest - mean) / sd
    status = "spike" if z >= 3 else "elevated" if z >= 1.75 else "normal"
    return {"status": status, "z": round(z, 2), "mean": round(mean, 3), "latest": round(latest, 3)}


def title_matches(title, terms):
    t = (title or "").lower()
    return any(term in t for term in terms)


def assess_headlines(articles, match_terms):
    """Does the news behind a statistical spike actually mention the topic?
    Returns (verdict, relevant_count):
      unavailable  headlines could not be fetched (articles is None) — keep the spike, flag unverified
      no-articles  fetch worked but GDELT returned nothing — the spike is a data artifact
      unconfirmed  articles came back but none mention the topic — noise
      confirmed    at least one headline mentions the topic
    """
    if articles is None:
        return "unavailable", 0
    if not articles:
        return "no-articles", 0
    relevant = sum(1 for a in articles if title_matches(a.get("title", ""), match_terms))
    return ("confirmed" if relevant else "unconfirmed"), relevant


def compute_corroboration(signal_id, match_terms, status, outbreaks, other_statuses):
    """Corroborate against tracked outbreaks using topic-specific terms (NOT generic label
    words: matching on words like 'influenza' or 'cases' linked an avian-flu spike to an
    unrelated seasonal H1N1 entry). Closed outbreaks never corroborate a live spike."""
    if status not in ("elevated", "spike"):
        return None, None
    terms = [t.lower() for t in (match_terms or [])]
    for o in outbreaks:
        if "closed" in (o.get("status") or "").lower():
            continue
        haystack = " ".join([o.get("title", ""), " ".join(o.get("tags", []))]).lower()
        if any(t in haystack for t in terms):
            return True, f"Matches tracked outbreak: \"{o['title']}\""
    other_elevated = sum(1 for sid, s in other_statuses.items() if sid != signal_id and s in ("elevated", "spike"))
    if other_elevated:
        return True, f"{other_elevated} other keyword(s) also elevated simultaneously"
    return False, "Single, isolated signal — no matching tracked event or concurrent spike. Treat with extra caution."


def with_retry(fn, *args, max_retries=4, backoff_sec=45):
    """Retry on rate limiting and plain network flakiness; fail fast on errors that waiting
    won't fix (e.g. a rejected query)."""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            is_retryable = "429" in msg or "timed out" in msg or "timeout" in msg
            if attempt < max_retries and is_retryable:
                log(f"  {type(e).__name__} ({e}), retrying in {backoff_sec}s ({attempt+1}/{max_retries})...")
                time.sleep(backoff_sec)
            elif not is_retryable:
                break
    raise last_err


def fetch_series_with_fallback(kw):
    """Try the tightened query; if GDELT rejects it as a syntax problem (non-JSON reply),
    fall back once to the legacy query so a bad guess can't silently kill a signal."""
    try:
        return with_retry(fetch_gdelt_timeline, kw["query"]), kw["query"], False
    except Exception as e:
        if kw.get("fallback_query") and "non-json" in str(e).lower():
            log(f"  primary query rejected for {kw['id']} ({e}); using legacy fallback query")
            return with_retry(fetch_gdelt_timeline, kw["fallback_query"]), kw["fallback_query"], True
        raise


def fetch_gdelt_articles(query, max_records=5, timespan="7d"):
    """Real headlines behind an elevated/spiking signal — only called when a signal is
    already flagged, so this doesn't add extra load on every keyword every run."""
    url = (f"{GDELT_ENDPOINT}?query={urllib.parse.quote(query)}&mode=artlist&format=json"
           f"&maxrecords={max_records}&timespan={timespan}")
    req = urllib.request.Request(url, headers={"User-Agent": "prism-containment-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"GDELT artlist returned non-JSON: {text[:150]}")
    articles = data.get("articles", [])
    return [
        {"title": a.get("title", "Untitled"), "url": a.get("url", ""),
         "domain": a.get("domain", ""), "date": a.get("seendate", "")}
        for a in articles[:max_records]
    ]


def run_gdelt_pass(data):
    outbreaks = data.get("outbreaks", [])
    old_items_by_id = {i["id"]: i for i in data.get("signals", {}).get("items", [])}
    results, statuses = {}, {}
    for i, kw in enumerate(SIGNAL_KEYWORDS):
        if i > 0:
            time.sleep(REQUEST_SPACING_SEC)
        try:
            series, used_query, used_fallback = fetch_series_with_fallback(kw)
            sig = compute_signal(series)
            res = {"label": kw["label"], **sig, "usedFallback": used_fallback}
            log(f"OK  {kw['id']}: {sig['status']} (z={sig.get('z')})")
            if sig["status"] in ("elevated", "spike"):
                time.sleep(REQUEST_SPACING_SEC)
                articles = None
                try:
                    articles = with_retry(fetch_gdelt_articles, used_query, 10, max_retries=2)
                    log(f"     +{len(articles)} article(s) fetched for {kw['id']}")
                except Exception as e:
                    log(f"     article fetch failed for {kw['id']}: {e}")
                verdict, relevant = assess_headlines(articles, kw["match_terms"])
                res.update({"articles": articles or [], "headlineCheck": verdict, "relevantHeadlines": relevant})
                if verdict in ("unconfirmed", "no-articles"):
                    reason = ("none of the %d retrieved headlines mention this topic" % len(articles)
                              if verdict == "unconfirmed"
                              else "GDELT returned no matching articles for the last 7 days, so it is likely a data artifact")
                    res.update({"rawStatus": sig["status"], "status": "normal", "dismissed": True,
                                "dismissNote": f"Raw z-score {sig['z']} ({sig['status']}) dismissed as noise: {reason}."})
                    log(f"     DISMISSED {kw['id']}: {reason}")
            results[kw["id"]] = res
            statuses[kw["id"]] = res["status"]
        except Exception as e:
            results[kw["id"]] = {"label": kw["label"], "status": "error", "note": f"Fetch failed: {e}"}
            statuses[kw["id"]] = "error"
            log(f"FAIL {kw['id']}: {e}")

    items = []
    for kw in SIGNAL_KEYWORDS:
        r = results[kw["id"]]
        corroborated, corrob_note = compute_corroboration(kw["id"], kw["match_terms"], r.get("status"), outbreaks, statuses)
        if r.get("note"):
            note = r["note"]
        elif r.get("z") is None:
            note = "Insufficient data points for a baseline."
        else:
            note = f"z-score {r['z']} against 60-day baseline (mean {r['mean']}%, latest {r['latest']}%)."
            if r.get("dismissed"):
                note += " " + r["dismissNote"]
            elif r.get("headlineCheck") == "unavailable":
                note += " Headline check unavailable this run — treat as unverified."
            if r.get("usedFallback"):
                note += " (Legacy query used — the refined query was rejected by GDELT.)"
        items.append({
            "id": kw["id"], "label": kw["label"], "status": r.get("status", "error"), "note": note,
            "corroborated": bool(corroborated), "corrobNote": corrob_note or "",
            "articles": r.get("articles", []),
            "headlineCheck": r.get("headlineCheck"), "dismissed": bool(r.get("dismissed")),
        })

    # History: one entry per fresh transition INTO elevated/spike (not one per 12h cycle).
    history = data.get("signalHistory", [])
    now = datetime.now(timezone.utc)
    for item in items:
        old_status = old_items_by_id.get(item["id"], {}).get("status")
        if item["status"] in ("elevated", "spike") and old_status not in ("elevated", "spike"):
            history.insert(0, {
                "id": item["id"], "label": item["label"], "status": item["status"],
                "note": item["note"], "corroborated": item["corroborated"], "corrobNote": item["corrobNote"],
                "articles": item.get("articles", []), "headlineCheck": item.get("headlineCheck"),
                "detectedAt": now.isoformat(),
            })
            log(f"     NEW history entry logged for {item['id']} ({item['status']})")
        elif item["status"] in ("elevated", "spike") and item.get("articles"):
            # Backfill: a spike logged earlier with no headlines (fetch failed) gets them if it is still live.
            for h in history:
                if h["id"] == item["id"] and not h.get("articles"):
                    try:
                        age = now - datetime.fromisoformat(h["detectedAt"])
                    except Exception:
                        break
                    if age <= timedelta(hours=72):
                        h["articles"], h["headlineCheck"] = item["articles"], item.get("headlineCheck")
                        log(f"     backfilled headlines into history entry for {item['id']}")
                    break
    data["signalHistory"] = history[:30]

    data["signals"] = {
        "checkedAt": now.isoformat(),
        "checkedBy": "Automated — GitHub Actions + live GDELT z-score (mode=timelinevol), headline-checked",
        "items": items,
    }
    return data


def run_wastewater_pass(data):
    """CDC NWSS wastewater coverage snapshot. Deliberately conservative: this reports
    record/site COUNTS only, not a computed trend — the exact field names in this
    Socrata dataset weren't verifiable from this environment (same domain-allowlist
    restriction that's affected every live API test in this project), so rather than
    guess at a field name and silently compute a wrong trend, this only reports what
    can be safely derived from any reasonable shape: how many records came back and
    how many distinct site-like values appear in them."""
    try:
        url = f"{CDC_NWSS_ENDPOINT}?$limit=500&$order=date_end%20DESC"
        req = urllib.request.Request(url, headers={"User-Agent": "prism-containment-tracker/1.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            records = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(records, list) or not records:
            raise RuntimeError("empty or unexpected response shape")
        site_field = next((k for k in records[0] if "key_plot_id" in k or "wwtp" in k.lower() or "site" in k.lower()), None)
        distinct_sites = len({r.get(site_field) for r in records if site_field and r.get(site_field)}) if site_field else None
        date_field = next((k for k in records[0] if "date" in k.lower()), None)
        latest_date = max((r.get(date_field, "") for r in records), default="") if date_field else ""
        data["upstreamIndicators"] = data.get("upstreamIndicators", {})
        data["upstreamIndicators"]["wastewater"] = {
            "status": "ok",
            "recordCount": len(records),
            "distinctSites": distinct_sites,
            "mostRecentDate": latest_date,
            "note": f"{len(records)} recent CDC NWSS wastewater records" +
                    (f" across {distinct_sites} distinct sites" if distinct_sites else "") +
                    ". Coverage snapshot only — not a computed trend, since this dataset's exact field "
                    "schema wasn't independently verified before building this integration.",
        }
        log(f"OK  wastewater: {len(records)} records, {distinct_sites} sites")
    except Exception as e:
        data["upstreamIndicators"] = data.get("upstreamIndicators", {})
        data["upstreamIndicators"]["wastewater"] = {"status": "error", "note": f"Fetch failed: {e}"}
        log(f"FAIL wastewater: {e}")
    return data


# ---- Preprint volume via Europe PMC ------------------------------------------------------
# Replaces the earlier bioRxiv/medRxiv date-window scan, which only read the first ~400
# records per server and so reported "0 preprints" for topics as large as Ebola. Europe PMC
# returns an exact hitCount for a query (its preprint index includes bioRxiv and medRxiv),
# so no pagination or sampling is needed.
PREPRINT_TOPICS = [
    {"label": "ebola / bundibugyo", "terms": ["ebola", "bundibugyo"]},
    {"label": "cholera", "terms": ["cholera"]},
    {"label": "measles", "terms": ["measles"]},
    {"label": "avian influenza / H5N1", "terms": ["avian influenza", "H5N1"]},
    {"label": "mpox", "terms": ["mpox", "monkeypox"]},
    {"label": "nipah", "terms": ["nipah"]},
]


def europepmc_query(terms, start_date, end_date):
    topic = " OR ".join(f'TITLE:"{t}" OR ABSTRACT:"{t}"' for t in terms)
    return f"({topic}) AND (SRC:PPR) AND (FIRST_PDATE:[{start_date} TO {end_date}])"


def fetch_preprint_count(terms, start_date, end_date):
    params = urllib.parse.urlencode({"query": europepmc_query(terms, start_date, end_date),
                                     "format": "json", "pageSize": 1, "resultType": "lite"})
    req = urllib.request.Request(f"{EUROPEPMC_ENDPOINT}?{params}", headers={"User-Agent": "prism-containment-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if "hitCount" not in payload:
        raise RuntimeError("unexpected Europe PMC response (no hitCount)")
    return int(payload["hitCount"])


def run_preprint_pass(data):
    today = datetime.now(timezone.utc).date()
    recent_start, recent_end = (today - timedelta(days=14)).isoformat(), today.isoformat()
    prior_start, prior_end = (today - timedelta(days=28)).isoformat(), (today - timedelta(days=15)).isoformat()
    results = {}
    for topic in PREPRINT_TOPICS:
        label = topic["label"]
        try:
            recent = with_retry(fetch_preprint_count, topic["terms"], recent_start, recent_end, max_retries=2, backoff_sec=10)
            time.sleep(1.5)
            prior = with_retry(fetch_preprint_count, topic["terms"], prior_start, prior_end, max_retries=2, backoff_sec=10)
            time.sleep(1.5)
            ratio = (recent / prior) if prior > 0 else (float("inf") if recent > 0 else 1.0)
            status = "elevated" if (recent >= 5 and ratio >= 2.0) else "normal"
            results[label] = {
                "status": status, "recentCount": recent, "priorCount": prior, "method": "europepmc",
                "note": (f"{recent} preprint(s) with '{label}' in the title or abstract in the last 14 days, vs {prior} in the prior 14 days "
                         f"(preprint servers indexed by Europe PMC, which include bioRxiv and medRxiv)."),
            }
            log(f"OK  preprint/{label}: recent={recent} prior={prior} status={status}")
        except Exception as e:
            results[label] = {"status": "error", "method": "europepmc", "note": f"Fetch failed: {e}"}
            log(f"FAIL preprint/{label}: {e}")
    data["upstreamIndicators"] = data.get("upstreamIndicators", {})
    data["upstreamIndicators"]["preprints"] = results
    return data


def run_reliefweb_pass(data):
    appname = os.environ.get("RELIEFWEB_APPNAME", "").strip()
    if not appname:
        log("Skipping ReliefWeb pass: RELIEFWEB_APPNAME not set. Request an approved appname at "
            "https://apidoc.reliefweb.int/parameters and add it as a repo secret to enable this.")
        return data
    try:
        params = {
            "appname": appname,
            "query[value]": 'epidemic OR outbreak OR biosecurity OR "disease outbreak"',
            "query[operator]": "AND",
            "limit": "10",
            "sort[]": "date:desc",
            "fields[include][]": ["title", "date.created", "source.name", "url_alias", "country.name"],
        }
        url = f"{RELIEFWEB_ENDPOINT}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, headers={"User-Agent": "prism-containment-tracker/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.load(resp)
    except Exception as e:
        log(f"ReliefWeb pass failed (non-fatal): {e}")
        return data

    existing_ids = {o["id"] for o in data.get("outbreaks", [])}
    added = 0
    for item in raw.get("data", []):
        rw_id = f"rw-{item['id']}"
        if rw_id in existing_ids:
            continue
        f = item.get("fields", {})
        created = f.get("date", {}).get("created", datetime.now(timezone.utc).isoformat())
        source = (f.get("source") or [{}])[0].get("name", "ReliefWeb/OCHA")
        countries = [c.get("name") for c in f.get("country", [])] or ["Unspecified"]
        data.setdefault("outbreaks", []).insert(0, {
            "id": rw_id, "date": created[:10], "sortDate": created[:10],
            "title": f.get("title", "Untitled report"), "level": 2, "status": "Needs review",
            "country": countries,
            "desc": f"Auto-pulled from ReliefWeb — review and rewrite before treating as curated. Source: {source}.",
            "more": "", "src": f"ReliefWeb/OCHA — {f.get('url_alias','')}",
            "tags": ["auto-pulled", "needs-review"],
        })
        added += 1
    log(f"ReliefWeb pass added {added} new item(s) as 'Needs review'.")
    return data


def main():
    dry_run = "--dry-run" in sys.argv
    with open(DATA_PATH) as f:
        data = json.load(f)

    data = run_gdelt_pass(data)
    data = run_wastewater_pass(data)
    data = run_preprint_pass(data)
    data = run_reliefweb_pass(data)

    now = datetime.now(timezone.utc).isoformat()
    # updatedAt / signalsUpdatedAt mean "the AUTOMATED layer last ran". meta.contentUpdatedAt and
    # meta.contentSource (manual refresh of outbreaks/governance) are deliberately left untouched.
    data["meta"]["updatedAt"] = now
    data["meta"]["signalsUpdatedAt"] = now
    data["meta"]["source"] = "Automated — GitHub Actions (GDELT live signals" + \
        (" + ReliefWeb supplement)" if os.environ.get("RELIEFWEB_APPNAME") else ", ReliefWeb skipped — no appname)")

    if dry_run:
        print(json.dumps(data["signals"], indent=2))
        log("Dry run — data.json not written.")
        return

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)
    log("data.json updated.")


if __name__ == "__main__":
    main()
