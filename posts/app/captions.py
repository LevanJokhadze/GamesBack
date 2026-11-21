"""Caption clean-up, applied to every caption the pipeline writes or the user saves.

Hashtags are written in lower case. Platforms treat them case-insensitively, but TikTok's composer offers its own
(lower-case) tag while a mixed-case one is pasted or typed and can leave the rest of the word behind: "#KreekCraft"
came out as "#kreekcraftCraft". A lower-case tag matches the suggestion exactly. Tags already broken that way
(a repeated tail) are repaired, and a tag that appears twice is kept once.
"""
import re

TAG = re.compile(r"#(\w+)")


def _fix_tag(body: str) -> str:
    b = body.lower()
    for k in range(len(b) // 2, 2, -1):  # "kreekcraftcraft" -> "kreekcraft"
        if b.endswith(b[-k:] * 2):
            b = b[:-k]
            break
    return b


def clean(text: str | None) -> str:
    if not text:
        return text or ""
    seen: set[str] = set()

    def sub(m):
        tag = _fix_tag(m.group(1))
        if tag in seen:
            return ""
        seen.add(tag)
        return "#" + tag

    out = TAG.sub(sub, text)
    return re.sub(r"[ \t]{2,}", " ", out).strip()
