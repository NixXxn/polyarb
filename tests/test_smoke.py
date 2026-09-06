from __future__ import annotations

from arbot.config import load_settings, settings_public_dict, update_settings


def test_load_settings():
    s = load_settings()
    assert s.arbitrage.max_pair_cost < 1.0
    assert s.arbitrage.exit_ladder_prices
    assert s.live.clob_host


def test_settings_roundtrip(tmp_path):
    src = load_settings().settings_path.read_text()
    path = tmp_path / "settings.yaml"
    path.write_text(src)
    updated = update_settings(
        {"arbitrage": {"position_usd": 55, "max_open_pairs": 3}},
        path=path,
    )
    assert updated.arbitrage.position_usd == 55
    assert updated.arbitrage.max_open_pairs == 3
    pub = settings_public_dict(updated)
    assert "editable" in pub
    assert pub["arbitrage"]["position_usd"] == 55


def test_analyze_import():
    from arbot.strategy import analyze_arbitrage, arbitrage_exits
    assert callable(analyze_arbitrage)
    assert callable(arbitrage_exits)


def test_dashboard_index_renders():
    from pathlib import Path

    from arbot.dashboard.app import app

    template = Path(app.template_folder) / "index.html"
    assert template.is_file(), template
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Arbot" in response.data
    assert b"Server time" in response.data
    assert b"CSV trades" in response.data
    assert b"Reset paper account" in response.data
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["ok"] is True


def test_csv_export(tmp_path, monkeypatch):
    monkeypatch.setenv("ARBOT_DATA_DIR", str(tmp_path))
    from arbot.dashboard.app import app

    client = app.test_client()
    activity = client.get("/api/export/activity.csv")
    assert activity.status_code == 200
    assert b"ts," in activity.data
    trades = client.get("/api/export/trades.csv")
    assert trades.status_code == 200
    assert b"created_at" in trades.data


def test_reset_balances_sets_starting_cash(tmp_path, monkeypatch):
    src = load_settings().settings_path.read_text()
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(src)
    monkeypatch.setenv("ARBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARBOT_SETTINGS_PATH", str(settings_path))
    from arbot.dashboard.app import app

    client = app.test_client()
    res = client.post(
        "/api/reset-balances",
        json={"balance": 250, "mode": "paper"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["accounts"][0]["cash"] == 250
    assert body["accounts"][0]["starting_balance"] == 250
    assert body["settings"]["arbitrage"]["starting_balance"] == 250
    bad = client.post("/api/reset-balances", json={"balance": 0, "mode": "paper"})
    assert bad.status_code == 400


