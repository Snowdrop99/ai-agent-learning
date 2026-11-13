"""抓取 + 正文抽取：ingest.py 和 agent.py 共用一份实现

为什么单独抽出来？——两处各写一份"剥标签"看着无害，改一处忘另一处，
就会出现"离线入库干净、在线抓取脏"这种最难查的不一致。
"""
import re
import urllib.request
from html import unescape

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) research-agent-learning"}


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw, cs = r.read(), r.headers.get_content_charset() or "utf-8"
    return raw.decode(cs, errors="replace")


def _looks_like_code(line: str) -> bool:
    """识别 JS/CSS/JSON 残渣行。

    难看的检索结果多半不是模型的问题，是语料里混进了 srcset、\\u0026、`":"` 这类东西。
    判据：出现明显的代码标记，或"字母数字+汉字"占比过低。
    """
    if re.search(r"(\\u[0-9a-fA-F]{4}|srcset|data-[\w-]+\s*[:=]|function\s*\(|=>|\{\s*\"|@media)", line):
        return True
    good = sum(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in line)
    return good / max(len(line), 1) < 0.55


def strip_html(html: str, min_line: int = 40) -> str:
    """阻塞标签 → 换行，剥标签，再丢掉导航/代码残渣。粗糙但可解释。"""
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article|pre|blockquote)>", "\n\n", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", "", html))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if len(ln) >= min_line and not _looks_like_code(ln)]
    return "\n\n".join(re.sub(r"\n\s*\n+", "\n\n", ln) for ln in keep)
