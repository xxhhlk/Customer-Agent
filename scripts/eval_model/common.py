"""模型评测公共层。

只读生产库与知识库，产物一律写 temp/eval/（已 gitignore）。
不修改 config.json、不写 channel_shop.db、不触碰 user_data/。

数据口径说明：
- 金标准 = chat_message_records 里 reply_source='staff' 的客服人工回复。
- 上下文 = 同一 (shop_id, buyer_uid) 会话里该人工回复之前的历史消息。
- 参考话术 = LanceDB 知识库 143 条话术里与买家问题最相近的 3 条（A′ 口径）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "database" / "channel_shop.db"
VECTOR_DIR = ROOT / "data" / "vector_db"
OUT_DIR = ROOT / "temp" / "eval"

# 评测用的上下文预算（比生产紧：生产 500k 窗口，这里压到 8k 保证 41 个模型都能吃下）
HIST_BUDGET_TOKENS = 2400
MAX_HISTORY_MSGS = 30
MAX_STAFF_INJECT = 20

# 与 agent.py 的禁用词元词过滤保持一致
_BANNED_META_MARKERS = ("禁用词", "这条不要发给客户")

_EMOJI_TAG = re.compile(r"\[[^\]]{1,6}\]")
_URL_RE = re.compile(r"https?://[^\s，。；、）)\"']+")
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,60})[a-z0-9]\.)+(?:com|cn|net|org|xyz|top|cc|io|me|vip|info|site|shop|app|dev)\b", re.I)
_VERSION_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{1,2}\b")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def set_config_in_memory(key: str, value: Any) -> Any:
    """只改内存里的配置（save=False），不写 config.json。

    评测需要临时切 db_path / model_name，绝不能落到用户的配置文件里。
    """
    from config import set_config  # 延迟导入：仅 Agent 口径需要

    return set_config(key, value, save=False)


def ro_conn(path: Path = DB_PATH) -> sqlite3.Connection:
    """只读打开，避免任何写入副作用（含 WAL）。"""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def buyer_hash(uid: Any) -> str:
    return hashlib.sha1(str(uid).encode("utf-8")).hexdigest()[:8]


def est_tokens(text: str) -> int:
    """粗估 token：CJK/全角 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    return cjk + (len(text) - cjk) // 4


def banned_block(cfg: dict) -> str:
    words: list[str] = []
    for raw in cfg.get("banned_words") or []:
        w = str(raw or "").strip()
        if not w or any(m in w for m in _BANNED_META_MARKERS):
            continue
        if w not in words:
            words.append(w)
    if not words:
        return ""
    return (
        "## 禁用词（拼多多平台违规，回复中绝对不能出现）\n"
        "下面的词出现在回复里会被发送前拦截、要求重写。生成时就避开，也不要用同音字、"
        "拆字、加空格加符号的方式绕过去；拿不准的词就换个说法：\n"
        + "、".join(words)
    )


def system_prompt(cfg: dict | None = None) -> str:
    """复刻生产 system message：description + instructions + 禁用词块 + additional_context。"""
    cfg = cfg or load_config()
    p = cfg.get("prompt") or {}
    parts = [str(p.get("description") or ""), "\n".join(p.get("instructions") or [])]
    blk = banned_block(cfg)
    if blk:
        parts.append(blk)
    parts.append(str(p.get("additional_context") or ""))
    return "\n\n".join(x for x in parts if x).strip()


_KB_CACHE: list[dict] | None = None


def load_kb() -> list[dict]:
    """读取 LanceDB 知识库话术（独立脚本进程内直连，不走主进程 IPC）。"""
    global _KB_CACHE
    if _KB_CACHE is not None:
        return _KB_CACHE
    items: list[dict] = []
    try:
        import lancedb  # noqa: PLC0415  (脚本进程独占，不违反主进程禁 import 约束)

        db = lancedb.connect(str(VECTOR_DIR))
        if "customer_knowledge" in db.table_names():
            df = db.open_table("customer_knowledge").search().limit(500).to_pandas()
            for _, row in df.iterrows():
                try:
                    payload = json.loads(row["payload"])
                except Exception:
                    continue
                content = str(payload.get("content") or "").strip()
                if not content:
                    continue
                items.append({
                    "name": str(payload.get("name") or (payload.get("meta_data") or {}).get("title") or ""),
                    "content": content,
                })
    except Exception as exc:  # 知识库缺失不应让评测挂掉
        print(f"[warn] 知识库读取失败: {type(exc).__name__}: {exc}")
    _KB_CACHE = items
    return items


def _ngrams(text: str, n: int = 2) -> set[str]:
    t = re.sub(r"\s+", "", text or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _terms(text: str) -> set[str]:
    """中文 2-gram + 英文词，作为检索词。"""
    lowered = (text or "").lower()
    cjk = re.sub(r"[^\u4e00-\u9fff]+", "", lowered)
    grams = {cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))}
    words = set(re.findall(r"[a-z]{3,}", lowered))
    return grams | words


def retrieve_reference(question: str, kb: list[dict], k: int = 3,
                       max_similarity: float = 0.6) -> tuple[list[dict], int]:
    """IDF 加权 2-gram 检索参考话术。

    两个硬约束：
    - 相似度 >= max_similarity 的话术视为「答案泄漏」直接丢弃（人工常直接复制话术，
      注进去等于把答案给模型）；
    - 只在检索有明显胜出者时注入（score >= top1 * 0.5），避免塞进无关话术当噪音。
    返回 (话术列表, 因泄漏丢弃的条数)。
    """
    q = _terms(question)
    if len(q) < 3 or not kb:
        return [], 0

    docs = [_terms(f"{i['name']} {i['content']}") for i in kb]
    n_docs = len(docs)
    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n_docs + 1) / (c + 0.5)) for t, c in df.items()}

    scored: list[tuple[float, dict]] = []
    dropped = 0
    for item, doc in zip(kb, docs):
        if not doc:
            continue
        hit = [t for t in q if t in doc]
        if not hit:
            continue
        raw = sum(idf.get(t, 0.0) for t in hit) / (1 + math.log(len(doc) + 1))
        # 答案泄漏过滤：与金标准候选话术高度重合的直接丢弃
        a, b = _ngrams(item["content"]), _ngrams(question)
        if a and b and len(a & b) / len(a | b) >= max_similarity:
            dropped += 1
            continue
        scored.append((raw, item))

    if not scored:
        return [], dropped
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    picked = [(s, it) for s, it in scored[:k] if s >= top * 0.5 and s > 0]
    return [dict(it, score=round(s, 4)) for s, it in picked], dropped


def kb_fact_texts(kb: list[dict]) -> str:
    return "\n".join(f"{i['name']} {i['content']}" for i in kb)


def extract_facts(text: str) -> dict[str, set[str]]:
    """抽取可核验事实：URL / 域名 / 版本号。端口单列（易误判，只做提示）。"""
    text = text or ""
    return {
        "url": {u.rstrip(".,;:)") for u in _URL_RE.findall(text)},
        "domain": {d.lower() for d in _DOMAIN_RE.findall(text)},
        "version": set(_VERSION_RE.findall(text)),
    }


def clean_buyer_text(text: str) -> str:
    """去掉表情标记，保留语义。"""
    return _EMOJI_TAG.sub("", text or "").strip()


def chat(messages: list[dict], model: str, cfg: dict | None = None,
         timeout: float = 120.0, retries: int = 2) -> dict:
    """调本地网关 chat/completions，返回 {ok, content, latency, usage, error}。"""
    import httpx

    cfg = cfg or load_config()
    llm = cfg.get("llm") or {}
    base = str(llm.get("api_base") or "").rstrip("/")
    key = str(llm.get("api_key") or "")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "stream": False,
    }
    last_err = ""
    t0 = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as cli:
                resp = cli.post(f"{base}/chat/completions", json=payload,
                                headers={"Authorization": f"Bearer {key}"})
            dt = time.perf_counter() - t0
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(1.5 * (attempt + 1))
                continue
            data = resp.json()
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            return {
                "ok": True,
                "content": str(msg.get("content") or ""),
                "latency": round(dt, 2),
                "usage": data.get("usage") or {},
                "error": "",
            }
        except Exception as exc:  # 超时/连接失败重试
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
    return {"ok": False, "content": "", "latency": round(time.perf_counter() - t0, 2),
            "usage": {}, "error": last_err}


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
