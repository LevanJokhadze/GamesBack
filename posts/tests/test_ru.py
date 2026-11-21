"""Russian campaigns: Cyrillic fallback titles, title templates and geo routing. Run: cd posts && python -m pytest tests"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.integrations import titles  # noqa: E402
from app.workers import publish, render  # noqa: E402


def test_cyrillic_fallback_keeps_the_clip_title():
    t = titles._fallback("Эвелон не ожидал такого от чата", "Эвелон", "ru")
    assert t["title"] == "Эвелон не ожидал такого от чата"
    assert t["screen"] == "ЭВЕЛОН НЕ ОЖИДАЛ ТАКОГО ОТ ЧАТА"
    assert titles._fallback("лол", "Строго", "ru")["screen"] == "ЖДИ КОНЦОВКУ…"
    assert titles._fallback("He trusted the GPS", "Boeshi")["title"] == "He trusted the GPS (Boeshi, GTA 6)"


def test_title_template_fits_youtube():
    c = {"title_template": "twitch: evelone2004 {title} #evelone"}
    assert render.apply_title_template(c, "Эвелон в шоке") == "twitch: evelone2004 Эвелон в шоке #evelone"
    long = render.apply_title_template(c, "а" * 200)
    assert len(long) <= 100 and long.startswith("twitch: evelone2004 ") and long.endswith("#evelone")
    assert render.apply_title_template({}, "Plain") == "Plain"
    assert render.apply_title_template(c, "") == ""


def test_geo_routing():
    targeted = {"RU"}
    assert publish.geo_ok("RU", "RU", targeted)
    assert not publish.geo_ok("RU", "US", targeted)
    assert not publish.geo_ok("RU", None, targeted)      # unlinked Buffer channel = default US
    assert publish.geo_ok(None, "US", targeted)
    assert publish.geo_ok(None, None, targeted)
    assert not publish.geo_ok(None, "RU", targeted)      # US campaigns stay off the Russian accounts
    assert publish.geo_ok("", "US", targeted)


def test_geo_slots_use_the_geo_timezone():
    from datetime import datetime, timezone
    from app import db
    from app.workers import manual
    s = dict(db.DEFAULT_SETTINGS)
    tz, slots = publish.slot_plan(s, "ru")
    assert tz.key == "Europe/Moscow" and slots == ["15:00", "20:00"]
    assert publish.slot_plan(s, "US")[0] is publish.ET and publish.slot_plan(s, None)[1] == ["12:00", "15:00"]
    assert publish.slot_plan({"geo_slots": {"RU": {"tz": "Nope/Zone", "slots": ["10:00"]}}}, "RU")[0] is publish.ET
    assert publish.zone_label(tz) == "Moscow" and publish.zone_label(publish.ET) == "US Eastern"
    now = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)
    ru = [t for t in manual.slot_times(s, now, "RU") if t.date() == now.date()]
    assert [t.strftime("%H:%M") for t in ru] == ["12:00", "17:00"]            # 15:00 and 20:00 Moscow, in UTC
    us = [t for t in manual.slot_times(s, now) if t.date() == now.date()]
    assert [t.strftime("%H:%M") for t in us] == ["17:00", "20:00"]            # 12:00 and 15:00 US Eastern, in UTC
