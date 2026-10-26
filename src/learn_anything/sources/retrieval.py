"""轻量 BM25 检索：英文按词、中日韩按字二元组切分，无需向量库。"""
import math
import re
from collections import Counter
from typing import Iterable, TypeVar

T = TypeVar("T")
_CJK = r"㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[a-z0-9]+(?:[.\-+#][a-z0-9]+)*")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "for", "on", "with", "as", "by",
         "be", "it", "that", "this", "的", "了", "是"}


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0)
        if re.match(rf"[{_CJK}]", tok):
            if len(tok) == 1:
                tokens.append(tok)
            else:
                tokens.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        elif tok not in _STOP:
            tokens.append(tok)
    return tokens


class BM25:
    def __init__(self, docs: Iterable[tuple[T, str]], k1: float = 1.5, b: float = 0.75):
        self.items: list[T] = []
        self.tfs: list[Counter] = []
        self.lens: list[int] = []
        df: Counter = Counter()
        for item, text in docs:
            toks = tokenize(text)
            tf = Counter(toks)
            self.items.append(item)
            self.tfs.append(tf)
            self.lens.append(len(toks))
            df.update(tf.keys())
        n = len(self.items)
        self.avgdl = (sum(self.lens) / n) if n else 0
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def search(self, query: str, k: int = 5) -> list[tuple[T, float]]:
        q = set(tokenize(query))
        scored = []
        for item, tf, dl in zip(self.items, self.tfs, self.lens):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scored.append((item, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]
