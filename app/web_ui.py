"""Web onboarding wizard: the browser form linked from the Telegram bot.

`/start` sends `{PUBLIC_BASE_URL}/onboard?t=<signed token>` (see web_tokens);
this module renders the six-step form, writes the same onboarding state the
chat flow uses - so a setup can continue on either side - and hands the user
back to Telegram once the profile is live.
"""
from __future__ import annotations

import html
import logging
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, onboarding, platforms, web_tokens
from .store import store

log = logging.getLogger("web")

router = APIRouter()

_LABELS = ["Platform", "Account", "Your voice", "Rhythm", "Schedule", "Media"]
_MEDIA_CHOICES = (
    ("text", "Text", "Send ideas, links, or voice notes."),
    ("image", "Images", "A photo with a caption or idea. For full quality, "
                        "send it as a file."),
    ("video", "Video", "A Google Drive link (best for long videos), a file "
                       "under 20 MB, or a public URL. Add the angle in the "
                       "caption."),
    ("image_video", "Images and video", "A photo with a caption; for video "
                                        "use a Drive link, a file under "
                                        "20 MB, or a public URL."),
)
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TZ_HINTS = ("UTC", "Africa/Lagos", "Africa/Nairobi", "Europe/London",
             "Europe/Paris", "America/New_York", "America/Los_Angeles",
             "Asia/Dubai", "Asia/Kolkata", "Australia/Sydney")
_running: set[int] = set()  # voice analysis in flight (best effort)

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f2f5f8;color:#17222e;font:16px/1.5
 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
.card{max-width:640px;margin:28px auto 64px;background:#fff;
 border:1px solid #dbe3ec;border-radius:16px;padding:26px 30px 34px;
 box-shadow:0 8px 28px rgba(23,34,46,.07)}
h1{font-size:19px;margin:0 0 2px}
h1 span{display:block;font-size:13px;font-weight:400;color:#5b6b7c}
h2{font-size:17px;margin:20px 0 8px}
p{margin:8px 0}
.muted{color:#5b6b7c;font-size:14px}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.err{background:#fdeceb;border:1px solid #f3b7b2;color:#8f2a22;
 border-radius:10px;padding:10px 12px;margin:10px 0}
.note{background:#eef5ff;border:1px solid #c9ddf7;color:#1d4f86;
 border-radius:10px;padding:10px 12px;margin:10px 0}
.progress{display:flex;gap:6px;margin:16px 0 6px;flex-wrap:wrap}
.pl{display:flex;align-items:center;gap:6px;font-size:12px;color:#8195a8;
 padding:4px 8px;border:1px solid #dbe3ec;border-radius:99px}
.pl span{width:16px;height:16px;border-radius:50%;background:#e7edf4;
 text-align:center;font-size:11px;line-height:16px;color:#5b6b7c}
.pl.done{color:#1f7a43;border-color:#bfe3cc}
.pl.done span{background:#d7f0e0;color:#1f7a43}
.pl.now{color:#123;border-color:#8fb7e8;background:#eef5ff}
.pl.now span{background:#2f6fce;color:#fff}
form{margin:0}
label.opt{display:flex;gap:10px;align-items:flex-start;border:1px solid #dbe3ec;
 border-radius:12px;padding:12px 14px;margin:8px 0;cursor:pointer}
label.opt:hover{border-color:#9db8d8;background:#f7fafd}
label.opt input{margin-top:3px}
label.fld{display:block;margin:10px 0;font-size:14px;color:#3a4b5e}
input[type=text],input[type=time],textarea{width:100%;margin-top:4px;
 padding:9px 11px;border:1px solid #c8d4e0;border-radius:10px;font:inherit;
 background:#fff}
textarea{margin:6px 0;resize:vertical}
button{font:inherit;padding:10px 18px;border-radius:10px;border:1px solid #2f6fce;
 background:#2f6fce;color:#fff;cursor:pointer;margin-top:10px}
button:hover{filter:brightness(1.06)}
button.secondary{background:#fff;color:#2f6fce}
a.btn{display:inline-block;padding:10px 18px;border-radius:10px;
 background:#2f6fce;color:#fff;text-decoration:none;margin-top:10px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.days{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.days label{font-size:13px;color:#3a4b5e}
details{margin:8px 0}
summary{cursor:pointer;color:#2f6fce;font-size:14px}
ul.sum{margin:10px 0;padding-left:18px}
ul.sum li{margin:4px 0}
"""


# --- small helpers ---------------------------------------------------------

def _shell(title: str, prog: str, body: str, status: int = 200,
           refresh: int = 0) -> HTMLResponse:
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>{html.escape(title)}</title><style>{_CSS}</style>"
        "</head><body><main class='card'>"
        "<h1>Social posting agent<span>Telegram + Gemini + Composio</span></h1>"
        f"{prog}{body}</main></body></html>")
    return HTMLResponse(page, status_code=status)


def _progress(step: int) -> str:
    cells = []
    for n, label in enumerate(_LABELS, start=1):
        cls = " done" if n < step else (" now" if n == step else "")
        cells.append(f"<div class='pl{cls}'><span>{n}</span>"
                     f"{html.escape(label)}</div>")
    return f"<div class='progress'>{''.join(cells)}</div>"


def _hidden(t: str) -> str:
    return f"<input type='hidden' name='t' value='{html.escape(t)}'>"


def _form(t: str, action: str, fields: str, submit: str) -> str:
    return (f"<form method='post' action='{action}'>{_hidden(t)}{fields}"
            f"<button type='submit'>{html.escape(submit)}</button></form>")


def _err(msg: str) -> str:
    return f"<div class='err'>{html.escape(msg)}</div>" if msg else ""


def _note(msg: str) -> str:
    return f"<div class='note'>{html.escape(msg)}</div>" if msg else ""


def _invalid() -> HTMLResponse:
    return _shell(
        "Link expired", "",
        "<h2>This setup link is invalid or has expired</h2>"
        "<p>Send /start to the Telegram bot to get a fresh setup link.</p>",
        status=400)


def _detect(uid: int) -> list:
    s = store.get_state(uid)
    p = platforms.get(s.platform)
    if not p:
        return []
    try:
        return platforms.detect_accounts(uid, p.slug)
    except Exception:  # noqa: BLE001
        log.exception("account detection failed")
        return []


async def _form_uid(request: Request):
    form = await request.form()
    t = str(form.get("t") or "")
    return web_tokens.verify(t), t, form


# --- connect + analysis flow ------------------------------------------------

def _connect_advance(uid: int) -> tuple[bool, str, str]:
    """Prepare the connect step. Returns (redirect, url, error)."""
    s = store.get_state(uid)
    p = platforms.get(s.platform)
    if not p:
        onboarding._save(uid, onboarding_step="platform")
        return True, "", ""
    try:
        if platforms.is_connected(uid, p.slug):
            decision, _ = onboarding.resolve_accounts(uid, p)
            onboarding._save(uid, onboarding_step=(
                "samples" if decision in ("auto", "skip") else "account"))
            return True, "", ""
        url = s.connect_url or platforms.connect_url(uid, p.slug)
        onboarding._save(uid, connect_url=url, onboarding_step="connect")
        return False, url, ""
    except Exception as e:  # noqa: BLE001
        log.exception("connect start failed")
        return False, s.connect_url, str(e)


def _kickoff_analysis(uid: int, background: BackgroundTasks) -> None:
    if uid in _running:
        return
    _running.add(uid)
    background.add_task(_finish_analysis, uid)


def _finish_analysis(uid: int) -> None:
    try:
        onboarding._finish_samples(uid, uid)
    finally:
        _running.discard(uid)


# --- sections ----------------------------------------------------------------

def _platform_section(s, t: str, error: str = "") -> str:
    opts = []
    for p in platforms.choices():
        checked = " checked" if s.platform == p.slug else ""
        note = (f"<div class='muted'>{html.escape(p.note)}</div>"
                if p.note else "")
        opts.append(
            f"<label class='opt'><input type='radio' name='platform' "
            f"value='{p.slug}'{checked}>"
            f"<div><strong>{html.escape(p.name)}</strong>{note}</div></label>")
    return ("<h2>Step 1 of 6 - Where do you want to post?</h2>"
            + _err(error)
            + _form(t, "/onboard/platform", "".join(opts), "Continue"))


def _connect_section(s, t: str, url: str, error: str = "",
                     notice: str = "") -> str:
    p = platforms.get(s.platform)
    name = p.name if p else s.platform
    note = (f"<p class='muted'>{html.escape(p.note)}</p>"
            if p and p.note else "")
    link = (f"<a class='btn' href='{html.escape(url)}' target='_blank' "
            f"rel='noopener'>Connect {html.escape(name)}</a>") if url else ""
    return (
        f"<h2>Step 2 of 6 - Sign in to {html.escape(name)}</h2>"
        "<p>Open the Connect Link, finish the sign-in (grant posting access), "
        "then come back here and press &ldquo;I&rsquo;ve connected&rdquo;.</p>"
        f"{note}{_note(notice)}{_err(error)}{link}"
        "<div class='row'>"
        f"<form method='post' action='/onboard/connect/check'>{_hidden(t)}"
        "<button type='submit'>I&rsquo;ve connected</button></form>"
        f"<form method='post' action='/onboard/connect/newlink'>{_hidden(t)}"
        "<button class='secondary' type='submit'>Get a new link</button>"
        "</form></div>")


def _account_section(s, t: str, accounts, error: str = "") -> str:
    p = platforms.get(s.platform)
    name = p.name if p else s.platform
    hint = (p.account_hint if p else "") or "account ID"
    fields = []
    if accounts:
        fields.append(f"<p>Detected on your {html.escape(name)} "
                      "connection:</p>")
        for i, a in enumerate(accounts):
            checked = " checked" if i == 0 else ""
            fields.append(
                f"<label class='opt'><input type='radio' name='account' "
                f"value='{html.escape(a.value)}'{checked}>"
                f"<div><strong>{html.escape(a.label)}</strong>"
                f"<div class='mono muted'>{html.escape(a.value)}</div>"
                "</div></label>")
        fields.append(
            "<details><summary>Or enter an ID manually</summary>"
            f"<label class='fld'>{html.escape(hint)}"
            f"<input type='text' name='account_manual' "
            f"placeholder='{html.escape(hint)}'></label></details>")
    else:
        fields.append(
            f"<label class='fld'>{html.escape(hint)}"
            f"<input type='text' name='account' "
            f"value='{html.escape(s.account)}' "
            f"placeholder='{html.escape(hint)}'></label>")
    return (f"<h2>Step 2 of 6 - Which {html.escape(name)} account should I "
            "post to?</h2>"
            + _err(error)
            + _form(t, "/onboard/account", "".join(fields), "Continue"))


def _samples_section(s, t: str, error: str = "") -> str:
    target = onboarding.SAMPLE_TARGET
    boxes = []
    for i in range(max(target, len(s.samples))):
        val = html.escape(s.samples[i]) if i < len(s.samples) else ""
        boxes.append("<textarea name='samples' rows='3' "
                     "placeholder='Paste one past post here...'>"
                     f"{val}</textarea>")
    return (
        f"<h2>Step 3 of 6 - Teach me your voice</h2>"
        f"<p>Paste at least {target} posts you have already written - from "
        "any platform. I learn your tone, rhythm, hooks, and CTAs.</p>"
        f"{_err(error)}"
        f"<form method='post' action='/onboard/samples'>{_hidden(t)}"
        f"<div id='boxes'>{''.join(boxes)}</div>"
        "<button type='button' class='secondary' onclick='addBox()'>"
        "Add another post</button> "
        "<button type='submit'>Learn my voice</button></form>"
        "<script>function addBox(){var d=document.getElementById('boxes');"
        "var x=document.createElement('textarea');x.name='samples';x.rows=3;"
        "x.placeholder='Paste one past post here...';d.appendChild(x);"
        "x.scrollIntoView({block:'center'});x.focus();}</script>")


def _analyzing_section(s) -> str:
    return ("<h2>Learning your voice...</h2>"
            f"<p>Analysing {len(s.samples)} posts with Gemini. This usually "
            "takes 10-30 seconds; this page refreshes by itself.</p>")


def _mode_section(s, t: str, error: str = "") -> str:
    scheduled = " checked" if s.posting_mode == "scheduled" else ""
    on_demand = " checked" if s.posting_mode == "on_demand" else ""
    fields = (
        f"<label class='opt'><input type='radio' name='mode' "
        f"value='scheduled'{scheduled}>"
        "<div><strong>Scheduled</strong><div class='muted'>I draft and "
        "publish at your chosen times.</div></div></label>"
        f"<label class='opt'><input type='radio' name='mode' "
        f"value='on_demand'{on_demand}>"
        "<div><strong>Only when you send material</strong><div class='muted'>"
        "I draft when you give me something to post about, and publish after "
        "your approval.</div></div></label>")
    return ("<h2>Step 4 of 6 - How should posts happen?</h2>"
            + _err(error)
            + _form(t, "/onboard/mode", fields, "Continue"))


def _split_schedule(s):
    parts = (s.schedule or "").split()
    label = parts[0] if parts else "weekdays"
    time_str = parts[1] if len(parts) > 1 else "09:00"
    tz = s.timezone or config.DEFAULT_TIMEZONE
    if label in ("daily", "weekdays", "weekends"):
        return label, time_str, tz, set()
    return "custom", time_str, tz, {d for d in label.split(",") if d in _DAYS}


def _schedule_section(s, t: str, error: str = "", notice: str = "") -> str:
    freq, time_str, tz, sel_days = _split_schedule(s)

    def radio(value: str, label: str) -> str:
        checked = " checked" if freq == value else ""
        return (f"<label class='opt'><input type='radio' name='freq' "
                f"value='{value}'{checked}><div><strong>{label}</strong>"
                "</div></label>")

    days = "".join(
        f"<label><input type='checkbox' name='days' value='{d}'"
        f"{' checked' if d in sel_days else ''}> {d.capitalize()}</label>"
        for d in _DAYS)
    tz_options = "".join(f"<option value='{z}'></option>" for z in _TZ_HINTS)
    fields = (
        radio("daily", "Every day") + radio("weekdays", "Weekdays")
        + radio("weekends", "Weekends") + radio("custom", "Specific days")
        + f"<div class='days'>{days}</div>"
        + "<div class='row'><label class='fld'>Time (24h)"
        + f"<input type='time' name='time' value='{html.escape(time_str)}'>"
        + "</label><label class='fld'>Timezone"
        + f"<input type='text' name='timezone' list='tz' "
        + f"value='{html.escape(tz)}'></label></div>"
        + f"<datalist id='tz'>{tz_options}</datalist>")
    return (f"<h2>Step 5 of 6 - When should I publish?</h2>{_note(notice)}"
            + _err(error)
            + _form(t, "/onboard/schedule", fields, "Save schedule"))


def _media_section(s, t: str, error: str = "") -> str:
    opts = []
    for value, label, blurb in _MEDIA_CHOICES:
        checked = " checked" if s.media_pref == value else ""
        opts.append(
            f"<label class='opt'><input type='radio' name='media' "
            f"value='{value}'{checked}><div><strong>{html.escape(label)}"
            f"</strong><div class='muted'>{html.escape(blurb)}</div></div>"
            "</label>")
    ig = ("<p class='muted'>Instagram needs a public image/video URL to "
          "publish.</p>" if s.platform == "instagram" else "")
    return ("<h2>Step 6 of 6 - What will your posts usually include?</h2>"
            + ig + _err(error)
            + _form(t, "/onboard/media", "".join(opts), "Finish setup"))


def _done_section(s) -> str:
    p = platforms.get(s.platform)
    mode = (f"Scheduled - {html.escape(s.schedule)} ({html.escape(s.timezone)})"
            if s.posting_mode == "scheduled" else "Only when you send material")
    media = {v: label for v, label, _ in _MEDIA_CHOICES}.get(
        s.media_pref, s.media_pref)
    return (
        "<h2>You are all set</h2>"
        "<ul class='sum'>"
        f"<li><b>Platform:</b> {html.escape(p.name if p else s.platform)}</li>"
        f"<li><b>Account:</b> "
        f"{html.escape(s.account_label or s.account or 'not set')}</li>"
        f"<li><b>Voice:</b> learned from {len(s.samples)} posts</li>"
        f"<li><b>Mode:</b> {mode}</li>"
        f"<li><b>Media:</b> {html.escape(media)}</li></ul>"
        "<p>Head back to Telegram - send an idea, photo, video, voice note, "
        "or Google Drive link and I will draft it in your voice for you to "
        "approve. Commands: /status /settings /new</p>")


# --- routes ------------------------------------------------------------------

@router.get("/onboard", response_class=HTMLResponse)
def onboard_page(background: BackgroundTasks, t: str = ""):
    uid = web_tokens.verify(t)
    if uid is None:
        return _invalid()
    s = store.get_state(uid)
    if s.onboarded:
        return _shell("Setup complete", "", _done_section(s))
    step = s.onboarding_step or "platform"

    if step == "connect":
        moved, url, error = _connect_advance(uid)
        if moved:
            return RedirectResponse(f"/onboard?t={t}", status_code=303)
        s = store.get_state(uid)
        return _shell("Connect account", _progress(2),
                      _connect_section(s, t, url, error))
    if step == "analyzing":
        if s.voice_guide:
            onboarding._save(uid, onboarding_step="mode")
            return RedirectResponse(f"/onboard?t={t}", status_code=303)
        _kickoff_analysis(uid, background)
        return _shell("Learning your voice", _progress(3),
                      _analyzing_section(s), refresh=3)
    if step == "account":
        return _shell("Pick account", _progress(2),
                      _account_section(s, t, _detect(uid)))
    if step == "samples":
        return _shell("Your voice", _progress(3), _samples_section(s, t))
    if step == "mode":
        return _shell("Posting rhythm", _progress(4), _mode_section(s, t))
    if step == "schedule":
        return _shell("Schedule", _progress(5), _schedule_section(s, t))
    if step == "media":
        return _shell("Media", _progress(6), _media_section(s, t))
    return _shell("Platform", _progress(1), _platform_section(s, t))


@router.post("/onboard/platform")
async def post_platform(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    if not onboarding.set_platform(uid, str(form.get("platform") or "")):
        return _shell("Platform", _progress(1), _platform_section(
            store.get_state(uid), t, error="Pick one of the platforms."))
    return RedirectResponse(f"/onboard?t={t}", status_code=303)


@router.post("/onboard/connect/check")
async def post_connect_check(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    moved, url, error = _connect_advance(uid)
    if moved:
        return RedirectResponse(f"/onboard?t={t}", status_code=303)
    notice = ("" if error else
              "I still do not see an active connection. Finish the sign-in, "
              "then press the button again.")
    return _shell("Connect account", _progress(2), _connect_section(
        store.get_state(uid), t, url, error=error, notice=notice))


@router.post("/onboard/connect/newlink")
async def post_connect_newlink(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    s = store.get_state(uid)
    p = platforms.get(s.platform)
    if not p:
        onboarding._save(uid, onboarding_step="platform")
        return RedirectResponse(f"/onboard?t={t}", status_code=303)
    try:
        url = platforms.connect_url(uid, p.slug)
        onboarding._save(uid, connect_url=url, onboarding_step="connect")
    except Exception as e:  # noqa: BLE001
        log.exception("new connect link failed")
        return _shell("Connect account", _progress(2), _connect_section(
            s, t, s.connect_url, error=str(e)))
    return RedirectResponse(f"/onboard?t={t}", status_code=303)


@router.post("/onboard/account")
async def post_account(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    value = (str(form.get("account_manual") or "").strip()
             or str(form.get("account") or "").strip())
    ok, hint = onboarding.save_account(uid, value)
    if not ok:
        msg = (f"That does not look like {hint}. Try again." if hint
               else "Pick a platform first.")
        return _shell("Pick account", _progress(2), _account_section(
            store.get_state(uid), t, _detect(uid), error=msg))
    onboarding._save(uid, onboarding_step="samples")
    return RedirectResponse(f"/onboard?t={t}", status_code=303)


@router.post("/onboard/samples")
async def post_samples(request: Request, background: BackgroundTasks):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    n = onboarding.set_samples(uid, form.getlist("samples"))
    if n < onboarding.SAMPLE_TARGET:
        return _shell("Your voice", _progress(3), _samples_section(
            store.get_state(uid), t,
            error=f"I found {n} posts - I need at least "
                  f"{onboarding.SAMPLE_TARGET} to learn your voice."))
    onboarding._save(uid, onboarding_step="analyzing")
    _kickoff_analysis(uid, background)
    return RedirectResponse(f"/onboard?t={t}", status_code=303)


@router.post("/onboard/mode")
async def post_mode(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    if not onboarding.set_mode(uid, str(form.get("mode") or "")):
        return _shell("Posting rhythm", _progress(4), _mode_section(
            store.get_state(uid), t, error="Pick one of the two options."))
    return RedirectResponse(f"/onboard?t={t}", status_code=303)


@router.post("/onboard/schedule")
async def post_schedule(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    s = store.get_state(uid)
    freq = str(form.get("freq") or "")
    time_str = str(form.get("time") or "").strip()
    tz = str(form.get("timezone") or "").strip() or config.DEFAULT_TIMEZONE
    label, error = "", ""
    if freq == "custom":
        picked = [d for d in _DAYS if d in form.getlist("days")]
        label = ",".join(picked)
        if not picked:
            error = "Pick at least one day."
    elif freq in ("daily", "weekdays", "weekends"):
        label = freq
    else:
        error = "Pick how often to post."
    if not error and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_str):
        error = "Use a 24-hour time like 18:00."
    if not error:
        try:
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            error = f"Unknown timezone: {tz}"
    if not error:
        try:
            onboarding.save_schedule(uid, f"{label} {time_str} {tz}")
            return RedirectResponse(f"/onboard?t={t}", status_code=303)
        except ValueError as e:
            error = str(e)
    return _shell("Schedule", _progress(5),
                  _schedule_section(s, t, error=error))


@router.post("/onboard/media")
async def post_media(request: Request):
    uid, t, form = await _form_uid(request)
    if uid is None:
        return _invalid()
    pref = str(form.get("media") or "")
    if pref not in ("text", "image", "video", "image_video"):
        return _shell("Media", _progress(6), _media_section(
            store.get_state(uid), t, error="Pick a media option."))
    s = store.get_state(uid)
    if s.posting_mode == "scheduled" and not s.schedule:
        # A scheduled profile without a schedule string cannot publish.
        onboarding._save(uid, posting_mode="on_demand")
    onboarding._select_media(uid, uid, pref)
    return RedirectResponse(f"/onboard?t={t}", status_code=303)
