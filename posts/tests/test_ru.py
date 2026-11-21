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
    s = {**db.DEFAULT_SETTINGS, "geo_slots": {"RU": {"tz": "Europe/Moscow", "slots": ["20:00", "15:00"]}}}
    assert publish.slot_plan(db.DEFAULT_SETTINGS, "RU")[0].key == "Europe/Moscow"
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


def test_a_number_as_clip_title_never_becomes_the_post_title(monkeypatch):
    """The Evelone clip titled '2848481' went out as 'twitch: evelone2004 2848481 #evelone' with the wrapper twice
    in the caption."""
    from app import config
    t = titles._fallback("2848481", "Evelone", "ru")
    assert "2848481" not in t["title"] and "Evelone" in t["title"] and t["hook"] and t["needs_review"]
    assert titles._fallback("", "Boeshi")["title"].startswith("Boeshi")
    c = {"title_template": "twitch: evelone2004 {title} #evelone"}
    caption = "twitch: evelone2004 {hook} #evelone".replace("{hook}", t["hook"])
    assert render.apply_title_template(c, t["title"]).count("twitch:") == 1 and caption.count("twitch:") == 1

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    asked = []

    def empty_then_written(prompt):
        asked.append(prompt)
        return {"title": ""} if len(asked) == 1 else {"title": "Эвелон встретил старого знакомого", "hook": "Узнали? 👀",
                                                      "screen": "ОН ВЕРНУЛСЯ", "payoff": "A"}
    monkeypatch.setattr(titles, "_ask", empty_then_written)
    t = titles.make("2848481", "нико беллик", "Evelone", cands=[{"t": 3.0, "text": "нико"}], language="ru")
    assert t["title"] == "Эвелон встретил старого знакомого" and t["payoff"] == 0 and t["needs_review"] and not t["why"]
    assert titles.THIN in asked[1] and titles.THIN not in asked[0]

    def broken(prompt):
        raise RuntimeError("HTTP 529 overloaded")
    monkeypatch.setattr(titles, "_ask", broken)
    t = titles.make("2848481", "", "Evelone", language="ru")
    assert "2848481" not in t["title"] and "HTTP 529" in t["why"]
    t = titles.make("Эвелон не ожидал такого от чата", "", "Эвелон", language="ru")
    assert t["title"] == "Эвелон не ожидал такого от чата" and t["why"]


def test_a_geo_has_its_own_daily_limits():
    s = {"max_posts_per_day": 2, "max_per_day_by_service": {"youtube": 1},
         "geo_slots": {"RU": {"tz": "Europe/Moscow", "slots": ["12:00"], "per_day": {"tiktok": 4, "youtube": 2}}}}
    assert publish.day_cap("tiktok", s, "RU") == 4 and publish.day_cap("youtube", s, "ru") == 2
    assert publish.day_cap("instagram", s, "RU") == 2       # not listed for the geo: the general limit
    assert publish.day_cap("tiktok", s) == 2 and publish.day_cap("youtube", s, "US") == 1
    s["geo_slots"]["RU"]["per_day"] = {"default": 3, "youtube": "x"}
    assert publish.day_cap("tiktok", s, "RU") == 3 and publish.day_cap("youtube", s, "RU") == 1


def test_nudity_check_verdicts(monkeypatch, tmp_path):
    from app import config
    from app.integrations import safety
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(config, "NUDITY_CHECK", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert safety.check(src)["verdict"] == "unsure"          # no key: a person looks, nothing is auto-approved
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(safety, "frames", lambda src: [(0.0, b"jpg"), (1.0, b"jpg")])
    monkeypatch.setattr(safety, "_ask", lambda shots: {"verdict": "Nudity", "at": [1, "x"], "what": "a naked character"})
    assert safety.check(src) == {"verdict": "nudity", "what": "a naked character", "at": [1]}
    monkeypatch.setattr(safety, "_ask", lambda shots: {"verdict": "revealing", "at": [], "what": "bikini"})
    assert safety.check(src)["verdict"] == "revealing"
    monkeypatch.setattr(safety, "_ask", lambda shots: {"verdict": "maybe"})
    assert safety.check(src)["verdict"] == "unsure"

    def refused(shots):
        raise RuntimeError("the model declined to look at the frames")
    monkeypatch.setattr(safety, "_ask", refused)
    out = safety.check(src)
    assert out["verdict"] == "unsure" and "declined" in out["what"]
    monkeypatch.setattr(config, "NUDITY_CHECK", False)
    assert safety.check(src)["verdict"] == "off"
