"""
RWA Opportunity Intelligence — v1 (hypothesis-aware).

Pipeline: pull → Claude clustering → briefs.
Sources defined in analysis/sources.py.

HYPOTHESIS UNDER TEST
A prior run (v0) on Maple/Centrifuge/Goldfinch identified one strong convergent
theme: tokenized private credit has no post-origination workflow for distress —
no servicing/workout/recovery layer when a loan defaults, a redemption goes
unanswered, or an off-chain obligation is missed.

This run adds Sky/MakerDAO — the protocol with the longest public RWA history,
including vault offboardings, New Silver restructuring, and HVB. The question:
does the same gap show up in Maker's RWA threads, and if so, does it extend,
contradict, or add nuance to the existing evidence?

Usage:
    python -m analysis.opportunity_intel           # full run (uses cache if exists)
    python -m analysis.opportunity_intel --refetch # ignore cache, re-pull APIs
    python -m analysis.opportunity_intel --skip-pull  # use cached raw_docs.json
    python -m analysis.opportunity_intel --github-only # forum skip; GitHub only
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from typing import Optional

import requests
from dotenv import load_dotenv
import anthropic

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

import os
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN", "")

RAW_DOCS_PATH  = ROOT / "data" / "raw_docs.json"
BRIEFS_PATH    = ROOT / "data" / "opportunity_briefs.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("opportunity_intel")

UA = "RWA-Pulse-Research/0.1 (academic research; contact: research@rwapulse.io)"


# ── HTML → plaintext ───────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    for ent, rep in [("&nbsp;"," "),("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"')]:
        text = text.replace(ent, rep)
    return re.sub(r"\s+", " ", text).strip()


# ── Discourse pull ─────────────────────────────────────────────────────────────

def _fetch_topic(session: requests.Session, base_url: str, topic_id: int,
                 source_name: str) -> Optional[dict]:
    """Fetch a single Discourse topic. Returns doc dict or None."""
    for attempt in range(2):
        try:
            time.sleep(0.4)
            r = session.get(f"{base_url}/t/{topic_id}.json", timeout=15)
        except requests.RequestException as e:
            log.warning("Discourse/%s: request error topic %d: %s", source_name, topic_id, e)
            return None

        if r.status_code in (404, 403, 410, 301):
            return None
        if r.status_code == 429:
            log.warning("Discourse/%s: 429 on topic %d — backing off 45s", source_name, topic_id)
            time.sleep(45)
            continue
        if not r.ok:
            return None
        break
    else:
        return None

    try:
        t     = r.json()
        title = t.get("title", "")
        slug  = t.get("slug", str(topic_id))
        url   = f"{base_url}/t/{slug}/{topic_id}"
        posts = t.get("post_stream", {}).get("posts", [])
        if not posts:
            return None

        body_parts = []
        for p in posts[:3]:
            txt = _strip_html(p.get("cooked", ""))
            if txt:
                body_parts.append(txt)
        body = " | ".join(body_parts)

        return {
            "source_type": "discourse",
            "source_name": source_name,
            "url":         url,
            "author":      posts[0].get("username", ""),
            "title":       title,
            "body":        body,
            "created_at":  posts[0].get("created_at", t.get("created_at", "")),
        }
    except Exception as e:
        log.warning("Discourse/%s: parse error topic %d: %s", source_name, topic_id, e)
        return None


def _pull_discourse(source: dict) -> list[dict]:
    """
    Pull a Discourse forum. For Sky, also pulls from the legacy RWA category
    and runs targeted searches. Deduplicates by topic ID.
    """
    name     = source["name"]
    base_url = source["base_url"]
    cap      = source["cap"]

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    seen_ids: set[int] = set()
    topic_ids: list[int] = []

    def _add_ids(ids: list[int]):
        for tid in ids:
            if tid not in seen_ids:
                seen_ids.add(tid)
                topic_ids.append(tid)

    # ── /latest.json pages ────────────────────────────────────────────────────
    log.info("Discourse/%s: fetching /latest.json (cap=%d) from %s", name, cap, base_url)
    page = 0
    latest_ids: list[int] = []
    while len(latest_ids) < cap:
        try:
            r = session.get(f"{base_url}/latest.json", params={"page": page}, timeout=15)
        except requests.RequestException as e:
            log.warning("Discourse/%s: /latest.json error page %d: %s", name, page, e)
            break
        if r.status_code == 403:
            log.warning("Discourse/%s: 403 login-walled — skipping", name)
            return []
        if r.status_code == 429:
            time.sleep(30); continue
        if not r.ok:
            break
        try:
            topics = r.json().get("topic_list", {}).get("topics", [])
        except Exception:
            break
        if not topics:
            break
        latest_ids.extend(t["id"] for t in topics)
        page += 1
        time.sleep(0.5)

    _add_ids(latest_ids[:cap])

    # ── Sky-specific: legacy RWA category ─────────────────────────────────────
    rwa_cat = source.get("rwa_category")
    if rwa_cat:
        cat_path = rwa_cat["path"]
        cat_cap  = rwa_cat["cap"]
        log.info("Discourse/%s: fetching RWA category %s (cap=%d)", name, cat_path, cat_cap)
        try:
            r = session.get(f"{base_url}{cat_path}", timeout=15)
            if r.ok:
                cat_topics = r.json().get("topic_list", {}).get("topics", [])
                _add_ids([t["id"] for t in cat_topics[:cat_cap]])
                log.info("Discourse/%s: RWA category yielded %d topics", name, len(cat_topics))
        except Exception as e:
            log.warning("Discourse/%s: RWA category error: %s", name, e)
        time.sleep(0.5)

    # ── Sky-specific: targeted searches ───────────────────────────────────────
    rwa_searches = source.get("rwa_searches", [])
    for search_cfg in rwa_searches:
        query      = search_cfg["query"]
        search_cap = search_cfg["cap"]
        log.info("Discourse/%s: search %r (cap=%d)", name, query, search_cap)
        try:
            r = session.get(
                f"{base_url}/search.json",
                params={"q": query, "page": 1},
                timeout=15,
            )
            if r.ok:
                results = r.json().get("topics", [])
                _add_ids([t["id"] for t in results[:search_cap]])
        except Exception as e:
            log.warning("Discourse/%s: search %r error: %s", name, query, e)
        time.sleep(0.5)

    log.info("Discourse/%s: %d unique topic IDs to fetch", name, len(topic_ids))

    # ── Fetch individual topics ────────────────────────────────────────────────
    docs = []
    for tid in topic_ids:
        doc = _fetch_topic(session, base_url, tid, name)
        if doc:
            docs.append(doc)

    log.info("Discourse/%s: collected %d docs", name, len(docs))
    return docs


# ── GitHub pull ────────────────────────────────────────────────────────────────

def _pull_github(source: dict) -> list[dict]:
    name  = source["name"]
    repo  = source["repo"]
    cap   = source["cap"]
    docs  = []

    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    log.info("GitHub/%s: fetching issues from %s", name, repo)

    page = 1
    while len(docs) < cap:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/issues",
                params={"state": "all", "per_page": 100, "sort": "updated", "page": page},
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as e:
            log.warning("GitHub/%s: request error: %s", name, e); break

        if r.status_code == 404:
            log.warning("GitHub/%s: repo %s not found — skipping", name, repo); return []
        if r.status_code in (403, 429):
            time.sleep(60); continue
        if not r.ok:
            break

        items = r.json()
        if not items:
            break

        for item in items:
            if "pull_request" in item:
                continue
            docs.append({
                "source_type": "github_issue",
                "source_name": name,
                "url":         item.get("html_url", ""),
                "author":      (item.get("user") or {}).get("login", ""),
                "title":       item.get("title", ""),
                "body":        (item.get("body") or "")[:2000],
                "created_at":  item.get("created_at", ""),
                "state":       item.get("state", ""),
                "labels":      [l["name"] for l in item.get("labels", [])],
            })
            if len(docs) >= cap:
                break

        page += 1
        time.sleep(0.3)

    log.info("GitHub/%s: collected %d issues", name, len(docs))
    return docs


def pull_all(sources_cfg: dict, refetch: bool = False,
             skip_github: bool = False) -> list[dict]:
    if not refetch and RAW_DOCS_PATH.exists():
        log.info("Loading cached docs from %s", RAW_DOCS_PATH)
        with open(RAW_DOCS_PATH) as f:
            docs = json.load(f)
        log.info("Cache hit: %d docs", len(docs))
        return docs

    all_docs = []
    for src in sources_cfg.get("discourse", []):
        all_docs.extend(_pull_discourse(src))
    if not skip_github:
        for src in sources_cfg.get("github", []):
            all_docs.extend(_pull_github(src))

    RAW_DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_DOCS_PATH, "w") as f:
        json.dump(all_docs, f, indent=2)
    log.info("Pulled %d total docs → %s", len(all_docs), RAW_DOCS_PATH)
    return all_docs


# ── Claude helpers ─────────────────────────────────────────────────────────────

def _call_claude(prompt: str, system: str, max_tokens: int = 4096) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _extract_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\[[\s\S]+\])", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    log.error("Could not parse JSON from Claude response:\n%s", text[:500])
    return []


# ── Prompts ────────────────────────────────────────────────────────────────────

CLUSTER_SYSTEM = """\
You are a product researcher analysing real user posts from private-credit DeFi
protocols (Maple Finance, Centrifuge, Goldfinch) and from the Sky/MakerDAO RWA
governance forum.

HYPOTHESIS UNDER TEST:
A prior analysis found ONE strong convergent theme across Maple, Centrifuge, and
Goldfinch: these protocols have no structured post-origination workflow for
distress events — no servicing layer, no workout process, no recovery
infrastructure when a loan defaults, a redemption goes unanswered, or an
off-chain obligation is missed. Evidence: Goldfinch Lend East (58% principal
loss), Centrifuge HTC2 (2.5-year silent redemption queue), Centrifuge CFG
migration deadline failures with no escalation path.

Sky/MakerDAO has the longest public RWA history. Does the same gap appear there?
Does it manifest differently at Maker's institutional scale? Does Maker's
governance-heavy approach solve the problem or just defer it?

YOUR JOB:
1. Identify distinct recurring pain-point themes.
2. For each theme, note whether it CONFIRMS the hypothesis (same gap, different
   protocol), EXTENDS it (adds a new dimension or mechanism not seen before),
   CONTRADICTS it (evidence that the gap is addressed), or is INDEPENDENT (real
   pain but not related to post-origination servicing).
3. Flag themes supported primarily by Sky/Maker posts separately — that's the
   new signal in this run.

IGNORE: governance voting procedures, quorum mechanics, routine treasury ops,
"thank you" posts, announcements with no user reaction, delegate compensation
discussions. These are structural noise in a DAO forum.

FOCUS ON: operational friction after a deal is live, workout attempts,
counterparty silence, monitoring gaps, off-chain/on-chain coordination failures,
participants describing losses or blocked capital.
"""

CLUSTER_PROMPT_TEMPLATE = """\
Below are {n} posts from private-credit DeFi forums and GitHub issues.
Sky/Maker posts are tagged [sky]; Centrifuge posts tagged [centrifuge];
Goldfinch posts tagged [goldfinch]; GitHub issues tagged [github/source].

Each entry: [ID] [TAG] TITLE | BODY (truncated to ~500 chars)

{doc_block}

Identify the distinct recurring pain-point themes.
For each theme output a JSON object with:
  - theme_id: short slug
  - theme_name: 4-8 word label
  - pain_point: 1-2 sentence description
  - hypothesis_relation: "confirms" | "extends" | "contradicts" | "independent"
  - hypothesis_note: 1 sentence on how this theme relates to the post-origination
    servicing hypothesis
  - sky_maker_signal: true if Sky/Maker posts are among the primary evidence
  - supporting_doc_ids: list of [ID] numbers
  - frequency: "rare" | "occasional" | "recurring" | "persistent"
  - recency: "old (pre-2023)" | "mixed" | "recent (2024+)"

Return ONLY a JSON array. No prose outside the JSON.
"""

MERGE_PROMPT_TEMPLATE = """\
You have candidate pain-point themes from multiple analysis batches.
Merge into a single deduplicated list of the strongest, most evidence-backed themes.

{chunks_block}

Rules:
- Merge clearly-identical themes; keep the best hypothesis_note from the merging set
- Drop themes with only 1 supporting doc unless the pain is very specific and severe
- Keep at most 10 themes, ranked by: evidence strength first, then recency
- Preserve sky_maker_signal=true where ANY contributing chunk had it true
- Set hypothesis_relation to the most specific value across merged chunks
  (prefer "extends" over "confirms" if one chunk says extends)

Return ONLY a JSON array. Same schema: theme_id, theme_name, pain_point,
hypothesis_relation, hypothesis_note, sky_maker_signal, supporting_doc_ids,
frequency, recency.
"""

BRIEF_PROMPT_TEMPLATE = """\
You are writing opportunity briefs for a product team investigating the post-
origination servicing gap in private-credit DeFi. The prior run found strong
convergent evidence; this run adds Sky/MakerDAO data.

THEMES:
{themes_json}

POSTS (for evidence lookup by [ID]):
{evidence_block}

For each theme, produce a structured brief. Return a JSON array where each object:
  - theme_id
  - pain_point (1-2 sentences, precise and structural — not symptoms)
  - who_is_affected (specific personas)
  - hypothesis_relation: "confirms" | "extends" | "contradicts" | "independent"
  - sky_maker_signal (bool): whether Sky/Maker posts provide primary evidence
  - sky_maker_insight: if sky_maker_signal=true, 1-2 sentences on what the
    Maker/Sky evidence adds or changes vs. the prior Centrifuge/Goldfinch signal.
    If false, set to null.
  - evidence: array of 2-4 objects, each with:
      url, summary (1-sentence paraphrase — no raw quotes)
  - signal:
      frequency, recency, source_mix (list of protocol names)
  - candidate_interview_targets: author usernames most active on this theme

Return ONLY a JSON array.
"""


def _format_doc_block(docs: list[dict], id_offset: int = 0) -> str:
    lines = []
    for i, doc in enumerate(docs):
        did  = i + id_offset
        tag  = f"[{doc['source_type'].replace('github_issue','github')}/{doc['source_name']}]"
        title = doc.get("title", "")[:100]
        body  = (doc.get("body") or "")[:500].replace("\n", " ")
        lines.append(f"[{did}] {tag} {title} | {body}")
    return "\n".join(lines)


# ── Cluster + briefs ───────────────────────────────────────────────────────────

def cluster_docs(docs: list[dict]) -> list[dict]:
    DOCS_PER_CHUNK = 60

    if len(docs) <= DOCS_PER_CHUNK:
        log.info("Single-pass clustering: %d docs", len(docs))
        block  = _format_doc_block(docs)
        prompt = CLUSTER_PROMPT_TEMPLATE.format(n=len(docs), doc_block=block)
        return _extract_json(_call_claude(prompt, CLUSTER_SYSTEM))

    log.info("Map-reduce: %d docs in chunks of %d", len(docs), DOCS_PER_CHUNK)
    chunk_results = []
    for start in range(0, len(docs), DOCS_PER_CHUNK):
        chunk  = docs[start : start + DOCS_PER_CHUNK]
        block  = _format_doc_block(chunk, id_offset=start)
        prompt = CLUSTER_PROMPT_TEMPLATE.format(n=len(chunk), doc_block=block)
        log.info("  Clustering chunk %d–%d", start, start + len(chunk) - 1)
        themes = _extract_json(_call_claude(prompt, CLUSTER_SYSTEM))
        if themes:
            chunk_results.append({"start": start, "themes": themes})
        time.sleep(1)

    log.info("Merge pass: %d chunk results", len(chunk_results))
    chunks_block = "\n\n".join(
        f"CHUNK {i+1} (docs {c['start']}+):\n{json.dumps(c['themes'], indent=2)}"
        for i, c in enumerate(chunk_results)
    )
    themes = _extract_json(_call_claude(
        MERGE_PROMPT_TEMPLATE.format(chunks_block=chunks_block), CLUSTER_SYSTEM
    ))
    log.info("Merge: %d final themes", len(themes))
    return themes


def generate_briefs(themes: list[dict], docs: list[dict]) -> list[dict]:
    if not themes:
        log.warning("No themes to brief"); return []

    cited_ids: set[int] = set()
    for t in themes:
        cited_ids.update(t.get("supporting_doc_ids", []))

    evidence_lines = []
    for did in sorted(cited_ids):
        if isinstance(did, int) and 0 <= did < len(docs):
            d = docs[did]
            body_preview = (d.get("body") or "")[:400].replace("\n", " ")
            evidence_lines.append(
                f"[{did}] url={d['url']} author={d['author']} src={d['source_name']} | {body_preview}"
            )

    prompt = BRIEF_PROMPT_TEMPLATE.format(
        themes_json=json.dumps(themes, indent=2),
        evidence_block="\n".join(evidence_lines),
    )
    briefs = _extract_json(_call_claude(prompt, CLUSTER_SYSTEM, max_tokens=8000))
    log.info("Generated %d briefs", len(briefs))
    return briefs


# ── Console output ─────────────────────────────────────────────────────────────

def print_briefs(briefs: list[dict], doc_count: int, source_breakdown: dict):
    SEP  = "─" * 70
    SEP2 = "═" * 70

    print(f"\n{SEP2}")
    print("  RWA OPPORTUNITY INTELLIGENCE v1 — HYPOTHESIS TEST RUN")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  {doc_count} docs | {source_breakdown}")
    print(f"{SEP2}\n")

    # Separate by hypothesis relation
    confirms   = [b for b in briefs if b.get("hypothesis_relation") == "confirms"]
    extends    = [b for b in briefs if b.get("hypothesis_relation") == "extends"]
    independ   = [b for b in briefs if b.get("hypothesis_relation") == "independent"]
    contradicts = [b for b in briefs if b.get("hypothesis_relation") == "contradicts"]

    sky_themes = [b for b in briefs if b.get("sky_maker_signal")]

    print(f"HYPOTHESIS SCORECARD")
    print(f"  confirms   : {len(confirms)} theme(s)")
    print(f"  extends    : {len(extends)} theme(s)")
    print(f"  independent: {len(independ)} theme(s)")
    print(f"  contradicts: {len(contradicts)} theme(s)")
    print(f"  sky/maker primary evidence in: {len(sky_themes)} theme(s)\n")
    print(SEP)

    for i, b in enumerate(briefs, 1):
        rel   = b.get("hypothesis_relation","").upper()
        sky   = " [SKY/MAKER ✓]" if b.get("sky_maker_signal") else ""
        print(f"\n[{i}/{len(briefs)}] {b.get('theme_id','').upper()}  —  {rel}{sky}")
        print(SEP)
        print(f"PAIN POINT\n  {b.get('pain_point','')}\n")
        print(f"WHO IS AFFECTED\n  {b.get('who_is_affected','')}\n")

        sky_insight = b.get("sky_maker_insight")
        if sky_insight:
            print(f"SKY/MAKER INSIGHT\n  {sky_insight}\n")

        sig = b.get("signal", {})
        print("SIGNAL")
        print(f"  Frequency : {sig.get('frequency','')}")
        print(f"  Recency   : {sig.get('recency','')}")
        print(f"  Sources   : {sig.get('source_mix','')}\n")

        for e in b.get("evidence", []):
            print(f"  • {e.get('url','')}")
            print(f"    {e.get('summary','')}")
        print()

        targets = b.get("candidate_interview_targets", [])
        if targets:
            print(f"INTERVIEW TARGETS\n  {', '.join(str(t) for t in targets)}\n")

    print(SEP2)
    print(f"  {len(briefs)} brief(s) → {BRIEFS_PATH}")
    print(SEP2 + "\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    from analysis.sources import PRIVATE_CREDIT

    parser = argparse.ArgumentParser()
    parser.add_argument("--refetch",     action="store_true")
    parser.add_argument("--skip-pull",   action="store_true")
    parser.add_argument("--github-only", action="store_true")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — aborting"); sys.exit(1)

    # ── Step 1: Pull ──────────────────────────────────────────────────────────
    if args.skip_pull:
        if not RAW_DOCS_PATH.exists():
            log.error("--skip-pull but %s missing", RAW_DOCS_PATH); sys.exit(1)
        with open(RAW_DOCS_PATH) as f:
            docs = json.load(f)
        log.info("Loaded %d docs from cache", len(docs))
    else:
        docs = pull_all(PRIVATE_CREDIT, refetch=args.refetch,
                        skip_github=args.github_only)

    by_source = {}
    for d in docs:
        k = f"{d['source_type']}/{d['source_name']}"
        by_source[k] = by_source.get(k, 0) + 1
    log.info("Doc breakdown: %s", by_source)

    # ── Step 2: Cluster ───────────────────────────────────────────────────────
    themes = cluster_docs(docs)
    if not themes:
        log.error("No themes produced — aborting"); sys.exit(1)

    # ── Step 3: Briefs ────────────────────────────────────────────────────────
    briefs = generate_briefs(themes, docs)

    output = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "hypothesis":       "post-origination-servicing-gap",
        "doc_count":        len(docs),
        "source_breakdown": by_source,
        "themes":           themes,
        "briefs":           briefs,
    }
    BRIEFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BRIEFS_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print_briefs(briefs, len(docs), by_source)


if __name__ == "__main__":
    main()
