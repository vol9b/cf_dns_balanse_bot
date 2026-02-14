import logging
import aiohttp
from typing import Optional, List
from config import Config


def warn(msg: str) -> None:
    logging.warning(msg)


TG_API = "https://api.telegram.org/bot{token}"


def _api_url(token: str, method: str) -> str:
    return f"{TG_API.format(token=token)}/{method}"


_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def tg_send(
    cfg: Config, session: aiohttp.ClientSession, text: str,
) -> Optional[dict]:
    """Send a plain text message."""
    if not cfg.tg_enabled:
        return None
    return await tg_send_with_keyboard(
        cfg, session, text, keyboard=None,
    )


async def tg_send_with_keyboard(
    cfg: Config, session: aiohttp.ClientSession, text: str,
    keyboard: Optional[List[List[dict]]] = None,
) -> Optional[dict]:
    """Send message with optional InlineKeyboardMarkup.

    keyboard is a list of rows, each row is a list of dicts with
    keys: text, callback_data.
    """
    if not cfg.tg_enabled:
        return None
    try:
        url = _api_url(cfg.tg_token, "sendMessage")
        payload: dict = {
            "chat_id": cfg.tg_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {
                "inline_keyboard": keyboard,
            }
        async with session.post(
            url, json=payload, timeout=_TIMEOUT,
        ) as resp:
            data = await resp.json()
            if resp.status >= 300:
                warn(f"Telegram send failed: {resp.status} {data}")
                return None
            return data.get("result")
    except Exception as e:
        warn(f"Telegram send exception: {e}")
        return None


async def tg_edit_message(
    cfg: Config, session: aiohttp.ClientSession,
    chat_id: int, message_id: int, text: str,
    keyboard: Optional[List[List[dict]]] = None,
) -> Optional[dict]:
    """Edit an existing message text + keyboard."""
    if not cfg.tg_enabled:
        return None
    try:
        url = _api_url(cfg.tg_token, "editMessageText")
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {
                "inline_keyboard": keyboard,
            }
        async with session.post(
            url, json=payload, timeout=_TIMEOUT,
        ) as resp:
            data = await resp.json()
            if resp.status >= 300:
                warn(f"Telegram edit failed: {resp.status} {data}")
                return None
            return data.get("result")
    except Exception as e:
        warn(f"Telegram edit exception: {e}")
        return None


async def tg_answer_callback(
    cfg: Config, session: aiohttp.ClientSession,
    callback_query_id: str, text: str = "",
    show_alert: bool = False,
) -> None:
    """Answer a callback query (acknowledge button press)."""
    if not cfg.tg_enabled:
        return
    try:
        url = _api_url(cfg.tg_token, "answerCallbackQuery")
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        async with session.post(
            url, json=payload, timeout=_TIMEOUT,
        ) as resp:
            if resp.status >= 300:
                data = await resp.text()
                warn(f"Telegram answer_cb failed: {resp.status} {data}")
    except Exception as e:
        warn(f"Telegram answer_cb exception: {e}")
