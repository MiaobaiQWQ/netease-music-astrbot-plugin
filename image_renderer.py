import asyncio
import io
from typing import Any, Awaitable, Callable, Dict, List, Optional


class SearchResultImageRenderer:
    def __init__(
        self,
        download_image: Callable[[str, float], Awaitable[Optional[bytes]]],
    ) -> None:
        self._download_image = download_image
        self._fonts: Optional[Dict[str, Any]] = None

    def _get_pil_fonts(self) -> Dict[str, Any]:
        if self._fonts is not None:
            return self._fonts

        try:
            from PIL import ImageFont
        except Exception:
            self._fonts = {}
            return self._fonts

        candidates = [
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\msyh.ttf",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
            "msyh.ttc",
            "msyh.ttf",
            "simhei.ttf",
            "simsun.ttc",
        ]

        def load(size: int):
            for p in candidates:
                try:
                    return ImageFont.truetype(p, size, index=0)
                except Exception:
                    try:
                        return ImageFont.truetype(p, size)
                    except Exception:
                        continue
            try:
                return ImageFont.load_default()
            except Exception:
                return None

        self._fonts = {
            "title": load(30),
            "sub": load(14),
            "name": load(18),
            "info": load(13),
            "idx": load(16),
            "footer": load(12),
        }
        return self._fonts

    async def render_search_result_image(
        self,
        keyword: str,
        items: List[Dict[str, str]],
        include_cover: bool,
        cover_timeout: float = 5,
    ) -> Optional[bytes]:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw, ImageFont
        except Exception:
            return None

        fonts = self._get_pil_fonts()
        if not fonts:
            return None

        width = 720
        padding = 24
        header_h = 118
        item_h = 92
        gap = 12
        footer_h = 56
        count = max(1, len(items))
        height = padding + header_h + gap + count * item_h + max(0, count - 1) * gap + footer_h + padding

        bg_start = (255, 245, 248)
        bg_end = (245, 248, 255)

        img = PILImage.new("RGB", (width, height), bg_start)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            r = int(bg_start[0] + (bg_end[0] - bg_start[0]) * y / max(1, height - 1))
            g = int(bg_start[1] + (bg_end[1] - bg_start[1]) * y / max(1, height - 1))
            b = int(bg_start[2] + (bg_end[2] - bg_start[2]) * y / max(1, height - 1))
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        draw.rectangle([padding, padding, width - padding, padding + 6], fill=(255, 107, 157))

        def text_w(text: str, font) -> int:
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                return bbox[2] - bbox[0]
            except Exception:
                return len(text) * 10

        def ellipsize(text: str, font, max_w: int) -> str:
            if text_w(text, font) <= max_w:
                return text
            suffix = "…"
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi) // 2
                candidate = text[:mid] + suffix
                if text_w(candidate, font) <= max_w:
                    lo = mid + 1
                else:
                    hi = mid
            cut = max(0, lo - 1)
            return text[:cut] + suffix

        title = "音乐搜索"
        title_font = fonts.get("title") or ImageFont.load_default()
        sub_font = fonts.get("sub") or ImageFont.load_default()

        title_x = (width - text_w(title, title_font)) // 2
        draw.text((title_x, padding + 18), title, fill=(102, 78, 163), font=title_font)

        sub = f"搜索：{keyword} · 共 {len(items)} 首"
        sub_x = (width - text_w(sub, sub_font)) // 2
        draw.text((sub_x, padding + 58), sub, fill=(142, 142, 142), font=sub_font)

        cover_bytes_list: List[Optional[bytes]] = [None] * len(items)
        if include_cover and items:
            tasks = []
            for it in items:
                url = (it.get("cover_url") or "").strip()
                tasks.append(self._download_image(url, cover_timeout) if url else asyncio.sleep(0, result=None))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, (bytes, bytearray)) and r:
                    cover_bytes_list[i] = bytes(r)

        y = padding + header_h + gap
        for idx, it in enumerate(items, 1):
            left = padding
            top = y
            right = width - padding
            bottom = y + item_h

            try:
                draw.rounded_rectangle([left, top, right, bottom], radius=16, fill=(255, 255, 255), outline=(230, 230, 250), width=2)
            except Exception:
                draw.rectangle([left, top, right, bottom], fill=(255, 255, 255), outline=(230, 230, 250), width=2)

            circle_cx = left + 30
            circle_cy = top + item_h // 2
            rr = 18
            draw.ellipse([circle_cx - rr, circle_cy - rr, circle_cx + rr, circle_cy + rr], fill=(255, 107, 157))

            idx_font = fonts.get("idx") or ImageFont.load_default()
            idx_text = str(idx)
            idx_x = circle_cx - text_w(idx_text, idx_font) // 2
            draw.text((idx_x, circle_cy - 10), idx_text, fill=(255, 255, 255), font=idx_font)

            cover_x = left + 62
            cover_y = top + (item_h - 64) // 2
            draw.ellipse([cover_x, cover_y, cover_x + 64, cover_y + 64], fill=(243, 243, 255), outline=(230, 230, 250), width=1)

            cover_bytes = cover_bytes_list[idx - 1] if idx - 1 < len(cover_bytes_list) else None
            if include_cover and cover_bytes:
                try:
                    cover_img = PILImage.open(io.BytesIO(cover_bytes)).convert("RGBA")
                    cover_img = cover_img.resize((64, 64), PILImage.Resampling.LANCZOS)
                    mask = PILImage.new("L", (64, 64), 0)
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse([0, 0, 64, 64], fill=255)
                    cover_img.putalpha(mask)
                    img.paste(cover_img, (cover_x, cover_y), cover_img)
                except Exception:
                    cover_bytes = None

            if not cover_bytes:
                placeholder_font = fonts.get("idx") or ImageFont.load_default()
                placeholder = "♪"
                name0 = (it.get("name") or "").strip()
                if name0:
                    placeholder = name0[0]
                px = cover_x + (64 - text_w(placeholder, placeholder_font)) // 2
                py = cover_y + 20
                draw.text((px, py), placeholder, fill=(180, 180, 180), font=placeholder_font)

            name_font = fonts.get("name") or ImageFont.load_default()
            info_font = fonts.get("info") or ImageFont.load_default()

            text_x = left + 140
            max_text_w = right - text_x - 14
            name = ellipsize(it.get("name") or "", name_font, max_text_w)
            artists = it.get("artists") or ""
            album = it.get("album") or ""
            duration = it.get("duration") or ""
            info1 = ellipsize(f"{artists} · {album}", info_font, max_text_w)
            info2 = ellipsize(f"时长：{duration}", info_font, max_text_w)

            draw.text((text_x, top + 18), name, fill=(68, 68, 68), font=name_font)
            draw.text((text_x, top + 44), info1, fill=(142, 142, 142), font=info_font)
            draw.text((text_x, top + 62), info2, fill=(142, 142, 142), font=info_font)

            y += item_h + gap

        footer_font = fonts.get("footer") or ImageFont.load_default()
        footer_text = "回复序号即可播放，例如：1"
        footer_x = (width - text_w(footer_text, footer_font)) // 2
        draw.text((footer_x, height - padding - 26), footer_text, fill=(180, 180, 180), font=footer_font)

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()

    async def render_song_detail_image(
        self,
        title: str,
        artists: str,
        album: str,
        duration: str,
        quality: str,
        cover_url: str,
        cover_timeout: float = 6,
    ) -> Optional[bytes]:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw, ImageFont
        except Exception:
            return None

        fonts = self._get_pil_fonts()
        if not fonts:
            return None

        width = 900
        padding = 32
        header_h = 110
        card_h = 320
        footer_h = 52
        height = padding + header_h + 18 + card_h + 18 + footer_h + padding

        bg_start = (255, 245, 248)
        bg_end = (245, 248, 255)

        img = PILImage.new("RGB", (width, height), bg_start)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            r = int(bg_start[0] + (bg_end[0] - bg_start[0]) * y / max(1, height - 1))
            g = int(bg_start[1] + (bg_end[1] - bg_start[1]) * y / max(1, height - 1))
            b = int(bg_start[2] + (bg_end[2] - bg_start[2]) * y / max(1, height - 1))
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        def text_w(text: str, font) -> int:
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                return bbox[2] - bbox[0]
            except Exception:
                return len(text) * 10

        def ellipsize(text: str, font, max_w: int) -> str:
            if text_w(text, font) <= max_w:
                return text
            suffix = "…"
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi) // 2
                candidate = text[:mid] + suffix
                if text_w(candidate, font) <= max_w:
                    lo = mid + 1
                else:
                    hi = mid
            cut = max(0, lo - 1)
            return text[:cut] + suffix

        title_font = fonts.get("title") or ImageFont.load_default()
        sub_font = fonts.get("sub") or ImageFont.load_default()
        name_font = fonts.get("name") or ImageFont.load_default()
        info_font = fonts.get("info") or ImageFont.load_default()
        footer_font = fonts.get("footer") or ImageFont.load_default()

        header_title = "正在播放"
        hx = (width - text_w(header_title, title_font)) // 2
        draw.text((hx, padding + 14), header_title, fill=(102, 78, 163), font=title_font)

        sub = "网易云音乐"
        sx = (width - text_w(sub, sub_font)) // 2
        draw.text((sx, padding + 64), sub, fill=(142, 142, 142), font=sub_font)

        card_left = padding
        card_top = padding + header_h + 18
        card_right = width - padding
        card_bottom = card_top + card_h

        try:
            draw.rounded_rectangle([card_left, card_top, card_right, card_bottom], radius=22, fill=(255, 255, 255), outline=(230, 230, 250), width=2)
        except Exception:
            draw.rectangle([card_left, card_top, card_right, card_bottom], fill=(255, 255, 255), outline=(230, 230, 250), width=2)

        cover_size = 210
        cover_x = card_left + 34
        cover_y = card_top + (card_h - cover_size) // 2
        draw.rounded_rectangle([cover_x, cover_y, cover_x + cover_size, cover_y + cover_size], radius=24, fill=(243, 243, 255), outline=(230, 230, 250), width=1)

        cover_bytes: Optional[bytes] = None
        url0 = (cover_url or "").strip()
        if url0:
            try:
                r = await self._download_image(url0, cover_timeout)
                if isinstance(r, (bytes, bytearray)) and r:
                    cover_bytes = bytes(r)
            except Exception:
                cover_bytes = None

        if cover_bytes:
            try:
                cover_img = PILImage.open(io.BytesIO(cover_bytes)).convert("RGB")
                cover_img = cover_img.resize((cover_size, cover_size), PILImage.Resampling.LANCZOS)
                img.paste(cover_img, (cover_x, cover_y))
            except Exception:
                cover_bytes = None

        if not cover_bytes:
            placeholder = "♪"
            t0 = (title or "").strip()
            if t0:
                placeholder = t0[0]
            px = cover_x + (cover_size - text_w(placeholder, title_font)) // 2
            py = cover_y + (cover_size - 30) // 2
            draw.text((px, py), placeholder, fill=(180, 180, 180), font=title_font)

        text_x = cover_x + cover_size + 32
        max_text_w = card_right - text_x - 28

        title0 = ellipsize((title or "").strip(), name_font, max_text_w)
        draw.text((text_x, card_top + 54), title0, fill=(68, 68, 68), font=name_font)

        artists0 = ellipsize(f"歌手：{(artists or '').strip()}", info_font, max_text_w)
        album0 = ellipsize(f"专辑：{(album or '').strip()}", info_font, max_text_w)
        dur0 = ellipsize(f"时长：{(duration or '').strip()}", info_font, max_text_w)
        q0 = ellipsize(f"音质：{(quality or '').strip()}", info_font, max_text_w)

        info_y = card_top + 112
        line_gap = 34
        for line in [artists0, album0, dur0, q0]:
            if line.strip() and not line.endswith("："):
                draw.text((text_x, info_y), line, fill=(142, 142, 142), font=info_font)
                info_y += line_gap

        foot = "喜欢的话，记得收藏喵~"
        fx = (width - text_w(foot, footer_font)) // 2
        draw.text((fx, height - padding - 28), foot, fill=(180, 180, 180), font=footer_font)

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
