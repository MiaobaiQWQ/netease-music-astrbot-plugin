"""
AstrBot 豪华网易云点歌插件
- 作者：NachoCrazy
- 仓库：https://github.com/NachoCrazy/netease-music-astrbot-plugin
- 功能：交互式选歌、封面展示、语音播放、音质自动回退。
"""

import re
import time
import base64
import aiohttp
import asyncio
import urllib.parse
import os
import importlib.util
from typing import Dict, Any, Optional, List

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record

def _load_search_result_image_renderer_class():
    try:
        from image_renderer import SearchResultImageRenderer
        return SearchResultImageRenderer
    except Exception:
        pass

    try:
        file_path = os.path.join(os.path.dirname(__file__), "image_renderer.py")
        spec = importlib.util.spec_from_file_location("netease_music_image_renderer", file_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls = getattr(module, "SearchResultImageRenderer", None)
            if cls is not None:
                return cls
    except Exception:
        pass

    return None

# --- API 封装 ---
class NeteaseMusicAPI:
    """
    对 NeteaseCloudMusicApi 的简单封装。
    统一提供搜索、获取歌曲详情、获取音频链接等接口调用。
    """
    def __init__(self, api_url: str, session: aiohttp.ClientSession):
        self.base_url = api_url.rstrip("/")
        self.session = session

    async def search_songs(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """按关键词搜索歌曲。"""
        url = f"{self.base_url}/search?keywords={urllib.parse.quote(keyword)}&limit={limit}&type=1"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("result", {}).get("songs", [])

    async def get_song_details(self, song_id: int) -> Optional[Dict[str, Any]]:
        """获取单首歌曲的详细信息。"""
        url = f"{self.base_url}/song/detail?ids={str(song_id)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data["songs"][0] if data.get("songs") else None

    async def get_song_details_batch(self, song_ids: List[int]) -> List[Dict[str, Any]]:
        """批量获取歌曲详细信息。"""
        ids = [str(int(x)) for x in song_ids if str(x).strip().isdigit() or isinstance(x, int)]
        if not ids:
            return []
        url = f"{self.base_url}/song/detail?ids={','.join(ids)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            songs = data.get("songs")
            if isinstance(songs, list):
                return songs
        return []

    async def get_audio_url(self, song_id: int, quality: str) -> Optional[str]:
        """
        获取歌曲音频流地址（自动音质回退）。
        """
        qualities_to_try = list(dict.fromkeys([quality, "exhigh", "higher", "standard"]))
        for q in qualities_to_try:
            url = f"{self.base_url}/song/url/v1?id={str(song_id)}&level={q}"
            async with self.session.get(url) as r:
                r.raise_for_status()
                data = await r.json()
                audio_info = data.get("data", [{}])[0]
                if audio_info.get("url"):
                    return audio_info["url"]
        return None

    async def download_image(self, url: str) -> Optional[bytes]:
        """下载图片字节数据。"""
        return await self.download_image_with_timeout(url, 10)

    async def download_image_with_timeout(self, url: str, timeout: float) -> Optional[bytes]:
        if not url:
            return None
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    return await r.read()
        except Exception:
            return None
        return None

# --- 插件主类 ---
class Main(star.Star):
    """
    猫娘主题的网易云点歌插件：搜索、选择并在聊天中直接播放歌曲。
    """
    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.config.setdefault("api_url", "http://127.0.0.1:3000")
        self.config.setdefault("quality", "exhigh")
        self.config.setdefault("search_limit", 5)
        self.config.setdefault("search_result_image", True)
        self.config.setdefault("search_result_include_cover", True)
        self.config.setdefault("song_detail_image", True)
        self.config.setdefault("group_access_mode", "off")
        self.config.setdefault("whitelist_groups", "")
        self.config.setdefault("blacklist_groups", "")
        self.config.setdefault("group_block_tip", "喵~ 本群暂未开启点歌功能哦。")
        self.config.setdefault("cmd_empty_keyword_tip", "主人，请告诉我您想听什么歌喵~ 例如：/点歌 Lemon")
        self.config.setdefault("natural_language_enabled", True)
        self.config.setdefault(
            "natural_language_regex",
            r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$",
        )
        self.config.setdefault("natural_language_keyword_group", 2)
        
        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.result_message_map: Dict[str, Dict[str, Any]] = {}
        
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.api = NeteaseMusicAPI(self.config["api_url"], self.http_session)
        renderer_cls = _load_search_result_image_renderer_class()
        self.image_renderer = renderer_cls(self.api.download_image_with_timeout) if renderer_cls else None
        
        self.cleanup_task: Optional[asyncio.Task] = None

    # --- 生命周期 ---

    async def initialize(self):
        """插件启用时启动后台清理任务。"""
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("网易云点歌插件：后台清理任务已启动。")

    async def terminate(self):
        """插件卸载时释放资源。"""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                logger.info("网易云点歌插件：后台清理任务已取消。")
        
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("网易云点歌插件：HTTP 会话已关闭。")

    async def _periodic_cleanup(self):
        """定时清理过期的等待会话与缓存。"""
        while True:
            await asyncio.sleep(60)  # 每 60 秒执行一次
            now = time.time()
            expired_sessions = []
            
            for user_key, user_session in self.waiting_users.items():
                if user_session['expire'] < now:
                    expired_sessions.append((user_key, user_session['key']))
            
            if expired_sessions:
                logger.info(f"网易云点歌插件：清理 {len(expired_sessions)} 个过期会话。")
                for user_key, cache_key in expired_sessions:
                    if user_key in self.waiting_users:
                        del self.waiting_users[user_key]
                    if cache_key in self.song_cache:
                        del self.song_cache[cache_key]
                    self._remove_cache_key_mappings(cache_key)

            expired_msg_ids = []
            for msg_id, it in list(self.result_message_map.items()):
                try:
                    if float(it.get("expire", 0)) < now:
                        expired_msg_ids.append(msg_id)
                except Exception:
                    expired_msg_ids.append(msg_id)
            for msg_id in expired_msg_ids:
                self.result_message_map.pop(msg_id, None)

    def _get_user_key(self, event: AstrMessageEvent) -> str:
        session_id = ""
        try:
            session_id = str(event.get_session_id() or "")
        except Exception:
            session_id = ""

        uid = None
        for attr in ["get_user_id", "get_userid", "get_sender_id", "get_senderid", "get_author_id", "get_authorid"]:
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    v = fn()
                    if v is not None and str(v).strip():
                        uid = str(v).strip()
                        break
                except Exception:
                    pass
        if uid is None:
            for attr in ["user_id", "userid", "sender_id", "senderid", "author_id", "authorid"]:
                v = getattr(event, attr, None)
                if v is not None and str(v).strip():
                    uid = str(v).strip()
                    break

        if uid:
            return f"{session_id}:{uid}"
        return session_id

    def _extract_send_message_id(self, send_result: Any) -> Optional[str]:
        if send_result is None:
            return None
        if isinstance(send_result, (str, int)):
            s = str(send_result).strip()
            return s if s else None
        if isinstance(send_result, dict):
            for k in ["message_id", "msg_id", "id"]:
                v = send_result.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
        for attr in ["message_id", "msg_id", "id", "messageId", "msgId"]:
            v = getattr(send_result, attr, None)
            if v is not None and str(v).strip():
                return str(v).strip()
        for attr in ["get_message_id", "get_msg_id", "get_id"]:
            fn = getattr(send_result, attr, None)
            if callable(fn):
                try:
                    v = fn()
                    if v is not None and str(v).strip():
                        return str(v).strip()
                except Exception:
                    pass
        return None

    def _extract_reply_message_id(self, event: AstrMessageEvent) -> Optional[str]:
        for attr in ["get_reply_message_id", "get_reply_id", "get_quote_message_id", "get_quote_id", "get_refer_id"]:
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    v = fn()
                    if v is not None and str(v).strip():
                        return str(v).strip()
                except Exception:
                    pass

        for attr in ["reply_message_id", "reply_id", "quote_message_id", "quote_id", "refer_id"]:
            v = getattr(event, attr, None)
            if v is not None and str(v).strip():
                return str(v).strip()

        for container_attr in ["message", "msg", "message_obj", "messageObject", "message_event"]:
            obj = getattr(event, container_attr, None)
            if obj is None:
                continue
            if isinstance(obj, dict):
                for k in ["reply", "quote", "reference", "refer", "source"]:
                    sub = obj.get(k)
                    if isinstance(sub, dict):
                        for kk in ["message_id", "msg_id", "id"]:
                            v = sub.get(kk)
                            if v is not None and str(v).strip():
                                return str(v).strip()
            for sub_attr in ["reply", "quote", "reference", "refer", "source"]:
                sub = getattr(obj, sub_attr, None)
                if sub is None:
                    continue
                for id_attr in ["message_id", "msg_id", "id"]:
                    v = getattr(sub, id_attr, None)
                    if v is not None and str(v).strip():
                        return str(v).strip()
        return None

    def _remove_cache_key_mappings(self, cache_key: str) -> None:
        to_del = []
        for msg_id, it in list(self.result_message_map.items()):
            try:
                if it.get("key") == cache_key:
                    to_del.append(msg_id)
            except Exception:
                continue
        for msg_id in to_del:
            self.result_message_map.pop(msg_id, None)

    def _parse_id_set(self, value: Any) -> set:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            return {str(x).strip() for x in value if str(x).strip()}
        s = str(value)
        s = s.replace("，", ",").replace("；", ",").replace(";", ",")
        parts = []
        for p in s.split(","):
            p2 = p.strip()
            if p2:
                parts.append(p2)
        out = set()
        for p in parts:
            for token in p.split():
                t = token.strip()
                if t:
                    out.add(t)
        return out

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        for attr in ["get_group_id", "get_groupid", "getGroupId", "getGroupID"]:
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    gid = fn()
                    if gid is not None and str(gid).strip():
                        return str(gid).strip()
                except Exception:
                    pass
        for attr in ["group_id", "groupid", "groupId", "groupID", "guild_id", "channel_id"]:
            gid = getattr(event, attr, None)
            if gid is not None and str(gid).strip():
                return str(gid).strip()

        sid = ""
        try:
            sid = str(event.get_session_id() or "")
        except Exception:
            sid = ""
        sid_l = sid.lower()
        if any(k in sid_l for k in ["group", "guild", "channel"]):
            nums = re.findall(r"\d+", sid)
            if nums:
                return nums[-1]
        return None

    async def _check_group_access(self, event: AstrMessageEvent, notify: bool) -> bool:
        mode = str(self.config.get("group_access_mode", "off") or "off").strip().lower()
        if mode == "off":
            return True

        gid = self._get_group_id(event)
        if gid is None:
            return True

        whitelist = self._parse_id_set(self.config.get("whitelist_groups", ""))
        blacklist = self._parse_id_set(self.config.get("blacklist_groups", ""))

        allowed = True
        if mode == "whitelist":
            allowed = gid in whitelist
        elif mode == "blacklist":
            allowed = gid not in blacklist

        if not allowed and notify:
            await event.send(MessageChain([Plain(str(self.config.get("group_block_tip") or "喵~ 本群暂未开启点歌功能哦。"))]))
        return allowed

    # --- 事件处理 ---

    @filter.command("点歌", alias={"music", "听歌", "网易云"})
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        """处理 /点歌 命令。"""
        if not await self._check_group_access(event, notify=True):
            return
        if not keyword.strip():
            await event.send(MessageChain([Plain(str(self.config.get("cmd_empty_keyword_tip") or "主人，请告诉我您想听什么歌喵~ 例如：/点歌 Lemon"))]))
            return
        await self.search_and_show(event, keyword.strip())

    @filter.regex(r"^.+$", priority=10)
    async def natural_language_handler(self, event: AstrMessageEvent):
        """处理自然语言点歌。"""
        if not await self._check_group_access(event, notify=False):
            return
        if not bool(self.config.get("natural_language_enabled", True)):
            return

        msg = (event.message_str or "").strip()
        if not msg or msg.isdigit():
            return

        pattern = str(
            self.config.get(
                "natural_language_regex",
                r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$",
            )
        )
        try:
            match = re.search(pattern, event.message_str)
        except Exception:
            return
        if match:
            group_idx = 2
            try:
                group_idx = int(self.config.get("natural_language_keyword_group", 2))
            except Exception:
                group_idx = 2
            keyword = ""
            try:
                if "keyword" in match.groupdict():
                    keyword = (match.group("keyword") or "").strip()
                else:
                    keyword = (match.group(group_idx) or "").strip()
            except Exception:
                keyword = ""
            if keyword:
                await self.search_and_show(event, keyword)

    @filter.regex(r"^\d+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        """处理用户在搜索结果中的数字选择。"""
        if not await self._check_group_access(event, notify=False):
            return
        user_key = self._get_user_key(event)
        now = time.time()
        cache_key = None

        reply_msg_id = self._extract_reply_message_id(event)
        if reply_msg_id:
            it = self.result_message_map.get(str(reply_msg_id))
            if it:
                try:
                    if float(it.get("expire", 0)) >= now and it.get("key"):
                        cache_key = str(it["key"])
                except Exception:
                    pass

        user_session = self.waiting_users.get(user_key)
        if cache_key is None:
            if not user_session:
                return
            if now > user_session.get("expire", 0):
                return
            cache_key = user_session.get("key")
            if not cache_key:
                return

        try:
            num = int(event.message_str.strip())
        except ValueError:
            return

        limit = self.config.get("search_limit", 5)
        if not (1 <= num <= limit):
            return

        event.stop_event()
        await self.play_selected_song(event, str(cache_key), num)
        
        if user_key in self.waiting_users:
            del self.waiting_users[user_key]

    # --- 核心逻辑 ---

    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        """搜索歌曲并向用户展示候选列表。"""
        try:
            songs = await self.api.search_songs(keyword, self.config["search_limit"])
        except Exception as e:
            logger.error(f"网易云点歌插件：API 搜索失败。错误：{e!s}")
            await event.send(MessageChain([Plain(f"呜喵...和音乐服务器的连接断掉了...主人，请检查一下API服务是否正常运行喵？")]))
            return

        if not songs:
            await event.send(MessageChain([Plain(f"对不起主人...我...我没能找到「{keyword}」这首歌喵... T_T")]))
            return

        user_key = self._get_user_key(event)
        expire_at = time.time() + 60
        cache_key = f"{user_key}_{int(time.time())}"
        self.song_cache[cache_key] = songs

        send_result = None
        if self.image_renderer and self.config.get("search_result_image", True):
            cover_url_by_id: Dict[int, str] = {}
            if self.config.get("search_result_include_cover", True):
                try:
                    song_ids = []
                    for s in songs:
                        v = s.get("id")
                        if isinstance(v, int):
                            song_ids.append(v)
                        else:
                            try:
                                song_ids.append(int(str(v).strip()))
                            except Exception:
                                pass
                    details = await self.api.get_song_details_batch(song_ids)
                    for d in details:
                        try:
                            sid = int(d.get("id"))
                        except Exception:
                            continue
                        cover = ""
                        al = d.get("al") or {}
                        if isinstance(al, dict):
                            cover = str(al.get("picUrl") or "").strip()
                        if cover:
                            cover_url_by_id[sid] = cover
                except Exception:
                    cover_url_by_id = {}

            items: List[Dict[str, str]] = []
            for song in songs:
                artists = " / ".join(a.get("name", "") for a in song.get("artists", []) if a.get("name"))
                album = song.get("album", {}).get("name", "未知专辑")
                duration_ms = song.get("duration", 0)
                dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
                song_id = song.get("id")
                cover_url = ""
                try:
                    if isinstance(song_id, int) and song_id in cover_url_by_id:
                        cover_url = cover_url_by_id[song_id]
                    else:
                        cover_url = cover_url_by_id.get(int(str(song_id).strip()), "")
                except Exception:
                    cover_url = ""
                if not cover_url:
                    cover_url = song.get("album", {}).get("picUrl") or ""
                items.append(
                    {
                        "name": song.get("name", ""),
                        "artists": artists,
                        "album": album,
                        "duration": dur_str,
                        "cover_url": cover_url,
                    }
                )

            img_bytes = await self.image_renderer.render_search_result_image(
                keyword=keyword,
                items=items,
                include_cover=self.config.get("search_result_include_cover", True),
            )
            if img_bytes:
                send_result = await event.send(MessageChain([Image.fromBase64(base64.b64encode(img_bytes).decode())]))
            else:
                send_result = await self._send_search_result_text(event, keyword, songs)
        else:
            send_result = await self._send_search_result_text(event, keyword, songs)

        msg_id = self._extract_send_message_id(send_result)
        if msg_id:
            self.result_message_map[str(msg_id)] = {"key": cache_key, "expire": expire_at, "user_key": user_key}

        self.waiting_users[user_key] = {"key": cache_key, "expire": expire_at}

    async def _send_search_result_text(self, event: AstrMessageEvent, keyword: str, songs: List[Dict[str, Any]]):
        response_lines = [f"主人，我为您找到了 {len(songs)} 首歌曲喵！请回复数字告诉我您想听哪一首~"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")
        return await event.send(MessageChain([Plain("\n".join(response_lines))]))

    async def play_selected_song(self, event: AstrMessageEvent, cache_key: str, num: int):
        """播放用户选择的歌曲。"""
        if cache_key not in self.song_cache:
            await event.send(MessageChain([Plain("喵呜~ 主人选择得太久了，搜索结果已经凉掉了哦，请重新点歌吧~")]))
            return

        songs = self.song_cache[cache_key]
        if not (1 <= num <= len(songs)):
             await event.send(MessageChain([Plain("主人，您输入的数字不对哦，请选择列表里的歌曲编号喵~")]))
             return
             
        selected_song = songs[num - 1]
        song_id = selected_song["id"]
        
        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("无法获取歌曲详细信息。")

            audio_url = await self.api.get_audio_url(song_id, self.config["quality"])
            if not audio_url:
                await event.send(MessageChain([Plain(f"喵~ 这首歌可能需要VIP或者没有版权，暂时不能为主人播放呢...")]))
                return

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"

            await self._send_song_messages(event, num, title, artists, album, dur_str, cover_url, audio_url)

        except Exception as e:
            logger.error(f"网易云点歌插件：播放歌曲 {song_id} 失败。错误：{e!s}")
            await event.send(MessageChain([Plain(f"呜...获取歌曲信息的时候失败了喵...")]))
        finally:
            if cache_key in self.song_cache:
                del self.song_cache[cache_key]
            self._remove_cache_key_mappings(cache_key)

    async def _send_song_messages(self, event: AstrMessageEvent, num: int, title: str, artists: str, album: str, dur_str: str, cover_url: str, audio_url: str):
        """构造并发送歌曲信息与语音消息。"""
        if self.image_renderer and self.config.get("song_detail_image", True):
            img_bytes = await self.image_renderer.render_song_detail_image(
                title=title,
                artists=artists,
                album=album,
                duration=dur_str,
                quality=self.config.get("quality", ""),
                cover_url=cover_url,
            )
            if img_bytes:
                await event.send(MessageChain([Image.fromBase64(base64.b64encode(img_bytes).decode())]))
            else:
                await self._send_song_detail_text(event, num, title, artists, album, dur_str, cover_url)
        else:
            await self._send_song_detail_text(event, num, title, artists, album, dur_str, cover_url)

        await event.send(MessageChain([Record(file=audio_url)]))

    async def _send_song_detail_text(self, event: AstrMessageEvent, num: int, title: str, artists: str, album: str, dur_str: str, cover_url: str):
        detail_text = f"""遵命，主人！为您播放第 {num} 首歌曲~

♪ 歌名：{title}
🎤 歌手：{artists}
💿 专辑：{album}
⏳ 时长：{dur_str}
✨ 音质：{self.config.get('quality', '')}

请主人享用喵~
"""
        info_components = [Plain(detail_text)]
        image_data = await self.api.download_image(cover_url)
        if image_data:
            info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))
        await event.send(MessageChain(info_components))
