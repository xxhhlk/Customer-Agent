"""视频抽帧：给不支持原生视频理解的模型提供帧序列输入。

背景与实测依据（见 docs 与 temp/frame_*_probe.py）：
- 均匀 fps 抽帧在手机录屏/拍屏上浪费严重：fps=1 时约 42% 的帧是同一屏的复制。
- 裸 scene 检测有时间覆盖缺陷：只取前 N 帧时可能只覆盖前 28% 的时间轴。
- 因此采用「一次抽全部候选 + 时间分层选择」：
    1. ffmpeg 一次抽出所有 scene>threshold 的候选帧（带 pts + scene_score）
    2. 时间轴切 N 个等宽窗口，每窗口取 score 最高的候选
    3. 无候选的窗口取窗口中点补 1 帧（静止页面上的静态信息也要覆盖）
    4. 保底帧去重（相邻窗口落到同一屏时只留一张）

并发：ffmpeg 解码吃满单核，4GB 机器上必须串行，模块级 BoundedSemaphore(1) 限流。
注意不能用 asyncio.Semaphore —— 每个账号的 AutoReplyThread 有独立事件循环，
模块级 asyncio 原语会绑定到第一个 loop，跨 loop 复用直接抛
"bound to a different event loop"。线程信号量配合 asyncio.to_thread 天然安全。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
from typing import List, Optional, Tuple

from utils.logger_loguru import get_logger
from utils.runtime_path import ensure_temp_dir

logger = get_logger("VideoFrames")

# ffmpeg 同时只允许一个实例：单核解码 + 4GB 机器的内存约束
_FFMPEG_SEM = threading.BoundedSemaphore(1)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 候选帧扫描超时（解码全片，1080p/113s 实测约 30s，留 3 倍余量）
_SCAN_TIMEOUT = 120
# 单帧定点补抽超时
_SEEK_TIMEOUT = 20

# 帧目录放在 temp 下，启动时整体清空
_FRAME_ROOT = "video_frames"

_FFMPEG_CANDIDATES = (
    os.path.join("imageio_ffmpeg", "binaries"),   # imageio-ffmpeg 打包后的标准位置
    "ffmpeg.exe",
)


def _sem_run(cmd: List[str], timeout: int) -> Tuple[int, str]:
    """串行执行 ffmpeg，返回 (returncode, 合并输出)。"""
    with _FFMPEG_SEM:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def resolve_ffmpeg() -> Optional[str]:
    """定位 ffmpeg 可执行文件。

    打包后 imageio-ffmpeg 的 binaries 目录由 PyInstaller 解压到 sys._MEIPASS，
    其自带 API 依赖 __file__ 推导路径，在 onedir 模式下不一定可靠，故显式探测。
    """
    import sys

    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    try:
        import imageio_ffmpeg

        roots.append(os.path.dirname(os.path.dirname(imageio_ffmpeg.__file__)))
        # 开发态：直接问库要路径
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass

    for root in roots:
        for sub in _FFMPEG_CANDIDATES:
            p = os.path.join(root, sub)
            if os.path.isfile(p):
                return p
            if os.path.isdir(p):
                hits = sorted(glob.glob(os.path.join(p, "ffmpeg*.exe")))
                if hits:
                    return hits[-1]

    # 最后查系统 PATH
    found = shutil.which("ffmpeg")
    if found:
        return found
    return None


def _parse_duration(text: str) -> Optional[float]:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def probe_duration(video_path: str, ffmpeg: str) -> Optional[float]:
    """ffmpeg -i 解析时长（不产出帧，仅读元数据）。"""
    try:
        _, out = _sem_run([ffmpeg, "-hide_banner", "-i", video_path], _SEEK_TIMEOUT)
    except Exception as e:
        logger.warning(f"[video] probe 失败: {e}")
        return None
    return _parse_duration(out)


def download_video(
    url: str,
    dest: str,
    max_bytes: int,
    timeout: int = 60,
) -> Optional[str]:
    """下载视频到本地。走与聊天媒体一致的代理策略。

    不能把 URL 直接交给 ffmpeg：项目代理是 socks5://，ffmpeg 不认；
    PDD CDN 也可能校验 Referer/UA。统一由 requests 下载。
    """
    import requests

    from utils.proxy_config import get_media_proxies

    try:
        with requests.get(
            url, timeout=timeout, stream=True, proxies=get_media_proxies()
        ) as resp:
            resp.raise_for_status()
            total = 0
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        f.close()
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        logger.warning(
                            f"[video] 视频超过 {max_bytes / 1048576:.0f}MB，跳过抽帧"
                        )
                        return None
                    f.write(chunk)
            os.replace(tmp, dest)
            return dest
    except Exception as e:
        logger.warning(f"[video] 下载失败: {e}")
        return None


def _extract_candidates(
    video_path: str, out_dir: str, threshold: float, width: int, ffmpeg: str
) -> List[Tuple[str, float, float]]:
    """一次抽出所有 scene>threshold 的候选帧，返回 [(路径, 时间, 分数)]。"""
    p = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-y",
            "-i", video_path,
            "-vf", f"select='gt(scene,{threshold})',scale={width}:-2,showinfo",
            "-fps_mode", "vfr", "-q:v", "4",
            os.path.join(out_dir, "%04d.jpg"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=_SCAN_TIMEOUT,
        creationflags=_CREATE_NO_WINDOW,
    )
    files = sorted(glob.glob(os.path.join(out_dir, "*.jpg")))
    if not files:
        return []

    raw = p.stderr or ""
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", raw)]
    scores = [float(x) for x in re.findall(r"lavfi\.scene_score=([\d.]+)", raw)]

    def _align(seq: List[float]) -> List[float]:
        # showinfo 的行数可能与产出帧数不完全一致，取尾部对齐，缺失补 0
        if len(seq) >= len(files):
            return seq[-len(files):]
        return seq + [0.0] * (len(files) - len(seq))

    times, scores = _align(times), _align(scores)
    return [(f, t, s) for f, t, s in zip(files, times, scores)]


def _layered_select(
    cands: List[Tuple[str, float, float]], duration: float, target: int
) -> List[Tuple[str, float, float, bool]]:
    """时间轴切 target 个等宽窗口，每窗口取分数最高的候选。

    返回 [(路径, 时间, 分数, 是否复用)]。没有候选的窗口说明画面静止，
    直接复用相邻窗口的候选帧（而不是重新 seek 解码）——静止窗口的画面本来就一样，
    复用既省掉一次子进程启动（实测 35 个保底窗口要 38s），语义也无损。
    """
    if duration <= 0:
        duration = cands[-1][1] if cands else 1.0
    win = duration / target
    buckets: List[List[Tuple[str, float, float]]] = [[] for _ in range(target)]
    for f, t, s in cands:
        idx = min(int(t / win), target - 1)
        buckets[idx].append((f, t, s))

    picked: List[Tuple[str, float, float, bool]] = []
    last: Optional[Tuple[str, float, float]] = None
    for i, b in enumerate(buckets):
        if b:
            best = max(b, key=lambda x: x[2])
            picked.append((best[0], best[1], best[2], False))
            last = best
        elif last is not None:
            picked.append((last[0], (i + 0.5) * win, last[2], True))
        else:
            picked.append(("", (i + 0.5) * win, 0.0, True))

    # 首个有效候选之前的空窗口，回填第一个候选，避免开头整段缺失
    first = next((p for p in picked if p[0]), None)
    if first is not None:
        picked = [first if not p[0] else p for p in picked]
    return picked


def _fill_gaps(
    video_path: str,
    picked: List[Tuple[str, float, float, bool]],
    out_dir: str,
    width: int,
    ffmpeg: str,
) -> List[Tuple[str, bool]]:
    """把选择结果落成 (路径, 是否复用/保底)。

    正常情况下 picked 里空的路径已被 _layered_select 回填，走到 seek 的只有
    「整段视频没有任何画面变化」这种极端情况，因此这里通常不产生子进程。
    """
    fill_dir = os.path.join(out_dir, "_fill")
    out: List[Tuple[str, bool]] = []
    for i, (f, t, _s, reused) in enumerate(picked):
        if f and os.path.exists(f):
            out.append((f, reused))
            continue
        os.makedirs(fill_dir, exist_ok=True)
        dst = os.path.join(fill_dir, f"f{i:04d}.jpg")
        try:
            _sem_run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t:.2f}", "-i", video_path,
                    "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", dst,
                ],
                _SEEK_TIMEOUT,
            )
        except Exception as e:
            logger.warning(f"[video] 补帧失败 t={t:.1f}: {e}")
            continue
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            out.append((dst, True))
    return out


def _dedup(pairs: List[Tuple[str, bool]], thresh: float = 3.0) -> List[str]:
    """保底帧去重：与已保留帧的 32x32 灰度平均差小于阈值视为同一画面。

    只对相邻的保底帧生效——scene 候选帧是模型判断过的事件帧，全部保留。
    """
    import numpy as np
    from PIL import Image

    kept: List[str] = []
    last_fill: Optional[np.ndarray] = None
    for path, is_fill in pairs:
        if not is_fill:
            kept.append(path)
            last_fill = None
            continue
        try:
            arr = np.asarray(
                Image.open(path).convert("L").resize((32, 32)), dtype=np.float32
            )
        except Exception:
            continue
        if last_fill is not None and float(np.abs(arr - last_fill).mean()) < thresh:
            continue  # 静止画面重复出现，跳过
        kept.append(path)
        last_fill = arr
    return kept


def _to_data_urls(files: List[str], budget_bytes: int) -> List[str]:
    """转 data URL；总 base64 体积超预算时按比例抽稀（保头保尾）。"""
    import base64
    import mimetypes

    sizes = []
    for f in files:
        try:
            sizes.append(os.path.getsize(f))
        except OSError:
            sizes.append(0)
    total = sum(sizes) * 4 / 3
    keep = list(range(len(files)))
    if total > budget_bytes and files:
        step = total / budget_bytes
        keep = sorted({int(i * step) for i in range(int(len(files) / step) + 1)})
        keep = [i for i in keep if i < len(files)]
    out: List[str] = []
    for i in keep:
        f = files[i]
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        mime = mimetypes.guess_type(f)[0] or "image/jpeg"
        out.append(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
    return out


def extract_for_model(
    video_path: str,
    *,
    duration: Optional[float] = None,
    target_frames: int = 80,
    width: int = 768,
    threshold: float = 0.02,
    budget_bytes: int = 24 * 1024 * 1024,
) -> List[str]:
    """本地抽帧主入口，返回 data URL 列表。失败返回空列表，由调用方降级。"""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        logger.error("[video] 找不到 ffmpeg，无法本地抽帧")
        return []

    dur = duration
    if not dur or dur <= 0:
        dur = probe_duration(video_path, ffmpeg)
    if not dur or dur <= 0:
        logger.warning("[video] 无法确定时长，按候选帧尾帧时间兜底")
        dur = 0.0

    out_dir = str(ensure_temp_dir(os.path.join(_FRAME_ROOT, str(abs(hash(video_path)) % 10**10))))
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    try:
        # 不做窗口下限：窗口越细，落在不同窗口的候选点越多，保留的画面越多。
        # 实测 12s 视频 80 窗口得 16 帧、压到 30 窗口只剩 11 帧（同窗口内多个操作被合并）。
        # 静止窗口复用已有候选帧、不额外解码，所以复用率高只是日志噪音，不是开销。

        # 短视频：窗口比帧还密，直接按 fps 均匀取，避免分层退化成逐帧
        if dur and dur < 3:
            fps = max(target_frames / dur, 1.0)
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", video_path,
                "-vf", f"fps={fps:.3f},scale={width}:-2",
                "-frames:v", str(target_frames), "-q:v", "4",
                os.path.join(out_dir, "u%04d.jpg"),
            ]
            _sem_run(cmd, _SCAN_TIMEOUT)
            files = sorted(glob.glob(os.path.join(out_dir, "u*.jpg")))
            return _to_data_urls(files, budget_bytes)

        cands = _extract_candidates(video_path, out_dir, threshold, width, ffmpeg)
        if not cands:
            logger.warning("[video] 未检测到画面变化，尝试均匀抽帧兜底")
            fps = max(target_frames / max(dur, 1.0), 0.05)
            _sem_run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", video_path,
                    "-vf", f"fps={fps:.3f},scale={width}:-2",
                    "-frames:v", str(target_frames), "-q:v", "4",
                    os.path.join(out_dir, "u%04d.jpg"),
                ],
                _SCAN_TIMEOUT,
            )
            return _to_data_urls(sorted(glob.glob(os.path.join(out_dir, "u*.jpg"))), budget_bytes)

        picked = _layered_select(cands, dur, target_frames)
        pairs = _fill_gaps(video_path, picked, out_dir, width, ffmpeg)
        files = _dedup(pairs)
        logger.info(
            f"[video] 抽帧完成 候选={len(cands)} 窗口={target_frames} "
            f"实抽={len(files)} 复用={sum(1 for _, f in pairs if f)}"
        )
        return _to_data_urls(files, budget_bytes)
    except subprocess.TimeoutExpired:
        logger.error("[video] 抽帧超时")
        return []
    except Exception as e:
        logger.error(f"[video] 抽帧失败: {e}", exc_info=True)
        return []
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def prepare_from_url(
    url: str,
    *,
    duration: Optional[float] = None,
    target_frames: int = 80,
    width: int = 768,
    threshold: float = 0.02,
    max_video_bytes: int = 80 * 1024 * 1024,
    download_timeout: int = 60,
) -> List[str]:
    """下载 + 抽帧的高层入口，供 agent 侧在线程池中调用。"""
    work = ensure_temp_dir(os.path.join(_FRAME_ROOT, "_dl"))
    name = str(abs(hash(url)) % 10**10) + ".mp4"
    dest = os.path.join(work, name)
    try:
        if not download_video(url, dest, max_video_bytes, download_timeout):
            return []
        return extract_for_model(
            dest,
            duration=duration,
            target_frames=target_frames,
            width=width,
            threshold=threshold,
        )
    finally:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


def cleanup_leftovers() -> None:
    """启动时清空遗留帧目录（上次异常退出可能残留）。"""
    try:
        root = ensure_temp_dir(_FRAME_ROOT)
        for entry in os.listdir(root):
            p = os.path.join(root, entry)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:
        logger.warning(f"[video] 清理遗留帧目录失败: {e}")
