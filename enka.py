"""
统一三游戏 API 客户端：原神 / 崩铁 / 绝区零（数据源 enka.network）。

端点（已实测）
--------------
  原神   https://enka.network/api/uid/<uid>
         -> {"playerInfo": {...}, "avatarInfoList": [...], "uid": ..., "ttl": 60}
  崩铁   https://enka.network/api/hsr/uid/<uid>
         -> {"detailInfo": {..., "avatarDetailList": [...]}, "uid": ...}
  绝区零 https://enka.network/api/zzz/uid/<uid>
         -> {"PlayerInfo": {"ShowcaseDetail": {"AvatarList": [...]},
                            "SocialDetail": {...}}, "uid": ...}

缓存策略（服务器带宽只有 8Mbps）
--------------------------------
  内存缓存 180s + 磁盘缓存 180s（{cache_dir}/api_{game}_{uid}.json）
  force=True 时两者都绕过，重新请求并重写磁盘缓存。

错误处理
--------
  统一抛 PanelError（来自 common.py），文案面向普通玩家。
  HTTP 400/404/424/429 有专门提示；网络错误 / 超时自动重试 3 次，
  退避 1.5s / 3s / 4.5s。

对外接口（main.py / gi.py / hsr.py / zzz.py 会调用）
--------------------------------------------------
  ENKA_ENDPOINTS           端点模板 dict
  class EnkaClient(cache_dir)
      .fetch(session, game, uid, force=False) -> raw dict
      .probe(session, uid) -> (game, raw) | None
      .fetch_info(session, game, uid) -> {"nickname","level","extra",...}
      .fetch_player(session, uid)  -> 兼容旧版（自动探测后取 raw）
      .fetch_player_info(uid)      -> 兼容旧版（自动建 session，返回 fetch_info 结构）
      .detect_game(session, uid)   -> 只探测游戏名
      .clear_cache(game=None, uid=None)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

try:
    import aiohttp
except Exception:  # pragma: no cover - 容器里一定有，本地语法检查可能没有
    aiohttp = None  # type: ignore[assignment]

try:
    from .common import PanelError
except Exception:  # pragma: no cover - 支持被当作顶层模块直接 import
    try:
        from common import PanelError  # type: ignore[no-redef]
    except Exception:

        class PanelError(Exception):  # type: ignore[no-redef]
            """给用户看的友好错误（common.py 不可用时的兜底实现）。"""

            def __init__(self, message: str, hint: str = "", *, code: int = 0):
                self.message = str(message or "")
                self.hint = str(hint or "")
                self.code = int(code or 0)
                super().__init__(self.message)

            def __str__(self) -> str:
                return f"{self.message}\n{self.hint}" if self.hint else self.message


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ENKA_BASE = "https://enka.network/api"

# 端点模板：{uid} 会被替换
ENKA_ENDPOINTS: dict[str, str] = {
    "gi": f"{ENKA_BASE}/uid/{{uid}}",
    "hsr": f"{ENKA_BASE}/hsr/uid/{{uid}}",
    "zzz": f"{ENKA_BASE}/zzz/uid/{{uid}}",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

API_TTL = 180  # 秒：enka 自己的 ttl 是 60，我们放宽到 180 减少请求
RETRIES = 3
BACKOFF = (1.5, 3.0, 4.5)  # 每次重试前的等待
TIMEOUT_TOTAL = 45.0

GAME_NAMES = {"gi": "原神", "hsr": "崩铁", "zzz": "绝区零"}
GAME_ALIASES = {
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

# 「没公开数据」时的提示，按游戏区分要用户去开什么开关
_PRIVACY_HINT = {
    "gi": (
        "请在游戏内打开「显示角色详情」：\n"
        "• 米游社 App → 我的角色 → 打开「显示角色详情」\n"
        "• 或游戏内 → 设置 → 账号 → 显示角色详情"
    ),
    "hsr": (
        "请在游戏内打开「展示角色详情」：\n"
        "• 游戏 → 设置 → 其他 → 打开「在主页展示角色详情」\n"
        "• 或米游社 App → 我的角色 → 打开「显示角色详情」"
    ),
    "zzz": (
        "请在游戏内打开「展示柜 / 角色详情」：\n"
        "• 游戏 → 设置 → 其他 → 打开「允许他人查看角色详情」\n"
        "• 或米游社 App → 我的角色 → 打开「显示角色详情」"
    ),
}


def normalize_game(game: str) -> str:
    """把 '原神' / 'StarRail' / 'hsr' 之类都归一化成 'gi'|'hsr'|'zzz'。"""
    s = str(game or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if s in GAME_ALIASES:
        return GAME_ALIASES[s]
    if s.startswith("gi"):
        return "gi"
    if s.startswith("hsr"):
        return "hsr"
    if s.startswith("zzz"):
        return "zzz"
    return ""


def endpoint_of(game: str, uid: str) -> str:
    """取某游戏某 UID 的完整 URL。"""
    g = normalize_game(game)
    if g not in ENKA_ENDPOINTS:
        raise PanelError(f"不支持的游戏：{game}", hint="可用的有：gi / hsr / zzz")
    return ENKA_ENDPOINTS[g].format(uid=str(uid))


def _game_name(game: str) -> str:
    return GAME_NAMES.get(normalize_game(game), str(game))


def _uid_digits(uid: Any) -> str:
    """从用户输入里抽出 UID 数字串（'UID: 101000001' -> '101000001'）。"""
    s = str(uid or "").strip()
    m = re.search(r"(\d{8,12})", s)
    return m.group(1) if m else re.sub(r"\D", "", s)


def _looks_ok(uid: str) -> bool:
    return bool(re.fullmatch(r"\d{8,10}", uid or ""))


# ---------------------------------------------------------------------------
# 响应结构判定
# ---------------------------------------------------------------------------


def has_gi_data(raw: Any) -> bool:
    """原神：有 avatarInfoList 或 playerInfo 就算有数据。"""
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("avatarInfoList")) or bool(raw.get("playerInfo"))


def has_hsr_data(raw: Any) -> bool:
    """崩铁：detailInfo 存在就算有数据。"""
    return isinstance(raw, dict) and isinstance(raw.get("detailInfo"), dict)


def has_zzz_data(raw: Any) -> bool:
    """绝区零：PlayerInfo 存在就算有数据。"""
    return isinstance(raw, dict) and isinstance(raw.get("PlayerInfo"), dict)


HAS_DATA_FN = {"gi": has_gi_data, "hsr": has_hsr_data, "zzz": has_zzz_data}


def has_data(game: str, raw: Any) -> bool:
    fn = HAS_DATA_FN.get(normalize_game(game))
    return bool(fn(raw)) if fn else False


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class EnkaClient:
    """统一三游戏 API 客户端（带内存 + 磁盘缓存）。"""

    def __init__(self, cache_dir: str = ""):
        self.cache_dir = str(cache_dir or "")
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
            except Exception:
                pass
        # key = f"{game}:{uid}" -> (写入时间, raw)
        self._mem: dict[str, tuple[float, dict]] = {}
        self.ttl = API_TTL
        self.retries = RETRIES
        self.timeout = TIMEOUT_TOTAL
        self.last_request_at: dict[str, float] = {}

    # ---------- 缓存 ----------

    @staticmethod
    def _key(game: str, uid: str) -> str:
        return f"{normalize_game(game)}:{uid}"

    def cache_path(self, game: str, uid: str) -> str:
        g = normalize_game(game) or "gi"
        safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(uid))
        return os.path.join(self.cache_dir or ".", f"api_{g}_{safe}.json")

    def _read_disk(self, game: str, uid: str) -> dict | None:
        p = self.cache_path(game, uid)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_disk(self, game: str, uid: str, data: dict) -> None:
        if not self.cache_dir:
            return
        p = self.cache_path(game, uid)
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _disk_fresh(self, game: str, uid: str) -> bool:
        p = self.cache_path(game, uid)
        try:
            return os.path.exists(p) and (time.time() - os.path.getmtime(p) < self.ttl)
        except Exception:
            return False

    def clear_cache(self, game: str | None = None, uid: str | None = None) -> int:
        """清缓存（/更新面板 用）。返回清掉的条目数。

        clear_cache()                    -> 全清
        clear_cache(uid="101000001")     -> 清该 UID 所有游戏
        clear_cache("gi", "101000001")   -> 清指定一条
        """
        removed = 0
        g = normalize_game(game) if game else ""
        u = _uid_digits(uid) or str(uid or "")
        for key in list(self._mem):
            kg, ku = key.split(":", 1)
            if (not g or kg == g) and (not u or ku == u):
                self._mem.pop(key, None)
                removed += 1
        if self.cache_dir and os.path.isdir(self.cache_dir):
            try:
                for fn in os.listdir(self.cache_dir):
                    if not (fn.startswith("api_") and fn.endswith(".json")):
                        continue
                    m = re.match(r"api_([a-z]+)_(.+)\.json$", fn)
                    if not m:
                        continue
                    fg, fu = m.group(1), m.group(2)
                    if (not g or fg == g) and (not u or fu == u):
                        try:
                            os.remove(os.path.join(self.cache_dir, fn))
                            removed += 1
                        except Exception:
                            pass
            except Exception:
                pass
        return removed

    # ---------- HTTP ----------

    def _raise_for_status(self, status: int, game: str, uid: str) -> None:
        """把 enka 的 HTTP 状态码翻译成友好中文。"""
        gname = _game_name(game)
        if status == 400:
            raise PanelError(
                "UID 格式不正确",
                hint=f"{gname} UID 应为 8~10 位纯数字，例如 101000001",
                code=400,
            )
        if status == 404:
            raise PanelError(
                f"查不到 UID {uid} 的{gname}数据",
                hint=_PRIVACY_HINT.get(normalize_game(game), _PRIVACY_HINT["gi"]),
                code=404,
            )
        if status == 424:
            raise PanelError(
                "enka 正在维护中",
                hint="通常是游戏刚更新版本，等几小时再试～",
                code=424,
            )
        if status == 429:
            raise PanelError(
                "请求太频繁，被 enka 限流了",
                hint="等 1 分钟再试，别连续刷",
                code=429,
            )
        if status in (500, 502, 503, 504):
            # 5xx 是 enka 侧临时故障，可以重试（标记 retryable，供 _request 判断）
            err = PanelError(
                f"enka 服务器暂时不可用（HTTP {status}）",
                hint="过几分钟再试～",
                code=status,
            )
            setattr(err, "retryable", True)
            raise err
        raise PanelError(f"enka 返回了异常状态 HTTP {status}", code=status)

    async def _request_once(self, session, url: str, game: str, uid: str) -> dict:
        """发一次请求；HTTP 错误直接抛 PanelError，网络错误抛原始异常。"""
        timeout = None
        if aiohttp is not None:
            timeout = aiohttp.ClientTimeout(total=self.timeout, connect=15, sock_read=self.timeout)
        kwargs: dict[str, Any] = {"headers": HEADERS}
        if timeout is not None:
            kwargs["timeout"] = timeout
        async with session.get(url, **kwargs) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            if status != 200:
                # 先把 body 读掉，避免连接泄漏
                try:
                    await resp.read()
                except Exception:
                    pass
                self._raise_for_status(status, game, uid)
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = ""
                try:
                    text = await resp.text()
                except Exception:
                    pass
                raise PanelError(
                    "enka 返回的数据无法解析",
                    hint=f"可能是临时故障，稍后再试（{text[:80]}）"
                    if text
                    else "可能是临时故障，稍后再试",
                )
            # ⚠ 关键：enka 上游故障时会返回 HTTP 200 + 错误对象，例如
            #   {"code":"upstream_fail","message":"Fetching player data failed."}
            #   {"code":"maintenance","message":"The game servers are under maintenance."}
            # 若不识别，会被当成有效玩家数据 → 用户看到「开拓者 Lv0」假信息，
            # 而且假数据还会写入 180s 磁盘缓存（静默错误，比报错危害更大）。
            err_code, err_msg = self._detect_error_object(data)
            if err_code:
                raise self._error_object_exception(err_code, err_msg, game)

            return data if isinstance(data, dict) else {}

    # ---------- 上游错误对象识别 ----------

    # enka 的「HTTP 200 + 错误对象」形态
    _ERR_CODES = {
        "upstream_fail": (502, "enka 拉取游戏数据失败（上游故障）", True),
        "maintenance": (424, "游戏服务器正在维护中", False),
        "rate_limited": (429, "enka 限流了", False),
        "player_hidden": (404, "该玩家未公开数据", False),
    }

    # 正常响应的顶层标识字段：只要命中任一，就绝不当成错误对象
    _OK_KEYS = ("playerInfo", "avatarInfoList", "detailInfo", "PlayerInfo")

    @classmethod
    def _detect_error_object(cls, data: Any) -> tuple[str, str]:
        """判断响应是否是上游错误对象，返回 (code, message)；不是则返回 ("", "")"""
        if not isinstance(data, dict):
            return "", ""

        # 防御误拦：有正常数据字段就一定是有效响应
        for k in cls._OK_KEYS:
            if k in data:
                return "", ""

        code = str(data.get("code") or "").strip().lower()
        if not code:
            return "", ""

        msg = str(data.get("message") or data.get("msg") or "")
        return code, msg

    @classmethod
    def _error_object_exception(cls, code: str, msg: str, game: str) -> PanelError:
        gname = _game_name(game)
        status, text, retryable = cls._ERR_CODES.get(
            code, (502, f"{gname} 数据拉取失败（{code}）", True)
        )
        hint = msg or "这是 enka 上游的问题，稍后重试即可"
        if code == "maintenance":
            hint = "游戏刚更新版本时常见，等几小时再试"
        err = PanelError(text, hint=hint, code=status)
        try:
            err.retryable = retryable
        except Exception:
            pass
        return err

    async def _request(self, session, game: str, uid: str) -> dict:
        """带重试的请求：网络错误 / 超时 / 5xx 重试 3 次（1.5s/3s/4.5s）。

        注意：400/404/424/429 是确定的业务结果，重试没有意义，直接抛出；
        但 500/502/503/504 是 enka 侧临时故障（实测经常出现），要重试。
        """
        url = endpoint_of(game, uid)
        last_err: Exception | None = None
        for attempt in range(max(1, self.retries)):
            try:
                data = await self._request_once(session, url, game, uid)
                self.last_request_at[self._key(game, uid)] = time.time()
                return data
            except PanelError as e:
                # 只有明确标记 retryable 的（5xx）才重试，其余业务错误立即抛
                if not getattr(e, "retryable", False):
                    raise
                last_err = e
                if attempt < self.retries - 1:
                    await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
            except Exception as e:
                last_err = e
                if attempt < self.retries - 1:
                    await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        if isinstance(last_err, PanelError):
            raise last_err  # 5xx 重试用尽：抛出扣好的友好文案
        gname = _game_name(game)
        if isinstance(last_err, asyncio.TimeoutError):
            raise PanelError(
                f"连接 enka 超时（{gname}）",
                hint="enka 在国外，偶尔会慢，稍后再试～",
            )
        raise PanelError(
            f"网络错误，拉取{gname}数据失败",
            hint=f"{type(last_err).__name__}: {last_err}" if last_err else "请稍后再试",
        )

    # ---------- 对外：拉取原始数据 ----------

    async def fetch(self, session, game: str, uid: str, force: bool = False) -> dict:
        """拉取某游戏某 UID 的原始 JSON（内存/磁盘缓存 180s）。

        force=True 时绕过内存和磁盘缓存，强制重新请求并重写磁盘缓存。
        """
        g = normalize_game(game)
        if not g:
            raise PanelError(f"不支持的游戏：{game}", hint="可用的有：gi / hsr / zzz")
        u = _uid_digits(uid)
        if not u:
            raise PanelError("UID 不能为空", hint="用法示例：/原神面板 101000001")
        if not _looks_ok(u):
            raise PanelError(
                f"UID {u} 格式不正确",
                hint=f"{_game_name(g)} UID 应为 8~10 位纯数字",
            )

        key = self._key(g, u)

        if not force:
            hit = self._mem.get(key)
            if hit and (time.time() - hit[0] < self.ttl):
                return hit[1]
            if self._disk_fresh(g, u):
                data = self._read_disk(g, u)
                if isinstance(data, dict) and data:
                    self._mem[key] = (time.time(), data)
                    return data
        else:
            self._mem.pop(key, None)

        data = await self._request(session, g, u)
        # 带 playerInfo 但 avatarInfoList 为空（没开角色详情）也算有效数据，
        # 由上层决定是「显示玩家信息」还是「提示开开关」，这里不丢。
        self._mem[key] = (time.time(), data)
        self._write_disk(g, u, data)
        return data

    # ---------- 对外：自动探测 ----------

    async def probe(self, session, uid: str) -> tuple[str, dict] | None:
        """自动探测 UID 属于哪个游戏，返回 (game, raw) 或 None。

        规则（按 UID 位数剪枝）：
          • 8 位 UID   → **只探绝区零**
          • 9~10 位    → 依次探原神、崩铁、绝区零
        为什么这样剪枝（实测数据）：
          enka 对「不属于该游戏的 UID」返回 400 wrong_uid，但**要等 13~15 秒**
          才吐出来（8位→hsr 实测稳定 14s）。8 位 UID 不可能是原神/崩铁
          （两者都是 9 位），去试纯属白等 30 秒。
          注意反例：zzz 对 9 位 UID 返回的是 maintenance 而**不是** wrong_uid，
          所以「9 位 → 不是 zzz」无法证明，9 位场景不能剪枝。

        探测期间：retries 降为 1、超时压到 20s（探测是「试探」，
        宁可放弃也不让用户干等）；force=True 避免污染缓存。
        400/404 视为「不是这个游戏」继续试下一个；424/429/网络错误记录后继续试。
        """
        u = _uid_digits(uid)
        if not u or not _looks_ok(u):
            return None

        # 剪枝：原神/崩铁 UID 都是 9 位，绝区零是 8 位（实测 enka 对错误游戏返回
        # 400 wrong_uid）。8 位只探绝区零，避免无意义请求。
        if len(u) <= 8:
            order = ["zzz"]
        else:
            order = ["gi", "hsr", "zzz"]

        errors: list[PanelError] = []

        # 探测期间把重试降为 1：探测语义是「快速判断属于哪个游戏」，
        # 某个游戏暂时不可用就跳过。否则 3 游戏 × 3 次重试 × 退避，
        # 上游全故障时会卡 13~17 秒，用户以为机器人死了。
        saved_retries = self.retries
        saved_timeout = self.timeout
        self.retries = 1
        # 探测用短超时：实测 zzz 维护时 424 要 12.8s、gi 正常也要 15.9s。
        # 探测是「试探」，宁可放弃也不要让用户干等。
        try:
            self.timeout = min(float(self.timeout or 20), 20.0)
        except Exception:
            pass
        try:
            for g in order:
                try:
                    raw = await self.fetch(session, g, u, force=True)
                except PanelError as e:
                    if e.code in (400, 404):
                        continue  # 该 UID 不属于这个游戏
                    errors.append(e)
                    continue
                except Exception:
                    continue
                if has_data(g, raw):
                    return (g, raw)
        finally:
            self.retries = saved_retries
            self.timeout = saved_timeout

        # 全都没探到：如果有人给出「维护/限流/网络」这类可重试错误，优先报它
        # （比笼统的「查不到」更有信息量）
        if errors:
            # 相同文案去重，避免把 3 条一样的错误堆给用户
            seen: set[str] = set()
            uniq = []
            for e in errors:
                if str(e) not in seen:
                    seen.add(str(e))
                    uniq.append(e)
            if len(uniq) == 1:
                raise uniq[0]
            raise PanelError(
                "暂时查不到这个 UID",
                hint=f"{uniq[0].message}\n再等一会儿，或直接指定游戏绑定",
            )
        return None

    async def detect_game(self, session, uid: str) -> str | None:
        """只探测游戏名，不返回数据（绑定流程用）。"""
        hit = await self.probe(session, uid)
        return hit[0] if hit else None

    # ---------- 对外：轻量玩家信息（绑定验证用） ----------

    async def fetch_info(self, session, game: str, uid: str) -> dict:
        """取统一结构的玩家信息，用于绑定验证。

        返回 {"nickname": str, "level": int, "extra": str,
              "game": str, "uid": str, "has_data": bool, "raw": dict}
        """
        g = normalize_game(game)
        raw = await self.fetch(session, g, uid)
        nick, level, extra, has = self._parse_info(g, raw, uid)
        return {
            "nickname": nick,
            "level": level,
            "extra": extra,
            "game": g,
            "game_name": _game_name(g),
            "uid": str(uid),
            "has_data": has,
            "raw": raw,
        }

    @staticmethod
    def _parse_info(game: str, raw: dict, uid: str) -> tuple[str, int, str, bool]:
        """从原始 JSON 里抽 (昵称, 等级, 附加说明, 是否有角色数据)。"""
        if game == "gi":
            pi = raw.get("playerInfo") or {}
            nick = str(pi.get("nickname") or "").strip()
            level = _as_int(pi.get("level"))
            wl = _as_int(pi.get("worldLevel"))
            extra = f"冒险等阶 {level} · 世界等级 {wl}"
            return (nick or "旅行者", level, extra, bool(raw.get("avatarInfoList")))

        if game == "hsr":
            di = raw.get("detailInfo") or {}
            nick = str(di.get("nickname") or "").strip()
            level = _as_int(di.get("level"))
            wl = _as_int(di.get("worldLevel"))
            extra = f"开拓等级 {level} · 均衡等级 {wl}"
            adl = di.get("avatarDetailList") or []
            return (nick or "开拓者", level, extra, bool(adl))

        if game == "zzz":
            pi = raw.get("PlayerInfo") or {}
            sd = pi.get("SocialDetail") or {}
            pd = sd.get("ProfileDetail") or {}
            nick = _clean_zzz_nick(pd.get("Nickname"))
            level = _as_int(pd.get("Level"))
            if not nick:
                # 昵称是纯不可见填充时兜底，避免发出去一串空白
                nick = f"绳匠{str(uid)[:4]}"
            extra = f"绳网等级 {level}"
            avatars = (pi.get("ShowcaseDetail") or {}).get("AvatarList") or []
            return (nick, level, extra, bool(avatars))

        return ("", 0, "", False)

    # ---------- 兼容旧接口 ----------

    async def fetch_player(self, session, uid: str) -> dict:
        """兼容旧版 enka.EnkaClient.fetch_player：自动探测游戏后返回原始 JSON。"""
        code = _uid_digits(uid)
        if not code:
            raise PanelError("UID 不能为空", hint="用法示例：/原神面板 101000001")
        hit = await self.probe(session, code)
        if not hit:
            raise PanelError(
                f"查不到 UID {code} 的数据",
                hint=(
                    "可能是 UID 输入错误，或者没在游戏内打开「显示角色详情」。\n"
                    f"也可以直接指定游戏：/绑定原神 {code}"
                ),
            )
        return hit[1]

    async def fetch_player_info(self, uid: str) -> dict:
        """兼容旧版：自己建 session，返回统一结构的玩家信息。

        旧版返回 {"nickname","level","world_level","achievement"}，
        这里在统一结构基础上额外带上旧字段，保证老调用方不炸。
        """
        code = _uid_digits(uid)
        if not code:
            raise PanelError("UID 不能为空", hint="用法示例：绑定uid 101000001")
        if aiohttp is None:  # pragma: no cover
            raise PanelError("缺少 aiohttp 依赖，无法联网")
        async with aiohttp.ClientSession() as session:
            hit = await self.probe(session, code)
            if not hit:
                raise PanelError(
                    f"查不到 UID {code} 的数据",
                    hint=(
                        "请确认 UID 正确，并在游戏内打开「显示角色详情 / 展示柜」。\n"
                        f"也可以直接指定游戏：/绑定原神 {code}"
                    ),
                )
            game, raw = hit
            nick, level, extra, has = self._parse_info(game, raw, code)

        info = {
            "nickname": nick,
            "level": level,
            "extra": extra,
            "game": game,
            "game_name": _game_name(game),
            "uid": code,
            "has_data": has,
        }
        # 旧字段兼容
        if game == "gi":
            pi = raw.get("playerInfo") or {}
            info["world_level"] = _as_int(pi.get("worldLevel"))
            info["achievement"] = _as_int(pi.get("finishAchievementNum"))
        elif game == "hsr":
            di = raw.get("detailInfo") or {}
            info["world_level"] = _as_int(di.get("worldLevel"))
            info["achievement"] = _as_int((di.get("recordInfo") or {}).get("achievementCount"))
        else:
            info["world_level"] = 0
            info["achievement"] = 0
        return info


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _clean_zzz_nick(v: Any) -> str:
    """绝区零昵称可能整串是 \\xa0 填充，必须 strip 掉不可见字符。"""
    if v is None:
        return ""
    s = str(v)
    # \xa0 不间断空格、\u3000 全角空格、零宽字符都要去掉
    s = s.replace("\xa0", " ").replace("\u3000", " ").replace("\u200b", "")
    s = re.sub(r"[\u200b-\u200f\u2028-\u202f\ufeff]", "", s)
    return s.strip()


__all__ = [
    "PanelError",
    "EnkaClient",
    "ENKA_ENDPOINTS",
    "ENKA_BASE",
    "API_TTL",
    "HEADERS",
    "GAME_NAMES",
    "normalize_game",
    "endpoint_of",
    "has_data",
    "has_gi_data",
    "has_hsr_data",
    "has_zzz_data",
]
