"""Home Assistant bridge (spec 12, ``ha_bridge``): "На ТВ" calls a script of the user's
own Home Assistant over its REST API, with no agent in between.

MIH calls ``script.<name>`` with the channel and up to MIH_HA_STREAMS stream URLs as
variables and waits for the script to finish: it ran = ``started``, it raised =
``failed``. The script decides how the TV plays the channel. MIH never changes the
Home Assistant config; the long-lived token lives only in the server's environment.
"""

from __future__ import annotations

import httpx

from mih.config import Settings

# What each answer of Home Assistant means for the command.
_HTTP_ERRORS = {401: "ha_unauthorized", 403: "ha_unauthorized", 404: "ha_script_not_found"}


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
        code = _HTTP_ERRORS.get(resp.status_code, "ha_script_failed")
        raise HaError(code, f"Home Assistant answered {resp.status_code}")
    answer = _response(resp)
    if str(answer.get("ok", "")).strip().lower() == "false":
        detail = str(answer.get("error") or "").strip()[:200] or None
        raise HaError("ha_not_playing", "the script says the channel did not start", detail)


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
           "script": script_entity(settings), "ok": False, "error": None}  # fmt: skip
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


def _client(settings: Settings, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {settings.ha_token}"},
        timeout=settings.ha_timeout_s,
        transport=transport,
        follow_redirects=False,
    )
