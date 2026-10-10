"""拼多多素材空间（图片空间 / 视频空间）API 封装。

接口来源与逐字段实测记录见 `docs/material-space-send-research-2026-10-11.md`。

关键结论（实测）：
- 「图片空间」与「视频空间」是同一套 `garner` 服务下的两个独立库，靠 `dir_id` 区分，
  容量各自独立计算（图片空间 10GB / 视频空间 20GB）。
- 图片空间：`dir_id` **省略** = 全店素材（含各文件夹）；`dir_id=0` = 仅根目录。
- 视频空间（客服专用视频）：**`dir_id = -1`**（不在文件夹列表 `dirListV2` 里的隐藏目录）。
- 列表接口 **不需要 anti-content**，仅需 cookies。
- 素材项 `id` == 消息 `info.file_id`；发视频的 `download_url` 取素材 `transcode_url`（`.f30.mp4`）。

所有请求均为只读（list / dir / sumSize），不会改动素材库。
"""

from typing import Any, Dict, List, Optional

from ..base_request import BaseRequest

_BASE_URL = "https://mms.pinduoduo.com"

# 空间标识
SPACE_IMAGE = "image"
SPACE_VIDEO = "video"

# 空间 → 列表接口 dir_id（None 表示该字段整个省略，即"全店素材"）
_LIST_DIR_ID: Dict[str, Optional[int]] = {
    SPACE_IMAGE: None,
    SPACE_VIDEO: -1,
}

# 空间 → 容量接口 dir_id（sumSize 只认具体值）
_SUM_SIZE_DIR_ID: Dict[str, int] = {
    SPACE_IMAGE: 0,
    SPACE_VIDEO: -1,
}

# 审核通过状态码（前端会过滤掉其它状态；本题材库实测 2 = 已通过）
CHECK_STATUS_PASSED = 2

# 素材列表页面的 referer（与前端一致，降低被风控概率）
_REQUEST_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://mms.pinduoduo.com",
    "referer": "https://mms.pinduoduo.com/material/index.html",
}

_BYTES_PER_MB = 1048576.0


class MaterialSpace(BaseRequest):
    """素材空间（图片空间 / 视频空间）只读接口。

    用法::

        ms = MaterialSpace(shop_id, user_id)
        data = ms.list_files(space=MaterialSpace.SPACE_VIDEO, page=1, page_size=30)
        for item in data["items"]:
            ...  # item 已归一化

    说明：本类的实例化会从数据库读取账号 cookies（同步 DB 操作），
    请在后台线程中创建/使用，不要在 UI 主线程直接调用。
    """

    # 供外部引用
    SPACE_IMAGE = SPACE_IMAGE
    SPACE_VIDEO = SPACE_VIDEO

    def __init__(self, shop_id: Optional[str] = None, user_id: Optional[str] = None, cookies=None):
        super().__init__(shop_id=shop_id, user_id=user_id)
        if cookies:
            self.update_cookies(cookies)
        if not self.cookies:
            self.logger.warning(
                "素材空间接口缺少 cookies，请求大概率失败 (shop_id=%s, user_id=%s)",
                shop_id, user_id,
            )

    # ------------------------------------------------------------------ #
    # 列表
    # ------------------------------------------------------------------ #

    def list_files(
        self,
        space: str = SPACE_IMAGE,
        page: int = 1,
        page_size: int = 30,
        keyword: str = "",
        dir_id: Optional[int] = None,
        order_by: str = "",
    ) -> Dict[str, Any]:
        """拉取素材列表。

        Args:
            space: ``"image"``（图片空间）或 ``"video"``（视频空间）
            page: 页码，从 1 开始
            page_size: 每页数量
            keyword: 文件名关键字
            dir_id: 指定文件夹 id；为 None 时用该空间的默认值
                （图片空间=省略即全店；视频空间=-1）
            order_by: 排序字段，留空为默认排序

        Returns:
            ``{"success": bool, "items": [归一化素材项], "total": int,
               "page": int, "page_size": int, "error_msg": str}``
        """
        space = self._normalize_space(space)
        body: Dict[str, Any] = {
            "page": max(1, int(page)),
            "page_size": max(1, int(page_size)),
            "file_name": keyword or "",
            "check_status_list": [],
            "order_by": order_by or "",
            "tool_source": "",
        }
        effective_dir = _LIST_DIR_ID.get(space) if dir_id is None else dir_id
        if effective_dir is not None:
            body["dir_id"] = effective_dir

        result = self.post(
            f"{_BASE_URL}/garner/mms/file/list",
            json_data=body,
            headers=_REQUEST_HEADERS,
        )

        if not result or not result.get("success"):
            error_msg = ""
            if isinstance(result, dict):
                error_msg = str(
                    (result.get("result") or {}).get("error")
                    or result.get("error_msg")
                    or result
                )
            else:
                error_msg = "请求失败（无响应）"
            self.logger.error("获取素材列表失败 (space=%s, page=%s): %s", space, page, error_msg)
            return {
                "success": False,
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "error_msg": error_msg,
            }

        inner = result.get("result") or {}
        raw_list = inner.get("list") or []
        items: List[Dict[str, Any]] = []
        for raw in raw_list:
            norm = self._normalize_item(raw)
            if norm:
                items.append(norm)

        return {
            "success": True,
            "items": items,
            "total": int(inner.get("total") or len(items)),
            "page": page,
            "page_size": page_size,
            "error_msg": "",
        }

    # ------------------------------------------------------------------ #
    # 文件夹 / 容量
    # ------------------------------------------------------------------ #

    def list_dirs(self) -> List[Dict[str, Any]]:
        """拉取文件夹清单。

        Returns:
            ``[{"id": int, "name": str, "parent_dir_id": int,
                "child_file_count": int, "child_dir_count": int}]``
            （失败返回空列表）
        """
        result = self.post(
            f"{_BASE_URL}/garner/mms/dir/dirListV2",
            json_data={"page": 1, "page_size": 100},
            headers=_REQUEST_HEADERS,
        )
        if not result or not result.get("success"):
            self.logger.error("获取素材文件夹列表失败: %s", result)
            return []

        raw = result.get("result")
        # result 可能是 list，也可能包在 dict 中
        if isinstance(raw, dict):
            raw = raw.get("list") or raw.get("dir_list") or []
        if not isinstance(raw, list):
            return []

        dirs: List[Dict[str, Any]] = []
        for d in raw:
            if not isinstance(d, dict):
                continue
            dirs.append({
                "id": d.get("id"),
                "name": d.get("name") or "",
                "parent_dir_id": d.get("parent_dir_id"),
                "child_file_count": d.get("child_file_count") or 0,
                "child_dir_count": d.get("child_dir_count") or 0,
            })
        return dirs

    def sum_size(self, space: str = SPACE_IMAGE) -> Dict[str, Any]:
        """查询空间容量。

        Returns:
            ``{"success": bool, "sum_size": int, "max_size": int, "error_msg": str}``
            （单位：字节）
        """
        space = self._normalize_space(space)
        result = self.post(
            f"{_BASE_URL}/garner/mms/file/sumSize",
            json_data={"dir_id": _SUM_SIZE_DIR_ID.get(space, 0)},
            headers=_REQUEST_HEADERS,
        )
        if not result or not result.get("success"):
            return {"success": False, "sum_size": 0, "max_size": 0,
                    "error_msg": str(result)}
        inner = result.get("result") or {}
        return {
            "success": True,
            "sum_size": int(inner.get("sum_size") or 0),
            "max_size": int(inner.get("max_size") or 0),
            "error_msg": "",
        }

    # ------------------------------------------------------------------ #
    # 归一化 / 发送元数据构造
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_space(space: str) -> str:
        """容错处理空间标识（兼容 "pic"/"video"/"img" 等写法）"""
        s = (space or "").strip().lower()
        if s in ("video", "videos", "vid", "out_video", "service_video"):
            return SPACE_VIDEO
        return SPACE_IMAGE

    @staticmethod
    def _normalize_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把接口原始素材项整理成 UI 友好的结构。

        保留字段：id/name/file_type/url/transcode_url/extension/size/
        check_status/create_time/duration/width/height/cover_url/dir_names
        """
        if not isinstance(raw, dict) or raw.get("id") is None:
            return None

        extra = raw.get("extra_info") or {}
        if not isinstance(extra, dict):
            extra = {}

        file_type = str(raw.get("file_type") or "").lower()
        if file_type not in ("pic", "video"):
            # 兜底：按扩展名判断
            ext = str(raw.get("extension") or "").lower()
            file_type = "video" if ext in ("mp4", "mov", "avi", "mkv") else "pic"

        dir_names: List[str] = []
        for d in (raw.get("mall_dir_name_list") or []):
            if isinstance(d, dict) and d.get("name"):
                dir_names.append(str(d["name"]))

        return {
            "id": str(raw.get("id")),
            "name": str(raw.get("name") or ""),
            "file_type": file_type,
            "url": raw.get("url") or "",
            "transcode_url": raw.get("transcode_url") or raw.get("url") or "",
            "extension": str(raw.get("extension") or ""),
            "size": int(raw.get("size") or 0),
            "check_status": raw.get("check_status"),
            "create_time": raw.get("create_time"),
            "duration": extra.get("duration"),
            "width": extra.get("width"),
            "height": extra.get("height"),
            "cover_url": extra.get("video_cover_url") or "",
            "dir_names": dir_names,
        }

    @staticmethod
    def is_sendable(item: Dict[str, Any]) -> bool:
        """素材是否可发送（审核通过）。

        ``check_status`` 为 None 时按可发送处理（部分老素材不返回该字段）。
        """
        status = item.get("check_status")
        return status is None or status == CHECK_STATUS_PASSED

    @staticmethod
    def build_video_info(item: Dict[str, Any]) -> Dict[str, Any]:
        """从素材项构造 PDD `send_message` 的视频 ``info`` 字段。

        映射规则（逐字段实测，见调研文档 §3.2）：
            file_id      ← 素材 id
            download_url ← 素材 transcode_url（.f30.mp4 转码版）
            duration     ← extra_info.duration
            size         ← size / 1048576（单位 MB）
            preview.url  ← extra_info.video_cover_url
            preview.size ← extra_info.width / height
            status       ← 固定 0
        """
        size_bytes = int(item.get("size") or 0)
        duration = item.get("duration")
        return {
            "download_url": item.get("transcode_url") or item.get("url") or "",
            "duration": int(duration) if duration else 0,
            "file_id": str(item.get("id") or ""),
            "preview": {
                "url": item.get("cover_url") or "",
                "size": {
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                },
            },
            "size": round(size_bytes / _BYTES_PER_MB, 2),
            "status": 0,
        }

    @staticmethod
    def build_send_payload(item: Dict[str, Any]) -> Dict[str, Any]:
        """给出「发送该素材」所需的字段集合，供发送层直接消费。

        Returns:
            ``{"context_type": "image"|"video", "url": str, "info": dict|None,
               "media_meta": str|None}``
            - 图片：``url`` = 素材 url，``info`` = None（PDD 图片消息非必需）
            - 视频：``url`` = download_url，``info`` = 完整 info，
              ``media_meta`` = 供入库/转发的 JSON（含 raw_info）
        """
        if str(item.get("file_type") or "").lower() == "video":
            info = MaterialSpace.build_video_info(item)
            media_meta = None
            try:
                import json
                media_meta = json.dumps(
                    {
                        "raw_info": info,
                        "duration": info.get("duration"),
                        "cover_url": (info.get("preview") or {}).get("url"),
                        "cover_size": (info.get("preview") or {}).get("size"),
                        "file_id": info.get("file_id"),
                        "name": item.get("name") or "",
                    },
                    ensure_ascii=False,
                )
            except Exception:
                media_meta = None
            return {
                "context_type": "video",
                "url": info["download_url"],
                "info": info,
                "media_meta": media_meta,
            }

        return {
            "context_type": "image",
            "url": item.get("url") or item.get("transcode_url") or "",
            "info": None,
            "media_meta": None,
        }


__all__ = ["MaterialSpace", "SPACE_IMAGE", "SPACE_VIDEO", "CHECK_STATUS_PASSED"]
