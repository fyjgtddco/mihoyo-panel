"""
原神（Genshin Impact）面板解析与渲染

数据源：https://enka.network/api/uid/<uid>
  {"playerInfo": {...}, "avatarInfoList": [...], "uid": ..., "region": ...}

关键字段（均已用真实数据验证）：
  fightPropMap:
    2000 生命上限  2001 攻击力  2002 防御力
    20 暴击率(比例) 22 暴击伤害(比例) 23 元素充能(比例) 28 元素精通(整数)
    26 治疗加成(比例)
    40 火伤 41 雷伤 42 水伤 43 草伤 44 风伤 45 岩伤 46 冰伤 30 物伤（均为比例）
  propMap["4001"]["ival"] = 角色等级
  skillLevelMap = {技能ID: 等级}
  talentIdList = 命座ID列表（长度=命座数）
  equipList[i].flat:
    itemType: ITEM_RELIQUARY / ITEM_WEAPON
    equipType: EQUIP_BRACER 生之花 / EQUIP_NECKLACE 死之羽 / EQUIP_SHOES 时之沙
               EQUIP_RING 空之杯 / EQUIP_ONE_PIECE 理之冠
    setNameTextMapHash -> 套装中文名（查 loc.json["zh-cn"]）
    reliquaryMainstat: {mainPropId: FIGHT_PROP_*, statValue: <已是显示值>}
    reliquarySubstats: [{appendPropId: FIGHT_PROP_*, statValue: <已是显示值>}]
    reliquary.level: 21 表示 +20（1-based）

注意：flat 里的 statValue 已经是最终显示数值（17.9 就是 17.9%），不要再乘 100！
"""
from __future__ import annotations

import re

# ---------- 优先用 common.py，缺失时兜底自实现 ----------
# 说明：gi.py 只做单级包内相对导入，这是 AstrBot 的真实加载方式。
#   · 包导入（线上）：from .common 成功 -> 用 common，_HAS_COMMON=True
#   · 扁平导入（本地脚本）：相对导入失败 -> 走下方自实现兜底，_HAS_COMMON=False
# 兜底实现功能完整（含 method=2 的 WebP 编码），与 common 版本行为基本等价，
# 所以扁平场景下走兜底是可接受的。
#
# 历史教训：曾经在这里加过「二级兜底 import common」试图兼容扁平导入，
# 但下方兜底定义位于 except 块内、会无条件执行并覆盖二级绑定的函数，
# 造成 _HAS_COMMON=True 而实际用的是兜底（标志位撒谎）。
# 因此这里保持单级导入，让标志位与真实行为严格一致。
try:
    from .common import (  # type: ignore
        ACCENT, ACCENT_DEEP, ACCENT_SOFT, BAD, BG, BG_CARD, BG_SOFT,
        FG, FG_DIM, FG_MUTED, GOOD, LINE, LINE_STRONG,
        PanelError,
        draw_bar, draw_section, draw_star, draw_stars, draw_tag,
        elem_color, elem_cn, fmt_int, fmt_num, fmt_pct, font, new_image,
        rounded, save_webp, star_color, text_h, text_w, truncate, wrap_text,
    )

    _HAS_COMMON = True
except Exception:  # pragma: no cover
    _HAS_COMMON = False

    class PanelError(Exception):
        def __init__(self, message, hint="", code=0):
            super().__init__(message)
            self.message = message
            self.hint = hint
            self.code = code

        def __str__(self):
            return f"{self.message}\n{self.hint}" if self.hint else self.message

    BG = (28, 32, 44)
    BG_CARD = (38, 44, 58)
    FG = (232, 236, 245)
    FG_DIM = (150, 160, 178)
    FG_MUTED = (110, 120, 138)
    ACCENT = (212, 160, 56)
    ACCENT_DEEP = (158, 108, 18)
    ACCENT_SOFT = (250, 242, 224)
    LINE = (60, 68, 86)
    GOOD = (120, 220, 150)
    BAD = (235, 110, 110)

    _FONT_CACHE: dict = {}
    _FONT_PATH = None

    def _find_font():
        import os

        for p in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if os.path.exists(p):
                return p
        return None

    def font(size=16, bold=False):
        from PIL import ImageFont

        key = (size, bold)
        if key in _FONT_CACHE:
            return _FONT_CACHE[key]
        path = _FONT_PATH or _find_font()
        if path and bold and "Regular" in path:
            alt = path.replace("Regular", "Bold")
            import os

            if os.path.exists(alt):
                path = alt
        try:
            f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f

    def text_w(draw, s, f):
        try:
            b = draw.textbbox((0, 0), str(s), font=f)
            return b[2] - b[0]
        except Exception:
            return len(str(s)) * 10

    def text_h(draw, s, f):
        try:
            b = draw.textbbox((0, 0), str(s), font=f)
            return b[3] - b[1]
        except Exception:
            return 16

    def new_image(width, height, bg=BG, mode="RGB"):
        from PIL import Image

        return Image.new(mode, (width, height), bg)

    def rounded(draw, box, r=10, fill=None, outline=None, width=1):
        try:
            draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)
        except Exception:
            draw.rectangle(box, fill=fill, outline=outline, width=width)

    def draw_bar(draw, x, y, w, h, ratio, color, bg=None):
        bg = bg or LINE
        rounded(draw, (x, y, x + w, y + h), h // 2, bg)
        rw = max(0, min(w, int(w * ratio)))
        if rw > 1:
            rounded(draw, (x, y, x + rw, y + h), h // 2, color)

    def draw_tag(draw, x, y, text, color, f=None):
        f = f or font(17)
        w = text_w(draw, text, f) + 12
        h = text_h(draw, text, f) + 6
        rounded(draw, (x, y, x + w, y + h), 4, None, color, 1)
        draw.text((x + 6, y + 2), str(text), font=f, fill=color)
        return w

    def draw_star(draw, cx, cy, r=7, color=ACCENT):
        draw.polygon(
            [(cx, cy - r), (cx + r * 0.7, cy), (cx, cy + r), (cx - r * 0.7, cy)], fill=color
        )

    def draw_stars(draw, x, y, n, r=6, color=ACCENT, gap=3):
        for i in range(max(0, min(5, int(n)))):
            draw_star(draw, x + i * (r * 2 + gap) + r, y, r, color)

    def truncate(draw, text, f, max_w, ellipsis="…"):
        text = str(text)
        if text_w(draw, text, f) <= max_w:
            return text
        while text and text_w(draw, text + ellipsis, f) > max_w:
            text = text[:-1]
        return text + ellipsis

    def wrap_text(draw, text, f, max_w):
        text = str(text)
        out, cur = [], ""
        for ch in text:
            if text_w(draw, cur + ch, f) > max_w:
                out.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            out.append(cur)
        return out

    def draw_section(draw, x, y, w, title, f=None, color=ACCENT):
        f = f or font(22, True)
        draw.text((x, y), str(title), font=f, fill=color)
        ty = y + text_h(draw, title, f) + 5
        draw.line((x, ty, x + w, ty), fill=LINE, width=1)
        return ty + 9

    def save_webp(img, quality=88):
        import io

        # 兜底实现：仅在 common 导入失败时使用。
        # method 用 2（与 common.save_webp 一致）——method=6 是最慢档，
        # 实测同一画布 6 比 2 慢约 3.2 倍，体积仅差 5.7%。
        buf = io.BytesIO()
        try:
            img.save(buf, "WEBP", quality=int(quality), method=2)
        except Exception:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "WEBP", quality=int(quality))
        return buf.getvalue()

    def fmt_int(v):
        try:
            return f"{int(round(float(v))):,}"
        except Exception:
            return "0"

    def fmt_pct(v, digits=1):
        try:
            return f"{float(v) * 100:.{digits}f}%"
        except Exception:
            return "0%"

    def fmt_pct_raw(v, digits=1):
        try:
            return f"{float(v):.{digits}f}%"
        except Exception:
            return "0%"

    def fmt_num(v, digits=1):
        try:
            return f"{float(v):.{digits}f}"
        except Exception:
            return "0"

    def star_color(rarity):
        return {5: (255, 190, 90), 4: (170, 130, 220)}.get(int(rarity or 0), (150, 160, 178))

    _ELEM = {
        "火": (232, 89, 60),
        "水": (74, 159, 232),
        "风": (95, 201, 168),
        "雷": (169, 127, 232),
        "草": (124, 201, 74),
        "冰": (111, 211, 232),
        "岩": (212, 165, 60),
    }

    def elem_color(game, elem, default=FG_DIM):
        return _ELEM.get(str(elem or ""), default)

    def elem_cn(game, elem, default=""):
        return str(elem or default)


# ---------- 元素 ----------
ELEMENT_CN = {
    "Fire": "火",
    "Water": "水",
    "Wind": "风",
    "Electric": "雷",
    "Grass": "草",
    "Ice": "冰",
    "Rock": "岩",
    "Physical": "物理",
}

# 元素伤害加成属性 id（gi/relics.json Sets.AddProps 里的 key）
ELEM_BY_PROPID = {
    "40": "火",
    "41": "雷",
    "42": "水",
    "43": "草",
    "44": "风",
    "45": "岩",
    "46": "冰",
    "30": "物理",
}

# fightPropMap 里的元素伤害加成 / 物理伤害加成
ELEM_DMG_PROPS = {
    "40": ("火伤", "火"),
    "41": ("雷伤", "雷"),
    "42": ("水伤", "水"),
    "43": ("草伤", "草"),
    "44": ("风伤", "风"),
    "45": ("岩伤", "岩"),
    "46": ("冰伤", "冰"),
    "30": ("物伤", "物理"),
}

# ---------- 圣遗物 ----------
SLOT_CN = {
    "EQUIP_BRACER": "生之花",
    "EQUIP_NECKLACE": "死之羽",
    "EQUIP_SHOES": "时之沙",
    "EQUIP_RING": "空之杯",
    "EQUIP_ONE_PIECE": "理之冠",
    # 实测新版 enka 用 EQUIP_DRESS 表示理之冠
    "EQUIP_DRESS": "理之冠",
}

# 属性枚举 -> (中文名, 是否百分比)
PROP_CN = {
    "FIGHT_PROP_HP": ("生命值", False),
    "FIGHT_PROP_HP_PERCENT": ("生命值", True),
    "FIGHT_PROP_ATTACK": ("攻击力", False),
    "FIGHT_PROP_ATTACK_PERCENT": ("攻击力", True),
    "FIGHT_PROP_DEFENSE": ("防御力", False),
    "FIGHT_PROP_DEFENSE_PERCENT": ("防御力", True),
    "FIGHT_PROP_CRITICAL": ("暴击率", True),
    "FIGHT_PROP_CRITICAL_HURT": ("暴击伤害", True),
    "FIGHT_PROP_CHARGE_EFFICIENCY": ("元素充能", True),
    "FIGHT_PROP_ELEMENT_MASTERY": ("元素精通", False),
    "FIGHT_PROP_HEAL_ADD": ("治疗加成", True),
    "FIGHT_PROP_HEALED_ADD": ("受治疗加成", True),
    "FIGHT_PROP_FIRE_ADD_HURT": ("火伤加成", True),
    "FIGHT_PROP_WATER_ADD_HURT": ("水伤加成", True),
    "FIGHT_PROP_WIND_ADD_HURT": ("风伤加成", True),
    "FIGHT_PROP_ELECTRIC_ADD_HURT": ("雷伤加成", True),
    "FIGHT_PROP_GRASS_ADD_HURT": ("草伤加成", True),
    "FIGHT_PROP_ICE_ADD_HURT": ("冰伤加成", True),
    "FIGHT_PROP_ROCK_ADD_HURT": ("岩伤加成", True),
    "FIGHT_PROP_PHYSICAL_ADD_HURT": ("物伤加成", True),
    "FIGHT_PROP_SHIELD_COST_MINUS": ("护盾强效", True),
}

# 副词条价值评估：最高单次强化值（用于归一化）
SUBSTAT_MAX = {
    "FIGHT_PROP_CRITICAL": 3.89,
    "FIGHT_PROP_CRITICAL_HURT": 7.77,
    "FIGHT_PROP_ATTACK_PERCENT": 5.83,
    "FIGHT_PROP_HP_PERCENT": 5.83,
    "FIGHT_PROP_DEFENSE_PERCENT": 7.29,
    "FIGHT_PROP_CHARGE_EFFICIENCY": 6.48,
    "FIGHT_PROP_ELEMENT_MASTERY": 23.31,
    "FIGHT_PROP_HP": 298.75,
    "FIGHT_PROP_ATTACK": 19.45,
    "FIGHT_PROP_DEFENSE": 23.15,
}

SUBSTAT_WEIGHT = {
    "FIGHT_PROP_CRITICAL": 1.0,
    "FIGHT_PROP_CRITICAL_HURT": 1.0,
    "FIGHT_PROP_ATTACK_PERCENT": 0.85,
    "FIGHT_PROP_HP_PERCENT": 0.55,
    "FIGHT_PROP_DEFENSE_PERCENT": 0.4,
    "FIGHT_PROP_CHARGE_EFFICIENCY": 0.7,
    "FIGHT_PROP_ELEMENT_MASTERY": 0.5,
    "FIGHT_PROP_HP": 0.25,
    "FIGHT_PROP_ATTACK": 0.35,
    "FIGHT_PROP_DEFENSE": 0.25,
}

# 高亮词条（金色）
HIGHLIGHT_PROPS = {"FIGHT_PROP_CRITICAL", "FIGHT_PROP_CRITICAL_HURT"}


def _prop_text(prop_id: str, value) -> str:
    """把 FIGHT_PROP_* + statValue 转成显示字符串"""
    name, is_pct = PROP_CN.get(prop_id, (prop_id.replace("FIGHT_PROP_", ""), False))
    try:
        v = float(value)
    except Exception:
        return name
    if is_pct:
        return f"{name} {v:.1f}%"
    return f"{name} {fmt_int(v) if v >= 10 else f'{v:.1f}'}"


def _prop_name(prop_id: str) -> str:
    return PROP_CN.get(prop_id, (prop_id.replace("FIGHT_PROP_", ""), False))[0]


def _num(a: dict, k: str, default: float = 0.0) -> float:
    try:
        return float((a.get("fightPropMap") or {}).get(k, default) or default)
    except Exception:
        return default


def _prop(a: dict, k: str) -> str:
    v = (a.get("propMap") or {}).get(k) or {}
    return str(v.get("ival") or v.get("val") or "0")


def _drawer(img):
    """创建 ImageDraw 对象（避免在函数里重复 import）"""
    from PIL import ImageDraw

    return ImageDraw.Draw(img)


# ---------- 解析上下文 ----------


class _Ctx:
    """解析上下文：持有三张映射表"""

    def __init__(self, store=None):
        self.store = store
        self._avatars = {}
        self._loc = {}
        self._relic_sets = {}
        self._set_name_to_id: dict[str, str] = {}

        if store is not None:
            try:
                self._avatars = store.get("gi/avatars") or {}
            except Exception:
                self._avatars = {}
            try:
                self._loc = store.loc_gi() or {}
            except Exception:
                self._loc = {}
            try:
                relics = store.get("gi/relics") or {}
                # 实测结构: {"Items": {...}, "Sets": {"10001": {"Name": "<hash>"}}}
                self._relic_sets = relics.get("Sets") or {}
                if not self._relic_sets and isinstance(relics, dict):
                    # 兼容 {setId: {...}} 直接平铺的形式
                    if relics and all(str(k).isdigit() for k in list(relics.keys())[:5]):
                        self._relic_sets = relics
            except Exception:
                self._relic_sets = {}

            # 建 套装中文名 -> setID 反查表
            try:
                for sid, info in (self._relic_sets or {}).items():
                    if not isinstance(info, dict):
                        continue
                    h = info.get("Name")
                    if isinstance(h, dict):
                        h = h.get("Hash")
                    cn = self.loc(h)
                    if cn:
                        self._set_name_to_id[cn] = str(sid)
            except Exception:
                self._set_name_to_id = {}

    def loc(self, h) -> str:
        if h is None:
            return ""
        return self._loc.get(str(h), "") or ""

    def avatar_meta(self, aid: str) -> dict:
        return self._avatars.get(str(aid)) or {}

    def set_name(self, set_hash, set_id) -> str:
        # 优先用 enka 直接给的 setNameTextMapHash
        nm = self.loc(set_hash)
        if nm:
            return nm

        # 回退：gi/relics.json 的 Sets[str(setId)]["Name"] 是 loc hash 字符串
        info = self._relic_sets.get(str(set_id))
        if isinstance(info, str):
            return self.loc(info) or ""
        if isinstance(info, dict):
            h = info.get("NameTextMapHash")
            if not h:
                nm_obj = info.get("Name")
                if isinstance(nm_obj, (str, int)):
                    h = nm_obj
                elif isinstance(nm_obj, dict):
                    h = nm_obj.get("Hash")
            if h is None:
                return ""
            return self.loc(h) or ""
        return ""

    def set_add_props(self, set_name: str) -> dict:
        """按套装中文名反查 2 件套加成 {propId: value}"""
        key = self._set_name_to_id.get(set_name)
        if not key:
            return {}
        info = self._relic_sets.get(str(key)) or {}
        if isinstance(info, dict):
            return info.get("AddProps") or {}
        return {}


# ---------- 解析 ----------


def parse_avatar(a: dict, ctx: _Ctx) -> dict:
    aid = str(a.get("avatarId", ""))
    meta = ctx.avatar_meta(aid)

    # 角色名：优先 avatars.json 的 NameTextMapHash，兜底角色 ID
    cname = ctx.loc(meta.get("NameTextMapHash"))
    if not cname:
        cname = f"角色{aid}"

    elem_en = meta.get("Element", "")
    elem = ELEMENT_CN.get(elem_en, elem_en)
    rarity = int(meta.get("Rarity") or 5)
    level = _prop(a, "4001")

    # 命座数
    consts = len(a.get("talentIdList") or [])

    # 天赋
    skill_map = a.get("skillLevelMap") or {}
    extra = a.get("proudSkillExtraLevelMap") or {}
    skills = []
    for sid, lv in list(skill_map.items())[:3]:
        bonus = 0
        for k, v in extra.items():
            if str(k)[:4] == str(sid)[:4]:
                bonus = max(bonus, int(v))
                break
        skills.append({"id": str(sid), "level": int(lv), "bonus": bonus})

    # 核心属性
    hp = _num(a, "2000")
    atk = _num(a, "2001")
    dfn = _num(a, "2002")
    crit = _num(a, "20")
    critdmg = _num(a, "22")
    er = _num(a, "23")
    em = _num(a, "28")
    heal = _num(a, "26")

    core = [
        ("生命值", fmt_int(hp)),
        ("攻击力", fmt_int(atk)),
        ("防御力", fmt_int(dfn)),
        ("元素精通", fmt_int(em)),
        ("暴击率", fmt_pct(crit)),
        ("暴击伤害", fmt_pct(critdmg)),
        ("元素充能", fmt_pct(er)),
    ]

    # 装备
    weapon = None
    relics = []
    set_count: dict[str, int] = {}

    for eq in a.get("equipList") or []:
        flat = eq.get("flat") or {}
        itype = flat.get("itemType", "")

        if itype == "ITEM_WEAPON":
            w = eq.get("weapon") or {}
            refine = 1
            am = w.get("affixMap") or {}
            if am:
                try:
                    refine = max(int(v) for v in am.values()) + 1
                except Exception:
                    refine = 1
            wname = ctx.loc(flat.get("nameTextMapHash")) or "武器"
            weapon = {
                "name": wname,
                "level": int(w.get("level") or 0),
                "refine": refine,
                "rarity": int(flat.get("rankLevel") or 4),
            }

        elif itype == "ITEM_RELIQUARY":
            rel = eq.get("reliquary") or {}
            rel_level = int(rel.get("level") or 0)

            main = flat.get("reliquaryMainstat") or {}
            main_id = str(main.get("mainPropId") or "")
            main_val = main.get("statValue")

            subs = []
            for s in flat.get("reliquarySubstats") or []:
                pid = str(s.get("appendPropId") or "")
                val = s.get("statValue")
                try:
                    fv = float(val)
                except Exception:
                    fv = 0.0
                mx = SUBSTAT_MAX.get(pid, 0.0)
                w = SUBSTAT_WEIGHT.get(pid, 0.0)
                ratio = (fv / mx) if mx > 0 else 0.0
                subs.append(
                    {
                        "prop": pid,
                        "name": _prop_name(pid),
                        "value": fv,
                        "text": _prop_text(pid, val),
                        "weight": ratio * w,
                        "highlight": pid in HIGHLIGHT_PROPS,
                    }
                )

            # 词条价值 0~1：按 4 条副词条的「强化次数 × 有效权重」评估
            # 实测满级20 圣遗物权重和常见 3.5~6.6，取 7.0 作为理论上限
            score = min(1.0, sum(s["weight"] for s in subs) / 7.0)

            try:
                set_name = ctx.set_name(flat.get("setNameTextMapHash"), flat.get("setId"))
            except Exception:
                set_name = ""
            if set_name:
                set_count[set_name] = set_count.get(set_name, 0) + 1

            relics.append(
                {
                    "slot": SLOT_CN.get(flat.get("equipType", ""), "圣遗物"),
                    "slot_en": flat.get("equipType", ""),
                    "set": set_name,
                    "level": rel_level,
                    "plus": max(0, rel_level - 1),
                    "main_name": _prop_name(main_id),
                    "main_text": _prop_text(main_id, main_val),
                    "main_id": main_id,
                    "main_value": main_val,
                    "rarity": int(flat.get("rankLevel") or 5),
                    "subs": subs,
                    "score": score,
                }
            )

    # 套装效果
    sets = []
    for sname, cnt in sorted(set_count.items(), key=lambda x: -x[1]):
        if cnt >= 4:
            sets.append(f"{sname} 4件")
        elif cnt >= 2:
            sets.append(f"{sname} 2件")

    # ---------- 元素伤害加成：三路汇总（必须在 relics 构建之后）----------
    elem_dmg_map: dict[str, float] = {}

    # 1) fightPropMap 直接给的（enka 只对部分角色提供）
    for pid, (label, cn_elem) in ELEM_DMG_PROPS.items():
        v = _num(a, pid)
        if v > 0.001:
            elem_dmg_map[cn_elem] = max(elem_dmg_map.get(cn_elem, 0.0), v)

    # 2) 空之杯主词条（flat.statValue 已是百分数）
    MAIN_DMG_MAP = {
        "FIGHT_PROP_FIRE_ADD_HURT": "火",
        "FIGHT_PROP_WATER_ADD_HURT": "水",
        "FIGHT_PROP_WIND_ADD_HURT": "风",
        "FIGHT_PROP_ELECTRIC_ADD_HURT": "雷",
        "FIGHT_PROP_GRASS_ADD_HURT": "草",
        "FIGHT_PROP_ICE_ADD_HURT": "冰",
        "FIGHT_PROP_ROCK_ADD_HURT": "岩",
        "FIGHT_PROP_PHYSICAL_ADD_HURT": "物理",
    }
    for r in relics:
        mid = r.get("main_id") or ""
        k = MAIN_DMG_MAP.get(mid)
        if not k:
            continue
        try:
            mv = float(r.get("main_value") or 0) / 100.0
        except Exception:
            mv = 0.0
        if mv > 0:
            elem_dmg_map[k] = max(elem_dmg_map.get(k, 0.0), mv)

    # 3) 套装 2 件套加成
    for sname, cnt in set_count.items():
        if cnt < 2:
            continue
        bonus = ctx.set_add_props(sname)
        if not isinstance(bonus, dict):
            continue
        for pid, val in bonus.items():
            cn_elem = ELEM_BY_PROPID.get(str(pid))
            if not cn_elem:
                continue
            try:
                fv = float(val)
            except Exception:
                continue
            if fv > 0.001:
                elem_dmg_map[cn_elem] = max(elem_dmg_map.get(cn_elem, 0.0), fv)

    elem_dmg = []
    for cn_elem, v in sorted(elem_dmg_map.items(), key=lambda x: -x[1]):
        label = "物伤" if cn_elem == "物理" else f"{cn_elem}伤"
        elem_dmg.append(
            {
                "label": label,
                "value": fmt_pct(v),
                "ratio": v,
                "color": elem_color("gi", cn_elem),
            }
        )

    # 角色自身元素的伤害加成优先用于期望伤害
    own_bonus = elem_dmg_map.get(elem, 0.0)

    # 期望伤害估算
    talent_sum = sum(s["level"] + s.get("bonus", 0) for s in skills)
    talent_factor = 1.0 + talent_sum / 60.0
    # 优先用角色自身元素加成，没有则取最大加成
    main_dmg = own_bonus if own_bonus > 0 else (
        max([e["ratio"] for e in elem_dmg]) if elem_dmg else 0.0
    )
    crit_f = 1.0 + min(max(crit, 0.0), 1.0) * max(critdmg, 0.0)
    dmg_index = atk * crit_f * (1.0 + main_dmg) * talent_factor

    # 圣遗物平均分
    relic_score = 0.0
    if relics:
        relic_score = sum(r["score"] for r in relics) / len(relics)

    return {
        "id": aid,
        "name": cname,
        "element": elem,
        "element_en": elem_en,
        "rarity": rarity,
        "level": level,
        "constellation": consts,
        "core": core,
        "core_map": {
            "hp": hp,
            "atk": atk,
            "def": dfn,
            "crit": crit,
            "critdmg": critdmg,
            "er": er,
            "em": em,
            "heal": heal,
        },
        "elem_dmg": elem_dmg,
        "skills": skills,
        "weapon": weapon,
        "relics": relics,
        "relic_sets": sets,
        "relic_score": relic_score,
        "fetter": int((a.get("fetterInfo") or {}).get("expLevel") or 0),
        "dmg_index": dmg_index,
        "talent_factor": talent_factor,
        "_raw": a,
    }


def parse_player(raw: dict, store=None) -> dict:
    """整份 enka 响应 -> 渲染用 player 结构"""
    pi = (raw or {}).get("playerInfo") or {}
    al = (raw or {}).get("avatarInfoList") or []

    if not pi and not al:
        raise PanelError(
            "这个 UID 没有公开的原神数据。",
            "请在米游社 App → 我的角色 → 打开「显示角色详情」后重试。",
        )

    ctx = _Ctx(store)
    avatars = []
    for a in al:
        try:
            avatars.append(parse_avatar(a, ctx))
        except Exception:
            continue

    avatars.sort(key=lambda x: (-int(x.get("level") or 0), -int(x.get("rarity") or 0)))

    # 相对评级：按 dmg_index 排名
    if avatars:
        ordered = sorted(avatars, key=lambda x: -float(x.get("dmg_index") or 0))
        n = len(ordered)
        for rank, av in enumerate(ordered):
            p = rank / max(1, n - 1) if n > 1 else 0.0
            if p <= 0.15:
                av["grade"] = "S"
            elif p <= 0.40:
                av["grade"] = "A"
            elif p <= 0.70:
                av["grade"] = "B"
            else:
                av["grade"] = "C"

    return {
        "fetter_count": int(pi.get("fetterCount") or 0),
        "theater_act": int(pi.get("theaterActIndex") or 0),
        "theater_mode": int(pi.get("theaterModeIndex") or 0),
        "theater_star": int(pi.get("theaterStarIndex") or 0),
        "game": "gi",
        "uid": str((raw or {}).get("uid") or pi.get("uid") or ""),
        "nickname": pi.get("nickname") or "旅行者",
        "level": int(pi.get("level") or 0),
        "world_level": int(pi.get("worldLevel") or 0),
        "achievement": int(pi.get("finishAchievementNum") or 0),
        "tower_floor": int(pi.get("towerFloorIndex") or 0),
        "tower_level": int(pi.get("towerLevelIndex") or 0),
        "tower_star": int(pi.get("towerStarIndex") or 0),
        "signature": pi.get("signature") or "",
        "avatar_count": len(avatars),
        "avatars": avatars,
        "has_data": bool(al),
    }


# ---------- 渲染：角色列表 ----------


def _header(d, player: dict, width: int, title_suffix: str = "") -> int:
    """玩家头部，返回 y"""
    h = 132
    d.rectangle((0, 0, width, h), fill=BG_CARD)

    name = player.get("nickname") or "旅行者"
    d.text((30, 26), truncate(d, name, font(41, True), width - 320), font=font(41, True), fill=FG)

    lv = player.get("level") or 0
    wl = player.get("world_level") or 0
    d.text((30, 74), f"冒险等阶 {lv}", font=font(22), fill=FG_DIM)
    d.text((150, 74), f"世界等级 {wl}", font=font(22), fill=FG_DIM)

    cnt = player.get("avatar_count") or 0
    d.text((30, 100), f"展示角色 {cnt} 个" + title_suffix, font=font(18), fill=FG_MUTED)

    # 右侧
    rx = width - 30
    tf = player.get("tower_floor") or 0
    tl = player.get("tower_level") or 0
    ts = player.get("tower_star") or 0
    if tf:
        t1 = f"深境螺旋 {tf}-{tl}" + (f"  ★{ts}" if ts else "")
        w1 = text_w(d, t1, font(23))
        d.text((rx - w1, 30), t1, font=font(23), fill=ACCENT)

    ach = player.get("achievement") or 0
    t2 = f"成就 {ach}"
    w2 = text_w(d, t2, font(20))
    d.text((rx - w2, 60), t2, font=font(20), fill=FG_DIM)

    t3 = f"UID {player.get('uid') or ''}"
    w3 = text_w(d, t3, font(18))
    d.text((rx - w3, 86), t3, font=font(18), fill=FG_MUTED)

    return h


def _char_row(d, av: dict, x0: int, y0: int, w: int) -> int:
    """单个角色紧凑行，返回行高"""
    card_h = 168
    rounded(d, (x0, y0, x0 + w, y0 + card_h), 10, BG_CARD)

    ec = elem_color("gi", av.get("element"))
    d.rectangle((x0, y0, x0 + 5, y0 + card_h), fill=ec)

    tx = x0 + 22
    ty = y0 + 14

    # 第一行：角色名（左）
    name = av.get("name") or "?"
    d.text((tx, ty), truncate(d, name, font(29, True), 200), font=font(29, True), fill=FG)

    # 第二行：星级 + 等级（同一行，不重叠）
    sy = ty + 34
    stars = int(av.get("rarity") or 5)
    draw_stars(d, tx, sy + 5, stars, size=5, color=star_color(av.get("rarity")))
    star_w = stars * 12
    d.text((tx + star_w + 10, sy), f"Lv.{av.get('level')}", font=font(20, True), fill=ACCENT)

    # 第三行：元素 + 好感 + 评级
    ey = sy + 24
    elem = av.get("element") or ""
    d.text((tx, ey), elem, font=font(19), fill=ec)
    ew = text_w(d, elem, font(19))

    fetter = av.get("fetter") or 0
    if fetter:
        d.text((tx + ew + 12, ey), f"好感{fetter}", font=font(17), fill=FG_MUTED)

    grade = av.get("grade") or ""
    if grade:
        gcolor = {"S": ACCENT, "A": GOOD, "B": FG_DIM, "C": FG_MUTED}.get(grade, FG_DIM)
        gx = tx + ew + 12 + (text_w(d, f"好感{fetter}", font(17)) + 16 if fetter else 0)
        draw_tag(d, gx, ey - 2, grade, gcolor, font(15))

    # 中间属性 2 列 x 4 行
    core = av.get("core") or []
    ax = x0 + 250
    ay = y0 + 12
    for i, (k, v) in enumerate(core):
        col = i % 2
        row = i // 2
        cx = ax + col * 142
        cy = ay + row * 26
        d.text((cx, cy), k, font=font(17), fill=FG_MUTED)
        kw = text_w(d, k, font(17))
        d.text((cx + kw + 6, cy - 1), v, font=font(18, True), fill=FG)

    # 元素伤害
    ed = av.get("elem_dmg") or []
    ey2 = ay + 4 * 26 + 2
    for e in ed[:2]:
        d.text((ax, ey2), e["label"], font=font(15), fill=FG_MUTED)
        lw = text_w(d, e["label"], font(15))
        d.text((ax + lw + 5, ey2), e["value"], font=font(17, True), fill=e["color"])
        ey2 += 18

    # 右侧武器
    wx = x0 + w - 220
    wy = y0 + 14
    wp = av.get("weapon")
    d.text((wx, wy), "武器", font=font(15), fill=FG_MUTED)
    if wp:
        d.text(
            (wx, wy + 18),
            truncate(d, wp.get("name") or "", font(19, True), 200),
            font=font(19, True),
            fill=FG,
        )
        d.text(
            (wx, wy + 42),
            f"精{wp.get('refine')}  Lv.{wp.get('level')}",
            font=font(17),
            fill=FG_DIM,
        )

    # 套装
    sy2 = wy + 68
    rs = av.get("relic_sets") or []
    if rs:
        for s in rs[:2]:
            d.text((wx, sy2), truncate(d, s, font(15), 200), font=font(15), fill=FG_DIM)
            sy2 += 17

    # 底部：天赋（左）+ 圣遗物评分（右）
    by = y0 + card_h - 32
    skills = av.get("skills") or []
    labels = ["普攻", "战技", "爆发"]
    sx = tx
    d.text((sx, by), "天赋", font=font(15), fill=FG_MUTED)
    sx += 38
    for i, sk in enumerate(skills[:3]):
        txt = f"{labels[i]}{sk['level']}"
        if sk.get("bonus"):
            txt += f"+{sk['bonus']}"
        d.text((sx, by), txt, font=font(15), fill=FG_DIM)
        sx += text_w(d, txt, font(15)) + 12

    rs_score = float(av.get("relic_score") or 0)
    d.text((wx, by), "圣遗物", font=font(15), fill=FG_MUTED)
    draw_bar(d, wx + 46, by + 4, 110, 7, rs_score, ACCENT if rs_score >= 0.5 else FG_DIM, LINE)
    d.text(
        (wx + 46 + 118, by),
        f"{rs_score * 100:.0f}",
        font=font(15, True),
        fill=ACCENT if rs_score >= 0.5 else FG_DIM,
    )

    return card_h


def render_list(player: dict, width: int = 1100) -> bytes:
    """角色列表图"""
    avatars = player.get("avatars") or []
    show = avatars[:12]
    has_more = len(avatars) > 12

    card_h = 168
    gap = 12
    header_h = 132
    foot_h = 110

    H = header_h + 18 + len(show) * (card_h + gap)
    if has_more:
        H += 44
    H += foot_h

    img = new_image(width, H, BG)
    # common.new_image 返回 (img, draw) 二元组
    d = img[1] if isinstance(img, tuple) else _drawer(img)
    img = img[0] if isinstance(img, tuple) else img

    _header(d, player, width)

    y = header_h + 18
    for av in show:
        _char_row(d, av, 24, y, width - 48)
        y += card_h + gap

    if has_more:
        txt = f"还有 {len(avatars) - 12} 个角色未显示"
        tw = text_w(d, txt, font(19))
        d.text(((width - tw) // 2, y + 6), txt, font=font(19), fill=FG_MUTED)
        y += 44

    # 底部提示（三行不重叠：操作提示 / 数据来源 / 项目主页）
    tip = "发  /原神面板 角色名  查看单角色详情"
    tw = text_w(d, tip, font(18))
    d.text(((width - tw) // 2, H - 98), tip, font=font(18), fill=ACCENT_DEEP)

    src = "数据来源 enka.network · 默认显示前 12 个角色"
    sw = text_w(d, src, font(14))
    d.text(((width - sw) // 2, H - 66), src, font=font(14), fill=FG_MUTED)

    ftxt = "Github：https://github.com/fyjgtddco/mihoyo-panel"
    fw = text_w(d, ftxt, font(15))
    d.text(((width - fw) // 2, H - 34), ftxt, font=font(15), fill=ACCENT_DEEP)

    return save_webp(img, 88)


# ---------- 渲染：单角色详情 ----------


def _relic_card(d, x0: int, y0: int, w: int, h: int, rel: dict) -> None:
    """单件圣遗物卡片"""
    rounded(d, (x0, y0, x0 + w, y0 + h), 9, BG_CARD)

    # 稀有度顶部条
    rc = star_color(rel.get("rarity") or 5)
    d.rectangle((x0, y0, x0 + w, y0 + 3), fill=rc)

    # 部位 + 强化等级（同一行，强化等级右对齐且下移避开顶部色条）
    d.text((x0 + 12, y0 + 12), rel.get("slot") or "", font=font(19, True), fill=FG_DIM)

    lv_txt = f"+{rel.get('plus') or 0}"
    lw = text_w(d, lv_txt, font(20, True))
    lc = ACCENT if (rel.get("plus") or 0) >= 20 else FG
    d.text((x0 + w - lw - 14, y0 + 12), lv_txt, font=font(20, True), fill=lc)

    # 主词条
    d.text(
        (x0 + 12, y0 + 38),
        truncate(d, rel.get("main_text") or "", font(20, True), w - 24),
        font=font(20, True),
        fill=ACCENT,
    )

    # 套装名
    if rel.get("set"):
        d.text(
            (x0 + 12, y0 + 62),
            truncate(d, rel["set"], font(15), w - 24),
            font=font(15),
            fill=FG_MUTED,
        )

    # 副词条 4 条
    sy = y0 + 82
    for s in (rel.get("subs") or [])[:4]:
        color = ACCENT if s.get("highlight") else FG_DIM
        d.text(
            (x0 + 12, sy),
            truncate(d, s.get("text") or "", font(17), w - 60),
            font=font(17),
            fill=color,
        )
        sy += 19

    # 词条价值条（右下角）
    score = float(rel.get("score") or 0)
    bx = x0 + w - 46
    draw_bar(d, bx, y0 + h - 22, 32, 6, score, GOOD if score >= 0.5 else FG_MUTED, LINE)


def render_detail(av: dict, player: dict, width: int = 1100) -> bytes:
    """单角色详情图（含圣遗物逐件 + 期望伤害）"""
    relics = av.get("relics") or []

    # 高度估算
    header_h = 150
    stats_h = 190
    relic_row_h = 198
    relic_rows = (len(relics) + 1) // 2
    relic_h = 46 + relic_rows * relic_row_h + 60
    weapon_h = 118
    skill_h = 92
    dmg_h = 132
    foot_h = 84

    H = header_h + stats_h + relic_h + weapon_h + skill_h + dmg_h + foot_h

    img = new_image(width, H, BG)
    # common.new_image 返回 (img, draw) 二元组
    d = img[1] if isinstance(img, tuple) else _drawer(img)
    img = img[0] if isinstance(img, tuple) else img

    # ---------- 头部 ----------
    ec = elem_color("gi", av.get("element"))
    d.rectangle((0, 0, width, 6), fill=ec)
    d.rectangle((0, 0, width, header_h), fill=BG_CARD)

    name = av.get("name") or "?"
    d.text((30, 24), truncate(d, name, font(46, True), width - 420), font=font(46, True), fill=FG)

    draw_stars(d, 30, 78, av.get("rarity") or 5, size=7, color=star_color(av.get("rarity")))

    lv = av.get("level") or 0
    d.text((30, 100), f"Lv.{lv}", font=font(24, True), fill=ACCENT)

    elem_txt = av.get("element") or ""
    d.text((110, 102), elem_txt, font=font(22), fill=ec)

    fetter = av.get("fetter") or 0
    d.text((160, 102), f"好感 {fetter}", font=font(19), fill=FG_MUTED)

    consts = av.get("constellation") or 0
    d.text((250, 102), f"{consts}命", font=font(19), fill=FG_DIM)

    # 右上角：玩家 + UID
    rx = width - 30
    t1 = player.get("nickname") or ""
    w1 = text_w(d, t1, font(22))
    d.text((rx - w1, 28), t1, font=font(22), fill=FG_DIM)
    t2 = f"UID {player.get('uid') or ''}"
    w2 = text_w(d, t2, font(18))
    d.text((rx - w2, 54), t2, font=font(18), fill=FG_MUTED)

    grade = av.get("grade") or ""
    if grade:
        gcolor = {"S": ACCENT, "A": GOOD, "B": FG_DIM, "C": FG_MUTED}.get(grade, FG_DIM)
        gtxt = f"评价 {grade}"
        gw = text_w(d, gtxt, font(23, True))
        d.text((rx - gw, 80), gtxt, font=font(23, True), fill=gcolor)

    # ---------- 属性区 ----------
    y = header_h + 8
    y = draw_section(d, 26, y, width - 52, "属性面板", font(22, True))

    core = av.get("core") or []
    ax, ay = 32, y
    for i, (k, v) in enumerate(core):
        col = i % 4
        row = i // 4
        cx = ax + col * ((width - 64) // 4)
        cy = ay + row * 46
        d.text((cx, cy), k, font=font(17), fill=FG_MUTED)
        d.text((cx, cy + 18), v, font=font(24, True), fill=FG)

    # 元素伤害加成
    ed = av.get("elem_dmg") or []
    ey = ay + 96
    if ed:
        d.text((ax, ey), "伤害加成", font=font(17), fill=FG_MUTED)
        ex = ax + 80
        for e in ed[:4]:
            txt = f"{e['label']} {e['value']}"
            d.text((ex, ey), txt, font=font(18, True), fill=e["color"])
            ex += text_w(d, txt, font(18, True)) + 20
    else:
        d.text((ax, ey), "伤害加成", font=font(17), fill=FG_MUTED)
        d.text(
            (ax + 80, ey),
            "无（空之杯主词条非元素伤害）",
            font=font(17),
            fill=FG_MUTED,
        )

    # 治疗加成
    heal = (av.get("core_map") or {}).get("heal") or 0
    if heal > 0.001:
        d.text((ax, ey + 26), f"治疗加成 {fmt_pct(heal)}", font=font(18), fill=GOOD)

    y = ay + 132

    # ---------- 圣遗物区 ----------
    y = draw_section(
        d, 26, y, width - 52, f"圣遗物（{len(relics)} 件）", font(22, True)
    )

    # 套装摘要
    rs = av.get("relic_sets") or []
    if rs:
        sx = 32
        for s in rs[:3]:
            tw = text_w(d, s, font(17))
            rounded(d, (sx - 4, y - 2, sx + tw + 8, y + 20), 4, BG_CARD)
            d.text((sx + 2, y + 1), s, font=font(17), fill=ACCENT)
            sx += tw + 20
        y += 30

    # 圣遗物卡片 2 列
    card_w = (width - 64 - 16) // 2
    for idx, rel in enumerate(relics):
        col = idx % 2
        row = idx // 2
        x0 = 32 + col * (card_w + 16)
        y0 = y + row * relic_row_h
        _relic_card(d, x0, y0, card_w, 182, rel)

    y += relic_rows * relic_row_h
    y += 12

    # ---------- 武器区 ----------
    y = draw_section(d, 26, y, width - 52, "武器", font(22, True))

    wp = av.get("weapon")
    if wp:
        d.text((32, y), truncate(d, wp.get("name") or "", font(26, True), width - 240), font=font(26, True), fill=FG)
        d.text(
            (32, y + 30),
            f"精炼 {wp.get('refine')} 阶　Lv.{wp.get('level')}",
            font=font(18),
            fill=FG_DIM,
        )
        y += 62
    else:
        d.text((32, y), "未装备武器", font=font(19), fill=FG_MUTED)
        y += 40

    # ---------- 天赋 ----------
    y = draw_section(d, 26, y, width - 52, "天赋", font(22, True))
    labels = ["普通攻击", "元素战技", "元素爆发"]
    skills = av.get("skills") or []
    sx = 32
    for i, sk in enumerate(skills[:3]):
        lvl = sk.get("level") or 0
        bonus = sk.get("bonus") or 0
        txt = f"{labels[i]}  {lvl}"
        if bonus:
            txt += f"+{bonus}"
        d.text((sx, y), txt, font=font(19), fill=FG)
        sx += text_w(d, txt, font(19)) + 32
    y += 40

    # ---------- 期望伤害 ----------
    y = draw_section(d, 26, y, width - 52, "期望伤害估算", font(22, True))

    cm = av.get("core_map") or {}
    atk = cm.get("atk") or 0
    crit = cm.get("crit") or 0
    critdmg = cm.get("critdmg") or 0
    crit_f = 1.0 + min(max(crit, 0.0), 1.0) * max(critdmg, 0.0)
    main_dmg = max([e["ratio"] for e in (av.get("elem_dmg") or [])] or [0.0])
    own = av.get("elem_dmg") or []
    own_elem = av.get("element") or ""
    for e in own:
        if own_elem and own_elem in e.get("label", ""):
            main_dmg = e["ratio"]
            break
    tf_ = av.get("talent_factor") or 1.0
    idx_val = av.get("dmg_index") or 0

    # 分项
    items = [
        ("攻击力", fmt_int(atk)),
        ("暴击期望", f"{crit_f:.3f}"),
        ("伤害加成", fmt_pct(main_dmg)),
        ("天赋系数", f"{tf_:.2f}"),
    ]
    ix = 32
    for k, v in items:
        d.text((ix, y), k, font=font(15), fill=FG_MUTED)
        d.text((ix, y + 16), v, font=font(19, True), fill=FG)
        ix += (width - 64) // 4

    y += 46
    d.text((32, y), "综合指数", font=font(17), fill=FG_MUTED)
    d.text((110, y - 2), f"{idx_val:,.0f}", font=font(28, True), fill=ACCENT)

    note = "※ 本地估算，仅供参考，与实际伤害无直接对应关系"
    d.text((32, y + 30), note, font=font(15), fill=FG_MUTED)

    # ---------- 底部 ----------
    foot = "数据来源 enka.network"
    fw = text_w(d, foot, font(14))
    d.text(((width - fw) // 2, H - 50), foot, font=font(14), fill=FG_MUTED)
    ftxt = "Github：https://github.com/fyjgtddco/mihoyo-panel"
    fw2 = text_w(d, ftxt, font(15))
    d.text(((width - fw2) // 2, H - 26), ftxt, font=font(15), fill=ACCENT_DEEP)

    return save_webp(img, 88)


# ===========================================================================
# 圣遗物列表（三游戏统一接口）
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
    """把所有角色的圣遗物摊平，按评分降序返回统一结构。"""
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')
    out = []
    for av in player.get("avatars") or []:
        owner = av.get("name") or ""
        elem = av.get("element") or ""
        for r in av.get("relics") or []:
            subs, scored = [], []
            for s in r.get("subs") or []:
                pid = str(s.get("prop") or "")
                try:
                    val = abs(float(s.get("value") or 0))
                except Exception:
                    val = 0.0
                subs.append({
                    "text": s.get("text") or "",
                    "highlight": bool(s.get("highlight")),
                })
                scored.append({"prop": pid, "value": val, "level": 1})

            main_id = str(r.get("main_id") or "")
            main_ok = _gi_main_matches(main_id, elem)
            try:
                score = cm.artifact_score(scored, main_ok, game="gi")
            except Exception:
                score = 0.0

            out.append({
                "name": r.get("main_name") or r.get("slot") or "圣遗物",
                "slot": r.get("slot") or "",
                "set": r.get("set") or "",
                "score": score,
                "grade": cm.score_grade(score, game="gi"),
                "main_name": r.get("main_name") or "",
                "main_text": r.get("main_text") or "",
                "subs": subs,
                "rarity": int(r.get("rarity") or 5),
                "owner": owner,
                "plus": r.get("plus") or 0,
            })

    out.sort(key=lambda x: -float(x.get("score") or 0))
    return out


def _gi_main_matches(main_id: str, elem: str) -> bool:
    """主词条是否与角色需求对口。"""
    if not main_id:
        return True
    if main_id in (
        "FIGHT_PROP_CRITICAL",
        "FIGHT_PROP_CRITICAL_HURT",
        "FIGHT_PROP_ATTACK_PERCENT",
        "FIGHT_PROP_CHARGE_EFFICIENCY",
    ):
        return True
    want = {
        "火": "FIGHT_PROP_FIRE_ADD_HURT",
        "水": "FIGHT_PROP_WATER_ADD_HURT",
        "风": "FIGHT_PROP_WIND_ADD_HURT",
        "雷": "FIGHT_PROP_ELECTRIC_ADD_HURT",
        "草": "FIGHT_PROP_GRASS_ADD_HURT",
        "冰": "FIGHT_PROP_ICE_ADD_HURT",
        "岩": "FIGHT_PROP_ROCK_ADD_HURT",
        "物理": "FIGHT_PROP_PHYSICAL_ADD_HURT",
    }.get(elem or "")
    return bool(want and main_id == want)


def best_artifact_score(player: dict) -> float:
    """该玩家圣遗物最高分（排行榜用）。"""
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')
    items = collect_artifacts(player)
    return float(items[0].get("score") or 0) if items else 0.0


def top_artifact(player: dict) -> dict | None:
    """该玩家最好的一件圣遗物。"""
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')
    items = collect_artifacts(player)
    return items[0] if items else None


# ===========================================================================
# v4：战绩 / 收集度 / 伤害计算
# ===========================================================================

def collect_record(player: dict) -> dict:
    """原神战绩：三大高难副本（深境螺旋 / 幻想真境剧诗 / 幽境危战）。

    深境螺旋   —— 每月 16 日刷新，12 层，满星 36
    幻想真境剧诗 —— 每月 1 日刷新，限制元素类型，考验角色池深度
    幽境危战   —— 每版本第二个周三，6 个难度（N1~N6）
    """
    tf = int(player.get("tower_floor") or 0)
    tl = int(player.get("tower_level") or 0)
    ts = int(player.get("tower_star") or 0)
    ta = int(player.get("theater_act") or 0)
    tm = int(player.get("theater_mode") or 0)
    tstar = int(player.get("theater_star") or 0)

    # 深境螺旋
    abyss_val = f"{tf}-{tl}" if tf else "未通关"
    abyss_desc = f"本期 ★{ts}/36" if ts else "尚未挑战"

    # 幻想真境剧诗：mode 6+ 为月谕模式
    if ta:
        mode_cn = "月谕模式" if tm >= 6 else "常规模式"
        thea_val = f"第 {ta} 幕"
        thea_desc = f"{mode_cn} · ★{tstar}"
    else:
        thea_val, thea_desc = "未通关", "尚未挑战"

    items = [
        ("深境螺旋", abyss_val, abyss_desc),
        ("幻想真境剧诗", thea_val, thea_desc),
        ("成就达成", str(player.get("achievement") or 0), "累计成就数"),
        ("展示角色", str(player.get("avatar_count") or 0), "公开的角色详情数"),
    ]

    hl = []
    if ts >= 36:
        hl.append(("深渊满星", "★36"))
    elif tf >= 12:
        hl.append(("通关12层", f"★{ts}"))
    if tm >= 6:
        hl.append(("月谕模式", f"第{ta}幕"))
    if ta >= 10:
        hl.append(("剧诗通关", f"{ta}幕"))

    return {
        "game_cn": "原神",
        "nickname": player.get("nickname") or "",
        "uid": player.get("uid") or "",
        "signature": player.get("signature") or "",
        "main": ("深境螺旋进度", abyss_val, abyss_desc),
        "items": items,
        "highlights": hl,
    }


def collect_collection(player: dict) -> dict:
    """原神收集度。"""
    avatars = player.get("avatars") or []
    five = sum(1 for a in avatars if int(a.get("rarity") or 0) >= 5)
    four = sum(1 for a in avatars if int(a.get("rarity") or 0) == 4)
    levels = [int(a.get("level") or 0) for a in avatars if a.get("level")]
    avg = round(sum(levels) / len(levels), 1) if levels else 0

    # 收集度 = 五星权重高（5星=10，4星=5）
    total = five * 10 + four * 5
    return {
        "total": total,
        "five_star": five,
        "six_star": 0,
        "char_count": len(avatars),
        "level_avg": avg,
        "fetter_sum": player.get("fetter_count") or 0,
    }


def calc_damage(player: dict, av: dict, *, skill: str = "") -> dict:
    """原神伤害计算：用真实/参考技能倍率算单段伤害。

    公式（业界通用近似）：
        单段伤害 = 攻击力 × 技能倍率 × 暴击期望 × (1 + 增伤) × 抗性系数
        暴击期望 = 1 + min(暴击率,1) × 暴击伤害
        抗性系数取 0.9（大多数敌人 10% 抗性）
    """
    cm = _get_common()
    core = av.get("core_map") or {}
    atk = float(core.get("atk") or 0)
    crit = float(core.get("crit") or 0)
    critdmg = float(core.get("critdmg") or 0)
    em = float(core.get("em") or 0)

    elem = av.get("element") or ""
    elem_bonus = 0.0
    for e in av.get("elem_dmg") or []:
        if elem and elem in (e.get("label") or ""):
            elem_bonus = float(e.get("ratio") or 0)
            break

    char = av.get("name") or ""
    mult, exact = cm.skill_multiplier("gi", char)
    mult_r = mult / 100.0

    crit_f = 1.0 + min(max(crit, 0.0), 1.0) * max(critdmg, 0.0)
    resist = 0.9

    # 分步
    step1 = atk * mult_r                       # 倍率区
    step2 = step1 * crit_f                     # 暴击区
    step3 = step2 * (1.0 + elem_bonus)         # 增伤区
    final = step3 * resist                     # 抗性区

    tag = "" if exact else "（参考倍率）"
    return {
        "game_cn": "原神",
        "char": char,
        "owner": player.get("nickname") or "",
        "uid": player.get("uid") or "",
        "stats": [
            ("攻击力", f"{atk:,.0f}", cm.FG),
            ("暴击率", f"{crit*100:.1f}%", cm.FG),
            ("暴击伤害", f"{critdmg*100:.1f}%", cm.FG),
            ("元素加成", f"{elem_bonus*100:.1f}%", cm.elem_color("gi", elem)),
            ("元素精通", f"{em:.0f}", cm.FG),
            ("暴击期望", f"{crit_f:.3f}", cm.ACCENT_DEEP),
        ],
        "skills": [
            ("采用技能倍率", f"{mult:.0f}%{tag}"),
            ("抗性系数", f"×{resist}"),
        ],
        "steps": [
            (f"攻击力 × 倍率 ({mult:.0f}%)", f"{step1:,.0f}"),
            (f"× 暴击期望 ({crit_f:.3f})", f"{step2:,.0f}"),
            (f"× (1 + 增伤 {elem_bonus*100:.1f}%)", f"{step3:,.0f}"),
            (f"× 抗性 {resist}", f"{final:,.0f}"),
        ],
        "final": ("单段期望伤害", f"{final:,.0f}"),
        "note": (
            "※ 单段期望值，含暴击期望与 10% 抗性。"
            + ("倍率取自参考表。" if exact else "该角色未收录精确倍率，按通用系数估算。")
        ),
    }


