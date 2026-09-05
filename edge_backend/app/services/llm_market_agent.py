"""LLM Market Reasoning & Retail Optimization Agent.

Analyzes customer choices, visual product interactions (dwell, reaches, put-backs),
and POS transaction data to generate automated supermarket merchandising,
pricing, and layout optimization decisions using Ollama with dynamic discovery
and clean edge deterministic fallback.
"""

import datetime
import json
import logging
import re
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

logger = logging.getLogger("LLMMarketAgent")

# Model prioritization hierarchy and non-generative keyword exclusions
MODEL_PREFERENCE_ORDER = ["ornith-brain", "ornith", "qwen", "llama", "lfm"]
EMBEDDING_KEYWORDS = ["embed", "bert", "bge", "nomic-embed"]


def is_embedding_model(model_info: Dict[str, Any]) -> bool:
    """Detects whether a model is an embedding-only model rather than generative."""
    name = str(model_info.get("name", "")).lower()
    details = model_info.get("details", {}) or {}
    family = str(details.get("family", "")).lower()
    capabilities = model_info.get("capabilities", []) or []

    if "embedding" in capabilities and "completion" not in capabilities:
        return True
    if any(k in name for k in EMBEDDING_KEYWORDS):
        return True
    if family == "bert":
        return True
    return False


def select_best_model(models: List[Dict[str, Any]]) -> Optional[str]:
    """Selects the best generative LLM using priority order:
    ornith-brain > ornith > qwen > llama > lfm > first non-embedding model.
    """
    generative_models = [m for m in models if not is_embedding_model(m)]
    if not generative_models:
        return None

    # Priority matching
    for pref in MODEL_PREFERENCE_ORDER:
        for m in generative_models:
            name = str(m.get("name", ""))
            if pref in name.lower():
                return name

    # Fallback to first non-embedding model found
    return generative_models[0].get("name")


def check_ollama_status(ollama_url: Optional[str] = None) -> Dict[str, Any]:
    """Dynamically queries Ollama /api/tags (timeout=2.5s) to discover service
    status and available generative models.
    """
    url = (ollama_url or "http://localhost:11434").rstrip("/")
    tags_endpoint = f"{url}/api/tags"

    try:
        req = urllib.request.Request(tags_endpoint, headers={"User-Agent": "Edge-AI-CCTV/1.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                payload = json.loads(body)
                raw_models = payload.get("models", [])
                available_models = [m.get("name") for m in raw_models if m.get("name")]
                active_model = select_best_model(raw_models)

                if active_model:
                    return {
                        "status": "online",
                        "ollama_active": True,
                        "active_model": active_model,
                        "available_models": available_models,
                        "warning": None
                    }
                else:
                    return {
                        "status": "warning",
                        "ollama_active": False,
                        "active_model": None,
                        "available_models": available_models,
                        "warning": "Ollama service offline or model unavailable. Using deterministic edge rule engine."
                    }
    except Exception as exc:
        logger.debug("Dynamic Ollama health probe failed: %s", exc)

    return {
        "status": "offline",
        "ollama_active": False,
        "active_model": None,
        "available_models": [],
        "warning": "Ollama service offline or model unavailable. Using deterministic edge rule engine."
    }


class MarketOptimizationItem(BaseModel):
    id: str
    category: str  # "MERCHANDISING", "PRICING", "RESTOCKING", "LAYOUT_EXPERIMENT"
    priority: str  # "CRITICAL", "HIGH", "MEDIUM"
    target_product_or_zone: str
    sku_id: Optional[str] = None
    observed_behavior: str
    empirical_evidence: Dict[str, Any]
    root_cause_hypothesis: str
    automated_recommendation: str
    expected_business_impact: str
    status: str = "PENDING_REVIEW"


class LLMMarketOptimizationResponse(BaseModel):
    store_id: str
    analysis_timestamp: str
    total_products_monitored: int
    optimizations: List[MarketOptimizationItem]
    executive_strategic_takeaway: str
    model_used: Optional[str] = "deterministic-edge-rules"
    ollama_status: Optional[str] = "offline"
    warning: Optional[str] = None
    ollama_active: Optional[bool] = False


class LLMMarketReasoningAgent:
    """Combines LLM dynamic Ollama discovery with deterministic edge heuristics fallback."""

    def __init__(self, ollama_url: Optional[str] = None, generate_timeout: float = 6.0):
        self.ollama_url = (ollama_url or "http://localhost:11434").rstrip("/")
        self.generate_timeout = generate_timeout

    def check_ollama_status(self) -> Dict[str, Any]:
        """Inspects status and discovered model via dynamic /api/tags check."""
        return check_ollama_status(self.ollama_url)

    def _generate_deterministic_items(
        self,
        product_stats: List[Dict[str, Any]],
        hourly_traffic_forecast: Optional[List[Dict[str, Any]]] = None
    ) -> List[MarketOptimizationItem]:
        """Edge heuristic reasoning engine: high-precision deterministic rules."""
        items: List[MarketOptimizationItem] = []

        for p in product_stats:
            sku_id = p.get("sku_id", "SKU")
            name = p.get("product_name", "Product")
            price = p.get("price", 0.0)
            touches = p.get("touches", 0)
            picks = p.get("picks", 0)
            put_backs = p.get("put_backs", 0)
            sales = p.get("pos_sales", 0)
            friction = p.get("friction_index", 0.0)
            tier = p.get("shelf_tier", "EYE_LEVEL")
            avg_dwell = p.get("avg_dwell_sec", 0.0)

            # Pattern 1: High Inspection Dwell with High Put-Back (Price / Confusion)
            if touches >= 20 and friction >= 50.0:
                items.append(MarketOptimizationItem(
                    id=f"opt_frict_{sku_id}",
                    category="PRICING",
                    priority="HIGH",
                    target_product_or_zone=name,
                    sku_id=sku_id,
                    observed_behavior=f"High customer interest ({touches} touches) with {friction}% put-back rate.",
                    empirical_evidence={"touches": touches, "put_backs": put_backs, "friction_pct": friction, "price": price},
                    root_cause_hypothesis=f"Shoppers inspect packaging for {avg_dwell}s and reject at the current ${price:.2f} price point.",
                    automated_recommendation=f"Deploy an introductory 15% promotional tag (${price * 0.85:.2f}) or add prominent certification callout.",
                    expected_business_impact=f"Estimated +22% conversion lift, converting ~{int(put_backs * 0.35)} abandoned touches into purchases weekly."
                ))

            # Pattern 2: Hidden Gem on Bottom Shelf (High Intent, Low Exposure)
            if tier == "BOTTOM" and picks >= 10:
                items.append(MarketOptimizationItem(
                    id=f"opt_tier_{sku_id}",
                    category="MERCHANDISING",
                    priority="HIGH",
                    target_product_or_zone=name,
                    sku_id=sku_id,
                    observed_behavior=f"Strong customer intent despite low-visibility bottom shelf placement.",
                    empirical_evidence={"shelf_tier": tier, "picks": picks, "sales": sales},
                    root_cause_hypothesis="Customers intentionally bend down to retrieve this staple; prime eye-level real estate is underutilized.",
                    automated_recommendation=f"Relocate {name} to Eye-Level or Top Shelf Tier. Shift slower-moving private label SKU to bottom.",
                    expected_business_impact="Projected +38% increase in category footfall and +$450/month incremental revenue."
                ))

            # Pattern 3: Endcap Promotional Star
            if tier == "ENDCAP" and touches >= 50:
                items.append(MarketOptimizationItem(
                    id=f"opt_endcap_{sku_id}",
                    category="LAYOUT_EXPERIMENT",
                    priority="MEDIUM",
                    target_product_or_zone=name,
                    sku_id=sku_id,
                    observed_behavior="High attraction velocity on promotional endcap display.",
                    empirical_evidence={"shelf_tier": tier, "touches": touches, "picks": picks},
                    root_cause_hypothesis="Prime line-of-sight drives impulse grabs.",
                    automated_recommendation=f"Cross-merchandise complementary category item (e.g. Dips / Cold Drinks) adjacent to {name}.",
                    expected_business_impact="Boost multi-item basket size by an estimated 1.4 units per customer transaction."
                ))

        # Restocking Alert based on traffic forecast
        if hourly_traffic_forecast:
            peak_hours = [h["hour"] for h in hourly_traffic_forecast if h.get("is_peak_hour")]
            if peak_hours:
                items.append(MarketOptimizationItem(
                    id="opt_restock_peak",
                    category="RESTOCKING",
                    priority="CRITICAL",
                    target_product_or_zone="Aisle 3 & Dairy Coolers",
                    sku_id=None,
                    observed_behavior=f"Predicted peak traffic surge between {peak_hours[0]} and {peak_hours[-1]}.",
                    empirical_evidence={"peak_hours": peak_hours},
                    root_cause_hypothesis="High shelf pick velocity during rush period depletes front-facing inventory, causing phantom stockouts.",
                    automated_recommendation=f"Schedule floor staff replenishment cycle at 4:30 PM, 30 minutes prior to {peak_hours[0]} surge.",
                    expected_business_impact="Prevents visual out-of-stock lost sales estimated at $680 during peak traffic window."
                ))

        return items

    def _query_ollama(
        self,
        model_name: str,
        store_id: str,
        product_stats: List[Dict[str, Any]],
        items: List[MarketOptimizationItem],
        timeout: float
    ) -> Optional[str]:
        """Queries Ollama /api/generate to synthesize an executive strategic takeaway."""
        generate_url = f"{self.ollama_url}/api/generate"
        summary_context = [
            {"product": it.target_product_or_zone, "action": it.automated_recommendation}
            for it in items[:4]
        ]
        prompt = (
            f"Supermarket Store '{store_id}' Edge AI Analysis:\n"
            f"Active recommendations: {json.dumps(summary_context)}\n"
            "Provide a crisp, 2-sentence executive retail strategy summary synthesizing these findings. Do not include thinking tags."
        )

        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": 120,
                "temperature": 0.2
            }
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            generate_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Edge-AI-CCTV/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                res_json = json.loads(resp.read().decode("utf-8"))
                text = (res_json.get("response") or "").strip()
                if not text:
                    # Check thinking if response is empty
                    thinking = (res_json.get("thinking") or "").strip()
                    if thinking:
                        # Extract non-meta text if available
                        clean_think = re.sub(r"^(The user wants|Let me|I need to).*", "", thinking, flags=re.MULTILINE).strip()
                        if clean_think:
                            text = clean_think[:250]
                if text:
                    return text
        return None

    def generate_optimizations(
        self,
        store_id: str,
        product_stats: List[Dict[str, Any]],
        hourly_traffic_forecast: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None
    ) -> LLMMarketOptimizationResponse:
        """Evaluates product metrics and generates actionable merchandising & pricing recommendations."""
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Step 1: Generate deterministic heuristics
        items = self._generate_deterministic_items(product_stats, hourly_traffic_forecast)
        default_summary = (
            f"Evaluated {len(product_stats)} product zones. "
            f"Identified {len(items)} actionable optimizations across pricing elasticity, "
            f"shelf tier re-balancing, and pre-rush restocking."
        )

        # Step 2: Dynamic Ollama discovery
        ollama_info = self.check_ollama_status()
        active_model = ollama_info.get("active_model")
        ollama_active = ollama_info.get("ollama_active", False)

        if not ollama_active or not active_model:
            return LLMMarketOptimizationResponse(
                store_id=store_id,
                analysis_timestamp=now_str,
                total_products_monitored=len(product_stats),
                optimizations=items,
                executive_strategic_takeaway=default_summary,
                model_used="deterministic-edge-rules",
                ollama_status="offline",
                warning="Ollama service offline or model unavailable. Using deterministic edge rule engine.",
                ollama_active=False
            )

        # Step 3: Ollama is online with discovered model - attempt generation
        req_timeout = timeout if timeout is not None else self.generate_timeout
        try:
            llm_summary = self._query_ollama(
                model_name=active_model,
                store_id=store_id,
                product_stats=product_stats,
                items=items,
                timeout=req_timeout
            )
            strategic_takeaway = llm_summary if llm_summary else default_summary

            return LLMMarketOptimizationResponse(
                store_id=store_id,
                analysis_timestamp=now_str,
                total_products_monitored=len(product_stats),
                optimizations=items,
                executive_strategic_takeaway=strategic_takeaway,
                model_used=active_model,
                ollama_status="online",
                warning=None,
                ollama_active=True
            )
        except Exception as exc:
            logger.warning("Ollama generation query failed or timed out: %s. Falling back to edge heuristics.", exc)
            return LLMMarketOptimizationResponse(
                store_id=store_id,
                analysis_timestamp=now_str,
                total_products_monitored=len(product_stats),
                optimizations=items,
                executive_strategic_takeaway=default_summary,
                model_used="deterministic-edge-rules",
                ollama_status="offline",
                warning="Ollama service offline or model unavailable. Using deterministic edge rule engine.",
                ollama_active=False
            )


llm_market_agent = LLMMarketReasoningAgent()
