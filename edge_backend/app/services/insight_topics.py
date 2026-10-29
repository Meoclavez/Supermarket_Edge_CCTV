"""Insight topics: group the daily findings into recurring issues and analyse each one.

The rule engine (``business_analysis_service``) writes at most one finding per
(zone, category) per store day to ``ai_decision_recommendations``. A problem
that lasts a fortnight therefore shows up as fourteen near-identical cards.
This module groups them into **topics** and gives each topic one unified
analysis over its whole history.

Grouping (deterministic, no model):

1. Every finding gets a **signature**: the issue *family* (which rules mean the
   same problem, e.g. "dwell but no reach", "dead shelf" and "browsed, not
   touched" are all ``low_pickup``), the normalised *subject* (the blueprint
   zone id when the finding names a blueprint zone, else the sorted,
   singularised product words of its name, so "Pet Foods" == "pet food") and
   the *direction* of the finding (low / high / shift). The key is
   ``family|subject|direction``.
2. A finding joins the topic with the same key (or a key merged into it).
3. Otherwise a **fallback similarity merge** compares it with every topic of
   the same family and a compatible direction: subject similarity (token
   Jaccard over the subject's name and the products the zone sells; numbered
   names such as "Aisle 3" / "Aisle 5" never match) and a TF-IDF cosine of the
   finding text with numbers removed. It joins when the subject similarity is
   at least ``SUBJECT_SIM_MIN`` and ``0.6 * subject + 0.4 * text`` is at least
   ``MERGE_SCORE_MIN``. The thresholds are pinned by tests/test_insight_topics.py.

Grouping runs on every analysis run (new rows only) and as a batched,
idempotent backfill at startup; only rows with ``topic_id IS NULL`` are
touched, so a finding the operator split out is never regrouped.

Analysis per topic (deterministic template, optionally narrated by the local
model): first/last seen, how many analysed days it appeared on, trend
(improving / worsening / stable / recurring / new), weekday and time-of-day
pattern, the rule's headline metric across the topic's days plus the zone's
own visits / dwell / pick-up (or the product's shelf reaches) over
``WINDOW_DAYS``, a before/after comparison around the date an action was
marked done, and a short prioritised action plan. The local model (Ollama on
this box) may only rewrite the summary paragraph from those facts; an answer
that cites a number not in the facts is discarded and the template is kept.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import case, delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import AIDecisionRecommendationModel as Rec
from app.models.db_models import AnalysisRunModel, InsightTopicModel as Topic
from app.services.timeutil import local_midnight_utc, local_now, to_local, utcnow

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------- thresholds
SUBJECT_SIM_MIN = 0.5          # subject token Jaccard needed for a fallback merge
MERGE_SCORE_MIN = 0.55         # 0.6 * subject + 0.4 * text cosine
TREND_REL_CHANGE = 0.15        # early vs late third of the headline metric
QUIET_DAYS_IMPROVING = 3       # analysed days without the finding -> improving
FREQ_SHIFT_DAYS = 3            # occurrences in the last 7 vs previous 7 analysed days
BEFORE_AFTER_DAYS = 14
BEFORE_AFTER_MIN_DAYS = 3      # analysed days needed after an action to judge it
WINDOW_DAYS = _env_int("INSIGHT_TOPICS_WINDOW_DAYS", 42)     # zone / product metrics window
TIMELINE_DAYS = 28
BACKFILL_BATCH = _env_int("INSIGHT_TOPICS_BATCH", 200)
MAX_ANALYSE_PER_SYNC = _env_int("INSIGHT_TOPICS_MAX_ANALYSE", 40)
BACKFILL_DELAY_SEC = _env_int("INSIGHT_TOPICS_BACKFILL_DELAY_SEC", 30)
NARRATE_TIMEOUT_SEC = _env_int("INSIGHT_TOPICS_LLM_TIMEOUT_SEC", 60)
SNOOZE_DAYS_DEFAULT = 7

STATUSES = ("open", "acknowledged", "snoozed", "resolved")
TRENDS = ("worsening", "recurring", "new", "stable", "improving")
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
SPLIT_PREFIX = "split:"

TREND_LABELS = {
    "worsening": "Getting worse",
    "improving": "Improving",
    "stable": "Ongoing",
    "recurring": "Keeps coming back",
    "new": "New",
}

# ------------------------------------------------------------ rule families
# rule id -> (family, direction). Families group rules that describe the same
# problem in different words; direction is the sense of the finding.
RULES = {
    "dwell_low_engage": ("low_pickup", "low"),
    "dead_shelf": ("low_pickup", "low"),
    "browse_no_touch": ("low_pickup", "low"),
    "reach_no_sale": ("low_conversion", "low"),
    "shelf_level": ("shelf_position", "low"),
    "low_traffic": ("low_traffic", "low"),
    "dead_space": ("low_traffic", "low"),
    "queue_wait": ("congestion", "high"),
    "peak_congestion": ("congestion", "high"),
    "lost_sales": ("lost_sales", "high"),
    "hotspot_shift": ("hotspot_shift", "shift"),
}
CATEGORY_RULES = {
    "STORE_LAYOUT": "low_traffic",
    "STAFFING": "queue_wait",
    "LOSS_PREVENTION": "lost_sales",
    "HEATMAP_DEAD_SPACE": "dead_space",
    "HEATMAP_HOTSPOT_SHIFT": "hotspot_shift",
    "HEATMAP_CONGESTION": "peak_congestion",
    "HEATMAP_BROWSE_NO_TOUCH": "browse_no_touch",
}
# MERCHANDISING holds four rules: told apart by their cited evidence, or by
# the fixed wording of the finding on rows older than the evidence column.
_MERCH_RULES = (
    ("dwell_low_engage", "engagement_rate_pct", re.compile(r"\bdwell\b.*\breach for product", re.I)),
    ("dead_shelf", "camera_reaches", re.compile(r"^no shopper reached", re.I)),
    ("shelf_level", "eye_level_reaches_per_zone", re.compile(r"reaches per product", re.I)),
    ("reach_no_sale", "units_per_reaching_shopper", re.compile(r"\bbut pos recorded\b", re.I)),
)
_LOW_WORDS = {"below", "fewer", "less", "low", "lower", "only", "cold", "no", "nobody", "drop", "dropped",
              "fell", "down", "under", "lowest", "decline", "declined"}
_HIGH_WORDS = {"above", "more", "high", "higher", "longer", "rose", "up", "exceeds", "over", "busiest",
               "peak", "increase", "increased", "highest"}

TITLES = {
    "low_pickup": "Shoppers look at {s} but rarely pick it up",
    "low_conversion": "{s} is handled but not bought",
    "shelf_position": "{s} get fewer reaches than eye level",
    "low_traffic": "Few shoppers go to {s}",
    "congestion": "Queues and crowding at {s}",
    "lost_sales": "Shoppers handle products but leave without buying",
    "hotspot_shift": "The busiest part of the floor moved to {s}",
}

# Headline metric per rule: (evidence -> value, label, unit, higher_is_bad)
def _ratio(a: str, b: str, scale: float = 100.0):
    def f(ev: dict) -> Optional[float]:
        x, y = ev.get(a), ev.get(b)
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and y:
            return float(x) / float(y) * scale
        return None
    return f


def _first(*fns):
    """The first of several metric readers that finds its numbers."""
    def f(ev: dict) -> Optional[float]:
        for fn in fns:
            v = fn(ev)
            if v is not None:
                return v
        return None
    return f


def _key(k: str, scale: float = 1.0):
    def f(ev: dict) -> Optional[float]:
        v = ev.get(k)
        return float(v) * scale if isinstance(v, (int, float)) else None
    return f


METRICS = {
    "dwell_low_engage": (_key("engagement_rate_pct"), "visits with a product pick-up", "%", False),
    "browse_no_touch": (_key("reaches_per_shopper_minute"), "reaches per shopper-minute", "", False),
    "reach_no_sale": (_key("units_per_reaching_shopper"), "units sold per shopper who reached", "", False),
    "shelf_level": (_ratio("reaches_per_zone", "eye_level_reaches_per_zone"), "of eye-level reaches", "%", False),
    "low_traffic": (_first(_ratio("visits_per_m2", "store_avg_visits_per_m2"), _ratio("visits", "store_avg_visits")),
                    "of the store-average visits", "%", False),
    "dead_space": (_ratio("zone_visitors_per_cell_per_day", "median_zone_visitors_per_cell_per_day"),
                   "of a typical area's traffic", "%", False),
    "queue_wait": (_key("avg_wait_seconds", 1 / 60.0), "minutes average wait", " min", True),
    "peak_congestion": (_key("avg_people_at_peak"), "people standing at the peak hour", "", True),
    "lost_sales": (_key("lost_sales_index_pct"), "of shoppers who handled product but did not buy", "%", True),
}

# Zone metric used when a topic's own findings carry too few numbers.
ZONE_METRIC_FOR_FAMILY = {
    "low_pickup": ("engagement_pct", "visits with a product pick-up", "%", False),
    "low_traffic": ("visits", "visits per day", "", False),
    "congestion": ("avg_dwell_seconds", "seconds average time in the area", " s", True),
    "low_conversion": ("engagement_pct", "visits with a product pick-up", "%", False),
}

# Action playbooks: (id, action, why, expected effect, effort, owner role, check after days)
PLAYBOOKS: dict[str, list[tuple]] = {
    "low_pickup": [
        ("stock_facing", "Check stock, facings and gaps on {s}; fill and face up the shelf.",
         "Empty or untidy facings are the most common reason a looked-at shelf is not touched.",
         "More shoppers pick up a product within a few days.", "low", "Department lead", 3),
        ("price_label", "Check every shelf-edge price label and promotion ticket on {s}.",
         "Missing or confusing prices stop shoppers reaching for a product.",
         "Fewer shoppers walk away after looking.", "low", "Department lead", 3),
        ("planogram", "Move the best sellers of {s} to eye level and review the planogram.",
         "Shoppers reach most at eye level.", "Pick-ups rise over the next fortnight.", "medium", "Merchandiser", 14),
        ("price_review", "Compare the price of {s} with nearby competitors.",
         "If stock, labels and placement are right, price is the likely barrier.",
         "Pick-ups and sales recover after a price or promotion change.", "medium", "Store manager", 28),
    ],
    "low_conversion": [
        ("price_label", "Check the price label, pack condition and use-by dates on {s}.",
         "Handled but not bought usually means price, label or condition put the shopper off.",
         "More of the shoppers who pick it up buy it.", "low", "Department lead", 3),
        ("price_review", "Review the price or run a trial promotion on {s}.",
         "Price resistance is the usual cause once condition and labels are right.",
         "Units sold per shopper rise.", "medium", "Store manager", 14),
    ],
    "shelf_position": [
        ("planogram", "Review which {s} would sell better at eye level and swap them in.",
         "Shoppers reach most at eye level.", "Reaches on the moved lines rise.", "medium", "Merchandiser", 14),
    ],
    "low_traffic": [
        ("signage", "Walk {s}: check sightlines, aisle-end signs and anything blocking the way in.",
         "An area off the main path or hidden behind stock is easily missed.",
         "More shoppers enter within a week.", "low", "Store manager", 7),
        ("destination", "Move a destination or promoted category into {s}, or add an aisle-end display.",
         "A reason to visit pulls the main traffic flow through the area.",
         "Visits rise towards the store average.", "medium", "Merchandiser", 14),
    ],
    "congestion": [
        ("roster", "Roster an extra lane or staff member at {s} before the busy hour.",
         "Arrivals outpace service at the peak.", "Shorter waits and less crowding at the peak.",
         "medium", "Shift supervisor", 7),
        ("call_for_help", "Agree a call-for-help rule (e.g. open a lane at three in the queue) for {s}.",
         "Staff react faster with a clear trigger.", "Queues clear sooner on busy days.", "low", "Front-end supervisor", 7),
    ],
    "lost_sales": [
        ("price_and_queue", "Review pricing on the most-handled lines together with checkout waits.",
         "Shoppers who handle products but do not buy are put off by price or by the queue.",
         "A larger share of shoppers buy.", "medium", "Store manager", 14),
    ],
    "hotspot_shift": [
        ("follow_traffic", "Check what changed near {s} and move staffing and replenishment with the traffic.",
         "A layout, promotion or obstruction change has redirected shoppers.",
         "Staff and stock are where the shoppers are.", "low", "Store manager", 7),
    ],
}
FAMILY_EFFECT = {
    "low_pickup": "More shoppers pick up a product.",
    "low_traffic": "More shoppers visit the area.",
    "congestion": "Shorter waits at the busy hour.",
    "low_conversion": "More of the shoppers who handle it buy it.",
    "shelf_position": "More reaches on the moved lines.",
    "lost_sales": "A larger share of shoppers buy.",
    "hotspot_shift": "Staff and stock follow the traffic.",
}
FAMILY_OWNER = {
    "low_pickup": "Department lead", "low_conversion": "Department lead", "shelf_position": "Merchandiser",
    "low_traffic": "Store manager", "congestion": "Shift supervisor", "lost_sales": "Store manager",
    "hotspot_shift": "Store manager",
}

_STOP = {"the", "a", "an", "and", "of", "in", "at", "for", "to", "on", "by", "with", "from", "is", "are",
         "was", "but", "it", "its", "this", "that", "than", "per", "over", "today", "s", "sku", "x"}
_GENERIC = {"aisle", "area", "zone", "section", "bay", "shelf", "shelve", "product", "department", "dept",
            "range", "end", "gondola", "fixture", "display"}


# =========================================================================== text
def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "xes", "sses")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def words(text: Optional[str]) -> list[str]:
    """Lower-case, singularised word tokens without stop words."""
    toks = re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and "))
    return [_singular(t) for t in toks if t not in _STOP]


def subject_tokens(name: Optional[str]) -> list[str]:
    """Words naming a subject. Generic words (aisle, area, shelf...) drop out when others remain."""
    toks = words(name)
    specific = [t for t in toks if t not in _GENERIC]
    keep = specific if any(not t.isdigit() for t in specific) else toks
    out: list[str] = []
    for t in keep:
        if t not in out:
            out.append(t)
    return out


def text_tokens(text: Optional[str]) -> list[str]:
    """Words of a finding with numbers and times removed (for the TF-IDF comparison)."""
    return [t for t in words(text) if not any(c.isdigit() for c in t)]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    da, db_ = {t for t in sa if t.isdigit()}, {t for t in sb if t.isdigit()}
    if da and db_ and da != db_:
        return 0.0     # "Aisle 3" is never "Aisle 5"
    return len(sa & sb) / len(sa | sb)


def subject_similarity(aliases_a: list[list[str]], aliases_b: list[list[str]]) -> float:
    best = 0.0
    for a in aliases_a or []:
        for b in aliases_b or []:
            best = max(best, _jaccard(a, b))
    return best


def tfidf_cosine(a: list[str], b: list[str], corpus: list[list[str]]) -> float:
    """Cosine of TF-IDF vectors of two token lists; IDF from ``corpus`` (+ a and b)."""
    if not a or not b:
        return 0.0
    docs = list(corpus) + [a, b]
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))

    def vec(toks: list[str]) -> dict[str, float]:
        tf = Counter(toks)
        return {t: (c / len(toks)) * (math.log((1 + n) / (1 + df[t])) + 1.0) for t, c in tf.items()}

    va, vb = vec(a), vec(b)
    dot = sum(v * vb.get(t, 0.0) for t, v in va.items())
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def text_direction(text: Optional[str]) -> Optional[str]:
    toks = re.findall(r"[a-z]+", (text or "").lower())
    lo = sum(1 for t in toks if t in _LOW_WORDS)
    hi = sum(1 for t in toks if t in _HIGH_WORDS)
    if lo > hi:
        return "low"
    if hi > lo:
        return "high"
    return None


def classify(category: Optional[str], finding: Optional[str], evidence: Any) -> tuple[str, str, Optional[str]]:
    """(rule, family, direction) of one finding."""
    cat = (category or "").upper()
    ev = evidence if isinstance(evidence, dict) else {}
    rule = CATEGORY_RULES.get(cat)
    if cat == "MERCHANDISING":
        for rid, ev_key, rx in _MERCH_RULES:
            if ev_key in ev:
                rule = rid
                break
        else:
            for rid, _k, rx in _MERCH_RULES:
                if rx.search(finding or ""):
                    rule = rid
                    break
    if rule and rule in RULES:
        fam, direction = RULES[rule]
        return rule, fam, direction
    generic = (cat or "other").lower()
    return generic, generic, text_direction(finding)


# ====================================================================== context
@dataclass
class Context:
    """What the store's blueprint and Studio product zones call things."""

    zones: dict[str, dict] = field(default_factory=dict)       # lower name -> {id, name, products}
    products: dict[str, dict] = field(default_factory=dict)    # lower name -> {id, name, sku}

    def zone(self, name: Optional[str]) -> Optional[dict]:
        return self.zones.get((name or "").strip().lower())

    def product(self, name: Optional[str]) -> Optional[dict]:
        return self.products.get((name or "").strip().lower())


async def load_context(db: AsyncSession) -> Context:
    ctx = Context()
    try:
        from app.services.store_layout_service import store_layout_service

        layout = await store_layout_service.get_active_layout(db)
        if layout is not None:
            for z in await store_layout_service.list_zones(db, layout.id):
                ctx.zones[(z.name or "").strip().lower()] = {
                    "id": z.id, "name": z.name, "products": list(z.products or [])}
    except Exception as e:  # never let the blueprint break grouping
        logger.debug(f"insight topics: blueprint unavailable: {e}")
    try:
        from app.services.shelf_interaction_service import shelf_interaction_service

        for p in shelf_interaction_service.get_zones():
            ctx.products[(p.name or "").strip().lower()] = {
                "id": p.id, "name": p.name, "sku": getattr(p, "sku_id", None)}
    except Exception as e:
        logger.debug(f"insight topics: product zones unavailable: {e}")
    return ctx


@dataclass
class Signature:
    rule: str
    family: str
    direction: Optional[str]
    subject_key: str
    subject_label: str
    zone_id: Optional[str]
    product_zone_id: Optional[str]
    aliases: list[list[str]]
    products: list[str]

    @property
    def key(self) -> str:
        return f"{self.family}|{self.subject_key}|{self.direction or '-'}"


def signature(category: Optional[str], zone: Optional[str], finding: Optional[str], evidence: Any,
              ctx: Optional[Context] = None) -> Signature:
    ctx = ctx or Context()
    rule, family, direction = classify(category, finding, evidence)
    name = (zone or "").strip()
    z = ctx.zone(name)
    p = ctx.product(name)
    products = list((z or {}).get("products") or [])
    toks = subject_tokens(name)
    if name.lower() in ("store-wide", "store wide", "store", ""):
        key, label = "store", "the whole store"
    elif z is not None:
        key, label = f"zone:{z['id']}", z["name"]
    else:
        key, label = "name:" + " ".join(sorted(toks)), name
    aliases = [toks] if toks else []
    for prod in products:
        pt = subject_tokens(prod)
        if pt and pt not in aliases:
            aliases.append(pt)
    return Signature(rule=rule, family=family, direction=direction, subject_key=key, subject_label=label,
                     zone_id=(z or {}).get("id"), product_zone_id=(p or {}).get("id"),
                     aliases=aliases, products=products)


def _topic_meta(t: Topic) -> dict:
    return t.analysis if isinstance(t.analysis, dict) else {}


def _meta_copy(t: Topic) -> dict:
    """A deep copy, so assigning it back is always seen as a change."""
    return copy.deepcopy(_topic_meta(t))


def _is_store(key: Optional[str]) -> bool:
    parts = (key or "").split("|")
    return len(parts) > 1 and parts[1] == "store"


def match_topic(sig: Signature, finding: Optional[str], topics: list[Topic]) -> tuple[Optional[Topic], str]:
    """The topic a finding belongs to, and how it matched ("key", "similar" or "")."""
    live = [t for t in topics if not (t.topic_key or "").startswith(SPLIT_PREFIX)]
    exact = [t for t in live if t.topic_key == sig.key or sig.key in (t.alias_keys or [])]
    if exact:
        return max(exact, key=lambda t: (t.last_seen or "", t.created_at or datetime.min)), "key"
    cands = [t for t in live if t.family == sig.family
             and (t.direction is None or sig.direction is None or t.direction == sig.direction)]
    if not cands:
        return None, ""
    mine = text_tokens(finding)
    corpus = [text_tokens(_topic_meta(t).get("rep_text")) for t in cands]
    best, best_score = None, 0.0
    for t, rep in zip(cands, corpus):
        if sig.subject_key == "store" or _is_store(t.topic_key):
            subj = 1.0 if sig.subject_key == "store" and _is_store(t.topic_key) else 0.0
        else:
            subj = subject_similarity(sig.aliases, _topic_meta(t).get("subject_aliases") or [])
        if subj < SUBJECT_SIM_MIN:
            continue
        score = 0.6 * subj + 0.4 * tfidf_cosine(mine, rep, corpus)
        if score >= MERGE_SCORE_MIN and score > best_score:
            best, best_score = t, score
    return (best, "similar") if best is not None else (None, "")


# ======================================================================= dates
def _d(s: Any) -> Optional[date]:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def fmt_day(d: Optional[date], today: Optional[date] = None) -> str:
    if d is None:
        return "—"
    today = today or local_now().date()
    s = f"{d.day} {d.strftime('%b')}"
    return s if d.year == today.year else f"{s} {d.year}"


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _metric_subject(label: Optional[str]) -> str:
    """A METRICS label as the subject of "... went from X to Y".

    Share labels are written to follow a percentage ("21% of the store-average
    visits"); as a subject they need "the share" in front.
    """
    if not label:
        return "the measured figure"
    return f"the share {label}" if label.startswith("of ") else label


def _fmt_num(v: Optional[float], unit: str = "") -> str:
    if v is None:
        return "—"
    if unit == "%":
        return f"{v:.0f}%" if abs(v) >= 10 else f"{v:.1f}%"
    if abs(v) >= 100:
        s = f"{v:,.0f}"
    elif abs(v) >= 10:
        s = f"{v:.0f}"
    elif abs(v) >= 1:
        s = f"{v:.1f}"
    else:
        s = f"{v:.2f}"
    return f"{s}{unit}"


# ======================================================================= trend
def classify_trend(occ_days: list[date], analysed_days: list[date], *, today: date,
                   severities: Optional[list[tuple[date, str]]] = None,
                   series: Optional[list[tuple[date, float]]] = None,
                   higher_is_bad: Optional[bool] = None, reopened: bool = False) -> dict:
    """Trend of one topic. Pure; see the module docstring for the rules."""
    occ = sorted(set(occ_days))
    analysed = sorted({d for d in analysed_days if d <= today} | set(occ))
    out: dict[str, Any] = {"value": "stable", "reason": None, "metric": None}
    if not occ:
        out.update(value="improving", reason="No findings left in this topic.")
        return out
    last = occ[-1]
    quiet = [d for d in analysed if d > last]
    if len(quiet) >= QUIET_DAYS_IMPROVING:
        out.update(value="improving",
                   reason=f"Not found on the last {len(quiet)} analysed days.")
        return out

    metric = None
    if series and higher_is_bad is not None:
        pts = sorted(series)
        if len(pts) >= 3:
            k = max(1, len(pts) // 3)
            early = _mean([v for _d_, v in pts[:k]])
            late = _mean([v for _d_, v in pts[-k:]])
            if early is not None and late is not None:
                if early == 0:
                    rel = 0.0 if late == 0 else 1.0
                else:
                    rel = (late - early) / abs(early)
                metric = {"early": round(early, 3), "late": round(late, 3), "change_pct": round(rel * 100, 1),
                          "points": len(pts), "higher_is_bad": higher_is_bad,
                          "early_period": [pts[0][0].isoformat(), pts[k - 1][0].isoformat()],
                          "late_period": [pts[-k][0].isoformat(), pts[-1][0].isoformat()]}
                out["metric"] = metric
    if len(occ) == 1:
        out.update(value="new", reason="Found once so far.")
        return out

    if severities and len(severities) >= 2:
        sv = sorted(severities)
        first = SEVERITY_RANK.get((sv[0][1] or "").upper(), 9)
        latest = SEVERITY_RANK.get((sv[-1][1] or "").upper(), 9)
        if latest < first:
            out.update(value="worsening", reason=f"Priority rose from {sv[0][1].title()} to {sv[-1][1].title()}.")
            return out

    if metric is not None:
        bad = metric["change_pct"] / 100.0 * (1 if higher_is_bad else -1)
        if bad >= TREND_REL_CHANGE:
            out.update(value="worsening", reason="The measured figure moved the wrong way.")
            return out
        if bad <= -TREND_REL_CHANGE:
            out.update(value="improving", reason="The measured figure moved the right way.")
            return out

    if len(analysed) >= 10:
        last7, prev7 = analysed[-7:], analysed[-14:-7]
        n_late = sum(1 for d in occ if d in set(last7))
        n_early = sum(1 for d in occ if d in set(prev7))
        if prev7 and n_late - n_early >= FREQ_SHIFT_DAYS:
            out.update(value="worsening", reason=f"Found on {n_late} of the last 7 analysed days, "
                                                 f"up from {n_early} the week before.")
            return out
        if prev7 and n_early - n_late >= FREQ_SHIFT_DAYS:
            out.update(value="improving", reason=f"Found on {n_late} of the last 7 analysed days, "
                                                 f"down from {n_early} the week before.")
            return out

    gaps = [d for d in analysed if occ[0] < d < last and d not in set(occ)]
    if reopened or (len(occ) >= 3 and gaps):
        out.update(value="recurring",
                   reason="It came back after it was resolved." if reopened
                   else f"It comes and goes ({len(occ)} days with the finding, {len(gaps)} without in between).")
        return out
    out.update(value="stable", reason=f"Found on {len(occ)} analysed days in a row." if not gaps
               else f"Found on {len(occ)} analysed days.")
    return out


def weekday_pattern(occ_days: list[date], analysed_days: list[date]) -> Optional[str]:
    occ = sorted(set(occ_days))
    if len(occ) < 4:
        return None
    weekend = sum(1 for d in occ if d.weekday() >= 5)
    analysed_weekdays = sum(1 for d in set(analysed_days) if d.weekday() < 5)
    analysed_weekends = sum(1 for d in set(analysed_days) if d.weekday() >= 5)
    if weekend / len(occ) >= 0.7 and analysed_weekdays >= 3:
        return "It shows up mostly at weekends."
    top, n = Counter(d.weekday() for d in occ).most_common(1)[0]
    if n >= 3 and n / len(occ) >= 0.5:
        return f"It shows up mostly on {date(2024, 1, 1 + top).strftime('%A')}s."
    if weekend == 0 and analysed_weekends >= 2:
        return "It shows up on weekdays only."
    return None


def before_after(action_day: Optional[date], occ_days: list[date], analysed_days: list[date],
                 series: Optional[list[tuple[date, float]]], higher_is_bad: Optional[bool], *,
                 today: date, unit: str = "", label: str = "") -> Optional[dict]:
    """How the topic moved after an action was marked done (pure)."""
    if action_day is None:
        return None
    occ = set(occ_days)
    analysed = sorted({d for d in analysed_days if d <= today} | occ)
    lo, hi = action_day - timedelta(days=BEFORE_AFTER_DAYS), action_day + timedelta(days=BEFORE_AFTER_DAYS)
    before = [d for d in analysed if lo <= d < action_day]
    after = [d for d in analysed if action_day < d <= hi]
    out: dict[str, Any] = {
        "action_date": action_day.isoformat(),
        "before": {"analysed_days": len(before), "days_found": sum(1 for d in before if d in occ)},
        "after": {"analysed_days": len(after), "days_found": sum(1 for d in after if d in occ)},
        "metric": None, "verdict": "too_early", "text": "",
    }
    pts = sorted(series or [])
    mb = _mean([v for d, v in pts if lo <= d < action_day])
    ma = _mean([v for d, v in pts if action_day < d <= hi])
    if mb is not None and ma is not None and higher_is_bad is not None:
        rel = 0.0 if mb == 0 and ma == 0 else ((ma - mb) / abs(mb) if mb else 1.0)
        out["metric"] = {"label": label, "unit": unit, "before": round(mb, 3), "after": round(ma, 3),
                         "change_pct": round(rel * 100, 1)}
    if len(after) < BEFORE_AFTER_MIN_DAYS:
        need = BEFORE_AFTER_MIN_DAYS - len(after)
        out["text"] = (f"Action marked done on {fmt_day(action_day, today)}: too early to judge; "
                       f"check again after {need} more analysed day{'s' if need != 1 else ''}.")
        return out
    rb = out["before"]["days_found"] / len(before) if before else None
    ra = out["after"]["days_found"] / len(after)
    better = worse = False
    if rb is not None:
        better |= rb - ra >= 0.25
        worse |= ra - rb >= 0.25
    if out["metric"] is not None:
        bad = out["metric"]["change_pct"] / 100.0 * (1 if higher_is_bad else -1)
        better |= bad <= -TREND_REL_CHANGE
        worse |= bad >= TREND_REL_CHANGE
    out["verdict"] = "worse" if worse and not better else ("helped" if better and not worse else "no_clear_change")
    parts = [f"Since the action was marked done on {fmt_day(action_day, today)} it was found on "
             f"{out['after']['days_found']} of {len(after)} analysed days"]
    if rb is not None:
        parts.append(f"(before: {out['before']['days_found']} of {len(before)})")
    txt = " ".join(parts)
    if out["metric"] is not None:
        txt += (f"; {_metric_subject(label)} went from {_fmt_num(mb, unit)} to {_fmt_num(ma, unit)}")
    txt += {"helped": ". It looks like the action helped.",
            "worse": ". It has got worse since; try the next step.",
            "no_clear_change": ". No clear change yet; try the next step."}[out["verdict"]]
    out["text"] = txt
    return out


def action_plan(family: str, subject: str, *, latest_action: Optional[str], latest_cause: Optional[str],
                today: date, previous: Optional[list] = None, trend: Optional[str] = None,
                verdict: Optional[str] = None) -> list[dict]:
    """Short prioritised plan; keeps the done state of actions with the same id."""
    done = {a.get("id"): a for a in (previous or []) if isinstance(a, dict) and a.get("done")}
    plan: list[dict] = []
    if latest_action:
        plan.append({"id": "latest_rule_action", "action": latest_action,
                     "why": latest_cause or "What the latest finding recommends.",
                     "expected_effect": FAMILY_EFFECT.get(family, "The finding stops appearing."),
                     "effort": "low", "owner_role": FAMILY_OWNER.get(family, "Store manager"), "check_days": 3})
    for (aid, action, why, effect, effort, owner, days) in PLAYBOOKS.get(family, []):
        plan.append({"id": aid, "action": action.format(s=subject), "why": why, "expected_effect": effect,
                     "effort": effort, "owner_role": owner, "check_days": days})
    if verdict in ("no_clear_change", "worse"):
        plan.append({"id": "escalate", "action": "Raise this with the store manager: the earlier fix did not "
                                                 "move the numbers.",
                     "why": "The issue persisted after an action was marked done.",
                     "expected_effect": "A decision on the next, bigger change.", "effort": "low",
                     "owner_role": "Store manager", "check_days": 7})
    plan = plan[:5]
    # Done items sink to the end; open items keep their priority order.
    open_items = [a for a in plan if a["id"] not in done]
    done_items = [a for a in plan if a["id"] in done]
    out = []
    for i, a in enumerate(open_items + done_items, start=1):
        prev = done.get(a["id"])
        check = today + timedelta(days=int(a.pop("check_days")))
        a.update(priority=i, check_after=check.isoformat(), done=bool(prev),
                 done_at=(prev or {}).get("done_at"))
        if prev and prev.get("done_at"):
            pd = _d(prev["done_at"])
            if pd:
                a["check_after"] = (pd + timedelta(days=3)).isoformat()
        out.append(a)
    return out


# ===================================================================== service
def _plain(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


class InsightTopicService:
    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Any = None
        self._task: Optional[asyncio.Task] = None
        self.last_backfill: Optional[dict] = None

    def lock(self) -> asyncio.Lock:
        """One grouping/analysis at a time (scheduler, backfill and topic actions)."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    # ------------------------------------------------------------ grouping
    async def _all_topics(self, db: AsyncSession) -> list[Topic]:
        return list((await db.execute(select(Topic))).scalars().all())

    def _new_topic(self, sig: Signature, row: Rec) -> Topic:
        title = TITLES.get(sig.family, "{s}").format(s=sig.subject_label)
        if sig.family not in TITLES:
            title = _plain(row.finding)[:200] or sig.subject_label
        return Topic(
            id=f"topic_{uuid.uuid4().hex[:12]}", topic_key=sig.key[:255], alias_keys=[],
            title=title[:255], family=sig.family[:64], category=(row.category or "")[:64],
            zone_id=sig.zone_id, zone_name=(row.zone or "")[:128], subject=sig.subject_label[:255],
            direction=sig.direction, first_seen=row.date, last_seen=row.date, occurrences=1,
            status="open", severity=(row.severity or "MEDIUM").upper()[:16], needs_analysis=True,
            analysis={"subject_aliases": sig.aliases, "rep_text": _plain(row.finding),
                      "products": sig.products, "product_zone_id": sig.product_zone_id, "rules": [sig.rule]},
            created_at=utcnow(),
        )

    async def assign_unassigned(self, db: AsyncSession, *, limit: Optional[int] = None,
                                ctx: Optional[Context] = None) -> dict:
        """Group rows with no topic (oldest first). Idempotent; commits once per call."""
        stmt = (select(Rec).where(Rec.topic_id.is_(None))
                .order_by(Rec.date, Rec.created_at, Rec.id).limit(limit or BACKFILL_BATCH))
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            return {"assigned": 0, "created": 0, "touched": []}
        ctx = ctx or await load_context(db)
        topics = await self._all_topics(db)
        created = 0
        touched: set[str] = set()
        for r in rows:
            sig = signature(r.category, r.zone, r.finding, r.evidence, ctx)
            t, how = match_topic(sig, r.finding, topics)
            if t is None:
                t = self._new_topic(sig, r)
                db.add(t)
                topics.append(t)
                created += 1
            else:
                meta = _meta_copy(t)
                aliases = [list(a) for a in meta.get("subject_aliases") or []]
                for a in sig.aliases:
                    if a not in aliases:
                        aliases.append(a)
                meta["subject_aliases"] = aliases[:12]
                rules = list(meta.get("rules") or [])
                if sig.rule not in rules:
                    rules.append(sig.rule)
                meta["rules"] = rules
                if (r.date or "") >= (t.last_seen or ""):
                    meta["rep_text"] = _plain(r.finding)
                if sig.product_zone_id and not meta.get("product_zone_id"):
                    meta["product_zone_id"] = sig.product_zone_id
                for p in sig.products:
                    meta.setdefault("products", [])
                    if p not in meta["products"]:
                        meta["products"].append(p)
                t.analysis = meta
                if how == "similar" and sig.key != t.topic_key and sig.key not in (t.alias_keys or []):
                    t.alias_keys = list(t.alias_keys or []) + [sig.key[:255]]
                if sig.zone_id and not t.zone_id:
                    t.zone_id = sig.zone_id
                # A finding on a later day re-opens a resolved topic.
                if t.status == "resolved" and t.resolved_at is not None \
                        and (r.date or "") > to_local(t.resolved_at).date().isoformat():
                    t.status = "open"
                    t.reopened_at = utcnow()
                t.needs_analysis = True
            r.topic_id = t.id
            touched.add(t.id)
        await db.flush()
        for t in topics:
            if t.id in touched:
                await self._recount(db, t)
        await db.commit()
        return {"assigned": len(rows), "created": created, "touched": sorted(touched)}

    async def _members(self, db: AsyncSession, topic_id: str) -> list[Rec]:
        return list((await db.execute(select(Rec).where(Rec.topic_id == topic_id)
                                      .order_by(Rec.date, Rec.created_at))).scalars().all())

    async def _recount(self, db: AsyncSession, t: Topic, members: Optional[list[Rec]] = None) -> list[Rec]:
        members = members if members is not None else await self._members(db, t.id)
        if not members:
            return members
        days = sorted({m.date for m in members if m.date})
        t.first_seen, t.last_seen, t.occurrences = days[0], days[-1], len(days)
        latest = max(members, key=lambda m: (m.date or "", m.created_at or datetime.min))
        t.category = (latest.category or t.category or "")[:64]
        t.zone_name = (latest.zone or t.zone_name or "")[:128]
        last = _d(days[-1])
        recent = [m for m in members if last and _d(m.date) and (last - _d(m.date)).days <= 14]
        t.severity = min(((m.severity or "MEDIUM").upper() for m in (recent or members)),
                         key=lambda s: SEVERITY_RANK.get(s, 9))[:16]
        t.updated_at = utcnow()
        return members

    async def prune_orphans(self, db: AsyncSession) -> int:
        """Delete topics that no longer hold a finding (store reset, rows replaced)."""
        used = select(distinct(Rec.topic_id)).where(Rec.topic_id.isnot(None))
        res = await db.execute(delete(Topic).where(Topic.id.notin_(used)))
        await db.commit()
        return int(res.rowcount or 0)

    async def wake_snoozed(self, db: AsyncSession, today: Optional[date] = None) -> int:
        today = today or local_now().date()
        rows = (await db.execute(select(Topic).where(Topic.status == "snoozed"))).scalars().all()
        n = 0
        for t in rows:
            if not t.snooze_until or t.snooze_until < today.isoformat():
                t.status, t.snooze_until, t.needs_analysis = "open", None, True
                n += 1
        if n:
            await db.commit()
        return n

    # ------------------------------------------------------------- metrics
    async def analysed_days(self, db: AsyncSession, since: date) -> list[date]:
        """Store days on which the analysis ran (runs table) or recorded any finding."""
        days: set[date] = set()
        try:
            for (g,) in (await db.execute(select(AnalysisRunModel.generated_at)
                                          .where(AnalysisRunModel.generated_at >= local_midnight_utc(since)))).all():
                if g is not None:
                    days.add(to_local(g).date())
        except Exception as e:
            logger.debug(f"analysis runs unavailable: {e}")
        for (s,) in (await db.execute(select(distinct(Rec.date)).where(Rec.date >= since.isoformat()))).all():
            d = _d(s)
            if d:
                days.add(d)
        return sorted(days)

    async def zone_series(self, db: AsyncSession, zone_id: str, start: date, end: date) -> dict:
        """Daily visits, average dwell and pick-up share for one blueprint zone (one indexed query)."""
        from app.models.db_models import ZoneVisitModel as ZV
        from app.services.retail_metrics_service import _not_duplicate, _visit_is_real

        a, b = local_midnight_utc(start), local_midnight_utc(end + timedelta(days=1))
        hour = func.strftime("%Y-%m-%d %H", ZV.entered_at)
        half = func.strftime("%M", ZV.entered_at) >= "30"
        closed = ZV.exited_at.isnot(None)
        rows = (await db.execute(
            select(hour, half, func.count(ZV.id),
                   func.sum(case((closed, ZV.dwell_seconds), else_=0.0)),
                   func.sum(case((closed, 1), else_=0)),
                   func.sum(case((ZV.interacted.is_(True), 1), else_=0)))
            .where(ZV.zone_id == zone_id, ZV.entered_at >= a, ZV.entered_at < b,
                   _visit_is_real(), _not_duplicate(ZV.camera_id))
            .group_by(hour, half))).all()
        by_day: dict[date, list[float]] = {}
        by_hour: Counter = Counter()
        for h, hf, n, dsum, dn, inter in rows:
            if not h:
                continue
            try:
                t = datetime.strptime(h, "%Y-%m-%d %H") + timedelta(minutes=30 if hf else 0)
            except ValueError:
                continue
            loc = to_local(t)
            acc = by_day.setdefault(loc.date(), [0, 0.0, 0, 0])
            acc[0] += int(n or 0)
            acc[1] += float(dsum or 0.0)
            acc[2] += int(dn or 0)
            acc[3] += int(inter or 0)
            by_hour[loc.hour] += int(n or 0)
        days = [{"date": d.isoformat(), "visits": v[0],
                 "avg_dwell_seconds": round(v[1] / v[2], 1) if v[2] else None,
                 "engagement_pct": round(v[3] / v[0] * 100.0, 1) if v[0] else None}
                for d, v in sorted(by_day.items())]
        peak = by_hour.most_common(1)[0][0] if by_hour else None
        return {"zone_id": zone_id, "from": start.isoformat(), "to": end.isoformat(), "days": days,
                "peak_hour_local": f"{peak:02d}:00" if peak is not None else None}

    async def product_series(self, db: AsyncSession, product_zone_id: str, start: date, end: date) -> dict:
        from app.models.db_models import ShelfInteractionModel as SI

        a, b = local_midnight_utc(start), local_midnight_utc(end + timedelta(days=1))
        hour = func.strftime("%Y-%m-%d %H", SI.timestamp)
        half = func.strftime("%M", SI.timestamp) >= "30"
        rows = (await db.execute(
            select(hour, half, func.count(SI.id))
            .where(SI.shelf_zone_id == product_zone_id, SI.timestamp >= a, SI.timestamp < b)
            .group_by(hour, half))).all()
        by_day: Counter = Counter()
        for h, hf, n in rows:
            try:
                t = datetime.strptime(h, "%Y-%m-%d %H") + timedelta(minutes=30 if hf else 0)
            except (TypeError, ValueError):
                continue
            by_day[to_local(t).date()] += int(n or 0)
        return {"product_zone_id": product_zone_id, "from": start.isoformat(), "to": end.isoformat(),
                "days": [{"date": d.isoformat(), "reaches": n} for d, n in sorted(by_day.items())]}

    async def metric_series(self, db: AsyncSession, t: Topic, today: Optional[date] = None) -> dict:
        today = today or local_now().date()
        first = _d(t.first_seen) or today
        start = min(first, today - timedelta(days=WINDOW_DAYS - 1))
        out: dict[str, Any] = {"window": {"from": start.isoformat(), "to": today.isoformat(),
                                          "days": (today - start).days + 1}}
        try:
            if t.zone_id:
                out["zone"] = await self.zone_series(db, t.zone_id, start, today)
            pz = _topic_meta(t).get("product_zone_id")
            if pz:
                out["product"] = await self.product_series(db, pz, start, today)
        except Exception as e:   # the analysis stands without the extra series
            logger.warning(f"insight topic {t.id}: metric series unavailable: {e}")
        return out

    # ------------------------------------------------------------- analysis
    async def analyse_topic(self, db: AsyncSession, t: Topic, *, today: Optional[date] = None,
                            narrate: bool = False, analysed: Optional[list[date]] = None,
                            commit: bool = True) -> Topic:
        today = today or local_now().date()
        members = await self._recount(db, t)
        if not members:
            return t
        occ = sorted({_d(m.date) for m in members if _d(m.date)})
        since = min(occ[0], today - timedelta(days=WINDOW_DAYS - 1))
        if analysed is None:
            analysed = await self.analysed_days(db, since)
        analysed_in = sorted({d for d in analysed if since <= d <= today} | set(occ))
        meta = _meta_copy(t)
        latest = max(members, key=lambda m: (m.date or "", m.created_at or datetime.min))

        # Headline metric: the most common rule among the members that has a numeric spec.
        rules = Counter(classify(m.category, m.finding, m.evidence)[0] for m in members)
        head_rule = next((r for r, _n in rules.most_common() if r in METRICS), None)
        series: list[tuple[date, float]] = []
        label, unit, hib = "", "", None
        if head_rule:
            fn, label, unit, hib = METRICS[head_rule]
            for m in members:
                if classify(m.category, m.finding, m.evidence)[0] != head_rule:
                    continue
                v = fn(m.evidence) if isinstance(m.evidence, dict) else None
                if v is not None and _d(m.date):
                    series.append((_d(m.date), v))
        metrics = await self.metric_series(db, t, today)
        source = "findings"
        zspec = ZONE_METRIC_FOR_FAMILY.get(t.family)
        if len(series) < 3 and zspec and metrics.get("zone", {}).get("days"):
            key, label, unit, hib = zspec
            series = [(_d(r["date"]), float(r[key])) for r in metrics["zone"]["days"]
                      if r.get(key) is not None and _d(r["date"])]
            source = "zone"
        elif len(series) < 3 and metrics.get("product", {}).get("days") and t.family in ("low_pickup",):
            label, unit, hib = "shelf reaches per day", "", False
            series = [(_d(r["date"]), float(r["reaches"])) for r in metrics["product"]["days"] if _d(r["date"])]
            source = "product"

        trend = classify_trend(occ, analysed_in, today=today,
                               severities=[(_d(m.date), m.severity or "MEDIUM") for m in members if _d(m.date)],
                               series=series, higher_is_bad=hib, reopened=t.reopened_at is not None)
        if trend.get("metric"):
            trend["metric"].update(label=label, unit=unit, source=source)

        # When was an action done? The topic's own record, else a finding marked Done.
        action_day = _d(t.action_done_at)
        if action_day is None:
            applied = [to_local(m.updated_at).date() for m in members if m.status == "APPLIED" and m.updated_at]
            action_day = min(applied) if applied else None
        ba = before_after(action_day, occ, analysed_in, series, hib, today=today, unit=unit, label=label)

        pattern = weekday_pattern(occ, analysed_in)
        peak = Counter((m.evidence or {}).get("peak_hour_local") for m in members
                       if isinstance(m.evidence, dict) and (m.evidence or {}).get("peak_hour_local"))
        time_text = None
        if peak:
            time_text = f"The peak is around {peak.most_common(1)[0][0]}."
        elif metrics.get("zone", {}).get("peak_hour_local") and t.family in ("congestion", "low_traffic"):
            time_text = f"The area is busiest around {metrics['zone']['peak_hour_local']}."

        subject = t.subject or t.zone_name or "this area"
        causes = Counter(_plain(m.root_cause) for m in members if m.root_cause)
        cause = causes.most_common(1)[0][0] if causes else None
        actions = action_plan(t.family, subject, latest_action=_plain(latest.action_item) or None,
                              latest_cause=_plain(latest.root_cause) or None, today=today,
                              previous=t.unified_actions, trend=trend["value"],
                              verdict=(ba or {}).get("verdict"))

        n_an = len([d for d in analysed_in if d >= occ[0]])
        if len(occ) == 1:
            development = f"Found once so far, on {fmt_day(occ[0], today)}."
        else:
            share = f"all {n_an}" if len(occ) == n_an else f"{len(occ)} of the {n_an}"
            development = (f"Found on {share} analysed days since {fmt_day(occ[0], today)}, "
                           f"most recently {fmt_day(occ[-1], today)}.")
        word = TREND_LABELS.get(trend["value"], trend["value"])
        m = trend.get("metric")
        metric_txt = (f"{_metric_subject(m['label'])} went from {_fmt_num(m['early'], unit)} early on to "
                      f"{_fmt_num(m['late'], unit)} lately" if m and m.get("label") else "")
        if trend["value"] in ("worsening", "improving") and trend["reason"] in (
                "The measured figure moved the wrong way.", "The measured figure moved the right way.") and metric_txt:
            trend_txt = f"{word}: {metric_txt}."
        elif trend["value"] == "new":
            trend_txt = ""
        else:
            reason = trend["reason"] or ""
            trend_txt = f"{word}: {reason[:1].lower() + reason[1:]}" if reason else word + "."
            if metric_txt and trend["value"] != "improving":
                trend_txt += f" ({metric_txt[:1].upper() + metric_txt[1:]}.)"
        what = _plain(latest.finding)
        parts = [what, development, trend_txt]
        if cause:
            parts.append(f"Likely cause: {cause if cause[:2].isupper() else cause[0].lower() + cause[1:]}")
        if pattern:
            parts.append(pattern)
        if time_text:
            parts.append(time_text)
        if ba and ba.get("text"):
            parts.append(ba["text"])
        summary = " ".join(p.strip() for p in parts if p and p.strip())

        meta.update({
            "what": what, "development": development, "cause": cause, "pattern": pattern, "time_of_day": time_text,
            "trend": trend, "occurrence_days": [d.isoformat() for d in occ][-120:],
            "analysed_days": [d.isoformat() for d in analysed_in][-120:],
            "analysed_day_count": n_an, "frequency_pct": round(len(occ) / n_an * 100.0, 1) if n_an else None,
            "headline": {"rule": head_rule, "label": label, "unit": unit, "higher_is_bad": hib, "source": source,
                         "series": [{"date": d.isoformat(), "value": round(v, 3)} for d, v in sorted(series)][-60:]},
            "zone_peak_hour_local": metrics.get("zone", {}).get("peak_hour_local"),
            "before_after": ba, "summary_source": "template", "model": None,
            "rules": sorted(rules),
        })
        if narrate:
            # Release the SQLite write lock before the model call (up to 3 x 60 s):
            # an autoflushed topic UPDATE held it across narration, so the
            # pipeline flush, theft incidents and alert rows timed out and were
            # lost meanwhile (audit L4). Nothing below needs the old transaction.
            await db.commit()
            narrated = await asyncio.to_thread(self._narrate, t, meta, summary)
            if narrated:
                summary, meta["summary_source"], meta["model"] = narrated[0], "local_model", narrated[1]
            else:
                meta["narration_note"] = "The local AI model was unavailable or cited figures it was not given; " \
                                         "the summary below is the standard one."
        t.trend = trend["value"]
        t.unified_summary = summary
        t.unified_actions = actions
        t.analysis = meta
        t.data_window = metrics.get("window")
        t.analysed_at = utcnow()
        t.needs_analysis = False
        if commit:
            await db.commit()
        return t

    def _narrate(self, t: Topic, meta: dict, template: str) -> Optional[tuple[str, str]]:
        """Ask the local model (Ollama on this box) to rewrite the summary. None on any doubt."""
        try:
            from app.services.business_analysis_service import BusinessAnalysisService, business_analysis_service

            model = business_analysis_service.select_model()
        except Exception:
            return None
        if not model:
            return None
        facts = {"topic": t.title, "area": t.subject, "products": meta.get("products") or [],
                 "facts": template, "trend": TREND_LABELS.get(t.trend or "", t.trend)}
        facts_txt = json.dumps(facts, ensure_ascii=False)
        prompt = ("You are a retail operations analyst writing for a supermarket manager in Australia. "
                  "Below is everything measured about one recurring issue.\n\n"
                  f"{facts_txt}\n\n"
                  "Rewrite it as one short paragraph (at most 4 sentences): what is happening, how it has "
                  "developed, the likely cause, and what to do first. Use plain Australian English. Use only "
                  "the numbers and dates given. Do not invent figures. Do not use markdown.")
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False,
                              "options": {"temperature": 0.2, "num_predict": 400}}).encode()
        try:
            req = urllib.request.Request(f"{settings.OLLAMA_BASE_URL}/api/generate", data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=NARRATE_TIMEOUT_SEC) as r:
                body = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
            return None
        text = BusinessAnalysisService._strip_reasoning(body.get("response") or "")
        if not text or len(text) > 1500:
            return None
        allowed = set(re.findall(r"\d+(?:\.\d+)?", facts_txt))
        for n in re.findall(r"\d+(?:\.\d+)?", text):
            if n not in allowed and n.rstrip("0").rstrip(".") not in allowed:
                logger.info(f"insight topic narration discarded: cites {n}, not in the facts")
                return None
        return text, model

    # ------------------------------------------------------------- sync / backfill
    async def sync(self, db: AsyncSession, *, today: Optional[date] = None, narrate_top: int = 0,
                   max_analyse: int = MAX_ANALYSE_PER_SYNC, max_batches: int = 5) -> dict:
        """Group new findings and refresh the analyses that are due. Bounded per call."""
        async with self.lock():
            return await self._sync(db, today=today, narrate_top=narrate_top, max_analyse=max_analyse,
                                    max_batches=max_batches)

    async def _sync(self, db: AsyncSession, *, today: Optional[date], narrate_top: int, max_analyse: int,
                    max_batches: int) -> dict:
        today = today or local_now().date()
        ctx = await load_context(db)
        assigned = created = 0
        for _ in range(max(1, max_batches)):
            r = await self.assign_unassigned(db, ctx=ctx)
            assigned += r["assigned"]
            created += r["created"]
            if r["assigned"] < BACKFILL_BATCH:
                break
            await asyncio.sleep(0)
        removed = await self.prune_orphans(db)
        woke = await self.wake_snoozed(db, today)
        midnight = local_midnight_utc(today)
        if max_analyse <= 0:
            remaining = await db.scalar(select(func.count(Rec.id)).where(Rec.topic_id.is_(None))) or 0
            return {"assigned": assigned, "created": created, "removed_empty": removed, "unsnoozed": woke,
                    "analysed": 0, "unassigned_remaining": int(remaining)}
        due = list((await db.execute(select(Topic).where(
            (Topic.needs_analysis.is_(True))
            | ((Topic.status != "resolved") & ((Topic.analysed_at.is_(None)) | (Topic.analysed_at < midnight))))
            .order_by(Topic.last_seen.desc()).limit(max_analyse))).scalars().all())
        analysed = await self.analysed_days(db, today - timedelta(days=max(WINDOW_DAYS, 120)))
        narrate_ids: set[str] = set()
        if narrate_top > 0:
            ranked = sorted((t for t in due if t.status in ("open", "acknowledged")),
                            key=lambda t: (TRENDS.index(t.trend) if t.trend in TRENDS else 9,
                                           SEVERITY_RANK.get((t.severity or "").upper(), 9)))
            narrate_ids = {t.id for t in ranked[:narrate_top]}
        n = 0
        for t in due:
            try:
                await self.analyse_topic(db, t, today=today, analysed=analysed, narrate=t.id in narrate_ids)
                n += 1
            except Exception as e:  # one bad topic must not stop the rest
                logger.warning(f"insight topic {t.id} analysis failed: {e}")
                await db.rollback()
            await asyncio.sleep(0)
        remaining = await db.scalar(select(func.count(Rec.id)).where(Rec.topic_id.is_(None))) or 0
        return {"assigned": assigned, "created": created, "removed_empty": removed, "unsnoozed": woke,
                "analysed": n, "unassigned_remaining": int(remaining)}

    async def backfill(self, db: AsyncSession, *, max_rows: Optional[int] = None, max_analyse: int = 200,
                       today: Optional[date] = None) -> dict:
        """Group every ungrouped finding in batches, then analyse the topics. Idempotent."""
        batches = max(1, math.ceil((max_rows or 10 ** 9) / BACKFILL_BATCH))
        started = utcnow()
        total = {"assigned": 0, "created": 0, "analysed": 0}
        # The lock is taken per batch, so analysis runs and topic actions are never
        # held up for the whole backfill.
        res = None
        for _ in range(batches):
            async with self.lock():
                res = await self._sync(db, today=today, narrate_top=0, max_analyse=0, max_batches=1)
            total["assigned"] += res["assigned"]
            total["created"] += res["created"]
            if res["assigned"] == 0 or res["unassigned_remaining"] == 0:
                break
            await asyncio.sleep(0.05)
        while total["analysed"] < max_analyse:
            async with self.lock():
                res = await self._sync(db, today=today, narrate_top=0,
                                       max_analyse=min(10, max_analyse - total["analysed"]), max_batches=1)
            total["analysed"] += res["analysed"]
            if res["analysed"] == 0:
                break
            await asyncio.sleep(0.05)
        total["unassigned_remaining"] = (res or {}).get("unassigned_remaining", 0)
        total["seconds"] = round((utcnow() - started).total_seconds(), 2)
        self.last_backfill = {**total, "finished_at": utcnow().isoformat()}
        return total

    def start_background(self) -> None:
        """Startup backfill after a short delay; non-blocking, cancelled on shutdown."""
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._background(), name="insight-topics-backfill")
        except RuntimeError:
            self._task = None

    async def _background(self) -> None:
        from app.database import async_session_factory

        try:
            await asyncio.sleep(max(0, BACKFILL_DELAY_SEC))
            async with async_session_factory() as db:
                res = await self.backfill(db)
            logger.info(f"Insight topics backfill: {res}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the backfill take anything down
            logger.warning(f"Insight topics backfill failed: {e}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ---------------------------------------------------------------- reads
    @staticmethod
    def effective_status(t: Topic, today: Optional[date] = None) -> str:
        today = today or local_now().date()
        if t.status == "snoozed" and (not t.snooze_until or t.snooze_until < today.isoformat()):
            return "open"
        return t.status if t.status in STATUSES else "open"

    def serialize(self, t: Topic, today: Optional[date] = None) -> dict:
        today = today or local_now().date()
        meta = _topic_meta(t)
        occ = {s for s in meta.get("occurrence_days") or []}
        analysed = {s for s in meta.get("analysed_days") or []}
        timeline = []
        for i in range(TIMELINE_DAYS - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            timeline.append({"date": d, "seen": d in occ, "analysed": d in analysed or d in occ})
        first = _d(t.first_seen)
        sev = (t.severity or "MEDIUM").upper()
        trend = meta.get("trend") or {}
        ack_at = to_local(t.acknowledged_at).date().isoformat() if t.acknowledged_at else None
        status = self.effective_status(t, today)
        return {
            "id": t.id, "title": t.title, "family": t.family, "category": t.category,
            "zone_id": t.zone_id, "zone_name": t.zone_name, "subject": t.subject,
            "products": meta.get("products") or [],
            "status": status, "snooze_until": t.snooze_until if status == "snoozed" else None,
            "trend": t.trend or "new", "trend_label": TREND_LABELS.get(t.trend or "new", t.trend),
            "trend_reason": trend.get("reason"), "trend_metric": trend.get("metric"),
            "severity": sev, "priority": sev.lower(), "priority_rank": SEVERITY_RANK.get(sev, 9),
            "first_seen": t.first_seen, "last_seen": t.last_seen, "occurrences": t.occurrences,
            "seen_text": (f"Seen on {t.occurrences} day{'s' if t.occurrences != 1 else ''} since "
                          f"{fmt_day(first, today)}" if first else None),
            "new_since_acknowledged": (sum(1 for s in occ if s > ack_at) if ack_at and status == "acknowledged"
                                       else None),
            "unified_summary": t.unified_summary,
            "summary_source": meta.get("summary_source") or ("template" if t.unified_summary else None),
            "model": meta.get("model"), "narration_note": meta.get("narration_note"),
            "actions": t.unified_actions or [],
            "action_done_at": t.action_done_at,
            "before_after": meta.get("before_after"),
            "pattern": meta.get("pattern"), "time_of_day": meta.get("time_of_day"),
            "frequency_pct": meta.get("frequency_pct"),
            "headline": meta.get("headline"),
            "timeline": timeline,
            "analysed_at": to_local(t.analysed_at).isoformat() if t.analysed_at else None,
            "data_window": t.data_window,
            "acknowledged_at": to_local(t.acknowledged_at).isoformat() if t.acknowledged_at else None,
            "resolved_at": to_local(t.resolved_at).isoformat() if t.resolved_at else None,
            "reopened_at": to_local(t.reopened_at).isoformat() if t.reopened_at else None,
            "split_out": (t.topic_key or "").startswith(SPLIT_PREFIX),
        }

    async def list_topics(self, db: AsyncSession, *, status: Optional[str] = None, zone: Optional[str] = None,
                          trend: Optional[str] = None, include_resolved: bool = False,
                          today: Optional[date] = None) -> dict:
        today = today or local_now().date()
        rows = list((await db.execute(select(Topic))).scalars().all())
        items = [self.serialize(t, today) for t in rows]
        counts = Counter(i["status"] for i in items)
        want = (status or "").strip().lower() or None
        if want:
            items = [i for i in items if i["status"] == want]
        elif not include_resolved:
            items = [i for i in items if i["status"] != "resolved"]
        if zone:
            z = zone.strip().lower()
            items = [i for i in items if z in ((i["zone_id"] or "").lower(), (i["zone_name"] or "").lower(),
                                                (i["subject"] or "").lower())]
        if trend:
            items = [i for i in items if i["trend"] == trend.strip().lower()]
        status_order = {"open": 0, "acknowledged": 1, "snoozed": 2, "resolved": 3}
        trend_order = {k: i for i, k in enumerate(TRENDS)}
        # Newest first, then (stable sort) open before handled, priority, trend.
        items.sort(key=lambda i: i["last_seen"] or "", reverse=True)
        items.sort(key=lambda i: (status_order.get(i["status"], 9), i["priority_rank"],
                                  trend_order.get(i["trend"], 9)))
        unassigned = await db.scalar(select(func.count(Rec.id)).where(Rec.topic_id.is_(None))) or 0
        return {
            "topics": items,
            "counts": {"total": len(rows), "open": counts.get("open", 0),
                       "acknowledged": counts.get("acknowledged", 0), "snoozed": counts.get("snoozed", 0),
                       "resolved": counts.get("resolved", 0), "badge": counts.get("open", 0)},
            "unassigned_insights": int(unassigned),
            "generated_at": local_now().isoformat(),
        }

    async def get(self, db: AsyncSession, topic_id: str) -> Optional[Topic]:
        return await db.get(Topic, topic_id)

    @staticmethod
    def serialize_member(r: Rec) -> dict:
        return {"id": r.id, "date": r.date, "category": r.category, "severity": (r.severity or "").upper(),
                "zone": r.zone, "title": r.finding, "why": r.root_cause, "do": r.action_item,
                "status": r.status, "evidence": r.evidence, "source": r.source,
                "rule": classify(r.category, r.finding, r.evidence)[0],
                "updated_at": to_local(r.updated_at).isoformat() if r.updated_at else None}

    async def detail(self, db: AsyncSession, topic_id: str, today: Optional[date] = None) -> Optional[dict]:
        t = await self.get(db, topic_id)
        if t is None:
            return None
        members = await self._members(db, t.id)
        out = self.serialize(t, today)
        out["insights"] = [self.serialize_member(m) for m in reversed(members)]
        out["metrics"] = await self.metric_series(db, t, today)
        out["analysis"] = {k: v for k, v in _topic_meta(t).items()
                           if k not in ("subject_aliases", "rep_text")}
        return out

    # -------------------------------------------------------------- actions
    async def _members_pending_to_reviewed(self, db: AsyncSession, t: Topic) -> None:
        for m in await self._members(db, t.id):
            if m.status == "PENDING":
                m.status, m.updated_at = "REVIEWED", utcnow()

    async def set_status(self, db: AsyncSession, t: Topic, status: str, *, days: int = SNOOZE_DAYS_DEFAULT,
                         today: Optional[date] = None) -> Topic:
        today = today or local_now().date()
        async with self.lock():
            if status == "acknowledged":
                t.status, t.acknowledged_at, t.snooze_until = "acknowledged", utcnow(), None
                await self._members_pending_to_reviewed(db, t)
            elif status == "snoozed":
                t.status, t.snooze_until = "snoozed", (today + timedelta(days=max(1, min(int(days), 90)))).isoformat()
            elif status == "resolved":
                t.status, t.resolved_at, t.snooze_until = "resolved", utcnow(), None
                await self._members_pending_to_reviewed(db, t)
            elif status == "open":
                t.status, t.snooze_until = "open", None
            else:
                raise ValueError(f"unknown status {status!r}")
            t.updated_at = utcnow()
            await db.commit()
            return t

    async def mark_action_done(self, db: AsyncSession, t: Topic, action_id: Optional[str] = None,
                               on: Optional[date] = None, today: Optional[date] = None) -> Topic:
        today = today or local_now().date()
        on = on or today
        async with self.lock():
            actions = [dict(a) for a in (t.unified_actions or [])]
            if action_id is not None and action_id not in {a.get("id") for a in actions}:
                raise KeyError(action_id)
            target = action_id or next((a["id"] for a in actions if not a.get("done")), None)
            for a in actions:
                if a.get("id") == target:
                    a["done"], a["done_at"] = True, on.isoformat()
            t.unified_actions = actions
            t.action_done_at = on.isoformat()
            t.needs_analysis = True
            await self.analyse_topic(db, t, today=today)
            return t

    async def split(self, db: AsyncSession, t: Topic, insight_id: str, today: Optional[date] = None) -> Topic:
        async with self.lock():
            row = await db.get(Rec, insight_id)
            if row is None or row.topic_id != t.id:
                raise KeyError(insight_id)
            members = await self._members(db, t.id)
            if len(members) < 2:
                raise ValueError("This is the only finding in the topic.")
            ctx = await load_context(db)
            sig = signature(row.category, row.zone, row.finding, row.evidence, ctx)
            new = self._new_topic(sig, row)
            new.topic_key = f"{SPLIT_PREFIX}{row.id}"[:255]
            db.add(new)
            await db.flush()
            row.topic_id = new.id
            t.needs_analysis = True
            await db.flush()
            await self.analyse_topic(db, t, today=today, commit=False)
            await self.analyse_topic(db, new, today=today, commit=False)
            await db.commit()
            return new

    async def merge(self, db: AsyncSession, t: Topic, other_id: str, today: Optional[date] = None) -> Topic:
        async with self.lock():
            if other_id == t.id:
                raise ValueError("A topic cannot be merged with itself.")
            o = await db.get(Topic, other_id)
            if o is None:
                raise KeyError(other_id)
            aliases = list(t.alias_keys or [])
            for k in [o.topic_key] + list(o.alias_keys or []):
                if k and k != t.topic_key and k not in aliases and not k.startswith(SPLIT_PREFIX):
                    aliases.append(k)
            t.alias_keys = aliases
            meta, ometa = _meta_copy(t), _topic_meta(o)
            subj = [list(a) for a in meta.get("subject_aliases") or []]
            for a in ometa.get("subject_aliases") or []:
                if a not in subj:
                    subj.append(a)
            meta["subject_aliases"] = subj[:12]
            meta["products"] = sorted(set(meta.get("products") or []) | set(ometa.get("products") or []))
            meta["product_zone_id"] = meta.get("product_zone_id") or ometa.get("product_zone_id")
            t.analysis = meta
            t.zone_id = t.zone_id or o.zone_id
            if not t.action_done_at and o.action_done_at:
                t.action_done_at = o.action_done_at
            if t.status == "resolved" and o.status != "resolved":
                t.status = o.status
            for m in await self._members(db, o.id):
                m.topic_id = t.id
            await db.flush()
            await db.delete(o)
            t.needs_analysis = True
            await db.flush()
            await self.analyse_topic(db, t, today=today, commit=False)
            await db.commit()
            return t

    async def reanalyse(self, db: AsyncSession, t: Topic, *, narrate: bool = False,
                        today: Optional[date] = None) -> Topic:
        async with self.lock():
            return await self.analyse_topic(db, t, today=today, narrate=narrate)


insight_topic_service = InsightTopicService()
