"""把网页 / PDF / 纯文本转成干净的文本，并切块。"""
import io
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ..config import get_settings

MAX_BYTES = 5 * 1024 * 1024
CHUNK_CHARS = 900
MAX_CHUNKS = 200
UA = "Mozilla/5.0 (compatible; learn-anything/0.1)"


class ExtractError(ValueError):
    pass


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
             "blockquote", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.splitlines()]
    # 去掉过短的导航碎片行，但保留中文短句
    lines = [ln for ln in lines if len(ln) >= 2]
    return p.title.strip(), normalize_text("\n".join(lines))


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return normalize_text("\n\n".join((page.extract_text() or "") for page in reader.pages))
    except Exception as e:  # pypdf 对损坏文件抛出多种异常
        raise ExtractError(f"PDF 解析失败：{e}") from e


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\x00", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def bytes_to_text(data: bytes, filename: str = "", content_type: str = "") -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    name = filename.lower()
    if name.endswith(".pdf") or "pdf" in content_type or data[:5] == b"%PDF-":
        return filename, pdf_to_text(data)
    text = _decode(data)
    if name.endswith((".html", ".htm")) or "html" in content_type or text.lstrip()[:15].lower().startswith(
            ("<!doctype html", "<html")):
        title, body = html_to_text(text)
        return title or filename, body
    return filename, normalize_text(text)


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _check_host(url: str):
    """拒绝内网 / 回环 / 链路本地地址，防止多用户部署时被用来探测内网（SSRF）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ExtractError(f"只支持 http(s) 链接：{url}")
    if get_settings().allow_private_urls:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise ExtractError(f"无法解析域名：{parsed.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ExtractError(f"禁止访问内网地址：{parsed.hostname}（可设 LA_ALLOW_PRIVATE_URLS=true）")


def fetch_url(url: str) -> tuple[str, str]:
    """抓取网页或 PDF，返回 (标题, 正文)。手动跟随重定向，每一跳都做地址检查。"""
    with httpx.Client(timeout=20, headers={"User-Agent": UA}, follow_redirects=False) as client:
        for _ in range(5):
            _check_host(url)
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code >= 400:
                    raise ExtractError(f"抓取失败 HTTP {r.status_code}：{url}")
                buf = bytearray()
                for piece in r.iter_bytes():
                    buf.extend(piece)
                    if len(buf) > MAX_BYTES:
                        raise ExtractError(f"内容超过 {MAX_BYTES // 1024 // 1024}MB：{url}")
                title, text = bytes_to_text(bytes(buf), urlparse(url).path.rsplit("/", 1)[-1],
                                            r.headers.get("content-type", ""))
                return title or url, text
    raise ExtractError(f"重定向次数过多：{url}")


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        while len(para) > size:  # 超长段落按句子边界硬切
            cut = max(para.rfind(sep, 0, size) for sep in ("。", ". ", "！", "？", "; ", "；"))
            cut = cut + 1 if cut > size // 3 else size
            pieces, para = para[:cut], para[cut:].strip()
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(pieces.strip())
        if len(buf) + len(para) + 1 > size and buf:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks[:MAX_CHUNKS]
