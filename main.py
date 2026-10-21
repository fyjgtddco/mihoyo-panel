"""
米游社面板插件 v2 - 三游戏统一指令层（纯 command，AI 不参与）

指令：
  /绑定原神 <UID>          绑定原神
  /绑定崩铁 <UID>          绑定崩铁
  /绑定绝区零 <UID>        绑定绝区零
  /绑定uid <UID>           自动探测归属游戏后绑定
  /原神面板 [角色名]       列表 / 角色详情
  /崩铁面板 [角色名]
  /绝区零面板 [角色名]
  /我的uid                 查看所有绑定
  /解绑uid [游戏]          解绑（不带参数=解绑全部）
  /更新面板 [游戏] [角色名] 强制刷新（重拉数据 + 新图）

设计：
  - 全部 @filter.command，LLM 完全不参与
  - 列表图 + 详情图两段式，解决"一片一片看不清"
  - /更新面板 强制重拉 + 图片文件名带时间戳，保证换图
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from . import common, enka, gi, hsr, zzz
except ImportError:
    import common  # type: ignore
    import enka  # type: ignore
    import gi  # type: ignore
    import hsr  # type: ignore
    import zzz  # type: ignore


GAMES = {
    "gi": {"name": "原神", "cmd": "原神面板"},
    "hsr": {"name": "崩铁", "cmd": "崩铁面板"},
    "zzz": {"name": "绝区零", "cmd": "绝区零面板"},
}

GAME_ALIASES = {
    "原神": "gi", "genshin": "gi", "gi": "gi", "原": "gi",
    "崩铁": "hsr", "星铁": "hsr", "崩坏星穹铁道": "hsr", "hsr": "hsr", "星穹铁道": "hsr",
    "绝区零": "zzz", "绝区": "zzz", "zzz": "zzz", "zenless": "zzz",
}


def parse_game(text: str) -> str:
    """从文本里识别游戏，返回 gi/hsr/zzz 或空串"""
    if not text:
        return ""
    t = text.strip().lower()
    for k, v in GAME_ALIASES.items():
        if k.lower() == t:
            return v
    for k, v in GAME_ALIASES.items():
        if k.lower() in t:
            return v
    return ""


@register(
    "miyoyo_panel",
    "dsh",
    "米游社面板：支持原神/崩铁/绝区零，绑定 UID 后发指令返回角色列表与单角色详情图（纯指令，不经过 AI）",
    "2.0.0",
)
class MiHoYoPanel(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        try:
            self.data_dir = str(StarTools.get_data_dir("miyoyo_panel"))
        except Exception:
            self.data_dir = "/AstrBot/data/plugin_data/miyoyo_panel"
        os.makedirs(self.data_dir, exist_ok=True)

        self.bind_file = os.path.join(self.data_dir, "bind.json")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        self.img_dir = os.path.join(self.data_dir, "img")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.img_dir, exist_ok=True)

        self.binds: dict[str, dict[str, str]] = self._load_binds()
        self.client = enka.EnkaClient(self.cache_dir)
        self.store = common.LocalStore(self.cache_dir)

        self.cache_ttl = int(self.config.get("cache_ttl", 180))
        self.cooldown = float(self.config.get("cooldown", 3))
        self.max_chars = int(self.config.get("max_characters", 12))
        self._lock = asyncio.Lock()
        self._last: dict[str, float] = {}
        self._rank_count = 0

        total = sum(len(v) for v in self.binds.values())
        logger.info(
            f"[miyoyo_panel] v2 已启用 | {len(self.binds)} 个用户 / {total} 个绑定"
        )

        # 启动后异步预热 store 映射表：避免首张图因缺表而显示兜底名
        self._warm_task = asyncio.create_task(self._warmup())

    async def _warmup(self) -> None:
        """后台预热三游戏 store（缺表时首图会大面积显示兜底名）"""
        try:
            import aiohttp

            await asyncio.sleep(5)  # 让 AstrBot 先完成启动
            async with aiohttp.ClientSession() as session:
                for game in ("gi", "hsr", "zzz"):
                    try:
                        # ensure_missing 只下载缺失的表，已有则秒返回
                        t = time.time()
                        res = await self.store.ensure_missing(session, game)
                        got = sum(1 for v in (res or {}).values() if v)
                        if got or (res and len(res) > 0):
                            logger.info(
                                f"[miyoyo_panel] 预热 {game}: "
                                f"{got}/{len(res or {})} 下载完成，{time.time()-t:.1f}s"
                            )
                    except Exception as e:
                        logger.warning(f"[miyoyo_panel] 预热 {game} 失败: {e}")
                logger.info("[miyoyo_panel] store 预热结束")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[miyoyo_panel] 预热任务异常: {e}")

    # ---------- 绑定存储 ----------

    def _load_binds(self) -> dict:
        if os.path.exists(self.bind_file):
            try:
                with open(self.bind_file, encoding="utf-8") as f:
                    d = json.load(f)
                if d and all(isinstance(v, dict) and "uid" in v for v in d.values()):
                    out: dict[str, dict[str, str]] = {}
                    for qq, v in d.items():
                        out[qq] = {"gi": str(v["uid"])}
                    return out
                return d
            except Exception:
                pass
        return {}

    def _save_binds(self) -> None:
        try:
            with open(self.bind_file, "w", encoding="utf-8") as f:
                json.dump(self.binds, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[miyoyo_panel] 保存绑定失败: {e}")

    def _get_uid(self, sender: str, game: str) -> str:
        return (self.binds.get(sender) or {}).get(game, "")

    def _set_uid(self, sender: str, game: str, uid: str) -> None:
        self.binds.setdefault(sender, {})[game] = uid
        self._save_binds()

    # ---------- 绑定指令 ----------

    async def _do_bind(self, event: AstrMessageEvent, game: str, uid_text: str):
        uid = re.sub(r"\D", "", uid_text or "")
        if not uid:
            m = re.search(r"(\d{7,10})", event.message_str or "")
            if m:
                uid = m.group(1)

        if not uid:
            yield event.plain_result(
                "用法：/绑定原神 101000001\n"
                "　　　/绑定崩铁 101000001\n"
                "　　　/绑定绝区零 10153059\n"
                "或者 /绑定uid 你的UID（自动识别游戏）"
            )
            return

        gname = GAMES[game]["name"] if game else ""
        yield event.plain_result(
            f"正在验证 {gname + ' ' if gname else ''}UID {uid} …"
        )

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                if game:
                    # 已知游戏：直接验证，省 1~2 次网络请求
                    info = await self.client.fetch_info(session, game, uid)
                else:
                    # 真不知道游戏时才探测
                    hit = await self.client.probe(session, uid)
                    if not hit:
                        yield event.plain_result(
                            f"UID {uid} 在三个游戏里都查不到数据。\n"
                            "请检查：\n"
                            "• UID 是否正确\n"
                            "• 游戏内是否打开了「显示角色详情 / 展示柜」"
                        )
                        return
                    game, raw = hit
                    gname = GAMES[game]["name"]
                    # 探测已拿到数据，复用避免再请求一次
                    try:
                        nick, lv, extra, has = self.client._parse_info(game, raw, uid)
                        info = {"nickname": nick, "level": lv, "extra": extra,
                                "has_data": has, "game": game, "uid": uid}
                    except Exception:
                        info = await self.client.fetch_info(session, game, uid)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 验证失败: {e}", exc_info=True)
            yield event.plain_result(f"验证失败：{e}")
            return

        sender = str(event.get_sender_id())
        self._set_uid(sender, game, uid)
        cmd = GAMES[game]["cmd"]

        yield event.plain_result(
            f"绑定成功！\n"
            f"游戏：{gname}\n"
            f"UID：{uid}\n"
            f"昵称：{info.get('nickname') or '未知'}\n"
            f"等级：{info.get('level')}　{info.get('extra', '')}\n\n"
            f"发 /{cmd} 查看角色列表～\n"
            f"发 /{cmd} 角色名 查看单个角色详情"
        )

    @filter.command("绑定原神")
    async def bind_gi(self, event: AstrMessageEvent, uid: str = ""):
        async for r in self._do_bind(event, "gi", uid):
            yield r

    @filter.command("绑定崩铁")
    async def bind_hsr(self, event: AstrMessageEvent, uid: str = ""):
        async for r in self._do_bind(event, "hsr", uid):
            yield r

    @filter.command("绑定绝区零")
    async def bind_zzz(self, event: AstrMessageEvent, uid: str = ""):
        async for r in self._do_bind(event, "zzz", uid):
            yield r

    @filter.command("绑定uid")
    async def bind_auto(self, event: AstrMessageEvent, arg: str = ""):
        game = parse_game(arg)
        async for r in self._do_bind(event, game, arg):
            yield r

    # ---------- 面板指令 ----------

    async def _do_panel(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]

        arg = (arg or "").strip()
        uid = ""
        char_name = ""

        if re.fullmatch(r"\d{7,10}", arg):
            uid = arg
        else:
            rest = arg
            mm = re.search(r"(\d{7,10})", arg)
            if mm:
                uid = mm.group(1)
                rest = arg.replace(uid, "").strip()
            char_name = rest

            if not uid:
                uid = self._get_uid(sender, game)
                if not uid:
                    yield event.plain_result(
                        f"你还没绑定{gname} UID 哦～\n"
                        f"先发：/绑定{gname} 你的UID\n"
                        f"也可以直接：/{gname}面板 你的UID"
                    )
                    return

        cd = self._cooldown_check(sender, game)
        if cd:
            yield event.plain_result(cd)
            return

        # 提示不承诺具体耗时：实测 enka 上游响应在 0.5~18s 间随机波动
        hint = "（崩铁/绝区零数据拉取偶有抖动）" if game in ("hsr", "zzz") else ""
        yield event.plain_result(
            f"正在生成{gname}面板"
            + (f"（{char_name}）" if char_name else "")
            + f" …{hint}"
        )

        try:
            img_path = await self._build(game, uid, char_name, force=False)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 生成失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return

        async for r in self._send_image(event, img_path):
            yield r

    def _cooldown_check(self, sender: str, game: str) -> str:
        key = f"{sender}:{game}"
        now = time.time()
        last = self._last.get(key, 0.0)
        if now - last < self.cooldown:
            wait = int(self.cooldown - (now - last)) + 1
            return f"慢一点啦，{wait} 秒后再试～"
        self._last[key] = now
        return ""

    async def _send_image(self, event: AstrMessageEvent, img_path: str):
        """发图片，失败回退 base64"""
        try:
            from astrbot.api.message_components import Image as Img

            yield event.chain_result([Img.fromFileSystem(img_path)])
        except Exception as e:
            logger.error(f"[miyoyo_panel] 发图失败: {e}")
            import base64

            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            yield event.image_result(f"base64://{b64}")

    @filter.command("原神面板")
    async def panel_gi(self, event: AstrMessageEvent, arg: str = ""):
        async for r in self._do_panel(event, "gi", arg):
            yield r

    @filter.command("崩铁面板")
    async def panel_hsr(self, event: AstrMessageEvent, arg: str = ""):
        async for r in self._do_panel(event, "hsr", arg):
            yield r

    @filter.command("绝区零面板")
    async def panel_zzz(self, event: AstrMessageEvent, arg: str = ""):
        async for r in self._do_panel(event, "zzz", arg):
            yield r

    # ---------- 更新面板（强制刷新，换新图）----------

    @filter.command("更新面板")
    async def refresh(self, event: AstrMessageEvent, arg: str = ""):
        sender = str(event.get_sender_id())
        arg = (arg or "").strip()

        game = parse_game(arg)
        char_name = arg
        mm = re.search(r"(\d{7,10})", arg)
        uid_override = mm.group(1) if mm else ""
        if uid_override:
            char_name = arg.replace(uid_override, "").strip()

        # 去掉游戏名，剩下的当角色名
        for k in GAME_ALIASES:
            char_name = re.sub(re.escape(k), "", char_name, flags=re.I).strip()
        char_name = re.sub(r"\d{7,10}", "", char_name).strip()

        if not game:
            games = [g for g in GAMES if self._get_uid(sender, g)]
            if not games:
                yield event.plain_result(
                    "你还没绑定任何游戏 UID 哦～\n先发 /绑定原神 你的UID"
                )
                return
            game = games[0]

        uid = uid_override or self._get_uid(sender, game)
        if not uid:
            yield event.plain_result(
                f"你还没绑定{GAMES[game]['name']} UID 哦～\n"
                f"先发 /绑定{GAMES[game]['name']} 你的UID"
            )
            return

        gname = GAMES[game]["name"]
        yield event.plain_result(
            f"正在刷新{gname}面板" + (f"（{char_name}）" if char_name else "") + "，请稍等…"
        )

        try:
            img_path = await self._build(game, uid, char_name, force=True)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 刷新失败: {e}", exc_info=True)
            yield event.plain_result(f"刷新失败：{e}")
            return

        async for r in self._send_image(event, img_path):
            yield r

    # ---------- 圣遗物列表 ----------

    async def _do_artifacts(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]

        # AstrBot 会把「/圣遗物 第2页」的参数传给 arg；
        # 但不同版本/前缀配置下 arg 可能为空，所以再从 message_str 兜底取一次
        arg = (arg or "").strip()
        if not arg:
            msg = (event.message_str or "").strip()
            # 去掉指令名本体
            for kw in ("圣遗物", "遗器", "驱动盘"):
                if kw in msg:
                    arg = msg.split(kw, 1)[-1].strip()
                    break
            else:
                arg = msg

        uid = ""
        page = 1

        # 解析页码：支持 "第2页" / "2"
        mpage = re.search(r"第?\s*(\d+)\s*页", arg)
        if mpage:
            page = max(1, int(mpage.group(1)))
        else:
            mnum = re.fullmatch(r"\d{1,3}", arg)
            if mnum:
                page = max(1, int(mnum.group(1)))

        muid = re.search(r"(\d{7,10})", arg)
        if muid:
            uid = muid.group(1)
        if not uid:
            uid = self._get_uid(sender, game)
            if not uid:
                yield event.plain_result(
                    f"你还没绑定{gname} UID 哦～\n"
                    f"先发：/绑定{gname} 你的UID\n"
                    f"也可以直接：/圣遗物{'' if game == 'gi' else GAMES[game]['name']} 你的UID"
                )
                return

        cd = self._cooldown_check(sender, f"{game}:art")
        if cd:
            yield event.plain_result(cd)
            return

        yield event.plain_result(f"正在整理{gname}圣遗物（第 {page} 页）…")

        try:
            img_path = await self._build_artifacts(game, uid, page, force=False)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 圣遗物列表失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return

        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("圣遗物")
    async def artifacts_gi(self, event: AstrMessageEvent, arg: str = ""):
        """原神圣遗物列表"""
        async for r in self._do_artifacts(event, "gi", arg):
            yield r

    @filter.command("遗器")
    async def artifacts_hsr(self, event: AstrMessageEvent, arg: str = ""):
        """崩铁遗器列表"""
        async for r in self._do_artifacts(event, "hsr", arg):
            yield r

    @filter.command("驱动盘")
    async def artifacts_zzz(self, event: AstrMessageEvent, arg: str = ""):
        """绝区零驱动盘列表"""
        async for r in self._do_artifacts(event, "zzz", arg):
            yield r

    # ---------- 圣遗物得分排行榜 ----------

    async def _do_rank(self, event: AstrMessageEvent, game: str, arg: str = ""):
        gname = GAMES[game]["name"]
        yield event.plain_result(
            f"正在统计群内{gname}圣遗物排行…\n"
            f"（首次统计需要逐个拉取群友数据，可能要一会儿）"
        )

        try:
            img_path, n = await self._build_rank(game, force=False)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 排行榜失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return

        if n == 0:
            yield event.plain_result(
                f"群里还没有人绑定{gname} UID 哦～\n"
                f"让大家发 /绑定{gname} 自己的UID 就能上榜了"
            )
            return

        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("圣遗物排行")
    async def rank_gi(self, event: AstrMessageEvent, arg: str = ""):
        """群内原神圣遗物得分排行"""
        async for r in self._do_rank(event, "gi", arg):
            yield r

    @filter.command("遗器排行")
    async def rank_hsr(self, event: AstrMessageEvent, arg: str = ""):
        """群内崩铁遗器得分排行"""
        async for r in self._do_rank(event, "hsr", arg):
            yield r

    @filter.command("驱动盘排行")
    async def rank_zzz(self, event: AstrMessageEvent, arg: str = ""):
        """群内绝区零驱动盘得分排行"""
        async for r in self._do_rank(event, "zzz", arg):
            yield r

    @filter.command("圣遗物总榜")
    async def rank_all(self, event: AstrMessageEvent, arg: str = ""):
        """三个游戏合并的圣遗物总排行"""
        yield event.plain_result("正在统计三个游戏的圣遗物总排行…")

        try:
            img_path, n = await self._build_rank("all", force=False)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 总榜失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return

        if n == 0:
            yield event.plain_result("群里还没有人绑定 UID 哦～")
            return

        async for r in self._send_image(event, img_path):
            yield r

    # ---------- v4：战绩 ----------

    async def _do_record(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]
        uid = ""
        mm = re.search(r"(\d{7,10})", arg or "")
        if mm:
            uid = mm.group(1)
        if not uid:
            uid = self._get_uid(sender, game)
            if not uid:
                yield event.plain_result(
                    f"你还没绑定{gname} UID 哦～\n先发：/绑定{gname} 你的UID"
                )
                return

        cd = self._cooldown_check(sender, f"{game}:rec")
        if cd:
            yield event.plain_result(cd)
            return

        yield event.plain_result(f"正在生成{gname}战绩卡…")
        try:
            img_path = await self._build_record(game, uid)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 战绩卡失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return
        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("战绩")
    async def record_gi(self, event: AstrMessageEvent, arg: str = ""):
        """原神深渊战绩（深境螺旋 / 幻想真境剧诗）"""
        async for r in self._do_record(event, "gi", arg):
            yield r

    @filter.command("深渊战绩")
    async def record_gi2(self, event: AstrMessageEvent, arg: str = ""):
        """原神深渊战绩（同上，别名）"""
        async for r in self._do_record(event, "gi", arg):
            yield r

    # ---------- v4：角色收集度排行 ----------

    async def _do_collection_rank(self, event: AstrMessageEvent, game: str):
        gname = GAMES[game]["name"]
        yield event.plain_result(f"正在统计群内{gname}角色收集度…")
        try:
            img_path, n = await self._build_collection_rank(game)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 收集度排行失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return

        if n == 0:
            yield event.plain_result(f"群里还没有人绑定{gname} UID 哦～")
            return
        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("角色收集排行")
    async def coll_rank_gi(self, event: AstrMessageEvent):
        """群内原神角色收集度排行"""
        async for r in self._do_collection_rank(event, "gi"):
            yield r

    @filter.command("遗器收集排行")
    async def coll_rank_hsr(self, event: AstrMessageEvent):
        """群内崩铁角色收集度排行"""
        async for r in self._do_collection_rank(event, "hsr"):
            yield r

    @filter.command("代理人收集排行")
    async def coll_rank_zzz(self, event: AstrMessageEvent):
        """群内绝区零代理人收集度排行"""
        async for r in self._do_collection_rank(event, "zzz"):
            yield r

    # ---------- v4：伤害计算 ----------

    async def _do_damage(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]

        arg = (arg or "").strip()
        if not arg:
            msg = (event.message_str or "").strip()
            for kw in ("伤害计算", "算伤害", "伤害"):
                if kw in msg:
                    arg = msg.split(kw, 1)[-1].strip()
                    break

        char_name = arg
        uid = self._get_uid(sender, game)
        if not uid:
            yield event.plain_result(
                f"你还没绑定{gname} UID 哦～\n先发：/绑定{gname} 你的UID"
            )
            return

        if not char_name:
            yield event.plain_result(
                f"用法：/伤害 {gname}角色名\n例如：/伤害 兹白"
            )
            return

        cd = self._cooldown_check(sender, f"{game}:dmg")
        if cd:
            yield event.plain_result(cd)
            return

        yield event.plain_result(f"正在计算 {char_name} 的伤害…")
        try:
            img_path = await self._build_damage(game, uid, char_name)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 伤害计算失败: {e}", exc_info=True)
            yield event.plain_result(f"计算失败：{e}")
            return
        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("伤害")
    async def dmg_gi(self, event: AstrMessageEvent, arg: str = ""):
        """伤害计算（默认原神，可用 /伤害 崩铁xxx）"""
        g = parse_game(arg)
        game = g or "gi"
        a = arg
        for k in GAME_ALIASES:
            a = re.sub(re.escape(k), "", a, flags=re.I).strip()
        async for r in self._do_damage(event, game, a):
            yield r

    # ---------- v4：对比 ----------

    async def _do_compare(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]

        arg = (arg or "").strip()
        if not arg:
            msg = (event.message_str or "").strip()
            for kw in ("对比", "比较", "vs", "VS"):
                if kw in msg:
                    arg = msg.split(kw, 1)[-1].strip()
                    break

        parts = [x.strip() for x in re.split(r"[\s,，vsVS]+", arg) if x.strip()]
        parts = [x for x in parts if not re.fullmatch(r"\d{7,10}", x)]

        if len(parts) < 2:
            yield event.plain_result(
                f"用法：/对比 {gname}角色A {gname}角色B\n例如：/对比 兹白 桑多涅"
            )
            return

        uid = self._get_uid(sender, game)
        if not uid:
            yield event.plain_result(f"你还没绑定{gname} UID 哦～")
            return

        cd = self._cooldown_check(sender, f"{game}:cmp")
        if cd:
            yield event.plain_result(cd)
            return

        yield event.plain_result(f"正在对比 {parts[0]} 和 {parts[1]}…")
        try:
            img_path = await self._build_compare(game, uid, parts[0], parts[1])
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 对比失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return
        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("对比")
    async def compare_gi(self, event: AstrMessageEvent, arg: str = ""):
        """角色对比（默认原神）"""
        g = parse_game(arg)
        game = g or "gi"
        a = arg
        for k in GAME_ALIASES:
            a = re.sub(re.escape(k), "", a, flags=re.I).strip()
        async for r in self._do_compare(event, game, a):
            yield r

    # ---------- v4：养成周报 ----------

    async def _do_weekly(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]
        uid = self._get_uid(sender, game)
        if not uid:
            yield event.plain_result(f"你还没绑定{gname} UID 哦～")
            return

        yield event.plain_result(f"正在生成{gname}养成周报…")
        try:
            img_path, is_first = await self._build_weekly(game, uid)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] 周报失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return

        if is_first:
            yield event.plain_result(
                "这是第一次生成周报，已记录当前快照。\n"
                "过几天再发一次就能看到变化了～"
            )
            return

        async for r in self._send_image(event, img_path):
            yield r

    @filter.command("养成周报")
    async def weekly_gi(self, event: AstrMessageEvent, arg: str = ""):
        """原神养成周报"""
        g = parse_game(arg)
        async for r in self._do_weekly(event, g or "gi", arg):
            yield r

    @filter.command("崩铁周报")
    async def weekly_hsr(self, event: AstrMessageEvent):
        """崩铁养成周报"""
        async for r in self._do_weekly(event, "hsr", ""):
            yield r

    @filter.command("绝区零周报")
    async def weekly_zzz(self, event: AstrMessageEvent):
        """绝区零养成周报"""
        async for r in self._do_weekly(event, "zzz", ""):
            yield r

    # ---------- v4：动态图（GIF） ----------

    async def _do_gif(self, event: AstrMessageEvent, game: str, arg: str = ""):
        sender = str(event.get_sender_id())
        gname = GAMES[game]["name"]
        uid = ""
        mm = re.search(r"(\d{7,10})", arg or "")
        if mm:
            uid = mm.group(1)
        if not uid:
            uid = self._get_uid(sender, game)
            if not uid:
                yield event.plain_result(f"你还没绑定{gname} UID 哦～")
                return

        cd = self._cooldown_check(sender, f"{game}:gif")
        if cd:
            yield event.plain_result(cd)
            return

        yield event.plain_result(f"正在生成{gname}动态面板（滚动动画）…")
        try:
            path = await self._build_gif(game, uid)
        except common.PanelError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[miyoyo_panel] GIF 失败: {e}", exc_info=True)
            yield event.plain_result(f"生成失败：{e}")
            return
        async for r in self._send_image(event, path):
            yield r

    @filter.command("动态面板")
    async def gif_gi(self, event: AstrMessageEvent, arg: str = ""):
        """原神动态面板（滚动 GIF）"""
        g = parse_game(arg)
        async for r in self._do_gif(event, g or "gi", arg):
            yield r

    @filter.command("崩铁动图")
    async def gif_hsr(self, event: AstrMessageEvent):
        """崩铁动态面板"""
        async for r in self._do_gif(event, "hsr", ""):
            yield r

    @filter.command("绝区零动图")
    async def gif_zzz(self, event: AstrMessageEvent):
        """绝区零动态面板"""
        async for r in self._do_gif(event, "zzz", ""):
            yield r

    # ---------- 解绑 / 查询 ----------

    @filter.command("解绑uid")
    async def unbind(self, event: AstrMessageEvent, arg: str = ""):
        sender = str(event.get_sender_id())
        if sender not in self.binds:
            yield event.plain_result("你还没绑定任何 UID 呢～")
            return

        game = parse_game(arg)
        if game:
            old = self.binds[sender].pop(game, None)
            if not self.binds[sender]:
                self.binds.pop(sender, None)
            self._save_binds()
            if old:
                yield event.plain_result(f"已解绑{GAMES[game]['name']}（原 UID：{old}）")
            else:
                yield event.plain_result(f"你没有绑定{GAMES[game]['name']}")
        else:
            old = self.binds.pop(sender, {})
            self._save_binds()
            lines = [f"  {GAMES[g]['name']}：{u}" for g, u in old.items()]
            yield event.plain_result("已解绑全部：\n" + "\n".join(lines))

    @filter.command("我的uid")
    async def my_uid(self, event: AstrMessageEvent):
        sender = str(event.get_sender_id())
        rec = self.binds.get(sender) or {}
        if not rec:
            yield event.plain_result(
                "还没绑定哦～\n"
                "/绑定原神 你的UID\n"
                "/绑定崩铁 你的UID\n"
                "/绑定绝区零 你的UID"
            )
            return
        lines = [f"{GAMES[g]['name']}：{uid}" for g, uid in rec.items()]
        yield event.plain_result("你的绑定：\n" + "\n".join(lines))

    # ---------- 核心：拉数据 + 渲染 ----------

    async def _build(self, game: str, uid: str, char_name: str, force: bool) -> str:
        """生成图片，返回本地路径"""
        tag = ("detail_" + re.sub(r"\W", "", char_name)) if char_name else "list"
        out = os.path.join(self.img_dir, f"{game}_{uid}_{tag}.webp")

        if not force and os.path.exists(out):
            if (time.time() - os.path.getmtime(out)) < self.cache_ttl:
                return out

        import aiohttp

        async with self._lock:
            if not force and os.path.exists(out):
                if (time.time() - os.path.getmtime(out)) < self.cache_ttl:
                    return out

            async with aiohttp.ClientSession() as session:
                await self.store.ensure(session, game)
                raw = await self.client.fetch(session, game, uid, force=force)

                if game == "gi":
                    player = gi.parse_player(raw, self.store)
                    mod = gi
                elif game == "hsr":
                    player = hsr.parse_player(raw, self.store)
                    mod = hsr
                else:
                    player = zzz.parse_player(raw, self.store)
                    mod = zzz

                if not player.get("avatars"):
                    raise common.PanelError(
                        f"UID {uid} 没有公开角色数据。\n"
                        "请在游戏内打开「显示角色详情 / 展示柜」后重试。"
                    )

                if char_name:
                    av = self._match_char(player["avatars"], char_name)
                    if not av:
                        names = "、".join(a["name"] for a in player["avatars"][:10])
                        raise common.PanelError(
                            f"没找到角色「{char_name}」。\n"
                            f"可用：{names}\n"
                            f"（发 /{GAMES[game]['cmd']} 查看完整列表）"
                        )
                    fn = mod.render_detail
                    args = (av, player)
                else:
                    fn = mod.render_list
                    args = (player,)

                data = await asyncio.get_event_loop().run_in_executor(None, fn, *args)

            if force:
                out = os.path.join(
                    self.img_dir, f"{game}_{uid}_{tag}_{int(time.time())}.webp"
                )

            tmp = out + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out)

            self._cleanup(game, uid, out)

            logger.info(
                f"[miyoyo_panel] {game} {uid} {tag} -> {len(data)/1024:.0f}KB"
                + (" [强制刷新]" if force else "")
            )
            return out

    # ---------- 圣遗物列表 / 排行榜的构建 ----------

    async def _fetch_player(self, session, game: str, uid: str, force: bool) -> dict:
        """拉数据 + 解析，返回 player"""
        raw = await self.client.fetch(session, game, uid, force=force)
        if game == "gi":
            return gi.parse_player(raw, self.store)
        if game == "hsr":
            return hsr.parse_player(raw, self.store)
        return zzz.parse_player(raw, self.store)

    async def _build_artifacts(self, game: str, uid: str, page: int, force: bool) -> str:
        """生成圣遗物列表图"""
        out = os.path.join(self.img_dir, f"{game}_{uid}_arts_p{page}.webp")

        if not force and os.path.exists(out):
            if (time.time() - os.path.getmtime(out)) < self.cache_ttl:
                return out

        import aiohttp

        async with self._lock:
            if not force and os.path.exists(out):
                if (time.time() - os.path.getmtime(out)) < self.cache_ttl:
                    return out

            async with aiohttp.ClientSession() as session:
                await self.store.ensure(session, game)
                player = await self._fetch_player(session, game, uid, force)

            avatars = player.get("avatars") or []
            if not avatars:
                raise common.PanelError(
                    f"UID {uid} 没有公开角色数据。\n"
                    "请在游戏内打开「显示角色详情 / 展示柜」后重试。"
                )

            mod = {"gi": gi, "hsr": hsr, "zzz": zzz}[game]
            items = mod.collect_artifacts(player)

            if not items:
                raise common.PanelError(
                    f"UID {uid} 没有可展示的圣遗物数据。\n"
                    "可能是角色未公开装备详情。"
                )

            titles = {
                "gi": "原神圣遗物列表",
                "hsr": "崩铁遗器列表",
                "zzz": "绝区零驱动盘列表",
            }
            data = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: common.render_artifact_grid(
                    items,
                    title=titles[game],
                    uid=uid,
                    page=page,
                    page_size=15,
                    cols=5,
                    game=game,
                ),
            )

            tmp = out + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out)
            self._cleanup(game, uid, out)

            logger.info(
                f"[miyoyo_panel] {game} {uid} 圣遗物 p{page} -> "
                f"{len(items)} 件 {len(data)/1024:.0f}KB"
            )
            return out

    async def _build_rank(self, game: str, force: bool) -> tuple[str, int]:
        """生成群圣遗物排行榜图。game 可为 gi/hsr/zzz/all。

        返回 (图片路径, 上榜人数)
        """
        games = ["gi", "hsr", "zzz"] if game == "all" else [game]
        tag = game if game != "all" else "all"
        out = os.path.join(self.img_dir, f"rank_{tag}.webp")

        if not force and os.path.exists(out):
            if (time.time() - os.path.getmtime(out)) < self.cache_ttl:
                return out, self._rank_count

        # 汇总：每个 QQ 在每个游戏绑定的 UID
        entries = []  # (qq, game, uid)
        for qq, rec in (self.binds or {}).items():
            for g in games:
                uid = (rec or {}).get(g)
                if uid:
                    entries.append((qq, g, uid))

        if not entries:
            self._rank_count = 0
            raise common.PanelError(
                "群里还没有人绑定 UID，无法生成排行。\n"
                "让大家发 /绑定原神 自己的UID 就能上榜了。"
            )

        import aiohttp

        rows = []
        async with aiohttp.ClientSession() as session:
            for g in games:
                try:
                    await self.store.ensure(session, g)
                except Exception:
                    pass

            for qq, g, uid in entries:
                try:
                    player = await self._fetch_player(session, g, uid, force)
                except Exception as e:
                    logger.warning(f"[miyoyo_panel] 排行跳过 {g}/{uid}: {e}")
                    continue

                mod = {"gi": gi, "hsr": hsr, "zzz": zzz}[g]
                try:
                    items = mod.collect_artifacts(player)
                except Exception:
                    items = []
                if not items:
                    continue

                top = items[0]
                rows.append(
                    {
                        "qq": qq,
                        "game": g,
                        "game_cn": GAMES[g]["name"],
                        "uid": uid,
                        "nickname": player.get("nickname") or uid,
                        "best": float(top.get("score") or 0),
                        "best_name": top.get("name") or "",
                        "best_set": top.get("set") or "",
                        "best_main": top.get("main_text") or "",
                        "owner": top.get("owner") or "",
                        "count": len(items),
                    }
                )

        if not rows:
            self._rank_count = 0
            raise common.PanelError(
                "拉取到的群友都还没有公开圣遗物数据。\n"
                "请在游戏内打开「显示角色详情」后重试。"
            )

        # 按最高分排序
        rows.sort(key=lambda x: -x["best"])
        for i, r in enumerate(rows, 1):
            r["rank"] = i

        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: common.render_rank_board(rows, game=game)
        )

        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, out)

        self._rank_count = len(rows)
        logger.info(
            f"[miyoyo_panel] 排行 {game} -> {len(rows)} 人 {len(data)/1024:.0f}KB"
        )
        return out, len(rows)

    # ---------- v4：构建方法 ----------

    def _mod(self, game: str):
        return {"gi": gi, "hsr": hsr, "zzz": zzz}[game]

    async def _get_player(self, game: str, uid: str, force: bool = False) -> dict:
        """拉取 + 解析（内部用，自带 store 预加载）"""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            await self.store.ensure(session, game)
            return await self._fetch_player(session, game, uid, force)

    async def _build_record(self, game: str, uid: str) -> str:
        """战绩卡"""
        out = os.path.join(self.img_dir, f"{game}_{uid}_record.webp")
        if os.path.exists(out) and (time.time() - os.path.getmtime(out)) < self.cache_ttl:
            return out

        player = await self._get_player(game, uid)
        if not player.get("avatars"):
            raise common.PanelError(
                f"UID {uid} 没有公开角色数据。\n请先在游戏内打开「显示角色详情」。"
            )

        info = self._mod(game).collect_record(player)
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: common.render_record_card(info)
        )
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, out)
        logger.info(f"[miyoyo_panel] {game} {uid} 战绩卡 {len(data)/1024:.0f}KB")
        return out

    async def _build_collection_rank(self, game: str) -> tuple[str, int]:
        """角色收集度排行"""
        out = os.path.join(self.img_dir, f"collrank_{game}.webp")
        if os.path.exists(out) and (time.time() - os.path.getmtime(out)) < self.cache_ttl:
            return out, self._rank_count

        entries = [
            (qq, (rec or {}).get(game))
            for qq, rec in (self.binds or {}).items()
            if (rec or {}).get(game)
        ]
        if not entries:
            self._rank_count = 0
            raise common.PanelError(
                "群里还没有人绑定这个游戏的 UID。\n"
                f"让大家发 /绑定{GAMES[game]['name']} 自己的UID 就能上榜。"
            )

        import aiohttp

        rows = []
        async with aiohttp.ClientSession() as session:
            try:
                await self.store.ensure(session, game)
            except Exception:
                pass
            for qq, uid in entries:
                try:
                    player = await self._fetch_player(session, game, uid, False)
                except Exception as e:
                    logger.warning(f"[miyoyo_panel] 收集度跳过 {uid}: {e}")
                    continue
                if not player.get("avatars"):
                    continue
                try:
                    coll = self._mod(game).collect_collection(player)
                except Exception:
                    continue
                rows.append(
                    {
                        "qq": qq,
                        "nickname": player.get("nickname") or uid,
                        "uid": uid,
                        **coll,
                    }
                )

        if not rows:
            self._rank_count = 0
            raise common.PanelError("拉取到的群友都没有公开数据。")

        rows.sort(key=lambda x: -int(x.get("total") or 0))
        for i, r in enumerate(rows, 1):
            r["rank"] = i

        data = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: common.render_collection_board(rows, game_cn=GAMES[game]["name"]),
        )
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, out)
        self._rank_count = len(rows)
        logger.info(f"[miyoyo_panel] 收集度排行 {game} {len(rows)} 人")
        return out, len(rows)

    async def _build_damage(self, game: str, uid: str, char_name: str) -> str:
        """伤害计算卡"""
        player = await self._get_player(game, uid)
        avatars = player.get("avatars") or []
        if not avatars:
            raise common.PanelError(f"UID {uid} 没有公开角色数据。")

        av = self._match_char(avatars, char_name)
        if not av:
            names = "、".join(a["name"] for a in avatars[:10])
            raise common.PanelError(
                f"没找到角色「{char_name}」。\n可用：{names}"
            )

        info = self._mod(game).calc_damage(player, av)
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: common.render_damage_card(info)
        )
        out = os.path.join(self.img_dir, f"{game}_{uid}_dmg_{re.sub(r'[^\\w]', '', char_name)}.webp")
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, out)
        logger.info(f"[miyoyo_panel] {game} 伤害计算 {char_name} {len(data)/1024:.0f}KB")
        return out

    async def _build_compare(self, game: str, uid: str, a_name: str, b_name: str) -> str:
        """角色对比"""
        player = await self._get_player(game, uid)
        avatars = player.get("avatars") or []
        a = self._match_char(avatars, a_name)
        b = self._match_char(avatars, b_name)
        if not a or not b:
            miss = a_name if not a else b_name
            names = "、".join(x["name"] for x in avatars[:10])
            raise common.PanelError(f"没找到角色「{miss}」。\n可用：{names}")

        def side(av):
            cm_map = av.get("core_map") or {}
            st = [
                ("生命值", f"{cm_map.get('hp', 0):,.0f}"),
                ("攻击力", f"{cm_map.get('atk', 0):,.0f}"),
                ("防御力", f"{cm_map.get('def', 0):,.0f}"),
                ("暴击率", f"{float(cm_map.get('crit', 0))*100:.1f}%"),
                ("暴击伤害", f"{float(cm_map.get('critdmg', 0))*100:.1f}%"),
                ("元素精通", f"{cm_map.get('em', 0):.0f}"),
            ]
            crit_f = 1.0 + min(max(float(cm_map.get("crit") or 0), 0), 1) * float(
                cm_map.get("critdmg") or 0
            )
            score = float(cm_map.get("atk") or 0) * crit_f
            return {
                "name": av.get("name") or "",
                "sub": f"{av.get('element') or ''} · Lv{av.get('level') or 0}",
                "stats": st,
                "score": score,
            }

        L, R = side(a), side(b)
        rows = []
        for i in range(len(L["stats"])):
            lab = L["stats"][i][0]
            lv, rv = L["stats"][i][1], R["stats"][i][1]
            try:
                lf = float(str(lv).replace("%", "").replace(",", ""))
                rf = float(str(rv).replace("%", "").replace(",", ""))
                better = -1 if lf > rf else (1 if rf > lf else 0)
            except Exception:
                better = 0
            rows.append((lab, lv, rv, better))

        diff = abs(L["score"] - R["score"])
        if L["score"] >= R["score"]:
            verdict = f"{L['name']} 略胜一筹（综合指数高 {diff:,.0f}）"
        else:
            verdict = f"{R['name']} 略胜一筹（综合指数高 {diff:,.0f}）"

        info = {
            "title": f"{L['name']} VS {R['name']}",
            "left": L,
            "right": R,
            "rows": rows,
            "verdict": verdict,
        }
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: common.render_compare(info)
        )
        out = os.path.join(self.img_dir, f"{game}_cmp_{int(time.time())}.webp")
        with open(out, "wb") as f:
            f.write(data)
        logger.info(f"[miyoyo_panel] {game} 对比图 {len(data)/1024:.0f}KB")
        return out

    async def _build_weekly(self, game: str, uid: str) -> tuple[str, bool]:
        """养成周报：与上次快照对比。返回 (路径, 是否首次)"""
        snap_file = os.path.join(self.data_dir, "snapshots.json")
        snaps = {}
        if os.path.exists(snap_file):
            try:
                snaps = json.load(open(snap_file, encoding="utf-8"))
            except Exception:
                snaps = {}

        player = await self._get_player(game, uid)
        if not player.get("avatars"):
            raise common.PanelError(f"UID {uid} 没有公开角色数据。")

        # 当前快照
        cur = {
            "ts": int(time.time()),
            "avatar_count": player.get("avatar_count") or 0,
            "level": player.get("level") or 0,
            "achievement": player.get("achievement") or 0,
            "record": player.get("record") or {},
            "names": [a.get("name") for a in (player.get("avatars") or [])],
        }
        key = f"{game}:{uid}"
        old = snaps.get(key)

        snaps[key] = cur
        try:
            with open(snap_file, "w", encoding="utf-8") as f:
                json.dump(snaps, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[miyoyo_panel] 保存快照失败: {e}")

        if not old:
            return "", True

        days = max(1, int((cur["ts"] - int(old.get("ts") or cur["ts"])) / 86400))

        changes = []

        def add(name, o, n, higher_better=True):
            if o == n:
                return
            delta = n - o
            better = delta > 0 if higher_better else delta < 0
            changes.append((name, f"{o:,}", f"{n:,}", f"{delta:+,}", better))

        add("冒险等阶/等级", int(old.get("level") or 0), int(cur.get("level") or 0))
        add("成就数", int(old.get("achievement") or 0), int(cur.get("achievement") or 0))
        add("展示角色", int(old.get("avatar_count") or 0), int(cur.get("avatar_count") or 0))

        orc = old.get("record") or {}
        crc = cur.get("record") or {}
        for k, label in (
            ("achievementCount", "成就"),
            ("avatarCount", "角色数"),
            ("relicCount", "遗器数"),
        ):
            if k in crc or k in orc:
                add(label, int(orc.get(k) or 0), int(crc.get(k) or 0))

        old_names = set(old.get("names") or [])
        new_names = [n for n in (cur.get("names") or []) if n and n not in old_names]

        if not changes and not new_names:
            raise common.PanelError(
                f"最近 {days} 天没有检测到变化。\n"
                "过段时间再来看看～"
            )

        better_cnt = sum(1 for c in changes if c[4])
        summary = (
            f"最近 {days} 天共 {len(changes)} 项数据变化，"
            f"其中 {better_cnt} 项提升。"
            + (f" 新获得 {len(new_names)} 个角色。" if new_names else "")
        )

        info = {
            "game_cn": GAMES[game]["name"],
            "nickname": player.get("nickname") or "",
            "uid": uid,
            "days": days,
            "changes": changes,
            "new_chars": new_names[:12],
            "summary": summary,
        }
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: common.render_weekly_report(info)
        )
        out = os.path.join(self.img_dir, f"{game}_{uid}_weekly.webp")
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, out)
        logger.info(f"[miyoyo_panel] {game} 周报 {len(data)/1024:.0f}KB")
        return out, False

    async def _build_gif(self, game: str, uid: str) -> str:
        """动态面板：把长图切成滚动帧合成 GIF"""
        player = await self._get_player(game, uid)
        avatars = player.get("avatars") or []
        if not avatars:
            raise common.PanelError(f"UID {uid} 没有公开角色数据。")

        # 先渲染一张长图（列表图）
        mod = self._mod(game)
        webp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: mod.render_list(player)
        )

        def make():
            import io as _io

            from PIL import Image

            img = Image.open(_io.BytesIO(webp)).convert("RGB")
            frames = common.make_scroll_frames(img, frame_h=760, step=300)
            # 限制帧数（GIF 体积与帧数线性相关，6 帧足够看出滚动效果）
            if len(frames) > 6:
                step = max(1, len(frames) // 6)
                frames = frames[::step][:6]
            return common.save_gif(frames, duration=140, max_size=600)

        data = await asyncio.get_event_loop().run_in_executor(None, make)
        out = os.path.join(self.img_dir, f"{game}_{uid}_anim.gif")
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, out)
        logger.info(f"[miyoyo_panel] {game} GIF {len(data)/1024:.0f}KB")
        return out

    def _cleanup(self, game: str, uid: str, keep: str) -> None:
        try:
            prefix = f"{game}_{uid}_"
            files = [
                os.path.join(self.img_dir, f)
                for f in os.listdir(self.img_dir)
                if f.startswith(prefix)
            ]
            files.sort(key=os.path.getmtime, reverse=True)
            for f in files[3:]:
                if f != keep:
                    os.remove(f)
        except Exception:
            pass

    def _match_char(self, avatars: list, name: str) -> dict | None:
        """角色名模糊匹配"""
        name = name.strip()
        if not name:
            return None

        for a in avatars:
            if a.get("name") == name:
                return a

        for a in avatars:
            if name in (a.get("name") or ""):
                return a

        for a in avatars:
            if (a.get("name") or "") in name:
                return a

        return None

    async def terminate(self):
        try:
            task = getattr(self, "_warm_task", None)
            if task and not task.done():
                task.cancel()
        except Exception:
            pass
        logger.info("[miyoyo_panel] 已停止")
