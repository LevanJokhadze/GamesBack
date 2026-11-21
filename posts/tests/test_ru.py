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
