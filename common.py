"""
共享基础层：三游戏 store 映射表加载 / 本地缓存 / 绘图基元 / 配色 / 格式化。

设计要点
--------
1. 服务器带宽只有约 8Mbps，一个 300KB 的 store 文件要十几秒到几十秒，
   所以 store 缓存 TTL 固定 7 天，且必须支持「离线回退」：网络失败时
   继续用本地旧文件，绝不让用户看到报错。
2. 只依赖 标准库 + aiohttp + Pillow，不下载任何图片/图标。
3. 绘图统一走本模块的基元，保证三个游戏出的图风格一致（深色主题）。

模块结构
--------
  PanelError        给用户看的友好错误
  LocalStore        store 映射表（JSON）下载 + 内存缓存 + 磁盘缓存（同步 get）
  配色常量 / 元素配色表 / 中文名表
  字体与绘图基元：font / text_w / rounded / draw_bar / draw_tag / draw_star ...
  格式化：fmt_int / fmt_pct / fmt_pct_raw
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import time
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# 错误类
# ---------------------------------------------------------------------------


class PanelError(Exception):
    """给用户看的友好错误。

    用法：raise PanelError("UID 格式不正确", hint="国服是 9 位数字，例如 100000001")
    str(e) 会把 hint 拼在下一行，可以直接发给用户。
    """

    def __init__(self, message: str, hint: str = "", *, code: int = 0):
        self.message = str(message or "")
        self.hint = str(hint or "")
        self.code = int(code or 0)
        super().__init__(self.message)

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n{self.hint}"
        return self.message

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"PanelError({self.message!r}, hint={self.hint!r})"


# ---------------------------------------------------------------------------
# store 映射表加载
# ---------------------------------------------------------------------------

STORE_BASE = "https://raw.githubusercontent.com/EnkaNetwork/API-docs/master/store"
STORE_TTL = 7 * 86400  # 7 天：带宽太贵，绝不高频重下

# 规范名 -> 相对 store 根目录的文件名（key 是去掉 .json 的路径）
STORE_FILES: dict[str, str] = {
    # ---- 原神 ----
    "gi/avatars": "gi/avatars.json",
    "gi/locs": "gi/locs.json",
    "gi/weapons": "gi/weapons.json",
    "gi/relics": "gi/relics.json",
    "gi/affixes": "gi/affixes.json",
    # ---- 通用（原神 + 崩铁共用的文本，根目录那份）----
    "loc": "loc.json",
    # ---- 崩铁 ----
    "hsr/avatars": "hsr/avatars.json",
    "hsr/relics": "hsr/relics.json",
    "hsr/affixes": "hsr/affixes.json",
    "hsr/weapons": "hsr/weapons.json",
    "hsr/hsr": "hsr/hsr.json",  # 崩铁中文名全在这里，loc.json 里没有崩铁 hash
    "hsr/tree": "hsr/tree.json",  # 行迹节点表：pointId -> 属性加成（1080 节点）
    # ---- 绝区零 ----
    "zzz/avatars": "zzz/avatars.json",
    "zzz/weapons": "zzz/weapons.json",
    "zzz/equipments": "zzz/equipments.json",
    "zzz/locs": "zzz/locs.json",
    "zzz/property": "zzz/property.json",
}

# 每个游戏预加载哪些文件
GAME_FILES: dict[str, tuple[str, ...]] = {
    "gi": ("gi/avatars", "gi/locs", "gi/weapons", "gi/relics", "gi/affixes", "loc"),
    "hsr": ("hsr/avatars", "hsr/relics", "hsr/affixes", "hsr/weapons", "hsr/hsr", "hsr/tree"),
    "zzz": ("zzz/avatars", "zzz/weapons", "zzz/equipments", "zzz/locs", "zzz/property"),
}

# 宽松别名：兼容旧代码 / 手写简写
STORE_ALIASES: dict[str, str] = {
    # 旧 enka.py 的 Store 用的是裸名（只支持原神）
    "avatars": "gi/avatars",
    "characters": "gi/avatars",
    "gi/characters": "gi/avatars",
    "gi/avatar": "gi/avatars",
    "weapons": "gi/weapons",
    "gi/weapon": "gi/weapons",
    "relics": "gi/relics",
    "gi/relic": "gi/relics",
    "affixes": "gi/affixes",
    "gi/affix": "gi/affixes",
    "locs": "gi/locs",
    "gi/loc": "loc",
    "gi/locs/zh": "gi/locs",
    # 崩铁：中文文本在 hsr/hsr.json
    "hsr/locs": "hsr/hsr",
    "hsr/loc": "hsr/hsr",
    "hsr/json": "hsr/hsr",
    "hsr/avatar": "hsr/avatars",
    "hsr/relic": "hsr/relics",
    "hsr/affix": "hsr/affixes",
    "hsr/weapon": "hsr/weapons",
    "hsr/skilltree": "hsr/tree",
    "hsr/skilltrees": "hsr/tree",
    "hsr/traces": "hsr/tree",
    "hsr/points": "hsr/tree",
    # 绝区零
    "zzz/loc": "zzz/locs",
    "zzz/avatar": "zzz/avatars",
    "zzz/weapon": "zzz/weapons",
    "zzz/equipment": "zzz/equipments",
    "zzz/prop": "zzz/property",
    "zzz/properties": "zzz/property",
    "property": "zzz/property",
    "equipments": "zzz/equipments",
}

# 游戏名归一化
GAME_ALIASES: dict[str, str] = {
    "gi": "gi",
    "genshin": "gi",
    "genshinimpact": "gi",
    "原神": "gi",
    "hsr": "hsr",
    "starrail": "hsr",
    "honkaistarrail": "hsr",
    "崩铁": "hsr",
    "星穹铁道": "hsr",
    "崩坏星穹铁道": "hsr",
    "zzz": "zzz",
    "zenlesszonezero": "zzz",
    "绝区零": "zzz",
}

STORE_HEADERS = {
    "User-Agent": "AstrBot-MiHoYoPanel/2.0 (+https://enka.network)",
    "Accept-Encoding": "gzip, deflate",
}


def normalize_game(game: str) -> str:
    """把 '原神' / 'hsr' / 'StarRail' 之类都归一化成 'gi'|'hsr'|'zzz'。"""
    s = str(game or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if s in GAME_ALIASES:
        return GAME_ALIASES[s]
    if s.startswith("gi"):
        return "gi"
    if s.startswith("hsr"):
        return "hsr"
    if s.startswith("zzz"):
        return "zzz"
    return s


def normalize_store_name(name: str) -> str:
    """把任意写法的 store 文件名归一化成规范 key（如 'hsr/avatars'）。

    支持：'hsr/avatars'、'hsr/avatars.json'、'hsr_avatars'、'hsr:avatars'、
          'HSR/Avatars'、以及裸名 'avatars'（自动当成原神）。
    """
    if not name:
        return ""
    s = str(name).strip()
    if s.startswith("http://") or s.startswith("https://"):
        # 直接给 URL 也认：取最后两段路径
        s = s.split("/store/")[-1]
    s = s.lower().replace("\\", "/").replace(":", "/").replace("_", "/")
    if s.endswith(".json"):
        s = s[:-5]
    s = re.sub(r"/{2,}", "/", s).strip("/")
    if s in STORE_FILES:
        return s
    if s in STORE_ALIASES:
        return STORE_ALIASES[s]
    # 后缀匹配：'zzz/property' 之类写全了更好，裸 'avatars' 已在别名里
    for key in STORE_FILES:
        if key.endswith("/" + s):
            return key
    return s


def store_url(key: str) -> str:
    """规范 key -> 下载地址。"""
    key = normalize_store_name(key)
    fname = STORE_FILES.get(key, key + ".json")
    return f"{STORE_BASE}/{fname}"


class LocalStore:
    """store 映射表（JSON 字典）的本地缓存。

    三级读取顺序（get() 是**同步**的）：
        内存缓存 -> 磁盘缓存（未过期优先，过期也读）-> 默认值 {}

    ensure() 是异步的，负责真正下载：TTL 7 天，失败自动离线回退旧文件。

    典型用法：
        store = LocalStore(cache_dir)
        await store.ensure(session, "hsr")        # 预加载 5 个文件
        avatars = store.get("hsr/avatars")        # 同步取
        zh = store.locs("hsr")                    # 同步取中文名表
    """

    def __init__(
        self,
        cache_dir: str,
        *,
        ttl: int = STORE_TTL,
        timeout: float = 300.0,
        retries: int = 3,
        concurrency: int = 3,
    ):
        self.dir = str(cache_dir or "")
        if self.dir:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except Exception:
                pass
        self.ttl = int(ttl)
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.concurrency = max(1, int(concurrency))
        self._mem: dict[str, Any] = {}
        self._sem: asyncio.Semaphore | None = None
        self._fail_until: dict[str, float] = {}
        self._loaded_games: set[str] = set()
        # 调试信息：最近一次下载失败的 key -> 原因
        self.last_errors: dict[str, str] = {}

    # ---------- 路径与磁盘 ----------

    def path_of(self, name: str) -> str:
        key = normalize_store_name(name)
        safe = key.replace("/", "_").replace("\\", "_") or "unknown"
        return os.path.join(self.dir or ".", f"store_{safe}.json")

    def _fresh(self, key: str) -> bool:
        p = self.path_of(key)
        try:
            return os.path.exists(p) and (time.time() - os.path.getmtime(p) < self.ttl)
        except Exception:
            return False

    def _read_disk(self, key: str) -> Any:
        p = self.path_of(key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data
        except Exception:
            return None

    def _write_disk(self, key: str, data: Any) -> None:
        if not self.dir:
            return
        p = self.path_of(key)
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, p)  # 原子替换，避免写一半的文件
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    # ---------- 同步读取 ----------

    def get(self, name: str, default: Any = None) -> Any:
        """同步取 store 数据：内存 -> 磁盘 -> default。

        不联网、不 await，可以在同步的 parse_player / render_* 里放心调用。
        找不到时返回 default（默认给空 dict，方便下游 .get()）。
        """
        key = normalize_store_name(name)
        if not key:
            return {} if default is None else default
        if key in self._mem:
            return self._mem[key]
        data = self._read_disk(key)
        if data is not None:
            self._mem[key] = data
            return data
        return {} if default is None else default

    # get_cached 就是 get 的同义写法（强调「只读缓存、不联网」）
    def get_cached(self, name: str, default: Any = None) -> Any:
        return self.get(name, default)

    def has(self, name: str) -> bool:
        key = normalize_store_name(name)
        if key in self._mem:
            return True
        return os.path.exists(self.path_of(key))

    def age(self, name: str) -> float:
        """磁盘缓存已存在多少秒；不存在返回 -1。"""
        p = self.path_of(name)
        try:
            return time.time() - os.path.getmtime(p)
        except Exception:
            return -1.0

    # ---------- 异步下载 ----------

    def _get_sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        return self._sem

    async def _fetch_one(self, session, key: str) -> bool:
        """下载单个文件并写缓存；成功返回 True。"""
        if key in self._mem:
            return True

        # 1) 磁盘缓存还新鲜：直接用，不浪费带宽
        if self._fresh(key):
            data = self._read_disk(key)
            if data is not None:
                self._mem[key] = data
                return True

        # 2) 刚失败过：短时间内不再重试网络，直接用旧文件（离线回退）
        until = self._fail_until.get(key, 0.0)
        if until > time.time():
            data = self._read_disk(key)
            if data is not None:
                self._mem[key] = data
                return True
            return False

        # 3) 真正下载
        url = store_url(key)
        data = None
        err = ""
        async with self._get_sem():
            for attempt in range(self.retries):
                try:
                    timeout = _client_timeout(self.timeout)
                    async with session.get(url, headers=STORE_HEADERS, timeout=timeout) as resp:
                        status = getattr(resp, "status", 0)
                        if status == 200:
                            data = await resp.json(content_type=None)
                            break
                        err = f"HTTP {status}"
                        if status in (400, 401, 403, 404):
                            break  # 这些重试没意义
                except Exception as e:  # 网络错误 / 超时 / JSON 解析失败
                    err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(1.5 * (attempt + 1))

        if isinstance(data, dict) and data:
            self._mem[key] = data
            self._write_disk(key, data)
            self.last_errors.pop(key, None)
            return True
        if isinstance(data, list) and data:
            self._mem[key] = data
            self._write_disk(key, data)
            self.last_errors.pop(key, None)
            return True

        # 4) 下载失败 -> 离线回退：读旧文件（哪怕已过期），并记一笔
        self.last_errors[key] = err or "empty response"
        self._fail_until[key] = time.time() + 300
        data = self._read_disk(key)
        if data is not None:
            self._mem[key] = data
            return True
        return False

    async def ensure(self, session, game: str | Iterable[str] | None = None) -> dict[str, bool]:
        """预加载（异步、限流、失败自动回退）。

        参数
        ----
        session : aiohttp.ClientSession
        game    : None 表示全部 16 个文件；
                  "gi"/"hsr"/"zzz" 表示该游戏那一组；
                  也可以是某个具体文件名（如 "zzz/property"）；
                  还可以是列表。
        返回 {规范 key: 是否可用}
        """
        if game is None:
            keys: list[str] = list(STORE_FILES)
        elif isinstance(game, (list, tuple, set, frozenset)):
            keys = [normalize_store_name(g) for g in game]
        else:
            g = normalize_game(str(game))
            if g in GAME_FILES:
                keys = list(GAME_FILES[g])
                self._loaded_games.add(g)
            else:
                key = normalize_store_name(str(game))
                keys = [key] if key else []
        keys = [k for k in keys if k]
        if not keys:
            return {}

        results = await asyncio.gather(
            *(self._fetch_one(session, k) for k in keys), return_exceptions=True
        )
        out: dict[str, bool] = {}
        for k, r in zip(keys, results):
            out[k] = bool(r) and not isinstance(r, Exception)
        return out

    async def ensure_missing(self, session, game: str | None = None) -> dict[str, bool]:
        """只补下载还没进内存的那部分（已加载过就秒回）。"""
        if game is None:
            keys = [k for k in STORE_FILES if k not in self._mem]
        else:
            g = normalize_game(str(game))
            names = GAME_FILES.get(g)
            keys = [k for k in names if k not in self._mem] if names else [normalize_store_name(str(game))]
        if not keys:
            return {}
        return await self.ensure(session, keys)

    # ---------- 中文名 ----------

    def locs(self, game: str) -> dict[str, str]:
        """同步取某游戏的「key -> 中文名」字典。

        gi  : loc.json["zh-cn"] 合并 gi/locs.json["zh-cn"]（单用哪一份都会缺，必须合并）
        hsr : hsr/hsr.json["zh-cn"]（崩铁名全在这，store/loc.json 里查不到）
        zzz : zzz/locs.json["zh-cn"]
        """
        g = normalize_game(str(game))
        out: dict[str, str] = {}
        if g == "gi":
            return self.loc_gi()
        elif g == "hsr":
            out = self.loc_hsr()
        elif g == "zzz":
            out = self.loc_zzz()
        return out

    def loc_text(self, game: str, key: Any, default: str = "") -> str:
        """同步按 key 取中文名；gi/hsr 传 hash（可为 int），zzz 传名字字符串。"""
        if key is None:
            return default
        table = self.locs(game)
        val = table.get(str(key))
        return val if isinstance(val, str) and val else default

    # name_of = loc_text 的别名，读起来更顺
    def name_of(self, game: str, key: Any, default: str = "") -> str:
        return self.loc_text(game, key, default)

    # ---- 三个游戏各自的显式便捷方法（同步，不联网）----

    def loc_gi(self) -> dict[str, str]:
        """原神中文名表：loc.json["zh-cn"] 合并 gi/locs.json["zh-cn"]。

        （原神武器 / 圣遗物套装名在 store/loc.json 里，部分角色名只在
         store/gi/locs.json 里，所以两份都要合并，合并后约 960 条。）
        """
        out: dict[str, str] = {}
        for key in ("loc", "gi/locs"):
            zh = _zh_section(self.get(key))
            if zh:
                out.update(zh)
        return out

    def loc_hsr(self) -> dict[str, str]:
        """崩铁中文名表：hsr/hsr.json["zh-cn"]（扁平 hash->名字）。

        注意：**不是** store/loc.json —— 那份只有原神内容。
        角色名、光锥名、遗器套装名、属性词条名（如 "MaxHP"）都在这一个字典里。
        """
        return dict(_zh_section(self.get("hsr/hsr")))

    def loc_zzz(self) -> dict[str, str]:
        """绝区零中文名表：zzz/locs.json["zh-cn"]（键是名字字符串，不是 hash）。"""
        return dict(_zh_section(self.get("zzz/locs")))

    def tr_gi(self, hash_or_none: Any, default: str = "") -> str:
        """原神：loc hash -> 中文名。"""
        if hash_or_none is None:
            return default
        val = self.loc_gi().get(str(hash_or_none))
        return val if isinstance(val, str) and val else default

    def tr_hsr(self, hash_or_none: Any, default: str = "") -> str:
        """崩铁：loc hash -> 中文名（也支持直接查属性名，如 "MaxHP"）。"""
        if hash_or_none is None:
            return default
        val = self.loc_hsr().get(str(hash_or_none))
        return val if isinstance(val, str) and val else default

    def tr_zzz(self, name_key_or_none: Any, default: str = "") -> str:
        """绝区零：名字字符串（如 Avatar_Female_Size02_Anbi）-> 中文名。

        ⚠ 必须走 locs 查表，**禁止从 Name 的字面英文推断中文**。
        这个坑很隐蔽：avatars["1341"].Name == "Avatar_Female_Size00_XiaoZhao"，
        但 locs["zh-cn"]["Avatar_Female_Size00_XiaoZhao"] == "照" ——
        Name 里的 XiaoZhao 是内部代号，跟中文名「照」毫无语义关系，
        任何人按字面猜都会猜成「赵小昭」。原神/崩铁的 hash 查表同理，
        一律以 locs 字典为准，不做任何字面推断。
        """
        if name_key_or_none is None:
            return default
        val = self.loc_zzz().get(str(name_key_or_none))
        return val if isinstance(val, str) and val else default

    def tr(self, game: str, key: Any, default: str = "") -> str:
        """统一入口：按游戏查中文名（gi/hsr 传 hash，zzz 传名字字符串）。"""
        g = normalize_game(str(game))
        if g == "gi":
            return self.tr_gi(key, default)
        if g == "hsr":
            return self.tr_hsr(key, default)
        if g == "zzz":
            return self.tr_zzz(key, default)
        return default

    # ---- 崩铁行迹节点表（hsr/tree.json）----

    def hsr_tree(self) -> dict:
        """崩铁行迹节点表：{pointId: {"1": {"props": {属性名: 数值}}}}。

        tree.json 只收录**带 props 的行迹属性节点**（1080 个，全部带 props），
        技能等级节点（普攻/战技/终结技等）不在里面 —— 所以「能在 tree 里
        查到」本身就等价于「这是一个属性行迹节点」，可以直接当过滤条件用。
        key 以 7 位为主（980 个），另有 100 个 8 位键，两种都直接查。
        """
        data = self.get("hsr/tree")
        return data if isinstance(data, dict) else {}

    def hsr_tree_node(self, point_id: Any) -> dict:
        """按 pointId 取行迹属性节点（返回 {"1": {"props": {...}}} 或 {}）。

        查不到返回 {} —— 这通常说明该 pointId 是**技能节点**而非属性行迹节点，
        属于正常情况，不是错误（实测样本 8 个角色：属性节点 79/79 全部直接命中，
        其余 70 个 key 是技能节点）。

        兼容性：tree.json 里 100 个 8 位键与对应 7 位键**内容不完全等价**
        （实测 100 对里 95 对相同、5 对是不同节点的属性，例如 11004201 是
        「效果命中+4%」而 1004201 是「攻击+4%」）。因此这里**只做直接精确查**，
        绝不做 -10000000 之类的模糊回退，避免把别的节点的属性算到该角色头上。
        """
        tree = self.hsr_tree()
        if not tree or point_id is None:
            return {}
        key = str(point_id).strip()
        if not key:
            return {}
        hit = tree.get(key)
        return hit if isinstance(hit, dict) else {}

    def hsr_trace_props(self, point_id: Any, level: int = 1) -> dict:
        """取某行迹节点在指定等级下的属性加成 {属性名: 数值}。

        tree.json 结构是 {节点号: {"1": {"props": {...}}, "2": {...}}}，
        第一个键通常是等级档位（"1"），找不到指定等级就退回第一档。
        """
        node = self.hsr_tree_node(point_id)
        if not node:
            return {}
        lv = str(level)
        stage = node.get(lv)
        if not isinstance(stage, dict):
            # 退回第一档（同时也兼容没有等级分档的写法）
            for k in node:
                if isinstance(node[k], dict):
                    stage = node[k]
                    break
        if not isinstance(stage, dict):
            return {}
        props = stage.get("props")
        return props if isinstance(props, dict) else {}

    def hsr_traces(self, skill_tree_list: Any) -> tuple[list[dict], list[dict]]:
        """把 skillTreeList 拆成 (属性行迹列表, 技能节点列表)。

        返回的每项：{"id": str, "level": int, "props": {属性名: 数值}}
          • 属性行迹 = 能在 tree.json 里查到、且带 props 的节点
          • 技能节点 = 其余节点（普攻/战技/终结技/天赋等，没有面板属性加成）

        用法（hsr.py 详情页）：
            traces, skills = store.hsr_traces(av["skillTreeList"])
            for t in traces:  # 只画有属性的行迹
                ...
        """
        traces: list[dict] = []
        skills: list[dict] = []
        if not isinstance(skill_tree_list, (list, tuple)):
            return traces, skills
        for item in skill_tree_list:
            if not isinstance(item, dict):
                continue
            pid = item.get("pointId")
            try:
                lvl = int(item.get("level") or 0)
            except Exception:
                lvl = 0
            props = self.hsr_trace_props(pid, level=1)
            rec = {"id": str(pid), "level": lvl, "props": props}
            (traces if props else skills).append(rec)
        return traces, skills

    def hsr_trace_total(self, skill_tree_list: Any) -> dict:
        """汇总某角色所有属性行迹的加成（同类属性数值相加）。

        返回 {属性名: 合计数值}，例如 {"CriticalChanceBase": 0.0539, "SpeedDelta": 4}。
        注意：这是 tree 表里节点 1 级档位的值；崩铁行迹节点的属性值
        不随等级变化（等级只影响技能倍率），所以直接按节点累加即可。
        """
        traces, _ = self.hsr_traces(skill_tree_list)
        total: dict[str, float] = {}
        for t in traces:
            for k, v in (t.get("props") or {}).items():
                try:
                    total[k] = total.get(k, 0.0) + float(v)
                except Exception:
                    continue
        return total

    def __repr__(self) -> str:  # pragma: no cover
        return f"LocalStore(dir={self.dir!r}, cached={len(self._mem)})"


def _zh_section(data: Any) -> dict:
    """从 {"zh-cn": {...}, "en": {...}} 里取 zh-cn 字典（兼容多种写法）。"""
    if not isinstance(data, dict):
        return {}
    for key in ("zh-cn", "zh-CN", "zh_cn", "chs", "zh"):
        sec = data.get(key)
        if isinstance(sec, dict):
            return sec
    # 兼容「已经是 hash -> 名字」的平表
    vals = list(data.values())[:5]
    if vals and all(isinstance(v, str) for v in vals):
        return data
    return {}


def _client_timeout(total: float):
    """构造 aiohttp 超时；aiohttp 缺失时返回 None（调用方本就不该走到这里）。"""
    try:
        import aiohttp

        return aiohttp.ClientTimeout(total=total, connect=30, sock_read=max(60.0, total / 2))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 配色：米白/暖白 + 金色点缀（v3 白底主题）
#
# 设计目标：解决"字太淡看不清"的反馈。
# 关键原则：
#   · 背景用暖白（米白 #FAF8F4），不用纯白 #FFFFFF——纯白刺眼且和卡片无层次
#   · 正文用近黑（#1F2328），对比度 > 12:1，远超 WCAG AA 的 4.5:1
#   · 金色的 ACCENT 在白底上偏浅，所以另设 ACCENT_DEEP 用于文字，
#     ACCENT 只用于色块/描边，这是白底主题最容易踩的坑
# ---------------------------------------------------------------------------

# 背景层
BG = (250, 248, 244)          # 米白：页面底色
BG_CARD = (255, 255, 255)     # 纯白：卡片
BG_CARD2 = (245, 241, 234)    # 浅米：次级卡片 / 表头
BG_SOFT = (238, 233, 224)     # 更深的米色：分隔块

# 文字层（白底上必须够黑，这是"字太淡"问题的根治）
FG = (31, 35, 40)             # 近黑：正文
FG_DIM = (85, 92, 102)        # 深灰：次要信息
FG_MUTED = (138, 145, 155)    # 中灰：弱化说明

# 点缀色
ACCENT = (212, 160, 56)       # 金：色块 / 描边 / 粗线
ACCENT_DEEP = (158, 108, 18)  # 深金：白底上的金色**文字**用这个，否则看不清
ACCENT_SOFT = (250, 242, 224) # 浅金：标签底色

LINE = (226, 220, 210)        # 分隔线：暖灰
LINE_STRONG = (198, 190, 178) # 强调分隔线

GOOD = (22, 138, 74)          # 绿：正向（白底要压深）
BAD = (196, 48, 48)           # 红：负向
WARN = (196, 122, 18)         # 橙：警告

# 稀有度星星颜色
STAR_COLOR = {
    5: (255, 190, 90),
    4: (170, 130, 220),
    3: (110, 170, 235),
    2: (130, 190, 130),
    1: (150, 160, 178),
}


def star_color(rarity: Any) -> tuple[int, int, int]:
    try:
        return STAR_COLOR.get(int(rarity), ACCENT)
    except Exception:
        return ACCENT


def hex_to_rgb(value: Any, default: tuple[int, int, int] = FG_DIM) -> tuple[int, int, int]:
    """'#56c3c1' / '#fff' / (r,g,b) -> RGB 元组。"""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(int(v) for v in value[:3])  # type: ignore[return-value]
        except Exception:
            return default
    s = str(value or "").strip().lstrip("#")
    try:
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) >= 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        pass
    return default


def mix_color(c1: Any, c2: Any, t: float) -> tuple[int, int, int]:
    """c1 与 c2 线性混色，t=0 得 c1，t=1 得 c2。用于画「淡色标签」底色。"""
    a = hex_to_rgb(c1)
    b = hex_to_rgb(c2)
    t = max(0.0, min(1.0, float(t)))
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))  # type: ignore[return-value]


def darken(color: Any, k: float = 0.7) -> tuple[int, int, int]:
    """把颜色压暗（k<1），用于卡片分隔线、禁用态。"""
    c = hex_to_rgb(color)
    k = max(0.0, min(1.0, float(k)))
    return tuple(int(round(v * k)) for v in c)  # type: ignore[return-value]


def brighten(color: Any, k: float = 1.2) -> tuple[int, int, int]:
    """把颜色提亮（k>1）。"""
    c = hex_to_rgb(color)
    return tuple(min(255, int(round(v * float(k)))) for v in c)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 元素 / 职业 / 命途 配色与中文名
# ---------------------------------------------------------------------------

# 原神：同时收录英文（store 里的值）与中文（解析后的值）两种键
GI_ELEM = {
    "Fire": (232, 89, 60),
    "火": (232, 89, 60),
    "Water": (74, 159, 232),
    "水": (74, 159, 232),
    "Wind": (95, 201, 168),
    "Anemo": (95, 201, 168),
    "风": (95, 201, 168),
    "Electric": (169, 127, 232),
    "Electro": (169, 127, 232),
    "雷": (169, 127, 232),
    "Grass": (124, 201, 74),
    "Dendro": (124, 201, 74),
    "草": (124, 201, 74),
    "Ice": (111, 211, 232),
    "Cryo": (111, 211, 232),
    "冰": (111, 211, 232),
    "Rock": (212, 165, 60),
    "Geo": (212, 165, 60),
    "岩": (212, 165, 60),
    "": FG_DIM,
}

GI_ELEM_CN = {
    "Fire": "火",
    "Water": "水",
    "Wind": "风",
    "Anemo": "风",
    "Electric": "雷",
    "Electro": "雷",
    "Grass": "草",
    "Dendro": "草",
    "Ice": "冰",
    "Cryo": "冰",
    "Rock": "岩",
    "Geo": "岩",
}

# 崩铁：store 里实际用的是 Thunder（不是 Lightning），两种都收
HSR_ELEM = {
    "Fire": (232, 89, 60),
    "Ice": (111, 211, 232),
    "Wind": (95, 201, 168),
    "Thunder": (169, 127, 232),
    "Lightning": (169, 127, 232),
    "Electric": (169, 127, 232),
    "Physical": (200, 200, 205),
    "Quantum": (110, 130, 235),
    "Imaginary": (240, 210, 120),
    "": FG_DIM,
}

HSR_ELEM_CN = {
    "Fire": "火",
    "Ice": "冰",
    "Wind": "风",
    "Thunder": "雷",
    "Lightning": "雷",
    "Electric": "雷",
    "Physical": "物理",
    "Quantum": "量子",
    "Imaginary": "虚数",
}

# 崩铁命途（store 里只有英文 AvatarBaseType，没有中文，用这张表）
HSR_PATH = {
    "Warrior": "毁灭",
    "Rogue": "巡猎",
    "Mage": "智识",
    "Shaman": "同谐",
    "Warlock": "虚无",
    "Knight": "存护",
    "Priest": "丰饶",
    "Memory": "记忆",
    "Elation": "欢愉",
    "Remembrance": "记忆",
}

# 绝区零：store 里实际值是 Elec / Physics / FireFrost / AuricEther / Lumen /
# ZhenZhenAssault，规格文档写的 Electric / Physical / Frost / Auric 也一并收。
ZZZ_ELEM = {
    "Fire": (232, 89, 60),
    "Ice": (111, 211, 232),
    "Ether": (190, 120, 232),
    "Physical": (200, 200, 205),
    "Physics": (200, 200, 205),
    "Electric": (169, 127, 232),
    "Elec": (169, 127, 232),
    "Frost": (140, 215, 235),
    "FireFrost": (215, 130, 175),
    "Auric": (245, 205, 95),
    "AuricEther": (245, 205, 95),
    "Lumen": (250, 220, 140),
    "Wind": (95, 201, 168),
    "ZhenZhenAssault": (250, 160, 110),
    "": FG_DIM,
}

ZZZ_ELEM_CN = {
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

# 绝区零职业
ZZZ_PROF_CN = {
    "Attack": "强攻",
    "Stun": "击破",
    "Anomaly": "异常",
    "Support": "支援",
    "Defense": "防护",
    "Rupture": "命破",
    "Armorer": "防护",  # 新版本职业，实际语义同样是防护系
}

# 统一入口
ELEM_COLOR = {"gi": GI_ELEM, "hsr": HSR_ELEM, "zzz": ZZZ_ELEM}
ELEM_CN = {"gi": GI_ELEM_CN, "hsr": HSR_ELEM_CN, "zzz": ZZZ_ELEM_CN}


def elem_color(game: str, elem: Any, default: tuple[int, int, int] = FG_DIM) -> tuple[int, int, int]:
    """按游戏取元素颜色；未知元素返回 default。"""
    g = normalize_game(str(game))
    table = ELEM_COLOR.get(g, {})
    return table.get(str(elem or ""), default)


def elem_cn(game: str, elem: Any, default: str = "") -> str:
    """按游戏取元素中文名。"""
    g = normalize_game(str(game))
    return ELEM_CN.get(g, {}).get(str(elem or ""), default)


def path_cn(base_type: Any) -> str:
    """崩铁命途中文名。"""
    return HSR_PATH.get(str(base_type or ""), str(base_type or ""))


def profession_cn(prof: Any) -> str:
    """绝区零职业中文名。"""
    return ZZZ_PROF_CN.get(str(prof or ""), str(prof or ""))


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

FONT_CANDIDATES = [
    # 容器内（目标环境）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # Windows（本地开发自测）
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]

_FONT_CACHE: dict[tuple[int, bool], Any] = {}
_FONT_PATH: str | None = None
_FONT_PATH_RESOLVED = False


def _find_font() -> str | None:
    global _FONT_PATH, _FONT_PATH_RESOLVED
    if _FONT_PATH_RESOLVED:
        return _FONT_PATH
    for p in FONT_CANDIDATES:
        try:
            if os.path.exists(p):
                _FONT_PATH = p
                break
        except Exception:
            continue
    _FONT_PATH_RESOLVED = True
    return _FONT_PATH


def font(size: int = 16, bold: bool = False):
    """取字体对象（带缓存）。找不到中文字体时退回 Pillow 默认字体。"""
    size = max(6, int(size))
    key = (size, bool(bold))
    hit = _FONT_CACHE.get(key)
    if hit is not None:
        return hit

    path = _find_font()
    if path and bold and "Regular" in path:
        alt = path.replace("Regular", "Bold")
        try:
            if os.path.exists(alt):
                path = alt
        except Exception:
            pass
    if path and bold and os.path.basename(path).lower() == "msyh.ttc":
        alt = path.replace("msyh.ttc", "msyhbd.ttc")
        try:
            if os.path.exists(alt):
                path = alt
        except Exception:
            pass

    try:
        f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        try:
            f = ImageFont.load_default()
        except Exception:  # pragma: no cover
            f = None
    _FONT_CACHE[key] = f
    return f


# ---------------------------------------------------------------------------
# 绘图基元
# ---------------------------------------------------------------------------


def new_image(width: int, height: int, bg: Any = BG, *, mode: str = "RGB"):
    """新建画布，返回 (img, draw)。"""
    img = Image.new(mode, (max(1, int(width)), max(1, int(height))), bg)
    return img, ImageDraw.Draw(img)


def text_w(draw, s: Any, f) -> int:
    """测字符串宽度（draw 保留是为了兼容旧调用）；异常时按字数估算。"""
    s = "" if s is None else str(s)
    if not s:
        return 0
    try:
        box = draw.textbbox((0, 0), s, font=f)
        return int(box[2] - box[0])
    except Exception:
        try:
            return int(draw.textlength(s, font=f))
        except Exception:
            size = getattr(getattr(f, "size", None), "real", None) or 16
            return int(len(s) * size * 0.62)


def text_h(draw, s: Any, f) -> int:
    """测字符串高度。"""
    s = "" if s is None else str(s)
    try:
        box = draw.textbbox((0, 0), s, font=f)
        return int(box[3] - box[1])
    except Exception:
        size = getattr(getattr(f, "size", None), "real", None) or 16
        return int(size * 1.3)


def rounded(draw, box, r: int = 10, fill=None, outline=None, width: int = 1):
    """圆角矩形（老版本 Pillow 自动退回直角矩形）。"""
    try:
        draw.rounded_rectangle(box, radius=int(r), fill=fill, outline=outline, width=width)
    except Exception:
        draw.rectangle(box, fill=fill, outline=outline, width=width)


def draw_bar(
    draw,
    x: float,
    y: float,
    w: float,
    h: float,
    ratio: float,
    color: Any,
    bg: Any = None,
) -> None:
    """画进度条。ratio 会自动夹到 0~1。"""
    x, y, w, h = float(x), float(y), float(w), float(h)
    r = max(1, int(h / 2))
    rounded(draw, (x, y, x + w, y + h), r, bg if bg is not None else LINE)
    try:
        ratio = max(0.0, min(1.0, float(ratio)))
    except Exception:
        ratio = 0.0
    fw = w * ratio
    if fw >= 1:
        rounded(draw, (x, y, x + max(fw, h), y + h), r, color)


def draw_tag(
    draw,
    x: float,
    y: float,
    text: Any,
    color: Any,
    f=None,
    *,
    text_color: Any = None,
    pad_x: int = 7,
    pad_y: int = 3,
    radius: int = 5,
) -> tuple[int, int]:
    """画一个小标签（淡色底 + 彩色字），返回占用的 (宽, 高) 方便右移光标。"""
    text = "" if text is None else str(text)
    if f is None:
        f = font(13)
    tw = text_w(draw, text, f)
    th = text_h(draw, text, f) or 14
    w = tw + pad_x * 2
    h = th + pad_y * 2
    fill = mix_color(BG_CARD, color, 0.22)
    rounded(draw, (x, y, x + w, y + h), radius, fill)
    try:
        draw.text((x + pad_x, y + pad_y - 1), text, font=f, fill=text_color or color)
    except Exception:
        pass
    return (int(w), int(h))


def draw_star(draw, cx: float, cy: float, r: float = 7, color: Any = ACCENT) -> None:
    """画一颗五角星（中心 cx,cy，外半径 r）。"""
    r = float(r)
    inner = r * 0.42
    pts = []
    for i in range(10):
        ang = math.pi / 2 + i * math.pi / 5  # 从正上方开始顺时针
        rad = r if i % 2 == 0 else inner
        # 屏幕坐标 y 向下，所以 cos/sin 对调方向
        pts.append((cx + rad * math.cos(ang), cy - rad * math.sin(ang)))
    try:
        draw.polygon(pts, fill=hex_to_rgb(color, ACCENT))
    except Exception:
        pass


def draw_stars(
    draw,
    x: float,
    y: float,
    n: int,
    r: float = 7,
    color: Any = None,
    *,
    gap: float = 4,
    size: float | None = None,
) -> float:
    """从 (x,y) 开始向右画 n 颗星（y 是星星中心线），返回结束后的 x。

    兼容旧写法：draw_stars(draw, x, y, n, size=7, color=...)
    """
    if size is not None:
        r = size
    try:
        n = max(0, int(n))
    except Exception:
        n = 0
    r = float(r)
    step = r * 2 + gap
    for i in range(n):
        draw_star(draw, x + i * step + r, y, r, color if color is not None else ACCENT)
    return x + n * step


def truncate(draw, text: Any, f, max_w: float, ellipsis: str = "…") -> str:
    """超宽截断并补省略号。"""
    text = "" if text is None else str(text)
    if max_w <= 0 or not text:
        return ""
    if text_w(draw, text, f) <= max_w:
        return text
    ell_w = text_w(draw, ellipsis, f)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_w(draw, text[:mid], f) + ell_w <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


def wrap_text(draw, text: Any, f, max_w: float) -> list[str]:
    """按宽度折行（中英文混排都按字符切），返回行列表。"""
    text = "" if text is None else str(text)
    if not text:
        return []
    max_w = max(1.0, float(max_w))
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            trial = cur + ch
            if cur and text_w(draw, trial, f) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        lines.append(cur)
    return lines


def draw_section(draw, x: float, y: float, w: float, title: Any, f=None, *, color: Any = ACCENT) -> float:
    """画一个分区标题（左侧竖色条 + 标题 + 一条横线），返回下一行的 y。"""
    if f is None:
        f = font(17, True)
    h = text_h(draw, title, f) or 20
    try:
        draw.rectangle((x, y + 2, x + 3, y + h - 2), fill=hex_to_rgb(color, ACCENT))
    except Exception:
        pass
    try:
        draw.text((x + 12, y), str(title), font=f, fill=FG)
        draw.line((x, y + h + 6, x + w, y + h + 6), fill=LINE)
    except Exception:
        pass
    return y + h + 12


def draw_kv(
    draw,
    x: float,
    y: float,
    label: Any,
    value: Any,
    *,
    f_label=None,
    f_value=None,
    label_color: Any = FG_MUTED,
    value_color: Any = FG,
) -> tuple[int, int]:
    """画一行「标签 + 值」，返回 (宽, 高)。"""
    f_label = f_label or font(13)
    f_value = f_value or font(14, True)
    label = "" if label is None else str(label)
    value = "" if value is None else str(value)
    lw = text_w(draw, label, f_label)
    try:
        draw.text((x, y), label, font=f_label, fill=hex_to_rgb(label_color))
        draw.text((x + lw + 6, y - 1), value, font=f_value, fill=hex_to_rgb(value_color))
    except Exception:
        pass
    return (lw + 6 + text_w(draw, value, f_value), max(text_h(draw, value, f_value), 16))


def save_webp(img, quality: int = 88, method: int = 2) -> bytes:
    """画布 -> WebP 字节（QQ 发图用）。

    method 是 Pillow 的编码档（0~6，越大压缩越狠但越慢）。
    实测同一张 900x916 真实详情图案布（3 次取最快）：
        method=6:  77ms  40.3KB  PSNR 40.6dB   ← 旧默认，最慢档
        method=4:  45ms  41.0KB  PSNR 40.7dB
        method=2:  21ms  42.8KB  PSNR 40.6dB   ← 现默认，快 3.7 倍
        method=1:  18ms  48.1KB  PSNR 40.9dB
    method=2 体积只 +6.2%，PSNR 仍 40.6dB（>40dB 肉眼不可辨），
    但编码耗时降到 1/3.7。三个游戏共用此函数，改一处全部受益。
    """
    try:
        q = int(quality)
    except Exception:
        q = 88
    try:
        m = max(0, min(6, int(method)))
    except Exception:
        m = 2

    buf = io.BytesIO()
    try:
        img.save(buf, "WEBP", quality=q, method=m)
    except Exception:
        # 某些模式（如 P/RGBA）不吃 method，退回最简调用
        buf = io.BytesIO()
        try:
            img.convert("RGB").save(buf, "WEBP", quality=q)
        except Exception:
            img.save(buf, "WEBP")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 通用格式化
# ---------------------------------------------------------------------------


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(",", "").replace("%", "")
        return float(s)
    except Exception:
        return default


def fmt_int(v: Any) -> str:
    """千分位整数：1234567 -> '1,234,567'。"""
    if v is None:
        return "0"
    try:
        if isinstance(v, str):
            v = v.strip().replace(",", "")
        return f"{int(round(float(v))):,}"
    except Exception:
        return str(v)


def fmt_pct(v: Any, digits: int = 1) -> str:
    """比例 -> 百分比字符串：0.663 -> '66.3%'。"""
    x = _to_float(v) * 100.0
    return f"{x:.{max(0, int(digits))}f}%"


def fmt_pct_raw(v: Any, digits: int = 1) -> str:
    """已经是百分数，直接加 %：66.3 -> '66.3%'。"""
    return f"{_to_float(v):.{max(0, int(digits))}f}%"


def fmt_num(v: Any, digits: int = 1) -> str:
    """普通数值，保留 digits 位；整数时不带小数点。"""
    x = _to_float(v)
    d = max(0, int(digits))
    if d == 0 or abs(x - round(x)) < 1e-9:
        return f"{x:.0f}"
    return f"{x:.{d}f}"


def fmt_signed(v: Any, digits: int = 1, pct: bool = False) -> str:
    """带正负号：'+12.5%' / '-30'。"""
    x = _to_float(v) * (100.0 if pct else 1.0)
    sign = "+" if x >= 0 else ""
    body = f"{x:.{max(0, int(digits))}f}" if abs(x - round(x)) > 1e-9 else f"{x:.0f}"
    return f"{sign}{body}{'%' if pct else ''}"


def fmt_score(v: Any) -> str:
    """评分：大数用 k 表示。"""
    x = _to_float(v)
    if abs(x) >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if abs(x) >= 10_000:
        return f"{x / 1000:.1f}k"
    return f"{int(round(x)):,}"


def fmt_duration(seconds: Any) -> str:
    """秒 -> '1天2小时' 这类中文时长。"""
    try:
        s = int(float(seconds))
    except Exception:
        return "-"
    if s <= 0:
        return "0 秒"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


def ratio_of(value: Any, maximum: Any, default: float = 0.0) -> float:
    """安全算比例，用于进度条。"""
    try:
        mx = float(maximum)
        if mx <= 0:
            return default
        return max(0.0, min(1.0, float(value) / mx))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 圣遗物评分（三个游戏通用）
#
# 设计原则：评分要能横向比较，且不依赖外部数据库。
#   · 每个词条按「有效权重 × 强化命中率」折算
#   · 主词条单独计分（主词条本来就是「对的」才有价值）
#   · 归一化到 0~100，且用固定分母（不能用「同角色内最高分」，
#     否则每件都接近满分——这是上一版评分全 100 的根因）
# ---------------------------------------------------------------------------

# 词条类型 -> (显示名, 是否百分比, 满分单次强化值)
# 满分单次值用于计算「这条词条强化到了多高」
SUBSTAT_MAX_VALUE = {
    # 原神
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
    # 崩铁（数值按词条原始比例，已是比例值）
    "CriticalChance": 0.0486,
    "CriticalDamage": 0.0972,
    "AttackAddedRatio": 0.0648,
    "HPAddedRatio": 0.0648,
    "DefenceAddedRatio": 0.081,
    "SpeedDelta": 3.9,
    "BreakDamageAddedRatio": 0.10368,
    "StatusProbability": 0.0648,
    "StatusResistance": 0.0648,
    "AttackDelta": 6.774,
    "HPDelta": 13.548,
    "DefenceDelta": 6.774,
}

# 词条价值权重（越有用的词条越高）
SUBSTAT_WEIGHT = {
    "FIGHT_PROP_CRITICAL": 1.00,
    "FIGHT_PROP_CRITICAL_HURT": 1.00,
    "FIGHT_PROP_ATTACK_PERCENT": 0.85,
    "FIGHT_PROP_HP_PERCENT": 0.55,
    "FIGHT_PROP_DEFENSE_PERCENT": 0.40,
    "FIGHT_PROP_CHARGE_EFFICIENCY": 0.70,
    "FIGHT_PROP_ELEMENT_MASTERY": 0.50,
    "FIGHT_PROP_HP": 0.25,
    "FIGHT_PROP_ATTACK": 0.35,
    "FIGHT_PROP_DEFENSE": 0.25,
    "CriticalChance": 1.00,
    "CriticalDamage": 1.00,
    "AttackAddedRatio": 0.85,
    "HPAddedRatio": 0.55,
    "DefenceAddedRatio": 0.40,
    "SpeedDelta": 0.90,
    "BreakDamageAddedRatio": 0.75,
    "StatusProbability": 0.55,
    "StatusResistance": 0.40,
    "AttackDelta": 0.35,
    "HPDelta": 0.25,
    "DefenceDelta": 0.25,
}

# 高亮词条（金色强调）
HIGHLIGHT_SUBSTATS = {
    "FIGHT_PROP_CRITICAL",
    "FIGHT_PROP_CRITICAL_HURT",
    "CriticalChance",
    "CriticalDamage",
}

# 评分分母：4 条副词条的「等效满值强化次数 × 价值权重」之和。
# 实测单件总和约 3.5~9.5，极品（全暴击爆伤）可达 9+。
# 取 9.0 让 top1 落在 95 分附近，常见好件在 70~85。
# 注意：分母必须是固定值，不能用「同角色内最高分」归一化，
# 否则每件都接近满分——那是上一版评分全 100 的根因。
SCORE_DENOMINATOR = 9.0


# ---------------------------------------------------------------------------
# 圣遗物评分（按社区通用真实规则重写）
#
# 原神「双暴分法」（最通用，社区公认）：
#     分数 = 暴击率 × 2 + 暴击伤害 × 1
#     40 分 = 毕业线，50+ = 极品，理论上限约 54.6
# 扩展有效词条时按权重比 暴击:爆伤:攻击% = 1:2:1.5 折算，
# 即 攻击% × 1.333、精通 × 0.333、充能 × 0.5。
#
# 崩铁沿用同一套双暴逻辑（社区遗器分数）：
#     分数 = 暴击率 × 2 + 暴击伤害 × 1 + 攻击% × 1.3
#
# 绝区零工坊是「配平到 55 分/件」，与上面两套量纲不同，单独算。
# ---------------------------------------------------------------------------

# 有效词条权重（原神/崩铁通用，按 1% 暴击 = 2 分换算）
SUBSTAT_SCORE_RATE = {
    # 原神
    "FIGHT_PROP_CRITICAL": 2.00,          # 暴击率 1% = 2 分
    "FIGHT_PROP_CRITICAL_HURT": 1.00,     # 暴击伤害 1% = 1 分
    "FIGHT_PROP_ATTACK_PERCENT": 1.333,   # 攻击% 1% = 1.333 分
    "FIGHT_PROP_ELEMENT_MASTERY": 0.333,  # 精通 1 点 = 0.333 分
    "FIGHT_PROP_CHARGE_EFFICIENCY": 0.50, # 充能 1% = 0.5 分
    "FIGHT_PROP_HP_PERCENT": 0.50,
    "FIGHT_PROP_DEFENSE_PERCENT": 0.30,
    # 崩铁（数值已是百分比或定点）
    "CriticalChance": 2.00,
    "CriticalDamage": 1.00,
    "AttackAddedRatio": 1.333,
    "SpeedDelta": 1.50,
    "BreakDamageAddedRatio": 1.00,
    "HPAddedRatio": 0.50,
    "DefenceAddedRatio": 0.30,
    "StatusProbability": 0.50,
    "StatusResistance": 0.30,
}

# 原神/崩铁：毕业线（双暴分口径）
SCORE_EXCELLENT = 40.0   # 毕业
SCORE_GOD = 50.0         # 极品
SCORE_MAX_THEORY = 54.6  # 理论极限

# 绝区零：单件满分 55，全身 330
ZZZ_MAX_PER_PIECE = 55.0
ZZZ_MAX_TOTAL = 330.0


# 绝区零属性名 -> 统一词条 key 的别名表
# （绝区零 property.json 的 Name 字段与两套通用 key 都不同，需先映射）
ZZZ_SUBSTAT_ALIAS = {
    "Crit": "CriticalChance",
    "CritDmg": "CriticalDamage",
    "Atk_Ratio": "AttackAddedRatio",
    "Atk": "AttackDelta",
    "HpMax_Ratio": "HPAddedRatio",
    "HpMax": "HPDelta",
    "Def_Ratio": "DefenceAddedRatio",
    "Def": "DefenceDelta",
    "PenRatio": "StatusProbability",
    "PenDelta": "DefenceDelta",
    "SpRatio": "StatusResistance",
    "ElementMystery": "BreakDamageAddedRatio",
    "AnomalyMastery": "StatusProbability",
    "AnomalyProficiency": "BreakDamageAddedRatio",
}


def _norm_substat_key(prop: Any) -> str:
    """把各游戏的词条标识统一成 SUBSTAT_SCORE_RATE 里用的 key。"""
    if prop is None:
        return ""
    k = str(prop).strip()
    if not k:
        return ""
    if k in SUBSTAT_SCORE_RATE:
        return k
    if k in ZZZ_SUBSTAT_ALIAS:
        return ZZZ_SUBSTAT_ALIAS[k]
    return k


def substat_score(prop: Any, value: Any) -> float:
    """单条副词条按社区双暴分法折算的「分」。

    只在「数值口径」已经是百分比/定点时直接可用：
      原神 flat.statValue 已是 17.9 这样的百分数
      崩铁 _props 里是 0.188 这样的小数，调用方需先 ×100
    """
    key = _norm_substat_key(prop)
    if not key:
        return 0.0
    rate = SUBSTAT_SCORE_RATE.get(key)
    if not rate:
        return 0.0
    try:
        v = abs(float(value))
    except Exception:
        return 0.0
    return v * rate


def artifact_score(
    subs: list,
    main_ok: bool = True,
    *,
    game: str = "gi",
) -> float:
    """圣遗物评分。

    subs: [{"prop": 词条key, "value": 数值(已统一为百分数口径)}, ...]
    main_ok: 主词条是否对口（不对口则整体打折）
    game: gi / hsr / zzz

    返回：
        gi、hsr -> 双暴分（40 毕业 / 50 极品）
        zzz    -> 折算到 55 分单件制
    """
    if not subs:
        return 0.0

    total = 0.0
    for s in subs:
        total += substat_score(s.get("prop"), s.get("value"))

    # 主词条不对口：社区做法是「直接不计分/价值很低」
    if not main_ok:
        total *= 0.55

    if game == "zzz":
        # 绝区零：把双暴分折算成 55 分制（经验换算：45 双暴分 ≈ 55 单件满分）
        return round(min(ZZZ_MAX_PER_PIECE, total / 45.0 * ZZZ_MAX_PER_PIECE), 1)

    return round(min(SCORE_MAX_THEORY, total), 1)


def score_grade(score: Any, *, game: str = "gi") -> str:
    """评分 -> 等级。按各游戏社区口径。"""
    try:
        s = float(score)
    except Exception:
        return "-"

    if game == "zzz":
        # 绝区零 55 分制
        if s >= 48:
            return "SSS"
        if s >= 42:
            return "SS"
        if s >= 36:
            return "S"
        if s >= 30:
            return "A"
        if s >= 22:
            return "B"
        return "C"

    # 原神/崩铁 双暴分制
    if s >= SCORE_GOD:
        return "ACE"     # 50+ 极品
    if s >= SCORE_EXCELLENT:
        return "SSS"     # 40+ 毕业
    if s >= 32:
        return "SS"
    if s >= 24:
        return "S"
    if s >= 16:
        return "A"
    if s >= 8:
        return "B"
    return "C"


def score_color(score: Any, *, game: str = "gi") -> tuple[int, int, int]:
    """评分 -> 颜色（白底版，都压深保证可读）。"""
    g = score_grade(score, game=game)
    return {
        "ACE": (176, 96, 8),
        "SSS": (158, 108, 18),
        "SS": (128, 96, 32),
        "S": (42, 110, 178),
        "A": (32, 128, 96),
        "B": (110, 116, 126),
    }.get(g, FG_MUTED)


def score_benchmark(game: str = "gi") -> str:
    """返回评分口径说明（显示在图上，避免用户误解）。"""
    if game == "zzz":
        return "绝区零单件满分 55 分（工坊口径），全套满分 330"
    return "双暴分：暴击×2 + 暴伤×1，40 分毕业 / 50 分极品"


def render_artifact_grid(
    items: list,
    *,
    title: str = "圣遗物列表",
    uid: str = "",
    page: int = 1,
    page_size: int = 15,
    cols: int = 5,
    card_w: int = 330,
    card_h: int = 290,
    gap: int = 16,
    game: str = "gi",
) -> bytes:
    """渲染圣遗物网格图（对齐参考图布局，白底版）。

    items: [{
        "name": 圣遗物名,
        "slot": 部位,
        "score": 评分 0~100,
        "set": 套装名,
        "main_name": 主词条名, "main_value": "4,780",
        "subs": [{"text": "暴击率 +7.4%", "highlight": True}, ...],
        "rarity": 5,
    }, ...]

    返回 WebP 字节。
    """
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * page_size
    chunk = items[start : start + page_size]

    rows = max(1, (len(chunk) + cols - 1) // cols)
    pad = 26
    head_h = 96
    foot_h = 56

    W = pad * 2 + cols * card_w + (cols - 1) * gap
    H = head_h + rows * card_h + (rows - 1) * gap + foot_h + pad

    img, d = new_image(W, H, BG)

    # ---- 头部 ----
    d.rectangle((0, 0, W, head_h), fill=BG_CARD)
    d.line((0, head_h, W, head_h), fill=ACCENT, width=3)

    f_title = font(34, True)
    d.text((pad, 22), title, font=f_title, fill=FG)

    f_sub = font(17)
    d.text(
        (pad, 64),
        f"共 {total} 件 · 第 {page}/{pages} 页",
        font=f_sub,
        fill=FG_DIM,
    )
    # 评分口径说明（放右上，让用户知道分数怎么来的）
    bench = score_benchmark(game)
    bw = text_w(d, bench, font(14))
    d.text((W - pad - bw, 66), bench, font=font(14), fill=FG_MUTED)

    if uid:
        t = f"UID {uid}"
        tw = text_w(d, t, f_sub)
        d.text((W - pad - tw, 64), t, font=f_sub, fill=FG_MUTED)

    # ---- 卡片网格 ----
    f_name = font(21, True)
    f_score = font(28, True)
    f_grade = font(16, True)
    f_set = font(15)
    f_main = font(21, True)
    f_sub = font(17)

    for i, it in enumerate(chunk):
        col, row = i % cols, i // cols
        x0 = pad + col * (card_w + gap)
        y0 = head_h + 14 + row * (card_h + gap)

        rarity = int(it.get("rarity") or 5)
        rc = star_color(rarity)

        # 卡片：白底 + 顶部稀有度色条 + 细边
        rounded(d, (x0, y0, x0 + card_w, y0 + card_h), 12, BG_CARD,
                outline=LINE, width=1)
        rounded(d, (x0, y0, x0 + card_w, y0 + 6), 3, rc)

        # ---- 第一行：主词条名（大，这是用户最关心的）+ 部位（右上小字）----
        main_nm = str(it.get("main_name") or it.get("name") or "-")
        d.text(
            (x0 + 14, y0 + 16),
            truncate(d, main_nm, f_name, card_w - 100),
            font=f_name,
            fill=FG,
        )
        slot = str(it.get("slot") or "")
        if slot:
            sw = text_w(d, slot, f_set)
            d.text((x0 + card_w - sw - 14, y0 + 19), slot, font=f_set, fill=FG_MUTED)

        # ---- 第二行：主词条数值（金色，大字）----
        main_txt = str(it.get("main_text") or "")
        # main_text 常是「攻击力 46.6%」，去掉与上一行重复的属性名
        if main_txt.startswith(main_nm):
            main_txt = main_txt[len(main_nm):].strip()
        d.text(
            (x0 + 14, y0 + 46),
            truncate(d, main_txt or "-", f_main, card_w - 130),
            font=f_main,
            fill=ACCENT_DEEP,
        )

        # ---- 评分（右上角，大字）----
        score = it.get("score")
        sc_color = score_color(score, game=game)
        sc_txt = fmt_num(score, 1) if score is not None else "-"
        grd = score_grade(score, game=game)
        gtxt = f"{sc_txt}"
        gw = text_w(d, gtxt, f_score)
        grade_t = f"{grd}"
        grd_w = text_w(d, grade_t, f_grade)
        # 分数在右，等级在分数左侧
        d.text((x0 + card_w - gw - 14, y0 + 40), gtxt, font=f_score, fill=sc_color)
        d.text(
            (x0 + card_w - gw - 14 - grd_w - 8, y0 + 48),
            grade_t,
            font=f_grade,
            fill=sc_color,
        )

        # ---- 分隔线 ----
        d.line((x0 + 14, y0 + 84, x0 + card_w - 14, y0 + 84), fill=LINE, width=1)

        # ---- 副词条 4 条 ----
        sy = y0 + 96
        for s in (it.get("subs") or [])[:4]:
            txt = str(s.get("text") or "")
            hl = bool(s.get("highlight"))
            color = ACCENT_DEEP if hl else FG_DIM
            prefix = "★ " if hl else "· "
            d.text(
                (x0 + 14, sy),
                truncate(d, prefix + txt, f_sub, card_w - 28),
                font=f_sub,
                fill=color,
            )
            sy += 27

        # ---- 套装名（底部）----
        st = it.get("set") or ""
        if st:
            d.line((x0 + 14, y0 + card_h - 40, x0 + card_w - 14, y0 + card_h - 40),
                   fill=LINE, width=1)
            d.text(
                (x0 + 14, y0 + card_h - 32),
                truncate(d, st, f_set, card_w - 28),
                font=f_set,
                fill=FG_MUTED,
            )

    # 页脚（项目主页）
    fw_txt = PROJECT_FOOTER
    fw = text_w(d, fw_txt, font(15))
    d.text(((W - fw) // 2, H - 30), fw_txt, font=font(15), fill=ACCENT_DEEP)

    return save_webp(img, 88)


# ---------------------------------------------------------------------------
# 圣遗物得分排行榜（白底版）
# ---------------------------------------------------------------------------


def render_rank_board(
    rows: list,
    *,
    game: str = "all",
    title: str = "",
    width: int = 1000,
) -> bytes:
    """渲染群圣遗物得分排行榜。

    rows: [{
        "rank": 1, "nickname": 昵称, "qq": QQ号, "game_cn": 游戏名,
        "uid": UID, "best": 最高分, "best_name": 圣遗物名, "best_set": 套装,
        "best_main": 主词条, "owner": 角色名, "count": 圣遗物总数,
    }, ...]
    """
    n = len(rows)
    game_cn = {
        "gi": "原神",
        "hsr": "崩铁",
        "zzz": "绝区零",
        "all": "三游戏综合",
    }.get(game, "")
    if not title:
        title = f"{game_cn}圣遗物得分排行榜"

    pad = 30
    head_h = 130
    row_h = 108
    foot_h = 84
    H = head_h + max(1, n) * row_h + foot_h + pad

    img, d = new_image(width, H, BG)

    # ---- 头部 ----
    d.rectangle((0, 0, width, head_h), fill=BG_CARD)
    d.line((0, head_h, width, head_h), fill=ACCENT, width=4)

    d.text((pad, 24), title, font=font(40, True), fill=FG)
    bench = score_benchmark(game if game in ("gi", "hsr", "zzz") else "gi")
    d.text(
        (pad, 82),
        f"共 {n} 位群友上榜 · 按最高单件评分排名 · {bench}",
        font=font(17),
        fill=FG_DIM,
    )

    # ---- 表头 ----
    y = head_h + 14
    f_hdr = font(16, True)
    d.rectangle((pad, y, width - pad, y + 42), fill=BG_CARD2)
    d.text((pad + 16, y + 10), "排名", font=f_hdr, fill=FG_DIM)
    d.text((pad + 96, y + 10), "群友", font=f_hdr, fill=FG_DIM)
    d.text((pad + 330, y + 10), "最高分圣遗物", font=f_hdr, fill=FG_DIM)
    d.text((pad + 640, y + 10), "角色", font=f_hdr, fill=FG_DIM)
    rtxt = "评分"
    rw = text_w(d, rtxt, f_hdr)
    d.text((width - pad - rw - 20, y + 10), rtxt, font=f_hdr, fill=FG_DIM)
    y += 42

    # ---- 行 ----
    f_rank = font(30, True)
    f_name = font(23, True)
    f_sub = font(15)
    f_item = font(18, True)
    f_score = font(34, True)
    f_grade = font(17, True)

    medal = {1: (212, 160, 56), 2: (158, 158, 168), 3: (176, 124, 76)}

    for r in rows:
        rank = int(r.get("rank") or 0)
        # 行底：前三名浅金底
        if rank <= 3:
            d.rectangle((pad, y, width - pad, y + row_h - 8),
                        fill=mix_color(BG_CARD, ACCENT, 0.10 if rank == 1 else 0.06))
        else:
            d.rectangle((pad, y, width - pad, y + row_h - 8), fill=BG_CARD)
        d.line((pad, y + row_h - 8, width - pad, y + row_h - 8), fill=LINE, width=1)

        # 排名
        rk_color = medal.get(rank, FG_MUTED)
        rk_txt = str(rank)
        rw = text_w(d, rk_txt, f_rank)
        d.text((pad + 30 + (46 - rw) // 2, y + 22), rk_txt, font=f_rank, fill=rk_color)

        # 昵称（QQ 号作副标题）
        nick = truncate(d, r.get("nickname") or "", f_name, 210)
        d.text((pad + 96, y + 18), nick, font=f_name, fill=FG)
        qq = str(r.get("qq") or "")
        gname = r.get("game_cn") or ""
        sub = f"{gname} · {r.get('uid') or ''}"
        d.text((pad + 96, y + 54), truncate(d, sub, f_sub, 210), font=f_sub, fill=FG_MUTED)

        # 最高分圣遗物
        item = truncate(d, r.get("best_name") or r.get("best_main") or "-", f_item, 270)
        d.text((pad + 330, y + 18), item, font=f_item, fill=FG)
        st = r.get("best_set") or ""
        mn = r.get("best_main") or ""
        line2 = "  ".join(x for x in (st, mn) if x)
        d.text((pad + 330, y + 50), truncate(d, line2, f_sub, 280), font=f_sub, fill=FG_DIM)

        # 角色
        owner = truncate(d, r.get("owner") or "-", f_sub, 180)
        d.text((pad + 640, y + 30), owner, font=f_sub, fill=FG_DIM)

        # 分数
        sc = float(r.get("best") or 0)
        sc_color = score_color(sc, game=game if game in ("gi", "hsr", "zzz") else "gi")
        sc_txt = fmt_num(sc, 1)
        sw = text_w(d, sc_txt, f_score)
        d.text((width - pad - sw - 20, y + 16), sc_txt, font=f_score, fill=sc_color)

        grade = score_grade(sc, game=game if game in ("gi", "hsr", "zzz") else "gi")
        gw = text_w(d, grade, f_grade)
        d.text((width - pad - gw - 20, y + 58), grade, font=f_grade, fill=sc_color)

        y += row_h

    # ---- 页脚 ----
    src_txt = "数据来源 enka.network · 评分基于副词条有效权重"
    sw = text_w(d, src_txt, font(13))
    d.text(((width - sw) // 2, H - 58), src_txt, font=font(13), fill=FG_MUTED)

    fw = text_w(d, PROJECT_FOOTER, font(15))
    d.text(((width - fw) // 2, H - 34), PROJECT_FOOTER, font=font(15), fill=ACCENT_DEEP)

    return save_webp(img, 88)


# 项目主页（所有图片底部统一展示）
PROJECT_URL = "https://github.com/fyjgtddco/mihoyo-panel"
PROJECT_FOOTER = f"Github：{PROJECT_URL}"


def draw_project_footer(
    draw,
    width: int,
    y: int,
    *,
    extra: str = "",
    size: int = 14,
    color: Any = None,
    center: bool = True,
    x: int | None = None,
) -> int:
    """在图片底部画项目主页地址。

    extra: 可选的附加说明（如「数据来源 enka.network」），会放在上一行。
    返回文本占用的总高度。
    """
    f = font(size)
    f_small = font(max(12, size - 2))
    c = color if color is not None else FG_MUTED
    used = 0

    if extra:
        ew = text_w(draw, extra, f_small)
        ex = ((width - ew) // 2) if center else (x or 0)
        draw.text((ex, y), extra, font=f_small, fill=FG_MUTED)
        used += int(size * 1.5) + 4
        y += used

    # 主页地址：带下划线颜色（金色，白底上够醒目）
    tw = text_w(draw, PROJECT_FOOTER, f)
    tx = ((width - tw) // 2) if center else (x or 0)
    draw.text((tx, y), PROJECT_FOOTER, font=f, fill=ACCENT_DEEP)
    used += int(size * 1.5)

    return used


# ===========================================================================
# v4 新增渲染器
# ===========================================================================


def _card_head(d, width: int, title: str, subtitle: str = "", h: int = 110):
    """统一的卡片头部（白底 + 金线）。返回内容起始 y。"""
    d.rectangle((0, 0, width, h), fill=BG_CARD)
    d.line((0, h, width, h), fill=ACCENT, width=4)
    d.text((30, 20), title, font=font(38, True), fill=FG)
    if subtitle:
        d.text((30, 70), subtitle, font=font(17), fill=FG_DIM)
    return h + 18


def _kv_row(d, x: int, y: int, label: str, value: str, *, w: int = 0,
            vcolor=None, f_label=None, f_value=None):
    """一行「标签 + 值」。"""
    fl = f_label or font(15)
    fv = f_value or font(22, True)
    d.text((x, y), label, font=fl, fill=FG_MUTED)
    d.text((x, y + 22), value, font=fv, fill=vcolor or FG)
    return y + 22 + int(fv.size * 1.4)


# ---------------------------------------------------------------------------
# 1. 角色伤害计算卡
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 技能倍率参考表（v4 伤害计算用）
#
# 说明：enka 不提供技能倍率，这里是常用角色的「元素战技/爆发」单段倍率
# 参考值（Lv.10 天赋，取主要输出段）。数据来自游戏内天赋描述。
# 未收录的角色会用「按等级估算」的通用系数，并在图上标注。
# ---------------------------------------------------------------------------

SKILL_MULTIPLIER = {
    # 原神：角色名 -> (战技倍率%, 爆发倍率%)
    "雷电将军": (0.0, 720.0),      # 梦想真说：初始切 720%
    "胡桃": (0.0, 617.0),          # 安神秘法
    "神里绫华": (0.0, 202.0),      # 神里流·霜灭 每段
    "甘雨": (0.0, 89.0),           # 降众天华 每段
    "宵宫": (0.0, 0.0),            # 普攻系
    "刻晴": (0.0, 401.0),          # 天街巡游
    "纳西妲": (0.0, 402.0),        # 心景幻成
    "妮露": (0.0, 293.0),
    "流浪者": (0.0, 265.0),
    "莱依拉": (0.0, 0.0),
    "芙宁娜": (0.0, 0.0),          # 辅助为主
    "那维莱特": (0.0, 0.0),        # 重击系
    "阿蕾奇诺": (0.0, 0.0),        # 普攻系
    "夜兰": (0.0, 0.0),
    "钟离": (0.0, 401.0),
    "温迪": (0.0, 0.0),
    "万叶": (0.0, 0.0),
    "班尼特": (0.0, 0.0),
    "行秋": (0.0, 0.0),
    "香菱": (0.0, 0.0),

    # 崩铁：角色名 -> 终结技倍率%
    "希儿": (0.0, 425.0),
    "流萤": (0.0, 0.0),
    "黄泉": (0.0, 660.0),
    "花火": (0.0, 0.0),
    "镜流": (0.0, 0.0),
    "景元": (0.0, 0.0),
    "刃": (0.0, 0.0),
    "银狼": (0.0, 0.0),
    "卡芙卡": (0.0, 0.0),
}

# 通用兜底：按角色等级估一个平均倍率（含技能）
DEFAULT_MULTIPLIER = {
    "gi": 220.0,
    "hsr": 260.0,
    "zzz": 240.0,
}


def skill_multiplier(game: str, char: str) -> tuple[float, bool]:
    """返回 (倍率%, 是否来自精确表)。"""
    row = SKILL_MULTIPLIER.get(str(char or ""))
    if row:
        e, q = row
        best = max(float(e or 0), float(q or 0))
        if best > 0:
            return best, True
    return float(DEFAULT_MULTIPLIER.get(game, 220.0)), False


def render_damage_card(
    info: dict,
    *,
    width: int = 1100,
) -> bytes:
    """角色伤害计算卡。

    info: {
        "game_cn": "原神", "char": 角色名, "owner": 玩家名, "uid": ...,
        "element": 元素, "level": 等级,
        "stats": [(标签, 值, 颜色), ...],      基础属性
        "skills": [(名称, 倍率文本), ...],     技能倍率
        "steps": [(说明, 值), ...],            计算过程
        "final": (标题, 数值文本),             最终伤害
        "note": 备注,
    }
    """
    pad = 30
    H = 110 + 30
    H += 40 + ((len(info.get("stats") or []) + 2) // 3) * 56
    H += 50 + len(info.get("skills") or []) * 40
    H += 50 + len(info.get("steps") or []) * 42
    H += 150  # 最终伤害区
    H += 90   # 页脚

    img, d = new_image(width, H, BG)
    y = _card_head(
        d, width,
        f"{info.get('char') or ''} 伤害计算",
        f"{info.get('game_cn') or ''} · {info.get('owner') or ''} · UID {info.get('uid') or ''}",
    )

    W = width - pad * 2

    # ---- 基础属性 ----
    y = draw_section(d, pad, y, W, "面板属性", font(20, True))
    stats = info.get("stats") or []
    # 3 列 2 行，行高 56（14 标签 + 21 值 + 间距），避免与下一行重叠
    for i, item in enumerate(stats):
        label, value = item[0], item[1]
        vc = item[2] if len(item) > 2 else FG
        col = i % 3
        row = i // 3
        cx = pad + col * (W // 3)
        cy = y + row * 56
        d.text((cx, cy), str(label), font=font(14), fill=FG_MUTED)
        d.text((cx, cy + 22), str(value), font=font(21, True), fill=vc)
    y += ((len(stats) + 2) // 3) * 56 + 8

    # ---- 技能倍率 ----
    y = draw_section(d, pad, y, W, "技能倍率", font(20, True))
    for name, mult in info.get("skills") or []:
        d.text((pad, y), str(name), font=font(17), fill=FG_DIM)
        d.text((pad + 260, y), str(mult), font=font(18, True), fill=ACCENT_DEEP)
        y += 40
    y += 10

    # ---- 计算过程 ----
    y = draw_section(d, pad, y, W, "计算过程", font(20, True))
    for desc, val in info.get("steps") or []:
        d.text((pad, y), str(desc), font=font(16), fill=FG_DIM)
        vtxt = str(val)
        vw = text_w(d, vtxt, font(18, True))
        d.text((width - pad - vw, y), vtxt, font=font(18, True), fill=FG)
        y += 42
    y += 10

    # ---- 最终伤害 ----
    ftitle, fvalue = info.get("final") or ("期望伤害", "-")
    box_y = y
    rounded(d, (pad, box_y, width - pad, box_y + 110), 14,
            mix_color(BG_CARD, ACCENT, 0.12), outline=ACCENT, width=2)
    d.text((pad + 24, box_y + 18), str(ftitle), font=font(19, True), fill=ACCENT_DEEP)
    d.text((pad + 24, box_y + 48), str(fvalue), font=font(46, True), fill=ACCENT_DEEP)

    note = info.get("note") or ""
    if note:
        d.text((pad, box_y + 122), note, font=font(14), fill=FG_MUTED)

    # ---- 页脚 ----
    draw_project_footer(d, width, H - 42, extra="数据来源 enka.network · 本地估算")

    return save_webp(img, 88)


# ---------------------------------------------------------------------------
# 3. 战绩卡
# ---------------------------------------------------------------------------


def render_record_card(
    info: dict,
    *,
    width: int = 1100,
) -> bytes:
    """战绩卡（深境螺旋 / 混沌回忆 / 式舆防卫战）。

    info: {
        "game_cn", "nickname", "uid", "signature",
        "main": (标题, 主数值, 副说明),
        "items": [(名称, 值, 说明), ...],
        "highlights": [(标签, 值), ...],
    }
    """
    pad = 30
    items = info.get("items") or []
    hl = info.get("highlights") or []
    H = 200 + 130 + len(items) * 92 + (60 + len(hl) * 0) + 90 + 40

    img, d = new_image(width, H, BG)

    # 头部
    d.rectangle((0, 0, width, 150), fill=BG_CARD)
    d.line((0, 150, width, 150), fill=ACCENT, width=4)
    d.text((pad, 24), str(info.get("nickname") or ""), font=font(40, True), fill=FG)
    d.text(
        (pad, 82),
        f"{info.get('game_cn') or ''} · UID {info.get('uid') or ''}",
        font=font(18),
        fill=FG_DIM,
    )
    sig = info.get("signature") or ""
    if sig:
        stxt = truncate(d, sig, font(15), width - pad * 2 - 240)
        d.text((pad, 114), stxt, font=font(15), fill=FG_MUTED)

    y = 172
    W = width - pad * 2

    # 主战绩大卡
    mtitle, mvalue, msub = info.get("main") or ("", "-", "")
    rounded(d, (pad, y, width - pad, y + 120), 14,
            mix_color(BG_CARD, ACCENT, 0.10), outline=ACCENT, width=2)
    d.text((pad + 24, y + 16), str(mtitle), font=font(20, True), fill=ACCENT_DEEP)
    d.text((pad + 24, y + 46), str(mvalue), font=font(48, True), fill=ACCENT_DEEP)
    if msub:
        d.text((pad + 24, y + 96), str(msub), font=font(15), fill=FG_DIM)
    y += 146

    # 分项
    if items:
        y = draw_section(d, pad, y, W, "详细战绩", font(20, True))
        for name, value, desc in items:
            d.rectangle((pad, y, width - pad, y + 80), fill=BG_CARD)
            d.text((pad + 18, y + 16), str(name), font=font(19, True), fill=FG)
            if desc:
                d.text((pad + 18, y + 46), str(desc), font=font(14), fill=FG_MUTED)
            vtxt = str(value)
            vw = text_w(d, vtxt, font(28, True))
            d.text((width - pad - vw - 18, y + 24), vtxt, font=font(28, True),
                   fill=ACCENT_DEEP)
            y += 92

    # 高亮标签
    if hl:
        y = draw_section(d, pad, y, W, "亮点", font(20, True))
        x = pad
        for label, value in hl:
            t = f"{label} {value}"
            tw = text_w(d, t, font(16))
            if x + tw + 24 > width - pad:
                x = pad
                y += 44
            rounded(d, (x, y, x + tw + 24, y + 36), 8,
                    mix_color(BG_CARD, ACCENT, 0.14))
            d.text((x + 12, y + 8), t, font=font(16), fill=ACCENT_DEEP)
            x += tw + 34
        y += 50

    draw_project_footer(d, width, H - 42, extra="数据来源 enka.network")
    return save_webp(img, 88)


# ---------------------------------------------------------------------------
# 4. 角色收集度排行
# ---------------------------------------------------------------------------


def render_collection_board(
    rows: list,
    *,
    game_cn: str = "",
    width: int = 1000,
) -> bytes:
    """群友角色收集度排行。

    rows: [{"rank", "nickname", "qq", "uid", "total", "five_star",
            "six_star", "char_count", "level_avg", "fetter_sum"}, ...]
    """
    n = len(rows)
    pad = 30
    head_h = 130
    row_h = 100
    foot_h = 90
    H = head_h + max(1, n) * row_h + foot_h + pad

    img, d = new_image(width, H, BG)

    d.rectangle((0, 0, width, head_h), fill=BG_CARD)
    d.line((0, head_h, width, head_h), fill=ACCENT, width=4)
    d.text((pad, 22), f"{game_cn}角色收集度排行", font=font(38, True), fill=FG)
    d.text((pad, 80), f"共 {n} 位群友上榜", font=font(18), fill=FG_DIM)

    y = head_h + 12
    f_hdr = font(16, True)
    d.rectangle((pad, y, width - pad, y + 40), fill=BG_CARD2)
    d.text((pad + 16, y + 10), "排名", font=f_hdr, fill=FG_DIM)
    d.text((pad + 96, y + 10), "群友", font=f_hdr, fill=FG_DIM)
    d.text((pad + 330, y + 10), "展示角色", font=f_hdr, fill=FG_DIM)
    d.text((pad + 470, y + 10), "高稀有度", font=f_hdr, fill=FG_DIM)
    d.text((pad + 620, y + 10), "平均等级", font=f_hdr, fill=FG_DIM)
    rw = text_w(d, "收集度", f_hdr)
    d.text((width - pad - rw - 20, y + 10), "收集度", font=f_hdr, fill=FG_DIM)
    y += 40

    medal = {1: (212, 160, 56), 2: (158, 158, 168), 3: (176, 124, 76)}
    max_total = max([int(r.get("total") or 0) for r in rows] or [1]) or 1

    for r in rows:
        rank = int(r.get("rank") or 0)
        if rank <= 3:
            d.rectangle((pad, y, width - pad, y + row_h - 8),
                        fill=mix_color(BG_CARD, ACCENT, 0.10 if rank == 1 else 0.05))
        else:
            d.rectangle((pad, y, width - pad, y + row_h - 8), fill=BG_CARD)
        d.line((pad, y + row_h - 8, width - pad, y + row_h - 8), fill=LINE, width=1)

        rk = str(rank)
        fr = font(30, True)
        rw = text_w(d, rk, fr)
        d.text((pad + 30 + (46 - rw) // 2, y + 20), rk, font=fr,
               fill=medal.get(rank, FG_MUTED))

        d.text((pad + 96, y + 16),
               truncate(d, r.get("nickname") or "", font(22, True), 210),
               font=font(22, True), fill=FG)
        d.text((pad + 96, y + 50),
               truncate(d, f"UID {r.get('uid') or ''}", font(14), 210),
               font=font(14), fill=FG_MUTED)

        d.text((pad + 330, y + 28), f"{r.get('char_count') or 0}", font=font(22, True), fill=FG)
        d.text((pad + 470, y + 28), f"{r.get('five_star') or 0}", font=font(22, True),
               fill=star_color(5))

        avg = r.get("level_avg")
        d.text((pad + 620, y + 28), fmt_num(avg, 1) if avg else "-", font=font(22, True), fill=FG)

        # 收集度进度条
        total = int(r.get("total") or 0)
        ratio = total / max_total if max_total else 0
        bx = width - pad - 190
        draw_bar(d, bx, y + 34, 120, 12, ratio, ACCENT, LINE)
        d.text((bx + 128, y + 26), str(total), font=font(21, True), fill=ACCENT_DEEP)

        y += row_h

    draw_project_footer(d, width, H - 46, extra="按展示角色数量与稀有度综合排序")
    return save_webp(img, 88)


# ---------------------------------------------------------------------------
# 5. 养成周报（对比两次快照）
# ---------------------------------------------------------------------------


def render_weekly_report(
    info: dict,
    *,
    width: int = 1100,
) -> bytes:
    """养成周报：与上次快照对比的变化。

    info: {
        "game_cn", "nickname", "uid", "days": 间隔天数,
        "changes": [(项目, 旧值, 新值, 变化文本, 是否向好), ...],
        "new_chars": [角色名, ...],
        "summary": 一句话总结,
    }
    """
    pad = 30
    changes = info.get("changes") or []
    new_chars = info.get("new_chars") or []
    H = 200 + 60 + len(changes) * 66 + (60 + len(new_chars) * 36 if new_chars else 0)
    H += 150 + 40

    img, d = new_image(width, H, BG)

    d.rectangle((0, 0, width, 150), fill=BG_CARD)
    d.line((0, 150, width, 150), fill=ACCENT, width=4)
    d.text((pad, 24), "养成周报", font=font(40, True), fill=FG)
    days = info.get("days") or 0
    d.text((pad, 82),
           f"{info.get('nickname') or ''} · {info.get('game_cn') or ''} · 最近 {days} 天",
           font=font(18), fill=FG_DIM)

    y = 172
    W = width - pad * 2

    y = draw_section(d, pad, y, W, "变化明细", font(20, True))
    for name, old, new, delta, better in changes:
        d.rectangle((pad, y, width - pad, y + 56), fill=BG_CARD)
        d.text((pad + 16, y + 14), str(name), font=font(18), fill=FG_DIM)
        # 旧值 -> 新值
        mid = f"{old} → {new}"
        d.text((pad + 220, y + 14), mid, font=font(18), fill=FG)
        # 变化量
        dc = GOOD if better else BAD
        vtxt = str(delta)
        vw = text_w(d, vtxt, font(20, True))
        d.text((width - pad - vw - 16, y + 12), vtxt, font=font(20, True), fill=dc)
        y += 66

    if new_chars:
        y = draw_section(d, pad, y, W, "新获得角色", font(20, True))
        x = pad
        for c in new_chars:
            tw = text_w(d, str(c), font(16))
            if x + tw + 24 > width - pad:
                x = pad
                y += 42
            rounded(d, (x, y, x + tw + 24, y + 34), 8, mix_color(BG_CARD, ACCENT, 0.16))
            d.text((x + 12, y + 7), str(c), font=font(16), fill=ACCENT_DEEP)
            x += tw + 34
        y += 46

    summary = info.get("summary") or ""
    if summary:
        rounded(d, (pad, y + 10, width - pad, y + 110), 12,
                mix_color(BG_CARD, ACCENT, 0.10), outline=ACCENT, width=2)
        d.text((pad + 22, y + 26), "小结", font=font(18, True), fill=ACCENT_DEEP)
        for i, line in enumerate(wrap_text(d, summary, font(17), W - 60)[:2]):
            d.text((pad + 22, y + 54 + i * 26), line, font=font(17), fill=FG)

    draw_project_footer(d, width, H - 42, extra="与上次快照对比 · 数据来源 enka.network")
    return save_webp(img, 88)


# ---------------------------------------------------------------------------
# 6. 对比图（角色 vs 角色 / 群友 vs 群友）
# ---------------------------------------------------------------------------


def render_compare(
    info: dict,
    *,
    width: int = 1100,
) -> bytes:
    """左右对比图（布局：左名 | 中部居中标签 | 右名，三列不重叠）。

    info: {
        "title": 标题,
        "left": {"name", "sub", "score"},
        "right": {"name", "sub", "score"},
        "rows": [(标签, 左值, 右值, 谁更好 -1/0/1), ...],
        "verdict": 结论文本,
    }
    """
    pad = 30
    left = info.get("left") or {}
    right = info.get("right") or {}
    rows = info.get("rows") or []

    row_h = 56
    H = 130 + 120 + 70 + len(rows) * row_h + 130 + 60

    img, d = new_image(width, H, BG)

    # ---- 标题 ----
    d.rectangle((0, 0, width, 108), fill=BG_CARD)
    d.line((0, 108, width, 108), fill=ACCENT, width=4)
    t = info.get("title") or "对比"
    tw = text_w(d, t, font(38, True))
    d.text(((width - tw) // 2, 28), t, font=font(38, True), fill=FG)

    # ---- 双方标题（左 / 右两栏，中线留空给标签） ----
    y = 132
    col_l_x = pad
    col_r_x = width // 2 + 110
    col_w = width // 2 - 140

    d.text((col_l_x, y), truncate(d, left.get("name") or "-", font(30, True), col_w),
           font=font(30, True), fill=ACCENT_DEEP)
    d.text((col_r_x, y), truncate(d, right.get("name") or "-", font(30, True), col_w),
           font=font(30, True), fill=ACCENT_DEEP)
    y += 44
    d.text((col_l_x, y), truncate(d, left.get("sub") or "", font(16), col_w),
           font=font(16), fill=FG_MUTED)
    d.text((col_r_x, y), truncate(d, right.get("sub") or "", font(16), col_w),
           font=font(16), fill=FG_MUTED)
    y += 44

    # ---- 综合分 ----
    lsc = float(left.get("score") or 0)
    rsc = float(right.get("score") or 0)
    lt = f"综合 {lsc:,.1f}"
    rt = f"综合 {rsc:,.1f}"
    d.text((col_l_x, y), lt, font=font(26, True),
           fill=ACCENT_DEEP if lsc >= rsc else FG_DIM)
    d.text((col_r_x, y), rt, font=font(26, True),
           fill=ACCENT_DEEP if rsc >= lsc else FG_DIM)
    y += 60

    # ---- 表头 ----
    d.rectangle((pad, y, width - pad, y + 44), fill=BG_CARD2)
    d.text((col_l_x + 8, y + 12), "数值", font=font(16, True), fill=FG_DIM)
    mid = "属性"
    mw = text_w(d, mid, font(16, True))
    d.text(((width - mw) // 2, y + 12), mid, font=font(16, True), fill=FG_DIM)
    d.text((col_r_x + 8, y + 12), "数值", font=font(16, True), fill=FG_DIM)
    y += 44

    # ---- 逐行 ----
    f_val = font(22, True)
    f_lab = font(16)
    for i, (label, lval, rval, better) in enumerate(rows):
        if i % 2 == 0:
            d.rectangle((pad, y, width - pad, y + row_h - 4), fill=BG_CARD)
        d.line((pad, y + row_h - 4, width - pad, y + row_h - 4), fill=LINE, width=1)

        # 左值
        ltxt = str(lval)
        d.text((col_l_x + 8, y + 12), ltxt, font=f_val,
               fill=GOOD if better == -1 else FG)

        # 中间标签（真正居中）
        nw = text_w(d, str(label), f_lab)
        d.text(((width - nw) // 2, y + 16), str(label), font=f_lab, fill=FG_MUTED)

        # 右值
        rtxt = str(rval)
        d.text((col_r_x + 8, y + 12), rtxt, font=f_val,
               fill=GOOD if better == 1 else FG)

        # 胜负标记：在胜出一方的数值后面加一个上箭头
        if better == -1:
            lw2 = text_w(d, ltxt, f_val)
            d.text((col_l_x + 8 + lw2 + 8, y + 14), "↑", font=font(16, True), fill=GOOD)
        elif better == 1:
            rw2 = text_w(d, rtxt, f_val)
            d.text((col_r_x + 8 + rw2 + 8, y + 14), "↑", font=font(16, True), fill=GOOD)

        y += row_h

    y += 14

    # ---- 结论 ----
    verdict = info.get("verdict") or ""
    if verdict:
        rounded(d, (pad, y, width - pad, y + 86), 12,
                mix_color(BG_CARD, ACCENT, 0.10), outline=ACCENT, width=2)
        d.text((pad + 22, y + 16), "结论", font=font(18, True), fill=ACCENT_DEEP)
        vt = truncate(d, verdict, font(18), width - pad * 2 - 60)
        d.text((pad + 22, y + 46), vt, font=font(18), fill=FG)

    draw_project_footer(d, width, H - 40, extra="数据来源 enka.network")
    return save_webp(img, 88)


# ---------------------------------------------------------------------------
# 8. 动态图（GIF 帧序列）
# ---------------------------------------------------------------------------


def save_gif(frames: list, *, duration: int = 60, max_size: int = 640) -> bytes:
    """把多张 PIL 图合成 GIF（等比例缩小到 max_size 宽以控制体积）。

    frames: [PIL.Image, ...]
    duration: 每帧毫秒
    返回 GIF 字节。
    """
    import io as _io

    if not frames:
        raise ValueError("frames 为空")

    out = []
    for f in frames:
        im = f.convert("RGB")
        if im.width > max_size:
            ratio = max_size / im.width
            im = im.resize((max_size, max(1, int(im.height * ratio))), Image.LANCZOS)
        # GIF 需要调色板
        out.append(im.convert("P", palette=Image.ADAPTIVE, colors=96))

    buf = _io.BytesIO()
    out[0].save(
        buf,
        "GIF",
        save_all=True,
        append_images=out[1:],
        duration=max(20, int(duration)),
        loop=0,
        optimize=True,
    )
    return buf.getvalue()


def make_scroll_frames(
    img,
    *,
    frame_h: int = 700,
    step: int = 220,
) -> list:
    """把一张长图切成滚动动画帧（模拟向下滚动）。"""
    frames = []
    w, h = img.size
    if h <= frame_h:
        return [img]

    y = 0
    while y + frame_h <= h:
        frames.append(img.crop((0, y, w, y + frame_h)))
        y += step
    # 末帧贴底
    if frames and y - step + frame_h < h:
        frames.append(img.crop((0, h - frame_h, w, h)))
    return frames


__all__ = [
    # 错误
    "PanelError",
    # store
    "STORE_BASE",
    "STORE_TTL",
    "STORE_FILES",
    "GAME_FILES",
    "STORE_ALIASES",
    "GAME_ALIASES",
    "LocalStore",
    "normalize_game",
    "normalize_store_name",
    "store_url",
    # 颜色
    "BG",
    "BG_CARD",
    "BG_CARD2",
    "FG",
    "FG_DIM",
    "FG_MUTED",
    "ACCENT",
    "LINE",
    "GOOD",
    "BAD",
    "STAR_COLOR",
    "GI_ELEM",
    "GI_ELEM_CN",
    "HSR_ELEM",
    "HSR_ELEM_CN",
    "HSR_PATH",
    "ZZZ_ELEM",
    "ZZZ_ELEM_CN",
    "ZZZ_PROF_CN",
    "ELEM_COLOR",
    "ELEM_CN",
    "star_color",
    "hex_to_rgb",
    "mix_color",
    "darken",
    "brighten",
    "elem_color",
    "elem_cn",
    "path_cn",
    "profession_cn",
    # 字体 / 绘图
    "FONT_CANDIDATES",
    "font",
    "new_image",
    "text_w",
    "text_h",
    "rounded",
    "draw_bar",
    "draw_tag",
    "draw_star",
    "draw_stars",
    "truncate",
    "wrap_text",
    "draw_section",
    "draw_kv",
    "save_webp",
    "draw_project_footer",
    "PROJECT_URL",
    "PROJECT_FOOTER",
    # 圣遗物评分
    "artifact_score",
    "score_grade",
    "score_color",
    "score_benchmark",
    "substat_score",
    "SUBSTAT_SCORE_RATE",
    "SCORE_EXCELLENT",
    "SCORE_GOD",
    "ZZZ_MAX_PER_PIECE",
    "render_artifact_grid",
    "render_rank_board",
    "render_damage_card",
    "SKILL_MULTIPLIER",
    "skill_multiplier",
    "render_record_card",
    "render_collection_board",
    "render_weekly_report",
    "render_compare",
    "save_gif",
    "make_scroll_frames",
    # 格式化
    "fmt_int",
    "fmt_pct",
    "fmt_pct_raw",
    "fmt_num",
    "fmt_signed",
    "fmt_score",
    "fmt_duration",
    "ratio_of",
]
