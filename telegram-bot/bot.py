"""
SillyTavern Telegram Bot v2.0
支持预设、WorldInfo、完整角色卡功能
"""

import os
import asyncio
import json
import logging
import secrets
import time
import html
import re
from typing import Dict, Any, AsyncIterator, Optional
from pathlib import Path

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SILLYTAVERN_URL = os.getenv('SILLYTAVERN_URL', 'http://sillytavern:8000')
# SillyTavern 插件路由前缀
PLUGIN_API_BASE = '/api/plugins/telegram-integration'
ALLOWED_USER_ID = int(os.getenv('ALLOWED_USER_ID', '0'))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
# SillyTavern Basic Auth（可选）
ST_AUTH_USER = os.getenv('ST_AUTH_USER', '')
ST_AUTH_PASS = os.getenv('ST_AUTH_PASS', '')

# Bot-level multi-user authorization (admin-managed allowlist)
TG_AUTH_DB_PATH = os.getenv('TG_AUTH_DB_PATH', '/app/data/auth.json')
TG_REGISTRATION_ENABLED_DEFAULT = os.getenv('TG_REGISTRATION_ENABLED', '1').lower() in ('1', 'true', 'yes', 'y', 'on')

# Bot performance (multi-user)
TG_CONCURRENT_UPDATES = int(os.getenv('TG_CONCURRENT_UPDATES', '8'))
TG_CONNECTION_POOL_SIZE = int(os.getenv('TG_CONNECTION_POOL_SIZE', '64'))
TG_POOL_TIMEOUT = float(os.getenv('TG_POOL_TIMEOUT', '30'))

# Telegram streaming / typing simulation
TELEGRAM_STREAM_RESPONSES = os.getenv('TELEGRAM_STREAM_RESPONSES', '1').lower() in ('1', 'true', 'yes', 'y', 'on')
TELEGRAM_STREAM_EDIT_INTERVAL_MS = int(os.getenv('TELEGRAM_STREAM_EDIT_INTERVAL_MS', '750'))
TELEGRAM_TYPING_INTERVAL_MS = int(os.getenv('TELEGRAM_TYPING_INTERVAL_MS', '3500'))
TELEGRAM_STREAM_PLACEHOLDER = os.getenv('TELEGRAM_STREAM_PLACEHOLDER', '输入中...')

# Optional per-user model menu choices (comma-separated)
TG_MODEL_CHOICES = [
    m.strip()
    for m in os.getenv('TG_MODEL_CHOICES', 'gpt-4o-mini,gpt-4o,gpt-4.1-mini,gpt-4.1').split(',')
    if m.strip()
]

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO)
)
logger = logging.getLogger(__name__)

# HTTP Client with optional Basic Auth
_auth = httpx.BasicAuth(ST_AUTH_USER, ST_AUTH_PASS) if ST_AUTH_USER else None
http_client = httpx.AsyncClient(timeout=120.0, auth=_auth)


def md_escape(text: object) -> str:
    return escape_markdown(str(text), version=1)


async def send_text_safe(send_func, text: str, *, parse_mode: str = None, reply_markup=None):
    try:
        if parse_mode:
            return await send_func(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return await send_func(text, reply_markup=reply_markup)
    except BadRequest as e:
        if parse_mode and "Can't parse entities" in str(e):
            return await send_func(text, reply_markup=reply_markup)
        raise


def _now_ms() -> int:
    return int(time.time() * 1000)


class AuthStore:
    def __init__(self, path: str, *, admin_user_id: int, registration_enabled_default: bool):
        self.path = Path(path)
        self.admin_user_id = admin_user_id
        self._lock = asyncio.Lock()
        self.data: Dict[str, Any] = {
            "version": 1,
            "registrationEnabled": registration_enabled_default,
            "allowedUsers": {},
            "pendingUsers": {},
            "invites": {},
            "userSettings": {}
        }
        self._loaded = False

    def load_sync(self) -> None:
        if self._loaded:
            return
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(self.data.get("userSettings"), dict):
                    self.data["userSettings"] = {}
                self._loaded = True
                return
        except Exception as e:
            logger.error(f"Auth DB load failed: {e}")

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            logger.error(f"Auth DB init failed: {e}")
        self._loaded = True

    async def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp_path, self.path)

    async def save(self) -> None:
        async with self._lock:
            await self._save_unlocked()

    def is_admin(self, user_id: int) -> bool:
        return self.admin_user_id != 0 and user_id == self.admin_user_id

    def is_allowed(self, user_id: int) -> bool:
        if self.admin_user_id == 0:
            return True
        if self.is_admin(user_id):
            return True
        return str(user_id) in (self.data.get("allowedUsers") or {})

    def registration_enabled(self) -> bool:
        return bool(self.data.get("registrationEnabled", TG_REGISTRATION_ENABLED_DEFAULT))

    async def set_registration_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self.data["registrationEnabled"] = bool(enabled)
            await self._save_unlocked()

    async def request_access(self, user_id: int, user_name: str) -> bool:
        async with self._lock:
            if self.is_allowed(user_id):
                return False
            pending = self.data.get("pendingUsers") or {}
            pending[str(user_id)] = {
                "userId": int(user_id),
                "userName": user_name,
                "requestedAt": _now_ms(),
            }
            self.data["pendingUsers"] = pending
            await self._save_unlocked()
            return True

    async def approve(self, user_id: int, *, approved_by: int, note: str = "") -> bool:
        async with self._lock:
            allowed = self.data.get("allowedUsers") or {}
            pending = self.data.get("pendingUsers") or {}
            user_key = str(user_id)
            user_meta = pending.pop(user_key, None) or {"userId": int(user_id), "userName": "", "requestedAt": None}
            allowed[user_key] = {
                "userId": int(user_id),
                "userName": user_meta.get("userName") or "",
                "requestedAt": user_meta.get("requestedAt"),
                "approvedAt": _now_ms(),
                "approvedBy": int(approved_by),
                "note": note,
            }
            self.data["allowedUsers"] = allowed
            self.data["pendingUsers"] = pending
            await self._save_unlocked()
            return True

    async def reject(self, user_id: int) -> bool:
        async with self._lock:
            pending = self.data.get("pendingUsers") or {}
            removed = pending.pop(str(user_id), None)
            self.data["pendingUsers"] = pending
            await self._save_unlocked()
            return removed is not None

    async def revoke(self, user_id: int) -> bool:
        async with self._lock:
            allowed = self.data.get("allowedUsers") or {}
            removed = allowed.pop(str(user_id), None)
            self.data["allowedUsers"] = allowed
            await self._save_unlocked()
            return removed is not None

    async def create_one_time_invite(self, *, created_by: int) -> str:
        async with self._lock:
            invites = self.data.get("invites") or {}
            while True:
                code = secrets.token_urlsafe(8)
                if code not in invites:
                    break
            invites[code] = {
                "code": code,
                "usesRemaining": 1,
                "createdAt": _now_ms(),
                "createdBy": int(created_by),
            }
            self.data["invites"] = invites
            await self._save_unlocked()
            return code

    async def redeem_invite(self, *, user_id: int, user_name: str, code: str, approved_by: int) -> bool:
        async with self._lock:
            if self.is_allowed(user_id):
                return True
            invites = self.data.get("invites") or {}
            invite = invites.get(code)
            if not invite:
                return False
            uses = int(invite.get("usesRemaining", 0))
            if uses <= 0:
                invites.pop(code, None)
                self.data["invites"] = invites
                await self._save_unlocked()
                return False

            invite["usesRemaining"] = uses - 1
            if invite["usesRemaining"] <= 0:
                invites.pop(code, None)
            else:
                invites[code] = invite

            allowed = self.data.get("allowedUsers") or {}
            allowed[str(user_id)] = {
                "userId": int(user_id),
                "userName": user_name,
                "requestedAt": None,
                "approvedAt": _now_ms(),
                "approvedBy": int(approved_by),
                "note": "invite",
            }
            pending = self.data.get("pendingUsers") or {}
            pending.pop(str(user_id), None)

            self.data["invites"] = invites
            self.data["allowedUsers"] = allowed
            self.data["pendingUsers"] = pending
            await self._save_unlocked()
            return True

    def list_pending(self) -> list[dict]:
        pending = self.data.get("pendingUsers") or {}
        return [pending[k] for k in sorted(pending.keys())]

    def list_allowed(self) -> list[dict]:
        allowed = self.data.get("allowedUsers") or {}
        return [allowed[k] for k in sorted(allowed.keys())]

    def get_user_llm_model(self, user_id: int) -> Optional[str]:
        settings = self.data.get("userSettings") or {}
        entry = settings.get(str(user_id)) if isinstance(settings, dict) else None
        if not isinstance(entry, dict):
            return None
        model = entry.get("llmModel")
        if not isinstance(model, str):
            return None
        model = model.strip()
        return model or None

    async def set_user_llm_model(self, user_id: int, model: Optional[str]) -> None:
        key = str(user_id)
        normalized = None
        if isinstance(model, str):
            normalized = model.strip() or None

        async with self._lock:
            settings = self.data.get("userSettings")
            if not isinstance(settings, dict):
                settings = {}

            entry = settings.get(key)
            if not isinstance(entry, dict):
                entry = {}

            if normalized is None:
                entry.pop("llmModel", None)
                if entry:
                    settings[key] = entry
                else:
                    settings.pop(key, None)
            else:
                entry["llmModel"] = normalized
                settings[key] = entry

            self.data["userSettings"] = settings
            await self._save_unlocked()


class SillyTavernClient:
    """SillyTavern API Client"""

    def __init__(self, base_url: str, api_prefix: str = ''):
        self.base_url = base_url.rstrip('/')
        self.api_prefix = api_prefix

    async def _get(self, path: str, params: dict = None) -> Dict[str, Any]:
        url = f"{self.base_url}{self.api_prefix}{path}"
        response = await http_client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, data: dict) -> Dict[str, Any]:
        url = f"{self.base_url}{self.api_prefix}{path}"
        response = await http_client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def get_plugin_config(self) -> Dict[str, Any]:
        return await self._get('/config')

    async def set_plugin_config(self, updates: dict) -> Dict[str, Any]:
        return await self._post('/config', updates)

    async def health_check(self) -> bool:
        try:
            result = await self._get('/health')
            return result.get('success', False)
        except Exception:
            return False

    async def get_characters(self) -> Dict[str, Any]:
        return await self._get('/characters')

    async def get_presets(self) -> Dict[str, Any]:
        return await self._get('/presets')

    async def get_worldinfo(self) -> Dict[str, Any]:
        return await self._get('/worldinfo')

    async def get_session(self, user_id: str) -> Dict[str, Any]:
        return await self._get('/session', {'telegramUserId': user_id})

    async def switch_character(self, user_id: str, char_id: int,
                                preset: str = None, world: str = None) -> Dict[str, Any]:
        data = {'telegramUserId': user_id, 'characterId': char_id}
        if preset:
            data['presetName'] = preset
        if world is not None:
            data['worldInfoName'] = world
        return await self._post('/character/switch', data)

    async def set_preset(self, user_id: str, preset_name: str) -> Dict[str, Any]:
        return await self._post('/session/preset', {
            'telegramUserId': user_id,
            'presetName': preset_name
        })

    async def set_worldinfo(self, user_id: str, world_name: str) -> Dict[str, Any]:
        return await self._post('/session/worldinfo', {
            'telegramUserId': user_id,
            'worldInfoName': world_name
        })

    async def send_message(self, user_id: str, message: str, user_name: str, llm_model: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            'telegramUserId': user_id,
            'message': message,
            'user': user_name
        }
        if isinstance(llm_model, str) and llm_model.strip():
            payload['llmModel'] = llm_model.strip()
        return await self._post('/send', payload)

    async def send_message_stream(self, user_id: str, message: str, user_name: str, llm_model: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
        url = f"{self.base_url}{self.api_prefix}/send/stream"
        payload = {
            'telegramUserId': user_id,
            'message': message,
            'user': user_name
        }
        if isinstance(llm_model, str) and llm_model.strip():
            payload['llmModel'] = llm_model.strip()

        async with http_client.stream(
            "POST",
            url,
            json=payload,
            headers={"Accept": "text/event-stream"},
            timeout=None,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue

    async def get_history(self, user_id: str, limit: int = 10, character_id: str = None) -> Dict[str, Any]:
        params = {'telegramUserId': user_id, 'limit': limit}
        if character_id is not None:
            params['characterId'] = character_id
        return await self._get('/history', params)

    async def get_history_summary(self, user_id: str) -> Dict[str, Any]:
        return await self._get('/history/summary', {'telegramUserId': user_id})

    async def clear_history(self, user_id: str) -> Dict[str, Any]:
        return await self._post('/history/clear', {'telegramUserId': user_id})

    async def clear_all_history(self, user_id: str) -> Dict[str, Any]:
        return await self._post('/history/clear/all', {'telegramUserId': user_id})

    async def get_greeting(self, user_id: str, user_name: str) -> Dict[str, Any]:
        return await self._get('/greeting', {
            'telegramUserId': user_id,
            'userName': user_name
        })

    async def switch_greeting(self, user_id: str, direction: str) -> Dict[str, Any]:
        """切换开场白 (next/prev/random)"""
        return await self._post('/greeting/switch', {
            'telegramUserId': user_id,
            'greetingIndex': direction
        })


# Global client
st_client = SillyTavernClient(SILLYTAVERN_URL, PLUGIN_API_BASE)

auth_store = AuthStore(
    TG_AUTH_DB_PATH,
    admin_user_id=ALLOWED_USER_ID,
    registration_enabled_default=TG_REGISTRATION_ENABLED_DEFAULT,
)
auth_store.load_sync()

def is_authorized(user_id: int) -> bool:
    return auth_store.is_allowed(user_id)


def is_admin(user_id: int) -> bool:
    return auth_store.is_admin(user_id)


def get_register_help_text() -> str:
    if ALLOWED_USER_ID == 0:
        return "✅ 当前未启用授权限制（ALLOWED_USER_ID=0）"
    if not auth_store.registration_enabled():
        return "⛔ 当前未开放注册，请联系管理员开通。"
    return (
        "你尚未获得使用权限。\n\n"
        "注册方式：\n"
        "1) 有邀请码：发送 `/register <邀请码>`\n"
        "2) 无邀请码：发送 `/register` 申请（管理员审批）"
    )


_last_register_hint_at: Dict[int, float] = {}


async def maybe_send_register_hint(update: Update) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    user = update.effective_user
    if not user:
        return
    message = update.effective_message
    if not message:
        return
    now = time.monotonic()
    last = _last_register_hint_at.get(user.id, 0.0)
    if now - last < 10.0:
        return
    _last_register_hint_at[user.id] = now
    try:
        await send_text_safe(message.reply_text, get_register_help_text(), parse_mode='Markdown')
    except Exception:
        pass


def get_main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🎭 选择角色", callback_data="menu_characters")],
        [InlineKeyboardButton("📋 选择预设", callback_data="menu_presets")],
        [InlineKeyboardButton("📚 选择世界书", callback_data="menu_worldinfo")],
        [InlineKeyboardButton("📜 查看历史", callback_data="menu_history")],
        [InlineKeyboardButton("🗑️ 清除当前角色历史", callback_data="menu_clear")],
        [InlineKeyboardButton("🧹 一键清除全部历史", callback_data="menu_clear_all")],
        [InlineKeyboardButton("ℹ️ 当前状态", callback_data="menu_status")],
    ]
    keyboard.insert(3, [InlineKeyboardButton("🧠 我的模型", callback_data="menu_my_model")])
    return InlineKeyboardMarkup(keyboard)


async def send_typing_periodically(chat, interval_ms: int) -> None:
    interval_s = max(0.5, interval_ms / 1000.0)
    try:
        while True:
            try:
                await chat.send_action('typing')
            except Exception:
                pass
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        return


async def edit_message_if_changed(message_obj, text: str) -> None:
    try:
        if getattr(message_obj, "text", None) == text:
            return
        await message_obj.edit_text(text)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def edit_message_html_if_changed(message_obj, html_text: str) -> None:
    try:
        await message_obj.edit_text(html_text, parse_mode='HTML', disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        if "Can't parse entities" in str(e):
            safe = re.sub(r"<[^>]+>", "", html_text)
            await edit_message_if_changed(message_obj, safe)
            return
        raise


async def send_long_plain_text(bot, chat_id: int, text: str, *, chunk_size: int = 4000) -> None:
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        await bot.send_message(chat_id=chat_id, text=text[i:i + chunk_size])


def looks_like_preformatted_block(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return (
        "<stausblock" in lowered
        or "<statusblock" in lowered
        or "```xml" in lowered
        or "```" in lowered and ("<stausblock" in lowered or "<statusblock" in lowered)
    )


def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        inner = stripped[3:-3]
        inner = re.sub(r"^\s*[a-zA-Z0-9_-]+\s*\n", "", inner, count=1)
        return inner.strip()
    return text


def _markdown_bold_to_html(escaped: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


def parse_statusblock(text: str) -> Optional[Dict[str, str]]:
    if not text:
        return None
    text = _strip_code_fences(text)
    lowered = text.lower()
    if "<stausblock" not in lowered and "<statusblock" not in lowered:
        return None

    match = re.search(r"<(stausblock|statusblock)>([\s\S]*?)</\1>", text, flags=re.IGNORECASE)
    if not match:
        return None

    inner = match.group(2)
    pairs = re.findall(r"<([^<>/\s]+)>([\s\S]*?)</\1>", inner)
    if not pairs:
        return None

    result: Dict[str, str] = {}
    for tag, value in pairs:
        tag = str(tag).strip()
        value = str(value).strip()
        if not tag:
            continue
        result[tag] = value
    return result or None


def render_statusblock_messages(fields: Dict[str, str]) -> list[str]:
    header_keys = ["天气", "地点", "日期", "时间"]
    body_key = "正文"
    tips_key = "TIPS"

    def line(label: str, value: str) -> str:
        escaped_value = _markdown_bold_to_html(html.escape(value, quote=False))
        return f"<b>{html.escape(label, quote=False)}：</b>{escaped_value}"

    sections: list[str] = []

    header_lines: list[str] = []
    for key in header_keys:
        if fields.get(key):
            header_lines.append(line(key, fields[key]))
    if header_lines:
        sections.append("\n".join(header_lines))

    if fields.get(body_key):
        body = _markdown_bold_to_html(html.escape(fields[body_key], quote=False))
        sections.append(f"<b>正文</b>\n{body}")

    if fields.get(tips_key):
        tips_raw = fields[tips_key].strip()
        tips_lines = [l.strip() for l in tips_raw.splitlines() if l.strip()]
        tips_html = "\n".join(html.escape(l, quote=False) for l in tips_lines)
        sections.append(f"<b>行动建议</b>\n{tips_html}")

    skip = set(header_keys + [body_key, tips_key])
    rest_lines: list[str] = []
    for key, value in fields.items():
        if key in skip:
            continue
        value = str(value).strip()
        if not value:
            continue
        rest_lines.append(line(key, value))
    if rest_lines:
        sections.append("<b>状态</b>\n" + "\n".join(rest_lines))

    messages: list[str] = []
    current = ""
    max_chars = 3500

    for section in sections:
        section = section.strip()
        if not section:
            continue
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            messages.append(current)
            current = ""
        if len(section) <= max_chars:
            current = section
            continue
        buf = ""
        for ln in section.splitlines():
            cand = f"{buf}\n{ln}" if buf else ln
            if len(cand) <= max_chars:
                buf = cand
                continue
            if buf:
                messages.append(buf)
            buf = ln
        if buf:
            current = buf

    if current:
        messages.append(current)
    return messages


async def send_statusblock_html(bot, chat_id: int, text: str) -> bool:
    parsed = parse_statusblock(text)
    if not parsed:
        return False
    messages = render_statusblock_messages(parsed)
    if not messages:
        return False
    for msg in messages:
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML', disable_web_page_preview=True)
    return True


def parse_status_fields_partial(text: str) -> Dict[str, str]:
    text = _strip_code_fences(text or "")
    lowered = text.lower()

    start_tag = None
    if "<stausblock" in lowered:
        start_tag = "stausblock"
    elif "<statusblock" in lowered:
        start_tag = "statusblock"
    if not start_tag:
        return {}

    start_marker = f"<{start_tag}>"
    start_index = lowered.find(start_marker)
    if start_index == -1:
        return {}

    inner = text[start_index + len(start_marker):]
    end_marker = f"</{start_tag}>"
    end_index = inner.lower().find(end_marker)
    if end_index != -1:
        inner = inner[:end_index]

    pairs = re.findall(r"<([^<>/\s]+)>([\s\S]*?)</\1>", inner)
    result: Dict[str, str] = {}
    for tag, value in pairs:
        tag = str(tag).strip()
        if not tag or tag.lower() in ("stausblock", "statusblock"):
            continue
        result[tag] = str(value).strip()
    return result


def extract_partial_between(text: str, start_tag: str, end_tag: str, *, stop_tags: Optional[list[str]] = None) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    start = lowered.find(start_tag.lower())
    if start == -1:
        return None
    start += len(start_tag)
    after = text[start:]
    after_lower = after.lower()

    candidates: list[int] = []
    end_pos = after_lower.find(end_tag.lower())
    if end_pos != -1:
        candidates.append(end_pos)

    if stop_tags:
        for tag in stop_tags:
            p = after_lower.find(tag.lower())
            if p != -1:
                candidates.append(p)

    cut = min(candidates) if candidates else len(after)
    return after[:cut].strip()


def split_text_pages(text: str, *, max_chars: int) -> list[str]:
    if not text:
        return [""]
    pages: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            pages.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars)
        if cut == -1 or cut < int(max_chars * 0.6):
            cut = max_chars
        pages.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    return pages or [""]


def render_status_panel_html(fields: Dict[str, str]) -> str:
    if not fields:
        return "状态读取中…"

    header_order = ["天气", "地点", "日期", "时间"]
    exclude = {"正文"}

    lines: list[str] = []
    for key in header_order:
        if fields.get(key):
            val = _markdown_bold_to_html(html.escape(fields[key], quote=False))
            lines.append(f"<b>{html.escape(key, quote=False)}：</b>{val}")

    other_keys = [k for k in fields.keys() if k not in set(header_order) and k not in exclude]
    other_keys.sort()
    for key in other_keys:
        value = fields.get(key)
        if not value:
            continue
        val = _markdown_bold_to_html(html.escape(str(value), quote=False))
        lines.append(f"<b>{html.escape(key, quote=False)}：</b>{val}")

    max_chars = 3500
    output = ""
    shown = 0
    for ln in lines:
        candidate = f"{output}\n{ln}" if output else ln
        if len(candidate) > max_chars:
            break
        output = candidate
        shown += 1

    if shown < len(lines):
        output += f"\n<b>…</b> 还有 {len(lines) - shown} 项（生成中/稍后发送）"
    return output if output else "状态读取中…"


def render_body_html(body: str) -> str:
    escaped = _markdown_bold_to_html(html.escape(body or "", quote=False))
    return escaped if escaped else "…"


def render_tips_html(tips: str) -> str:
    lines = [l.strip() for l in (tips or "").splitlines() if l.strip()]
    joined = "\n".join(html.escape(l, quote=False) for l in lines)
    return f"<b>行动建议</b>\n{joined}" if joined else "<b>行动建议</b>\n（无）"


def render_full_state_messages(fields: Dict[str, str], *, exclude_keys: Optional[set[str]] = None) -> list[str]:
    exclude_keys = exclude_keys or set()
    items = [(k, v) for k, v in fields.items() if k not in exclude_keys and str(v).strip()]
    if not items:
        return []
    items.sort(key=lambda kv: kv[0])

    blocks: list[str] = []
    current = "<b>状态（完整）</b>\n"
    max_chars = 3500
    for k, v in items:
        line = f"<b>{html.escape(k, quote=False)}：</b>{_markdown_bold_to_html(html.escape(str(v), quote=False))}\n"
        if len(current) + len(line) > max_chars:
            blocks.append(current.rstrip())
            current = "<b>状态（续）</b>\n" + line
        else:
            current += line
    if current.strip():
        blocks.append(current.rstrip())
    return blocks


async def send_preformatted_html(bot, chat_id: int, text: str, *, max_message_chars: int = 3800) -> None:
    if not text:
        return

    escaped_lines = html.escape(text, quote=False).splitlines()
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for line in escaped_lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_message_chars:
            flush()
            if len(line) > max_message_chars:
                for i in range(0, len(line), max_message_chars):
                    chunks.append(line[i:i + max_message_chars])
            else:
                current = line
        else:
            current = candidate

    flush()

    for chunk in chunks:
        payload = f"<pre>\n{chunk}\n</pre>"
        await bot.send_message(chat_id=chat_id, text=payload, parse_mode='HTML')


async def handle_message_streaming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await maybe_send_register_hint(update)
        return

    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "User"
    message = update.message.text

    typing_task = asyncio.create_task(send_typing_periodically(update.message.chat, TELEGRAM_TYPING_INTERVAL_MS))
    try:
        placeholder = await update.message.reply_text(TELEGRAM_STREAM_PLACEHOLDER)

        parts: list[str] = []
        final_message: Optional[str] = None

        last_edit = 0.0
        edit_interval_s = max(0.2, TELEGRAM_STREAM_EDIT_INTERVAL_MS / 1000.0)

        async for event in st_client.send_message_stream(user_id, message, user_name):
            if isinstance(event.get('error'), str) and event['error']:
                raise RuntimeError(event['error'])

            delta = event.get('delta')
            if isinstance(delta, str) and delta:
                parts.append(delta)

            if event.get('done') and isinstance(event.get('message'), str):
                final_message = event['message']

            now = time.monotonic()
            if now - last_edit >= edit_interval_s and parts:
                partial_text = ''.join(parts)
                await edit_message_if_changed(
                    placeholder,
                    partial_text[:4000] if partial_text else TELEGRAM_STREAM_PLACEHOLDER,
                )
                last_edit = now

        if final_message is None:
            final_message = ''.join(parts).strip()

        if not final_message:
            final_message = '...'

        if looks_like_preformatted_block(final_message):
            try:
                await placeholder.delete()
            except Exception:
                await edit_message_if_changed(placeholder, "📄 已发送格式化内容")
            if not await send_statusblock_html(context.bot, update.effective_chat.id, final_message):
                await send_preformatted_html(context.bot, update.effective_chat.id, final_message)
            return

        await edit_message_if_changed(placeholder, final_message[:4000])

        if len(final_message) > 4000:
            for i in range(4000, len(final_message), 4000):
                await update.message.reply_text(final_message[i:i+4000])

    except httpx.HTTPStatusError as e:
        if getattr(e.response, "status_code", None) == 404:
            await handle_message(update, context)
        else:
            await update.message.reply_text(f"? 错误: {e}")
    except httpx.ConnectError:
        await update.message.reply_text("? 无法连接 SillyTavern")
    except httpx.TimeoutException:
        await update.message.reply_text("⏱️ 响应超时，请稍后重试")
    except Exception as e:
        logger.error(f"Streaming message error: {e}")
        await update.message.reply_text(f"? 错误: {e}")
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except Exception:
            pass


# New streaming UI: separate status panel + body stream (HTML, mobile-friendly)
async def handle_message_streaming_ui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await maybe_send_register_hint(update)
        return

    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "User"
    message = update.message.text
    llm_model = auth_store.get_user_llm_model(update.effective_user.id)

    typing_task = asyncio.create_task(send_typing_periodically(update.message.chat, TELEGRAM_TYPING_INTERVAL_MS))
    try:
        status_message = await update.message.reply_text(TELEGRAM_STREAM_PLACEHOLDER)

        buffer = ""
        final_message: Optional[str] = None

        status_mode = False
        body_messages = []
        tips_sent = False

        last_edit = 0.0
        edit_interval_s = max(0.2, TELEGRAM_STREAM_EDIT_INTERVAL_MS / 1000.0)

        async for event in st_client.send_message_stream(user_id, message, user_name, llm_model=llm_model):
            if isinstance(event.get('error'), str) and event['error']:
                raise RuntimeError(event['error'])

            delta = event.get('delta')
            if isinstance(delta, str) and delta:
                buffer += delta

            if event.get('done') and isinstance(event.get('message'), str):
                final_message = event['message']

            now = time.monotonic()
            if now - last_edit < edit_interval_s:
                continue
            if not buffer:
                continue

            lowered = buffer.lower()
            if not status_mode and ("<stausblock" in lowered or "<statusblock" in lowered):
                status_mode = True
                await edit_message_html_if_changed(status_message, "状态读取中…")
                body_messages.append(await update.message.reply_text("正文生成中…"))

            if not status_mode:
                await edit_message_if_changed(
                    status_message,
                    buffer[:4000] if buffer else TELEGRAM_STREAM_PLACEHOLDER,
                )
                last_edit = now
                continue

            fields_partial = parse_status_fields_partial(buffer)
            await edit_message_html_if_changed(status_message, render_status_panel_html(fields_partial))

            if not tips_sent and "</tips>" in lowered:
                tips = extract_partial_between(buffer, "<TIPS>", "</TIPS>")
                if tips is not None:
                    tips_sent = True
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=render_tips_html(tips),
                        parse_mode='HTML',
                        disable_web_page_preview=True,
                    )

            body = extract_partial_between(
                buffer,
                "<正文>",
                "</正文>",
                stop_tags=["<TIPS>", "<变量>", "<秘氛>", "<邪名>", "</stausblock>", "</statusblock>"],
            )
            if body is not None:
                if not body_messages:
                    body_messages.append(await update.message.reply_text("正文生成中…"))
                pages = split_text_pages(body, max_chars=3500)
                while len(body_messages) < len(pages):
                    body_messages.append(await update.message.reply_text("…"))
                for i, page in enumerate(pages):
                    await edit_message_html_if_changed(body_messages[i], f"<b>正文</b>\n{render_body_html(page)}")

            last_edit = now

        if final_message is None:
            final_message = buffer.strip()

        if not final_message:
            final_message = '...'

        if status_mode and looks_like_preformatted_block(final_message):
            full_fields = parse_statusblock(final_message) or {}
            if full_fields:
                await edit_message_html_if_changed(status_message, render_status_panel_html(full_fields))

                body_final = extract_partial_between(
                    final_message,
                    "<正文>",
                    "</正文>",
                    stop_tags=["<TIPS>", "<变量>", "<秘氛>", "<邪名>", "</stausblock>", "</statusblock>"],
                )
                if body_final is not None:
                    if not body_messages:
                        body_messages.append(await update.message.reply_text("…"))
                    pages = split_text_pages(body_final, max_chars=3500)
                    while len(body_messages) < len(pages):
                        body_messages.append(await update.message.reply_text("…"))
                    for i, page in enumerate(pages):
                        await edit_message_html_if_changed(body_messages[i], f"<b>正文</b>\n{render_body_html(page)}")

                for msg in render_full_state_messages(full_fields, exclude_keys={"正文"}):
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=msg,
                        parse_mode='HTML',
                        disable_web_page_preview=True,
                    )

                if full_fields.get("TIPS") and not tips_sent:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=render_tips_html(full_fields["TIPS"]),
                        parse_mode='HTML',
                        disable_web_page_preview=True,
                    )
            return

        if looks_like_preformatted_block(final_message):
            if not await send_statusblock_html(context.bot, update.effective_chat.id, final_message):
                await send_preformatted_html(context.bot, update.effective_chat.id, final_message)
            return

        await edit_message_if_changed(status_message, final_message[:4000])
        if len(final_message) > 4000:
            for i in range(4000, len(final_message), 4000):
                await update.message.reply_text(final_message[i:i+4000])

    except httpx.HTTPStatusError as e:
        if getattr(e.response, "status_code", None) == 404:
            await handle_message(update, context)
        else:
            await update.message.reply_text(f"? 错误: {e}")
    except httpx.ConnectError:
        await update.message.reply_text("? 无法连接 SillyTavern")
    except httpx.TimeoutException:
        await update.message.reply_text("?? 响应超时，请稍后重试")
    except Exception as e:
        logger.error(f"Streaming message error: {e}")
        await update.message.reply_text(f"? 错误: {e}")
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except Exception:
            pass


# ============================================
# Command Handlers
# ============================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await send_text_safe(update.message.reply_text, get_register_help_text(), parse_mode='Markdown')
        return
        await update.message.reply_text("⛔ 无权限使用此机器人")
        return

    await send_text_safe(update.message.reply_text,
        "🎭 **SillyTavern Telegram Bot v2.0**\n\n"
        "支持预设、世界书、完整角色卡\n\n"
        "直接发送消息即可与角色对话\n"
        "使用下方按钮进行设置：",
        reply_markup=get_main_menu(),
        parse_mode='Markdown'
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await send_text_safe(update.message.reply_text, get_register_help_text(), parse_mode='Markdown')
        return
        return

    help_text = """
📖 **SillyTavern Telegram Bot 帮助**

**命令：**
/start - 主菜单
/help - 帮助信息
/status - 当前状态
/chars - 角色列表
/presets - 预设列表
/worlds - 世界书列表
/clear - 清除对话历史
/mymodel - 我的模型（仅对自己生效）
/delmodel - 删除我的模型（恢复默认）

**模型（管理员）：**
/model - 查看/设置默认模型（别名：/llm）

**使用方法：**
1. 选择角色 → 选择预设 → 开始对话
2. 直接发送消息与 AI 角色对话
3. 可选择世界书增强角色设定

**提示：**
- 高端角色卡需要配合适当的预设
- 世界书用于提供额外的设定信息
"""
    await send_text_safe(update.message.reply_text, help_text, parse_mode='Markdown')


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return

    user = update.effective_user
    if not user:
        return

    if ALLOWED_USER_ID == 0:
        await update.message.reply_text("当前未启用授权限制（ALLOWED_USER_ID=0）。")
        return

    if is_authorized(user.id):
        await update.message.reply_text("你已经拥有权限，可以直接对话。")
        return

    if not auth_store.registration_enabled():
        await update.message.reply_text("当前未开放注册，请联系管理员。")
        return

    invite_code = None
    if getattr(context, "args", None):
        invite_code = str(context.args[0]).strip()

    if invite_code:
        ok = await auth_store.redeem_invite(
            user_id=user.id,
            user_name=user.first_name or user.username or "",
            code=invite_code,
            approved_by=ALLOWED_USER_ID,
        )
        if ok:
            await update.message.reply_text("邀请码验证成功，已开通权限。发送任意消息开始对话。")
        else:
            await update.message.reply_text("邀请码无效或已使用。也可以发送 /register 申请审批。")
        return

    created = await auth_store.request_access(user.id, user.first_name or user.username or "")
    if not created:
        await update.message.reply_text("你的申请已存在，请等待管理员审批。")
        return

    await update.message.reply_text("已提交申请，请等待管理员审批。")

    if ALLOWED_USER_ID != 0:
        keyboard = [
            [
                InlineKeyboardButton("通过", callback_data=f"auth_approve_{user.id}"),
                InlineKeyboardButton("拒绝", callback_data=f"auth_reject_{user.id}"),
            ]
        ]
        try:
            await context.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=f"新注册申请：\n- user_id: `{user.id}`\n- name: `{md_escape(user.first_name or user.username or '')}`",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown',
            )
        except Exception as e:
            logger.error(f"Notify admin failed: {e}")


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    code = await auth_store.create_one_time_invite(created_by=update.effective_user.id)
    await send_text_safe(
        update.message.reply_text,
        f"一次性邀请码：`{code}`\n让对方私聊机器人发送：`/register {code}`",
        parse_mode='Markdown',
    )


async def cmd_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    if not getattr(context, "args", None):
        status = "ON" if auth_store.registration_enabled() else "OFF"
        await update.message.reply_text(f"当前注册开关：{status}\n用法：/registration on 或 /registration off")
        return

    arg = str(context.args[0]).strip().lower()
    enabled = arg in ("1", "true", "yes", "y", "on", "open")
    if arg in ("0", "false", "no", "n", "off", "close"):
        enabled = False
    await auth_store.set_registration_enabled(enabled)
    await update.message.reply_text("已更新注册开关：" + ("ON" if enabled else "OFF"))


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    users = auth_store.list_allowed()
    if not users:
        await update.message.reply_text("当前没有已授权用户。")
        return

    lines = [f"已授权用户：{len(users)}"]
    for item in users[:50]:
        uid = item.get("userId")
        name = item.get("userName") or ""
        lines.append(f"- {uid} {name}".strip())
    if len(users) > 50:
        lines.append("...（列表过长已截断）")
    await update.message.reply_text("\n".join(lines))


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    pending = auth_store.list_pending()
    if not pending:
        await update.message.reply_text("当前没有待审批申请。")
        return

    lines = [f"待审批：{len(pending)}"]
    for item in pending[:20]:
        uid = item.get("userId")
        name = item.get("userName") or ""
        lines.append(f"- {uid} {name}".strip())
    await update.message.reply_text("\n".join(lines))


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if not getattr(context, "args", None):
        await update.message.reply_text("用法：/approve <user_id>")
        return
    try:
        target = int(str(context.args[0]).strip())
    except ValueError:
        await update.message.reply_text("user_id 格式错误")
        return

    await auth_store.approve(target, approved_by=update.effective_user.id)
    await update.message.reply_text(f"已通过：{target}")
    try:
        await context.bot.send_message(chat_id=target, text="你的权限已开通，现在可以开始对话。")
    except Exception:
        pass


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if not getattr(context, "args", None):
        await update.message.reply_text("用法：/revoke <user_id>")
        return
    try:
        target = int(str(context.args[0]).strip())
    except ValueError:
        await update.message.reply_text("user_id 格式错误")
        return
    removed = await auth_store.revoke(target)
    await update.message.reply_text(("已移除授权" if removed else "目标不在授权列表") + f"：{target}")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    try:
        if not getattr(context, "args", None):
            result = await st_client.get_plugin_config()
            cfg = result.get("config", {}) if isinstance(result, dict) else {}
            current = cfg.get("llmModel") or "unknown"
            await send_text_safe(update.message.reply_text, f"当前模型：`{current}`\n用法：`/model <模型名>`", parse_mode='Markdown')
            return

        model_name = " ".join(str(a) for a in context.args).strip()
        if not model_name:
            await update.message.reply_text("用法：/model <模型名>")
            return

        updated = await st_client.set_plugin_config({"llmModel": model_name})
        if isinstance(updated, dict) and updated.get("success") is False:
            await update.message.reply_text(f"设置失败：{updated.get('error', 'unknown error')}")
            return

        verify = await st_client.get_plugin_config()
        cfg = verify.get("config", {}) if isinstance(verify, dict) else {}
        current = cfg.get("llmModel") or model_name
        await send_text_safe(update.message.reply_text, f"✅ 已切换模型为：`{current}`", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ 设置模型失败：{e}")


async def cmd_mymodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user:
        return
    if not is_authorized(update.effective_user.id):
        await maybe_send_register_hint(update)
        return

    user_id = update.effective_user.id
    user_model = auth_store.get_user_llm_model(user_id)
    default_model: Optional[str] = None
    try:
        result = await st_client.get_plugin_config()
        cfg = result.get("config", {}) if isinstance(result, dict) else {}
        default_model = cfg.get("llmModel")
    except Exception:
        default_model = None

    default_model = str(default_model).strip() if isinstance(default_model, str) else None
    effective_model = user_model or default_model or "unknown"

    if not getattr(context, "args", None):
        await send_text_safe(
            update.message.reply_text,
            "🧠 **我的模型（仅对你生效）**\n\n"
            f"- 当前：`{md_escape(effective_model)}`\n"
            f"- 我的覆盖：`{md_escape(user_model or '（未设置）')}`\n"
            f"- 默认：`{md_escape(default_model or 'unknown')}`\n\n"
            "用法：\n"
            "- `/mymodel <模型名>` 设置我的模型\n"
            "- `/mymodel clear` 删除我的模型（恢复默认）",
            parse_mode='Markdown',
        )
        return

    arg = " ".join(str(a) for a in context.args).strip()
    if arg.lower() in ("clear", "default", "reset", "del", "delete", "remove", "off", "0", "none"):
        await auth_store.set_user_llm_model(user_id, None)
        await update.message.reply_text("✅ 已删除我的模型设置（恢复默认）。")
        return

    if not arg:
        await update.message.reply_text("用法：/mymodel <模型名> 或 /mymodel clear")
        return

    await auth_store.set_user_llm_model(user_id, arg)
    await send_text_safe(update.message.reply_text, f"✅ 已设置我的模型为：`{md_escape(arg)}`", parse_mode='Markdown')


async def cmd_delmodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if not update.effective_user:
        return
    if not is_authorized(update.effective_user.id):
        await maybe_send_register_hint(update)
        return
    await auth_store.set_user_llm_model(update.effective_user.id, None)
    await update.message.reply_text("✅ 已删除我的模型设置（恢复默认）。")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    await update.message.chat.send_action('typing')
    user_id = str(update.effective_user.id)

    try:
        connected = await st_client.health_check()
        if not connected:
            await update.message.reply_text("❌ 无法连接到 SillyTavern")
            return

        session = await st_client.get_session(user_id)
        s = session.get('session', {})

        text = f"""
✅ **连接正常**

🎭 角色: {s.get('characterName') or '未选择'}
📋 预设: {s.get('presetName') or 'Default'}
📚 世界书: {s.get('worldInfoName') or '无'}
💬 历史: {s.get('historyLength', 0)} 条消息
"""
        user_model = auth_store.get_user_llm_model(update.effective_user.id)
        default_model = None
        try:
            cfg_result = await st_client.get_plugin_config()
            cfg = cfg_result.get("config", {}) if isinstance(cfg_result, dict) else {}
            default_model = cfg.get("llmModel")
        except Exception:
            default_model = None

        default_model = str(default_model).strip() if isinstance(default_model, str) else None
        effective_model = user_model or default_model or "unknown"
        note = "（我的覆盖）" if user_model else "（默认）"
        text += f"\n🧠 模型: `{md_escape(effective_model)}` {note}\n"

        await send_text_safe(update.message.reply_text, text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Status error: {e}")
        await update.message.reply_text(f"❌ 错误: {e}")


async def cmd_chars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await show_characters(update, context, is_callback=False)


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await show_presets(update, context, is_callback=False)


async def cmd_worlds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await show_worldinfo(update, context, is_callback=False)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    try:
        await st_client.clear_history(str(update.effective_user.id))
        await update.message.reply_text("✅ 对话历史已清除")
    except Exception as e:
        await update.message.reply_text(f"❌ 清除失败: {e}")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != 'private':
        return

    if not update.effective_user:
        return

    if not is_authorized(update.effective_user.id):
        await maybe_send_register_hint(update)
        return

    await update.message.reply_text(
        "未识别的命令。\n"
        "可用命令：/start /help /status /chars /presets /worlds /clear /mymodel /delmodel\n"
        "多用户：/register\n"
        "（管理员：/invite /pending /approve /revoke /registration /users）"
    )


# ============================================
# Message Handler
# ============================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await maybe_send_register_hint(update)
        return

    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "User"
    message = update.message.text
    llm_model = auth_store.get_user_llm_model(update.effective_user.id)

    await update.message.chat.send_action('typing')

    try:
        result = await st_client.send_message(user_id, message, user_name, llm_model=llm_model)

        if result.get('success'):
            ai_response = result.get('message', '...')

            if looks_like_preformatted_block(ai_response):
                if not await send_statusblock_html(context.bot, update.effective_chat.id, ai_response):
                    await send_preformatted_html(context.bot, update.effective_chat.id, ai_response)
                return

            # 分割长消息
            if len(ai_response) > 4000:
                for i in range(0, len(ai_response), 4000):
                    await update.message.reply_text(ai_response[i:i+4000])
            else:
                await update.message.reply_text(ai_response)
        else:
            error = result.get('error', 'Unknown error')
            await update.message.reply_text(f"❌ {error}")

    except httpx.ConnectError:
        await update.message.reply_text("❌ 无法连接 SillyTavern")
    except httpx.TimeoutException:
        await update.message.reply_text("⏱️ 响应超时，请稍后重试")
    except Exception as e:
        logger.error(f"Message error: {e}")
        await update.message.reply_text(f"❌ 错误: {e}")


# ============================================
# Callback Query Handlers
# ============================================

async def show_characters(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           is_callback: bool = True) -> None:
    query = update.callback_query if is_callback else None
    if query:
        await query.answer()

    try:
        result = await st_client.get_characters()
        chars = result.get('characters', [])

        if not chars:
            text = "📭 没有可用角色\n请在 SillyTavern 中创建角色"
            if query:
                await query.edit_message_text(text)
            else:
                await update.message.reply_text(text)
            return

        keyboard = []
        for c in chars[:10]:  # 最多 10 个
            name = c.get('name', 'Unknown')[:20]
            keyboard.append([InlineKeyboardButton(
                f"🎭 {name}",
                callback_data=f"char_{c.get('id', 0)}"
            )])

        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="menu_main")])

        text = "👥 **选择角色：**"
        if query:
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                             parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Characters error: {e}")
        text = f"❌ 获取角色失败: {e}"
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)


async def show_presets(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        is_callback: bool = True) -> None:
    query = update.callback_query if is_callback else None
    if query:
        await query.answer()

    try:
        result = await st_client.get_presets()
        presets = result.get('presets', [])

        if not presets:
            text = "📭 没有可用预设"
            if query:
                await query.edit_message_text(text)
            else:
                await update.message.reply_text(text)
            return

        # Store full list to avoid callback_data truncation/collisions
        context.user_data['presets'] = presets

        keyboard = []
        for idx, p in enumerate(presets[:10]):
            keyboard.append([InlineKeyboardButton(
                f"📋 {p[:25]}",
                callback_data=f"preset_idx_{idx}"
            )])

        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="menu_main")])

        text = "📋 **选择预设：**\n\n预设决定了 AI 的行为风格和输出格式"
        if query:
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                             parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Presets error: {e}")
        text = f"❌ 获取预设失败: {e}"
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)


async def show_worldinfo(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          is_callback: bool = True) -> None:
    query = update.callback_query if is_callback else None
    if query:
        await query.answer()

    try:
        result = await st_client.get_worldinfo()
        worlds = result.get('worlds', [])

        # Store full list to avoid callback_data truncation/collisions
        context.user_data['worlds'] = worlds

        keyboard = [[InlineKeyboardButton("❌ 不使用世界书", callback_data="world_none")]]

        for idx, w in enumerate(worlds[:8]):
            keyboard.append([InlineKeyboardButton(
                f"📚 {w[:25]}",
                callback_data=f"world_idx_{idx}"
            )])

        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="menu_main")])

        text = "📚 **选择世界书：**\n\n世界书提供额外的设定和知识"
        if query:
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                             parse_mode='Markdown')

    except Exception as e:
        logger.error(f"WorldInfo error: {e}")
        text = f"❌ 获取世界书失败: {e}"
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)


async def show_my_model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             is_callback: bool = True) -> None:
    query = update.callback_query if is_callback else None
    if query:
        await query.answer()

    user = update.effective_user
    if not user:
        return
    if not is_authorized(user.id):
        await maybe_send_register_hint(update)
        return

    user_model = auth_store.get_user_llm_model(user.id)
    default_model = None
    try:
        result = await st_client.get_plugin_config()
        cfg = result.get("config", {}) if isinstance(result, dict) else {}
        default_model = cfg.get("llmModel")
    except Exception:
        default_model = None

    default_model = str(default_model).strip() if isinstance(default_model, str) else None
    effective_model = user_model or default_model or "unknown"

    models: list[str] = []
    for m in ([default_model] if default_model else []) + TG_MODEL_CHOICES + ([user_model] if user_model else []):
        if not isinstance(m, str):
            continue
        m = m.strip()
        if not m or m in models:
            continue
        if len(m) > 50:
            continue
        models.append(m)

    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for m in models[:10]:
        label = m if len(m) <= 20 else (m[:19] + "…")
        row.append(InlineKeyboardButton(label, callback_data=f"my_model_set:{m}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("♻️ 使用默认", callback_data="my_model_clear")])
    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="menu_main")])

    text = (
        "🧠 **我的模型**（仅对你生效）\n\n"
        f"- 当前：`{md_escape(effective_model)}`\n"
        f"- 我的覆盖：`{md_escape(user_model or '（未设置）')}`\n"
        f"- 默认：`{md_escape(default_model or 'unknown')}`\n\n"
        "点击按钮切换，或用 `/mymodel <模型名>` 设置，`/delmodel` 删除。"
    )

    if query:
        await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await send_text_safe(update.message.reply_text, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)

    try:
        summary = await st_client.get_history_summary(user_id)
        items = summary.get('items', [])

        if not items:
            result = await st_client.get_history(user_id, limit=5)
            messages = result.get('messages', [])
            total = result.get('total', 0)

            if not messages:
                text = "📭 暂无对话记录"
            else:
                text = f"📜 **最近 {len(messages)} 条消息** (共 {total} 条)\n\n"
                for msg in messages:
                    role = "👤" if msg.get('role') == 'user' else "🤖"
                    content = msg.get('content', '')[:80]
                    if len(msg.get('content', '')) > 80:
                        content += '...'
                    text += f"{role} {content}\n\n"

            keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="menu_main")]]
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard))
            return

        context.user_data['history_chars'] = {
            str(item.get('characterId')): str(item.get('characterName') or f"Character {item.get('characterId')}")
            for item in items
        }

        text = "📜 **历史会话（按角色）**\n\n选择一个角色查看对话历史："
        keyboard = []
        for item in items[:12]:
            char_id = item.get('characterId')
            name = str(item.get('characterName') or f"Character {char_id}")
            total = item.get('total', 0)
            keyboard.append([InlineKeyboardButton(f"🎭 {name} ({total})", callback_data=f"hist_{char_id}")])
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="menu_main")])

        await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                             parse_mode='Markdown')

    except Exception as e:
        await query.edit_message_text(f"❌ 获取历史失败: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user_id = str(update.effective_user.id)

    actor_id = update.effective_user.id

    if data.startswith("auth_"):
        await query.answer()
        if not is_admin(actor_id):
            await maybe_send_register_hint(update)
            return

        try:
            _, action, target_str = data.split("_", 2)
            target_id = int(target_str)
        except Exception:
            await query.edit_message_text("无效操作")
            return

        if action == "approve":
            await auth_store.approve(target_id, approved_by=actor_id)
            await query.edit_message_text(f"已通过：{target_id}")
            try:
                await context.bot.send_message(chat_id=target_id, text="你的权限已开通，现在可以开始对话。")
            except Exception:
                pass
            return

        if action == "reject":
            await auth_store.reject(target_id)
            await query.edit_message_text(f"已拒绝：{target_id}")
            try:
                await context.bot.send_message(chat_id=target_id, text="你的申请未通过。如有需要请联系管理员。")
            except Exception:
                pass
            return

        await query.edit_message_text("未知操作")
        return

    if not is_authorized(actor_id):
        await query.answer()
        await maybe_send_register_hint(update)
        return

    # Menu navigation
    if data == "menu_main":
        await query.answer()
        await send_text_safe(query.edit_message_text,
            "🎭 **SillyTavern Telegram Bot**\n\n选择操作：",
            reply_markup=get_main_menu(),
            parse_mode='Markdown'
        )

    elif data == "menu_characters":
        await show_characters(update, context)

    elif data == "menu_presets":
        await show_presets(update, context)

    elif data == "menu_worldinfo":
        await show_worldinfo(update, context)

    elif data == "menu_my_model":
        await show_my_model_menu(update, context)

    elif data == "menu_history":
        await show_history(update, context)

    elif data == "my_model_clear":
        await query.answer()
        await auth_store.set_user_llm_model(actor_id, None)
        await show_my_model_menu(update, context)

    elif data.startswith("my_model_set:"):
        await query.answer()
        model_name = data.split(":", 1)[1].strip()
        if not model_name:
            await query.edit_message_text("模型名为空。")
            return
        await auth_store.set_user_llm_model(actor_id, model_name)
        await show_my_model_menu(update, context)

    elif data.startswith("hist_"):
        await query.answer()
        try:
            char_id = data.split("_", 1)[1]
            name_map = context.user_data.get('history_chars', {})
            char_name = name_map.get(str(char_id)) or f"Character {char_id}"

            result = await st_client.get_history(user_id, limit=5, character_id=str(char_id))
            messages = result.get('messages', [])
            total = result.get('total', 0)

            safe_name = md_escape(char_name)
            if not messages:
                text = f"📜 **{safe_name}**\n\n📭 暂无对话记录"
            else:
                text = f"📜 **{safe_name}**\n\n最近 {len(messages)} 条消息（共 {total} 条）：\n\n"
                for msg in messages:
                    role = "👤" if msg.get('role') == 'user' else "🤖"
                    content = msg.get('content', '')[:120]
                    if len(msg.get('content', '')) > 120:
                        content += '...'
                    text += f"{role} {md_escape(content)}\n\n"

            keyboard = [[
                InlineKeyboardButton("🔙 历史列表", callback_data="menu_history"),
                InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")
            ]]
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')
        except Exception as e:
            await query.edit_message_text(f"❌ 获取历史失败: {e}")

    elif data == "menu_clear":
        await query.answer()
        try:
            await st_client.clear_history(user_id)
            await query.edit_message_text(
                "✅ 已清除当前角色的对话历史",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 返回", callback_data="menu_main")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ 清除失败: {e}")

    elif data == "menu_clear_all":
        await query.answer()
        try:
            await st_client.clear_all_history(user_id)
            await query.edit_message_text(
                "✅ 已清除全部角色的对话历史",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 返回", callback_data="menu_main")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ 清除失败: {e}")

    elif data == "menu_status":
        await query.answer()
        try:
            session = await st_client.get_session(user_id)
            s = session.get('session', {})
            text = f"""
ℹ️ **当前状态**

🎭 角色: {s.get('characterName') or '未选择'}
📋 预设: {s.get('presetName') or 'Default'}
📚 世界书: {s.get('worldInfoName') or '无'}
💬 历史: {s.get('historyLength', 0)} 条
"""
            keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="menu_main")]]
            user_model = auth_store.get_user_llm_model(actor_id)
            default_model = None
            try:
                cfg_result = await st_client.get_plugin_config()
                cfg = cfg_result.get("config", {}) if isinstance(cfg_result, dict) else {}
                default_model = cfg.get("llmModel")
            except Exception:
                default_model = None
            default_model = str(default_model).strip() if isinstance(default_model, str) else None
            effective_model = user_model or default_model or "unknown"
            note = "（我的覆盖）" if user_model else "（默认）"
            text += f"\n🧠 模型: `{md_escape(effective_model)}` {note}\n"

            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                           parse_mode='Markdown')
        except Exception as e:
            await query.edit_message_text(f"❌ 错误: {e}")

    # Character selection
    elif data.startswith("char_"):
        await query.answer()
        try:
            char_id = int(data.split("_")[1])
            user_name = update.effective_user.first_name or "User"

            result = await st_client.switch_character(user_id, char_id)

            if result.get('success'):
                char = result.get('character', {})
                greeting = result.get('greeting')
                greetings_count = result.get('greetingsCount', 1)
                current_index = result.get('currentGreetingIndex', 0)

                safe_name = md_escape(char.get('name') or 'Unknown')
                text = f"✅ 已选择角色: **{safe_name}**\n"
                if greeting:
                    await send_long_plain_text(
                        context.bot,
                        query.message.chat_id,
                        f"💬 开场白 ({current_index + 1}/{greetings_count}):\n{greeting}",
                    )
                    text += f"\n💬 开场白 ({current_index + 1}/{greetings_count}) 已发送"

                keyboard = []
                # 如果有多个开场白，显示切换按钮
                if greetings_count > 1:
                    keyboard.append([
                        InlineKeyboardButton("⬅️", callback_data="greeting_prev"),
                        InlineKeyboardButton("🎲 随机", callback_data="greeting_random"),
                        InlineKeyboardButton("➡️", callback_data="greeting_next")
                    ])
                keyboard.append([InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")])
                await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                     parse_mode='Markdown')
            else:
                await query.edit_message_text("❌ 切换角色失败")

        except Exception as e:
            logger.error(f"Character switch error: {e}")
            await query.edit_message_text(f"❌ 错误: {e}")

    # Greeting swipe (切换开场白)
    elif data.startswith("greeting_"):
        await query.answer()
        try:
            direction = data.split("_")[1]  # prev/next/random
            result = await st_client.switch_greeting(user_id, direction)

            if result.get('success'):
                greeting = result.get('greeting')
                greetings_count = result.get('greetingsCount', 1)
                current_index = result.get('currentGreetingIndex', 0)

                if greeting:
                    await send_long_plain_text(
                        context.bot,
                        query.message.chat_id,
                        f"💬 开场白 ({current_index + 1}/{greetings_count}):\n{greeting}",
                    )
                    text = f"💬 已发送开场白：({current_index + 1}/{greetings_count})"
                else:
                    text = f"💬 开场白：({current_index + 1}/{greetings_count})（无开场白）"

                keyboard = []
                if greetings_count > 1:
                    keyboard.append([
                        InlineKeyboardButton("⬅️", callback_data="greeting_prev"),
                        InlineKeyboardButton("🎲 随机", callback_data="greeting_random"),
                        InlineKeyboardButton("➡️", callback_data="greeting_next")
                    ])
                keyboard.append([InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")])
                await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                     parse_mode='Markdown')
            else:
                await query.edit_message_text("❌ 切换开场白失败")

        except Exception as e:
            logger.error(f"Greeting switch error: {e}")
            await query.edit_message_text(f"❌ 错误: {e}")

    # Preset selection
    elif data.startswith("preset_idx_"):
        await query.answer()
        try:
            idx = int(data.split("_")[-1])
            presets = context.user_data.get('presets', [])
            if idx < 0:
                raise ValueError("Invalid preset index")
            if idx >= len(presets):
                refreshed = await st_client.get_presets()
                presets = refreshed.get('presets', [])
                context.user_data['presets'] = presets
            if idx >= len(presets):
                raise ValueError("Preset list expired, please reopen /presets")

            preset_name = presets[idx]
            await st_client.set_preset(user_id, preset_name)

            text = f"✅ 已选择预设: **{md_escape(preset_name)}**"
            keyboard = [[InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")]]
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Preset switch error: {e}")
            await query.edit_message_text(f"❌ 错误: {e}")

    elif data.startswith("preset_"):
        await query.answer()
        try:
            preset_name = data[7:]  # Remove "preset_" prefix
            await st_client.set_preset(user_id, preset_name)

            text = f"✅ 已选择预设: **{md_escape(preset_name)}**"
            keyboard = [[InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")]]
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Preset switch error: {e}")
            await query.edit_message_text(f"❌ 错误: {e}")

    # WorldInfo selection
    elif data.startswith("world_idx_"):
        await query.answer()
        try:
            idx = int(data.split("_")[-1])
            worlds = context.user_data.get('worlds', [])
            if idx < 0:
                raise ValueError("Invalid world index")
            if idx >= len(worlds):
                refreshed = await st_client.get_worldinfo()
                worlds = refreshed.get('worlds', [])
                context.user_data['worlds'] = worlds
            if idx >= len(worlds):
                raise ValueError("World list expired, please reopen /worlds")

            world_name = worlds[idx]
            await st_client.set_worldinfo(user_id, world_name)

            text = f"✅ 已选择世界书: **{md_escape(world_name)}**"
            keyboard = [[InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")]]
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')
        except Exception as e:
            logger.error(f"WorldInfo switch error: {e}")
            await query.edit_message_text(f"❌ 错误: {e}")

    elif data.startswith("world_"):
        await query.answer()
        try:
            world_name = data[6:]  # Remove "world_" prefix
            if world_name == "none":
                world_name = None
                await st_client.set_worldinfo(user_id, "")
                text = "✅ 已禁用世界书"
            else:
                await st_client.set_worldinfo(user_id, world_name)
                text = f"✅ 已选择世界书: **{md_escape(world_name)}**"

            keyboard = [[InlineKeyboardButton("🔙 返回菜单", callback_data="menu_main")]]
            await send_text_safe(query.edit_message_text, text, reply_markup=InlineKeyboardMarkup(keyboard),
                                 parse_mode='Markdown')

        except Exception as e:
            logger.error(f"WorldInfo switch error: {e}")
            await query.edit_message_text(f"❌ 错误: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return

    builder = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(TG_CONCURRENT_UPDATES)
        .connection_pool_size(TG_CONNECTION_POOL_SIZE)
        .pool_timeout(TG_POOL_TIMEOUT)
    )
    app = builder.build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("registration", cmd_registration))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("llm", cmd_model))
    app.add_handler(CommandHandler("mymodel", cmd_mymodel))
    app.add_handler(CommandHandler("umodel", cmd_mymodel))
    app.add_handler(CommandHandler("delmodel", cmd_delmodel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_status))
    app.add_handler(CommandHandler("stars", cmd_status))
    app.add_handler(CommandHandler("chars", cmd_chars))
    app.add_handler(CommandHandler("characters", cmd_chars))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("worlds", cmd_worlds))
    app.add_handler(CommandHandler("clear", cmd_clear))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Messages
    message_handler = handle_message_streaming_ui if TELEGRAM_STREAM_RESPONSES else handle_message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    # Errors
    app.add_error_handler(error_handler)

    # Start
    if WEBHOOK_URL:
        port = int(os.getenv('PORT', '8443'))
        logger.info(f"Starting webhook on port {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook"
        )
    else:
        logger.info("Starting polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
