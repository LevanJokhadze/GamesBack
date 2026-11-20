"""Submit a post link on the campaign's Content Rewards page with headless Chromium (Playwright), signed in with
the session the user pasted on the Settings page (exported cookies, optionally localStorage).

Content Rewards has no public API, so this does what a person does: open the campaign page, press Submit, paste
the link, tick the rule boxes, press Submit again, and wait for the confirmation. Every step's selector can be
overridden with the `browser_selectors` setting if the page changes. A failed attempt keeps a screenshot (Blob)
so the reason is visible on the dashboard and in the failure email.

The session is saved back after every run (sites refresh their tokens), and a keep-alive opens the page every few
hours so an expired sign-in is noticed, and emailed, long before a post needs it.
"""
import os
import re
import tempfile
import time
from pathlib import Path

from .. import db, vault

DEFAULT_SELECTORS = {
    # button or link that opens the submission form on the campaign page
    "open": r"submit clip|submit content|submit video|submit post|make a submission|new submission|^submit$",
    # field for the post link
    "link_input": 'input[type="url"], input[name*="url" i], input[name*="link" i], input[placeholder*="link" i], '
                  'input[placeholder*="url" i], input[placeholder*="http" i], input[placeholder*="tiktok" i], '
                  'input[placeholder*="youtube" i], input[placeholder*="instagram" i]',
    # final button inside the form
    "confirm": r"^submit|submit clip|submit content|submit video|confirm|send",
    # text that means it worked / failed / we are signed out
    "success": r"submitted|submission received|under review|pending review|in review|thanks|success",
    "error": r"already (been )?submitted|invalid|not allowed|not eligible|error|failed|too late|expired|limit reached",
    "signed_out": r"log ?in|sign ?in|sign up|continue with (google|email|discord)",
}

SESSION_KEY = "browser_session"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


class BrowserError(RuntimeError):
    def __init__(self, message: str, screenshot: str | None = None, signed_out: bool = False):
        super().__init__(message)
        self.screenshot = screenshot
        self.signed_out = signed_out


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def has_session() -> bool:
    return bool(vault.get(SESSION_KEY))


def save_session(raw) -> dict:
    """Accepts a cookie export (list of cookies, e.g. from the Cookie-Editor extension), a Playwright storage state
    ({cookies, origins}), and optionally a localStorage dump ({key: value}) for the site's origin."""
    cookies, origins = [], []
    if isinstance(raw, dict) and "cookies" in raw:
        cookies, origins = raw.get("cookies") or [], raw.get("origins") or []
    elif isinstance(raw, dict) and "cookies_json" in raw:
        cookies = raw.get("cookies_json") or []
        ls = raw.get("local_storage") or {}
        origin = (raw.get("origin") or "https://contentrewards.com").rstrip("/")
        if ls:
            origins = [{"origin": origin, "localStorage": [{"name": k, "value": str(v)} for k, v in ls.items()]}]
    elif isinstance(raw, list):
        cookies = raw
    clean = []
    for c in cookies:
        if not c.get("name") or c.get("value") is None or not c.get("domain"):
            continue
        same = (c.get("sameSite") or "Lax").lower()
        clean.append({"name": c["name"], "value": str(c["value"]), "domain": c["domain"], "path": c.get("path") or "/",
                      "expires": float(c.get("expirationDate") or c.get("expires") or -1),
                      "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure", True)),
                      "sameSite": {"no_restriction": "None", "none": "None", "strict": "Strict"}.get(same, "Lax")})
    if not clean and not origins:
        raise ValueError("No cookies found. Paste the JSON exported from the browser while signed in.")
    state = {"cookies": clean, "origins": origins}
    vault.put(SESSION_KEY, state)
    return {"cookies": len(clean), "origins": len(origins)}


def _sel() -> dict:
    return {**DEFAULT_SELECTORS, **(db.settings().get("browser_selectors") or {})}


def _shot(page, name: str) -> str | None:
    try:
        from . import blob
        p = Path(tempfile.gettempdir()) / f"{name}-{int(time.time())}.png"
        page.screenshot(path=str(p), full_page=True)
        return blob.upload(p, f"submit-shots/{p.name}", "image/png")
    except Exception:
        return None


def _visible(loc):
    for i in range(min(loc.count(), 12)):
        el = loc.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            pass
    return None


def _button(scope, pattern: str):
    rx = re.compile(pattern, re.I)
    for role in ("button", "link"):
        el = _visible(scope.get_by_role(role, name=rx))
        if el:
            return el
    return None


def _text(page) -> str:
    try:
        return page.inner_text("body", timeout=3000)
    except Exception:
        return ""


def run(campaign_url: str, link: str | None, platform: str | None = None, dry_run: bool = False) -> dict:
    """Submit `link` (or with dry_run only check that the session works and the form opens)."""
    from playwright.sync_api import TimeoutError as PwTimeout, sync_playwright

    state = vault.get(SESSION_KEY)
    if not state:
        raise BrowserError("No browser session saved: paste your Content Rewards cookies on the Settings page",
                           signed_out=True)
    sel = _sel()
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"],
                               executable_path=os.environ.get("CHROMIUM_PATH") or None)
        try:
            ctx = b.new_context(storage_state=state, user_agent=UA, viewport={"width": 1280, "height": 900},
                                locale="en-US", timezone_id="America/New_York")
            page = ctx.new_page()
            page.goto(campaign_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PwTimeout:
                pass
            opener = _button(page, sel["open"])
            if not opener:
                signed_out = bool(re.search(sel["signed_out"], _text(page)[:4000], re.I)) or "login" in page.url
                shot = _shot(page, "no-submit-button")
                raise BrowserError("Signed out of Content Rewards: paste fresh cookies on the Settings page" if signed_out
                                   else "Could not find the Submit button on the campaign page", shot, signed_out)
            opener.click()
            field = None
            for _ in range(20):
                field = _visible(page.locator(sel["link_input"]))
                if field:
                    break
                page.wait_for_timeout(500)
            if not field:
                raise BrowserError("The submit form opened but has no link field", _shot(page, "no-link-field"))
            result = {"url": page.url, "form": True}
            if dry_run:
                result["screenshot"] = _shot(page, "dry-run")
                vault.put(SESSION_KEY, ctx.storage_state())
                return result
            field.fill(link)
            field.press("Tab")
            # choose the platform if the form asks for it
            if platform:
                for s in page.locator("select").all()[:4]:
                    try:
                        if s.is_visible():
                            opts = [o.strip() for o in s.locator("option").all_inner_texts()]
                            hit = next((o for o in opts if platform.lower() in o.lower()), None)
                            if hit:
                                s.select_option(label=hit)
                    except Exception:
                        pass
            # rule / terms confirmations in the form
            for cb in page.locator('[role="dialog"] input[type="checkbox"], form input[type="checkbox"], '
                                   '[role="dialog"] [role="checkbox"]').all()[:8]:
                try:
                    if cb.is_visible() and not cb.is_checked():
                        cb.check(timeout=2000)
                except Exception:
                    pass
            scope = page.locator('[role="dialog"]').last if page.locator('[role="dialog"]').count() else page
            confirm = _button(scope, sel["confirm"])
            if not confirm:
                raise BrowserError("Could not find the final Submit button in the form", _shot(page, "no-confirm"))
            for _ in range(10):
                if confirm.is_enabled():
                    break
                page.wait_for_timeout(500)
            before = _text(page)
            confirm.click()
            ok_rx, err_rx = re.compile(sel["success"], re.I), re.compile(sel["error"], re.I)
            for _ in range(40):
                page.wait_for_timeout(500)
                now = _text(page)
                new = now.replace(before, "") if before and before in now else now
                if err_rx.search(new) and not ok_rx.search(new):
                    m = err_rx.search(new)
                    line = next((ln for ln in new.splitlines() if m.group(0) in ln), m.group(0))
                    raise BrowserError(f"Content Rewards refused it: {line.strip()[:200]}", _shot(page, "refused"))
                if ok_rx.search(new) or not _visible(page.locator(sel["link_input"])):
                    vault.put(SESSION_KEY, ctx.storage_state())
                    return {"url": page.url, "confirmed": bool(ok_rx.search(new))}
            raise BrowserError("Pressed Submit but saw no confirmation within 20 s", _shot(page, "no-confirmation"))
        finally:
            b.close()
