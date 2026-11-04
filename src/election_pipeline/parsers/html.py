"""Small HTML tree for result tables, without executing page scripts."""

from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def text(self):
        return " ".join(
            " ".join(c if isinstance(c, str) else c.text for c in self.children).split()
        )

    def find(self, tag=None, *, css=None, identity=None):
        found = []
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if (
                (tag is None or child.tag == tag)
                and (css is None or css in child.attrs.get("class", "").split())
                and (identity is None or child.attrs.get("id") == identity)
            ):
                found.append(child)
            found.extend(child.find(tag, css=css, identity=identity))
        return found


class Tree(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 64:
            raise ValueError("HTML nesting exceeds the supported depth")
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)
