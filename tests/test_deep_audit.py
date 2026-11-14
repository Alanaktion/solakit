"""Regressions from the November 13 runtime audit."""

import asyncio
import json

import pytest
from test_channels import FakeAdapter, configured
from test_execution import completion, model_list, setup

from solakit.channels import Channels
from solakit.core import structured
from solakit.core.archival import archive_session
from solakit.core.config import Delivery
from solakit.core.store import Store


@pytest.mark.parametrize(
    "schema",
    [
        {"type": []},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"x": {"type": "unknown"}}},
        {"type": "object", "properties": {"x": None}},
        {"type": "object", "required": "title"},
        {"type": "array", "items": []},
        {"type": "string", "enum": "allowed"},
        {"type": "object", "additionalProperties": "false"},
    ],
)
def test_malformed_output_schemas_fail_at_validation(schema):
    with pytest.raises(ValueError, match="output_schema"):
        structured.check_schema(schema)


def test_render_preserves_data_as_the_whole_value():
    value = {"data": "nested", "title": "Report"}
    assert structured.render("{{ title }}: {{ data.data }}", value) == "Report: nested"
    assert structured.render("{{ data|join(', ') }}", ["a", "b"]) == "a, b"


@pytest.mark.parametrize(
    "sample",
    [
        [],
        "broken",
        {},
        {"bytes_per_token": 0, "samples": 1},
        {"bytes_per_token": float("nan"), "samples": 1},
        {"bytes_per_token": float("inf"), "samples": 1},
        {"bytes_per_token": "4", "samples": 1},
        {"bytes_per_token": 4, "samples": "broken"},
        {"bytes_per_token": 4, "samples": True},
        {"bytes_per_token": 4, "samples": -1},
    ],
)
def test_damaged_calibration_is_discarded_and_relearned(config, sample):
    store = Store(config.paths.data)
    try:
        path = store.root / "calibration.json"
        path.write_text(json.dumps({"m": sample, "other": {"kept": True}}))
        assert store.calibration("m") is None
        assert store.record_calibration("m", 400, 100) == {
            "bytes_per_token": 4.0,
            "samples": 1,
        }
        assert json.loads(path.read_text())["other"] == {"kept": True}
    finally:
        store.close()


def test_delivery_reads_archived_run_without_scanning_session(config, monkeypatch):
    configured(config)

    def model(request):
        return (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "stored final")
        )

    execution, adapter = setup(config, model), FakeAdapter()
    channels = Channels(config, execution, {"discord": adapter})

    def full_scan(*args):
        pytest.fail("Delivery must use the run's indexed message addresses")

    async def exercise():
        run = await execution.submit("s", "task")
        archive_session(execution.store, "s")
        monkeypatch.setattr(execution.store, "all_messages", full_scan)
        await channels.deliver(run, Delivery(to="one"))
        assert run.delivery_status == "delivered"
        assert adapter.sent[0][1].text == "stored final"
        await execution.close()

    asyncio.run(exercise())
