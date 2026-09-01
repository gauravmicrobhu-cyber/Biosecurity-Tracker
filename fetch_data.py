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
     be caught up in that block — this is the actual reason this should work
     where the browser-based version didn't, not just "server-to-server is
     allowed."

WHAT THIS SCRIPT DOES:
  - Fetches GDELT news-volume timelines for each tracked keyword (mode=timelinevol),
    computes a real z-score against each keyword's own recent baseline, and writes
    the result into data.json's "signals" block — replacing Claude's manual
    qualitative check with an actual statistical one, now that live fetching is
    viable from this environment.
  - Attempts a ReliefWeb/OCHA supplement pull for new outbreak-relevant reports.
    This requires a ReliefWeb-*approved* appname (their Nov 2025 policy change) —
    if ARM_RELIEFWEB_APPNAME isn't set to a working value, this step is skipped
    with a warning, not a hard failure, so the GDELT update still lands.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO:
  - It does NOT touch outbreaks, governance, AI-Bio, or synthesis-screening data
    beyond the optional ReliefWeb supplement above. WHO's Disease Outbreak News
    page is JavaScript-rendered from an undocumented internal API — a plain
    requests.get() call gets an empty shell, not outbreak data. That content
    stays on the existing manual "ask Claude to check" workflow.
  - It does NOT assign containment levels to new ReliefWeb items intelligently —
    they land tagged "needs-review" at level 2 until a human retags them.

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
from datetime import datetime, timezone

DATA_PATH = "data.json"
GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
RELIEFWEB_ENDPOINT = "https://api.reliefweb.int/v2/reports"

# Same six keywords tracked in the dashboard's Signal Detection module.
# Boolean OR groups must be wrapped in parentheses — GDELT rejects a bare OR
# outside parens (this cost real debugging time before; documented here so it
# isn't relearned the hard way again).
SIGNAL_KEYWORDS = [
    {"id": "hemfever-drc", "label": "Hemorrhagic fever — Central Africa",
     "query": 'hemorrhagic fever (DRC OR Congo OR Uganda OR "South Sudan")'},
    {"id": "unknown-pneumonia", "label": "Unknown pneumonia / mystery illness cluster",
     "query": '("unknown pneumonia" OR "mystery illness") outbreak'},
    {"id": "mass-illness-sasia", "label": "Mass illness — South Asia",
     "query": '("mass illness" OR "unknown disease") (India OR Pakistan OR Bangladesh)'},
    {"id": "cholera-global", "label": "Cholera outbreak — Global",
     "query": "cholera outbreak"},
    {"id": "avian-flu-human", "label": "Avian influenza — human cases",
     "query": '"avian influenza" human case'},
    {"id": "lab-biosafety", "label": "Lab biosafety incident / breach",
     "query": "laboratory biosafety (incident OR breach OR leak)"},
]

REQUEST_SPACING_SEC = 45  # widened after live testing: GitHub's shared cloud IP pool got HTTP 429
                           # (a real rate-limit response, not a ban) even at 6s spacing — GDELT is
                           # likely throttling that IP range harder due to unrelated traffic from
                           # other GitHub Actions users sharing it. This runs unattended, so the
                           # extra ~4 minutes costs nothing.


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


def compute_corroboration(signal_id, label, status, outbreaks, other_statuses):
    if status not in ("elevated", "spike"):
        return None, None
    key_terms = [w for w in label.lower().replace("—", " ").split() if len(w) > 4]
    for o in outbreaks:
        tags = [t.lower() for t in o.get("tags", [])]
        if any(any(w in t or t in w for w in key_terms) for t in tags):
            return True, f"Matches tracked outbreak: \"{o['title']}\""
    other_elevated = sum(1 for sid, s in other_statuses.items() if sid != signal_id and s in ("elevated", "spike"))
    if other_elevated:
        return True, f"{other_elevated} other keyword(s) also elevated simultaneously"
    return False, "Single, isolated signal — no matching tracked event or concurrent spike. Treat with extra caution."


def fetch_with_retry(query, max_retries=3, backoff_sec=25):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fetch_gdelt_timeline(query)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Retry on rate-limiting AND on plain network flakiness (timeouts, SSL handshake
            # stalls) — live testing showed 3 of 6 keywords failed this way and were almost
            # certainly transient, since other keywords in the same run succeeded fine.
            is_retryable = "429" in msg or "timed out" in msg or "timeout" in msg
            if attempt < max_retries and is_retryable:
                log(f"  {type(e).__name__} ({e}), retrying in {backoff_sec}s ({attempt+1}/{max_retries})...")
                time.sleep(backoff_sec)
            elif not is_retryable:
                break  # don't retry on errors that clearly won't resolve by waiting (e.g. malformed query)
    raise last_err


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
    # Capture the PREVIOUS run's statuses before we overwrite anything — this is what lets
    # us tell "just became elevated" apart from "has been elevated for three runs already",
    # so the history log gets one entry per event, not one per 12-hour cycle it persists.
    old_items_by_id = {i["id"]: i for i in data.get("signals", {}).get("items", [])}
    results = {}
    statuses = {}
    for i, kw in enumerate(SIGNAL_KEYWORDS):
        if i > 0:
            time.sleep(REQUEST_SPACING_SEC)
        try:
            series = fetch_with_retry(kw["query"])
            sig = compute_signal(series)
            results[kw["id"]] = {"label": kw["label"], **sig}
            statuses[kw["id"]] = sig["status"]
            log(f"OK  {kw['id']}: {sig['status']} (z={sig.get('z')})")
            # Only spend an extra request pulling real headlines when there's actually
            # something worth explaining — no point fetching articles for a "normal" reading.
            if sig["status"] in ("elevated", "spike"):
                time.sleep(REQUEST_SPACING_SEC)
                try:
                    articles = fetch_gdelt_articles(kw["query"])
                    results[kw["id"]]["articles"] = articles
                    log(f"     +{len(articles)} article(s) fetched for {kw['id']}")
                except Exception as e:
                    log(f"     article fetch failed for {kw['id']}: {e}")
        except Exception as e:
            results[kw["id"]] = {"label": kw["label"], "status": "error", "note": f"Fetch failed: {e}"}
            statuses[kw["id"]] = "error"
            log(f"FAIL {kw['id']}: {e}")

    items = []
    for kw in SIGNAL_KEYWORDS:
        r = results[kw["id"]]
        corroborated, corrob_note = compute_corroboration(kw["id"], kw["label"], r.get("status"), outbreaks, statuses)
        item = {
            "id": kw["id"],
            "label": kw["label"],
            "status": r.get("status", "error"),
            "note": r.get("note") or (
                f"z-score {r['z']} against 60-day baseline (mean {r['mean']}%, latest {r['latest']}%)."
                if r.get("z") is not None else "Insufficient data points for a baseline."
            ),
            "corroborated": bool(corroborated),
            "corrobNote": corrob_note or "",
            "articles": r.get("articles", []),
        }
        items.append(item)

    # Log a history entry only on a fresh transition INTO elevated/spike — not on every
    # run where an already-known spike is still ongoing, and not on a bare "error" reading.
    history = data.get("signalHistory", [])
    for item in items:
        old_status = old_items_by_id.get(item["id"], {}).get("status")
        if item["status"] in ("elevated", "spike") and old_status not in ("elevated", "spike"):
            history.insert(0, {
                "id": item["id"], "label": item["label"], "status": item["status"],
                "note": item["note"], "corroborated": item["corroborated"], "corrobNote": item["corrobNote"],
                "articles": item.get("articles", []),
                "detectedAt": datetime.now(timezone.utc).isoformat(),
            })
            log(f"     NEW history entry logged for {item['id']} ({item['status']})")
    data["signalHistory"] = history[:30]  # keep this bounded, not an ever-growing file

    data["signals"] = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "checkedBy": "Automated — GitHub Actions + live GDELT z-score (mode=timelinevol)",
        "items": items,
    }
    return data


CDC_NWSS_ENDPOINT = "https://data.cdc.gov/resource/2ew6-ywp6.json"  # NWSS Public SARS-CoV-2 Wastewater Metric Data (Socrata)
BIORXIV_ENDPOINT = "https://api.biorxiv.org/details"

# Small, defensible keyword set for the preprint-volume check — kept short since each
# term needs its own date-range scan across two servers (bioRxiv + medRxiv).
PREPRINT_KEYWORDS = ["ebola", "cholera", "measles", "avian influenza"]


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


def fetch_preprint_count(keyword, start_date, end_date):
    total = 0
    for server in ("biorxiv", "medrxiv"):
        cursor = 0
        while True:
            url = f"{BIORXIV_ENDPOINT}/{server}/{start_date}/{end_date}/{cursor}"
            req = urllib.request.Request(url, headers={"User-Agent": "prism-containment-tracker/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            items = payload.get("collection", [])
            for it in items:
                text = (it.get("title", "") + " " + it.get("abstract", "")).lower()
                if keyword.lower() in text:
                    total += 1
            if len(items) < 100:
                break
            cursor += 100
            if cursor > 300:  # hard cap — keep this bounded, it's a volume check not a full census
                break
    return total


def run_preprint_pass(data):
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    recent_start, recent_end = (today - timedelta(days=14)).isoformat(), today.isoformat()
    prior_start, prior_end = (today - timedelta(days=28)).isoformat(), (today - timedelta(days=15)).isoformat()
    results = {}
    for kw in PREPRINT_KEYWORDS:
        try:
            recent = fetch_preprint_count(kw, recent_start, recent_end)
            time.sleep(3)
            prior = fetch_preprint_count(kw, prior_start, prior_end)
            time.sleep(3)
            ratio = (recent / prior) if prior > 0 else (float("inf") if recent > 0 else 1.0)
            status = "elevated" if (recent >= 3 and ratio >= 2.0) else "normal"
            results[kw] = {
                "status": status, "recentCount": recent, "priorCount": prior,
                "note": f"{recent} preprint(s) mentioning '{kw}' in the last 14 days (bioRxiv+medRxiv), vs {prior} in the prior 14 days.",
            }
            log(f"OK  preprint/{kw}: recent={recent} prior={prior} status={status}")
        except Exception as e:
            results[kw] = {"status": "error", "note": f"Fetch failed: {e}"}
            log(f"FAIL preprint/{kw}: {e}")
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

    data["meta"]["updatedAt"] = datetime.now(timezone.utc).isoformat()
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
