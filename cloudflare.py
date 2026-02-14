import asyncio
import logging
import aiohttp
from typing import List, Dict, Optional

CF_API_BASE = "https://api.cloudflare.com/client/v4"

# Bug #9: retry-able status codes
_RETRY_STATUSES = {429, 500, 502, 503}
_MAX_RETRIES = 3


def info(msg: str) -> None:
    logging.info(msg)


def cf_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


async def _request_with_retry(session: aiohttp.ClientSession, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
    """Bug #9 fix: retry with exponential backoff for transient errors."""
    # Bug #13 fix: use aiohttp.ClientTimeout instead of deprecated int
    if "timeout" in kwargs and isinstance(kwargs["timeout"], (int, float)):
        kwargs["timeout"] = aiohttp.ClientTimeout(total=kwargs["timeout"])

    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await session.request(method, url, **kwargs)
            if resp.status not in _RETRY_STATUSES:
                return resp
            body = await resp.text()
            last_exc = aiohttp.ClientResponseError(
                request_info=resp.request_info,
                history=resp.history,
                status=resp.status,
                message=f"Retry-able error: {resp.status} {body}",
            )
            logging.warning(f"CF API {method} {url} returned {resp.status}, retry {attempt+1}/{_MAX_RETRIES}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            logging.warning(f"CF API {method} {url} error: {e}, retry {attempt+1}/{_MAX_RETRIES}")

        await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s

    raise last_exc  # type: ignore[misc]


async def cf_list_records(session: aiohttp.ClientSession, zone_id: str, name: str, type_: Optional[str], api_token: str) -> List[dict]:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    params = {"name": name}
    if type_:
        params["type"] = type_
    info(f"🌐 CF API запрос: GET {url} params={params}")
    all_results: List[dict] = []
    page = 1
    while True:
        page_params = {**params, "page": page, "per_page": 100}
        resp = await _request_with_retry(session, "GET", url, headers=cf_headers(api_token), params=page_params, timeout=15)
        resp.raise_for_status()
        data = await resp.json()

        if not data.get("success"):
            raise RuntimeError(f"Cloudflare API error: {data}")
        results = data.get("result", [])
        info(f"🌐 CF API ответ: страница {page}, найдено {len(results)} записей")
        if results:
            for r in results[:3]:
                info(f"🌐 CF запись: {r.get('name')} ({r.get('type')}) -> {r.get('content')}")
        if not results:
            break
        all_results.extend(results)
        total_pages = data.get("result_info", {}).get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1
    return all_results


async def cf_create_record(
    session: aiohttp.ClientSession, zone_id: str, name: str, type_: str,
    content: str, proxied: bool, ttl: int, api_token: str
) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    payload = {
        "type": type_,
        "name": name,
        "content": content,
        "ttl": ttl,
        "proxied": proxied,
    }
    resp = await _request_with_retry(session, "POST", url, headers=cf_headers(api_token), json=payload, timeout=15)
    resp.raise_for_status()
    return await resp.json()


async def cf_update_record(session: aiohttp.ClientSession, zone_id: str, record_id: str, fields: Dict[str, object], api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    resp = await _request_with_retry(session, "PATCH", url, headers=cf_headers(api_token), json=fields, timeout=15)
    resp.raise_for_status()
    return await resp.json()


async def cf_delete_record(session: aiohttp.ClientSession, zone_id: str, record_id: str, api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    resp = await _request_with_retry(session, "DELETE", url, headers=cf_headers(api_token), timeout=15)
    resp.raise_for_status()
    return await resp.json()
