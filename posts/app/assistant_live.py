"""Jarvis voice sessions: the browser's WebSocket relayed to Gemini Live, with the tools Gemini calls.

The same design as the Personal Dashboard's work mode: the browser trades its login for a one-use ticket
(POST /api/assistant/session) and opens /api/assistant/live; this server opens the Gemini Live session with the
instructions, the live snapshot of the pipeline and the tools, passes audio and text both ways, and runs the tools.
Credentials never reach the browser. Tools that change the dashboard on screen (cards, modals, pages, links) are
sent to the browser as {"ui": ...} messages; the rest run here against the database.

Messages to the browser: {"ready"}, {"audio": b64 pcm 24 kHz}, {"you": text}, {"ai": text}, {"interrupted"},
{"turnComplete"}, {"tool": name}, {"ui": {...}}, {"changed": [tool names]}, {"handle": resumption handle},
{"goAway"}. Close codes: 4001 Gemini ended a working session (reconnect with the handle), 4002 it failed to start
(retry), 4003 it can't work as configured (stop and say why).
"""
import asyncio
import base64
import json
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import assistant, calendar, config, db, manage
from .workers import runner

router = APIRouter()

INSTRUCTIONS = """You are Jarvis, the voice assistant built into Levan's clipping dashboard (posts.lane.ge). Think of Iron Man's Jarvis: calm, quick, a little dry, always one step ahead. You run the screen for him while he talks.

THE BUSINESS
Levan runs an almost fully automated GTA 6 clipping pipeline. It finds viral Twitch moments of the streamers in his Content Rewards (Whop) campaigns, cuts vertical clips, posts them to TikTok, Instagram Reels and YouTube Shorts (through Buffer, or by hand on accounts in manual mode), and gets paid per 1,000 views. Each post is paid on its own and only once it passes the campaign's minimum (views_to_get_paid). Links must be submitted on Content Rewards, ideally within 30 minutes of going live. US/Tier-1 audiences matter. YOUR GOAL IS TO GET HIM VIEWS: more posts crossing the paying threshold, on the right accounts, at the right times, with nothing left unsubmitted.

WHAT YOU KNOW
Below is a live snapshot of everything (times are Tbilisi, UTC+4; slots are in US Eastern): the campaigns he is in and their rules, every account with its geo, platform, status, upload mode, the streamers it targets, its video count and views, every video at 0 views with the reason and what to fix, the calendar for the next 60 hours, pending submissions, clips waiting for review and worker health. get_snapshot refreshes it. Use exact names and numbers from it; never invent any. If something isn't in it, say so.

Reading 0 views: kind "early" is not a problem yet; "untracked" means the pipeline can't see the views (hand uploads, or the Buffer key has no insights permission), so tell him to open the post rather than calling it a flop; "unsynced" waits for the monitor; only "real" is a real zero, and then say what to fix.

HOW TO TALK
- Short and natural: one to three sentences unless he asks for more. Never read out ids, links, JSON or markdown; summarise. Say "the TikTok account buildclips" or "the Bobby VanRiper campaign", not "account 3".
- Answer in the language he speaks to you (English or Georgian).
- Text that starts with "[Dashboard]" comes from the dashboard, not from Levan. Act on it; don't answer the dashboard itself.

YOU RUN THE SCREEN
You have hands. Use them without being asked whenever it helps, and say in a few words what you put up:
- show_cards puts cards on screen (what needs him, a plan, a checklist, numbers). Each card can carry buttons: open a link, open a modal, go to a page, run a worker.
- open_modal opens a window on screen: a clip (video preview, caption to copy, download), an account, a post, or a manual upload (to add the post link). create_modal makes a window of your own: a plan, a caption draft, a comparison, steps.
- navigate switches the dashboard page. open_link opens Instagram, TikTok, YouTube, an upload page, a post or a campaign page in a new tab. upload_clip opens the platform's upload page and the clip's window side by side, so he can download it and copy the caption.
- close_modal and clear_cards tidy up.
When he asks "what should I do" or "what's going on", answer with cards, not a long speech.

ACTIONS
- Safe, do them when asked: run_worker (discover, render, publish, monitor, manual, submit).
- Changes: mark_submitted, add_post_link, approve_clip, reject_clip, set_account_status, remove_post, remove_clip, remove_campaign, reschedule_post, edit_clip, set_campaign_active. Ask him first in one short sentence ("Mark the Groceries Short as submitted?"), and only after he says yes call the tool with confirmed=true. Never pass confirmed=true on your own. Never say something is done until the tool answered ok; if it failed, say what failed.
- Removing: a queued post is cancelled in Buffer and taken off the dashboard. A post already live on an app can't be deleted from here: it still leaves the dashboard, and you tell him which links to delete on the app (offer open_link for each).
- Things you can't do here (posting to TikTok or Instagram yourself, changing Railway variables, logging into accounts): say so and open the page he needs instead.
- Never say you did something unless the tool call just returned ok.

WHEN A SESSION STARTS
The dashboard has already put the most important cards on screen and tells you which. Greet him in one short sentence and say the two or three things that matter most for views right now, then wait."""


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, **({"required": required} if required else {})}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
ACTION = _obj({"label": S, "url": {**S, "description": "open this https link in a new tab"},
               "page": {"type": "string", "enum": list(assistant.PAGES)},
               "modal_kind": {"type": "string", "enum": ["clip", "account", "post", "upload"]}, "modal_id": I,
               "worker": {"type": "string", "enum": list(assistant.WORKERS)}}, ["label"])
CARD = _obj({"title": S, "body": S, "tone": {"type": "string", "enum": ["urgent", "warn", "info", "good"]},
             "actions": {"type": "array", "items": ACTION}}, ["title"])
CONFIRMED = {**B, "description": "true only after Levan said yes to this exact action"}

TOOLS = [
    # reads
    {"name": "get_snapshot", "description": "Refresh the live picture: campaigns, accounts, videos and views, 0-view videos, calendar, submissions, review queue, workers. section narrows it.",
     "parameters_json_schema": _obj({"section": {"type": "string", "enum": ["all", "campaigns", "accounts", "videos", "zero_view_videos",
                                                                                 "pending_submissions", "calendar_next_60h", "review_queue",
                                                                                 "clips_ready", "pipeline", "totals"]}})},
    {"name": "get_calendar", "description": "Posting calendar (manual uploads due, Buffer posts) for the coming days, Tbilisi time.",
     "parameters_json_schema": _obj({"days": {**I, "description": "1 to 14, default 3"}})},
    {"name": "get_details", "description": "Everything about one clip, account, post or manual upload (without showing it on screen).",
     "parameters_json_schema": _obj({"kind": {"type": "string", "enum": ["clip", "account", "post", "upload"]}, "id": I}, ["kind", "id"])},
    {"name": "recent_events", "description": "The pipeline's event log (newest first): what the workers did and what failed.",
     "parameters_json_schema": _obj({"errors_only": B, "limit": I})},
    # safe action
    {"name": "run_worker", "description": "Run a worker now: discover (find moments), render (cut clips), publish (schedule to Buffer), monitor (sync posts and stats), manual (upload reminders), submit (Content Rewards).",
     "parameters_json_schema": _obj({"name": {"type": "string", "enum": list(assistant.WORKERS)}}, ["name"])},
    # changes (confirmed)
    {"name": "mark_submitted", "description": "Mark a live post as submitted on Content Rewards (or not). Ask first.",
     "parameters_json_schema": _obj({"post_id": I, "submitted": B, "confirmed": CONFIRMED}, ["post_id", "confirmed"])},
    {"name": "add_post_link", "description": "Record the link of a clip uploaded by hand (a manual upload), so it counts and gets submitted. Ask first.",
     "parameters_json_schema": _obj({"upload_id": I, "link": S, "confirmed": CONFIRMED}, ["upload_id", "link", "confirmed"])},
    {"name": "approve_clip", "description": "Approve a clip waiting for review so it gets posted. Ask first.",
     "parameters_json_schema": _obj({"clip_id": I, "confirmed": CONFIRMED}, ["clip_id", "confirmed"])},
    {"name": "reject_clip", "description": "Reject a clip so it is never posted. Ask first.",
     "parameters_json_schema": _obj({"clip_id": I, "confirmed": CONFIRMED}, ["clip_id", "confirmed"])},
    {"name": "set_account_status", "description": "Change an account's status (warming, active, paused, banned, retired). Ask first.",
     "parameters_json_schema": _obj({"account_id": I, "status": {"type": "string", "enum": ["warming", "active", "paused", "banned", "retired"]},
                                     "confirmed": CONFIRMED}, ["account_id", "status", "confirmed"])},
    {"name": "remove_post", "description": "Remove one post: cancels it in Buffer if it hasn't gone out, and takes it off the dashboard (posts, calendar, totals). A post already live on TikTok/Instagram/YouTube can't be taken down from here: it is still removed from the dashboard, and the answer gives its link so he can delete it on the app. Ask first.",
     "parameters_json_schema": _obj({"post_id": I, "confirmed": CONFIRMED}, ["post_id", "confirmed"])},
    {"name": "remove_clip", "description": "Remove a clip with all its posts (cancelled in Buffer where still queued) and its upload reminders, from everywhere on the dashboard. Ask first.",
     "parameters_json_schema": _obj({"clip_id": I, "confirmed": CONFIRMED}, ["clip_id", "confirmed"])},
    {"name": "remove_campaign", "description": "Remove a whole campaign (e.g. a streamer we stopped clipping): its clips, posts (queued ones cancelled in Buffer) and upload reminders, and accounts stop targeting its streamer. Use get_snapshot for campaign ids. Ask first, naming the campaign and how many posts go.",
     "parameters_json_schema": _obj({"campaign_id": I, "confirmed": CONFIRMED}, ["campaign_id", "confirmed"])},
    {"name": "reschedule_post", "description": "Move a queued post to another time (also in Buffer). due_at is ISO 8601 with its offset, e.g. 2026-11-22T21:00:00+04:00 for 21:00 Tbilisi. Ask first.",
     "parameters_json_schema": _obj({"post_id": I, "due_at": S, "confirmed": CONFIRMED}, ["post_id", "due_at", "confirmed"])},
    {"name": "edit_clip", "description": "Change a clip's title or caption (for posts not made yet). Ask first, reading the new text back.",
     "parameters_json_schema": _obj({"clip_id": I, "title": S, "caption": S, "confirmed": CONFIRMED}, ["clip_id", "confirmed"])},
    {"name": "set_campaign_active", "description": "Turn a campaign on or off (off: no new clips are cut or posted for it; its posts stay). Ask first.",
     "parameters_json_schema": _obj({"campaign_id": I, "active": B, "confirmed": CONFIRMED}, ["campaign_id", "active", "confirmed"])},
    # on screen
    {"name": "show_cards", "description": "Put cards on the dashboard (they stack on the right). replace=true clears earlier cards first.",
     "parameters_json_schema": _obj({"cards": {"type": "array", "items": CARD}, "replace": B}, ["cards"])},
    {"name": "clear_cards", "description": "Remove the cards from the screen."},
    {"name": "open_modal", "description": "Open a window on screen for a clip (video, caption, download), account, post or manual upload (add its link).",
     "parameters_json_schema": _obj({"kind": {"type": "string", "enum": ["clip", "account", "post", "upload"]}, "id": I}, ["kind", "id"])},
    {"name": "create_modal", "description": "Open a window of your own: a plan, steps, a caption draft, a comparison. text is plain text; items are bullet points; actions are buttons.",
     "parameters_json_schema": _obj({"title": S, "text": S, "items": {"type": "array", "items": S},
                                     "copy_text": {**S, "description": "text he can copy with one press (e.g. a caption)"},
                                     "actions": {"type": "array", "items": ACTION}}, ["title"])},
    {"name": "close_modal", "description": "Close the open window."},
    {"name": "navigate", "description": "Switch the dashboard page.",
     "parameters_json_schema": _obj({"page": {"type": "string", "enum": list(assistant.PAGES)}}, ["page"])},
    {"name": "open_link", "description": "Open a link in a new tab: Instagram, TikTok, YouTube, an upload page, a post, a profile, a campaign page.",
     "parameters_json_schema": _obj({"url": S, "label": S}, ["url"])},
    {"name": "upload_clip", "description": "Help him upload a clip by hand: opens the platform's upload page and the clip's window (download and caption).",
     "parameters_json_schema": _obj({"clip_id": I, "platform": {"type": "string", "enum": ["tiktok", "instagram", "youtube"]}}, ["clip_id", "platform"])},
]
UI_TOOLS = {"show_cards", "clear_cards", "open_modal", "create_modal", "close_modal", "navigate", "open_link", "upload_clip"}
WRITE_TOOLS = {"run_worker", "mark_submitted", "add_post_link", "approve_clip", "reject_clip", "set_account_status",
               "remove_post", "remove_clip", "remove_campaign", "reschedule_post", "edit_clip", "set_campaign_active"}


class NeedsConfirm(Exception):
    pass


def _confirm(args: dict, what: str):
    if args.get("confirmed") is not True:
        raise NeedsConfirm(f"Not done: ask Levan first ({what}?) and call again with confirmed=true once he says yes.")


def _https(url: str | None) -> str:
    url = (url or "").strip()
    if not re.match(r"^https://[^\s]+$", url):
        raise ValueError("only https links can be opened")
    return url


def _action(a: dict) -> dict:
    """A card or modal button from the model's flat form to the dashboard's."""
    out = {"label": str(a.get("label") or "Open")[:40]}
    if a.get("url"):
        out["url"] = _https(a["url"])
    elif a.get("page") in assistant.PAGES:
        out["page"] = a["page"]
    elif a.get("modal_kind") and a.get("modal_id"):
        out["modal"] = {"kind": a["modal_kind"], "id": int(a["modal_id"])}
    elif a.get("worker") in assistant.WORKERS:
        out["worker"] = a["worker"]
    return out


def run_tool(name: str, args: dict) -> tuple[object, dict | None]:
    """Runs one tool. Returns (answer for Gemini, message for the browser or None). Blocking (database)."""
    if name == "get_snapshot":
        snap = assistant.snapshot()
        sec = args.get("section") or "all"
        return assistant._plain(snap if sec == "all" else {sec: snap.get(sec)}), None
    if name == "get_calendar":
        days = max(1, min(int(args.get("days") or 3), 14))
        cal = calendar.build(assistant.datetime.now(assistant.TBILISI).date(), days)
        keep = ("kind", "status", "day", "time", "time_et", "platform", "handle", "account_id", "upload_id", "post_id",
                "clip_id", "title", "link")
        return assistant._plain({"slots_local": cal["slots_local"], "events": [{k: e.get(k) for k in keep} for e in cal["events"]]}), None
    if name == "get_details":
        return assistant._plain(assistant.modal_data(args["kind"], int(args["id"]))), None
    if name == "recent_events":
        lim = max(1, min(int(args.get("limit") or 25), 80))
        rows = db.q("SELECT ts, level, worker, message FROM events " + ("WHERE level IN ('error','warn') " if args.get("errors_only") else "")
                    + "ORDER BY id DESC LIMIT %s", (lim,))
        return assistant._plain(rows), None
    if name == "run_worker":
        if not runner.trigger(args.get("name", "")):
            raise ValueError("unknown worker")
        db.log("assistant", f"Jarvis ran the {args['name']} worker")
        return {"ok": True, "note": "started; results show in the event log in a minute"}, None
    if name == "mark_submitted":
        sub = args.get("submitted", True) is not False
        _confirm(args, "mark it " + ("submitted" if sub else "not submitted"))
        p = db.one("SELECT id FROM posts WHERE id=%s", (int(args["post_id"]),))
        if not p:
            raise ValueError("post not found")
        db.q("""UPDATE posts SET submitted=%s, submit_status=%s, submitted_at=CASE WHEN %s THEN COALESCE(submitted_at, now()) END,
                  submit_next_at=NULL WHERE id=%s""", (sub, "manual" if sub else None, sub, p["id"]))
        db.log("assistant", f"Jarvis marked post #{p['id']} " + ("submitted" if sub else "not submitted"))
        return {"ok": True}, None
    if name == "add_post_link":
        _confirm(args, "save that link")
        from . import accounts
        accounts.upload_done(int(args["upload_id"]), accounts.UploadDone(link=_https(args.get("link"))))
        return {"ok": True}, None
    if name in ("approve_clip", "reject_clip"):
        _confirm(args, name.replace("_clip", "") + " the clip")
        cid = int(args["clip_id"])
        if name == "approve_clip":
            r = db.one("UPDATE clips SET status='approved', approved_at=now() WHERE id=%s AND status IN ('review','rejected') RETURNING id", (cid,))
            if not r:
                raise ValueError("that clip isn't waiting for review")
            runner.trigger("publish")
        else:
            db.q("UPDATE clips SET status='rejected' WHERE id=%s AND status IN ('review','approved')", (cid,))
        db.log("assistant", f"Jarvis {name.split('_')[0]}d clip #{cid}")
        return {"ok": True}, None
    if name == "set_account_status":
        _confirm(args, f"set the account to {args.get('status')}")
        if args.get("status") not in ("warming", "active", "paused", "banned", "retired"):
            raise ValueError("unknown status")
        r = db.one("UPDATE accounts SET status=%s, updated_at=now() WHERE id=%s RETURNING handle", (args["status"], int(args["account_id"])))
        if not r:
            raise ValueError("account not found")
        db.log("assistant", f"Jarvis set @{r['handle']} to {args['status']}")
        return {"ok": True}, None
    if name in ("remove_post", "remove_clip", "remove_campaign"):
        kind = name.split("_")[1]
        oid = int(args[f"{kind}_id"])
        _confirm(args, f"remove {kind} #{oid}")
        try:
            out = getattr(manage, name)(oid, who="assistant")
        except LookupError as e:
            raise ValueError(str(e))
        return assistant._plain(out), None
    if name == "reschedule_post":
        _confirm(args, "move the post")
        out = manage.reschedule_post(int(args["post_id"]), assistant.datetime.fromisoformat(str(args["due_at"]).replace("Z", "+00:00")),
                                     who="assistant")
        return out, None
    if name == "edit_clip":
        _confirm(args, "change the clip")
        from . import captions
        cid = int(args["clip_id"])
        if not db.one("SELECT 1 AS x FROM clips WHERE id=%s", (cid,)):
            raise ValueError("clip not found")
        if args.get("caption"):
            db.q("UPDATE clips SET caption=%s WHERE id=%s", (captions.clean(str(args["caption"])), cid))
        if args.get("title"):
            db.q("UPDATE clips SET title=%s WHERE id=%s", (str(args["title"])[:200], cid))
        db.log("assistant", f"Jarvis edited clip #{cid}")
        return {"ok": True}, None
    if name == "set_campaign_active":
        on = bool(args.get("active"))
        _confirm(args, "turn the campaign " + ("on" if on else "off"))
        r = db.one("UPDATE campaigns SET active=%s WHERE id=%s RETURNING name", (on, int(args["campaign_id"])))
        if not r:
            raise ValueError("campaign not found")
        db.log("assistant", f"Jarvis turned {r['name']} " + ("on" if on else "off"))
        return {"ok": True}, None
    # on screen
    if name == "show_cards":
        cards = [{"id": f"ai-{i}-{abs(hash(c.get('title', ''))) % 10**6}", "tone": c.get("tone") or "info",
                  "title": str(c.get("title", ""))[:120], "body": str(c.get("body") or "")[:600],
                  "actions": [_action(a) for a in (c.get("actions") or [])[:3]]} for i, c in enumerate(args.get("cards") or [])]
        return {"ok": True, "shown": len(cards)}, {"ui": {"type": "cards", "cards": cards, "replace": bool(args.get("replace"))}}
    if name == "clear_cards":
        return {"ok": True}, {"ui": {"type": "clear_cards"}}
    if name == "open_modal":
        data = assistant._plain(assistant.modal_data(args["kind"], int(args["id"])))
        return {"ok": True, "shown": args["kind"]}, {"ui": {"type": "modal", "kind": args["kind"], "data": data}}
    if name == "create_modal":
        m = {"title": str(args.get("title", ""))[:120], "text": str(args.get("text") or "")[:3000],
             "items": [str(x)[:300] for x in (args.get("items") or [])[:20]], "copy_text": args.get("copy_text"),
             "actions": [_action(a) for a in (args.get("actions") or [])[:4]]}
        return {"ok": True}, {"ui": {"type": "modal", "kind": "custom", "data": m}}
    if name == "close_modal":
        return {"ok": True}, {"ui": {"type": "close_modal"}}
    if name == "navigate":
        if args.get("page") not in assistant.PAGES:
            raise ValueError("unknown page")
        return {"ok": True}, {"ui": {"type": "navigate", "page": args["page"]}}
    if name == "open_link":
        url = _https(args.get("url"))
        return {"ok": True, "note": "opened, or shown as a button if the browser blocked the new tab"}, \
            {"ui": {"type": "open", "url": url, "label": args.get("label") or url}}
    if name == "upload_clip":
        from . import notify
        data = assistant._plain(assistant.modal_data("clip", int(args["clip_id"])))
        url = notify.UPLOAD_PAGE.get(args.get("platform"), "")
        return {"ok": True}, {"ui": {"type": "upload", "url": url, "platform": args.get("platform"), "data": data}}
    raise ValueError(f"unknown tool {name}")


# --- the Gemini side ------------------------------------------------------------------------------------------

class NotConfigured(Exception):
    pass


def _credentials():
    raw = (config.GOOGLE_CREDENTIALS_JSON or "").strip()
    if not raw:
        return None
    texts = [raw]
    try:
        texts.append(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf8", "ignore"))
    except ValueError:
        pass
    for text in texts:
        try:
            c = json.loads(text)
        except ValueError:
            continue
        if isinstance(c, dict) and c.get("client_email") and c.get("private_key"):
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_info(
                c, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    raise NotConfigured("GOOGLE_CREDENTIALS_JSON is not a service account key: paste the whole JSON key file")


_client = None


def client():
    global _client
    if _client:
        return _client
    from google import genai
    if config.GOOGLE_CLOUD_PROJECT:
        _client = genai.Client(vertexai=True, project=config.GOOGLE_CLOUD_PROJECT, location=config.GOOGLE_CLOUD_LOCATION,
                               credentials=_credentials())
    elif config.GEMINI_API_KEY:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    else:
        raise NotConfigured("Jarvis needs GEMINI_API_KEY (or GOOGLE_CLOUD_PROJECT + GOOGLE_CREDENTIALS_JSON) in Railway")
    return _client


def live_config(memory: str | None, handle: str | None):
    from google.genai import types
    cfg = {
        "response_modalities": ["AUDIO"],
        "system_instruction": INSTRUCTIONS + (f"\n\nLIVE SNAPSHOT (JSON)\n{memory}" if memory else ""),
        "tools": [{"function_declarations": TOOLS}],
        "session_resumption": {"handle": handle} if handle else {},
        "context_window_compression": {"sliding_window": {}},
        "input_audio_transcription": {},
        "output_audio_transcription": {},
    }
    if config.GEMINI_VOICE:
        cfg["speech_config"] = {"voice_config": {"prebuilt_voice_config": {"voice_name": config.GEMINI_VOICE}}}
    return types.LiveConnectConfig(**cfg)


FATAL = re.compile(r"credential|invalid_grant|permission|unauthori[sz]ed|forbidden|not found|not configured|api key|billing|\b40[134]\b", re.I)


def _clip(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    while len(s.encode()) > 120:
        s = s[:-1]
    return s


@router.websocket("/api/assistant/live")
async def live(ws: WebSocket, ticket: str = ""):
    if not assistant.take_ticket(ticket):
        await ws.close(code=4003, reason="login expired, open Jarvis again")
        return
    await ws.accept()
    try:
        start = (await ws.receive_json()).get("start") or {}
    except Exception:
        return
    handle = start.get("handle") if isinstance(start.get("handle"), str) else None
    ready = False
    try:
        memory = None if handle else await asyncio.to_thread(lambda: assistant.dumps(assistant.snapshot()))
        from google.genai import types
        async with client().aio.live.connect(model=config.GEMINI_LIVE_MODEL, config=live_config(memory, handle)) as session:
            ready = True
            await ws.send_json({"ready": True, "resumed": bool(handle)})
            db.log("assistant", "Jarvis session started" + (" (resumed)" if handle else ""))

            async def browser_to_gemini():
                while True:
                    m = await ws.receive_json()
                    if m.get("audio"):
                        await session.send_realtime_input(audio=types.Blob(data=base64.b64decode(m["audio"]), mime_type="audio/pcm;rate=16000"))
                    elif m.get("audioEnd"):
                        await session.send_realtime_input(audio_stream_end=True)
                    elif isinstance(m.get("text"), str):
                        await session.send_client_content(turns=[{"role": "user", "parts": [{"text": m["text"][:20000]}]}],
                                                          turn_complete=m.get("reply", True) is not False)

            async def tools(calls):
                responses, changed = [], []
                for fc in calls:
                    args = dict(fc.args or {})
                    try:
                        out, ui = await asyncio.to_thread(run_tool, fc.name, args)
                        if ui:
                            await ws.send_json(ui)
                        if fc.name in WRITE_TOOLS:
                            changed.append(fc.name)
                        resp = {"output": out}
                    except NeedsConfirm as e:
                        resp = {"error": str(e)}
                    except Exception as e:  # the model gets the error as the answer and can tell Levan
                        detail = getattr(e, "detail", None) or str(e)
                        db.log("assistant", f"Jarvis tool {fc.name} failed: {detail}", "warn")
                        resp = {"error": str(detail)}
                    responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=resp))
                await session.send_tool_response(function_responses=responses)
                if changed:
                    await ws.send_json({"changed": changed})

            async def gemini_to_browser():
                while True:
                    async for msg in session.receive():
                        if msg.tool_call and msg.tool_call.function_calls:
                            for fc in msg.tool_call.function_calls:
                                await ws.send_json({"tool": fc.name})
                            asyncio.create_task(tools(msg.tool_call.function_calls))
                        if msg.session_resumption_update and msg.session_resumption_update.resumable and msg.session_resumption_update.new_handle:
                            await ws.send_json({"handle": msg.session_resumption_update.new_handle})
                        if msg.go_away:
                            await ws.send_json({"goAway": True})
                        c = msg.server_content
                        if not c:
                            continue
                        for part in (c.model_turn.parts if c.model_turn and c.model_turn.parts else []):
                            if part.inline_data and part.inline_data.data and (part.inline_data.mime_type or "").startswith("audio/"):
                                await ws.send_json({"audio": base64.b64encode(part.inline_data.data).decode()})
                        if c.interrupted:
                            await ws.send_json({"interrupted": True})
                        if c.input_transcription and c.input_transcription.text:
                            await ws.send_json({"you": c.input_transcription.text})
                        if c.output_transcription and c.output_transcription.text:
                            await ws.send_json({"ai": c.output_transcription.text})
                        if c.turn_complete:
                            await ws.send_json({"turnComplete": True})

            keepalive_task = asyncio.create_task(_keepalive(ws))
            tasks = [asyncio.create_task(browser_to_gemini()), asyncio.create_task(gemini_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            keepalive_task.cancel()
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                    raise t.exception()
            await _close(ws, 4001, "Gemini ended the session")
    except WebSocketDisconnect:
        return
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        db.log("assistant", f"Jarvis session {'ended' if ready else 'could not start'}: {reason}", "warn")
        code = 4001 if ready else 4003 if isinstance(e, NotConfigured) or FATAL.search(reason) else 4002
        await _close(ws, code, reason)


async def _keepalive(ws: WebSocket):
    while True:  # keeps proxies from closing a quiet socket
        await asyncio.sleep(25)
        await ws.send_json({"keepalive": True})


async def _close(ws: WebSocket, code: int, reason: str):
    try:
        await ws.close(code=code, reason=_clip(reason))
    except Exception:
        pass
