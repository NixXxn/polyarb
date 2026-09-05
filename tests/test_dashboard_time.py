from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from arbot.dashboard.data import format_dashboard_ts, server_clock


UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def test_format_aware_iso_stays_utc():
    assert (
        format_dashboard_ts("2026-09-05T14:50:10.527890+00:00", tz=UTC)
        == "2026-09-05 14:50:10 UTC"
    )


def test_format_zulu_and_naive_are_utc_not_local():
    assert format_dashboard_ts("2026-09-05T14:50:10Z", tz=UTC) == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts("2026-09-05 14:50:10", tz=UTC) == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts("2026-09-05T14:50:10", tz=UTC) == "2026-09-05 14:50:10 UTC"


def test_format_offset_converts_to_target_zone():
    assert (
        format_dashboard_ts("2026-09-05T16:50:10+02:00", tz=UTC) == "2026-09-05 14:50:10 UTC"
    )
    assert (
        format_dashboard_ts("2026-09-05T14:50:10+00:00", tz=BERLIN)
        == "2026-09-05 16:50:10 CEST"
    )


def test_format_datetime_objects():
    aware = datetime(2026, 9, 5, 14, 50, 10, tzinfo=UTC)
    naive = datetime(2026, 9, 5, 14, 50, 10)
    offset = datetime(2026, 9, 5, 16, 50, 10, tzinfo=timezone(timedelta(hours=2)))
    assert format_dashboard_ts(aware, tz=UTC) == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts(naive, tz=UTC) == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts(offset, tz=UTC) == "2026-09-05 14:50:10 UTC"


def test_format_passthrough_and_empty():
    assert format_dashboard_ts(None) == ""
    assert format_dashboard_ts("") == ""
    assert format_dashboard_ts("2026-09-05 14:50:10 UTC") == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts("not-a-time") == "not-a-time"


def test_server_clock_uses_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    clock = server_clock(datetime(2026, 9, 5, 14, 50, 10, tzinfo=UTC))
    assert clock["timezone"] == "Europe/Berlin"
    assert clock["timezone_abbr"] == "CEST"
    assert clock["utc_offset_minutes"] == 120
    assert clock["server_time"] == "2026-09-05 16:50:10 CEST"


def test_dashboard_html_shows_server_clock():
    from pathlib import Path

    from arbot.dashboard.app import app

    html = (Path(app.template_folder) / "index.html").read_text()
    assert "Server time" in html
    assert "serverClock" in html
    assert "Times in server timezone" in html
    assert "iso + \"Z\"" in html or "iso + 'Z'" in html or 'iso + "Z"' in html
