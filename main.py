import os
import json
import asyncio
import aiohttp
import re
from pathlib import Path
from typing import Dict, Any, Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image as MsgImage, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# ------------------------------------------------------------
# 常量
# ------------------------------------------------------------
GAME_NAMES = {
    "genshin": "原神",
    "hsr": "星穹铁道",
    "zzz": "绝区零"
}

# 卡片图片端点
CARD_URLS = {
    "genshin": "https://enka.network/card/{uid}",
    "hsr": "https://enka.network/hsr/card/{uid}",
    "zzz": "https://enka.network/zzz/card/{uid}"
}

BIND_KEYWORDS = {
    "原神": "genshin",
    "星铁": "hsr",
    "崩铁": "hsr",
    "绝区零": "zzz",
}

QUERY_KEYWORDS = {
    "查询原神面板": "genshin",
    "查询星铁面板": "hsr",
    "查询崩铁面板": "hsr",
    "查询绝区零面板": "zzz",
}

UNBIND_KEYWORDS = {
    "解绑原神": "genshin",
    "解绑星铁": "hsr",
    "解绑崩铁": "hsr",
    "解绑绝区零": "zzz",
}


class MihoyoPanel(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.data_dir = plugin_data_path
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.binds_file = self.data_dir / "binds.json"
        self.session: Optional[aiohttp.ClientSession] = None

    async def on_start(self):
        proxy = self.config.get("proxy", "").strip()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=True) if proxy else None
        )
        logger.info("米游社面板插件已启动（Enka 图片模式）")

    async def on_stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    # ------------------------------------------------------------
    # 绑定数据读写
    # ------------------------------------------------------------
    def _get_binds(self) -> dict:
        if not self.binds_file.exists():
            return {}
        return json.loads(self.binds_file.read_text(encoding="utf-8"))

    def _save_binds(self, binds: dict):
        self.binds_file.write_text(json.dumps(binds, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_uid(self, event: AstrMessageEvent, game: str) -> Optional[str]:
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        user_id = str(event.message_obj.sender.user_id)
        return self._get_binds().get(group_id, {}).get(user_id, {}).get(game)

    # ------------------------------------------------------------
    # 消息拦截
    # ------------------------------------------------------------
    @filter.on_llm_request()
    async def handle_command(self, event: AstrMessageEvent, *args, **kwargs):
        text = event.message_str.strip()

        for kw, game in UNBIND_KEYWORDS.items():
            if f"/{kw}" in text:
                event.stop_event()
                await self._unbind(event, game)
                return

        for kw, game in BIND_KEYWORDS.items():
            m = re.search(rf"/{kw}\s+(\d+)", text)
            if m:
                event.stop_event()
                await self._bind(event, game, m.group(1))
                return

        for kw, game in QUERY_KEYWORDS.items():
            if f"/{kw}" in text:
                event.stop_event()
                await self._query(event, game)
                return

    async def _reply(self, event: AstrMessageEvent, msg: str):
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain([Plain(msg)])
        )

    # ------------------------------------------------------------
    # 绑定 / 解绑
    # ------------------------------------------------------------
    async def _bind(self, event: AstrMessageEvent, game: str, uid: str):
        if not uid.isdigit():
            return await self._reply(event, "❌ UID 必须为纯数字")
        gid = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        uid_key = str(event.message_obj.sender.user_id)
        binds = self._get_binds()
        binds.setdefault(gid, {}).setdefault(uid_key, {})[game] = uid
        self._save_binds(binds)
        await self._reply(event, f"✅ {GAME_NAMES[game]} UID 绑定成功：{uid}")

    async def _unbind(self, event: AstrMessageEvent, game: str):
        gid = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        uid_key = str(event.message_obj.sender.user_id)
        binds = self._get_binds()
        user_binds = binds.get(gid, {}).get(uid_key, {})
        if game in user_binds:
            del user_binds[game]
            self._save_binds(binds)
            await self._reply(event, f"✅ 已解绑{GAME_NAMES[game]}账号")
        else:
            await self._reply(event, f"❌ 你还没有绑定{GAME_NAMES[game]}账号")

    # ------------------------------------------------------------
    # 查询（改用 Enka 卡片图片）
    # ------------------------------------------------------------
    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            proxy = self.config.get("proxy", "").strip()
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True) if proxy else None
            )

    async def _query(self, event: AstrMessageEvent, game: str):
        await self._ensure_session()

        uid = self._get_uid(event, game)
        if not uid:
            return await self._reply(event, f"❌ 请先用 /原神 或 /星铁 <UID> 绑定你的{GAME_NAMES[game]}账号")

        card_url = CARD_URLS[game].format(uid=uid)
        proxy = self.config.get("proxy", "").strip() or None

        try:
            async with self.session.get(card_url, proxy=proxy, allow_redirects=True) as resp:
                if resp.status != 200:
                    await self._reply(event, f"❌ Enka 卡片服务返回状态 {resp.status}，可能展柜未公开或 UID 无效")
                    return

                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type:
                    # 可能返回了错误 HTML
                    body = await resp.text()
                    logger.warning(f"卡片端点返回非图片内容: {content_type}, 前200字符: {body[:200]}")
                    await self._reply(event, f"❌ Enka 卡片服务未返回图片，请稍后重试")
                    return

                # 保存图片
                img_data = await resp.read()
                tmp_path = self.data_dir / f"card_{game}_{uid}.png"
                tmp_path.write_bytes(img_data)

                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([MsgImage.fromFileSystem(str(tmp_path))])
                )
                os.remove(tmp_path)

        except asyncio.TimeoutError:
            await self._reply(event, "❌ 请求超时，Enka 服务响应慢，请稍后再试")
        except Exception as e:
            logger.error(f"获取卡片图片失败: {e}", exc_info=True)
            await self._reply(event, f"❌ 获取面板图片失败：{e}")