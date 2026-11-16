"""Allow-list SVG sanitiser for model-written drawings (jfollow10c ruling 2). Stdlib only.

The model's SVG is hostile input: it is parsed with expat (DOCTYPE, ENTITY and external entities
refused, also inside the parser, so a declared encoding cannot hide them from the text scan), then
rebuilt from an element and attribute allow-list. The output is shown only as an <img> source in
the drawing card. Fails closed: any doubt -> svg None plus a fixed reason word.
"""

import math
import re
from dataclasses import dataclass
from xml.parsers import expat

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_BYTES = 48_000
MAX_ELEMENTS = 2_000
MAX_DEPTH = 24
FONT_FLOOR = 22

ELEMENTS = frozenset(
    "svg g defs title desc rect circle ellipse line polyline polygon path text tspan marker"
    " linearGradient radialGradient stop use".split())
UNWRAP = frozenset({"a", "switch"})  # dropped, but their children and text are kept (and re-checked)
DRAWABLE = frozenset("rect circle ellipse line polyline polygon path text tspan use".split())
ATTRS = frozenset(
    "id x y x1 y1 x2 y2 cx cy r rx ry width height points d transform fill stroke stroke-width"
    " stroke-linecap stroke-linejoin stroke-dasharray opacity fill-opacity stroke-opacity font-size"
    " font-weight font-family text-anchor dominant-baseline dx dy viewBox markerWidth markerHeight"
    " refX refY orient marker-start marker-mid marker-end offset stop-color stop-opacity"
    " gradientUnits".split())

_ID = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
_ID_RE = re.compile(_ID)
_LOCAL_REF = re.compile(r"#" + _ID)
_URL_REF = re.compile(r"url\(#" + _ID + r"\)")
_BAD_VALUE = re.compile(r"javascript:|data:|expression\(|@import|\\", re.I)
_DOCTYPE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)|<\?xml-stylesheet", re.I)
_FONT_SIZE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*")


@dataclass
class Result:
    svg: str | None
    reason: str  # ok | too_big | doctype | unparseable | not_svg | no_viewbox | too_many | too_deep | empty
    kept: int = 0
    removed: int = 0
    attrs_removed: int = 0


class _Refuse(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _refuse_doctype(*_args):
    raise _Refuse("doctype")


def _parse(text: str) -> tuple[dict, int]:
    """expat -> (root node, element count). Node = {"tag", "ns", "attrs", "children"}; a child is a
    node or a str. Raises _Refuse for a DOCTYPE/ENTITY, a cap, or a parse error."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.EntityDeclHandler = _refuse_doctype
    parser.UnparsedEntityDeclHandler = _refuse_doctype
    parser.ExternalEntityRefHandler = _refuse_doctype
    stack: list[dict] = []
    root: list[dict] = []
    count = [0]

    def start(name, attrs):
        count[0] += 1
        if count[0] > MAX_ELEMENTS:
            raise _Refuse("too_many")
        if len(stack) >= MAX_DEPTH:
            raise _Refuse("too_deep")
        ns, _, tag = name.rpartition(" ")
        node = {"tag": tag, "ns": ns, "attrs": attrs, "children": []}
        (stack[-1]["children"] if stack else root).append(node)
        stack.append(node)

    def end(_name):
        stack.pop()

    def chars(data):
        if stack:
            kids = stack[-1]["children"]
            if kids and isinstance(kids[-1], str):
                kids[-1] += data
            else:
                kids.append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(text, True)  # a str is fed as UTF-8 whatever encoding= the text declares
    except expat.ExpatError:
        raise _Refuse("unparseable") from None
    if not root:
        raise _Refuse("unparseable")
    return root[0], count[0]


def _view_box(value) -> str | None:
    parts = re.split(r"[\s,]+", value.strip()) if isinstance(value, str) else []
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) != 4 or not all(math.isfinite(n) for n in nums):
        return None
    if not (0 < nums[2] <= 10_000 and 0 < nums[3] <= 10_000):
        return None
    return " ".join(parts)


def _num(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _attr(tag: str, name: str, value: str) -> tuple[str, str] | None:
    """One attribute (expat name, namespace-qualified as "uri local") -> (name, value) kept, or None."""
    ns, _, local = name.rpartition(" ")
    if local == "href" and ns in ("", XLINK_NS):
        return ("href", value) if tag == "use" and _LOCAL_REF.fullmatch(value.strip()) else None
    if ns or local not in ATTRS or _BAD_VALUE.search(value):
        return None
    if "url(" in value.lower() and not _URL_REF.fullmatch(value.strip()):
        return None
    if local == "id" and not _ID_RE.fullmatch(value):
        return None
    if local == "font-size":
        m = _FONT_SIZE.fullmatch(value)
        if not m:
            return None
        return local, _num(max(float(m.group(1)), FONT_FLOOR))
    return local, value


class _Builder:
    def __init__(self):
        self.kept = self.removed = self.attrs_removed = 0
        self.drawable = False

    def _subtree(self, node: dict) -> int:
        return 1 + sum(self._subtree(c) for c in node["children"] if isinstance(c, dict))

    def children(self, node: dict, out: list[str]) -> None:
        for child in node["children"]:
            if isinstance(child, str):
                out.append(_esc(child))
                continue
            known_ns = child["ns"] in ("", SVG_NS)
            if known_ns and child["tag"] in UNWRAP:
                self.removed += 1
                self.attrs_removed += len(child["attrs"])
                self.children(child, out)
            elif known_ns and child["tag"] in ELEMENTS:
                self.element(child, out)
            else:
                self.removed += self._subtree(child)

    def element(self, node: dict, out: list[str], root: str = "") -> None:
        tag = node["tag"]
        self.kept += 1
        self.drawable = self.drawable or tag in DRAWABLE
        if root:
            self.attrs_removed += sum(1 for k in node["attrs"] if k != "viewBox")
            out.append(f'<svg xmlns="{SVG_NS}" viewBox="{_esc(root, True)}">')
        else:
            attrs = []
            for name, value in node["attrs"].items():
                kept = _attr(tag, name, value)
                if kept is None:
                    self.attrs_removed += 1
                else:
                    attrs.append(f' {kept[0]}="{_esc(kept[1], True)}"')
            out.append(f"<{tag}{''.join(attrs)}>")
        self.children(node, out)
        out.append(f"</{tag}>")


def _esc(text: str, attr: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace('"', "&quot;") if attr else text


def sanitize(text) -> Result:
    """Model SVG text -> Result. Never raises; svg is None unless reason == "ok"."""
    try:
        return _sanitize(text)
    except _Refuse as exc:
        return Result(None, exc.reason)
    except Exception:  # noqa: BLE001 - fail closed on anything unexpected
        return Result(None, "unparseable")


def _sanitize(text) -> Result:
    if not isinstance(text, str):
        return Result(None, "not_svg")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        return Result(None, "too_big")
    if _DOCTYPE.search(text.replace("\x00", "")):  # NULs: UTF-16 text read as single bytes
        return Result(None, "doctype")
    root, _count = _parse(text)
    if root["tag"] != "svg" or root["ns"] not in ("", SVG_NS):
        return Result(None, "not_svg")
    view_box = _view_box(root["attrs"].get("viewBox"))
    if view_box is None:
        return Result(None, "no_viewbox")
    b = _Builder()
    out: list[str] = []
    b.element(root, out, root=view_box)
    if not b.drawable:
        return Result(None, "empty", b.kept, b.removed, b.attrs_removed)
    svg = "".join(out)
    if len(svg.encode("utf-8")) > MAX_BYTES:
        return Result(None, "too_big", b.kept, b.removed, b.attrs_removed)
    return Result(svg, "ok", b.kept, b.removed, b.attrs_removed)
