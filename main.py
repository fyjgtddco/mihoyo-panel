import io
import os
import json
import asyncio
import aiohttp
import re
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
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

DATA_BASE = "https://raw.githubusercontent.com/EnkaNetwork/API-docs/master/store/"
UI_BASE = "https://enka.network/ui/"
CARD_W, CARD_H = 1100, 520

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
        self.mini_font: Optional[ImageFont.FreeTypeFont] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._download_lock = asyncio.Lock()

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def on_start(self):
        proxy = self.config.get("proxy", "").strip()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=True) if proxy else None
        )
        try:
            await self._ensure_static_data()
        except Exception as e:
            logger.warning(f"静态数据下载失败: {e}")
        try:
            await self._ensure_font()
        except Exception as e:
            logger.warning(f"字体加载失败: {e}")
            self.font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()
            self.mini_font = ImageFont.load_default()
        logger.info("米游社面板插件已启动")

    async def on_stop(self):
        if self.session:
            await self.session.close()
            self.session = None

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
            self.font = ImageFont.truetype(str(font_path), 22)
            self.small_font = ImageFont.truetype(str(font_path), 16)
            self.mini_font = ImageFont.truetype(str(font_path), 13)
            return
        system_fonts = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "C:\\Windows\\Fonts\\msyh.ttc",
        ]
        for sf in system_fonts:
            if os.path.exists(sf):
                self.font = ImageFont.truetype(sf, 22)
                self.small_font = ImageFont.truetype(sf, 16)
                self.mini_font = ImageFont.truetype(sf, 13)
                return
        logger.info("自动下载中文字体...")
        data = await self._fetch(
            "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
        )
        font_path.write_bytes(data)
        self.font = ImageFont.truetype(str(font_path), 22)
        self.small_font = ImageFont.truetype(str(font_path), 16)
        self.mini_font = ImageFont.truetype(str(font_path), 13)

    # ------------------------------------------------------------
    # 本地化
    # ------------------------------------------------------------
    def _localize(self, hash_value) -> str:
        loc = self.static_data.get("loc", {})
        return loc.get("zh-CN", {}).get(str(hash_value), str(hash_value))

    def _get_character_name(self, avatar_id) -> str:
        aid = str(avatar_id)
        chars = self.static_data.get("characters", {})
        if isinstance(chars, list):
            for c in chars:
                if str(c.get("id")) == aid or str(c.get("Id")) == aid:
                    return self._localize(c.get("NameTextMapHash", ""))
        return aid

    def _get_character_icon(self, avatar_id) -> str:
        aid = str(avatar_id)
        chars = self.static_data.get("characters", {})
        if isinstance(chars, list):
            for c in chars:
                if str(c.get("id")) == aid or str(c.get("Id")) == aid:
                    icon = c.get("SideIconName") or c.get("IconName")
                    if icon:
                        return f"{icon}.png"
        # fallback: 用 avatarId 拼
        return f"UI_AvatarIcon_Side_{aid}.png"

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
    # 查询
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

        # ============ 关键：根据游戏提取角色列表 ============
        player_nickname = "未知"
        avatar_list: List[dict] = []

        if game == "genshin":
            # 原神: playerInfo + avatarInfoList
            player_info = data.get("playerInfo", {})
            player_nickname = player_info.get("nickname", "未知")
            avatar_list = data.get("avatarInfoList", [])

        elif game == "hsr":
            # 星铁: detailInfo.avatarDetailList
            detail = data.get("detailInfo", {})
            player_nickname = detail.get("nickname", "未知")
            avatar_list = detail.get("avatarDetailList", [])

        elif game == "zzz":
            # 绝区零: PlayerInfo.ShowcaseDetail.AvatarList
            player_info = data.get("PlayerInfo", {})
            showcase = player_info.get("ShowcaseDetail", {})
            player_nickname = player_info.get("Nickname", "未知")
            avatar_list = showcase.get("AvatarList", [])

        if not avatar_list:
            return await self._reply(event, f"玩家 {player_nickname} 的角色展柜为空或未公开。")

        await self._reply(event, f"正在生成 {player_nickname} 的{GAME_NAMES[game]}角色面板...")

        for i, ava in enumerate(avatar_list):
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
    # 绘图（美化版）
    # ------------------------------------------------------------
    async def _draw_card(self, ava: dict, game: str) -> Image.Image:
        # ---------- 统一提取字段 ----------
        if game == "genshin":
            avatar_id = str(ava.get("avatarId", ""))
            level = int(ava.get("propMap", {}).get("4001", {}).get("val", 1))
            cons = len(ava.get("talentIdList", []))
            fetter = ava.get("fetterInfo", {}).get("expLevel", 0)
            fight_props = ava.get("fightPropMap", {})
            equips = ava.get("equipList", [])
            skill_map = ava.get("skillLevelMap", {})
            weapon_field = "weapon"
            reliquary_field = "reliquary"

        elif game == "hsr":
            avatar_id = str(ava.get("avatarId", ""))
            level = int(ava.get("level", 1))
            cons = ava.get("rank", 0)
            fetter = 0
            fight_props = {}
            for prop in ava.get("properties", []):
                fight_props[str(prop.get("property_type", 0))] = prop.get("final", 0)
            equips = []
            if ava.get("equipment"):
                equips.append(ava["equipment"])
            for relic in ava.get("relicList", []):
                equips.append(relic)
            skill_map = {}
            for sk in ava.get("skillTreeList", []):
                skill_map[str(sk.get("pointId", ""))] = sk.get("level", 1)
            weapon_field = "equipment"
            reliquary_field = "relic"

        elif game == "zzz":
            avatar_id = str(ava.get("Id", ""))
            level = int(ava.get("Level", 1))
            cons = ava.get("MindscapeLevel", 0)
            fetter = 0
            fight_props = {}
            for prop in ava.get("Properties", []):
                fight_props[str(prop.get("PropertyId", 0))] = prop.get("Final", 0)
            equips = []
            equip_list = ava.get("EquippedList", [])
            if isinstance(equip_list, list):
                equips = equip_list
            weapon = ava.get("Weapon")
            if weapon:
                equips.insert(0, weapon)
            skill_map = {}
            for sk in ava.get("SkillLevelList", []):
                skill_map[str(sk.get("SkillId", ""))] = sk.get("Level", 1)
            weapon_field = "weapon"
            reliquary_field = "reliquary"

        # ---------- 立绘 ----------
        icon_url = UI_BASE + self._get_character_icon(avatar_id)
        try:
            icon_data = await self._fetch(icon_url)
            avatar_img = Image.open(io.BytesIO(icon_data)).convert("RGBA")
        except Exception:
            avatar_img = Image.new("RGBA", (300, 400), (40, 40, 50, 255))

        avatar_img = avatar_img.resize((280, 420), Image.LANCZOS)

        # ---------- 画布 ----------
        bg_color = (18, 22, 35, 255)  # 深蓝黑底
        card = Image.new("RGBA", (CARD_W, CARD_H), bg_color)
        draw = ImageDraw.Draw(card)

        # 立绘区域背景
        draw.rectangle([(10, 10), (300, CARD_H - 10)], fill=(25, 30, 45, 255), outline=(60, 65, 85, 255), width=1)
        avatar_x = 10 + (290 - 280) // 2
        avatar_y = 10 + (CARD_H - 20 - 420) // 2
        card.paste(avatar_img, (avatar_x, avatar_y), avatar_img)

        # 分隔线
        sep_x = 315
        draw.line([(sep_x, 20), (sep_x, CARD_H - 20)], fill=(70, 75, 100, 255), width=1)

        # ---------- 中间信息 ----------
        tx = sep_x + 20
        y = 20
        name = self._get_character_name(avatar_id)

        # 名字 + 等级
        draw.text((tx, y), f"{name}", fill=(255, 255, 255, 255), font=self.font)
        lv_text = f"Lv.{level}"
        lv_w = self.small_font.getbbox(lv_text)[2]
        draw.text((tx + 180, y + 3), lv_text, fill=(200, 200, 100, 255), font=self.small_font)
        y += 32

        # 好感 / 星魂
        if game == "genshin":
            draw.text((tx, y), f"好感 Lv.{fetter}", fill=(180, 180, 200, 255), font=self.small_font)
        draw.text((tx + 180, y), f"命座 {cons}/6", fill=(255, 200, 100, 255), font=self.small_font)
        y += 28

        # ---------- 属性 ----------
        key_props = {
            "hp": "生命", "atk": "攻击", "def": "防御",
            "spd": "速度", "critRate": "暴击", "critDmg": "爆伤",
            "breakUp": "击破", "enMax": "充能",
        }
        # 原神: 1-生命基 4-攻击基 7-防御基 2-生命 5-攻击 8-防御 20-暴击 22-爆伤 23-充能 28-精通
        if game == "genshin":
            prop_config = [
                ("生命值", "2"), ("攻击力", "5"), ("防御力", "8"),
                ("暴击率", "20"), ("暴击伤害", "22"),
                ("充能效率", "23"), ("元素精通", "28"),
            ]
            for label, pid in prop_config:
                v = fight_props.get(pid)
                if v is not None:
                    if pid in ("20", "22", "23"):
                        text = f"{label}: {v*100:.1f}%"
                    else:
                        text = f"{label}: {v:.0f}"
                    draw.text((tx, y), text, fill=(200, 210, 230, 255), font=self.mini_font)
                    y += 20

        elif game == "hsr":
            # 星铁属性可能在 properties 数组里，property_type 来区分
            # 常见 property_type: 1=hp, 2=atk, 3=def, 4=spd, 5=critRate, 6=critDmg, 9=break, 11=energy
            prop_config = [
                ("生命", "1"), ("攻击", "2"), ("防御", "3"), ("速度", "4"),
                ("暴击率", "5"), ("暴击伤害", "6"), ("击破特攻", "9"), ("能量上限", "11"),
            ]
            for label, pid in prop_config:
                v = fight_props.get(pid)
                if v is not None:
                    if pid in ("5", "6"):
                        text = f"{label}: {v*100:.1f}%"
                    else:
                        text = f"{label}: {v:.0f}"
                    draw.text((tx, y), text, fill=(200, 210, 230, 255), font=self.mini_font)
                    y += 20

        elif game == "zzz":
            prop_config = [
                ("生命", "1"), ("攻击", "2"), ("防御", "3"),
                ("暴击率", "5"), ("暴击伤害", "6"), ("穿透", "7"),
            ]
            for label, pid in prop_config:
                v = fight_props.get(pid)
                if v is not None:
                    if pid in ("5", "6"):
                        text = f"{label}: {v*100:.1f}%"
                    else:
                        text = f"{label}: {v:.0f}"
                    draw.text((tx, y), text, fill=(200, 210, 230, 255), font=self.mini_font)
                    y += 20

        # ---------- 技能 ----------
        if skill_map:
            y += 5
            draw.text((tx, y), "行迹:", fill=(180, 190, 210, 255), font=self.mini_font)
            skill_items = list(skill_map.items())[:5]
            for sk_id, sk_lv in skill_items:
                draw.text((tx + 50, y), f"{sk_id}: Lv.{sk_lv}", fill=(160, 170, 190, 255), font=self.mini_font)
                y += 18

        # ---------- 右侧：装备 ----------
        rx = 650
        ry = 20
        draw.line([(rx - 10, 20), (rx - 10, CARD_H - 20)], fill=(70, 75, 100, 255), width=1)

        # 区分武器和圣遗物
        wp = next((e for e in equips if e.get("flat", {}).get("itemType") == "ITEM_WEAPON" or weapon_field in e), None)
        arts = [e for e in equips if e.get("flat", {}).get("itemType") == "ITEM_RELIQUARY" or reliquary_field in e]

        # 武器
        if wp:
            if weapon_field in wp:
                # 星铁/绝区零格式
                wdata = wp[weapon_field]
                wname = self._localize(wdata.get("nameTextHashMap", wp.get("flat", {}).get("nameTextHashMap", "")))
                wlv = wdata.get("level", wp.get("flat", {}).get("level", 1)) if isinstance(wdata, dict) else wp.get("flat", {}).get("level", 1)
            else:
                wname = self._localize(wp.get("flat", {}).get("nameTextHashMap", ""))
                wlv = wp.get("weapon", {}).get("level", 1)
            draw.text((rx, ry), f"专武: {wname}", fill=(255, 220, 100, 255), font=self.small_font)
            ry += 24
            draw.text((rx + 10, ry), f"Lv.{wlv}", fill=(200, 200, 200, 255), font=self.mini_font)
            ry += 24

        # 圣遗物/遗器/驱动盘
        for art in arts[:6]:
            flat = art.get("flat", {})
            # 套装名
            set_hash = flat.get("setNameTextHashMap", "")
            set_name = self._localize(set_hash) if set_hash else "套装"
            # 主属性
            main_stat = flat.get("reliquaryMainstat", {}) or flat.get("mainStat", {})
            main_name = self._localize(main_stat.get("mainPropId", "")) or "主属性"
            main_val = main_stat.get("propValue", main_stat.get("Value", 0))
            lv = art.get("reliquary", {}).get("level", art.get(reliquary_field, {}).get("level", 1))
            if isinstance(lv, dict):
                lv = 1
            et = flat.get("equipType", flat.get("type", ""))
            rank = flat.get("rankLevel", art.get(reliquary_field, {}).get("rarity", ""))

            draw.text((rx, ry), f"{set_name} {et} +{lv}", fill=(200, 180, 100, 255), font=self.mini_font)
            ry += 18
            draw.text((rx + 15, ry), f"主: {main_name} +{main_val}", fill=(220, 220, 220, 255), font=self.mini_font)
            ry += 18
            subs = flat.get("reliquarySubstats", []) or art.get(reliquary_field, {}).get("substats", [])
            sub_texts = []
            for s in subs[:4]:
                sid = s.get("appendPropId", "") or s.get("key", "")
                sv = s.get("propValue", s.get("Value", 0))
                sub_texts.append(f"{self._localize(sid)}+{sv}")
            if sub_texts:
                draw.text((rx + 15, ry), " ".join(sub_texts), fill=(160, 160, 180, 255), font=self.mini_font)
                ry += 18
            ry += 4

        return card