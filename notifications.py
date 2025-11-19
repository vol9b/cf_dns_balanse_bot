import logging
import aiohttp
from config import Config

def warn(msg: str) -> None:
    logging.warning(msg)

async def tg_send(cfg: Config, session: aiohttp.ClientSession, text: str) -> None:
    if not cfg.tg_enabled:
        return
    try:
        url = f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage"
        payload = {
            "chat_id": cfg.tg_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status >= 300:
                text_resp = await resp.text()
                warn(f"Telegram send failed: {resp.status} {text_resp}")
    except Exception as e:
        warn(f"Telegram send exception: {e}")
