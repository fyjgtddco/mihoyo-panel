import io
import os
import json
import asyncio
import aiohttp
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image as MsgImage, MessageChain

# ------------------------------------------------------------
# 常量定义
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

# 本地化仓库基础地址
DATA_BASE = "https://raw.githubusercontent.com/EnkaNetwork/API-docs/master/store/"

# 需要下载的静态数据文件
STATIC_FILES = [
    "loc.json",
    "characters.json",
    "artifacts.json",   # 原神圣遗物
    "weapons.json",     # 原神武器
    "hsr/relics.json",  # 星铁遗器
    "hsr/light_cones.json",  # 星铁光锥
    "zzz/equipment.json",    # 绝区零驱动盘
    "zzz/weapons.json"       # 绝区零音擎
]

# 立绘 CDN 基础
UI_BASE = "https://enka.network/ui/"

# 图片尺寸
CARD_W, CARD_H = 1200, 700
LEFT_W = 400  # 立绘区域宽度


class MihoyoPanel(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(context.get_data_dir()) / "mihoyo-panel"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.font_dir = self.data_dir / "fonts"
        self.font_dir.mkdir(exist_ok=True)
        self.static_data: Dict[str, Any] = {}
        self.font: Optional[ImageFont.FreeTypeFont] = None
        self.small_font: Optional[ImageFont.FreeTypeFont] = None
        self.binds_file = self.data_dir / "binds.json"
        self.session: Optional[aiohttp.ClientSession] = None
        # 网络锁，避免并发下载
        self._download_lock = asyncio.Lock()

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def on_start(self):
        """插件启动时初始化网络会话和静态数据"""
        # 配置代理
        proxy = self.config.get("proxy", "").strip()
        connector = None
        if proxy:
            connector = aiohttp.connector.TCPConnector(force_close=True)
        self.session = aiohttp.ClientSession(connector=connector)
        # 后台下载必需文件
        await self._ensure_static_data()
        await self._ensure_font()
        logger.info("米游社面板插件已启动")

    async def on_stop(self):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------
    async def _fetch(self, url: str) -> bytes:
        """通过代理（如果配置）发起 GET 请求，返回原始字节"""
        proxy = self.config.get("proxy", "").strip()
        kwargs = {}
        if proxy:
            kwargs["proxy"] = proxy
        async with self.session.get(url, **kwargs) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _ensure_static_data(self):
        """确保静态本地化数据已下载并加载到内存"""
        if self.static_data:
            return
        async with self._download_lock:
            # 双重检查
            if self.static_data:
                return
            for filename in STATIC_FILES:
                filepath = self.cache_dir / filename.replace("/", "_")
                if not filepath.exists():
                    url = DATA_BASE + filename
                    try:
                        logger.info(f"下载静态数据: {url}")
                        data = await self._fetch(url)
                        filepath.write_bytes(data)
                    except Exception as e:
                        logger.error(f"下载 {filename} 失败: {e}，尝试使用旧缓存")
                # 加载到内存
                if filepath.exists():
                    try:
                        key = filename.split("/")[-1].split(".")[0]  # e.g. "loc"
                        # 对于子目录，用完整名避免冲突
                        if "/" in filename:
                            key = filename.replace("/", "_").split(".")[0]
                        self.static_data[key] = json.loads(filepath.read_text(encoding="utf-8"))
                    except Exception as e:
                        logger.warning(f"加载 {filename} 失败: {e}")

    async def _ensure_font(self):
        """确保中文字体可用，优先下载思源黑体"""
        font_path = self.font_dir / "NotoSansCJKsc-Regular.otf"
        if font_path.exists():
            self.font = ImageFont.truetype(str(font_path), 24)
            self.small_font = ImageFont.truetype(str(font_path), 18)
            return
        # 尝试从系统查找
        system_fonts = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "C:\\Windows\\Fonts\\msyh.ttc",  # 微软雅黑
        ]
        for sys_font in system_fonts:
            if os.path.exists(sys_font):
                self.font = ImageFont.truetype(sys_font, 24)
                self.small_font = ImageFont.truetype(sys_font, 18)
                logger.info(f"使用系统字体: {sys_font}")
                return
        # 自动下载
        try:
            url = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
            logger.info("自动下载中文字体...")
            font_data = await self._fetch(url)
            font_path.write_bytes(font_data)
            self.font = ImageFont.truetype(str(font_path), 24)
            self.small_font = ImageFont.truetype(str(font_path), 18)
            logger.info("字体下载完成")
        except Exception as e:
            logger.error(f"字体下载失败: {e}，将使用默认字体（可能中文乱码）")
            self.font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()

    def _localize(self, hash_value) -> str:
        """通过 loc.json 获取中文文本，失败则返回哈希值"""
        loc = self.static_data.get("loc", {})
        # loc 结构: { "zh-CN": { "12345": "文本" } }
        zh = loc.get("zh-CN", {})
        return zh.get(str(hash_value), str(hash_value))

    def _get_character_name(self, avatar_id: str) -> str:
        """根据角色 ID 获取中文名"""
        chars = self.static_data.get("characters", {})
        if isinstance(chars, list):
            for c in chars:
                if str(c.get("id")) == str(avatar_id):
                    hash_val = c.get("NameTextMapHash", "")
                    return self._localize(hash_val)
        return avatar_id

    def _get_character_icon(self, avatar_id: str) -> str:
        """从 characters.json 获取角色的立绘图标名"""
        chars = self.static_data.get("characters", {})
        if isinstance(chars, list):
            for c in chars:
                if str(c.get("id")) == str(avatar_id):
                    # 优先侧面立绘
                    icon = c.get("SideIconName", "") or c.get("IconName", "")
                    if icon:
                        return f"{icon}.png"
        # fallback
        return f"UI_AvatarIcon_Side_{avatar_id}.png"

    def _get_binds(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """读取绑定文件"""
        if not self.binds_file.exists():
            return {}
        return json.loads(self.binds_file.read_text(encoding="utf-8"))

    def _save_binds(self, binds: Dict):
        self.binds_file.write_text(json.dumps(binds, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------
    # 指令：绑定 UID
    # ------------------------------------------------------------
    @filter.command("原神")
    async def bind_genshin(self, event: AstrMessageEvent):
        uid = event.message_str.strip().split()[-1] if event.message_str.strip().split() else ""
        return await self._bind_uid(event, "genshin", uid)

    @filter.command("星铁")
    async def bind_hsr(self, event: AstrMessageEvent):
        uid = event.message_str.strip().split()[-1] if event.message_str.strip().split() else ""
        return await self._bind_uid(event, "hsr", uid)

    @filter.command("崩铁")
    async def bind_hsr2(self, event: AstrMessageEvent):
        uid = event.message_str.strip().split()[-1] if event.message_str.strip().split() else ""
        return await self._bind_uid(event, "hsr", uid)

    @filter.command("绝区零")
    async def bind_zzz(self, event: AstrMessageEvent):
        uid = event.message_str.strip().split()[-1] if event.message_str.strip().split() else ""
        return await self._bind_uid(event, "zzz", uid)

    async def _bind_uid(self, event: AstrMessageEvent, game: str, uid: str):
        if not uid.isdigit():
            yield event.plain_result("❌ UID 必须为纯数字")
            return
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        user_id = str(event.message_obj.sender.user_id)
        binds = self._get_binds()
        if group_id not in binds:
            binds[group_id] = {}
        if user_id not in binds[group_id]:
            binds[group_id][user_id] = {}
        binds[group_id][user_id][game] = uid
        self._save_binds(binds)
        yield event.plain_result(f"✅ {GAME_NAMES[game]} UID 绑定成功：{uid}")

    # ------------------------------------------------------------
    # 指令：查询面板
    # ------------------------------------------------------------
    @filter.command("查询原神面板")
    async def query_genshin(self, event: AstrMessageEvent):
        await self._query_panel(event, "genshin")

    @filter.command("查询星铁面板")
    async def query_hsr(self, event: AstrMessageEvent):
        await self._query_panel(event, "hsr")

    @filter.command("查询崩铁面板")
    async def query_hsr2(self, event: AstrMessageEvent):
        await self._query_panel(event, "hsr")

    @filter.command("查询绝区零面板")
    async def query_zzz(self, event: AstrMessageEvent):
        await self._query_panel(event, "zzz")

    async def _query_panel(self, event: AstrMessageEvent, game: str):
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        user_id = str(event.message_obj.sender.user_id)
        binds = self._get_binds()
        uid = binds.get(group_id, {}).get(user_id, {}).get(game)
        if not uid:
            yield event.plain_result(f"❌ 请先使用 /{GAME_NAMES[game]} <UID> 绑定你的{GAME_NAMES[game]}账号")
            return

        endpoint = GAME_ENDPOINTS[game]
        url = f"https://enka.network/api{endpoint}{uid}"
        try:
            async with self.session.get(url, proxy=self.config.get("proxy", "").strip() or None) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    yield event.plain_result(f"❌ 查询失败（{resp.status}）：可能展柜未公开或UID不存在。\n{txt[:100]}")
                    return
                data = await resp.json()
        except Exception as e:
            logger.error(f"网络请求失败: {e}")
            yield event.plain_result(f"❌ 网络请求失败: {e}")
            return

        player_info = data.get("playerInfo", {})
        avatar_list = data.get("avatarInfoList", [])
        if not avatar_list:
            nickname = player_info.get("nickname", "未知")
            yield event.plain_result(f"玩家 {nickname} 的角色展柜为空或未公开详情。")
            return

        nickname = player_info.get("nickname", "未知")
        # 发送提示
        yield event.plain_result(f"正在生成 {nickname} 的{GAME_NAMES[game]}角色面板，请稍候...")

        # 按顺序生成每个角色的图片
        for idx, avatar in enumerate(avatar_list):
            try:
                img = await self._draw_character_card(avatar, game, player_info)
                # 保存临时文件
                tmp_path = self.data_dir / f"temp_{game}_{uid}_{idx}.png"
                img.save(str(tmp_path), format="PNG")
                # 发送图片
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain([MsgImage.fromFileSystem(str(tmp_path))])
                )
                # 删除临时文件
                os.remove(tmp_path)
                await asyncio.sleep(0.5)  # 防止刷屏
            except Exception as e:
                logger.error(f"生成角色 {idx} 图片失败: {e}", exc_info=True)
                yield event.plain_result(f"⚠️ 角色 {idx+1} 生成失败：{e}")

    # ------------------------------------------------------------
    # 图片绘制逻辑
    # ------------------------------------------------------------
    async def _draw_character_card(self, avatar: dict, game: str, player_info: dict) -> Image.Image:
        """绘制单个角色面板图片"""
        # 基础信息
        avatar_id = str(avatar.get("avatarId", ""))
        name = self._get_character_name(avatar_id)
        level = avatar.get("propMap", {}).get("4001", {}).get("val", 1)  # 等级
        # 命座
        talent_ids = avatar.get("talentIdList", [])
        constellation = len(talent_ids)  # 解锁命座数量
        # 好感度
        fetter = avatar.get("fetterInfo", {}).get("expLevel", 0)

        # 属性
        fight_props = avatar.get("fightPropMap", {})

        # 装备
        equips = avatar.get("equipList", [])

        # 下载立绘
        icon_name = self._get_character_icon(avatar_id)
        icon_url = UI_BASE + icon_name
        try:
            icon_data = await self._fetch(icon_url)
            avatar_icon = Image.open(io.BytesIO(icon_data)).convert("RGBA")
        except Exception:
            # 使用纯色占位
            avatar_icon = Image.new("RGBA", (256, 256), (50, 50, 50, 255))

        # 缩放立绘到合适大小 (保持比例，高度最多 600)
        icon_ratio = avatar_icon.width / avatar_icon.height
        target_h = min(600, int(CARD_H * 0.85))
        target_w = int(target_h * icon_ratio)
        avatar_icon = avatar_icon.resize((target_w, target_h), Image.LANCZOS)

        # 创建画布
        card = Image.new("RGBA", (CARD_W, CARD_H), (30, 30, 40, 255))
        draw = ImageDraw.Draw(card)

        # 粘贴立绘 (左边垂直居中)
        icon_x = 20
        icon_y = (CARD_H - target_h) // 2
        card.paste(avatar_icon, (icon_x, icon_y), avatar_icon)

        # 分隔线
        line_x = LEFT_W
        draw.line([(line_x, 30), (line_x, CARD_H-30)], fill=(80, 80, 100, 255), width=2)

        # ----- 中间区域 -----
        text_x = line_x + 30
        y = 50

        # 名字 + 等级
        draw.text((text_x, y), f"{name}  Lv.{int(level)}  好感{fetter}",
                  fill=(255, 255, 255, 255), font=self.font)
        y += 45

        # 命座图标 (用文字代替)
        draw.text((text_x, y), f"命座: {constellation}/6",
                  fill=(200, 200, 200, 255), font=self.small_font)
        y += 30

        # 技能等级 (skillLevelMap)
        skill_map = avatar.get("skillLevelMap", {})
        skill_text = "  ".join([f"技能{k}: Lv.{v}" for k, v in skill_map.items()])
        if skill_text:
            draw.text((text_x, y), f"行迹: {skill_text}",
                      fill=(200, 200, 200, 255), font=self.small_font)
            y += 30

        # 武器/光锥/音擎
        weapon_obj = None
        for eq in equips:
            if eq.get("flat", {}).get("itemType") == "ITEM_WEAPON":
                weapon_obj = eq
                break
        if weapon_obj:
            flat = weapon_obj["flat"]
            w_name = self._localize(flat.get("nameTextHashMap", ""))
            w_level = weapon_obj.get("weapon", {}).get("level", 1)
            w_rank = list(weapon_obj.get("weapon", {}).get("affixMap", {}).values())
            w_rank = w_rank[0] if w_rank else 1
            draw.text((text_x, y), f"专武: {w_name} Lv.{w_level} 精炼{w_rank+1}",
                      fill=(255, 220, 100, 255), font=self.small_font)
            y += 30

        # 战斗属性 (选取关键属性)
        key_props = {
            "1": "基础生命", "2": "生命值", "3": "生命%", "4": "基础攻击",
            "5": "攻击力", "6": "攻击%", "7": "基础防御", "8": "防御力",
            "9": "防御%", "20": "暴击率", "22": "暴击伤害",
            "23": "充能效率", "28": "元素精通",
        }
        prop_lines = []
        for pid, label in key_props.items():
            val = fight_props.get(pid)
            if val is not None:
                if pid in ("20", "22", "23", "3", "6", "9"):  # 百分比
                    val_str = f"{val*100:.1f}%"
                else:
                    val_str = f"{val:.0f}"
                prop_lines.append(f"{label}: {val_str}")
        prop_text = "  |  ".join(prop_lines)
        draw.text((text_x, y), prop_text, fill=(180, 180, 200, 255), font=self.small_font)
        y += 40

        # ----- 右侧圣遗物/遗器/驱动盘 -----
        right_x = 850
        ry = 50
        artifacts = [eq for eq in equips if eq.get("flat", {}).get("itemType") == "ITEM_RELIQUARY"]
        draw.text((right_x, ry), "仪器:", fill=(255, 255, 255, 255), font=self.font)
        ry += 40
        for art in artifacts[:5]:  # 最多5件
            flat = art["flat"]
            set_hash = flat.get("setNameTextHashMap", "")
            set_name = self._localize(set_hash) if set_hash else "未知套装"
            main_stat_id = art.get("reliquary", {}).get("mainPropId", "")
            main_stat_name = self._localize(main_stat_id) if main_stat_id else "主属性"
            main_val = flat.get("reliquaryMainstat", {}).get("propValue", 0)
            level_art = art.get("reliquary", {}).get("level", 1)
            equip_type = flat.get("equipType", "")
            draw.text((right_x, ry), f"{set_name} {equip_type} Lv.{level_art}",
                      fill=(200, 180, 100, 255), font=self.small_font)
            ry += 25
            draw.text((right_x+20, ry), f"主: {main_stat_name} +{main_val}",
                      fill=(220, 220, 220, 255), font=self.small_font)
            ry += 25
            # 副属性
            subs = flat.get("reliquarySubstats", [])
            sub_text = "  ".join([f"{self._localize(s.get('appendPropId',''))} +{s.get('propValue',0)}" for s in subs])
            draw.text((right_x+20, ry), sub_text, fill=(180, 180, 180, 255), font=self.small_font)
            ry += 30

        return card