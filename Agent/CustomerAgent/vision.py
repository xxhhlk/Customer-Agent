"""图片内容理解：把一张图变成一句中文描述。

两个使用方：
- ``material_context``：给素材目录补「名称 —— 内容」描述，模型据名 + 描述挑素材；
- ``tools/view_material``：模型拿不准时按需查看素材实际画面。

注意：这里**不复用** ``agent.model.client``。多账号各自在线程里跑事件循环，
共用 httpx 连接池会跨 loop 报错；每次调用新建 ``AsyncOpenAI``，用完即关。
"""

import asyncio
from typing import List

from utils.logger_loguru import get_logger

logger = get_logger("Vision")

# 单张图描述调用超时（秒）
VISION_TIMEOUT = 30.0
# 描述文本长度上限（超长截断，避免把 prompt 撑大）
MAX_DESC_LEN = 60

DEFAULT_INSTRUCTION = (
    "用一句不超过25字的中文描述这张图片的内容（画面主体、关键界面或文字）。"
    "只客观描述，不要评价，不要加引号。"
)


def _clean(text: str) -> str:
    """压成一行、去引号、限长"""
    text = " ".join(str(text or "").split())
    text = text.strip(" \t\"'“”‘’。")
    if len(text) > MAX_DESC_LEN:
        text = text[:MAX_DESC_LEN].rstrip() + "…"
    return text


async def describe_image(url: str, instruction: str = "") -> str:
    """调视觉模型描述一张图；失败返回空串（调用方按「无描述」处理）。"""
    image = str(url or "").strip()
    if not image.startswith(("http://", "https://", "data:image")):
        return ""
    try:
        from config import config

        model = str(config.get("llm.model_name") or "").strip()
        base = str(config.get("llm.api_base") or "").strip()
        key = str(config.get("llm.api_key") or "").strip()
        if not (model and base and key):
            return ""

        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=key, base_url=base, timeout=VISION_TIMEOUT)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction or DEFAULT_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }],
                max_tokens=120,
            )
        finally:
            await client.close()
        return _clean(resp.choices[0].message.content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片描述失败: {type(e).__name__} {e}")
        return ""


async def describe_images(
    urls: List[str], concurrency: int = 3, instruction: str = ""
) -> List[str]:
    """批量描述（限并发），返回顺序与入参一致，失败位置为空串。"""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(u: str) -> str:
        async with sem:
            return await describe_image(u, instruction)

    return list(await asyncio.gather(*(one(u) for u in urls)))


__all__ = ["describe_image", "describe_images", "MAX_DESC_LEN", "VISION_TIMEOUT"]
