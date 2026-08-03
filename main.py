import io
import os
import json
import asyncio
import aiohttp
import re
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image as MsgImage, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# ------------------------------------------------------------
# 常量
# ------------------------------------------------------------
GAME_ENDPOINTS = {
    "genshin": "/uid/",
    "hsr": "/hsr/uid/",
    "zzz": "/zzz/uid/"
}

GAME_NAMES = {
    "genshin": "原神",
    "hsr": "星穹铁道",
    "zzz": "绝区零"
}

# 绑定指令: /原神 123456
BIND_KEYWORDS = {
    "原神": "genshin",
    "星铁": "hsr",
    "崩铁": "hsr",
    "绝区零": "zzz",
}

# 查询指令: /查询原神面板
QUERY_KEYWORDS = {
    "查询原神面板": "genshin",
    "查询星铁面板": "hsr",
    "查询崩铁面板": "hsr",
    "查询绝区零面板": "zzz",
}

# 解绑指令: /解绑原神
UNBIND_KEYWORDS = {
    "解绑原神": "genshin",
    "解绑星铁": "hsr",
    "解绑崩铁": "hsr",
    "解绑绝区零": "zzz",
}

DATA_BASE = "https://raw.githubusercontent.com/EnkaNetwork/API-docs/master/store/"
UI_BASE = "https://enka.network/ui/"
CARD_W, CARD_H = 1200, 700
LEFT_W = 400

STATIC_FILES = [
    "loc.json",
    "characters.json",
    "artifacts.json",
    "weapons.json",
    "hsr/relics.json",
    "hsr/light_cones.json",
    "zzz/equipment.json",
    "zzz/weapons.json"
]


class MihoyoPanel(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.data_dir = plugin_data_path
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.font_dir = self.data_dir / "fonts"
        self.font_dir.mkdir(exist_ok=True)
        self.binds_file = self.data_dir / "binds.json"
        self.static_data: Dict[str, Any] = {}
        self.font: Optional[ImageFont.FreeTypeFont] = None
        self.small_font: Optional[ImageFont.FreeTypeFont] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._download_lock = asyncio.Lock()

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def on_start(self):
        # 1. 先建 session，保证后续请求可用
        proxy = self.config.get("proxy", "").strip()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=True) if proxy else None
        )
        # 2. 后台下载数据，失败也不影响使用
        try:
            await self._ensure_static_data()
        except Exception as e:
            logger.warning(f"静态数据下载失败（可能影响本地化显示）: {e}")
        try:
            await self._ensure_font()
        except Exception as e:
            logger.warning(f"字体加载失败（可能中文乱码）: {e}")
            self.font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()
        logger.info("米游社面板插件已启动")

    async def on_stop(self):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------
    # 网络 & 数据
    # ------------------------------------------------------------
    async def _fetch(self, url: str) -> bytes:
        proxy = self.config.get("proxy", "").strip()
        kwargs = {"proxy": proxy} if proxy else {}
        async with self.session.get(url, **kwargs) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _ensure_static_data(self):
        if self.static_data:
            return
        async with self._download_lock:
            if self.static_data:
                return
            for filename in STATIC_FILES:
                filepath = self.cache_dir / filename.replace("/", "_")
                if not filepath.exists():
                    logger.info(f"下载静态数据: {DATA_BASE + filename}")
                    data = await self._fetch(DATA_BASE + filename)
                    filepath.write_bytes(data)
                if filepath.exists():
                    key = filename.replace("/", "_").rsplit(".", 1)[0]
                    self.static_data[key] = json.loads(filepath.read_text(encoding="utf-8"))

    async def _ensure_font(self):
        font_path = self.font_dir / "NotoSansCJKsc-Regular.otf"
        if font_path.exists():
            self.font = ImageFont.truetype(str(font_path), 24)
            self.small_font = ImageFont.truetype(str(font_path), 18)
            return
        system_fonts = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "C:\\Windows\\Fonts\\msyh.ttc",
        ]
        for sf in system_fonts:
            if os.path.exists(sf):
                self.font = ImageFont.truetype(sf, 24)
                self.small_font = ImageFont.truetype(sf, 18)
                return
        logger.info("自动下载中文字体...")
        data = await self._fetch(
            "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
        )
        font_path.write_bytes(data)
        self.font = ImageFont.truetype(str(font_path), 24)
        self.small_font = ImageFont.truetype(str(font_path), 18)

    # ------------------------------------------------------------
    # 本地化
    # ------------------------------------------------------------
    def _localize(self, hash_value) -> str:
        loc = self.static_data.get("loc", {})
        return loc.get("zh-CN", {}).get(str(hash_value), str(hash_value))

    def _get_character_name(self, avatar_id: str) -> str:
        chars = self.static_data.get("characters", {})
        if isinstance(chars, list):
            for c in chars:
                if str(c.get("id")) == str(avatar_id):
                    return self._localize(c.get("NameTextMapHash", ""))
        return avatar_id

    def _get_character_icon(self, avatar_id: str) -> str:
        chars = self.static_data.get("characters", {})
        if isinstance(chars, list):
            for c in chars:
                if str(c.get("id")) == str(avatar_id):
                    icon = c.get("SideIconName") or c.get("IconName")
                    if icon:
                        return f"{icon}.png"
        return f"UI_AvatarIcon_Side_{avatar_id}.png"

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

        # 解绑
        for kw, game in UNBIND_KEYWORDS.items():
            if f"/{kw}" in text:
                event.stop_event()
                await self._unbind(event, game)
                return

        # 绑定
        for kw, game in BIND_KEYWORDS.items():
            m = re.search(rf"/{kw}\s+(\d+)", text)
            if m:
                event.stop_event()
                await self._bind(event, game, m.group(1))
                return

        # 查询
        for kw, game in QUERY_KEYWORDS.items():
            if f"/{kw}" in text:
                event.stop_event()
                await self._query(event, game)
                return

    # ------------------------------------------------------------
    # 发送帮助
    # ------------------------------------------------------------
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
    # 查询
    # ------------------------------------------------------------
    async def _query(self, event: AstrMessageEvent, game: str):
        uid = self._get_uid(event, game)
        if not uid:
            return await self._reply(event, f"❌ 请先用 /{list(BIND_KEYWORDS.keys())[0]} <UID> 绑定你的{GAME_NAMES[game]}账号")

        url = f"https://enka.network/api{GAME_ENDPOINTS[game]}{uid}"
        try:
            proxy = self.config.get("proxy", "").strip() or None
            async with self.session.get(url, proxy=proxy) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    return await self._reply(event, f"❌ 查询失败（{resp.status}）：{txt[:100]}")
                data = await resp.json()
        except Exception as e:
            logger.error(f"网络请求失败: {e}")
            return await self._reply(event, f"❌ 网络请求失败: {e}")

        player = data.get("playerInfo", {})
        avatars = data.get("avatarInfoList", [])
        if not avatars:
            return await self._reply(event, f"玩家 {player.get('nickname', '未知')} 的角色展柜为空或未公开。")

        await self._reply(event, f"正在生成 {player.get('nickname', '未知')} 的{GAME_NAMES[game]}角色面板...")

        for i, ava in enumerate(avatars):
            try:
                img = await self._draw_card(ava, game)
                tmp = self.data_dir / f"tmp_{game}_{uid}_{i}.png"
                img.save(str(tmp), format="PNG")
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([MsgImage.fromFileSystem(str(tmp))])
                )
                os.remove(tmp)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"绘制角色 {i} 失败: {e}", exc_info=True)
                await self._reply(event, f"⚠️ 第 {i+1} 个角色生成失败：{e}")

    # ------------------------------------------------------------
    # 绘图
    # ------------------------------------------------------------
    async def _draw_card(self, ava: dict, game: str) -> Image.Image:
        aid = str(ava.get("avatarId", ""))
        name = self._get_character_name(aid)
        level = ava.get("propMap", {}).get("4001", {}).get("val", 1)
        cons = len(ava.get("talentIdList", []))
        fetter = ava.get("fetterInfo", {}).get("expLevel", 0)
        fp = ava.get("fightPropMap", {})
        equips = ava.get("equipList", [])

        # 立绘
        icon_url = UI_BASE + self._get_character_icon(aid)
        try:
            icon_data = await self._fetch(icon_url)
            icon = Image.open(io.BytesIO(icon_data)).convert("RGBA")
        except Exception:
            icon = Image.new("RGBA", (256, 256), (50, 50, 50, 255))
        th = min(600, int(CARD_H * 0.85))
        tw = int(th * icon.width / icon.height)
        icon = icon.resize((tw, th), Image.LANCZOS)

        card = Image.new("RGBA", (CARD_W, CARD_H), (30, 30, 40, 255))
        draw = ImageDraw.Draw(card)
        card.paste(icon, (20, (CARD_H - th) // 2), icon)

        lx = LEFT_W
        draw.line([(lx, 30), (lx, CARD_H - 30)], fill=(80, 80, 100, 255), width=2)

        tx, y = lx + 30, 50
        draw.text((tx, y), f"{name}  Lv.{int(level)}  好感{fetter}", fill=(255, 255, 255, 255), font=self.font)
        y += 45
        draw.text((tx, y), f"命座: {cons}/6", fill=(200, 200, 200, 255), font=self.small_font)
        y += 30

        sm = ava.get("skillLevelMap", {})
        if sm:
            st = "  ".join([f"技能{k}: Lv.{v}" for k, v in sm.items()])
            draw.text((tx, y), f"行迹: {st}", fill=(200, 200, 200, 255), font=self.small_font)
            y += 30

        # 武器
        wp = next((e for e in equips if e.get("flat", {}).get("itemType") == "ITEM_WEAPON"), None)
        if wp:
            f = wp["flat"]
            wname = self._localize(f.get("nameTextHashMap", ""))
            wlv = wp.get("weapon", {}).get("level", 1)
            wr = list(wp.get("weapon", {}).get("affixMap", {}).values())
            wr = wr[0] + 1 if wr else 1
            draw.text((tx, y), f"专武: {wname} Lv.{wlv} 精炼{wr}", fill=(255, 220, 100, 255), font=self.small_font)
            y += 30

        # 战斗属性
        kp = {
            "1": "基础生命", "2": "生命值", "3": "生命%", "4": "基础攻击",
            "5": "攻击力", "6": "攻击%", "7": "基础防御", "8": "防御力",
            "9": "防御%", "20": "暴击率", "22": "暴击伤害",
            "23": "充能效率", "28": "元素精通",
        }
        pl = []
        for pid, lb in kp.items():
            v = fp.get(pid)
            if v is not None:
                pl.append(f"{lb}: {v*100:.1f}%" if pid in ("20","22","23","3","6","9") else f"{lb}: {v:.0f}")
        draw.text((tx, y), "  |  ".join(pl), fill=(180, 180, 200, 255), font=self.small_font)
        y += 40

        # 圣遗物
        rx, ry = 850, 50
        arts = [e for e in equips if e.get("flat", {}).get("itemType") == "ITEM_RELIQUARY"]
        draw.text((rx, ry), "仪器:", fill=(255, 255, 255, 255), font=self.font)
        ry += 40
        for art in arts[:5]:
            f = art["flat"]
            sn = self._localize(f.get("setNameTextHashMap", "")) or "未知套装"
            mi = art.get("reliquary", {}).get("mainPropId", "")
            mn = self._localize(mi) if mi else "主属性"
            mv = f.get("reliquaryMainstat", {}).get("propValue", 0)
            alv = art.get("reliquary", {}).get("level", 1)
            et = f.get("equipType", "")
            draw.text((rx, ry), f"{sn} {et} Lv.{alv}", fill=(200, 180, 100, 255), font=self.small_font)
            ry += 25
            draw.text((rx+20, ry), f"主: {mn} +{mv}", fill=(220, 220, 220, 255), font=self.small_font)
            ry += 25
            ss = "  ".join([f"{self._localize(s.get('appendPropId',''))} +{s.get('propValue',0)}" for s in f.get("reliquarySubstats", [])])
            draw.text((rx+20, ry), ss, fill=(180, 180, 180, 255), font=self.small_font)
            ry += 30

        return card