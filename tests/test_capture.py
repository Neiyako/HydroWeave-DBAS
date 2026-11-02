from __future__ import annotations

import json
from pathlib import Path

from dbas.fetch.capture import CaptureConfig, JsonlWriter, ObservedCdsClient, redact, request_hash
from dbas.fetch.capture_report import summarize


def test_redact_and_hash_stable():
    value = redact({"Authorization": "Bearer secret", "year": "2024", "nested": {"api_key": "x"}})
    assert value["Authorization"] == "<redacted>"
    assert value["year"] == "2024"
    assert request_hash("d", {"year": "2024"}) == request_hash("d", {"year": "2024"})


def test_observed_client_writes_redacted_events(tmp_path):
    class Fake:
        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"nc")
            return {"ok": True}

    config = CaptureConfig(tmp_path)
    client = ObservedCdsClient(Fake(), JsonlWriter(config), task_id="task-1")
    target = tmp_path / "x.part"
    client.retrieve("dataset", {"variable": ["clwc"], "api_key": "secret"}, str(target))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    text = (tmp_path / "events.jsonl").read_text()
    assert "secret" not in text
    assert rows[0]["request_hash"]
    assert summarize(tmp_path)["requests"] == 1
    assert summarize(tmp_path)["responses"] == 1
    assert summarize(tmp_path)["cloud_water_coverage"]["clwc"]["captured"] is True
