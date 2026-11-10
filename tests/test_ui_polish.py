"""Presentation polish: status tones, compact chat actions and destructive styling."""

from fastapi.testclient import TestClient
from test_ui import sign_in
from test_ui_ux import seed

from solakit.main import create_app


def test_run_status_tones_and_stacked_runs_table(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "toned", 2)
        page = client.get("/settings/evidence/runs")
        assert 'class="run-status run-status-good">completed</span>' in page.text
        assert 'class="data-table stacked-table runs-table"' in page.text
        assert 'data-label="Status"' in page.text
        detail = client.get("/settings/evidence/runs/turn-0001")
        assert "run-status-good" in detail.text
        empty = client.get("/settings/evidence/runs?status=failed")
        assert "Clear filters" in empty.text


def test_chat_actions_have_compact_menu_and_jump_control(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        seed(app.state.store, "menu", 1)
        page = client.get("/sessions/menu")
        # One Actions menu serves desktop and mobile.
        assert page.text.count('href="/sessions/menu/rename"') == 1
        assert 'class="session-menu"' in page.text
        assert 'id="jump-latest"' in page.text
        assert page.text.count('href="/sessions/menu/compact"') == 1
