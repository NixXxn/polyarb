from __future__ import annotations

from datetime import datetime, timezone, timedelta

from arbot.dashboard.data import DISPLAY_TIMEZONE, format_dashboard_ts


def test_format_aware_iso_stays_utc():
    assert format_dashboard_ts("2026-09-05T14:50:10.527890+00:00") == "2026-09-05 14:50:10 UTC"


def test_format_zulu_and_naive_are_utc_not_local():
    assert format_dashboard_ts("2026-09-05T14:50:10Z") == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts("2026-09-05 14:50:10") == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts("2026-09-05T14:50:10") == "2026-09-05 14:50:10 UTC"


def test_format_offset_converts_to_utc():
    assert format_dashboard_ts("2026-09-05T16:50:10+02:00") == "2026-09-05 14:50:10 UTC"


def test_format_datetime_objects():
    aware = datetime(2026, 9, 5, 14, 50, 10, tzinfo=timezone.utc)
    naive = datetime(2026, 9, 5, 14, 50, 10)
    offset = datetime(2026, 9, 5, 16, 50, 10, tzinfo=timezone(timedelta(hours=2)))
    assert format_dashboard_ts(aware) == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts(naive) == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts(offset) == "2026-09-05 14:50:10 UTC"


def test_format_passthrough_and_empty():
    assert format_dashboard_ts(None) == ""
    assert format_dashboard_ts("") == ""
    assert format_dashboard_ts("2026-09-05 14:50:10 UTC") == "2026-09-05 14:50:10 UTC"
    assert format_dashboard_ts("not-a-time") == "not-a-time"
    assert DISPLAY_TIMEZONE == "UTC"


def test_dashboard_html_labels_utc():
    from pathlib import Path

    from arbot.dashboard.app import app

    html = (Path(app.template_folder) / "index.html").read_text()
    assert "Times in UTC" in html
    assert "When (UTC)" in html
    assert "getUTCFullYear" in html
    assert "getUTCHours" in html
    assert "iso + \"Z\"" in html or "iso + 'Z'" in html or 'iso + "Z"' in html
