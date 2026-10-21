# -*- coding: utf-8 -*-
"""
崩铁（崩坏：星穹铁道）面板渲染模块

两级渲染（解决"一片一片挤在一起"的问题）：
    render_list(player, width=900)       -> 角色列表概览图（一屏看清全部角色）
    render_detail(av, player, width=900) -> 单角色详情大图（属性 + 光锥 + 6 件遗器逐件 + 套装 + 行迹）

数据来源：https://enka.network/api/hsr/uid/<UID>

崩铁 enka 已经把遗器/光锥的最终数值算好放进 _flat.props：
    equipment._flat.props = [BaseHP, BaseAttack, BaseDefence]
    relic._flat.props[0]  = 主词条
    relic._flat.props[1:] = 副词条（与 subAffixList 一一对应）
所以本模块不自己算词条，直接取 _flat 里的值。

中文名解析（实测确认，非常重要）：
    崩铁的角色名 / 光锥名 / 遗器套装名 / 属性名 / 词条名，
    全部存在 store/hsr/hsr.json 的 "zh-cn" 字典里（键是 18~20 位 hash 字符串）。
    注意：**不是** store/loc.json！store/loc.json 的 zh-cn 只有原神内容，
    崩铁的 hash 在里面一个都查不到（实测 0 命中）。
    hsr.json 顶层是语言代码（zh-cn/en/ja/...），所以必须：
        hsr_data["zh-cn"][str(hash)]
    实测覆盖率：角色名 98/98、遗器套装 62/62、光锥 170/170 全部命中。

依赖：标准库 + Pillow。优先复用 common.py 的绘图基元，common 不可用时本文件自带兜底。
"""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

# ---------- 尝试复用 common.py（缺失时逐项兜底）----------
_C = None
try:  # 包内导入（AstrBot 插件方式）
    from . import common as _C
except Exception:
    try:  # 顶层导入（脚本直跑方式）
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
    """全局只扫描一次字体文件（Linux 容器 + Windows 开发机都覆盖）"""
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
    """兜底字体加载（带缓存）"""
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
    """比例 -> 百分比字符串（0.08748 -> '8.7%'）"""
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except Exception:
        return "0%"


def _fb_truncate(draw, text: str, f, max_w: int) -> str:
    """超宽文本截断并加省略号"""
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


# ---------- 逐项绑定：common 有就用 common，没有就用兜底 ----------


def _bind(name: str, fallback):
    """从 common 取属性/函数，取不到就用兜底"""
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
_common_fmt_pct = _bind("fmt_pct", None)
_common_truncate = _bind("truncate", None)


def _font(size: int, bold: bool = False):
    """优先用 common.font；common 缺失或返回非矢量字体（中文会变豆腐块）时用兜底"""
    if _common_font is not None:
        try:
            f = _common_font(size, bold)
            # load_default() 返回的不是 FreeTypeFont，无法渲染中文 -> 换兜底
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


def _fmt_pct(v, digits: int = 1) -> str:
    if _common_fmt_pct is not None:
        try:
            return str(_common_fmt_pct(v, digits))
        except Exception:
            pass
    return _fb_fmt_pct(v, digits)


def _truncate(draw, text: str, f, max_w: int) -> str:
    if _common_truncate is not None:
        try:
            return str(_common_truncate(draw, text, f, max_w))
        except Exception:
            pass
    return _fb_truncate(draw, text, f, max_w)


# ============================================================
# 崩铁静态表
# ============================================================

# 元素英文 -> 中文
_FB_ELEM_CN = {
    "Fire": "火",
    "Ice": "冰",
    "Wind": "风",
    "Thunder": "雷",
    "Lightning": "雷",
    "Physical": "物理",
    "Quantum": "量子",
    "Imaginary": "虚数",
}

# 元素英文 -> RGB（深色主题下够亮、互相可区分）
_FB_ELEM_COLOR = {
    "Fire": (232, 89, 60),
    "Ice": (111, 211, 232),
    "Wind": (95, 201, 168),
    "Thunder": (169, 127, 232),
    "Lightning": (169, 127, 232),
    "Physical": (198, 198, 198),
    "Quantum": (108, 96, 208),
    "Imaginary": (240, 200, 90),
}

# 命途（AvatarBaseType）英文 -> 中文（store 里没有，硬编码）
_FB_PATH_CN = {
    "Warrior": "毁灭",
    "Rogue": "巡猎",
    "Mage": "智识",
    "Shaman": "同谐",
    "Warlock": "虚无",
    "Knight": "存护",
    "Priest": "丰饶",
    "Memory": "记忆",
    "Elation": "欢愉",
}

# 遗器部位：relicList[].type
_RELIC_SLOT_CN = {
    1: "头部",
    2: "手部",
    3: "躯干",
    4: "脚部",
    5: "位面球",
    6: "连结绳",
}

# 属性英文名 -> 中文名兜底表
# 实测：hsr.json["zh-cn"] 的键就是这些英文枚举（如 CriticalChance -> 暴击率），
# 样本中实际出现的 31 个 type 有 30 个能命中，只有 SpeedAddedRatio 缺失。
# 另外 hsr.json 里没有 HPRatio / AttackRatio / DefenceRatio / SPBase 这些写法（实测 .get() 为 None），
# 这里保留一份完整兜底，保证任何情况下界面都不会直接显示英文枚举名。
_FB_PROP_CN = {
    "HPDelta": "生命值",
    "HPAddedRatio": "生命值",
    "HPRatio": "生命值%",
    "HPRatioBase": "生命值%",
    "AttackDelta": "攻击力",
    "AttackAddedRatio": "攻击力",
    "AttackRatio": "攻击力%",
    "DefenceDelta": "防御力",
    "DefenceAddedRatio": "防御力",
    "DefenceRatio": "防御力%",
    "SpeedDelta": "速度",
    "SpeedAddedRatio": "速度",
    "CriticalChance": "暴击率",
    "CriticalChanceBase": "暴击率",
    "CriticalDamage": "暴击伤害",
    "CriticalDamageBase": "暴击伤害",
    "BreakDamageAddedRatio": "击破特攻",
    "BreakDamageAddedRatioBase": "击破特攻",
    "StatusProbability": "效果命中",
    "StatusProbabilityBase": "效果命中",
    "StatusResistance": "效果抵抗",
    "StatusResistanceBase": "效果抵抗",
    "SPRatio": "能量恢复效率",
    "SPRatioBase": "能量恢复效率",
    "SPBase": "能量恢复效率",
    "MaxSP": "能量上限",
    "HealRatio": "治疗量加成",
    "HealRatioBase": "治疗量加成",
    "HealTakenRatio": "受治疗量提高",
    "BaseHP": "基础生命值",
    "BaseAttack": "基础攻击力",
    "BaseDefence": "基础防御力",
    "BaseSpeed": "速度",
    "PhysicalAddedRatio": "物理属性伤害提高",
    "FireAddedRatio": "火属性伤害提高",
    "IceAddedRatio": "冰属性伤害提高",
    "ThunderAddedRatio": "雷属性伤害提高",
    "WindAddedRatio": "风属性伤害提高",
    "QuantumAddedRatio": "量子属性伤害提高",
    "ImaginaryAddedRatio": "虚数属性伤害提高",
    "PhysicalResistance": "物理属性抗性提高",
    "FireResistance": "火属性抗性提高",
    "IceResistance": "冰属性抗性提高",
    "ThunderResistance": "雷属性抗性提高",
    "WindResistance": "风属性抗性提高",
    "QuantumResistance": "量子属性抗性提高",
    "ImaginaryResistance": "虚数属性抗性提高",
    "ElationDamageAddedRatio": "欢愉度",
    "ElationDamageAddedRatioBase": "欢愉度",
    "StanceBreakAddedRatio": "削韧值提高",
}

# 固定值类属性（其余按百分比处理）
_FIXED_PROPS = {
    "HPDelta",
    "AttackDelta",
    "DefenceDelta",
    "SpeedDelta",
    "BaseHP",
    "BaseAttack",
    "BaseDefence",
    "BaseSpeed",
}

# 需要金色高亮的词条（暴击 / 爆伤 / 速度）
_HOT_PROPS = {
    "CriticalChance",
    "CriticalChanceBase",
    "CriticalDamage",
    "CriticalDamageBase",
    "SpeedDelta",
    "SpeedAddedRatio",
}

# 单次强化标准值（用于遗器评分，单位：百分比数值或固定值）
_AFFIX_ROLL = {
    "CriticalChance": 3.24,
    "CriticalChanceBase": 3.24,
    "CriticalDamage": 6.48,
    "CriticalDamageBase": 6.48,
    "SpeedDelta": 2.0,
    "SpeedAddedRatio": 2.0,
    "AttackAddedRatio": 3.888,
    "HPAddedRatio": 3.888,
    "DefenceAddedRatio": 4.32,
    "BreakDamageAddedRatio": 6.48,
    "BreakDamageAddedRatioBase": 6.48,
    "StatusProbability": 4.32,
    "StatusProbabilityBase": 4.32,
    "StatusResistance": 4.32,
    "StatusResistanceBase": 4.32,
}

# 词条权重（暴击/爆伤/速度最有价值）
_AFFIX_WEIGHT = {
    "CriticalChance": 1.0,
    "CriticalChanceBase": 1.0,
    "CriticalDamage": 1.0,
    "CriticalDamageBase": 1.0,
    "SpeedDelta": 0.9,
    "SpeedAddedRatio": 0.9,
    "AttackAddedRatio": 0.8,
    "HPAddedRatio": 0.8,
    "DefenceAddedRatio": 0.8,
}

# 技能末尾编号 -> 技能名（pointId % 1000）
_SKILL_TAIL_CN = {
    1: "普攻",
    2: "战技",
    3: "终结技",
    4: "天赋",
    7: "秘技",
}

# 光锥叠影数量上限
_MAX_RANK = 5


# ============================================================
# store 读取辅助
# ============================================================


def _store_get(store, name: str) -> dict:
    """同步读取 LocalStore 里的字典；store 为空/异常一律返回 {}"""
    if store is None:
        return {}
    if isinstance(store, dict):
        d = store.get(name)
        return d if isinstance(d, dict) else {}
    # common.LocalStore 提供 get / get_cached（均为同步）
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


def _is_fixed_prop(ptype: str) -> bool:
    """判断属性是固定值（True）还是百分比（False）"""
    return ptype in _FIXED_PROPS


def _prop_value_text(ptype: str, value: float) -> str:
    """把 _flat.props 里的原始值格式化成显示文本"""
    try:
        v = float(value)
    except Exception:
        return "-"
    if _is_fixed_prop(ptype):
        # 速度可能是小数，其余取整
        if ptype in ("SpeedDelta", "SpeedAddedRatio"):
            return f"{v:.1f}" if abs(v - round(v)) > 0.05 else f"{int(round(v))}"
        return _fmt_int(v)
    return _fmt_pct(v)


# ============================================================
# 解析
# ============================================================


class _Ctx:
    """解析上下文：持有 store 各表 + 中文名字典 + 缓存"""

    def __init__(self, store):
        self.avatars = _store_get(store, "hsr/avatars")
        self.relics = _store_get(store, "hsr/relics")
        self.affixes = _store_get(store, "hsr/affixes")
        self.weapons = _store_get(store, "hsr/weapons")
        self.tree = _store_get(store, "hsr/tree")

        # 崩铁中文名：hsr/hsr.json 的 "zh-cn"（**不是** loc.json！）
        raw_loc = _store_get(store, "hsr/hsr")
        if isinstance(raw_loc.get("zh-cn"), dict):
            self.loc = raw_loc["zh-cn"]
        elif raw_loc and all(isinstance(k, str) for k in list(raw_loc)[:3]):
            # 某些实现可能直接返回扁平字典，兼容一下
            self.loc = raw_loc
        else:
            self.loc = {}

    # ---------- 名字解析 ----------

    def name_of_hash(self, h, fallback: str = "") -> str:
        """按 loc hash 查中文名（hsr/hsr.json 的 zh-cn）"""
        if h is None:
            return fallback
        return self.loc.get(str(h)) or fallback

    def prop_cn(self, ptype: str) -> str:
        """属性英文名 -> 中文名

        优先查 hsr.json["zh-cn"]（实测 31 个实际出现的 type 命中 30 个），
        再用内置兜底表补齐（如 hsr.json 里没有的 SpeedAddedRatio / HPRatio），
        最后才原样返回英文，保证界面上不会出现英文枚举名。
        """
        if not ptype:
            return ""
        return self.loc.get(ptype) or _FB_PROP_CN.get(ptype) or ptype

    def avatar_meta(self, aid: str) -> dict:
        return self.avatars.get(str(aid)) or {}

    def weapon_meta(self, tid) -> dict:
        return self.weapons.get(str(tid)) or {}

    def relic_meta(self, tid) -> dict:
        items = self.relics.get("Items") or {}
        return items.get(str(tid)) or {}

    def set_name(self, set_id) -> str:
        sets = self.relics.get("Sets") or {}
        s = sets.get(str(set_id)) or {}
        h = s.get("Name")
        return self.name_of_hash(h, f"套装#{set_id}")

    def set_effects(self, set_id) -> dict:
        """套装效果：{'2': [(词条中文, 值文本)], '4': [...]}"""
        sets = self.relics.get("Sets") or {}
        s = sets.get(str(set_id)) or {}
        skills = s.get("SetSkills") or {}
        out: dict = {}
        for cnt in ("2", "4"):
            props = ((skills.get(cnt) or {}).get("props")) or {}
            rows = []
            for ptype, val in props.items():
                rows.append((self.prop_cn(ptype), _prop_value_text(ptype, val)))
            out[cnt] = rows
        return out

    # ---------- 基础属性 ----------

    def base_stats(self, meta: dict, level: int, promotion: int) -> dict:
        """角色基础属性：查 avatars.json 的 Promotion 表插值"""
        promo = meta.get("Promotion") or {}
        try:
            lv = max(1, int(level or 1))
        except Exception:
            lv = 1
        idx = max(0, min(int(promotion or 0), max(0, len(promo) - 1)))
        p = promo.get(str(idx)) or promo.get(str(promotion)) or {}

        def _f(key, default=0.0):
            try:
                return float(p.get(key, default) or default)
            except Exception:
                return default

        hp = _f("HPBase") + _f("HPAdd") * (lv - 1)
        atk = _f("AttackBase") + _f("AttackAdd") * (lv - 1)
        df = _f("DefenceBase") + _f("DefenceAdd") * (lv - 1)
        spd = _f("SpeedBase", 100.0)
        cb = _f("CriticalChance", 0.05)
        cd = _f("CriticalDamage", 0.5)
        return {"hp": hp, "atk": atk, "def": df, "speed": spd, "crit": cb, "cdmg": cd}

    def weapon_base(self, wmeta: dict, level: int, promotion: int) -> dict:
        """光锥基础属性（白值）：Promotion 表插值"""
        promo = wmeta.get("Promotion") or {}
        try:
            lv = max(1, int(level or 1))
        except Exception:
            lv = 1
        idx = max(0, min(int(promotion or 0), max(0, len(promo) - 1)))
        p = promo.get(str(idx)) or promo.get(str(promotion)) or {}

        def _f(key, default=0.0):
            try:
                return float(p.get(key, default) or default)
            except Exception:
                return default

        return {
            "hp": _f("BaseHP") + _f("BaseHPAdd") * (lv - 1),
            "atk": _f("BaseAttack") + _f("BaseAttackAdd") * (lv - 1),
            "df": _f("BaseDefence") + _f("BaseDefenceAdd") * (lv - 1),
        }

    # ---------- 遗器 ----------

    def parse_relic(self, r: dict) -> dict:
        """单件遗器 -> 渲染用结构"""
        flat = r.get("_flat") or {}
        props = flat.get("props") or []
        rtype = int(r.get("type") or 0)
        level = int(r.get("level") or 0)

        # props[0] 主词条，其余副词条
        main_ptype, main_value = "", 0.0
        if props:
            p0 = props[0] or {}
            main_ptype = str(p0.get("type") or "")
            try:
                main_value = float(p0.get("value") or 0)
            except Exception:
                main_value = 0.0

        subs = []
        score = 0.0
        for p in props[1:]:
            if not isinstance(p, dict):
                continue
            ptype = str(p.get("type") or "")
            try:
                val = float(p.get("value") or 0)
            except Exception:
                val = 0.0
            if not ptype:
                continue
            subs.append(
                {
                    "type": ptype,
                    "name": self.prop_cn(ptype),
                    "text": _prop_value_text(ptype, val),
                    "hot": ptype in _HOT_PROPS,
                }
            )
            # 词条评分（近似"有效词条数"，满分约 9）
            unit = _AFFIX_ROLL.get(ptype)
            if unit:
                shown = val if _is_fixed_prop(ptype) else val * 100.0
                score += (shown / unit) * _AFFIX_WEIGHT.get(ptype, 0.5)

        set_id = flat.get("setID") or 0
        return {
            "slot_type": rtype,
            "slot": _RELIC_SLOT_CN.get(rtype, f"部位{rtype}"),
            "level": level,
            "main": {
                "type": main_ptype,
                "name": self.prop_cn(main_ptype),
                "text": _prop_value_text(main_ptype, main_value),
            },
            "subs": subs,
            "set_id": set_id,
            "set_name": self.set_name(set_id),
            "score": min(9.0, round(score, 1)),
            "_props": props,
        }

    # ---------- 行迹 ----------

    def parse_traces(self, av: dict) -> list:
        """行迹（属性加成节点）：skillTreeList + tree.json 的 props

        返回按词条聚合后的 [(中文名, 值文本, 原始比例/数值)]，方便详情页排版。
        """
        if not self.tree:
            return []
        # 按词条名聚合：同一词条多个行迹节点合并显示
        agg_ratio: dict = {}
        agg_fixed: dict = {}
        seen = set()
        for s in av.get("skillTreeList") or []:
            try:
                pid = int(s.get("pointId") or 0)
                lv = int(s.get("level") or 0)
            except Exception:
                continue
            if lv < 1:
                continue
            node = self._tree_node(pid)
            if not isinstance(node, dict):
                continue
            key = str(pid)
            if key in seen:
                continue
            props = ((node.get("1") or {}).get("props")) or {}
            if props:
                seen.add(key)
            for ptype, val in props.items():
                try:
                    pv = float(val or 0)
                except Exception:
                    continue
                if _is_fixed_prop(ptype):
                    agg_fixed[ptype] = agg_fixed.get(ptype, 0.0) + pv
                else:
                    agg_ratio[ptype] = agg_ratio.get(ptype, 0.0) + pv

        # 排序：百分比在前（更直观），固定值在后
        out = []
        for ptype, val in sorted(agg_ratio.items(), key=lambda kv: -abs(kv[1])):
            out.append((self.prop_cn(ptype), _prop_value_text(ptype, val), val))
        for ptype, val in sorted(agg_fixed.items(), key=lambda kv: -abs(kv[1])):
            out.append((self.prop_cn(ptype), _prop_value_text(ptype, val), val))
        return out

    def _tree_node(self, pid: int):
        """在 tree.json 里找属性行迹节点（只直接查，不做任何号段回退）

        实测结论（我最初判断错了，这里记录正确结论）：
        tree.json 共 1080 个键，7 位 980 个 + 8 位 100 个，**全部节点都带 props**。
        skillTreeList 里同时混着两类节点：
          - 属性行迹节点（如 11102201 / 1407201）-> tree.json 里有，直接查得到
          - 技能等级节点（如 11102001 普攻 / 11102002 战技）-> tree.json 里**本来就没有**，
            返回 None 是正确行为（它们不是属性，不该进面板属性累加）
        样本 8 个角色共 149 个节点，79 个属性节点 **79/79 直接命中**；
        原先写的 "pid-10000000" 回退救场次数为 0，属于纯死代码。

        而且回退有误命中风险：tree.json 的 100 个 8 位键里，有 5 个
        （11004201/11004203/11004205/11004207/11004210）减 1e7 后指向的是**另一个不同属性**的节点
        （效果命中+4% -> 攻击+4%）。当前样本靠"直接查一定命中"才没出事，
        一旦出现"直接查不到、减 1e7 恰好命中"的节点，行迹就会静默算错。
        所以这里只直接查，不保留任何回退。
        """
        try:
            node = self.tree.get(str(int(pid)))
        except Exception:
            return None
        return node if isinstance(node, dict) else None

    # ---------- 角色 ----------

    def parse_avatar(self, a: dict) -> dict:
        aid = str(a.get("avatarId") or "")
        meta = self.avatar_meta(aid)
        level = int(a.get("level") or 0)
        promotion = int(a.get("promotion") or 0)
        eidolon = int(a.get("rank") or 0)
        rarity = int(meta.get("Rarity") or 4)
        element = str(meta.get("Element") or "")
        path = str(meta.get("AvatarBaseType") or "")

        # 角色名：hsr.json 按 AvatarName.Hash 查；查不到就兜底"角色{id}"
        name_hash = (meta.get("AvatarName") or {}).get("Hash")
        name = self.name_of_hash(name_hash, f"角色{aid}")

        # --- 基础白值 ---
        base = self.base_stats(meta, level, promotion)

        # --- 光锥 ---
        eq = a.get("equipment") or {}
        weapon = None
        w_hp = w_atk = w_df = 0.0
        if eq:
            wtid = eq.get("tid")
            wmeta = self.weapon_meta(wtid)
            w_name_hash = (eq.get("_flat") or {}).get("name")
            w_name = self.name_of_hash(
                w_name_hash,
                self.name_of_hash(
                    (wmeta.get("EquipmentName") or {}).get("Hash"), f"光锥#{wtid}"
                ),
            )
            w_level = int(eq.get("level") or 0)
            w_promo = int(eq.get("promotion") or 0)
            w_rank = max(1, min(_MAX_RANK, int(eq.get("rank") or 1)))
            wb = self.weapon_base(wmeta, w_level, w_promo)
            w_hp, w_atk, w_df = wb["hp"], wb["atk"], wb["df"]
            # 光锥 _flat.props 也是白值，优先用它（enka 已算好）
            for p in (eq.get("_flat") or {}).get("props") or []:
                pt = str((p or {}).get("type") or "")
                try:
                    pv = float((p or {}).get("value") or 0)
                except Exception:
                    pv = 0.0
                if pt == "BaseHP":
                    w_hp = pv or w_hp
                elif pt == "BaseAttack":
                    w_atk = pv or w_atk
                elif pt == "BaseDefence":
                    w_df = pv or w_df
            weapon = {
                "tid": wtid,
                "name": w_name,
                "level": w_level,
                "rank": w_rank,
                "rarity": int(wmeta.get("Rarity") or 0),
                "hp": w_hp,
                "atk": w_atk,
                "def": w_df,
            }

        # --- 遗器 ---
        relics = []
        set_count: dict = {}
        for r in a.get("relicList") or []:
            try:
                rel = self.parse_relic(r)
            except Exception:
                continue
            relics.append(rel)
            sid = rel.get("set_id") or 0
            set_count[sid] = set_count.get(sid, 0) + 1
        relics.sort(key=lambda x: x["slot_type"])

        # --- 属性累加 ---
        fixed: dict = {}
        ratio: dict = {}

        def _acc(d: dict, ptype: str, val: float) -> None:
            d[ptype] = d.get(ptype, 0.0) + val

        for rel in relics:
            for p in rel.get("_props") or []:
                pt = str((p or {}).get("type") or "")
                if not pt:
                    continue
                try:
                    pv = float((p or {}).get("value") or 0)
                except Exception:
                    pv = 0.0
                if pt in ("BaseHP", "BaseAttack", "BaseDefence", "BaseSpeed"):
                    continue
                if _is_fixed_prop(pt):
                    _acc(fixed, pt, pv)
                else:
                    _acc(ratio, pt, pv)

        # 行迹属性加成（用 _tree_node 兼容 7 位 / 8 位节点号）
        traces = self.parse_traces(a)
        if self.tree:
            for s in a.get("skillTreeList") or []:
                try:
                    pid = int(s.get("pointId") or 0)
                    lv = int(s.get("level") or 0)
                except Exception:
                    continue
                if lv < 1:
                    continue
                node = self._tree_node(pid)
                if not isinstance(node, dict):
                    continue
                for ptype, val in (((node.get("1") or {}).get("props")) or {}).items():
                    try:
                        pv = float(val or 0)
                    except Exception:
                        continue
                    if _is_fixed_prop(ptype):
                        _acc(fixed, ptype, pv)
                    else:
                        _acc(ratio, ptype, pv)

        # 套装 2 件属性效果（4 件多为条件效果，不并入面板，只在套装区展示）
        for sid, cnt in set_count.items():
            if cnt < 2:
                continue
            p2 = ((((self.relics.get("Sets") or {}).get(str(sid)) or {}).get("SetSkills") or {}).get("2") or {}).get("props", {})
            for ptype, val in p2.items():
                try:
                    pv = float(val or 0)
                except Exception:
                    continue
                if _is_fixed_prop(ptype):
                    _acc(fixed, ptype, pv)
                else:
                    _acc(ratio, ptype, pv)

        base_hp = base["hp"] + w_hp
        base_atk = base["atk"] + w_atk
        base_df = base["def"] + w_df

        stats = {
            "hp": base_hp * (1.0 + ratio.get("HPAddedRatio", 0.0)) + fixed.get("HPDelta", 0.0),
            "atk": base_atk * (1.0 + ratio.get("AttackAddedRatio", 0.0)) + fixed.get("AttackDelta", 0.0),
            "def": base_df * (1.0 + ratio.get("DefenceAddedRatio", 0.0)) + fixed.get("DefenceDelta", 0.0),
            "speed": base["speed"] * (1.0 + ratio.get("SpeedAddedRatio", 0.0)) + fixed.get("SpeedDelta", 0.0),
            "crit_rate": base["crit"] + ratio.get("CriticalChance", 0.0) + ratio.get("CriticalChanceBase", 0.0),
            "crit_dmg": base["cdmg"] + ratio.get("CriticalDamage", 0.0) + ratio.get("CriticalDamageBase", 0.0),
            "break_effect": ratio.get("BreakDamageAddedRatio", 0.0) + ratio.get("BreakDamageAddedRatioBase", 0.0),
            "effect_hit": ratio.get("StatusProbability", 0.0) + ratio.get("StatusProbabilityBase", 0.0),
            "effect_res": ratio.get("StatusResistance", 0.0) + ratio.get("StatusResistanceBase", 0.0),
            "energy_regen": 1.0 + ratio.get("SPRatio", 0.0) + ratio.get("SPRatioBase", 0.0),
        }
        # 白值单独留存，详情页展示"白值/面板"对比
        stats["base_hp"] = base_hp
        stats["base_atk"] = base_atk
        stats["base_def"] = base_df

        # --- 套装汇总 ---
        sets = []
        for sid, cnt in sorted(set_count.items(), key=lambda kv: (-kv[1], kv[0])):
            eff = self.set_effects(sid)
            sets.append(
                {
                    "set_id": sid,
                    "name": self.set_name(sid),
                    "count": cnt,
                    "eff2": eff.get("2") or [],
                    "eff4": eff.get("4") or [],
                }
            )

        # --- 技能 ---
        # ⚠ 注意：这里需要「号段对齐」，而上面的行迹 _tree_node() 恰恰**不需要**，
        #   两条路径规则不同，不要合并、也不要互相抄。
        #
        # 为什么技能需要对:  avatars.json 的 SkillTree["0"]["AvatarSkills"] 一律是 7 位
        #   （如希儿 [1102001,1102002,1102003,1102004,1102007]），
        #   但部分角色的 skillTreeList 里同名技能节点是 8 位（11102001 等）。
        #   实测：希儿 1102 / 流萤 1310 / 阮·梅 1306 这三个角色，
        #   不做对齐时技能等级直接查成 [0,0,0,0,0]（全 0 的假数据）；
        #   做对齐后分别是 [5,10,10,10,1] / [6,10,10,9,1] / [3,10,10,9,1]，与游戏内一致。
        #   另外 5 个角色（1407/1415/1512/1222/1308）两种写法都是 7 位，对齐不产生影响。
        #
        # 为什么这样对齐是安全的:
        #   回退结果只用于「这是哪个技能」的查表匹配，不参与任何数值计算；
        #   且先写入真实 pointId、回退用 setdefault，实测回退键与真实 pointId
        #   冲突数为 0，不存在覆盖。
        #   对比行迹：那边的回退会拿去 tree.json 查属性数值，而 tree 里存在
        #   「另一个属性」的同名键（5 个），会产生静默错值 —— 所以行迹只直查。
        #   一句话：行迹看 tree（只直查），技能看 AvatarSkills（需要号段对齐）。
        skills = []
        lv_map = {}
        for s in a.get("skillTreeList") or []:
            try:
                pid = int(s.get("pointId") or 0)
                lv = int(s.get("level") or 0)
            except Exception:
                continue
            lv_map[pid] = lv
            # 同时登记 7 位形式，便于按 AvatarSkills（7 位）查；setdefault 保证不覆盖真实键
            if pid >= 10000000:
                lv_map.setdefault(pid - 10000000, lv)

        avatar_skills = (((meta.get("SkillTree") or {}).get("0") or {}).get("AvatarSkills")) or []
        for pid in avatar_skills:
            try:
                tail = int(pid) % 1000
            except Exception:
                continue
            label = _SKILL_TAIL_CN.get(tail)
            if not label:
                continue
            skills.append({"name": label, "level": int(lv_map.get(int(pid), 0) or 0)})

        if not skills:  # 兜底：从 skillTreeList 里按末位解析
            for pid in sorted(lv_map):
                tail = pid % 1000
                label = _SKILL_TAIL_CN.get(tail)
                if label:
                    skills.append({"name": label, "level": lv_map[pid]})

        return {
            "id": aid,
            "name": name,
            "element": element,
            "element_cn": _ELEM_CN.get(element, element),
            "element_color": _elem_color(element),
            "path": path,
            "path_cn": _PATH_CN.get(path, path),
            "rarity": rarity,
            "level": level,
            "promotion": promotion,
            "eidolon": eidolon,
            "stats": stats,
            "weapon": weapon,
            "relics": relics,
            "sets": sets,
            "traces": traces,
            "skills": skills,
            "relic_count": len(relics),
        }


# ---------- 元素/命途中文表：优先用 common 的 ----------

_ELEM_CN = _bind("HSR_ELEM_CN", _FB_ELEM_CN)
_PATH_CN = _bind("HSR_PATH", _FB_PATH_CN)
_HSR_ELEM_TABLE = _bind("HSR_ELEM", _FB_ELEM_COLOR)


def _elem_color(elem: str):
    """元素配色：优先 common.elem_color，其次 common.HSR_ELEM，最后本地表"""
    if _C is not None:
        fn = getattr(_C, "elem_color", None)
        if callable(fn):
            try:
                c = fn("hsr", elem, FG_DIM)
                if isinstance(c, tuple) and len(c) == 3:
                    return c
            except Exception:
                pass
    if isinstance(_HSR_ELEM_TABLE, dict):
        c = _HSR_ELEM_TABLE.get(elem)
        if isinstance(c, tuple) and len(c) == 3:
            return c
    return _FB_ELEM_COLOR.get(elem, FG_DIM)


def parse_player(raw: dict, store=None) -> dict:
    """整份 enka 响应 -> 渲染用 player 结构

    参数：
        raw   : enka /api/hsr/uid/<uid> 的原始 JSON
        store : common.LocalStore（同步 get 已加载的 store 表），可为 None
    """
    info = (raw or {}).get("detailInfo") or {}
    ctx = _Ctx(store)

    avatars = []
    for a in info.get("avatarDetailList") or []:
        try:
            avatars.append(ctx.parse_avatar(a))
        except Exception:
            continue

    # 排序：等级 -> 稀有度 -> 名字
    avatars.sort(
        key=lambda x: (-int(x.get("level") or 0), -int(x.get("rarity") or 0), x.get("name") or "")
    )

    record = info.get("recordInfo") or {}
    return {
        "game": "hsr",
        "uid": str(info.get("uid") or (raw or {}).get("uid") or ""),
        "nickname": str(info.get("nickname") or "开拓者"),
        "level": int(info.get("level") or 0),
        "world_level": int(info.get("worldLevel") or 0),
        "signature": str(info.get("signature") or ""),
        "avatar_count": len(avatars),
        "record": {
            "achievement": int(record.get("achievementCount") or 0),
            "avatar": int(record.get("avatarCount") or 0),
            "equipment": int(record.get("equipmentCount") or 0),
            "relic": int(record.get("relicCount") or 0),
            "music": int(record.get("musicCount") or 0),
            "rogue": int(record.get("maxRogueChallengeScore") or 0),
        },
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

    header_h = 172
    card_h = 96
    gap = 12
    footer_h = 110
    extra_h = 44 if has_more else 0

    height = header_h + len(show) * (card_h + gap) + extra_h + footer_h
    height = max(height, 320)

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # ---------- 玩家头部 ----------
    d.rectangle((0, 0, width, header_h), fill=BG_CARD)

    nick = player.get("nickname") or "开拓者"
    d.text((28, 22), nick, font=_font(38, True), fill=FG)

    nw = _text_w(d, nick, _font(38, True))
    lv_txt = f"开拓等级 {player.get('level', 0)}"
    d.text((28 + nw + 16, 32), lv_txt, font=_font(20), fill=ACCENT)

    sub = f"世界等级 {player.get('world_level', 0)}"
    d.text((28, 68), sub, font=_font(19), fill=FG_DIM)

    sig = player.get("signature") or ""
    if sig:
        sig = _truncate(d, sig, _font(18), width // 2)
        d.text((28, 96), sig, font=_font(18), fill=FG_MUTED)

    # 右侧统计
    rec = player.get("record") or {}
    right = width - 28
    lines = [
        (f"成就 {_fmt_int(rec.get('achievement', 0))}", ACCENT, 18),
        (
            f"角色 {_fmt_int(rec.get('avatar', 0))}  ·  遗器 {_fmt_int(rec.get('relic', 0))}",
            FG_DIM,
            17,
        ),
        (f"混沌回忆 {rec.get('rogue', 0)}", FG_DIM, 17),
        (f"UID {player.get('uid', '')}", FG_MUTED, 15),
    ]
    ly = 22
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

        ecolor = av.get("element_color") or FG_DIM
        # 左侧元素色条
        d.rectangle((x0, y0 + 6, x0 + 5, y0 + card_h - 6), fill=ecolor)

        tx = x0 + 22
        # 名字
        name = av.get("name") or "?"
        d.text((tx, y0 + 12), name, font=_font(27, True), fill=FG)
        nmw = _text_w(d, name, _font(27, True))

        # 等级
        lv_t = f"Lv.{av.get('level', 0)}"
        d.text((tx + nmw + 12, y0 + 18), lv_t, font=_font(18), fill=ACCENT)

        # 星级
        stars = "★" * max(0, min(5, int(av.get("rarity") or 0)))
        if stars:
            sf = _font(14)
            d.text(
                (tx + nmw + 12 + _text_w(d, lv_t, _font(18)) + 10, y0 + 19),
                stars,
                font=sf,
                fill=_STAR5 if (av.get("rarity") or 0) >= 5 else _STAR4,
            )

        # 元素 + 命途
        line2 = f"{av.get('element_cn') or av.get('element') or ''}属性  ·  {av.get('path_cn') or ''}"
        d.text((tx, y0 + 44), line2, font=_font(17), fill=ecolor)

        # 命座 + 遗器件数
        line3 = f"星魂 E{av.get('eidolon', 0)}  ·  遗器 {av.get('relic_count', 0)} 件"
        d.text((tx, y0 + 68), line3, font=_font(15), fill=FG_MUTED)

        # ---------- 关键属性（2 列 x 3 行）----------
        st = av.get("stats") or {}
        ax = x0 + 258
        items = [
            ("生命", _fmt_int(st.get("hp", 0))),
            ("攻击", _fmt_int(st.get("atk", 0))),
            ("防御", _fmt_int(st.get("def", 0))),
            ("暴击率", _fmt_pct(st.get("crit_rate", 0))),
            ("暴击伤害", _fmt_pct(st.get("crit_dmg", 0))),
            ("速度", f"{float(st.get('speed') or 0):.1f}"),
        ]
        for i, (label, val) in enumerate(items):
            col = i % 2
            row = i // 2
            cx = ax + col * 132
            cy = y0 + 12 + row * 27
            d.text((cx, cy), label, font=_font(14), fill=FG_MUTED)
            lw = _text_w(d, label, _font(14))
            vf = _font(18, True)
            d.text((cx + lw + 7, cy - 3), val, font=vf, fill=FG)

        # ---------- 光锥（右侧）----------
        wx = x0 + w - 232
        wp = av.get("weapon")
        d.text((wx, y0 + 12), "光锥", font=_font(13), fill=FG_MUTED)
        if wp:
            wname = _truncate(d, wp.get("name") or "-", _font(18, True), 214)
            d.text((wx, y0 + 28), wname, font=_font(18, True), fill=FG)
            rank = wp.get("rank", 1)
            wlv = wp.get("level", 0)
            d.text((wx, y0 + 52), f"叠影 {rank}  ·  Lv.{wlv}", font=_font(15), fill=FG_DIM)
        else:
            d.text((wx, y0 + 28), "未装备", font=_font(18), fill=FG_MUTED)

        # 套装简写
        sets = av.get("sets") or []
        if sets:
            parts = []
            for s in sets[:2]:
                short = s.get("name") or ""
                if len(short) > 8:
                    short = short[:7] + "…"
                parts.append(f"{short}×{s.get('count', 0)}")
            d.text((wx, y0 + 72), " · ".join(parts), font=_font(14), fill=FG_MUTED)

        # 序号
        idx_t = f"{idx + 1}"
        d.text(
            (x0 + w - 18 - _text_w(d, idx_t, _font(14)), y0 + card_h - 20),
            idx_t,
            font=_font(14),
            fill=FG_MUTED,
        )

        y += card_h + gap

    # ---------- 更多提示 ----------
    if has_more:
        t = f"还有 {len(avs) - max_show} 个角色未显示（列表按等级排序）"
        tw = _text_w(d, t, _font(18))
        d.text(((width - tw) // 2, y + 8), t, font=_font(18), fill=FG_MUTED)
        y += extra_h

    # ---------- 底部提示 ----------
    # 底部三行（固定位置，互不重叠）
    tip = "发「/崩铁面板 角色名」查看单角色详情（含遗器逐件词条）"
    tw = _text_w(d, tip, _font(18))
    d.text(((width - tw) // 2, height - 100), tip, font=_font(18), fill=ACCENT_DEEP)

    src = "数据来源 enka.network  ·  属性含装备/行迹估算，仅供参考"
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
    """小节标题：金色竖条 + 标题文字 + 右侧分隔线"""
    d.rectangle((x, y + 3, x + 4, y + 18), fill=ACCENT)
    d.text((x + 12, y), title, font=_font(20, True), fill=FG)
    tw = _text_w(d, title, _font(20, True))
    d.line((x + 22 + tw, y + 11, x + width - 12, y + 11), fill=LINE)


def _draw_relic_card(d, x0: int, y0: int, w: int, h: int, rel: dict) -> None:
    """单件遗器卡片：部位 / 强化等级 / 套装名 / 主词条 / 4 条副词条 / 评分"""
    _rounded(d, (x0, y0, x0 + w, y0 + h), 10, BG_CARD)

    # 部位名
    d.text((x0 + 16, y0 + 10), rel.get("slot") or "遗器", font=_font(18, True), fill=FG)

    # 强化等级（右上）
    lv = rel.get("level", 0)
    lv_t = f"+{lv}"
    lvf = _font(19, True)
    d.text((x0 + w - 16 - _text_w(d, lv_t, lvf), y0 + 9), lv_t, font=lvf, fill=ACCENT)

    # 套装名
    sname = _truncate(d, rel.get("set_name") or "", _font(14), w - 32)
    d.text((x0 + 16, y0 + 33), sname, font=_font(14), fill=FG_MUTED)

    # 分隔线
    d.line((x0 + 16, y0 + 52, x0 + w - 16, y0 + 52), fill=LINE)

    # 主词条
    main = rel.get("main") or {}
    d.text((x0 + 16, y0 + 60), "主词条", font=_font(13), fill=FG_MUTED)
    d.text((x0 + 16, y0 + 75), _truncate(d, main.get("name") or "-", _font(17), w - 140), font=_font(17), fill=FG_DIM)

    mv = str(main.get("text") or "-")
    mvf = _font(23, True)
    d.text((x0 + w - 16 - _text_w(d, mv, mvf), y0 + 69), mv, font=mvf, fill=ACCENT)

    # 副词条（2 列 x 2 行）
    subs = rel.get("subs") or []
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

    # 词条评分（右下角）
    sc = f"词条 {rel.get('score', 0):.1f}/9"
    scf = _font(13)
    d.text((x0 + w - 16 - _text_w(d, sc, scf), y0 + h - 20), sc, font=scf, fill=FG_MUTED)


def render_detail(av: dict, player: dict, width: int = 1100) -> bytes:
    """单角色详情大图：属性全展开 + 光锥 + 6 件遗器逐件 + 套装 + 行迹"""
    n = len(av.get("relics") or [])
    relic_rows = (n + 1) // 2 if n else 0
    relic_card_h = 172
    relic_gap = 12
    relic_block = relic_rows * relic_card_h + max(0, relic_rows - 1) * relic_gap

    # 预分配足够高度，画完再裁剪（布局自适应最简单可靠）
    est = 900 + relic_block + 700
    img = Image.new("RGB", (width, max(est, 900)), BG)
    d = ImageDraw.Draw(img)

    pad = 24
    x = pad
    cw = width - pad * 2
    y = 0

    # ================= 头部 =================
    header_h = 136
    d.rectangle((0, 0, width, header_h), fill=BG_CARD)
    ecolor = av.get("element_color") or FG_DIM

    # 左侧元素色块
    d.rectangle((0, 0, 6, header_h), fill=ecolor)

    name = av.get("name") or "?"
    d.text((pad, 20), name, font=_font(41, True), fill=FG)
    nw = _text_w(d, name, _font(41, True))

    # 等级
    lv_t = f"Lv.{av.get('level', 0)}"
    d.text((pad + nw + 14, 32), lv_t, font=_font(22), fill=ACCENT)

    # 星级
    stars = "★" * max(0, min(5, int(av.get("rarity") or 0)))
    if stars:
        d.text(
            (pad + nw + 14 + _text_w(d, lv_t, _font(22)) + 12, 34),
            stars,
            font=_font(17),
            fill=_STAR5 if (av.get("rarity") or 0) >= 5 else _STAR4,
        )

    # 元素 / 命途 / 星魂
    info_line = (
        f"{av.get('element_cn') or av.get('element') or ''}属性  ·  "
        f"{av.get('path_cn') or ''}命途  ·  星魂 E{av.get('eidolon', 0)}"
    )
    d.text((pad, 70), info_line, font=_font(19), fill=ecolor)

    _wp0 = av.get("weapon") or {}
    _wlv = _wp0.get("level")
    sub_line = f"遗器 {av.get('relic_count', 0)} 件  ·  " + (
        f"光锥 Lv.{_wlv}" if _wlv else "光锥未公开"
    )
    d.text((pad, 98), sub_line, font=_font(17), fill=FG_MUTED)

    # 右上：玩家信息
    right = width - pad
    pt = f"{player.get('nickname', '')}  ·  开拓等级 {player.get('level', 0)}"
    ptf = _font(18)
    d.text((right - _text_w(d, pt, ptf), 28), pt, font=ptf, fill=FG_DIM)

    uid_t = f"UID {player.get('uid', '')}"
    uf = _font(15)
    d.text((right - _text_w(d, uid_t, uf), 56), uid_t, font=uf, fill=FG_MUTED)

    y = header_h + 22

    # ================= 属性区 =================
    _section_title(d, pad, y, "面板属性（含装备 / 行迹估算）", cw)
    y += 30

    st = av.get("stats") or {}
    boxes = [
        ("生命值", _fmt_int(st.get("hp", 0))),
        ("攻击力", _fmt_int(st.get("atk", 0))),
        ("防御力", _fmt_int(st.get("def", 0))),
        ("速度", f"{float(st.get('speed') or 0):.1f}"),
        ("暴击率", _fmt_pct(st.get("crit_rate", 0))),
        ("暴击伤害", _fmt_pct(st.get("crit_dmg", 0))),
        ("击破特攻", _fmt_pct(st.get("break_effect", 0))),
        ("效果命中", _fmt_pct(st.get("effect_hit", 0))),
        ("效果抵抗", _fmt_pct(st.get("effect_res", 0))),
        ("能量恢复效率", _fmt_pct(st.get("energy_regen", 0))),
    ]
    cols = 5
    col_w = cw // cols
    box_h = 62
    for i, (label, val) in enumerate(boxes):
        col = i % cols
        row = i // cols
        bx = pad + col * col_w
        by = y + row * (box_h + 8)
        _rounded(d, (bx, by, bx + col_w - 8, by + box_h), 8, BG_CARD)
        d.text((bx + 12, by + 10), label, font=_font(14), fill=FG_MUTED)
        vf = _font(24, True)
        # 数值过长时缩字号
        while _text_w(d, val, vf) > col_w - 28 and vf.size > 12:
            vf = _font(vf.size - 1, True)
        d.text((bx + 12, by + 30), val, font=vf, fill=FG)

    # 白值提示
    bh = st.get("base_hp", 0)
    ba = st.get("base_atk", 0)
    bd = st.get("base_def", 0)
    y += box_h * 2 + 8 + 12
    base_line = (
        f"白值（角色+光锥）：生命 {_fmt_int(bh)} / 攻击 {_fmt_int(ba)} / 防御 {_fmt_int(bd)}"
    )
    d.text((pad, y), base_line, font=_font(14), fill=FG_MUTED)
    y += 22
    # 行迹表（store/hsr/tree.json）缺失时明确提示，不给假数字
    if not (av.get("traces") or []):
        d.text(
            (pad, y),
            "※ 行迹节点加成未计入（缺 store/hsr/tree.json，面板属性为遗器+光锥+套装估算）",
            font=_font(13),
            fill=FG_MUTED,
        )
        y += 20
    y += 4

    # ================= 光锥区 =================
    wp = av.get("weapon")
    _section_title(d, pad, y, "光锥", cw)
    y += 30
    if wp:
        card_h = 92
        _rounded(d, (pad, y, pad + cw, y + card_h), 10, BG_CARD)
        d.text((pad + 16, y + 12), _truncate(d, wp.get("name") or "-", _font(23, True), cw - 32), font=_font(23, True), fill=FG)

        wr = max(0, int(wp.get("rarity") or 0))
        d.text(
            (pad + 16, y + 44),
            f"Lv.{wp.get('level', 0)}  ·  叠影 {wp.get('rank', 1)}/{_MAX_RANK}",
            font=_font(17),
            fill=ACCENT,
        )

        # 基础属性（白值）
        stat_line = (
            f"基础生命 {_fmt_int(wp.get('hp', 0))}   "
            f"基础攻击 {_fmt_int(wp.get('atk', 0))}   "
            f"基础防御 {_fmt_int(wp.get('def', 0))}"
        )
        d.text((pad + 16, y + 66), stat_line, font=_font(15), fill=FG_DIM)

        # 稀有度星
        if wr:
            s = "★" * wr
            sf = _font(15)
            d.text((pad + cw - 16 - _text_w(d, s, sf), y + 14), s, font=sf, fill=_STAR5)
        y += card_h + 18
    else:
        d.text((pad, y), "未公开光锥数据", font=_font(17), fill=FG_MUTED)
        y += 34

    # ================= 遗器区（核心）=================
    relics = av.get("relics") or []
    _section_title(d, pad, y, f"遗器逐件（{len(relics)} 件）", cw)
    y += 30

    # 图例
    legend = "副词条中暴击 / 爆伤 / 速度以金色高亮"
    d.text((pad, y - 4), legend, font=_font(13), fill=FG_MUTED)
    y += 20

    if relics:
        card_w = (cw - 14) // 2
        for i, rel in enumerate(relics):
            col = i % 2
            row = i // 2
            rx = pad + col * (card_w + 14)
            ry = y + row * (relic_card_h + relic_gap)
            _draw_relic_card(d, rx, ry, card_w, relic_card_h, rel)
        y += relic_block + 14
    else:
        d.text((pad, y), "该角色未公开遗器数据", font=_font(17), fill=FG_MUTED)
        y += 34

    # ================= 套装区 =================
    sets = av.get("sets") or []
    if sets:
        _section_title(d, pad, y, "遗器套装效果", cw)
        y += 32
        for s in sets:
            cnt = s.get("count", 0)
            sname = s.get("name") or "-"
            d.text((pad + 4, y), sname, font=_font(18, True), fill=FG)
            sw = _text_w(d, sname, _font(18, True))
            d.text((pad + 4 + sw + 10, y + 2), f"{cnt} 件", font=_font(15), fill=ACCENT)
            y += 24

            # 2 件 / 4 件效果（数值型的直接显示，条件型提示见游戏内）
            for tag, key in (("2件", "eff2"), ("4件", "eff4")):
                rows = s.get(key) or []
                if cnt < int(tag[0]):
                    continue
                if rows:
                    txt = "  ".join(f"{a} {b}" for a, b in rows)
                else:
                    txt = "见游戏内说明（条件 / 触发型效果）"
                line = f"{tag}：{txt}"
                line = _truncate(d, line, _font(15), cw - 20)
                d.text((pad + 20, y), line, font=_font(15), fill=FG_DIM)
                y += 20
            y += 8

    # ================= 行迹区 =================
    traces = av.get("traces") or []
    if traces:
        _section_title(d, pad, y, "行迹加成", cw)
        y += 32
        # 每行 3 个
        per_row = 3
        col_w2 = cw // per_row
        for i, row in enumerate(traces):
            label = row[0]
            val = row[1]
            col = i % per_row
            r = i // per_row
            tx = pad + col * col_w2
            ty = y + r * 26
            d.text((tx + 4, ty), label, font=_font(14), fill=FG_MUTED)
            lw = _text_w(d, label, _font(14))
            d.text((tx + 4 + lw + 6, ty - 2), val, font=_font(17, True), fill=ACCENT)
        rows = (len(traces) + per_row - 1) // per_row
        y += rows * 26 + 14

    # ================= 技能区 =================
    skills = av.get("skills") or []
    if skills:
        _section_title(d, pad, y, "技能等级", cw)
        y += 32
        sx = pad + 4
        for s in skills:
            label = s.get("name") or ""
            lvl = s.get("level", 0)
            d.text((sx, y), label, font=_font(15), fill=FG_MUTED)
            lw = _text_w(d, label, _font(15))
            d.text((sx + lw + 6, y - 3), str(lvl), font=_font(20, True), fill=FG)
            vw = _text_w(d, str(lvl), _font(20, True))
            sx += lw + 6 + vw + 28
        y += 34

    # ================= 底部 =================
    y += 8
    foot = "数据来源 enka.network  ·  属性为本地估算值，仅供参考"
    ff = _font(14)
    fw = _text_w(d, foot, ff)
    if fw > cw:
        foot = _truncate(d, foot, ff, cw)
        fw = _text_w(d, foot, ff)
    d.text(((width - fw) // 2, y), foot, font=ff, fill=FG_MUTED)
    y += 30
    _ftxt = "Github：https://github.com/fyjgtddco/mihoyo-panel"
    _fw = _text_w(d, _ftxt, _font(15))
    d.text(((width - _fw) // 2, y), _ftxt, font=_font(15), fill=ACCENT_DEEP)
    y += 28

    # 裁剪到实际内容高度
    img = img.crop((0, 0, width, min(max(y, 300), img.height)))
    return _save_webp(img, 88)


# ===========================================================================
# 圣遗物（遗器）列表（三游戏统一接口）
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
    """崩铁：把所有角色的遗器摊平，按评分降序返回统一结构。

    实测遗器结构（hsr.py parse_avatar 产出）：
        r["slot"]       部位中文名
        r["level"]      强化等级
        r["main"]       {"type","name","text"}
        r["subs"]       [{"type","name","text","hot"}]
        r["set_name"]   套装名
        r["_props"]     原始 property 数值（可能缺失）
    """
    cm = _get_common()
    if cm is None:
        raise RuntimeError('common 模块不可用，请检查部署')

    out = []
    for av in player.get("avatars") or []:
        owner = av.get("name") or ""
        for r in av.get("relics") or []:
            props = r.get("_props") or {}
            subs, scored, view = [], [], []
            for s in r.get("subs") or []:
                ptype = str(s.get("type") or "")
                text = str(s.get("text") or "")
                # 数值：优先原始 props，否则从显示文本里解析
                val = _hsr_parse_value(ptype, text, props)
                subs.append({
                    "text": f"{s.get('name') or ''} {text}".strip(),
                    "highlight": bool(s.get("hot")),
                })
                scored.append({"prop": ptype, "value": val, "level": 1})

            main = r.get("main") or {}
            main_text = f"{main.get('name') or ''} {main.get('text') or ''}".strip()

            # 崩铁：用社区通用双暴分口径（暴击×2 + 爆伤×1 + 攻击×1.333 + 速度×1.5）
            try:
                score = cm.artifact_score(scored, True, game="hsr")
            except Exception:
                score = 0.0

            out.append({
                "name": main.get("name") or r.get("slot") or "遗器",
                "slot": r.get("slot") or "",
                "set": r.get("set_name") or "",
                "score": score,
                "grade": cm.score_grade(score, game="hsr"),
                "main_name": main.get("name") or "",
                "main_text": main_text,
                "subs": subs,
                "rarity": 5,
                "owner": owner,
                "plus": r.get("level") or 0,
            })

    out.sort(key=lambda x: -float(x.get("score") or 0))
    return out


def _hsr_parse_value(ptype: str, text: str, props) -> float:
    """从崩铁词条的显示文本 / _props 里取出数值。

    _props 实测是 **list**：[{"type": "CriticalDamage", "value": 0.188}, ...]
    （不是 dict），必须遍历找 type 匹配项，否则会 AttributeError。
    """
    # 1) _props 是 dict 的情况
    if isinstance(props, dict):
        raw = props.get(ptype)
        if raw is not None:
            try:
                v = abs(float(raw))
                return v * 100.0 if ("%" in text and v < 1.0) else v
            except Exception:
                pass

    # 2) _props 是 list 的情况（实际就是这种）
    if isinstance(props, (list, tuple)):
        for it in props:
            if not isinstance(it, dict):
                continue
            if str(it.get("type") or "") == ptype:
                try:
                    v = abs(float(it.get("value") or 0))
                    return v * 100.0 if ("%" in text and v < 1.0) else v
                except Exception:
                    break

    # 3) 从显示文本里抠数字（兜底）
    m = re.search(r"([\d.]+)", text or "")
    if not m:
        return 0.0
    try:
        return abs(float(m.group(1)))
    except Exception:
        return 0.0


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
    """崩铁战绩：三大高难副本（混沌回忆 / 虚构叙事 / 末日幻影）。

    数据来源 recordInfo.challengeInfo（enka 有则给，无则显示未公开）
    """
    rec = player.get("record") or {}
    ch = rec.get("challengeInfo") or {}

    # 混沌回忆
    mos = ch.get("mazeGroup") or ch.get("memoryOfChaos") or {}
    mos_star = int(ch.get("mocStar") or ch.get("memoryOfChaosStar") or 0)
    mos_tier = int(ch.get("mocTier") or ch.get("memoryOfChaosTier") or 0)

    # 虚构叙事
    pf_star = int(ch.get("pfStar") or ch.get("pureFictionStar") or 0)

    # 末日幻影
    ap_star = int(ch.get("apStar") or ch.get("apocalypticShadowStar") or 0)

    if mos_tier:
        mos_val = f"第 {mos_tier} 层"
        mos_desc = f"★{mos_star}"
    elif mos_star:
        mos_val = f"★{mos_star}"
        mos_desc = "本期星数"
    else:
        mos_val, mos_desc = "未公开", "游戏内未展示战绩"

    items = [
        ("混沌回忆", mos_val, mos_desc),
        ("虚构叙事", f"★{pf_star}" if pf_star else "未公开", "本期星数"),
        ("末日幻影", f"★{ap_star}" if ap_star else "未公开", "本期星数"),
        ("模拟宇宙", str(rec.get("maxRogueChallengeScore") or 0), "最高挑战评分"),
        ("成就达成", str(rec.get("achievementCount") or 0), "累计成就数"),
    ]

    hl = []
    if mos_star >= 12:
        hl.append(("混沌满星", f"★{mos_star}"))
    if pf_star >= 12:
        hl.append(("虚构满星", f"★{pf_star}"))
    if ap_star >= 12:
        hl.append(("末日满星", f"★{ap_star}"))
    rogue = int(rec.get("maxRogueChallengeScore") or 0)
    if rogue >= 9:
        hl.append(("模拟宇宙", str(rogue)))

    return {
        "game_cn": "崩铁",
        "nickname": player.get("nickname") or "",
        "uid": player.get("uid") or "",
        "signature": player.get("signature") or "",
        "main": ("混沌回忆", mos_val, mos_desc),
        "items": items,
        "highlights": hl,
    }


def collect_collection(player: dict) -> dict:
    """崩铁收集度。"""
    avatars = player.get("avatars") or []
    five = sum(1 for a in avatars if int(a.get("rarity") or 0) >= 5)
    four = sum(1 for a in avatars if int(a.get("rarity") or 0) == 4)
    levels = [int(a.get("level") or 0) for a in avatars if a.get("level")]
    avg = round(sum(levels) / len(levels), 1) if levels else 0
    rec = player.get("record") or {}

    total = five * 10 + four * 5
    return {
        "total": total,
        "five_star": five,
        "six_star": 0,
        "char_count": int(rec.get("avatarCount") or len(avatars)),
        "level_avg": avg,
        "fetter_sum": 0,
    }


def calc_damage(player: dict, av: dict, *, skill: str = "") -> dict:
    """崩铁伤害计算（用参考终结技倍率）。"""
    cm = _get_common()
    stats = av.get("stats") or {}
    if not stats:
        stats = dict(av.get("panel") or {})

    def g(*keys, default=0.0):
        for k in keys:
            v = stats.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    continue
        return default

    atk = g("攻击力", "atk", "Attack")
    crit = g("暴击率", "crit", "CriticalChance")
    critdmg = g("暴击伤害", "critdmg", "CriticalDamage")
    dmg_boost = g("伤害加成", "dmg_boost", "0")

    crit_r = crit / 100.0 if crit > 1.5 else crit
    critd_r = critdmg / 100.0 if critdmg > 3 else critdmg

    char = av.get("name") or ""
    mult, exact = cm.skill_multiplier("hsr", char)
    mult_r = mult / 100.0

    crit_f = 1.0 + min(max(crit_r, 0.0), 1.0) * max(critd_r, 0.0)
    resist = 0.9

    step1 = atk * mult_r
    step2 = step1 * crit_f
    step3 = step2 * (1.0 + dmg_boost / 100.0)
    final = step3 * resist

    tag = "" if exact else "（参考倍率）"
    return {
        "game_cn": "崩铁",
        "char": char,
        "owner": player.get("nickname") or "",
        "uid": player.get("uid") or "",
        "stats": [
            ("攻击力", f"{atk:,.0f}", cm.FG),
            ("暴击率", f"{crit:.1f}%", cm.FG),
            ("暴击伤害", f"{critdmg:.1f}%", cm.FG),
            ("伤害加成", f"{dmg_boost:.1f}%", cm.FG),
            ("暴击期望", f"{crit_f:.3f}", cm.ACCENT_DEEP),
        ],
        "skills": [
            ("采用倍率", f"{mult:.0f}%{tag}"),
            ("抗性系数", f"×{resist}"),
        ],
        "steps": [
            (f"攻击力 × 倍率 ({mult:.0f}%)", f"{step1:,.0f}"),
            (f"× 暴击期望 ({crit_f:.3f})", f"{step2:,.0f}"),
            (f"× (1 + 增伤 {dmg_boost:.1f}%)", f"{step3:,.0f}"),
            (f"× 抗性 {resist}", f"{final:,.0f}"),
        ],
        "final": ("终结技单段期望", f"{final:,.0f}"),
        "note": "※ 未计入击破伤害与易伤，实际数值会有浮动。",
    }


