"""Products a zone sells reach the findings and the local model's prompt."""

from __future__ import annotations

import io
import json

from app.services import business_analysis_service as bas
from app.services.business_analysis_service import Finding, _detect, attach_products, business_analysis_service
from app.services.retail_metrics_service import ZoneMetrics


def _zones():
    busy = [ZoneMetrics(zone_id=f"z{i}", name=f"Busy {i}", category="AISLE", visits=100) for i in range(3)]
    quiet = ZoneMetrics(zone_id="zq", name="Aisle 2", category="AISLE", visits=5,
                        products=["Chips", "Lollies", "Chocolate"])
    return busy + [quiet]


def test_zone_metrics_carry_products():
    z = ZoneMetrics(zone_id="z", name="A", category="AISLE", products=["Bread"])
    assert z.to_dict()["products"] == ["Bread"]
    assert ZoneMetrics(zone_id="y", name="B", category="AISLE").to_dict()["products"] == []


def test_findings_name_what_the_zone_sells():
    zones = _zones()
    findings = _detect(zones, {})
    attach_products(findings, {z.name: z.products for z in zones})
    layout = next(f for f in findings if f.category == "STORE_LAYOUT" and f.zone == "Aisle 2")
    assert layout.to_dict()["products"] == ["Chips", "Lollies", "Chocolate"]
    store_wide = Finding(category="LOSS_PREVENTION", severity="HIGH", zone="Store-wide", finding="f",
                         root_cause="r", action_item="a", evidence={})
    attach_products([store_wide], {"Aisle 2": ["Chips"]})
    assert store_wide.products == []


def test_narration_prompt_lists_zone_products(monkeypatch):
    sent = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return _Resp(json.dumps({"response": "Summary."}).encode())

    monkeypatch.setattr(business_analysis_service, "select_model", lambda: "qwen-test")
    monkeypatch.setattr(bas.urllib.request, "urlopen", fake_urlopen)
    f = Finding(category="STORE_LAYOUT", severity="MEDIUM", zone="Aisle 2", finding="Aisle 2 saw 5 visits.",
                root_cause="r", action_item="a", evidence={}, products=["Chips", "Lollies"])
    business_analysis_service.narrate([f], {})
    prompt = sent["body"]["prompt"]
    assert '"zone_sells": ["Chips", "Lollies"]' in prompt
    assert "zone_sells" in prompt.split("Findings:")[1]
