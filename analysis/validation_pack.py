"""
Validation pack generator for RWA opportunity briefs.

Reads data/opportunity_briefs.json, groups convergent themes, and produces
customer-discovery materials: problem framings, interview guides (Mom Test style),
contact shortlists, and outreach templates.

Output: data/validation_pack.json + data/validation_pack.md

Usage:
    python -m analysis.validation_pack
    python -m analysis.validation_pack --briefs path/to/other_briefs.json
"""

import argparse
import json
import logging
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BRIEFS_PATH       = ROOT / "data" / "opportunity_briefs.json"
PACK_JSON_PATH    = ROOT / "data" / "validation_pack.json"
PACK_MD_PATH      = ROOT / "data" / "validation_pack.md"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("validation_pack")


# ── Theme grouping / confidence config ────────────────────────────────────────

# Theme IDs that converge into the single primary opportunity
PRIMARY_THEME_IDS = {
    "loan-defaults-recovery",
    "redemption-issuer-unresponsive",
    "token-migration-deadline-missed",
}

# Confidence overrides for secondary themes.
# "low" → no outreach template generated; flagged in output.
# Rationale is included for transparency.
CONFIDENCE_OVERRIDES = {
    "physical-asset-verification-gap": {
        "confidence": "low",
        "rationale": (
            "Only two supporting posts, both using near-identical 'trillion-dollar market' "
            "framing and presenting as expert commentary rather than lived experience. "
            "Possible coordinated content marketing; insufficient authentic signal for "
            "customer discovery."
        ),
    },
    "local-dev-setup-failures": {
        "confidence": "low",
        "rationale": (
            "Goldfinch's mono repo appears to be in wind-down. Issues are stale and the "
            "protocol team is no longer actively developing. Developer friction in an "
            "abandoned codebase is noise, not a product opportunity."
        ),
    },
    "testnet-deployment-broken": {
        "confidence": "low",
        "rationale": (
            "Same codebase as local-dev-setup-failures; Goldfinch mono repo is likely "
            "in wind-down. These issues are pre-2023 with no recent activity."
        ),
    },
}


# ── Claude helpers ─────────────────────────────────────────────────────────────

def _call_claude(prompt: str, system: str, max_tokens: int = 3000) -> str:
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
    m = re.search(r"(\{[\s\S]+\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    log.error("Could not parse JSON from response:\n%s", text[:400])
    return {}


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM = """\
You are a senior product researcher with a background in institutional credit and \
financial infrastructure. You write with precision and restraint — no hype, no \
startup-pitch register, no crypto-native jargon unless directly quoting a source. \
Your audience is a practitioner who has seen real money lost and is trying to understand \
whether a structural gap is actually worth solving.

When writing about people who have experienced financial loss, your tone is respectful \
and matter-of-fact. You do not exploit their situation for persuasive effect. \
You do not make promises or imply that any tool or service exists or will exist."""


# ── Generation functions ───────────────────────────────────────────────────────

def generate_framing(opp: dict) -> str:
    """
    Generate a one-paragraph institutional-voice problem framing.
    Returns plain text.
    """
    evidence_block = "\n".join(
        f"- {e['summary']}" for e in opp["combined_evidence"]
    )
    prompt = f"""\
Here is a synthesized opportunity derived from three converging pain-point themes \
in private-credit DeFi protocols (Maple, Centrifuge, Goldfinch):

THEMES GROUPED:
{', '.join(opp['source_theme_ids'])}

COMBINED PAIN POINT:
{opp['combined_pain_point']}

EVIDENCE SUMMARIES:
{evidence_block}

Write a single paragraph (5-8 sentences) that frames the structural insight — \
not the symptoms. Write in a calm, methodology-first, institutional voice. \
Identify the category of problem (e.g. "post-origination servicing gap"), \
name why existing protocol infrastructure does not address it, and note what \
classes of participant are exposed. Do not hype the market size. \
Do not propose a solution. End with a sentence about why this is worth investigating now.

Return only the paragraph, no headers, no JSON."""

    return _call_claude(prompt, SYSTEM, max_tokens=600)


def generate_interview_guide(opp: dict) -> list[dict]:
    """
    Generate 8-12 Mom Test interview questions.
    Returns list of {question, probe_for, notes} dicts.
    """
    pain = opp["combined_pain_point"]
    who  = ", ".join(opp["who_is_affected"])

    prompt = f"""\
I'm designing a customer-discovery interview for the following opportunity:

STRUCTURAL PROBLEM:
{pain}

TARGET INTERVIEWEES:
{who}

Write 8-12 interview questions in the style of Rob Fitzpatrick's "The Mom Test":
- Ask about past behaviour and specific past incidents only
- Never ask "would you use a tool that..." or hypothetical willingness questions
- Probe for: what they actually did when the problem hit, what it cost in time or money,
  what workaround they used, who else was involved, what they tried and abandoned
- Order from rapport-building → specifics → signals of severity

For each question, include:
- "question": the question as you'd actually say it
- "probe_for": what information or signal this is designed to surface
- "notes": any interviewer guidance (e.g. "follow up if they mention X")

Return a JSON array of objects with keys: question, probe_for, notes.
No prose outside the JSON."""

    raw  = _call_claude(prompt, SYSTEM, max_tokens=2500)
    qs   = _extract_json(raw)
    if not isinstance(qs, list):
        log.warning("Interview guide parse issue — got %s", type(qs))
        return []
    return qs


def generate_contact_shortlist(opp: dict, all_briefs: list[dict]) -> list[dict]:
    """
    Build a contact shortlist from handles already present in brief evidence.
    Returns list of {handle, source_url, source_theme, why_relevant, caution} dicts.

    Does NOT invent handles, emails, or any details not in the source data.
    Applies a caution flag for participants who appear to have experienced direct loss.
    """
    # Collect handle → evidence mapping from the contributing briefs
    handle_evidence = {}  # handle -> [{url, summary, theme_id}]

    contributing_theme_ids = set(opp["source_theme_ids"])
    for brief in all_briefs:
        if brief["theme_id"] not in contributing_theme_ids:
            continue
        theme_id = brief["theme_id"]
        targets  = brief.get("candidate_interview_targets", [])
        evidence = brief.get("evidence", [])

        for handle in targets:
            if handle not in handle_evidence:
                handle_evidence[handle] = []
            # Associate evidence from this theme
            for ev in evidence[:2]:  # cap to first 2 per theme
                handle_evidence[handle].append({
                    "url":     ev["url"],
                    "summary": ev["summary"],
                    "theme":   theme_id,
                })

    # Heuristic caution flags: handles active on loss/stuck-funds threads
    LOSS_THEMES = {"redemption-issuer-unresponsive", "token-migration-deadline-missed"}
    # Protocol-side handles (team/foundation members): they're practitioners, not victims
    PRACTITIONER_SIGNALS = {"blakewest", "mustermeiszer", "mikesall"}

    shortlist = []
    for handle, evs in handle_evidence.items():
        if not evs:
            continue

        themes_involved = {e["theme"] for e in evs}
        is_loss_affected = bool(themes_involved & LOSS_THEMES)
        is_practitioner  = handle.lower() in {h.lower() for h in PRACTITIONER_SIGNALS}

        caution = None
        if is_loss_affected and not is_practitioner:
            caution = (
                "This person may have directly experienced financial loss or locked funds. "
                "Approach with particular care; make no implications about recovery or resolution."
            )

        # Best source URL: prefer the most specific evidence entry
        best_ev = evs[0]

        shortlist.append({
            "handle":       handle,
            "source_url":   best_ev["url"],
            "source_theme": best_ev["theme"],
            "why_relevant": best_ev["summary"],
            "is_practitioner": is_practitioner,
            "caution":      caution,
        })

    return shortlist


def generate_outreach_template(opp: dict) -> dict:
    """
    Generate a forum-DM-length outreach template with [bracketed blanks].
    Returns {template_text, usage_notes}.
    """
    pain_short = opp["combined_pain_point"][:300]

    prompt = f"""\
Write a short outreach message (forum DM or reply length — roughly 80-120 words) \
for reaching out to participants who experienced the following situation:

{pain_short}

Requirements:
1. Transparent: state clearly this is research, not a service or a fix
2. No promises: do NOT imply you can help with recovery, funds, or legal matters
3. Respectful: acknowledge their situation without being presumptuous or transactional
4. Ask only for a short conversation about their experience
5. Leave [bracketed blanks] for: [THEIR_HANDLE], [SPECIFIC_POST_REFERENCE], \
[YOUR_NAME], [OPTIONAL_CREDENTIAL_LINE]
6. Tone: peer-to-peer, not pitch, not corporate, not crypto-twitter

Return a JSON object with two keys:
- "template_text": the message with [bracketed blanks]
- "usage_notes": 2-3 bullet points of guidance for personalising the blanks

No prose outside the JSON."""

    raw    = _call_claude(prompt, SYSTEM, max_tokens=800)
    result = _extract_json(raw)
    if not result:
        return {"template_text": "", "usage_notes": []}
    return result


def generate_secondary_framing(brief: dict) -> str:
    """Brief one-paragraph framing for a secondary opportunity."""
    pain    = brief.get("pain_point", "")
    who     = ", ".join(brief.get("who_is_affected", []))
    sig     = brief.get("signal", {})
    ev_text = " ".join(e["summary"] for e in brief.get("evidence", [])[:2])

    prompt = f"""\
Write a concise two-sentence framing of this secondary opportunity for a product \
research backlog. Institutional voice; state the structural gap, not the symptom. \
Do not hype. Do not propose a solution.

Pain point: {pain}
Affected: {who}
Signal: {sig.get('frequency','')}, {sig.get('recency','')}
Evidence context: {ev_text}

Return only the two sentences, no headers."""

    return _call_claude(prompt, SYSTEM, max_tokens=200)


# ── Grouping logic ────────────────────────────────────────────────────────────

def build_primary_opportunity(briefs: list[dict]) -> dict:
    """
    Merge the three convergent themes into a single primary opportunity object.
    """
    contributing = [b for b in briefs if b["theme_id"] in PRIMARY_THEME_IDS]

    # Merge who_is_affected (deduplicated)
    all_affected = []
    seen = set()
    for b in contributing:
        for w in (b.get("who_is_affected") or []):
            if w not in seen:
                all_affected.append(w)
                seen.add(w)

    # Merge evidence
    all_evidence = []
    for b in contributing:
        for e in b.get("evidence", []):
            all_evidence.append({**e, "source_theme": b["theme_id"]})

    # Merge signal summary
    sources = set()
    for b in contributing:
        for s in (b.get("signal", {}).get("source_mix") or []):
            sources.add(s)

    return {
        "opportunity_id":      "post-origination-recovery-gap",
        "opportunity_name":    "Post-origination servicing and recovery gap in private-credit DeFi",
        "source_theme_ids":    [b["theme_id"] for b in contributing],
        "combined_pain_point": (
            "Private-credit DeFi protocols lack any structured workflow for events that "
            "occur after a loan or pool is originated: borrower defaults, silent redemption "
            "queues, and forced protocol migrations all expose participants to permanent loss "
            "with no escalation path, no status transparency, and no defined recovery process. "
            "Each protocol has handled these events reactively, through ad-hoc governance "
            "proposals, retained external advisors, and informal forum updates — at significant "
            "cost and with inconsistent outcomes for affected participants."
        ),
        "who_is_affected":     all_affected,
        "combined_evidence":   all_evidence,
        "signal": {
            "frequency": "persistent",
            "recency":   "recent (2024+)",
            "source_mix": sorted(sources),
        },
    }


# ── Markdown rendering ─────────────────────────────────────────────────────────

def _wrap(text: str, width: int = 90, indent: str = "") -> str:
    return textwrap.fill(text, width=width, initial_indent=indent,
                         subsequent_indent=indent)


def render_markdown(pack: dict) -> str:
    lines = []
    ts = pack.get("generated_at", "")[:16].replace("T", " ")

    lines += [
        "# RWA Opportunity Validation Pack",
        f"*Generated {ts} UTC*",
        "",
        "---",
        "",
        "## How to use this pack",
        "",
        "This pack contains customer-discovery materials for prioritised pain-point opportunities",
        "identified from Centrifuge, Goldfinch, and Maple forums and GitHub issues.",
        "All outreach is manual — send each message yourself, personalise the [bracketed blanks],",
        "and respect the caution notes on contacts who experienced direct financial loss.",
        "",
        "---",
        "",
    ]

    # ── Primary opportunity ────────────────────────────────────────────────────
    prim = pack["primary_opportunity"]
    lines += [
        "## PRIMARY OPPORTUNITY",
        "",
        f"### {prim['opportunity_name']}",
        "",
        f"*Converges themes:* {', '.join(prim['source_theme_ids'])}",
        "",
        "#### Problem framing",
        "",
        _wrap(prim.get("framing", "")),
        "",
        "#### Who is affected",
        "",
    ]
    for w in prim["who_is_affected"]:
        lines.append(f"- {w}")
    lines.append("")

    # Interview guide
    lines += ["#### Interview guide", ""]
    qs = prim.get("interview_guide", [])
    for i, q in enumerate(qs, 1):
        lines += [
            f"**Q{i}.** {q.get('question','')}",
            f"*Probe for:* {q.get('probe_for','')}",
        ]
        notes = q.get("notes", "")
        if notes:
            lines.append(f"*Notes:* {notes}")
        lines.append("")

    # Contact shortlist
    lines += ["#### Contact shortlist", ""]
    lines += [
        "> Handles are drawn only from public forum posts. No contact details have been ",
        "> looked up or inferred. ⚠ = person may have experienced direct financial loss.",
        "",
    ]
    for c in prim.get("contact_shortlist", []):
        flag     = "⚠ " if c.get("caution") else ""
        tag      = " *(practitioner)*" if c.get("is_practitioner") else ""
        lines   += [
            f"**{flag}{c['handle']}**{tag}",
            f"- Source: [{c['source_url']}]({c['source_url']})",
            f"- Relevance: {c['why_relevant']}",
        ]
        if c.get("caution"):
            lines.append(f"- ⚠ Caution: {c['caution']}")
        lines.append("")

    # Outreach template
    tmpl = prim.get("outreach_template", {})
    lines += ["#### Outreach template", ""]
    lines += [
        "```",
        tmpl.get("template_text", ""),
        "```",
        "",
        "**Usage notes:**",
        "",
    ]
    for note in (tmpl.get("usage_notes") or []):
        lines.append(f"- {note}")
    lines += ["", "---", ""]

    # ── Secondary opportunities ────────────────────────────────────────────────
    lines += ["## SECONDARY OPPORTUNITIES", ""]

    for sec in pack.get("secondary_opportunities", []):
        conf    = sec.get("confidence", "medium")
        conf_label = {"high": "🟢 HIGH", "medium": "🟡 MEDIUM", "low": "🔴 LOW CONFIDENCE"}.get(conf, conf)
        lines  += [
            f"### {sec['theme_id']} — {conf_label}",
            "",
        ]

        if conf == "low":
            rationale = sec.get("confidence_rationale", "")
            lines += [
                f"> **Flagged low-confidence.** {rationale}",
                "",
                "*No outreach template generated for this theme.*",
                "",
            ]
            continue

        framing = sec.get("framing", "")
        if framing:
            lines += [_wrap(framing), ""]

        # Brief contact list (no outreach template for secondaries)
        targets = sec.get("candidate_interview_targets", [])
        if targets:
            lines.append(f"*Candidate contacts (from brief):* {', '.join(targets)}")
            lines.append("")

        ev = sec.get("evidence", [])
        if ev:
            lines.append("*Evidence:*")
            lines.append("")
            for e in ev[:2]:
                lines.append(f"- [{e['url']}]({e['url']}): {e['summary']}")
            lines.append("")

    return "\n".join(lines)


# ── Console print (primary only) ──────────────────────────────────────────────

def print_primary(prim: dict):
    SEP  = "─" * 72
    SEP2 = "═" * 72

    print(f"\n{SEP2}")
    print("  VALIDATION PACK — PRIMARY OPPORTUNITY")
    print(SEP2)
    print(f"\n{prim['opportunity_name'].upper()}\n")

    framing = prim.get("framing", "")
    if framing:
        print("PROBLEM FRAMING\n")
        for line in textwrap.wrap(framing, width=70):
            print(f"  {line}")
        print()

    qs = prim.get("interview_guide", [])
    if qs:
        print(f"{SEP}")
        print("INTERVIEW GUIDE\n")
        for i, q in enumerate(qs, 1):
            print(f"  Q{i}. {q.get('question','')}")
            print(f"       ↳ Probe for: {q.get('probe_for','')}")
            notes = q.get("notes","")
            if notes:
                print(f"       ↳ Notes: {notes}")
            print()

    tmpl = prim.get("outreach_template", {})
    if tmpl.get("template_text"):
        print(f"{SEP}")
        print("OUTREACH TEMPLATE\n")
        for line in tmpl["template_text"].split("\n"):
            print(f"  {line}")
        print()
        usage = tmpl.get("usage_notes") or []
        if usage:
            print("  Usage notes:")
            for n in usage:
                print(f"    • {n}")
        print()

    print(SEP2)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validation pack generator")
    parser.add_argument("--briefs", default=str(BRIEFS_PATH),
                        help="Path to opportunity_briefs.json")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — aborting")
        sys.exit(1)

    briefs_path = Path(args.briefs)
    if not briefs_path.exists():
        log.error("Briefs file not found: %s", briefs_path)
        sys.exit(1)

    with open(briefs_path) as f:
        briefs_data = json.load(f)
    briefs = briefs_data["briefs"]
    log.info("Loaded %d briefs from %s", len(briefs), briefs_path)

    # ── Build primary opportunity ──────────────────────────────────────────────
    log.info("Building primary opportunity (grouping %s)", sorted(PRIMARY_THEME_IDS))
    prim_opp = build_primary_opportunity(briefs)

    log.info("Generating problem framing...")
    prim_opp["framing"] = generate_framing(prim_opp)

    log.info("Generating interview guide...")
    prim_opp["interview_guide"] = generate_interview_guide(prim_opp)

    log.info("Building contact shortlist...")
    prim_opp["contact_shortlist"] = generate_contact_shortlist(prim_opp, briefs)

    log.info("Generating outreach template...")
    prim_opp["outreach_template"] = generate_outreach_template(prim_opp)

    # ── Build secondary opportunities ──────────────────────────────────────────
    secondary_ids = [b["theme_id"] for b in briefs if b["theme_id"] not in PRIMARY_THEME_IDS]
    secondary_opps = []

    for brief in briefs:
        if brief["theme_id"] in PRIMARY_THEME_IDS:
            continue

        override   = CONFIDENCE_OVERRIDES.get(brief["theme_id"], {})
        confidence = override.get("confidence", "medium")

        sec = {
            "theme_id":                    brief["theme_id"],
            "confidence":                  confidence,
            "confidence_rationale":        override.get("rationale", ""),
            "pain_point":                  brief.get("pain_point", ""),
            "who_is_affected":             brief.get("who_is_affected", []),
            "evidence":                    brief.get("evidence", []),
            "signal":                      brief.get("signal", {}),
            "candidate_interview_targets": brief.get("candidate_interview_targets", []),
        }

        if confidence != "low":
            log.info("Generating framing for secondary: %s", brief["theme_id"])
            sec["framing"] = generate_secondary_framing(brief)
        else:
            log.info("Skipping framing for low-confidence: %s", brief["theme_id"])
            sec["framing"] = None

        secondary_opps.append(sec)

    # ── Assemble pack ──────────────────────────────────────────────────────────
    pack = {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "source_briefs_path":    str(briefs_path),
        "primary_opportunity":   prim_opp,
        "secondary_opportunities": secondary_opps,
    }

    # ── Write JSON ─────────────────────────────────────────────────────────────
    PACK_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PACK_JSON_PATH, "w") as f:
        json.dump(pack, f, indent=2)
    log.info("Wrote %s", PACK_JSON_PATH)

    # ── Write Markdown ────────────────────────────────────────────────────────
    md = render_markdown(pack)
    with open(PACK_MD_PATH, "w") as f:
        f.write(md)
    log.info("Wrote %s", PACK_MD_PATH)

    # ── Console output ────────────────────────────────────────────────────────
    print_primary(prim_opp)
    print(f"\nFull pack:\n  JSON → {PACK_JSON_PATH}\n  MD   → {PACK_MD_PATH}\n")


if __name__ == "__main__":
    main()
