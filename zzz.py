# -*- coding: utf-8 -*-
"""
绝区零（Zenless Zone Zero）面板渲染模块

两级渲染（解决"一片一片挤在一起"的问题）：
    render_list(player, width=900)       -> 角色列表概览图
    render_detail(av, player, width=900) -> 单角色详情大图（音擎 + 6 件驱动盘逐件 + 套装 + 技能）

数据来源：https://enka.network/api/zzz/uid/<UID>    注意 ZZZ 的 UID 是 8 位数字！

各字段解析路径（全部实测确认）：
  玩家：PlayerInfo.SocialDetail.ProfileDetail.Nickname / Level / Uid
        注意 Nickname 实测是 "\xa0\xa0\xa0\xa0\xa0\xa0\xa0"（纯不可见填充），必须 strip，
        为空时回退 "绳匠{uid前4位}"；签名在 PlayerInfo.SocialDetail.Desc
  角色：PlayerInfo.ShowcaseDetail.AvatarList[]
        Id / Level / PromotionLevel / TalentLevel / CoreSkillEnhancement / SkillLevelList
        EquippedList[].Slot 1~6（6 件驱动盘）+ Equipment{Id,Level,BreakLevel,...}
        Weapon{Id,Level,BreakLevel,UpgradeLevel}   ← 用 Weapon.Id，**不是** WeaponUid！
  名字：store/zzz/avatars.json[str(Id)]["Name"] -> store/zzz/locs.json["zh-cn"][那个名字]
        注意 zzz 是按**名字字符串**查，不是 hash。
  音擎：store/zzz/weapons.json[str(Weapon.Id)] -> ItemName -> locs 查中文
  驱动盘：store/zzz/equipments.json["Items"][str(Equipment.Id)]["SuitId"]
          -> ["Suits"][str(SuitId)]  {"Name":"EquipmentSuit_xxx_name","SetBonusProps":{...}}
          -> locs 查中文
  属性：store/zzz/property.json[str(PropertyId)] -> {"Name":..., "Format":"{0:0.#%}"}

数值换算（实测，与游戏内数值完全吻合，写在 _disc_main / _disc_sub 里）：
  驱动盘主词条（PropertyLevel 恒为 1）：
       Format 含 "%"  -> 显示 value/25 加 %     例：CritDmg 1200 -> 48.0%
       Format 不含 "%" -> 显示原值              例：HpMax 550 -> 550
  驱动盘副词条（PropertyLevel 是强化次数）：
       Format 含 "%"  -> 显示 value*PropertyLevel/100 加 %   例：Crit 240 × PL4 -> 9.6%
       Format 不含 "%" -> 显示 value*PropertyLevel           例：Atk 19 × PL1 -> 19
  音擎副属性 / 套装效果：
       Format 含 "%"  -> 显示 value/100 加 %     例：CritDmg 1920 -> 19.2%
       Format 不含 "%" -> 显示原值

依赖：标准库 + Pillow。优先复用 common.py 的绘图基元，common 不可用时本文件自带兜底。
"""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

# ---------- 尝试复用 common.py ----------
_C = None
try:
    from . import common as _C
except Exception:
    try:
        import common as _C  # type: ignore
    except Exception:
        _C = None


# ============================================================
# 兜底绘图基元（common.py 不可用 / 缺项时使用）
# ============================================================

try:
    ACCENT_DEEP = getattr(_C, "ACCENT_DEEP", None) or (158, 108, 18)
    ACCENT_SOFT = getattr(_C, "ACCENT_SOFT", None) or (250, 242, 224)
except Exception:
    ACCENT_DEEP = (158, 108, 18)
    ACCENT_SOFT = (250, 242, 224)

_BG = (28, 32, 44)
_BG_CARD = (38, 44, 58)
_FG = (232, 236, 245)
_FG_DIM = (150, 160, 178)
_FG_MUTED = (110, 120, 138)
_ACCENT = (255, 200, 100)
_LINE = (60, 68, 86)
_STAR5 = (255, 190, 90)
_STAR4 = (170, 130, 220)

_FONT_SCANNED = False
_FONT_REG = None
_FONT_BLD = None
_FONT_CACHE: dict = {}


def _scan_fonts() -> None:
    global _FONT_SCANNED, _FONT_REG, _FONT_BLD
    if _FONT_SCANNED:
        return
    _FONT_SCANNED = True
    regs = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/Deng.ttf",
    ]
    blds = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/Dengb.ttf",
    ]
    for p in regs:
        if os.path.exists(p):
            _FONT_REG = p
            break
    for p in blds:
        if os.path.exists(p):
            _FONT_BLD = p
            break
    if _FONT_BLD is None:
        _FONT_BLD = _FONT_REG


def _fb_font(size: int, bold: bool = False):
    key = (int(size), bool(bold))
    f = _FONT_CACHE.get(key)
    if f is not None:
        return f
    _scan_fonts()
    path = _FONT_BLD if bold else _FONT_REG
    try:
        f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _FONT_CACHE[key] = f
    return f


def _fb_text_w(draw, s: str, f) -> int:
    try:
        b = draw.textbbox((0, 0), s, font=f)
        return int(b[2] - b[0])
    except Exception:
        return len(s) * 10


def _fb_rounded(draw, box, r, fill, outline=None, width=1) -> None:
    try:
        draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)
    except Exception:
        draw.rectangle(box, fill=fill)


def _fb_save_webp(img, quality: int = 88) -> bytes:
    # 兜底实现：仅在 common 导入失败时使用。
    # method 用 2（与 common.save_webp 一致）——method=6 是最慢档，
    # 实测同一画布 6 比 2 慢约 3.2 倍（120ms vs 38ms），体积仅差 5.7%。
    buf = io.BytesIO()
    try:
        img.save(buf, "WEBP", quality=int(quality), method=2)
    except Exception:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "WEBP", quality=int(quality))
    return buf.getvalue()


def _fb_fmt_int(v) -> str:
    try:
        return f"{int(round(float(v))):,}"
    except Exception:
        return "0"


def _fb_fmt_pct(v, digits: int = 1) -> str:
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except Exception:
        return "0%"


def _fb_truncate(draw, text: str, f, max_w: int) -> str:
    if not text:
        return ""
    if _fb_text_w(draw, text, f) <= max_w:
        return text
    ell = "…"
    for i in range(len(text) - 1, 0, -1):
        t = text[:i] + ell
        if _fb_text_w(draw, t, f) <= max_w:
            return t
    return ell


def _bind(name: str, fallback):
    if _C is None:
        return fallback
    v = getattr(_C, name, None)
    return v if v is not None else fallback


BG = _bind("BG", _BG)
BG_CARD = _bind("BG_CARD", _BG_CARD)
FG = _bind("FG", _FG)
FG_DIM = _bind("FG_DIM", _FG_DIM)
FG_MUTED = _bind("FG_MUTED", _FG_MUTED)
ACCENT = _bind("ACCENT", _ACCENT)
LINE = _bind("LINE", _LINE)

_common_font = _bind("font", None)
_common_text_w = _bind("text_w", None)
_common_rounded = _bind("rounded", None)
_common_save_webp = _bind("save_webp", None)
_common_fmt_int = _bind("fmt_int", None)
_common_truncate = _bind("truncate", None)


def _font(size: int, bold: bool = False):
    """优先 common.font；返回非矢量字体（中文会变豆腐块）时用兜底"""
    if _common_font is not None:
        try:
            f = _common_font(size, bold)
            if isinstance(f, ImageFont.FreeTypeFont):
                return f
        except Exception:
            pass
    return _fb_font(size, bold)


def _text_w(draw, s: str, f) -> int:
    if _common_text_w is not None:
        try:
            return int(_common_text_w(draw, s, f))
        except Exception:
            pass
    return _fb_text_w(draw, s, f)


def _rounded(draw, box, r, fill):
    if _common_rounded is not None:
        try:
            _common_rounded(draw, box, r, fill)
            return
        except Exception:
            pass
    _fb_rounded(draw, box, r, fill)


def _save_webp(img, quality: int = 88) -> bytes:
    if _common_save_webp is not None:
        try:
            b = _common_save_webp(img)
            if isinstance(b, (bytes, bytearray)) and b:
                return bytes(b)
        except Exception:
            pass
    return _fb_save_webp(img, quality)


def _fmt_int(v) -> str:
    if _common_fmt_int is not None:
        try:
            return str(_common_fmt_int(v))
        except Exception:
            pass
    return _fb_fmt_int(v)


def _truncate(draw, text: str, f, max_w: int) -> str:
    if _common_truncate is not None:
        try:
            return str(_common_truncate(draw, text, f, max_w))
        except Exception:
            pass
    return _fb_truncate(draw, text, f, max_w)


# ============================================================
# 绝区零静态表
# ============================================================

# 元素：store 里的实际值是 Elec / Physics / Ether / Ice / Fire / FireFrost /
# AuricEther / Lumen / Wind / ZhenZhenAssault（**不是** Electric/Physical）
_FB_ZZZ_ELEM_CN = {
    "Fire": "火",
    "Ice": "冰",
    "Ether": "以太",
    "Physical": "物理",
    "Physics": "物理",
    "Electric": "电",
    "Elec": "电",
    "Frost": "霜",
    "FireFrost": "霜火",
    "Auric": "耀",
    "AuricEther": "耀以太",
    "Lumen": "辉",
    "Wind": "风",
    "ZhenZhenAssault": "真真强袭",
}

_FB_ZZZ_ELEM_COLOR = {
    "Fire": (240, 110, 70),
    "FireFrost": (250, 150, 120),
    "Ice": (110, 200, 240),
    "Frost": (150, 220, 250),
    "Ether": (200, 130, 240),
    "AuricEther": (250, 210, 110),
    "Auric": (250, 210, 110),
    "Lumen": (250, 240, 200),
    "Physical": (210, 210, 210),
    "Physics": (210, 210, 210),
    "Electric": (160, 140, 250),
    "Elec": (160, 140, 250),
    "Wind": (120, 220, 190),
    "ZhenZhenAssault": (250, 160, 200),
}

# 职业中文（store 里是英文枚举）
_FB_ZZZ_PROF_CN = {
    "Attack": "强攻",
    "Defense": "防护",
    "Stun": "击破",
    "Anomaly": "异常",
    "Support": "支援",
    "Rupture": "命破",
    "Armorer": "防护",
}

# 职业配色（列表页左侧色条）
_FB_ZZZ_PROF_COLOR = {
    "Attack": (232, 110, 70),
    "Defense": (110, 170, 120),
    "Stun": (240, 190, 90),
    "Anomaly": (190, 120, 230),
    "Support": (110, 180, 220),
    "Rupture": (230, 130, 160),
    "Armorer": (110, 170, 120),
}

# 驱动盘槽位（实测 EquippedList 的 Slot 就是 1~6 共 6 件）
_FB_DISC_SLOT_CN = {
    1: "1号位 · 上",
    2: "2号位 · 下",
    3: "3号位 · 左",
    4: "4号位 · 右",
    5: "5号位 · 中",
    6: "6号位 · 驱动盘",
}

# 需要金色高亮的词条（暴击 / 爆伤）
_HOT_PROPS_NAME = {
    "Crit",
    "CritDmg",
    "SharpCriticalDamage",
    "Crit_dmg",
    "CritRate",
}

# 属性英文名 -> 中文兜底（property.json 的 Name 在 zzz/locs.json 里通常能查到，
# 这里只是兜底，保证不会显示英文枚举）
_FB_ZZZ_PROP_CN = {
    "HpMax": "生命值",
    "HpMax_Ratio": "生命值百分比",
    "HpMax_base": "基础生命值",
    "Atk": "攻击力",
    "Atk_Ratio": "攻击力百分比",
    "Atk_base": "基础攻击力",
    "Def": "防御力",
    "Def_Ratio": "防御力百分比",
    "Def_base": "基础防御力",
    "Crit": "暴击率",
    "CritDmg": "暴击伤害",
    "CritRes": "暴击抗性",
    "CritDmgRes": "暴击伤害抗性",
    "PenRatio": "穿透率",
    "PenDelta": "穿透值",
    "BreakStun": "冲击力",
    "SpRecover": "能量自动回复",
    "SpMax": "能量上限",
    "ElementMystery": "异常掌控",
    "ElementAbnormalPower": "异常精通",
    "AddedDamageRatio": "伤害加成",
    "AddedDamageRatio_Physics": "物理属性伤害加成",
    "AddedDamageRatio_Fire": "火属性伤害加成",
    "AddedDamageRatio_Ice": "冰属性伤害加成",
    "AddedDamageRatio_Elec": "电属性伤害加成",
    "AddedDamageRatio_Ether": "以太属性伤害加成",
    "AddedDamageRatio_Wind": "风属性伤害加成",
    "HPHealRatio": "治疗加成",
    "AddedElementAccumulationRatio": "异常积蓄效率",
    "RpRecover": "支援点回复",
    "EpRecover": "终结技回复",
    "ArmorMax": "护盾值",
    "ShieldMax": "护盾值",
    "StunMax": "失衡值",
    "MaxBuddyBattery": "邦布电量上限",
    "SharpCriticalDamage": "贯穿暴击伤害",
    "SkipDefAtk": "无视防御攻击",
    "SkipDefDamageRatio": "无视防御伤害加成",
    "RpGetRatio": "支援点获得效率",
    "EpGetRatio": "终结技获得效率",
}

# 技能槽位中文（SkillLevelList 的 Index 实测为 0,1,2,3,5,6 六个，
# 按 Index 升序的位置对应游戏内 6 个技能，所以用**位置**映射而不是 Index 语义映射）
_SKILL_LABELS = ["普攻", "闪避", "支援", "特殊", "连携", "终结"]

_MAX_PROMOTION = 6


# ============================================================
# store 读取辅助
# ============================================================


def _store_get(store, name: str) -> dict:
    if store is None:
        return {}
    if isinstance(store, dict):
        d = store.get(name)
        return d if isinstance(d, dict) else {}
    for meth in ("get", "get_cached"):
        fn = getattr(store, meth, None)
        if not callable(fn):
            continue
        try:
            d = fn(name)
        except Exception:
            continue
        if isinstance(d, dict) and d:
            return d
    return {}


def _strip_nickname(s) -> str:
    """绝区零昵称实测是 \\xa0 填充的不可见字符，必须清掉"""
    if s is None:
        return ""
    t = str(s)
    for ch in ("\xa0", "\u3000", "\u200b", "\t", "\r", "\n", " "):
        t = t.replace(ch, "")
    return t.strip()


def _clean_text(s) -> str:
    """普通文本清理（签名等）：去 \\xa0 / 全角空格，保留内部空格"""
    if s is None:
        return ""
    t = str(s).replace("\xa0", " ").replace("\u3000", " ")
    return t.strip()


# ============================================================
# 解析
# ============================================================


class _Ctx:
    """解析上下文：持有 store 各表 + 中文名字典"""

    def __init__(self, store):
        self.avatars = _store_get(store, "zzz/avatars")
        self.weapons = _store_get(store, "zzz/weapons")
        self.equipments = _store_get(store, "zzz/equipments")
        self.properties = _store_get(store, "zzz/property")

        raw_loc = _store_get(store, "zzz/locs")
        if isinstance(raw_loc.get("zh-cn"), dict):
            self.loc = raw_loc["zh-cn"]
        elif raw_loc and all(isinstance(k, str) for k in list(raw_loc)[:3]):
            self.loc = raw_loc
        else:
            self.loc = {}

    # ---------- 名字 ----------

    def loc_text(self, key, fallback: str = "") -> str:
        if key is None:
            return fallback
        return self.loc.get(str(key)) or fallback

    def avatar_meta(self, aid) -> dict:
        return self.avatars.get(str(aid)) or {}

    def avatar_name(self, aid) -> str:
        meta = self.avatar_meta(aid)
        name_key = meta.get("Name")
        return self.loc_text(name_key, f"角色{aid}")

    def weapon_meta(self, wid) -> dict:
        return self.weapons.get(str(wid)) or {}

    def weapon_name(self, wid) -> str:
        meta = self.weapon_meta(wid)
        return self.loc_text(meta.get("ItemName"), f"音擎#{wid}")

    def disc_meta(self, eid) -> dict:
        items = self.equipments.get("Items") or {}
        return items.get(str(eid)) or {}

    def suit_meta(self, suit_id) -> dict:
        suits = self.equipments.get("Suits") or {}
        return suits.get(str(suit_id)) or {}

    def suit_name(self, suit_id) -> str:
        s = self.suit_meta(suit_id)
        return self.loc_text(s.get("Name"), f"套装#{suit_id}")

    # ---------- 属性 ----------

    def prop_meta(self, pid) -> dict:
        return self.properties.get(str(pid)) or {}

    def prop_name(self, pid) -> str:
        """属性中文名：property.json 的 Name 在同名 loc 里能查到，查不到用兜底表"""
        pm = self.prop_meta(pid)
        name = pm.get("Name") or ""
        if not name:
            return f"属性{pid}"
        cn = self.loc.get(name)
        if cn:
            return cn
        return _FB_ZZZ_PROP_CN.get(name, name)

    def prop_is_pct(self, pid) -> bool:
        """Format 含 % 说明是百分比属性（决定数值换算与显示格式）"""
        fmt = self.prop_meta(pid).get("Format") or ""
        return "%" in str(fmt)

    def prop_digits(self, pid) -> int:
        """根据 Format 推断小数位（{0:0.##} -> 2 位，其余 1 位）"""
        fmt = str(self.prop_meta(pid).get("Format") or "")
        if ".##}" in fmt:
            return 2
        return 1

    # --- 三种换算（实测，详见模块 docstring）---

    def disc_main_text(self, pid, value) -> str:
        """驱动盘主词条：百分比 value/25，固定值原样"""
        try:
            v = float(value or 0)
        except Exception:
            v = 0.0
        if self.prop_is_pct(pid):
            return f"{v / 25.0:.1f}%"
        d = self.prop_digits(pid)
        return f"{v:.{d}f}" if d > 0 and abs(v - round(v)) > 0.05 else _fmt_int(v)

    def disc_sub_text(self, pid, value, lv) -> str:
        """驱动盘副词条：百分比 value*PL/100，固定值 value*PL"""
        try:
            v = float(value or 0)
        except Exception:
            v = 0.0
        try:
            pl = max(1, int(lv or 1))
        except Exception:
            pl = 1
        if self.prop_is_pct(pid):
            return f"{v * pl / 100.0:.1f}%"
        real = v * pl
        d = self.prop_digits(pid)
        return f"{real:.{d}f}" if d > 0 and abs(real - round(real)) > 0.05 else _fmt_int(real)

    def flat_text(self, pid, value, scale: float = 1.0) -> str:
        """音擎副属性 / 套装效果：百分比 value/100，固定值原样"""
        try:
            v = float(value or 0) * float(scale)
        except Exception:
            v = 0.0
        if self.prop_is_pct(pid):
            return f"{v / 100.0:.1f}%"
        d = self.prop_digits(pid)
        return f"{v:.{d}f}" if d > 0 and abs(v - round(v)) > 0.05 else _fmt_int(v)

    # ---------- 驱动盘 ----------

    def parse_disc(self, slot: int, eq: dict) -> dict:
        eid = eq.get("Id")
        meta = self.disc_meta(eid)
        suit_id = meta.get("SuitId") or 0
        level = int(eq.get("Level") or 0)
        break_lv = int(eq.get("BreakLevel") or 0)

        # 主词条（实测只有 1 条）
        main = {}
        for m in eq.get("MainPropertyList") or []:
            pid = m.get("PropertyId")
            main = {
                "pid": pid,
                "name": self.prop_name(pid),
                "text": self.disc_main_text(pid, m.get("PropertyValue")),
                "hot": self.prop_name(pid) in ("暴击率", "暴击伤害"),
            }
            break
        if not main:
            main = {"pid": 0, "name": "-", "text": "-", "hot": False}

        # 副词条（实测 4 条）
        subs = []
        score = 0.0
        for s in eq.get("RandomPropertyList") or []:
            pid = s.get("PropertyId")
            pl = s.get("PropertyLevel") or 1
            name = self.prop_name(pid)
            text = self.disc_sub_text(pid, s.get("PropertyValue"), pl)
            is_pct = self.prop_is_pct(pid)
            try:
                pl_f = float(pl or 1)
                raw = float(s.get("PropertyValue") or 0)
            except Exception:
                pl_f, raw = 1.0, 0.0
            # 换算成"真实值"，供装备加成汇总使用
            real_pct = raw * pl_f / 100.0 if is_pct else 0.0
            real_flat = 0.0 if is_pct else raw * pl_f
            subs.append(
                {
                    "pid": pid,
                    "name": name,
                    "text": text,
                    "hot": name in ("暴击率", "暴击伤害"),
                    "is_pct": is_pct,
                    "real_pct": real_pct,
                    "real_flat": real_flat,
                    "rolls": pl_f,
                }
            )
            # 词条评分：暴击/爆伤最高，其余按强化次数折算
            if name == "暴击率":
                score += pl_f * 1.0
            elif name == "暴击伤害":
                score += pl_f * 1.0
            elif name in ("攻击力百分比", "生命值百分比", "防御力百分比", "穿透值", "异常精通"):
                score += pl_f * 0.7
            else:
                score += pl_f * 0.4

        return {
            "slot": slot,
            "slot_label": _FB_DISC_SLOT_CN.get(slot, f"{slot}号位"),
            "id": eid,
            "level": level,
            "break_level": break_lv,
            "main": main,
            "subs": subs,
            "suit_id": suit_id,
            "suit_name": self.suit_name(suit_id),
            "rarity": int(meta.get("Rarity") or 0),
            "score": min(12.0, round(score, 1)),
        }

    # ---------- 角色 ----------

    def parse_avatar(self, a: dict) -> dict:
        aid = a.get("Id")
        meta = self.avatar_meta(aid)
        level = int(a.get("Level") or 0)
        promo = int(a.get("PromotionLevel") or 0)
        talent = int(a.get("TalentLevel") or 0)
        core = int(a.get("CoreSkillEnhancement") or 0)

        elems = meta.get("ElementTypes") or []
        elem = str(elems[0]) if elems else ""
        prof = str(meta.get("ProfessionType") or "")
        name = self.avatar_name(aid)

        # 角色主题色（store 的 Colors.Accent），列表页色条用它
        accent_hex = str((meta.get("Colors") or {}).get("Accent") or "")
        accent = _hex_to_rgb(accent_hex, _FB_ZZZ_PROF_COLOR.get(prof, FG_DIM))

        # --- 音擎：必须用 Weapon.Id，不是 WeaponUid ---
        weapon = None
        w = a.get("Weapon") or {}
        if w.get("Id"):
            wid = w.get("Id")
            wmeta = self.weapon_meta(wid)
            ms = wmeta.get("MainStat") or {}
            ss = wmeta.get("SecondaryStat") or {}
            weapon = {
                "id": wid,
                "name": self.weapon_name(wid),
                "level": int(w.get("Level") or 0),
                "break_level": int(w.get("BreakLevel") or 0),
                "upgrade": int(w.get("UpgradeLevel") or 0),
                "rarity": int(wmeta.get("Rarity") or 0),
                "profession": str(wmeta.get("ProfessionType") or ""),
                "main_name": self.prop_name(ms.get("PropertyId")),
                "main_text": self.flat_text(ms.get("PropertyId"), ms.get("PropertyValue")),
                "sec_name": self.prop_name(ss.get("PropertyId")),
                "sec_text": self.flat_text(ss.get("PropertyId"), ss.get("PropertyValue")),
            }

        # --- 驱动盘逐件 ---
        discs = []
        suit_count: dict = {}
        for e in a.get("EquippedList") or []:
            try:
                slot = int(e.get("Slot") or 0)
                eq = e.get("Equipment") or {}
                if not eq.get("Id"):
                    continue
                disc = self.parse_disc(slot, eq)
            except Exception:
                continue
            discs.append(disc)
            sid = disc.get("suit_id") or 0
            suit_count[sid] = suit_count.get(sid, 0) + 1
        discs.sort(key=lambda x: x["slot"])

        # --- 装备加成汇总（驱动盘副词条，按真实值累加）---
        pct_sum: dict = {}
        flat_sum: dict = {}
        for d in discs:
            for s in d.get("subs") or []:
                pname = self.prop_meta(s.get("pid")).get("Name") or ""
                if not pname:
                    continue
                if s.get("is_pct"):
                    pct_sum[pname] = pct_sum.get(pname, 0.0) + float(s.get("real_pct") or 0)
                else:
                    flat_sum[pname] = flat_sum.get(pname, 0.0) + float(s.get("real_flat") or 0)

        bonuses = []
        for pname, val in sorted(pct_sum.items(), key=lambda kv: -abs(kv[1])):
            bonuses.append((self._prop_name_by_key(pname), f"{val:.1f}%"))
        for pname, val in sorted(flat_sum.items(), key=lambda kv: -abs(kv[1])):
            bonuses.append((self._prop_name_by_key(pname), _fmt_int(val)))

        # --- 角色基础属性（store 的 Base/Growth/Promotion/Core 表，估算）---
        base_stats = self._base_stats(meta, level, promo, core)

        # --- 套装 ---
        sets = []
        for sid, cnt in sorted(suit_count.items(), key=lambda kv: (-kv[1], kv[0])):
            sm = self.suit_meta(sid)
            bonus = sm.get("SetBonusProps") or {}
            rows = []
            for pid, val in bonus.items():
                rows.append((self.prop_name(pid), self.flat_text(pid, val)))
            sets.append(
                {
                    "suit_id": sid,
                    "name": self.suit_name(sid),
                    "count": cnt,
                    "bonus": rows,
                }
            )

        # --- 技能（按 Index 升序的位置映射到 6 个技能）---
        raw_skills = []
        for s in a.get("SkillLevelList") or []:
            try:
                raw_skills.append((int(s.get("Index") or 0), int(s.get("Level") or 0)))
            except Exception:
                continue
        raw_skills.sort(key=lambda x: x[0])
        skills = []
        for i, (idx, lv) in enumerate(raw_skills[: len(_SKILL_LABELS)]):
            skills.append({"name": _SKILL_LABELS[i], "level": lv, "index": idx})

        return {
            "id": aid,
            "name": name,
            "element": elem,
            "element_cn": _ELEM_CN.get(elem, elem or ""),
            "element_color": _elem_color(elem),
            "profession": prof,
            "profession_cn": _PROF_CN.get(prof, prof or ""),
            "profession_color": _FB_ZZZ_PROF_COLOR.get(prof, FG_DIM),
            "accent": accent,
            "level": level,
            "promotion": promo,
            "talent": talent,
            "core_skill": core,
            "weapon": weapon,
            "discs": discs,
            "disc_count": len(discs),
            "sets": sets,
            "bonuses": bonuses,
            "skills": skills,
            "base_stats": base_stats,
        }

    def _prop_name_by_key(self, key: str) -> str:
        """property.json 的 Name 键 -> 中文（如 Crit -> 暴击率）"""
        cn = self.loc.get(key)
        if cn:
            return cn
        return _FB_ZZZ_PROP_CN.get(key, key)

    def _base_stats(self, meta: dict, level: int, promo: int, core: int) -> list:
        """角色基础属性（按 store 的 Base/Growth/Promotion 表估算，标注"估算"）

        GrowthProps 是万分比成长值：value = base + growth*(level-1)/10000
        PromotionProps 按晋升次数追加，CoreEnhancementProps 按核心技等级追加。
        """
        base = meta.get("BaseProps") or {}
        growth = meta.get("GrowthProps") or {}
        promos = meta.get("PromotionProps") or []
        cores = meta.get("CoreEnhancementProps") or []

        try:
            lv = max(1, int(level or 1))
        except Exception:
            lv = 1

        pr = {}
        if promos:
            i = max(0, min(int(promo or 0) - 1 if promo else 0, len(promos) - 1))
            pr = promos[i] or {}
        cr = {}
        if cores:
            i = max(0, min(int(core or 0), len(cores) - 1))
            cr = cores[i] or {}

        def _calc(pid_key: str) -> float:
            try:
                b = float(base.get(pid_key) or 0)
            except Exception:
                b = 0.0
            try:
                g = float(growth.get(pid_key) or 0)
            except Exception:
                g = 0.0
            extra = float(pr.get(pid_key) or 0) + float(cr.get(pid_key) or 0)
            return b + g * (lv - 1) / 10000.0 + extra

        rows = []
        # 11101=生命 12101=攻击 13101=防御 12201=冲击力 20101=暴击率 21101=暴伤
        rows.append(("生命值", _fmt_int(_calc("11101"))))
        rows.append(("攻击力", _fmt_int(_calc("12101"))))
        rows.append(("防御力", _fmt_int(_calc("13101"))))
        rows.append(("冲击力", f"{_calc('12201'):.1f}"))
        # 暴击/爆伤在 BaseProps 里是万分比（5000 = 50%），要除以 100
        try:
            crit = float(base.get("20101") or 0) / 100.0
        except Exception:
            crit = 5.0
        try:
            cdmg = float(base.get("21101") or 0) / 100.0
        except Exception:
            cdmg = 50.0
        rows.append(("基础暴击率", f"{crit:.1f}%"))
        rows.append(("基础暴击伤害", f"{cdmg:.1f}%"))
        # 异常掌控(31201) / 异常精通(31401) 是整数点数，不做百分比换算
        rows.append(("异常掌控", f"{_calc('31201'):.1f}"))
        rows.append(("异常精通", f"{_calc('31401'):.1f}"))
        return rows


# ---------- 中文表：优先 common ----------

_ELEM_CN = _bind("ZZZ_ELEM_CN", _FB_ZZZ_ELEM_CN)
_PROF_CN = _bind("ZZZ_PROF_CN", _FB_ZZZ_PROF_CN)
_ZZZ_ELEM_TABLE = _bind("ZZZ_ELEM", _FB_ZZZ_ELEM_COLOR)


def _hex_to_rgb(value, default=(150, 160, 178)):
    """#56c3c1 -> (86, 195, 193)"""
    if _C is not None:
        fn = getattr(_C, "hex_to_rgb", None)
        if callable(fn):
            try:
                c = fn(value, default)
                if isinstance(c, tuple) and len(c) == 3:
                    return c
            except Exception:
                pass
    s = str(value or "").strip().lstrip("#")
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except Exception:
            pass
    return default


def _elem_color(elem: str):
    if _C is not None:
        fn = getattr(_C, "elem_color", None)
        if callable(fn):
            try:
                c = fn("zzz", elem, FG_DIM)
                if isinstance(c, tuple) and len(c) == 3:
                    return c
            except Exception:
                pass
    if isinstance(_ZZZ_ELEM_TABLE, dict):
        c = _ZZZ_ELEM_TABLE.get(elem)
        if isinstance(c, tuple) and len(c) == 3:
            return c
    return _FB_ZZZ_ELEM_COLOR.get(elem, FG_DIM)


def parse_player(raw: dict, store=None) -> dict:
    """整份 enka 响应 -> 渲染用 player 结构

    参数：
        raw   : enka /api/zzz/uid/<uid> 的原始 JSON
        store : common.LocalStore（同步 get 已加载的 store 表），可为 None
    """
    pi = (raw or {}).get("PlayerInfo") or {}
    sd = pi.get("SocialDetail") or {}
    profile = sd.get("ProfileDetail") or {}
    showcase = pi.get("ShowcaseDetail") or {}
    uid = str(profile.get("Uid") or (raw or {}).get("uid") or "")

    # 昵称实测是 \xa0 填充，必须清理；空了回退 "绳匠{uid前4位}"
    nickname = _strip_nickname(profile.get("Nickname"))
    if not nickname:
        nickname = f"绳匠{uid[:4]}" if uid else "绳匠"

    ctx = _Ctx(store)
    avatars = []
    for a in showcase.get("AvatarList") or []:
        try:
            avatars.append(ctx.parse_avatar(a))
        except Exception:
            continue

    avatars.sort(
        key=lambda x: (
            -int(x.get("level") or 0),
            x.get("profession_cn") or "",
            x.get("name") or "",
        )
    )

    return {
        "game": "zzz",
        "uid": uid,
        "nickname": nickname,
        "level": int(profile.get("Level") or 0),
        "signature": _clean_text(sd.get("Desc")),
        "avatar_count": len(avatars),
        "avatars": avatars,
    }


# ============================================================
# 渲染 - 列表概览
# ============================================================


def render_list(player: dict, width: int = 1100) -> bytes:
    """角色列表概览图（一屏看清全部角色，发角色名再看详情）"""
    avs = list(player.get("avatars") or [])
    max_show = 12
    show = avs[:max_show]
    has_more = len(avs) > max_show

    header_h = 158
    card_h = 92
    gap = 12
    footer_h = 110
    extra_h = 44 if has_more else 0

    height = header_h + len(show) * (card_h + gap) + extra_h + footer_h
    height = max(height, 320)

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # ---------- 玩家头部 ----------
    d.rectangle((0, 0, width, header_h), fill=BG_CARD)

    nick = player.get("nickname") or "绳匠"
    d.text((28, 22), nick, font=_font(38, True), fill=FG)
    nw = _text_w(d, nick, _font(38, True))
    d.text((28 + nw + 16, 32), f"绳网等级 {player.get('level', 0)}", font=_font(20), fill=ACCENT)

    sig = player.get("signature") or ""
    if sig:
        sig = _truncate(d, sig, _font(18), width - 300)
        d.text((28, 70), sig, font=_font(18), fill=FG_MUTED)

    right = width - 28
    lines = [
        (f"展示角色 {len(avs)}", ACCENT, 18),
        (f"UID {player.get('uid', '')}", FG_MUTED, 15),
    ]
    ly = 26
    for text, color, size in lines:
        f = _font(size)
        tw = _text_w(d, text, f)
        d.text((right - tw, ly), text, font=f, fill=color)
        ly += 34

    # ---------- 角色行 ----------
    y = header_h + 20
    for idx, av in enumerate(show):
        x0 = 24
        w = width - 48
        y0 = y
        _rounded(d, (x0, y0, x0 + w, y0 + card_h), 10, BG_CARD)

        # 左侧色条：优先用角色主题色（store Colors.Accent），更贴合 ZZZ 视觉
        accent = av.get("accent") or av.get("profession_color") or FG_DIM
        d.rectangle((x0, y0 + 6, x0 + 5, y0 + card_h - 6), fill=accent)

        tx = x0 + 22
        name = av.get("name") or "?"
        d.text((tx, y0 + 12), name, font=_font(27, True), fill=FG)
        nmw = _text_w(d, name, _font(27, True))

        d.text((tx + nmw + 12, y0 + 18), f"Lv.{av.get('level', 0)}", font=_font(18), fill=ACCENT)

        # 职业 + 元素
        line2 = f"{av.get('profession_cn') or ''}  ·  {av.get('element_cn') or ''}属性"
        d.text((tx, y0 + 44), line2, font=_font(17), fill=av.get("element_color") or FG_DIM)

        # 核心技 / 驱动盘
        line3 = f"核心技 {av.get('core_skill', 0)}/6  ·  驱动盘 {av.get('disc_count', 0)} 件"
        d.text((tx, y0 + 66), line3, font=_font(15), fill=FG_MUTED)

        # ---------- 音擎 ----------
        wx = x0 + 300
        wp = av.get("weapon")
        d.text((wx, y0 + 12), "音擎", font=_font(13), fill=FG_MUTED)
        if wp:
            wname = _truncate(d, wp.get("name") or "-", _font(19, True), 250)
            d.text((wx, y0 + 28), wname, font=_font(19, True), fill=FG)
            d.text(
                (wx, y0 + 52),
                f"Lv.{wp.get('level', 0)}  ·  改造 {wp.get('upgrade', 0)}/5",
                font=_font(15),
                fill=FG_DIM,
            )
            # 副属性
            d.text(
                (wx + 150, y0 + 52),
                f"{wp.get('sec_name', '')} {wp.get('sec_text', '')}",
                font=_font(15),
                fill=ACCENT,
            )
        else:
            d.text((wx, y0 + 28), "未装备", font=_font(19), fill=FG_MUTED)

        # ---------- 驱动盘套装（右侧）----------
        sx = x0 + w - 262
        sets = av.get("sets") or []
        if sets:
            d.text((sx, y0 + 12), "驱动盘套装", font=_font(13), fill=FG_MUTED)
            sy = y0 + 28
            for s in sets[:2]:
                short = s.get("name") or ""
                if len(short) > 9:
                    short = short[:8] + "…"
                txt = f"{short} ×{s.get('count', 0)}"
                d.text((sx, sy), txt, font=_font(15), fill=FG_DIM)
                sy += 20
        else:
            d.text((sx, y0 + 28), "无驱动盘数据", font=_font(15), fill=FG_MUTED)

        # 序号
        idx_t = f"{idx + 1}"
        d.text(
            (x0 + w - 18 - _text_w(d, idx_t, _font(14)), y0 + card_h - 20),
            idx_t,
            font=_font(14),
            fill=FG_MUTED,
        )

        y += card_h + gap

    if has_more:
        t = f"还有 {len(avs) - max_show} 个角色未显示（列表按等级排序）"
        tw = _text_w(d, t, _font(18))
        d.text(((width - tw) // 2, y + 8), t, font=_font(18), fill=FG_MUTED)
        y += extra_h

    # ---------- 底部 ----------
    # 底部三行（固定位置，互不重叠）
    tip = "发「/绝区零面板 角色名」查看单角色详情（含驱动盘逐件词条）"
    tw = _text_w(d, tip, _font(18))
    d.text(((width - tw) // 2, height - 100), tip, font=_font(18), fill=ACCENT_DEEP)

    src = "数据来源 enka.network  ·  数值以游戏内为准，仅供参考"
    sw = _text_w(d, src, _font(14))
    d.text(((width - sw) // 2, height - 66), src, font=_font(14), fill=FG_MUTED)
    _ftxt = "Github：https://github.com/fyjgtddco/mihoyo-panel"
    _fw = _text_w(d, _ftxt, _font(15))
    d.text(((width - _fw) // 2, height - 34), _ftxt, font=_font(15), fill=ACCENT_DEEP)

    return _save_webp(img, 88)


# ============================================================
# 渲染 - 单角色详情
# ============================================================


def _section_title(d, x: int, y: int, title: str, width: int) -> None:
    d.rectangle((x, y + 3, x + 4, y + 18), fill=ACCENT)
    d.text((x + 12, y), title, font=_font(20, True), fill=FG)
    tw = _text_w(d, title, _font(20, True))
    d.line((x + 22 + tw, y + 11, x + width - 12, y + 11), fill=LINE)


def _draw_disc_card(d, x0: int, y0: int, w: int, h: int, disc: dict) -> None:
    """单件驱动盘卡片：槽位 / 强化等级 / 套装 / 主词条 / 4 条副词条 / 评分"""
    _rounded(d, (x0, y0, x0 + w, y0 + h), 10, BG_CARD)

    d.text((x0 + 16, y0 + 10), disc.get("slot_label") or "驱动盘", font=_font(18, True), fill=FG)

    lv = disc.get("level", 0)
    lv_t = f"+{lv}"
    lvf = _font(19, True)
    d.text((x0 + w - 16 - _text_w(d, lv_t, lvf), y0 + 9), lv_t, font=lvf, fill=ACCENT)

    sname = _truncate(d, disc.get("suit_name") or "", _font(14), w - 32)
    d.text((x0 + 16, y0 + 33), sname, font=_font(14), fill=FG_MUTED)

    d.line((x0 + 16, y0 + 52, x0 + w - 16, y0 + 52), fill=LINE)

    main = disc.get("main") or {}
    d.text((x0 + 16, y0 + 60), "主词条", font=_font(13), fill=FG_MUTED)
    d.text(
        (x0 + 16, y0 + 75),
        _truncate(d, main.get("name") or "-", _font(17), w - 150),
        font=_font(17),
        fill=FG_DIM,
    )

    mv = str(main.get("text") or "-")
    mvf = _font(23, True)
    d.text((x0 + w - 16 - _text_w(d, mv, mvf), y0 + 69), mv, font=mvf, fill=ACCENT)

    subs = disc.get("subs") or []
    col_w = (w - 32) // 2
    for i, s in enumerate(subs[:4]):
        col = i % 2
        row = i // 2
        sx = x0 + 16 + col * col_w
        sy = y0 + 106 + row * 26
        color = ACCENT if s.get("hot") else FG
        label = s.get("name") or ""
        d.text((sx, sy), label, font=_font(14), fill=FG_DIM)
        lw = _text_w(d, label, _font(14))
        d.text((sx + lw + 6, sy - 2), str(s.get("text") or "-"), font=_font(15, True), fill=color)

    sc = f"词条 {disc.get('score', 0):.1f}"
    scf = _font(13)
    d.text((x0 + w - 16 - _text_w(d, sc, scf), y0 + h - 20), sc, font=scf, fill=FG_MUTED)


def render_detail(av: dict, player: dict, width: int = 1100) -> bytes:
    """单角色详情大图：属性 + 音擎 + 6 件驱动盘逐件 + 套装 + 技能"""
    n = len(av.get("discs") or [])
    disc_rows = (n + 1) // 2 if n else 0
    disc_card_h = 172
    disc_gap = 12
    disc_block = disc_rows * disc_card_h + max(0, disc_rows - 1) * disc_gap

    est = 860 + disc_block + 700
    img = Image.new("RGB", (width, max(est, 900)), BG)
    d = ImageDraw.Draw(img)

    pad = 24
    cw = width - pad * 2
    y = 0

    # ================= 头部 =================
    header_h = 136
    d.rectangle((0, 0, width, header_h), fill=BG_CARD)
    accent = av.get("accent") or av.get("element_color") or FG_DIM
    d.rectangle((0, 0, 6, header_h), fill=accent)

    name = av.get("name") or "?"
    d.text((pad, 20), name, font=_font(41, True), fill=FG)
    nw = _text_w(d, name, _font(41, True))

    d.text((pad + nw + 14, 32), f"Lv.{av.get('level', 0)}", font=_font(22), fill=ACCENT)

    # 职业 + 元素（ZZZ 的 Rarity 字段不可靠，按需求不显示星级）
    info_line = (
        f"{av.get('profession_cn') or ''}  ·  "
        f"{av.get('element_cn') or ''}属性  ·  "
        f"晋升 {av.get('promotion', 0)}/6"
    )
    d.text((pad, 70), info_line, font=_font(19), fill=av.get("element_color") or FG_DIM)

    sub_line = (
        f"核心技 {av.get('core_skill', 0)}/6  ·  "
        f"天赋 {av.get('talent', 0)}  ·  驱动盘 {av.get('disc_count', 0)} 件"
    )
    d.text((pad, 98), sub_line, font=_font(17), fill=FG_MUTED)

    right = width - pad
    pt = f"{player.get('nickname', '')}  ·  绳网等级 {player.get('level', 0)}"
    ptf = _font(18)
    d.text((right - _text_w(d, pt, ptf), 28), pt, font=ptf, fill=FG_DIM)
    uid_t = f"UID {player.get('uid', '')}"
    uf = _font(15)
    d.text((right - _text_w(d, uid_t, uf), 56), uid_t, font=uf, fill=FG_MUTED)

    y = header_h + 22

    # ================= 角色基础属性（估算）=================
    _section_title(d, pad, y, "角色基础属性（按等级成长估算）", cw)
    y += 30

    base_stats = av.get("base_stats") or []
    cols = 4
    col_w = cw // cols
    box_h = 56
    for i, (label, val) in enumerate(base_stats):
        col = i % cols
        row = i // cols
        bx = pad + col * col_w
        by = y + row * (box_h + 8)
        _rounded(d, (bx, by, bx + col_w - 8, by + box_h), 8, BG_CARD)
        d.text((bx + 12, by + 9), label, font=_font(13), fill=FG_MUTED)
        vf = _font(22, True)
        while _text_w(d, val, vf) > col_w - 26 and vf.size > 11:
            vf = _font(vf.size - 1, True)
        d.text((bx + 12, by + 27), val, font=vf, fill=FG)

    rows = (len(base_stats) + cols - 1) // cols
    y += rows * (box_h + 8) + 10
    d.text((pad, y), "※ 该数值不含驱动盘 / 音擎加成，为本地估算", font=_font(13), fill=FG_MUTED)
    y += 26

    # ================= 装备加成汇总 =================
    bonuses = av.get("bonuses") or []
    _section_title(d, pad, y, "装备加成汇总（驱动盘副词条）", cw)
    y += 30
    if bonuses:
        per_row = 4
        col_w2 = cw // per_row
        for i, (label, val) in enumerate(bonuses[:16]):
            col = i % per_row
            r = i // per_row
            bx = pad + col * col_w2
            by = y + r * 28
            d.text((bx + 4, by), label, font=_font(14), fill=FG_MUTED)
            lw = _text_w(d, label, _font(14))
            color = ACCENT if label in ("暴击率", "暴击伤害") else FG
            d.text((bx + 4 + lw + 6, by - 2), val, font=_font(17, True), fill=color)
        rows2 = (min(len(bonuses), 16) + per_row - 1) // per_row
        y += rows2 * 28 + 12
    else:
        d.text((pad, y), "无驱动盘副词条数据", font=_font(17), fill=FG_MUTED)
        y += 32

    # ================= 音擎区 =================
    wp = av.get("weapon")
    _section_title(d, pad, y, "音擎", cw)
    y += 30
    if wp:
        card_h = 96
        _rounded(d, (pad, y, pad + cw, y + card_h), 10, BG_CARD)
        d.text(
            (pad + 16, y + 12),
            _truncate(d, wp.get("name") or "-", _font(24, True), cw - 200),
            font=_font(24, True),
            fill=FG,
        )
        d.text(
            (pad + 16, y + 46),
            f"Lv.{wp.get('level', 0)}  ·  突破 {wp.get('break_level', 0)}  ·  "
            f"改造 {wp.get('upgrade', 0)}/5",
            font=_font(17),
            fill=ACCENT,
        )
        d.text(
            (pad + 16, y + 70),
            f"主属性 {wp.get('main_name', '')} {wp.get('main_text', '')}（初始值）  ·  "
            f"副属性 {wp.get('sec_name', '')} {wp.get('sec_text', '')}",
            font=_font(15),
            fill=FG_DIM,
        )
        # 右侧：音擎阶级（3 星=B / 4 星=A / 5 星=S）
        rarity = int(wp.get("rarity") or 0)
        grade = {5: "S", 4: "A", 3: "B", 2: "B"}.get(rarity, "-")
        gf = _font(31, True)
        d.text((pad + cw - 20 - _text_w(d, grade, gf), y + 16), grade, font=gf, fill=ACCENT)
        d.text((pad + cw - 20 - _text_w(d, "音擎", _font(13)), y + 52), "音擎", font=_font(13), fill=FG_MUTED)
        y += card_h + 18
    else:
        d.text((pad, y), "未公开音擎数据", font=_font(17), fill=FG_MUTED)
        y += 34

    # ================= 驱动盘逐件（核心）=================
    discs = av.get("discs") or []
    _section_title(d, pad, y, f"驱动盘逐件（{len(discs)} 件）", cw)
    y += 30
    d.text((pad, y - 4), "副词条中暴击 / 爆伤以金色高亮", font=_font(13), fill=FG_MUTED)
    y += 20

    if discs:
        card_w = (cw - 14) // 2
        for i, disc in enumerate(discs):
            col = i % 2
            row = i // 2
            rx = pad + col * (card_w + 14)
            ry = y + row * (disc_card_h + disc_gap)
            _draw_disc_card(d, rx, ry, card_w, disc_card_h, disc)
        y += disc_block + 14
    else:
        d.text((pad, y), "该角色未公开驱动盘数据", font=_font(17), fill=FG_MUTED)
        y += 34

    # ================= 套装区 =================
    sets = av.get("sets") or []
    if sets:
        _section_title(d, pad, y, "驱动盘套装效果", cw)
        y += 32
        for s in sets:
            cnt = s.get("count", 0)
            sname = s.get("name") or "-"
            d.text((pad + 4, y), sname, font=_font(18, True), fill=FG)
            sw = _text_w(d, sname, _font(18, True))
            d.text((pad + 4 + sw + 10, y + 2), f"{cnt} 件", font=_font(15), fill=ACCENT)
            y += 24
            for label, val in s.get("bonus") or []:
                d.text((pad + 20, y), f"{label} {val}", font=_font(15), fill=FG_DIM)
                y += 20
            if not (s.get("bonus") or []):
                d.text((pad + 20, y), "见游戏内套装说明", font=_font(15), fill=FG_MUTED)
                y += 20
            y += 8

    # ================= 技能区 =================
    skills = av.get("skills") or []
    if skills:
        _section_title(d, pad, y, "技能等级", cw)
        y += 32
        sx = pad + 4
        for s in skills:
            label = s.get("name") or ""
            d.text((sx, y), label, font=_font(15), fill=FG_MUTED)
            lw = _text_w(d, label, _font(15))
            lvl = str(s.get("level", 0))
            d.text((sx + lw + 6, y - 3), lvl, font=_font(20, True), fill=FG)
            sx += lw + 6 + _text_w(d, lvl, _font(20, True)) + 30
        y += 34

    # ================= 底部 =================
    y += 8
    foot = "数据来源 enka.network  ·  属性为本地估算，仅供参考"
    ff = _font(14)
    if _text_w(d, foot, ff) > cw:
        foot = _truncate(d, foot, ff, cw)
    fw = _text_w(d, foot, ff)
    d.text(((width - fw) // 2, y), foot, font=ff, fill=FG_MUTED)
    y += 30
    _ftxt = "Github：https://github.com/fyjgtddco/mihoyo-panel"
    _fw = _text_w(d, _ftxt, _font(15))
    d.text(((width - _fw) // 2, y), _ftxt, font=_font(15), fill=ACCENT_DEEP)
    y += 28

    img = img.crop((0, 0, width, min(max(y, 300), img.height)))
    return _save_webp(img, 88)


# ===========================================================================
# 驱动盘列表（三游戏统一接口）
# ===========================================================================

def _get_common():
    """拿到 common 模块（兼容包导入 / 扁平导入 / 兜底）。"""
    # hsr.py / zzz.py 用 _C
    c = globals().get("_C")
    if c is not None:
        return c
    # gi.py 用扁平符号，需要动态 import
    try:
        from . import common as c2  # type: ignore
        return c2
    except Exception:
        pass
    try:
        import common as c2  # type: ignore
        return c2
    except Exception:
        return None


def collect_artifacts(player: dict) -> list:
    """绝区零：把所有角色的驱动盘摊平，按评分降序返回统一结构。

    实测驱动盘结构（zzz.py parse_disc 产出）：
        d["slot"]       槽位数字
        d["slot_name"]  槽位中文
        d["main_name"]  主词条名
        d["main_text"]  主词条显示值
        d["subs"]       [{"pid","is_pct","real_pct","real_flat","text","level"}]
        d["suit_name"]  套装名
    """
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')

    out = []
    for av in player.get("avatars") or []:
        owner = av.get("name") or ""
        for d in av.get("discs") or []:
            subs, scored = [], []
            for s in d.get("subs") or []:
                pid = str(s.get("pid") or "")
                is_pct = bool(s.get("is_pct"))
                try:
                    raw = abs(float(s.get("real_pct") if is_pct else s.get("real_flat")))
                except Exception:
                    raw = 0.0
                # 百分比词条已是百分数口径（9.6 = 9.6%）；
                # 定点词条（小攻击等）按社区惯例折算为 1/3 价值
                val = raw if is_pct else raw / 3.0
                try:
                    lv = max(1, int(s.get("level") or 1))
                except Exception:
                    lv = 1
                subs.append({
                    "text": (s.get("text") or "").strip(),
                    "highlight": bool(s.get("highlight")),
                })
                scored.append({"prop": pid, "value": val, "level": lv})

            try:
                score = cm.artifact_score(scored, True, game="zzz")
            except Exception:
                score = 0.0

            out.append({
                "name": d.get("main_name") or d.get("slot_name") or "驱动盘",
                "slot": d.get("slot_name") or str(d.get("slot") or ""),
                "set": d.get("suit_name") or "",
                "score": score,
                "grade": cm.score_grade(score, game="zzz"),
                "main_name": d.get("main_name") or "",
                "main_text": f"{d.get('main_name') or ''} {d.get('main_text') or ''}".strip(),
                "subs": subs,
                "rarity": int(d.get("rarity") or 4),
                "owner": owner,
                "plus": d.get("level") or 0,
            })

    out.sort(key=lambda x: -float(x.get("score") or 0))
    return out


def best_artifact_score(player: dict) -> float:
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')
    items = collect_artifacts(player)
    return float(items[0].get("score") or 0) if items else 0.0


def top_artifact(player: dict) -> dict | None:
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')
    items = collect_artifacts(player)
    return items[0] if items else None


# ===========================================================================
# v4：战绩 / 收集度 / 伤害计算
# ===========================================================================

def collect_record(player: dict) -> dict:
    """绝区零战绩：高难玩法（式舆防卫战 / 危局强袭战 / 零号空洞）。

    注：enka 的绝区零接口只给展示柜角色数据，不给战绩。
    所以这里展示的是「可推算的练度指标」，并在图上标注。
    """
    avatars = player.get("avatars") or []
    full60 = sum(1 for a in avatars if int(a.get("level") or 0) >= 60)
    core6 = sum(1 for a in avatars if int(a.get("core_skill") or 0) >= 6)
    discs = sum(len(a.get("discs") or []) for a in avatars)

    items = [
        ("式舆防卫战", "需游戏内查看", "双队评分挑战"),
        ("危局强袭战", "需游戏内查看", "高威胁敌人挑战"),
        ("零号空洞", "需游戏内查看", "Roguelike 探索"),
        ("满级代理人", f"{full60}/{len(avatars)}", "Lv60 代理人"),
        ("核心技满级", f"{core6}/{len(avatars)}", "核心技 6/6"),
        ("驱动盘总数", str(discs), "累计驱动盘"),
    ]

    hl = []
    if full60:
        hl.append(("满级代理人", str(full60)))
    if core6:
        hl.append(("核心技满级", str(core6)))
    if discs >= 36:
        hl.append(("驱动盘充足", str(discs)))

    return {
        "game_cn": "绝区零",
        "nickname": player.get("nickname") or "",
        "uid": player.get("uid") or "",
        "signature": player.get("signature") or "",
        "main": ("代理人练度", f"{len(avatars)} 名", "展示柜中的代理人"),
        "items": items,
        "highlights": hl,
    }


def collect_collection(player: dict) -> dict:
    """绝区零收集度。"""
    avatars = player.get("avatars") or []
    s_rank = sum(1 for a in avatars if int(a.get("level") or 0) >= 60)
    levels = [int(a.get("level") or 0) for a in avatars if a.get("level")]
    avg = round(sum(levels) / len(levels), 1) if levels else 0

    total = len(avatars) * 10
    return {
        "total": total,
        "five_star": s_rank,
        "six_star": 0,
        "char_count": len(avatars),
        "level_avg": avg,
        "fetter_sum": 0,
    }


def calc_damage(player: dict, av: dict, *, skill: str = "") -> dict:
    """绝区零伤害计算（用参考倍率）。"""
    cm = _get_common()
    base = av.get("base_stats") or {}

    def g(*keys, default=0.0):
        for k in keys:
            v = base.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    continue
        return default

    atk = g("攻击力", "atk", "Atk")
    crit = g("暴击率", "crit", "Crit")
    critdmg = g("暴击伤害", "critdmg", "CritDmg")

    crit_r = crit / 100.0 if crit > 1.5 else crit
    critd_r = critdmg / 100.0 if critdmg > 3 else critdmg

    char = av.get("name") or ""
    mult, exact = cm.skill_multiplier("zzz", char)
    mult_r = mult / 100.0

    crit_f = 1.0 + min(max(crit_r, 0.0), 1.0) * max(critd_r, 0.0)
    def_rate = 0.5  # 绝区零敌人基础防御减伤约 50%
    final = atk * mult_r * crit_f * def_rate

    tag = "" if exact else "（参考倍率）"
    return {
        "game_cn": "绝区零",
        "char": char,
        "owner": player.get("nickname") or "",
        "uid": player.get("uid") or "",
        "stats": [
            ("攻击力", f"{atk:,.0f}", cm.FG),
            ("暴击率", f"{crit:.1f}%", cm.FG),
            ("暴击伤害", f"{critdmg:.1f}%", cm.FG),
            ("暴击期望", f"{crit_f:.3f}", cm.ACCENT_DEEP),
        ],
        "skills": [
            ("采用倍率", f"{mult:.0f}%{tag}"),
            ("防御减伤", f"×{def_rate}"),
        ],
        "steps": [
            (f"攻击力 × 倍率 ({mult:.0f}%)", f"{atk * mult_r:,.0f}"),
            (f"× 暴击期望 ({crit_f:.3f})", f"{atk * mult_r * crit_f:,.0f}"),
            (f"× 防御减伤 {def_rate}", f"{final:,.0f}"),
        ],
        "final": ("单段期望伤害", f"{final:,.0f}"),
        "note": "※ 未计入属性克制与异常积蓄，实际数值会有浮动。",
    }


