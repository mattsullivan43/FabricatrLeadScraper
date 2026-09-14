#!/usr/bin/env python3
"""Fabricatr forum lead monitor.

Polls RSS/Atom feeds listed in config.yaml, keyword-filters new entries, runs a
cheap OpenAI classifier on the hits, and posts confirmed leads to a Slack webhook.

Flags:
  --dry-run        print Slack messages to stdout instead of posting; never writes state
  --no-classify    skip the OpenAI classifier; every keyword hit is treated as a lead
  --probe          fetch every enabled feed, report alive/dead + item counts, exit
  --ignore-seen    treat every entry as new (testing only, pairs well with --dry-run)
  --reset-baseline forget all seen IDs and re-run the baseline (marks everything seen, sends nothing)
  --slack-test     post one fixed test message to the Slack webhook and exit

Env: SLACK_WEBHOOK_URL (required unless --dry-run), OPENAI_API_KEY (required unless --no-classify).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "seen.json"

CLASSIFIER_SYSTEM = (
    "You screen forum and Reddit posts for a company that sells shop-management software "
    "to metal fabrication, welding, steel, machine, and job shops. You answer with strict JSON only."
)

CLASSIFIER_QUESTION = (
    "Is this a metal fabrication, welding, steel, machine, or job shop owner/manager describing a "
    "pain point or asking for recommendations about how they quote, track jobs, schedule, or run "
    "the business side of the shop? Answer NO for hobbyists, for people asking technical "
    "welding/machining questions, for people selling something, for software developers or "
    "consultants building or implementing ERP/business systems, for employees asking about their own "
    "job or career, and for posts where the keyword is incidental. The person must plausibly own or "
    "run a shop.\n\n"
    'Respond with only this JSON, no prose, no code fences: {"lead": true or false, "reason": "<one sentence>"}'
)


# ----------------------------------------------------------------------------- logging

def log(msg: str) -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- config / state

def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        cfg = yaml.safe_load(f)
    for key in ("settings", "sources", "keywords"):
        if key not in cfg:
            raise SystemExit(f"CONFIG ERROR: missing top-level '{key}' in {CONFIG_PATH}")
    return cfg


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "baseline_done": False, "seen": {}}
    with STATE_PATH.open() as f:
        state = json.load(f)
    state.setdefault("version", 1)
    state.setdefault("baseline_done", False)
    state.setdefault("seen", {})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = dict(sorted(state["seen"].items()))
    state["updated_at"] = iso(now_utc())
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")
    tmp.replace(STATE_PATH)


def prune_state(state: dict, retention_days: int) -> int:
    cutoff = iso(now_utc() - timedelta(days=retention_days))
    before = len(state["seen"])
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    return before - len(state["seen"])


# ----------------------------------------------------------------------------- text helpers

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    s = _TAG_RE.sub(" ", s or "")
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def excerpt(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "..."


def unwrap_google_link(link: str) -> str:
    """Google Alerts wraps targets as https://www.google.com/url?...&url=<real>."""
    try:
        p = urlparse(link)
        if p.netloc.endswith("google.com") and p.path == "/url":
            real = parse_qs(p.query).get("url")
            if real:
                return real[0]
    except Exception:
        pass
    return link


# ----------------------------------------------------------------------------- keywords

def compile_keywords(kw: dict) -> dict[str, list[tuple[str, re.Pattern]]]:
    def compile_list(words):
        out = []
        for w in words or []:
            parts = [re.escape(p) for p in w.split()]
            pat = r"(?<![\w-])" + r"\s+".join(parts) + r"(?![\w-])"
            out.append((w, re.compile(pat, re.IGNORECASE)))
        return out

    return {
        "software": compile_list(kw.get("software")),
        "pain": compile_list(kw.get("pain")),
        "paired_only": compile_list(kw.get("paired_only")),
    }


def keyword_hits(text: str, compiled: dict) -> list[str]:
    hits = [w for w, p in compiled["software"] if p.search(text)]
    pain = [w for w, p in compiled["pain"] if p.search(text)]
    hits += pain
    if pain:
        hits += [w for w, p in compiled["paired_only"] if p.search(text)]
    return hits


# ----------------------------------------------------------------------------- feeds

class FeedError(Exception):
    pass


def fetch_feed(url: str, settings: dict) -> feedparser.FeedParserDict:
    headers = {"User-Agent": settings["user_agent"], "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
    timeout = settings.get("request_timeout_s", 20)
    for attempt in (1, 2):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            raise FeedError(f"request failed: {e.__class__.__name__}: {e}") from e
        if resp.status_code == 429 and attempt == 1:
            wait = min(int(resp.headers.get("Retry-After", "10") or 10), 30)
            log(f"  429 from {urlparse(url).netloc}, waiting {wait}s and retrying once")
            time.sleep(wait)
            continue
        break
    if resp.status_code != 200:
        snippet = strip_html(resp.text[:400])[:120]
        raise FeedError(f"HTTP {resp.status_code} ({snippet or resp.reason})")
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        ctype = resp.headers.get("Content-Type", "")
        title = strip_html(str(parsed.feed.get("title", "")))[:80]
        reason = f"0 entries; content-type={ctype!r}"
        if parsed.bozo and getattr(parsed, "bozo_exception", None):
            reason += f"; parse error: {parsed.bozo_exception}"
        if title:
            reason += f"; page title={title!r}"
        raise FeedError(reason)
    return parsed


_CATEGORY_ID_RE = re.compile(r"\.(\d+)/?$")


def entry_category(entry, category_filter: dict | None) -> tuple[bool, str | None]:
    """Return (keep, label). Without a filter everything is kept."""
    if not category_filter:
        return True, None
    for tag in entry.get("tags", []) or []:
        scheme = tag.get("scheme") or ""
        m = _CATEGORY_ID_RE.search(scheme)
        if m and m.group(1) in category_filter:
            return True, category_filter[m.group(1)]
        term = (tag.get("term") or "").strip()
        for cid, label in category_filter.items():
            if term and term.lower() == str(label).lower():
                return True, label
    return False, None


def entry_body(entry) -> str:
    if entry.get("content"):
        parts = [c.get("value", "") for c in entry.content]
        raw = " ".join(parts)
    else:
        raw = entry.get("summary", "") or entry.get("description", "")
    return strip_html(raw)


def entry_published(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def display_source(source: dict, entry, category_label: str | None) -> str:
    name = source["name"]
    if category_label:
        return f"{name} / {category_label}"
    if source["id"].startswith("reddit"):
        for tag in entry.get("tags", []) or []:
            label = tag.get("label") or ""
            if label.startswith("r/"):
                return f"{name} / {label}"
            term = tag.get("term") or ""
            if term and term != "multi":
                return f"{name} / r/{term}"
    if source.get("query"):
        return f"{name} / {source['query']}"
    return name


# ----------------------------------------------------------------------------- classifier

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def make_client():
    """Return a requests.Session with the OpenAI auth header (plain HTTP, no SDK)."""
    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}",
        "Content-Type": "application/json",
    })
    return sess


def classify(client, model: str, item: dict, body_chars: int) -> tuple[bool, str]:
    user = (
        f"Source: {item['source_display']}\n"
        f"Title: {item['title']}\n"
        f"Post:\n{item['body'][:body_chars] or '(no body text in feed)'}\n\n"
        f"{CLASSIFIER_QUESTION}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    for attempt in (1, 2):
        try:
            resp = client.post(OPENAI_URL, json=payload, timeout=60)
        except requests.RequestException as e:
            return False, f"classifier connection error: {e.__class__.__name__}"
        if resp.status_code == 429 and attempt == 1:
            time.sleep(min(int(resp.headers.get("retry-after", "5") or 5), 30))
            continue
        break
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except ValueError:
            msg = resp.text[:200]
        return False, f"classifier API error {resp.status_code}: {msg[:160]}"

    try:
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return False, f"unexpected classifier response shape: {e.__class__.__name__}"
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return False, f"unparseable classifier output: {text[:120]!r}"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, f"invalid JSON from classifier: {text[:120]!r}"
    lead = data.get("lead")
    if isinstance(lead, str):
        lead = lead.strip().lower() in ("true", "yes")
    reason = str(data.get("reason", "")).strip() or "(no reason given)"
    return bool(lead), reason


# ----------------------------------------------------------------------------- slack

def format_slack(item: dict, reason: str, excerpt_chars: int) -> str:
    quote = excerpt(item["body"], excerpt_chars) if item["body"] else "(no body text in feed)"
    return (
        f"🔩 Fab lead — {item['source_display']}\n"
        f"*{item['title']}*\n"
        f'"{quote}"\n'
        f"Why: {reason}\n"
        f"→ {item['link']}"
    )


def post_slack(webhook: str, text: str, timeout: int = 15) -> None:
    resp = requests.post(webhook, json={"text": text, "unfurl_links": False}, timeout=timeout)
    if resp.status_code != 200 or resp.text.strip() != "ok":
        raise RuntimeError(f"Slack webhook HTTP {resp.status_code}: {resp.text[:200]}")


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-classify", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--ignore-seen", action="store_true")
    ap.add_argument("--reset-baseline", action="store_true")
    ap.add_argument("--slack-test", action="store_true")
    args = ap.parse_args()

    if args.slack_test:
        webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if not webhook:
            log("CONFIG ERROR: SLACK_WEBHOOK_URL is not set")
            return 2
        post_slack(webhook, f"🔩 Fab lead monitor connected — test message from GitHub Actions at {iso(now_utc())}. "
                            "Real alerts look like: bold title, quoted excerpt, Why line, link.")
        log("SLACK TEST: message posted OK")
        return 0

    cfg = load_config()
    settings = cfg["settings"]
    sources = [s for s in cfg["sources"] if s.get("enabled")]
    compiled = compile_keywords(cfg["keywords"])
    delay = float(settings.get("delay_between_requests_s", 2))

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not args.dry_run and not args.probe and not webhook:
        log("CONFIG ERROR: SLACK_WEBHOOK_URL is not set (use --dry-run to test without it)")
        return 2
    if not args.no_classify and not args.probe and not os.environ.get("OPENAI_API_KEY", "").strip():
        log("CONFIG ERROR: OPENAI_API_KEY is not set (use --no-classify to test without it)")
        return 2

    log(f"=== fabricatr lead monitor — {iso(now_utc())} — {len(sources)} enabled source(s)"
        + (" [DRY RUN]" if args.dry_run else "") + (" [NO CLASSIFY]" if args.no_classify else "")
        + (" [IGNORE SEEN]" if args.ignore_seen else ""))

    state = load_state()
    if args.reset_baseline:
        log("RESET: forgetting all seen IDs; this run re-baselines")
        state = {"version": 1, "baseline_done": False, "seen": {}}

    # ---- fetch
    fetched: list[dict] = []
    feed_errors = 0
    probe_rows = []
    for i, src in enumerate(sources):
        if i:
            pause = float(src.get("delay_before_s", delay))
            if pause > delay:
                log(f"  pausing {pause:.0f}s before {src['id']} (per-source delay_before_s)")
            time.sleep(pause)
        url = src["url"]
        if not url or url.startswith("PASTE_"):
            log(f"FEED SKIP  {src['id']}: url is a placeholder — fill it in config.yaml")
            probe_rows.append((src["id"], "placeholder", 0))
            continue
        try:
            parsed = fetch_feed(url, settings)
        except FeedError as e:
            feed_errors += 1
            log(f"FEED ERROR {src['id']}: {e}")
            probe_rows.append((src["id"], f"DEAD — {e}", 0))
            continue
        kept = 0
        for entry in parsed.entries:
            keep, label = entry_category(entry, src.get("category_filter"))
            if not keep:
                continue
            eid = (entry.get("id") or entry.get("link") or "").strip()
            link = unwrap_google_link((entry.get("link") or "").strip())
            if not eid:
                eid = link
            if not eid:
                continue
            kept += 1
            fetched.append({
                "id": eid,
                "link": link,
                "title": strip_html(entry.get("title", "")) or "(untitled)",
                "body": entry_body(entry),
                "published": entry_published(entry),
                "source": src,
                "source_display": display_source(src, entry, label),
            })
        log(f"FEED OK    {src['id']}: {len(parsed.entries)} entries, {kept} kept")
        probe_rows.append((src["id"], "alive", kept))

    if args.probe:
        log("\n=== PROBE RESULT")
        for sid, status, n in probe_rows:
            log(f"  {sid:32s} {status}" + (f" ({n} kept)" if status == "alive" else ""))
        return 0

    # ---- baseline
    if not state["baseline_done"] and not args.ignore_seen:
        ts = iso(now_utc())
        for it in fetched:
            state["seen"][it["id"]] = ts
        state["baseline_done"] = True
        log(f"BASELINE: marked {len(fetched)} entries as seen, sent nothing. Alerts start next run.")
        if args.dry_run:
            log("DRY RUN: state not written")
        else:
            save_state(state)
        return 0

    # ---- new entries
    ts = iso(now_utc())
    max_age = settings.get("max_entry_age_hours")
    age_cutoff = now_utc() - timedelta(hours=float(max_age)) if max_age else None
    new_items: list[dict] = []
    seen_now = 0
    too_old = 0
    for it in fetched:
        if it["id"] in state["seen"] and not args.ignore_seen:
            state["seen"][it["id"]] = ts  # still in a feed: keep it from being pruned
            seen_now += 1
            continue
        if age_cutoff and it["published"] and it["published"] < age_cutoff and not args.ignore_seen:
            too_old += 1
            log(f"OLD        {it['source_display']} \"{it['title']}\" (published {iso(it['published'])}) — marked seen, not alerted")
            state["seen"][it["id"]] = ts
            continue
        new_items.append(it)
    log(f"NEW: {len(new_items)} new entries ({seen_now} already seen, {too_old} too old)")

    # ---- layer 1: keywords
    hits: list[dict] = []
    for it in new_items:
        words = keyword_hits(f"{it['title']}\n{it['body']}", compiled)
        if words:
            it["keywords"] = words
            hits.append(it)
            log(f"KW HIT     {it['source_display']} \"{it['title']}\" -> {words}")
        else:
            state["seen"][it["id"]] = ts
    log(f"KEYWORDS: {len(hits)} hit(s) out of {len(new_items)} new")

    # ---- layer 2: classifier
    cap = int(settings.get("max_classifier_calls_per_run", 40))
    body_chars = int(settings.get("body_chars_for_classifier", 3000))
    excerpt_chars = int(settings.get("excerpt_chars", 250))
    client = None if (args.no_classify or not hits) else make_client()
    yes = no = skipped = classified = sent = slack_errors = 0
    for idx, it in enumerate(hits):
        if args.no_classify:
            lead, reason = True, "keyword match: " + ", ".join(it["keywords"]) + " (classifier skipped)"
        elif classified >= cap:
            skipped += 1
            log(f"SKIP       classifier cap {cap} reached — \"{it['title']}\" left unseen for next run")
            continue
        else:
            lead, reason = classify(client, settings["model"], it, body_chars)
            classified += 1
        if lead:
            yes += 1
            log(f"YES        {it['source_display']} \"{it['title']}\" — {reason}")
            msg = format_slack(it, reason, excerpt_chars)
            if args.dry_run:
                log("---------- SLACK (dry run) ----------\n" + msg + "\n-------------------------------------")
            else:
                try:
                    post_slack(webhook, msg)
                    sent += 1
                except Exception as e:
                    slack_errors += 1
                    log(f"SLACK ERROR for \"{it['title']}\": {e}")
        else:
            no += 1
            log(f"NO         {it['source_display']} \"{it['title']}\" — {reason}")
        state["seen"][it["id"]] = ts

    pruned = prune_state(state, int(settings.get("seen_retention_days", 30)))
    log(f"SUMMARY fetched={len(fetched)} new={len(new_items)} kw_hits={len(hits)} classified={classified} "
        f"yes={yes} no={no} skipped={skipped} sent={sent} slack_errors={slack_errors} "
        f"feed_errors={feed_errors} pruned={pruned} seen_total={len(state['seen'])}")

    if args.dry_run:
        log("DRY RUN: state not written")
    else:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
