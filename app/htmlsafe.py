"""HTML-Mails sicher anzeigen.

Drei Schutzschichten:
1. Dieser Filter lässt nur eine feste Liste harmloser Tags und Attribute durch (kein Script, keine Formulare,
   keine Ereignis-Attribute, keine javascript:-Links, kein SVG).
2. Externe Inhalte (Bilder, Hintergründe, CSS mit url(…)) werden entfernt, solange der Benutzer sie nicht
   ausdrücklich lädt – so verraten Tracking-Pixel nicht, dass und wann eine Mail geöffnet wurde.
   Eingebettete Bilder (cid:…) werden als data:-Adresse direkt eingesetzt.
3. Die Oberfläche zeigt das Ergebnis in einem abgeschotteten iframe (sandbox ohne Scripts, eigene Content-
   Security-Policy) – selbst wenn hier etwas durchrutschte, könnte es nichts ausführen und nichts nachladen.
"""
import base64
import html
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

ALLOWED_TAGS = {
    "a", "abbr", "address", "b", "big", "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup",
    "dd", "del", "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins",
    "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul", "wbr", "style", "article", "section", "header",
    "footer", "main", "nav", "aside", "figure", "figcaption", "picture", "label",
}
VOID = {"br", "hr", "img", "col", "wbr"}
# Inhalt komplett verwerfen (nicht nur das Tag)
DROP_CONTENT = {"script", "noscript", "iframe", "object", "embed", "applet", "svg", "math", "template", "title",
                "frameset", "frame", "select", "textarea", "xml", "audio", "video", "canvas"}
ALLOWED_ATTRS = {
    "align", "alt", "bgcolor", "border", "cellpadding", "cellspacing", "class", "color", "colspan", "dir", "face",
    "height", "hspace", "lang", "rowspan", "size", "start", "style", "summary", "title", "type", "valign", "vspace",
    "width", "nowrap", "role", "scope", "headers", "abbr", "span",
}
SAFE_LINK = re.compile(r"^(https?:|mailto:|tel:|#)", re.I)
DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$", re.I)
CSS_URL = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_BAD = re.compile(r"expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|@import[^;]*;?", re.I)


def _clean_css(css: str, allow_remote: bool, flags: Dict) -> str:
    css = CSS_BAD.sub("", css)

    def repl(m):
        target = m.group(2).strip()
        if DATA_IMAGE.match(target):
            return m.group(0)
        if allow_remote and re.match(r"^https?://", target, re.I):
            return f"url('{target.replace(chr(39), '%27')}')"
        if re.match(r"^(https?:)?//", target, re.I):
            flags["remote"] = True
        return "none"
    return CSS_URL.sub(repl, css)


class _Sanitizer(HTMLParser):
    def __init__(self, allow_remote: bool, cid_map: Dict[str, str]):
        super().__init__(convert_charrefs=True)
        self.allow_remote = allow_remote
        self.cid_map = cid_map
        self.out: List[str] = []
        self.skip = 0           # Tiefe innerhalb eines zu verwerfenden Elements
        self.skip_tag: List[str] = []
        self.in_style = False
        self.flags = {"remote": False}

    def _attrs(self, tag: str, attrs) -> str:
        parts = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name.startswith("on"):
                continue
            if tag == "a" and name == "href":
                v = value.strip()
                if SAFE_LINK.match(v):
                    parts.append(("href", v))
                continue
            if tag == "img" and name == "src":
                v = value.strip()
                if v.lower().startswith("cid:"):
                    data = self.cid_map.get(v[4:].strip("<>"))
                    if data:
                        parts.append(("src", data))
                elif DATA_IMAGE.match(v):
                    parts.append(("src", v))
                elif re.match(r"^(https?:)?//", v, re.I):
                    if self.allow_remote and v.lower().startswith(("http://", "https://")):
                        parts.append(("src", v))
                    else:
                        self.flags["remote"] = True
                        parts.append(("data-blocked", "1"))
                continue
            if name == "background":
                v = value.strip()
                if self.allow_remote and re.match(r"^https?://", v, re.I):
                    parts.append(("background", v))
                elif v:
                    self.flags["remote"] = True
                continue
            if name == "style":
                parts.append(("style", _clean_css(value, self.allow_remote, self.flags)))
                continue
            if name in ALLOWED_ATTRS:
                parts.append((name, value))
        if tag == "a" and any(n == "href" for n, _ in parts):
            parts += [("target", "_blank"), ("rel", "noopener noreferrer")]
        return "".join(f' {n}="{html.escape(v, quote=True)}"' for n, v in parts)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip_tag[-1] and tag not in VOID:
                self.skip += 1
            return
        if tag in DROP_CONTENT:
            if tag not in VOID:
                self.skip = 1
                self.skip_tag.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return  # Tag weg, Inhalt bleibt
        if tag == "style":
            self.in_style = True
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.skip or tag in DROP_CONTENT or tag not in ALLOWED_TAGS or tag == "style":
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip_tag[-1]:
                self.skip -= 1
                if not self.skip:
                    self.skip_tag.pop()
            return
        if tag in ALLOWED_TAGS and tag not in VOID:
            if tag == "style":
                self.in_style = False
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_style:
            # Im <style> darf kein „</“ vorkommen (sonst könnte es aus dem Block ausbrechen)
            self.out.append(_clean_css(data, self.allow_remote, self.flags).replace("<", "\\3C "))
        else:
            self.out.append(html.escape(data, quote=False))

    # Kommentare, <!DOCTYPE>, <?xml …> und CDATA werden verworfen
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def sanitize(markup: str, allow_remote: bool = False,
             inline_images: Optional[Dict[str, Tuple[str, bytes]]] = None) -> Tuple[str, bool]:
    """Bereinigtes HTML und ob externe Inhalte blockiert wurden."""
    cid_map = {cid: f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
               for cid, (mime, data) in (inline_images or {}).items()}
    p = _Sanitizer(allow_remote, cid_map)
    try:
        p.feed(markup or "")
        p.close()
    except (AssertionError, ValueError):  # sehr kaputtes HTML – was bis dahin da ist, reicht
        pass
    if p.in_style:
        p.out.append("</style>")
    return "".join(p.out), p.flags["remote"]


def csp(allow_remote: bool) -> str:
    """Content-Security-Policy für den iframe, in dem die Mail angezeigt wird."""
    img = "data: https: http:" if allow_remote else "data:"
    return f"default-src 'none'; img-src {img}; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'"
