"""Home Assistant bridge (spec 12, ``ha_bridge``): "На ТВ" calls a script of the user's
own Home Assistant over its REST API, with no agent in between.

MIH calls ``script.<name>`` with the channel and up to MIH_HA_STREAMS stream URLs as
variables and waits for the script to finish: it ran = ``started``, it raised =
``failed``. The script decides how the TV plays the channel. MIH never changes the
Home Assistant config; the long-lived token lives only in the server's environment.
"""

from __future__ import annotations

import re

import httpx

from mih.config import Settings

# What each answer of Home Assistant means for the command.
_HTTP_ERRORS = {401: "ha_unauthorized", 403: "ha_unauthorized", 404: "ha_script_not_found"}


# What a Home Assistant message may carry that must not reach a browser: addresses,
# bearer tokens, key=value secrets and long token-like strings.
_SECRETS = [
    (re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"),
    (re.compile(r"\bbearer\s+\S+", re.I), "Bearer <token>"),
    (
        re.compile(r"\b(token|password|passwd|secret|api_key|apikey|key|auth)\s*[=:]\s*\S+", re.I),
        r"\1=<hidden>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"[A-Za-z0-9_\-]{24,}(?:\.[A-Za-z0-9_\-]{8,}){0,2}"), "<hidden>"),
]
# Home Assistant names a notify action it does not have: the TV's Companion app lost
# its registration (a new login registers notify.mobile_app_<new name>).
_NOTIFY_MISSING = re.compile(
    r"notify\.\S*.*(not found|unknown|not exist|missing|не найден|нет)"
    r"|(not found|unknown|missing|не найден|нет)\W.*notify\.",
    re.I,
)


def clean(text: object, limit: int = 200) -> str | None:
    """A Home Assistant message made safe to show: no URLs, IPs or tokens, one line."""
    out = " ".join(str(text or "").split())
    for pattern, repl in _SECRETS:
        out = pattern.sub(repl, out)
    return out[:limit] or None


def _code_for(detail: str | None, default: str) -> str:
    return "ha_notify_missing" if detail and _NOTIFY_MISSING.search(detail) else default


class HaError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        # What the script itself said went wrong, for the web UI.
        self.detail = detail


def script_entity(settings: Settings) -> str:
    name = settings.ha_script.strip()
    return name if name.startswith("script.") else f"script.{name}"


def variables(settings: Settings, values: dict) -> dict:
    """The script's variables, renamed per MIH_HA_FIELDS (``url=stream_url,...``); a
    field mapped to nothing is left out. Empty values are not sent."""
    out = {}
    for key, value in values.items():
        if value in (None, "", []):
            continue
        name = settings.ha_fields.get(key, key)
        if name:
            out[name] = value
    return out


def run_script(
    settings: Settings, values: dict, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Run the script and wait for it. Raises HaError with a code the web UI explains.

    A script that ends with ``stop: ... response_variable: result`` may answer
    ``{"ok": false, "error": "..."}``: that is a failure too, with its error as detail."""
    entity = script_entity(settings)
    url = f"{settings.ha_url}/api/services/script/{entity.removeprefix('script.')}"
    try:
        with _client(settings, transport) as client:
            resp = client.post(
                url, params={"return_response": ""}, json=variables(settings, values)
            )
    except httpx.TimeoutException as exc:
        raise HaError("ha_timeout", "Home Assistant did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise HaError(
            "ha_unreachable", f"Home Assistant is unreachable: {type(exc).__name__}"
        ) from exc
    if resp.status_code >= 400:
        detail = clean(_message(resp))
        code = _HTTP_ERRORS.get(resp.status_code) or _code_for(detail, "ha_script_failed")
        raise HaError(code, f"Home Assistant answered {resp.status_code}", detail)
    answer = _response(resp)
    if str(answer.get("ok", "")).strip().lower() == "false":
        detail = clean(answer.get("error"))
        raise HaError(_code_for(detail, "ha_not_playing"),
                      "the script says the channel did not start", detail)  # fmt: skip


def _message(resp: httpx.Response) -> str:
    """Home Assistant's own words on an error: {"message": ...} or plain text."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    return str(body.get("message") or "") if isinstance(body, dict) else ""


def _response(resp: httpx.Response) -> dict:
    """The script's own answer in ``service_response``; {} when there is none."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    answer = body.get("service_response") if isinstance(body, dict) else None
    return answer if isinstance(answer, dict) else {}


def check(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> dict:
    """For the settings page: does Home Assistant answer, accept the token and know the
    script? Reads only; runs nothing."""
    out = {"configured": bool(settings.ha_url and settings.ha_token),
           "script": script_entity(settings), "notify": notify_entity(settings),
           "ok": False, "error": None}  # fmt: skip
    if not out["configured"]:
        out["error"] = "ha_not_configured"
        return out
    try:
        with _client(settings, transport) as client:
            api = client.get(f"{settings.ha_url}/api/")
            if api.status_code >= 400:
                raise HaError(_HTTP_ERRORS.get(api.status_code, "ha_error"), str(api.status_code))
            script = client.get(f"{settings.ha_url}/api/states/{out['script']}")
            if script.status_code >= 400:
                code = _HTTP_ERRORS.get(script.status_code, "ha_error")
                raise HaError(code, str(script.status_code))
            if out["notify"] and not _has_notify(client, settings, out["notify"]):
                raise HaError("ha_notify_missing", out["notify"])
    except HaError as exc:
        out["error"] = exc.code
        return out
    except httpx.TimeoutException:
        out["error"] = "ha_timeout"
        return out
    except httpx.HTTPError:
        out["error"] = "ha_unreachable"
        return out
    out["ok"] = True
    return out


def notify_entity(settings: Settings) -> str | None:
    """The TV's Companion notify action (MIH_HA_NOTIFY), e.g. notify.mobile_app_<device>."""
    name = (settings.ha_notify or "").strip()
    if not name:
        return None
    return name if name.startswith("notify.") else f"notify.{name}"


def _has_notify(client: httpx.Client, settings: Settings, entity: str) -> bool:
    """Is the notify action registered? Companion re-registers under a new name after a
    new login, and the old one disappears."""
    resp = client.get(f"{settings.ha_url}/api/services")
    if resp.status_code >= 400:
        raise HaError(_HTTP_ERRORS.get(resp.status_code, "ha_error"), str(resp.status_code))
    name = entity.removeprefix("notify.")
    try:
        domains = resp.json()
    except ValueError:
        return False
    for domain in domains if isinstance(domains, list) else []:
        if isinstance(domain, dict) and domain.get("domain") == "notify":
            return name in (domain.get("services") or {})
    return False


def _client(settings: Settings, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {settings.ha_token}"},
        timeout=settings.ha_timeout_s,
        transport=transport,
        follow_redirects=False,
    )
