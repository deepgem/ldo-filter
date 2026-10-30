"""t.me/s/ 页面解析：轻量 DOM（标准库 html.parser），零依赖。

消息正文结构固定为：`作者 (@user) 在 <话题标题> 中发帖` + 正文
topic 是极强分类特征，务必解析出来。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"br", "img", "hr", "meta", "link", "input", "source", "area", "base", "col", "embed"}

TOPIC_RE = re.compile(r"^\s*(?P<author>.{1,60}?)\s+在\s+(?P<topic>.{1,120}?)\s+中(?:发帖|回复|发表)", re.S)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list["Node | str"] = []
        self.parent = parent

    def get(self, key: str, default: str = "") -> str:
        return self.attrs.get(key, default)

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def has_class(self, name: str) -> bool:
        return name in self.classes()

    def text(self) -> str:
        parts: list[str] = []
        self._collect(parts)
        return "".join(parts)

    def _collect(self, out: list[str]) -> None:
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag == "br":
                out.append("\n")
            elif c.tag == "img":
                alt = c.get("alt")
                if alt:
                    out.append(alt)
            else:
                c._collect(out)

    def find_all(self, pred, acc: list["Node"] | None = None) -> list["Node"]:
        if acc is None:
            acc = []
        for c in self.children:
            if isinstance(c, Node):
                if pred(c):
                    acc.append(c)
                c.find_all(pred, acc)
        return acc


class TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs) -> None:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def build_tree(html: str) -> Node:
    tb = TreeBuilder()
    tb.feed(html)
    tb.close()
    return tb.root


def _norm_ws(s: str) -> str:
    s = s.replace("\u200b", "").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_author_topic(raw: str) -> tuple[str, str, str]:
    """把 'RobinDai (@linuxdr) 在 某话题 中发帖<正文>' 拆成 (author, topic, body)。"""
    m = TOPIC_RE.match(raw)
    if not m:
        return "", "", raw.strip()
    author = m.group("author").strip()
    topic = m.group("topic").strip()
    body = raw[m.end():].strip()
    # 去掉作者名里的 (@handle) 括号部分，保留可读名
    author = re.sub(r"\s*\(@?[\w_]+\)\s*$", "", author).strip()
    return author, topic, body


def parse_page(html: str, channel: str) -> list[dict]:
    root = build_tree(html)
    wraps = root.find_all(lambda n: "tgme_widget_message_wrap" in n.classes())
    out: list[dict] = []
    for wrap in wraps:
        posts = wrap.find_all(lambda n: n.get("data-post") != "")
        if not posts:
            continue
        post = posts[0]
        data_post = post.get("data-post", "")
        mid = data_post.rsplit("/", 1)[-1]
        if not mid.isdigit():
            continue
        msg_id = int(mid)

        times = wrap.find_all(lambda n: n.tag == "time" and n.get("datetime"))
        posted_at = times[0].get("datetime") if times else ""

        text_nodes = wrap.find_all(lambda n: "tgme_widget_message_text" in n.classes())
        raw_text = _norm_ws(text_nodes[0].text()) if text_nodes else ""
        author, topic, body = split_author_topic(raw_text)

        if not author:
            author_nodes = wrap.find_all(
                lambda n: n.has_class("tgme_widget_message_author") or n.has_class("tgme_widget_message_from_author")
            )
            if author_nodes:
                author = _norm_ws(author_nodes[0].text())

        links: list[str] = []
        if text_nodes:
            for a in text_nodes[0].find_all(lambda n: n.tag == "a" and n.get("href")):
                href = a.get("href")
                if href.startswith("http"):
                    links.append(href)
        # 去重保序
        seen: set[str] = set()
        links = [x for x in links if not (x in seen or seen.add(x))]

        has_media = bool(
            wrap.find_all(
                lambda n: bool(
                    {"tgme_widget_message_photo_wrap", "tgme_widget_message_video_player",
                     "tgme_widget_message_document_wrap", "tgme_widget_message_sticker"} & n.classes()
                )
            )
        )

        out.append(
            {
                "msg_id": msg_id,
                "posted_at": posted_at,
                "author": author,
                "topic": topic,
                "text": body or raw_text,
                "links": links,
                "has_media": has_media,
            }
        )
    out.sort(key=lambda m: m["msg_id"])
    return out
