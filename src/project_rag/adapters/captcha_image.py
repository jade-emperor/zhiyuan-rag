"""验证码渲染(Pillow)。无自定义字体依赖:用 Pillow 自带的可缩放默认字体。"""

from __future__ import annotations

import io
import secrets

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 132, 44
_FONT_SIZE = 28


def _rand(low: int, high: int) -> int:
    """输入:区间。行为:取 ``[low, high)`` 内的随机整数。输出:整数。"""
    return low + secrets.randbelow(max(1, high - low))


class ImageCaptchaRenderer:
    """画一张带干扰的 PNG:字符逐个旋转错位,叠加干扰线与噪点。"""

    def render(self, text: str) -> bytes:
        """输入:验证码文本。行为:合成图片。输出:PNG 字节。"""
        image = Image.new("RGB", (WIDTH, HEIGHT), self._background())
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=_FONT_SIZE)

        step = WIDTH // (len(text) + 1)
        for index, char in enumerate(text):
            glyph = Image.new("RGBA", (_FONT_SIZE + 8, _FONT_SIZE + 10), (0, 0, 0, 0))
            ImageDraw.Draw(glyph).text((4, 2), char, font=font, fill=self._ink())
            glyph = glyph.rotate(secrets.choice((-18, -12, -6, 6, 12, 18)), expand=True)
            image.paste(glyph, (step * (index + 1) - 6, secrets.choice((2, 5, 8))), glyph)

        for _ in range(3):
            draw.line(
                [
                    (_rand(0, WIDTH), _rand(0, HEIGHT)),
                    (_rand(0, WIDTH), _rand(0, HEIGHT)),
                ],
                fill=self._noise(),
                width=1,
            )
        for _ in range(60):
            draw.point((_rand(0, WIDTH), _rand(0, HEIGHT)), fill=self._noise())

        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    @staticmethod
    def _background() -> tuple[int, int, int]:
        base = _rand(232, 250)
        return (base, base, min(255, base + 2))

    @staticmethod
    def _ink() -> tuple[int, int, int, int]:
        return (_rand(10, 70), _rand(30, 90), _rand(90, 160), 255)

    @staticmethod
    def _noise() -> tuple[int, int, int]:
        level = _rand(120, 190)
        return (level, level, level)
