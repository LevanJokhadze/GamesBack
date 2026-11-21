"""Trends worker: parsing, filters and the split-screen verdicts (no network, no database).
Run: cd posts && python -m pytest tests/test_trends.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402
from app.integrations import youtube  # noqa: E402
from app.workers import trends  # noqa: E402


def v(i, views=100_000, dur=30, channel="someclipper", tag="evelone"):
    return {"platform": "youtube", "id": f"id{i}", "title": f"ЭВЕЛОН УГАР {i} #evelone", "channel": channel,
            "views": views, "duration": dur, "published_at": "2026-11-21T10:00:00Z", "thumb": f"https://t/{i}.jpg",
            "url": f"https://www.youtube.com/shorts/id{i}", "tag": tag}


def test_iso_durations():
    assert youtube.seconds("PT59S") == 59 and youtube.seconds("PT1M5S") == 65 and youtube.seconds("P0D") == 0
    assert youtube.seconds("") == 0


def test_eligible():
    c = {"trend_min_views": 50_000}
    own = {"mrtemuka"}
    assert trends.eligible(v(1), c, own)
    assert not trends.eligible(v(1, views=10_000), c, own)
    assert not trends.eligible(v(1, dur=90), c, own)          # longer than a minute
    assert not trends.eligible(v(1, dur=4), c, own)
    assert not trends.eligible(v(1, channel="MrTemuka"), c, own)   # one of our own accounts


def test_keywords_drop_hashtags():
    assert trends.keywords("ЭВЕЛОН УГАР на стриме #evelone #shorts") == "ЭВЕЛОН УГАР стриме"


def _judge(monkeypatch, answer):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", None)
    monkeypatch.setattr(trends, "_jpeg", lambda url: b"\xff\xd8jpeg")
    monkeypatch.setattr(trends, "ask", lambda cand, main, others: answer)
    pool = [v(1, views=900_000), v(2), v(3), v(4, tag="buster")]
    return trends.judge(pool[0], pool, {"language": "ru"})


def test_judge_verdicts(monkeypatch):
    assert _judge(monkeypatch, {"candidate_split": True})[0] == "split"
    verdict, why = _judge(monkeypatch, {"candidate_split": False, "split_same_moment": [2], "same_moment": [1, 2]})
    assert verdict == "taken" and "id3" in why                 # others are id2, id3 (same tag only)
    verdict, why = _judge(monkeypatch, {"candidate_split": False, "split_same_moment": [], "same_moment": [1]})
    assert verdict == "new" and "1 other" in why
    assert _judge(monkeypatch, {"split_same_moment": [7]})[0] == "new"   # an index out of range is ignored


def test_judge_without_a_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    assert trends.judge(v(1), [v(1)], {})[0] == "unchecked"
