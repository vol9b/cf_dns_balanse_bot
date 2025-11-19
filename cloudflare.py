import logging
import aiohttp
from typing import List, Dict, Optional

CF_API_BASE = "https://api.cloudflare.com/client/v4"

def info(msg: str) -> None:
    logging.info(msg)

def cf_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

async def cf_list_records(session: aiohttp.ClientSession, zone_id: str, name: str, type_: Optional[str], api_token: str) -> List[dict]:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    params = {"name": name}
    if type_:
        params["type"] = type_
    info(f"🌐 CF API запрос: GET {url} params={params}")
    all_results: List[dict] = []
    page = 1
    while True:
        params.update({"page": page, "per_page": 100})
        async with session.get(url, headers=cf_headers(api_token), params=params, timeout=15) as resp:
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

async def cf_create_record(session: aiohttp.ClientSession, zone_id: str, name: str, type_: str, content: str, proxied: bool, ttl: int, api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    payload = {
        "type": type_,
        "name": name,
        "content": content,
        "ttl": ttl,
        "proxied": proxied,
    }
    async with session.post(url, headers=cf_headers(api_token), json=payload, timeout=15) as resp:
        resp.raise_for_status()
        return await resp.json()

async def cf_update_record(session: aiohttp.ClientSession, zone_id: str, record_id: str, fields: Dict[str, object], api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    async with session.patch(url, headers=cf_headers(api_token), json=fields, timeout=15) as resp:
        resp.raise_for_status()
        return await resp.json()

async def cf_delete_record(session: aiohttp.ClientSession, zone_id: str, record_id: str, api_token: str) -> dict:
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    async with session.delete(url, headers=cf_headers(api_token), timeout=15) as resp:
        resp.raise_for_status()
        return await resp.json()
