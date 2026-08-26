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


def fetch_with_retry(query, max_retries=2, backoff_sec=40):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fetch_gdelt_timeline(query)
        except Exception as e:
            last_err = e
            is_429 = "429" in str(e)
            if attempt < max_retries and is_429:
                log(f"  429 received, backing off {backoff_sec}s before retry {attempt+1}/{max_retries}...")
                time.sleep(backoff_sec)
            elif not is_429:
                break  # don't retry non-rate-limit errors (timeouts, bad response shape, etc.)
    raise last_err


def run_gdelt_pass(data):
    outbreaks = data.get("outbreaks", [])
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
        }
        items.append(item)

    data["signals"] = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "checkedBy": "Automated — GitHub Actions + live GDELT z-score (mode=timelinevol)",
        "items": items,
    }
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
