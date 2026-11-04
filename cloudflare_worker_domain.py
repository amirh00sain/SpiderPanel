"""Deploy the standalone VLESS gateway as a Cloudflare Pages project."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx

CF_API = "https://api.cloudflare.com/client/v4"
WORKER_TEMPLATE = Path(__file__).resolve().parent / "worker" / "worker.js"
TIMEOUT = httpx.Timeout(120.0, connect=15.0)
TOKEN_URL = "https://dash.cloudflare.com/profile/api-tokens"


class CloudflareWorkerError(RuntimeError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.strip()}", "User-Agent": "SpiderPanel"}


def _error(data: dict) -> str:
    errors = data.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "Cloudflare API request failed")
    return str(data.get("message") or "Cloudflare API request failed")


async def _request(method: str, path: str, token: str, *, params: dict | None = None,
                   payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        try:
            response = await client.request(
                method, f"{CF_API}{path}", headers=_headers(token),
                params=params, json=payload,
            )
        except httpx.HTTPError as exc:
            raise CloudflareWorkerError(f"Cloudflare API connection failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or data.get("success") is False:
        raise CloudflareWorkerError(f"Cloudflare API ({response.status_code}): {_error(data)}")
    return data


async def list_accounts(token: str) -> list[dict]:
    data = await _request("GET", "/accounts", token, params={"per_page": 50})
    return [
        {"id": str(item.get("id") or ""), "name": str(item.get("name") or item.get("id") or "")}
        for item in data.get("result", []) if item.get("id")
    ]


async def list_zones(token: str, account_id: str) -> list[dict]:
    zones: list[dict] = []
    page = 1
    while page <= 20:
        data = await _request(
            "GET", "/zones", token,
            params={"account.id": account_id, "per_page": 50, "page": page},
        )
        for item in data.get("result", []):
            if item.get("id") and item.get("name"):
                zones.append({"id": str(item["id"]), "name": str(item["name"])})
        info = data.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break
        page += 1
    return zones


async def _upload_pages_worker(token: str, account_id: str, project_name: str, source: str) -> dict:
    """Publish a Pages Direct Upload deployment containing Advanced Mode _worker.js."""
    worker_bytes = source.encode("utf-8")
    manifest = json.dumps({"_worker.js": hashlib.md5(worker_bytes).hexdigest()})
    boundary = "----SpiderPages" + secrets.token_hex(12)

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body = bytearray()
    body += field("branch", "main")
    body += field("commit_dirty", "false")
    body += field("commit_message", "SpiderPanel Pages Domain deploy")
    body += field("manifest", manifest)
    body += (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="_worker.js"; filename="_worker.js"\r\n'
        "Content-Type: application/javascript\r\n\r\n"
    ).encode()
    body += worker_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    path = f"/accounts/{account_id}/pages/projects/{quote(project_name, safe='')}/deployments"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.post(
                f"{CF_API}{path}",
                headers={**_headers(token), "Content-Type": f"multipart/form-data; boundary={boundary}"},
                content=bytes(body),
            )
        except httpx.HTTPError as exc:
            raise CloudflareWorkerError(f"Cloudflare Pages deployment request failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or data.get("success") is False:
        raise CloudflareWorkerError(f"Cloudflare Pages deploy ({response.status_code}): {_error(data)}")
    return data.get("result") or {}


async def deploy(token: str, account_id: str, panel_domain: str) -> dict:
    token = token.strip()
    if not token or len(token) > 4096:
        raise CloudflareWorkerError("Enter a valid Cloudflare API token")
    if not re.fullmatch(r"[a-f0-9]{32}", account_id, re.I):
        raise CloudflareWorkerError("Choose a valid Cloudflare account")
    accounts = await list_accounts(token)
    account = next((item for item in accounts if item["id"] == account_id), None)
    if not account:
        raise CloudflareWorkerError("The selected account is not available to this token")

    project_name = "spider-domain-" + secrets.token_hex(4)
    kv_title = project_name + "-db"
    kv_data = await _request(
        "POST", f"/accounts/{account_id}/storage/kv/namespaces", token,
        payload={"title": kv_title},
    )
    kv_id = str((kv_data.get("result") or {}).get("id") or "")
    if not kv_id:
        raise CloudflareWorkerError("Cloudflare did not return a KV namespace ID")

    project_created = False
    try:
        if not WORKER_TEMPLATE.is_file():
            raise CloudflareWorkerError("The worker/worker.js Pages deployment template is missing")
        source = WORKER_TEMPLATE.read_text(encoding="utf-8")
        pages_domain = f"{project_name}.pages.dev"
        control_token = "spider-pages-" + secrets.token_urlsafe(32)
        for marker, value in {
            "__PANEL_TOKEN__": json.dumps(control_token),
            "__PANEL_DOMAIN__": json.dumps(panel_domain),
            "__WORKER_DOMAIN__": json.dumps(pages_domain),
        }.items():
            source = source.replace(marker, value)

        binding = {"SPIDER_KV": {"namespace_id": kv_id}}
        project_data = await _request(
            "POST", f"/accounts/{account_id}/pages/projects", token,
            payload={
                "name": project_name,
                "production_branch": "main",
                "deployment_configs": {
                    "production": {"compatibility_date": "2026-09-18", "kv_namespaces": binding},
                    "preview": {"compatibility_date": "2026-09-18", "kv_namespaces": binding},
                },
            },
        )
        project = project_data.get("result") or {}
        project_id = str(project.get("id") or "")
        if not project_id:
            raise CloudflareWorkerError("Cloudflare did not return a Pages project ID")
        project_created = True
        deployment = await _upload_pages_worker(token, account_id, project_name, source)
        live_url = str(deployment.get("url") or "").strip() or f"https://{pages_domain}"
        return {
            "platform": "pages",
            "account_id": account_id,
            "account_name": account["name"],
            "worker_name": project_name,
            "pages_project_name": project_name,
            "pages_project_id": project_id,
            "worker_domain": pages_domain,
            "worker_url": f"https://{pages_domain}",
            "pages_url": live_url,
            "control_token": control_token,
            "kv_namespace_id": kv_id,
            "kv_namespace_title": kv_title,
            "token": token,
        }
    except Exception:
        if project_created:
            try:
                await _request("DELETE", f"/accounts/{account_id}/pages/projects/{project_name}", token)
            except Exception:
                pass
        try:
            await _request("DELETE", f"/accounts/{account_id}/storage/kv/namespaces/{kv_id}", token)
        except Exception:
            pass
        raise


async def sync_users(state: dict, users: list[dict]) -> dict:
    """Push current panel users into the Pages Advanced Mode worker's KV control plane."""
    domain = str(state.get("custom_domain") or state.get("worker_domain") or "").strip()
    token = str(state.get("control_token") or "")
    if not domain or not token:
        raise CloudflareWorkerError("The Pages project has no reachable domain yet")
    url = f"https://{domain}/panel/config"
    payload = {"users": users, "routes": {}, "proxies": {}, "settings": {}}
    last_error = "Pages project is not reachable yet"
    for attempt, delay in enumerate((0, 2, 4, 8), 1):
        if delay:
            await asyncio.sleep(delay)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            try:
                response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt == 4:
                    break
                continue
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                return {"ok": True}
        last_error = f"Pages project returned HTTP {response.status_code}: {response.text[:160]}"
        if response.status_code not in (404, 502, 503, 504) or attempt == 4:
            break
    raise CloudflareWorkerError(f"Could not sync users to Pages: {last_error}")


async def attach_custom_domain(token: str, account_id: str, project_name: str,
                               zone_id: str, zone_name: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", zone_id, re.I):
        raise CloudflareWorkerError("Choose a valid Cloudflare zone")
    if not re.fullmatch(r"[a-f0-9]{32}", account_id, re.I):
        raise CloudflareWorkerError("Invalid Cloudflare account")
    hostname = f"{secrets.token_hex(4)}.{zone_name}".lower()
    result = await _request(
        "POST", f"/accounts/{account_id}/pages/projects/{quote(project_name, safe='')}/domains", token,
        payload={"name": hostname},
    )
    domain = result.get("result") or {}
    if not domain.get("id"):
        raise CloudflareWorkerError("Cloudflare did not return the Pages custom domain ID")
    return {
        "custom_domain": hostname,
        "custom_domain_id": str(domain.get("domain_id") or domain.get("id") or hostname),
        "zone_id": zone_id,
        "zone_name": zone_name,
    }


async def _delete_legacy_worker(state: dict, token: str, account_id: str, worker_name: str) -> list[str]:
    """Clean up deployments created by the old Worker Scripts implementation."""
    errors: list[str] = []
    custom_domain_id = str(state.get("custom_domain_id") or "")
    kv_id = str(state.get("kv_namespace_id") or "")
    if custom_domain_id:
        try:
            await _request("DELETE", f"/accounts/{account_id}/workers/domains/{custom_domain_id}", token)
        except CloudflareWorkerError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    try:
        await _request("DELETE", f"/accounts/{account_id}/workers/scripts/{worker_name}/subdomain", token)
    except CloudflareWorkerError as exc:
        if "not found" not in str(exc).lower():
            errors.append(str(exc))
    try:
        await _request("DELETE", f"/accounts/{account_id}/workers/scripts/{worker_name}", token)
    except CloudflareWorkerError as exc:
        if "not found" not in str(exc).lower():
            errors.append(str(exc))
    if kv_id:
        try:
            await _request("DELETE", f"/accounts/{account_id}/storage/kv/namespaces/{kv_id}", token)
        except CloudflareWorkerError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    return errors


async def _delete_pages_custom_domain(state: dict, token: str, account_id: str,
                                      project_name: str, hostname: str) -> list[str]:
    errors: list[str] = []
    if not hostname:
        return errors
    encoded_project = quote(project_name, safe="")
    zone_id = str(state.get("zone_id") or "")
    pages_domain = str(state.get("worker_domain") or f"{project_name}.pages.dev").lower()
    if zone_id:
        try:
            records = await _request(
                "GET", f"/zones/{zone_id}/dns_records", token,
                params={"name": hostname, "per_page": 100},
            )
            for record in records.get("result", []):
                # Delete only the CNAME Pages created for this exact project.
                if (str(record.get("type") or "").upper() == "CNAME"
                        and str(record.get("name") or "").lower() == hostname.lower()
                        and str(record.get("content") or "").rstrip(".").lower() == pages_domain.rstrip(".").lower()
                        and record.get("id")):
                    await _request("DELETE", f"/zones/{zone_id}/dns_records/{record['id']}", token)
        except CloudflareWorkerError as exc:
            errors.append(f"Could not remove Pages DNS record: {exc}")
    if errors:
        return errors
    try:
        await _request(
            "DELETE",
            f"/accounts/{account_id}/pages/projects/{encoded_project}/domains/{quote(hostname, safe='')}",
            token,
        )
    except CloudflareWorkerError as exc:
        if "not found" not in str(exc).lower():
            errors.append(str(exc))
    return errors


async def delete_deployment(state: dict) -> None:
    """Remove the Pages project/domain/KV, or clean up an older Worker deploy."""
    token = str(state.get("token") or "")
    account_id = str(state.get("account_id") or "")
    project_name = str(state.get("pages_project_name") or "").strip()
    worker_name = str(state.get("worker_name") or "").strip()
    if not token or not account_id or not (project_name or worker_name):
        raise CloudflareWorkerError("No Cloudflare Pages deployment is connected")
    if not project_name or state.get("platform") != "pages":
        errors = await _delete_legacy_worker(state, token, account_id, worker_name)
        if errors:
            raise CloudflareWorkerError("; ".join(errors))
        return

    errors: list[str] = []
    hostname = str(state.get("custom_domain") or "")
    if hostname:
        domain_errors = await _delete_pages_custom_domain(state, token, account_id, project_name, hostname)
        if domain_errors:
            raise CloudflareWorkerError("; ".join(domain_errors))
    try:
        await _request("DELETE", f"/accounts/{account_id}/pages/projects/{quote(project_name, safe='')}", token)
    except CloudflareWorkerError as exc:
        if "not found" not in str(exc).lower():
            errors.append(str(exc))
    kv_id = str(state.get("kv_namespace_id") or "")
    if kv_id and not errors:
        try:
            await _request("DELETE", f"/accounts/{account_id}/storage/kv/namespaces/{kv_id}", token)
        except CloudflareWorkerError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    if errors:
        raise CloudflareWorkerError("; ".join(errors))
