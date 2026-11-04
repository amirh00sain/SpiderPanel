"""Deploy/delete SpiderPanel's WebSocket relay project on Vercel.

This Python/httpx implementation lets SpiderPanel manage the Vercel project
from System → Vercel Domain without needing a separate Node/Express panel.

The uploaded project is the relay app at deploy/vercel/ next to this file: a
Vercel Node.js function that bridges WebSocket connections on PUBLIC_RELAY_PATH
through WSS to {TARGET_DOMAIN}{RELAY_PATH}. The upstream HTTP-only XHTTP relay
has been replaced by a WebSocket bridge; generated Vercel configs use TLS+WS
and the path /ws/vl/{uuid}.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx

VERCEL_API = "https://api.vercel.com"
RELAY_DIR = Path(__file__).resolve().parent / "deploy" / "vercel"

# WebSocket relay deployments bundle a Node runtime dependency and a tiny landing page.
_DEPLOY_TIMEOUT = httpx.Timeout(90.0, connect=15.0)
_DELETE_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

_SKIP_DIR_NAMES = {"node_modules", ".vercel", ".git"}
# NOTICE.md is repository documentation and is not deployed into the relay project.
_SKIP_FILE_NAMES = {"NOTICE.md"}


class VercelError(RuntimeError):
    """Raised with a human-readable message safe to show the admin."""


def _collect_files(base: Path) -> list[dict]:
    """Mirror vercel.service.ts's collectFiles(): every file under RELAY_DIR,
    base64-encoded, keyed by its path relative to RELAY_DIR (forward slashes,
    as the Vercel deployments API expects)."""
    files: list[dict] = []
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        if _SKIP_DIR_NAMES & set(path.relative_to(base).parts):
            continue
        if path.name in _SKIP_FILE_NAMES:
            continue
        rel = path.relative_to(base).as_posix()
        files.append({
            "file": rel,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "encoding": "base64",
        })
    return files


def _api_url(path: str, team_id: str | None) -> str:
    url = f"{VERCEL_API}{path}"
    if team_id:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}teamId={team_id}"
    return url


def _extract_vercel_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = err.get("message") or data.get("message")
        if msg:
            return str(msg)
    except Exception:
        pass
    return resp.text[:500]


async def deploy_to_vercel(
    token: str,
    project_name: str,
    target_domain: str,
    relay_path: str,
    public_path: str,
    team_id: str | None = None,
) -> dict:
    """Upload deploy/vercel/ as a new Vercel deployment and return
    {"url": "https://<domain>.vercel.app", "deploy_id": "...", "project_id": "..."}.

    Raises VercelError with a message safe to surface to the admin.
    """
    if not RELAY_DIR.is_dir():
        raise VercelError("Relay template directory (deploy/vercel) is missing from this install.")

    files = _collect_files(RELAY_DIR)
    if not files:
        raise VercelError("Relay template directory is empty.")

    target = target_domain if ":" in target_domain else f"{target_domain}:443"
    env = {
        "TARGET_DOMAIN": f"https://{target}",
        "RELAY_PATH": relay_path,
        "PUBLIC_RELAY_PATH": public_path,
    }
    body = {
        "name": project_name,
        "files": files,
        # A public, stable project domain is required for a transport relay.
        # If `target` is omitted, Vercel creates a Preview deployment by
        # default; its generated deployment URL can be protected by SSO and
        # is not the production alias clients should use.
        "target": "production",
        # Explicitly clear framework inference and give Vercel a real output
        # directory. vercel.json also declares this; keeping it here covers
        # deployments where project settings are used before config parsing.
        "projectSettings": {"framework": None, "outputDirectory": "public"},
        "env": env,
    }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_DEPLOY_TIMEOUT, follow_redirects=False) as client:
        resp = await client.post(
            _api_url("/v13/deployments", team_id),
            headers=headers,
            content=json.dumps(body),
        )
        if resp.status_code >= 400:
            raise VercelError(f"Vercel deploy failed ({resp.status_code}): {_extract_vercel_error(resp)}")

        data = resp.json()
        deploy_id = str(data.get("id") or "").strip()
        if not deploy_id:
            raise VercelError("Vercel returned a deployment without an ID, so its build status cannot be verified.")
        original_project_id = str(data.get("projectId") or (data.get("project") or {}).get("id") or "")

        # POST /v13/deployments returns as soon as the deployment is created,
        # not necessarily when its build succeeded. Wait for READY before
        # touching project aliases or recording it as connected.
        last_state = str(data.get("readyState") or data.get("status") or "QUEUED").upper()
        last_message = str(data.get("errorMessage") or "").strip()
        terminal_failure_states = {"ERROR", "CANCELED", "CANCELLED", "DELETED"}
        for attempt in range(30):
            if last_state == "READY":
                break
            if last_state in terminal_failure_states:
                detail = last_message or str(data.get("errorCode") or last_state)
                raise VercelError(f"Vercel deployment failed ({last_state}): {detail}")
            if attempt:
                await asyncio.sleep(2)
            status_resp = await client.get(
                _api_url(f"/v13/deployments/{deploy_id}", team_id),
                headers={"Authorization": f"Bearer {token}"},
            )
            if status_resp.status_code >= 400:
                raise VercelError(
                    f"Vercel created deployment {deploy_id}, but its status could not be checked "
                    f"({status_resp.status_code}): {_extract_vercel_error(status_resp)}"
                )
            data = status_resp.json()
            original_project_id = str(data.get("projectId") or original_project_id)
            last_state = str(data.get("readyState") or data.get("status") or last_state).upper()
            last_message = str(data.get("errorMessage") or last_message or "").strip()
            if last_state == "READY":
                break
            if last_state in terminal_failure_states:
                detail = last_message or str(data.get("errorCode") or last_state)
                raise VercelError(f"Vercel deployment failed ({last_state}): {detail}")
        else:
            raise VercelError(
                f"Vercel deployment did not become READY within 60 seconds (last state: {last_state})."
            )

        # A newly-created project may inherit team-level Vercel Authentication.
        # XHTTP clients cannot follow an SSO redirect, so explicitly make this
        # dedicated relay project public. Do not do this to any pre-existing
        # user project: project_name is always generated uniquely by SpiderPanel.
        protection_resp = await client.patch(
            _api_url(f"/v9/projects/{project_name}", team_id),
            headers=headers,
            content=json.dumps({"ssoProtection": None}),
        )
        if protection_resp.status_code >= 400:
            raise VercelError(
                "Deployment is READY, but Vercel could not disable SSO protection for the relay project "
                f"({protection_resp.status_code}): {_extract_vercel_error(protection_resp)}"
            )

        # Use the stable *production project domain*, never the per-deployment
        # URL (`project-hash-team.vercel.app`). The latter can remain behind
        # Vercel Authentication even after a successful build.
        domain_resp = await client.get(
            _api_url(f"/v9/projects/{project_name}/domains?production=true&limit=100", team_id),
            headers={"Authorization": f"Bearer {token}"},
        )
        if domain_resp.status_code >= 400:
            raise VercelError(
                "Deployment is READY, but Vercel could not list the project's production domains "
                f"({domain_resp.status_code}): {_extract_vercel_error(domain_resp)}"
            )
        domain_data = domain_resp.json()
        entries = domain_data.get("domains") or []
        candidates: list[str] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            if item.get("verified") is False or item.get("redirect"):
                continue
            name = str(item.get("name") or "").strip().lower().rstrip(".")
            if name.endswith(".vercel.app") and "." in name:
                candidates.append(name)
        # A project created from the API normally has this stable alias. Prefer
        # it; otherwise accept the first verified production .vercel.app alias.
        preferred = f"{project_name}.vercel.app".lower()
        domain = preferred if preferred in candidates else (candidates[0] if candidates else "")
        if not domain:
            # Some API responses include aliases on the deployment before the
            # project-domain listing is populated. Accept only aliases reported
            # by Vercel, and never fall back to the unique deployment URL.
            aliases = data.get("alias") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            reported = [str(x).strip().lower().rstrip(".") for x in aliases if str(x).strip()]
            domain = preferred if preferred in reported else next(
                (x for x in reported if x.endswith(".vercel.app")), ""
            )
        if not domain:
            raise VercelError(
                "Vercel deployment reached READY, but no stable production *.vercel.app domain was assigned. "
                "The unique deployment URL was intentionally not used because it may be protected by SSO."
            )

        # Prove that the alias is publicly accessible and actually runs this
        # relay before SpiderPanel marks it connected. Deployment Protection
        # changes can take a moment to propagate, so retry briefly.
        last_probe = "no response"
        for attempt in range(8):
            try:
                # Do not probe `/`: Vercel correctly serves the project's
                # static public/index.html at the root before its catch-all
                # rewrite. The explicit health path is routed to the function.
                probe = await client.get(
                    f"https://{domain}/_spider-health",
                    headers={"Cache-Control": "no-cache, no-store"},
                )
                body_text = probe.text[:500]
                if (
                    probe.status_code == 200
                    and "SpiderPanel WebSocket relay is online" in body_text
                    and probe.headers.get("x-spiderpanel-relay") == "healthy"
                ):
                    break
                location = str(probe.headers.get("location") or "").strip()
                if probe.status_code in (301, 302, 303, 307, 308) and location:
                    last_probe = f"HTTP {probe.status_code} redirect to {location[:180]}"
                else:
                    last_probe = f"HTTP {probe.status_code}: {body_text[:180]}"
            except httpx.HTTPError as exc:
                last_probe = str(exc)[:180]
            if attempt < 7:
                await asyncio.sleep(2)
        else:
            raise VercelError(
                f"Vercel created the production domain {domain}, but its public WebSocket relay health check failed: "
                f"{last_probe}. The dedicated /_spider-health route did not reach the relay function; "
                "check that the latest Vercel deployment includes deploy/vercel/vercel.json and api/index.js, "
                "and that Deployment Protection is disabled for this production domain."
            )

    return {
        "url": f"https://{domain}",
        "domain": domain,
        "deploy_id": deploy_id,
        "project_id": original_project_id,
    }


async def delete_vercel_project(token: str, project_name: str, team_id: str | None = None) -> None:
    """Delete the Vercel project. A 404 (already gone) is not an error."""
    async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT) as client:
        resp = await client.delete(
            _api_url(f"/v9/projects/{project_name}", team_id),
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        raise VercelError(f"Vercel delete failed ({resp.status_code}): {_extract_vercel_error(resp)}")


async def verify_vercel_token(token: str) -> dict:
    """Light check that the token is usable before we attempt a deploy.
    Returns the Vercel user record on success; raises VercelError otherwise."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        resp = await client.get(
            f"{VERCEL_API}/v2/user",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code >= 400:
        raise VercelError(f"Vercel token rejected ({resp.status_code}): {_extract_vercel_error(resp)}")
    try:
        data = resp.json()
        return data.get("user") or data
    except Exception:
        return {}
