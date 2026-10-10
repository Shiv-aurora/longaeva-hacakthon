"""Offline checks on the technical report (app/static/lab/report.html).

The report's rule is that no figure is typed into the prose. A figure is either

  bound    <span data-bind="file:path" data-format="..."></span>, filled in the browser by
           report.js from app/static/data/<file>.json, or
  static   <span data-static="lab/<file>:path" data-format="...">text</span>, for the few
           sources under data/lab that the site does not serve. Its text is written by
           `python tests/test_lab_report.py --sync` and compared with the source here.

The reference list is generated the same way from data/lab/literature.json.

These tests check that the page and its assets exist and load nothing from another
host, that every bind is well formed and resolves against the files built today (and
against the contract's placeholder shape for files not built yet), that static text
and references match their sources, that no other digit is typed into the page, that
the main text stays within its length (detail a judge may want sits in closed
<details> blocks and is counted apart), that the section sidebar lists every section
and keeps the credit its license asks for, and that the sentences which state a finding
in words still agree with the numbers. Nothing here starts a server or a browser.
"""
from __future__ import annotations

import csv
import html
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
LAB = STATIC / "lab"
DATA = STATIC / "data"
FIXTURES = DATA / "fixtures"
LAB_DATA = ROOT / "data" / "lab"
PAGE = LAB / "report.html"
SCRIPT = LAB / "report.js"
SHEET = LAB / "report.css"
SIDEBAR_SCRIPT = LAB / "hook-sidebar.js"
SIDEBAR_SHEET = LAB / "hook-sidebar.css"
SIDEBAR_LICENSE = LAB / "LICENSE-hook-sidebar.txt"

# The one page the report links back to. The earlier workstation is gone and must not be linked.
ROUTES = {"/"}
MISSING_TEXT = "not yet computed"
# The main text, and the detail kept in closed blocks beside it.
WORDS_MIN, WORDS_MAX = 1000, 1300
DETAIL_WORDS_MAX = 520

# Digits a writer may type: years, quarter labels and five proper names
# (the Stage 1 proposal, a license, an index, a charting library and a hash function).
ALLOWED_DIGITS = [r"\b(?:19|20)\d{2}\b", r"\bQ[1-4]\b", r"\bStage 1\b", r"\bCC BY 4\.0\b", r"\bS&P 500\b", r"\bD3\b", r"\bSHA-256\b"]

# The Stage 1 proposal is in American English, so the report is too.
BRITISH = re.compile(
    r"\b(behaviour\w*|travell\w+|modell\w+|labell\w+|cancell\w+|fuell\w+|licence\w*|colour\w*|centre\w*|favour\w*|"
    r"programme\w*|organis\w+|recognis\w+|penalis\w+|normalis\w+|summaris\w+|analys(?:e|ed|es|ing)|catalogue\w*|"
    r"grey|whilst|towards|per cent|judgement|defence|metres?|learnt)\b", re.I)


# ------------------------------------------------------------------ the page as a tree

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list["Node | str"] = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def inside(self, test) -> bool:
        node = self
        while node is not None:
            if test(node):
                return True
            node = node.parent
        return False

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()

    def text(self, skip=lambda node: False, token=None) -> str:
        """Visible text. `skip` drops a subtree; `token`, if given, stands in for every bound or static element."""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag in ("script", "style") or skip(child):
                parts.append(" ")
            elif token is not None and ("data-bind" in child.attrs or "data-static" in child.attrs):
                parts.append(token)
            else:
                parts.append(child.text(skip, token))
        return "".join(parts)


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.node = self.root

    def handle_starttag(self, tag, attrs):
        child = Node(tag, {name: value or "" for name, value in attrs}, self.node)
        self.node.children.append(child)
        if tag not in VOID:
            self.node = child

    def handle_startendtag(self, tag, attrs):
        self.node.children.append(Node(tag, {name: value or "" for name, value in attrs}, self.node))

    def handle_endtag(self, tag):
        node = self.node
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.node = node.parent

    def handle_data(self, data):
        self.node.children.append(data)


def tree() -> Node:
    parser = Tree()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    return parser.root


def elements(test) -> list[Node]:
    return [node for node in tree().walk() if test(node)]


# ------------------------------------------------------------------ binds (mirrors report.js)

BIND = re.compile(r"^([a-z][a-z0-9_]*):(\S+)$")
SEGMENT = re.compile(r"^([A-Za-z0-9_-]+)?((?:\[[A-Za-z0-9_]+=[A-Za-z0-9_-]+\])*)$")
CONDITION = re.compile(r"\[([A-Za-z0-9_]+)=([A-Za-z0-9_-]+)\]")
ABSENT = object()


def parse_path(path: str):
    segments = []
    parts = path.split(".")
    for i, part in enumerate(parts):
        if part == "@count":
            if i != len(parts) - 1:
                return None
            segments.append(("@count", []))
            continue
        match = SEGMENT.match(part) if part else None
        if not match or (match.group(1) is None and not match.group(2)):
            return None
        segments.append((match.group(1), CONDITION.findall(match.group(2) or "")))
    return segments


def parse_bind(spec: str):
    match = BIND.match(spec.strip())
    if not match:
        return None
    segments = parse_path(match.group(2))
    return None if segments is None else (match.group(1), segments)


def resolve(node, segments):
    for key, where in segments:
        if node is None:
            return ABSENT
        if key == "@count":
            return len(node) if isinstance(node, (dict, list)) else ABSENT
        if key is not None:
            if isinstance(node, list):
                if not re.fullmatch(r"-?\d+", key) or not -len(node) <= int(key) < len(node):
                    return ABSENT
                node = node[int(key)]
            elif isinstance(node, dict):
                if key not in node:
                    return ABSENT
                node = node[key]
            else:
                return ABSENT
        if where:
            if not isinstance(node, list):
                return ABSENT
            node = next((item for item in node if isinstance(item, dict)
                         and all(_as_js_string(item.get(field)) == value for field, value in where)), ABSENT)
            if node is ABSENT:
                return ABSENT
    return ABSENT if node is None else node


def _as_js_string(value) -> str:
    return "null" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def format_names() -> set[str]:
    """The format names report.js defines, read from its source."""
    source = SCRIPT.read_text(encoding="utf-8")
    block = source[source.index("var FORMATS = {"):]
    block = block[:block.index("\n  };")]
    return set(re.findall(r"^    ([a-z0-9]+):", block, re.M))


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


NUMBER_FORMATS = re.compile(r"^(int|num\d|signed\d|pct\w*|usd\w*|cents0|words)$")


def suits(name: str, value) -> bool:
    """Whether a value is of the kind a format can show."""
    kind, _, inner = name.rpartition(":")
    if kind == "range":
        return isinstance(value, list) and len(value) == 2 and all(suits(inner, v) for v in value)
    if kind == "flex":
        return (isinstance(value, str) and bool(value)) or suits(inner, value) or suits("range:" + inner, value)
    if NUMBER_FORMATS.match(inner):
        return is_number(value)
    if inner == "date":
        return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}", value))
    if inner == "monthyear":
        return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}", value))
    if inner == "year":
        return (isinstance(value, str) and bool(re.match(r"^\d{4}(\D|$)", value))) or is_number(value)
    if inner == "period":
        return isinstance(value, str) and bool(re.fullmatch(r"\d{4}Q[1-4]", value))
    if inner == "hash":
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{16,}", value, re.I))
    if inner == "text":
        return (isinstance(value, str) and bool(value)) or is_number(value) or (
            isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value))
    return False


def binds() -> list[dict]:
    found = []
    for node in tree().walk():
        if "data-bind" in node.attrs:
            found.append({"bind": node.attrs["data-bind"], "format": node.attrs.get("data-format") or "text",
                          "scale": node.attrs.get("data-scale"), "node": node})
    return found


def bound_files() -> set[str]:
    names = set()
    for item in binds():
        for spec in item["bind"].split(","):
            parsed = parse_bind(spec)
            if parsed:
                names.add(parsed[0])
    return names


def load(name: str):
    """(data, kind): the built file, else the contract's placeholder, else nothing."""
    for folder, kind in ((DATA, "built"), (FIXTURES, "placeholder")):
        path = folder / f"{name}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), kind
    return None, "absent"


def built(name: str):
    path = DATA / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def bound_value(item: dict, kinds=("built", "placeholder")):
    """The value a bind resolves to, ABSENT if a field is not there, None if its file is not of the wanted kinds."""
    values = []
    for spec in item["bind"].split(","):
        name, segments = parse_bind(spec)
        data, kind = load(name)
        if kind not in kinds:
            return None
        value = resolve(data, segments)
        if value is ABSENT:
            return ABSENT
        values.append(value)
    value = values[0] if len(values) == 1 else values
    if item["scale"] not in (None, ""):
        k = float(item["scale"])
        value = [v * k for v in value] if isinstance(value, list) else value * k
    return value


# ------------------------------------------------------------------ static text and references

STATIC_SPEC = re.compile(r"^lab/([a-z0-9_/]+?)(\.csv)?:(\S+)$")
CSV_ROWS = re.compile(r"^@rows(?:\[([A-Za-z0-9_]+)=([A-Za-z0-9_|-]+)\])?$")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def static_value(spec: str):
    match = STATIC_SPEC.match(spec.strip())
    assert match, f"malformed data-static: {spec}"
    name, is_csv, path = match.groups()
    if is_csv:
        rows_spec = CSV_ROWS.match(path)
        assert rows_spec, f"a CSV source takes @rows or @rows[column=a|b]: {spec}"
        with (LAB_DATA / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        column, wanted = rows_spec.groups()
        return len(rows) if column is None else sum(row[column] in wanted.split("|") for row in rows)
    segments = parse_path(path)
    assert segments is not None, f"malformed data-static path: {spec}"
    value = resolve(json.loads((LAB_DATA / f"{name}.json").read_text(encoding="utf-8")), segments)
    assert value is not ABSENT, f"nothing at {spec}"
    return value


def static_text(value, name: str) -> str:
    """The same wording report.js gives the formats that static text uses."""
    if name == "int":
        return f"{round(value):,}"
    if name in ("date", "monthyear"):
        year, month, day = value[:4], int(value[5:7]), value[8:10]
        return f"{MONTHS[month - 1]} {int(day)}, {year}" if name == "date" else f"{MONTHS[month - 1]} {year}"
    if name == "text":
        return str(value)
    raise AssertionError(f"static text has no format {name!r}")


REFS_START, REFS_END = "<!-- references:start -->", "<!-- references:end -->"


def literature() -> list[dict]:
    return json.loads((LAB_DATA / "literature.json").read_text(encoding="utf-8"))["entries"]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text[-1:] in ".?!" else text + "."


def reference_item(entry: dict) -> str:
    title = f"<cite>{html.escape(_sentence(entry['title']), quote=False)}</cite>"
    if entry.get("url"):
        title = f'<a href="{html.escape(entry["url"], quote=True)}" rel="noopener noreferrer">{title}</a>'
    return (f'        <li data-ref="{html.escape(entry["id"], quote=True)}">{html.escape(entry["authors"], quote=False)} '
            f'({entry["year"]}). {title} {html.escape(_sentence(entry["venue"]), quote=False)}</li>')


def references_block() -> str:
    """The reference list: verified entries only, research first, then filings and other sources."""
    verified = sorted((e for e in literature() if e.get("verified") is True), key=lambda e: (e["authors"].lower(), e["year"], e["id"]))
    groups = (("Published research", [e for e in verified if e["kind"] == "paper"]),
              ("Filings, transcripts and analyst notes", [e for e in verified if e["kind"] != "paper"]))
    lines = [REFS_START]
    for heading, entries in groups:
        if entries:
            lines += [f"      <h3>{heading}</h3>", '      <ol class="refs">', *[reference_item(e) for e in entries], "      </ol>"]
    lines.append("      " + REFS_END)
    return "\n".join(lines)


STATIC_SPAN = re.compile(r'(<(\w+)\b[^>]*\bdata-static="([^"]+)"[^>]*\bdata-format="([^"]+)"[^>]*>)(.*?)(</\2>)', re.S)


def synced(source: str) -> str:
    """The page with its static text and its reference list rewritten from their sources."""
    source = STATIC_SPAN.sub(lambda m: m.group(1) + html.escape(static_text(static_value(m.group(3)), m.group(4)), quote=False) + m.group(6), source)
    start, end = source.index(REFS_START), source.index(REFS_END) + len(REFS_END)
    return source[:start] + references_block() + source[end:]


# ------------------------------------------------------------------ assets

def test_the_report_and_its_own_files_exist():
    for path in (PAGE, SCRIPT, SHEET, SIDEBAR_SCRIPT, SIDEBAR_SHEET, SIDEBAR_LICENSE):
        assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    assert "@media print" in SHEET.read_text(encoding="utf-8"), "report.css has no print layout"


def test_every_local_asset_and_link_resolves():
    root = tree()
    ids = {node.attrs["id"] for node in root.walk() if node.attrs.get("id")}
    missing = []
    for node in root.walk():
        for attribute in ("href", "src"):
            target = node.attrs.get(attribute)
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("url("):
                continue
            if target.startswith("#"):
                if target[1:] not in ids:
                    missing.append(target)
            elif target.split("#")[0].split("?")[0] in ROUTES:
                continue
            elif target.startswith("/static/"):
                if not (STATIC / target[len("/static/"):].split("?")[0]).is_file():
                    missing.append(target)
            else:
                missing.append(target)
    assert not missing, f"report.html points at things that do not exist: {missing}"
    page = PAGE.read_text(encoding="utf-8")
    for name in ("tokens.css", "base.css", "hook-sidebar.css", "report.css", "hook-sidebar.js", "report.js", "charts.js",
                 "vendor/d3.min.js", "vendor/plot.umd.min.js"):
        assert f"/static/lab/{name}" in page, f"report.html does not load {name}"
    # The sidebar's defaults come first so the report's own sheet and script can build on them.
    assert page.index("hook-sidebar.css") < page.index("report.css") and page.index("hook-sidebar.js") < page.index("report.js")
    assert 'href="/"' in page, "the report has no link back to the simulator"
    assert "/research" not in page, "the report links the earlier workstation"


def test_nothing_is_loaded_from_another_host():
    outside = re.compile(r"^\s*(https?:)?//", re.I)
    for node in tree().walk():
        loads = node.tag in ("script", "img", "source", "iframe", "embed", "video", "audio", "object", "use", "image") or (
            node.tag == "link" and set((node.attrs.get("rel") or "").lower().split()) != {"canonical"})
        if not loads:
            continue
        for attribute in ("src", "href", "data", "srcset", "poster", "xlink:href"):
            assert not outside.match(node.attrs.get(attribute) or ""), f"<{node.tag} {attribute}={node.attrs[attribute]!r}> loads from another host"
    sheet = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    assert "@import" not in sheet and not re.search(r"url\(\s*['\"]?(https?:)?//", sheet), "report.css loads from another host"
    script = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"https?://", script), "report.js names another host"
    assert not re.search(r"\bimport\s*\(", script)
    # The sidebar may name an XML namespace and the credit its license asks for, nothing else.
    sidebar = SIDEBAR_SCRIPT.read_text(encoding="utf-8")
    named = {address.rstrip(".,;)") for address in re.findall(r"https?://[^\s\"'`)<>]+", sidebar)}
    assert named <= {"http://www.w3.org/2000/svg", "https://rareui.com"}, f"hook-sidebar.js names {sorted(named)}"
    assert not re.search(r"\bimport\s*\(|\bfetch\s*\(", sidebar)
    for sheet_path in (SHEET, SIDEBAR_SHEET):
        css = re.sub(r"/\*.*?\*/", "", sheet_path.read_text(encoding="utf-8"), flags=re.S)
        assert "@import" not in css and "url(" not in css, f"{sheet_path.name} loads a file"
    # Links out are plain links, and do not hand the opener over.
    for match in re.finditer(r'<a\b[^>]*\bhref="https?://[^"]*"[^>]*>', PAGE.read_text(encoding="utf-8"), re.I):
        assert "noopener" in match.group(0), f"{match.group(0)} needs rel=noopener"


# ------------------------------------------------------------------ numbers

def test_every_bind_is_well_formed():
    names = format_names()
    assert {"pct0", "pct1", "usd2", "usd0m", "int", "date", "words", "text"} <= names, "report.js lacks a required format"
    found = binds()
    assert len(found) > 60, "the report binds too few figures to be the real page"
    for item in found:
        specs = item["bind"].split(",")
        assert len(specs) <= 2, f"more than two binds in {item['bind']!r}"
        for spec in specs:
            assert parse_bind(spec), f"malformed data-bind: {spec!r}"
        kind, _, inner = item["format"].rpartition(":")
        assert kind in ("", "range", "flex") and inner in names, f"unknown data-format {item['format']!r} on {item['bind']!r}"
        if item["scale"] not in (None, ""):
            float(item["scale"])
        assert item["node"].text().strip() == "", f"{item['bind']!r} has text typed inside it"


def test_every_bind_resolves_against_the_built_files():
    """A file built today must hold every field the page reads from it, in a form its format can show."""
    problems = []
    for item in binds():
        value = bound_value(item, kinds=("built",))
        if value is None:
            continue
        if value is ABSENT:
            problems.append(f"no value at {item['bind']}")
        elif not suits(item["format"], value):
            problems.append(f"{item['bind']} = {value!r} does not suit {item['format']}")
    assert not problems, "\n".join(problems)


def test_binds_to_files_not_built_yet_follow_the_contract():
    """For a file that is still to come, the path must exist in its placeholder under fixtures/."""
    problems = []
    for item in binds():
        files = {parse_bind(spec)[0] for spec in item["bind"].split(",")}
        if all(built(name) is not None for name in files):
            continue
        value = bound_value(item)
        if value is None:
            problems.append(f"{item['bind']}: no built file and no placeholder to check the path against")
        elif value is ABSENT:
            problems.append(f"{item['bind']} is not in the contract's placeholder")
        elif not suits(item["format"], value):
            problems.append(f"{item['bind']} = {value!r} does not suit {item['format']}")
    assert not problems, "\n".join(problems)


def test_static_text_matches_its_source():
    found = [node for node in tree().walk() if "data-static" in node.attrs]
    assert found, "the report has no static figures; this test would pass on anything"
    for node in found:
        expected = static_text(static_value(node.attrs["data-static"]), node.attrs.get("data-format") or "text")
        assert node.text().strip() == expected, (
            f"{node.attrs['data-static']} reads {node.text().strip()!r}, its source gives {expected!r}: "
            "run `python tests/test_lab_report.py --sync`")


def test_references_are_the_verified_entries_of_the_literature_file():
    source = PAGE.read_text(encoding="utf-8")
    start, end = source.index(REFS_START), source.index(REFS_END) + len(REFS_END)
    assert source[start:end] == references_block(), "the reference list is stale: run `python tests/test_lab_report.py --sync`"
    listed = {node.attrs["data-ref"] for node in tree().walk() if "data-ref" in node.attrs}
    entries = {entry["id"]: entry for entry in literature()}
    assert listed and listed == {key for key, entry in entries.items() if entry.get("verified") is True}
    page_text = tree().text()
    for key in listed:
        for field in ("authors", "title", "venue"):
            assert entries[key][field] in page_text, f"{key}: {field} is not on the page as recorded"
        assert entries[key]["url"] in source or html.escape(entries[key]["url"]) in source, f"{key}: link missing"


def _checked_elsewhere(node: Node) -> bool:
    # Static text and references are compared with their sources by the two tests above.
    return "data-static" in node.attrs or "data-ref" in node.attrs


def test_no_digit_is_typed_into_the_page():
    root = tree()
    body = next(node for node in root.walk() if node.tag == "body")
    texts = [("text", body.text(skip=_checked_elsewhere))]
    for node in body.walk():
        for attribute in ("aria-label", "title", "alt", "placeholder"):
            if node.attrs.get(attribute):
                texts.append((f"{node.tag} {attribute}", node.attrs[attribute]))
    typed = []
    for where, text in texts:
        for pattern in ALLOWED_DIGITS:
            text = re.sub(pattern, " ", text)
        typed += [f"{where}: ...{text[max(0, m.start() - 40):m.end() + 20].strip()}..." for m in re.finditer(r"\d+", text)]
    assert not typed, "digits typed into the page (bind them to a JSON field instead):\n" + "\n".join(typed)


def _in_details(node: Node) -> bool:
    return node.inside(lambda n: n.tag == "details")


def prose(part: str = "main") -> str:
    """The running text of the numbered sections: paragraphs and list items, a bound figure counting as one word.

    "main" is what a reader sees without opening anything, "details" the text inside the closed
    <details> blocks, "all" both.
    """
    def apart(node):
        return node.tag in ("figure", "table", "svg", "nav") or node.has_class("pending")
    parts = []
    for section in elements(lambda node: node.tag == "section" and "data-prose" in node.attrs):
        for node in section.walk():
            if node.tag in ("p", "li") and not node.inside(apart):
                if part == "all" or (part == "details") == _in_details(node):
                    parts.append(node.text(skip=apart, token=" figure "))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def word_count(part: str = "main") -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’&-]*", prose(part)))


def test_the_prose_is_a_report_and_not_a_book():
    count = word_count()
    assert WORDS_MIN <= count <= WORDS_MAX, f"the main text has {count} words; it should have {WORDS_MIN} to {WORDS_MAX}"
    extra = word_count("details")
    assert 0 < extra <= DETAIL_WORDS_MAX, f"the closed detail blocks hold {extra} words; at most {DETAIL_WORDS_MAX}"


def test_detail_blocks_start_closed_and_open_for_print():
    blocks = elements(lambda node: node.tag == "details")
    assert blocks, "the report has no detail blocks"
    for block in blocks:
        assert "open" not in block.attrs, "a detail block must start closed"
        assert block.has_class("more"), "a detail block uses the site's disclosure style"
        first = next((child for child in block.children if isinstance(child, Node)), None)
        assert first is not None and first.tag == "summary" and first.text().strip(), "a detail block needs a summary"
        assert not any(node.tag == "figure" or "data-chart" in node.attrs for node in block.walk()), "a chart cannot be drawn inside a closed block"
        assert block.inside(lambda n: n.tag == "section" and "data-prose" in n.attrs)
    script = SCRIPT.read_text(encoding="utf-8")
    printing = script[script.index("function printing(on)"):]
    printing = printing[:printing.index("\n  }\n")]
    assert 'all("details")' in printing and "el.open = true" in printing and "el.open = false" in printing, "print must open the detail blocks and close them again"


def test_the_report_has_its_sections_and_figures():
    root = tree()
    ids = [node.attrs.get("id") for node in root.walk() if node.tag == "section"]
    assert ids == ["summary", "simulator", "data", "measured", "reality", "agent", "limits", "scaling", "references"]
    figures = [node for node in root.walk() if node.tag == "figure"]
    assert 6 <= len(figures) <= 12, f"{len(figures)} figures and tables"
    for figure in figures:
        assert any(isinstance(child, Node) and child.tag == "figcaption" for child in figure.children), "a figure has no caption"
    assert any(node.tag == "svg" and node.inside(lambda n: n.has_class("diagram")) for node in root.walk()), "the simulator diagram is missing"
    summary = next(node for node in root.walk() if node.attrs.get("id") == "summary")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", summary.text(token="figure")).replace("Summary", "", 1).strip()) if s]
    assert 5 <= len(sentences) <= 6, f"the summary has {len(sentences)} sentences"


def test_the_report_is_in_american_english():
    body = next(node for node in tree().walk() if node.tag == "body")
    labels = " ".join(node.attrs.get("aria-label", "") for node in body.walk())
    for where, text in (("report.html", body.text(skip=lambda node: "data-ref" in node.attrs) + " " + labels),
                        ("report.js", SCRIPT.read_text(encoding="utf-8")),
                        ("hook-sidebar.js", SIDEBAR_SCRIPT.read_text(encoding="utf-8")),
                        ("hook-sidebar.css", SIDEBAR_SHEET.read_text(encoding="utf-8")),
                        ("report.css", SHEET.read_text(encoding="utf-8"))):
        found = sorted({match.group(0).lower() for match in BRITISH.finditer(text)})
        assert not found, f"{where} uses British spelling: {found}"


def test_no_question_is_asked_and_nothing_is_hyped():
    text = prose("all")
    assert "?" not in text, "the prose asks a question"
    for word in ("excited", "powerful", "robust", "cutting-edge", "state-of-the-art", "novel", "seamless", "unlock", "leverage"):
        assert not re.search(rf"\b{word}\b", text, re.I), f"the prose says {word!r}"
    assert "—" not in text, "the prose uses an em dash"


# ------------------------------------------------------------------ parts that are still to come

def test_a_part_whose_file_is_missing_is_replaced_by_one_line():
    root = tree()
    needs = [node for node in root.walk() if "data-needs" in node.attrs]
    pending = [node for node in root.walk() if "data-pending-for" in node.attrs]
    assert needs and pending
    lines = {frozenset(node.attrs["data-pending-for"].split()) for node in pending}
    for node in needs:
        assert frozenset(node.attrs["data-needs"].split()) in lines, f"data-needs={node.attrs['data-needs']!r} has no line to stand in for it"
    for node in pending:
        assert "hidden" in node.attrs, "a stand-in line must start hidden"
        text = re.sub(r"\s+", " ", node.text()).strip()
        assert "not been computed yet" in text and text.count(".") == 1, f"a stand-in is one plain sentence: {text!r}"
    # Outside a block that waits for its file, a figure may come only from a file that is expected to exist.
    waiting = {"track_record", "q3_call", "signal", "meta", "evidence"}
    for item in binds():
        files = {parse_bind(spec)[0] for spec in item["bind"].split(",")}
        covered = set()
        node = item["node"]
        while node is not None:
            covered |= set((node.attrs.get("data-needs") or "").split())
            node = node.parent
        assert not (files & waiting) - covered, f"{item['bind']} reads a file that may not exist, outside a block that waits for it"
    script = SCRIPT.read_text(encoding="utf-8")
    assert f'"{MISSING_TEXT}"' in script and "data-needs" in script and "data-pending-for" in script


def test_every_chart_on_the_page_is_one_the_script_can_draw():
    script = SCRIPT.read_text(encoding="utf-8")
    block = script[script.index("var CHARTS = {"):]
    block = block[:block.index("\n  };")]
    known = set(re.findall(r"^    ([a-z_]+): function", block, re.M))
    charts = [node for node in tree().walk() if "data-chart" in node.attrs]
    assert len(charts) >= 6
    for node in charts:
        assert node.attrs["data-chart"] in known, f"report.js has no chart named {node.attrs['data-chart']!r}"
        assert any("data-plot" in child.attrs for child in node.walk()), "a chart has nowhere to draw"
        assert node.inside(lambda n: "data-needs" in n.attrs), "a chart must wait for its file"


# ------------------------------------------------------------------ the section sidebar

SECTION_LABELS = {"summary": "Summary", "simulator": "The simulator", "data": "Data", "measured": "Measurements",
                  "reality": "Reality check", "agent": "The agent", "limits": "Limits", "scaling": "Scaling", "references": "References"}


def sidebar() -> Node:
    found = elements(lambda node: node.tag == "nav" and node.attrs.get("id") == "sections")
    assert len(found) == 1, "the report needs one <nav id=\"sections\">"
    return found[0]


def test_the_sidebar_has_one_item_for_each_section_in_order():
    nav = sidebar()
    assert nav.has_class("hook-sidebar") and nav.attrs.get("aria-label"), "the sidebar is a labeled navigation landmark"
    lists = [node for node in nav.walk() if node.has_class("hook-list")]
    assert len(lists) == 1
    items = [node for node in lists[0].walk() if node.has_class("hook-item")]
    assert all(node.tag == "a" for node in items), "sidebar items are real links, so they work without a pointer"
    sections = [node.attrs["id"] for node in tree().walk() if node.tag == "section"]
    assert [node.attrs.get("href") for node in items] == [f"#{name}" for name in sections], "one item for each section, in reading order"
    labels = [re.sub(r"\s+", " ", node.text()).strip() for node in items]
    assert labels == [SECTION_LABELS[name] for name in sections]
    for label in labels:
        assert 1 <= len(label.split()) <= 2, f"a sidebar label is two words at most: {label!r}"
    # Every anchor in the page lands on something.
    ids = {node.attrs["id"] for node in tree().walk() if node.attrs.get("id")}
    for node in tree().walk():
        target = node.attrs.get("href") or ""
        if target.startswith("#"):
            assert target[1:] in ids, f"{target} points nowhere"
    assert not elements(lambda node: node.has_class("contents")), "the inline contents list repeats the sidebar"


def test_the_sidebar_folds_into_a_control_on_narrow_screens():
    toggles = elements(lambda node: node.attrs.get("id") == "sections-toggle")
    assert len(toggles) == 1
    toggle = toggles[0]
    assert toggle.tag == "button" and toggle.attrs.get("type") == "button"
    assert toggle.attrs.get("aria-controls") == "sections" and toggle.attrs.get("aria-expanded") == "false"
    assert "hidden" in toggle.attrs, "the control appears only once the script that runs it has loaded"
    assert toggle.parent.has_class("report-side"), "the control sits in the sidebar's own box"
    assert any(isinstance(child, Node) and child.attrs.get("id") == "sections" for child in toggle.parent.children), "the control sits beside the list it opens"
    assert elements(lambda node: node.attrs.get("id") == "sections-here"), "the control names the section being read"
    sheet = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    assert re.search(r"@media \(min-width: 960px\)\s*\{\s*\.report-layout\s*\{[^}]*display:\s*grid", sheet), "two columns at desktop widths"
    assert re.search(r"\.report-side\.is-ready \.hook-sidebar\s*\{[^}]*display:\s*none", sheet), "the list is folded away on narrow screens"
    assert re.search(r"\.report-side\.is-ready\.is-open \.hook-sidebar\s*\{[^}]*display:\s*flex", sheet)
    assert re.search(r"@media print\s*\{.*\.report-side", sheet, re.S), "the sidebar is not printed"
    # The rail takes the site's accent, in both themes, through the token.
    assert re.search(r"\.report-side \.hook-sidebar\s*\{[^}]*--hook-color:\s*var\(--accent\)", sheet)
    for path in (SHEET, SIDEBAR_SHEET):
        css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        assert not re.search(r"\b(linear|radial|conic)-gradient\(", css), f"{path.name} uses a gradient"


def test_the_script_keeps_the_sidebar_on_the_section_being_read():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "HookSidebar.mount(nav" in script and 'current: "location"' in script, "the active item is marked aria-current=location"
    for needed in ('"scroll"', '"hashchange"', "location.hash", "scrollIntoView", '"Escape"', "aria-expanded", "setActive"):
        assert needed in script, f"report.js lacks {needed}"
    sidebar_script = SIDEBAR_SCRIPT.read_text(encoding="utf-8")
    assert 'setAttribute("aria-current"' in sidebar_script and 'removeAttribute("aria-current")' in sidebar_script
    done = shutil.which("node")
    if done:
        for path in (SCRIPT, SIDEBAR_SCRIPT):
            subprocess.run([done, "--check", str(path)], check=True, capture_output=True, timeout=60)


def test_the_hook_sidebar_port_keeps_its_credit_and_its_geometry():
    script = SIDEBAR_SCRIPT.read_text(encoding="utf-8")
    header = script.split("*/", 1)[0]
    assert "Rare UI" in header and "Hook Sidebar" in header and "https://rareui.com" in header
    assert "Copyright (c) 2026 Swami Malode" in header
    assert "What is approximated" in header, "the header says where the port departs from the original"
    license_text = SIDEBAR_LICENSE.read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Swami Malode" in license_text and "Attribution" in license_text and "https://rareui.com" in license_text
    # The numbers that define the shape and the motion; a change here is a change of design.
    assert re.search(r"CORNER\s*=\s*6\b", script)
    assert 'CORNER_PATH = "M0.5 0a6 6 0 0 0 6 6H12"' in script
    assert re.search(r'DASH\s*=\s*"2 2"', script)
    assert re.search(r"stiffness:\s*420,\s*damping:\s*34,\s*mass:\s*0\.7\b", script)
    sheet = SIDEBAR_SHEET.read_text(encoding="utf-8")
    assert re.search(r"\.hook-item\s*\{[^}]*padding:\s*6px 8px 6px 20px;[^}]*border-radius:\s*8px", sheet, re.S)
    assert re.search(r"\.hook-rail\s*\{[^}]*transition:\s*opacity 0\.2s", sheet, re.S)
    for name in ("--hook-color", "--hook-ink-rest", "--hook-rail-hover"):
        assert name in sheet
    # Nothing moves for a reader who asked for less motion.
    assert "prefers-reduced-motion" in script and "prefers-reduced-motion" in sheet
    # The original React source is not shipped.
    assert not list(STATIC.rglob("*.tsx"))


def test_the_credits_name_the_sidebar_and_its_author():
    credits = next(node for node in tree().walk() if node.has_class("credits"))
    line = next((node for node in credits.walk() if node.tag == "li" and "Hook Sidebar" in node.text()), None)
    assert line is not None, "the credits do not name Hook Sidebar"
    text = re.sub(r"\s+", " ", line.text())
    assert "Rare UI" in text and "Swami Malode" in text and "Navigation adapted from" in text
    links = {node.attrs.get("href") for node in line.walk() if node.tag == "a"}
    assert "https://rareui.com" in links, "the license asks for a visible link to the Rare UI site"
    assert "/static/lab/LICENSE-hook-sidebar.txt" in links and "/static/lab/LICENSE-gooey-nav.txt" in links
    assert line.inside(lambda node: node.attrs.get("id") == "references")


def test_the_forecast_is_described_without_build_dates():
    """The main text says the estimate was fixed before United reports; the freeze time appears only in its own record."""
    reality = next(node for node in tree().walk() if node.attrs.get("id") == "reality")
    main_text = re.sub(r"\s+", " ", reality.text(skip=lambda node: node.tag == "details", token="#"))
    assert "fixed before United reports on #" in main_text
    page_text = tree().text().lower()
    assert "timestamp" not in page_text, "there is no timestamp proof, so the page does not mention one"
    build_dates = re.compile(r"(^|[.:])(generated_at|frozen_at|retrieved_on|as_of)$|comparators\.[a-z_]+\.as_of$")
    for item in binds():
        for spec in item["bind"].split(","):
            if spec.strip() == "market_clock:as_of":      # the last day of market data, a data date
                continue
            if build_dates.search(spec.strip()):
                assert _in_details(item["node"]), f"{spec} prints a build date in the main text"
                assert item["format"] == "text", f"{spec}: the record's own time is shown as written, not reformatted"
    report_day = [item for item in binds() if item["bind"] == "q3_call:forecast.target.report_expected_on"]
    assert report_day and not _in_details(report_day[0]["node"])


# ------------------------------------------------------------------ the binder itself, under node

NODE_CHECK = r"""
const report = require(process.argv[process.argv.length - 1]);
const fs = require("fs");
const path = require("path");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const cache = {};
function lookup(name) {
  if (!(name in cache)) {
    const file = path.join(input.dir, name + ".json");
    cache[name] = fs.existsSync(file) ? { status: "ok", data: JSON.parse(fs.readFileSync(file, "utf8")) } : { status: "missing" };
  }
  return cache[name];
}
const out = input.binds.map((b) => report.evaluate(b.bind, b.format, b.scale, lookup));
process.stdout.write(JSON.stringify({ missing: report.MISSING, formats: report.FORMAT_NAMES, results: out }));
"""


def run_binder() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the binder is checked by the Python mirror above only")
    subprocess.run([node, "--check", str(SCRIPT)], check=True, capture_output=True, timeout=60)
    payload = {"dir": str(DATA), "binds": [{"bind": b["bind"], "format": b["format"], "scale": b["scale"]} for b in binds()]}
    done = subprocess.run([node, "-e", NODE_CHECK, str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_script_fills_every_figure_whose_file_is_built():
    out = run_binder()
    assert out["missing"] == MISSING_TEXT
    assert set(out["formats"]) == format_names()
    problems = []
    for item, result in zip(binds(), out["results"]):
        files = {parse_bind(spec)[0] for spec in item["bind"].split(",")}
        if all(built(name) is not None for name in files):
            if not result["ok"]:
                problems.append(f"{item['bind']} ({item['format']}): {result['reason']}")
            elif not result["text"].strip() or "NaN" in result["text"] or "undefined" in result["text"]:
                problems.append(f"{item['bind']} renders as {result['text']!r}")
        else:
            assert not result["ok"] and result.get("file") in files, f"{item['bind']} should wait for its file"
    assert not problems, "\n".join(problems)


def test_the_script_and_this_file_agree_on_values():
    out = run_binder()
    for item, result in zip(binds(), out["results"]):
        expected = bound_value(item, kinds=("built",))
        if expected is None or not result["ok"]:
            continue
        got = result["value"]
        pairs = zip(expected, got) if isinstance(expected, list) and isinstance(got, list) else [(expected, got)]
        for a, b in pairs:
            assert (abs(a - b) <= 1e-9 * max(1.0, abs(a))) if is_number(a) and is_number(b) else a == b, f"{item['bind']}: {a!r} here, {b!r} in report.js"


# ------------------------------------------------------------------ sentences that state a finding in words

def _lab(name: str):
    return json.loads((LAB_DATA / name).read_text(encoding="utf-8"))


def claims():
    """(file names, sentence in the report, check). A check runs only when its files are built."""
    def cost(d):
        test = d["cost_clock"]["by_split"]["test"]
        assert all(q["error"] < 0 for q in test["quarters"]) and len(test["quarters"]) == test["n"]
        assert test["inside_p10_p90"] < 0.8
        assert "does not hedge" in d["cost_clock"]["hedging_statement"]["finding"]
        assert d["cost_clock"]["fit"]["sample"].endswith("2023Q4")      # "fitted on quarters through 2023"
        assert d["cost_clock"]["predicted_2026q3"]["quarter"] == "2026Q3"
        check = d["cost_clock"]["delta_check"]
        assert check["method_closer_than_assumption"] is True and check["quarter"] == "2026Q3"

    def fare(d):
        f = d["fare_clock"]
        summary = f["oos_test"]["summary"]
        assert summary["test_h4"]["winner"] != "fitted" and summary["test_h1"]["winner"] != "fitted"
        assert summary["test_h4"]["distinguishable"] is False
        assert f["primary_spec"]["chosen_after_test_split_seen"] is True
        rests = f["smooth"]["through_latest"]["rests_on_latest_quarter"]
        assert rests["fitted_before_it"]["half_life_quarters"] - rests["fitted_with_it"]["half_life_quarters"] > 0.5
        share = f["smooth"]["through_latest"]["one_year_share"]
        assert share["p05"] > 0.25 and share["p95"] - share["p05"] < 0.6
        year = f["industry"]["elasticity_at_months"]["12"]
        assert year["lo"] < 0 < year["hi"]
        assert f["asymmetry"]["p_value"] > 0.1
        checks = f["united"]["data_checks"]
        assert checks["max_abs_gap_reported_vs_computed_trasm_cents"] < 0.01
        assert len(f["industry"]["missing_cpi_months"]) == 1
        latest = f["management_comparison"]["quarters"][0]
        assert latest["period"].startswith("2026") and latest["raw"]["fuel_cost_per_asm_change_cents"] > checks["fit_split_fuel_shock_range_cents"][1]

    def delta(d):
        q = d["fare_clock"]["delta_september_quarter"]
        assert q["eps_position"] == "below_range" and q["eps_actual_adj"] < q["eps_guided_low"]
        assert q["fuel_actual_adj_usd_gal"] > q["fuel_assumed_usd_gal"] and q["adj_trasm_yoy_pct"] > 0
        # The report reads Delta's quarter from the fare clock; it must say what data/lab/dal_check.json says.
        source = _lab("dal_check.json")
        guided, actual = source["guidance_sep_quarter"], source["actuals_sep_quarter"]
        assert (q["eps_guided_low"], q["eps_guided_high"], q["fuel_assumed_usd_gal"]) == (guided["eps_low"], guided["eps_high"], guided["fuel_price_assumed_usd_gal"])
        assert (q["eps_actual_adj"], q["fuel_actual_adj_usd_gal"]) == (actual["adj_eps_diluted"], actual["fuel_price_adj_usd_gal"])
        assert guided["made_on"][5:7] == "07" and actual["period"] == "2026Q3"

    def capacity(d):
        c = d["capacity_response"]
        last = c["curve"][-1]
        assert c["verdict"] == "none" and c["verdict_other_specification"] != c["verdict"]
        assert last["quarters_after"] == 4 and last["lo"] < 0 < last["hi"]
        assert c["by_split"]["test"]["response_helps"] is False

    def demand(d):
        s = d["demand_signal"]
        assert s["current_quarter_note"]["2026Q3"]["yoy"] < 0
        assert s["coverage"]["missing_days"] == 0
        assert abs(s["versus_united"]["correlation"]["tsa_vs_ual_rpm"]["r"]) < 0.3

    def market(d):
        m = d["market_clock"]
        same_day = m["by_split"]["test"]["same_day_ual"]
        assert same_day["beta"] < 0 and same_day["ci"][1] < 0
        assert m["verdict"]["label"] == "neither"
        assert m["select_to_test"][0]["measure"] == "same-day slope" and m["select_to_test"][0]["select_distinguishable"] is False
        rule = m["trading_rule"]
        assert "select" in rule["definition"]["chosen_by"]
        assert rule["by_split"]["select"]["mean_trade_net"] > 0 > rule["by_split"]["test"]["mean_trade_net"]
        assert rule["by_split"]["test"]["total_net"] < 0

    def fares(d):
        f = d["daily_fares_2022"]
        assert abs(f["fare_path"]["ua"]["start_to_end_pct"]) < 5
        assert f["fare_path"]["ua"]["peak"]["date"] > f["fuel_path"]["peak"]["date"]
        assert 0 < f["robustness"]["united_intervals_including_zero"] <= f["robustness"]["united_specifications"]
        assert f["dataset"]["licence_id"] == "CC-BY-4.0" and "Dillon Wong" in f["dataset"]["attribution"]
        assert f["dataset"]["search_dates"]["first"][:7] == "2022-04" and f["dataset"]["search_dates"]["last"][:7] == "2022-10"

    def peers(d):
        p = d["peer_recovery"]
        assert set(p["definition"]["carriers"]) == {"UAL", "DAL", "AAL"}
        pooled = p["pooled"]["one_year_share"]
        own = d["fare_clock"]["smooth"]["through_latest"]["one_year_share"]
        assert pooled["p95"] - pooled["p05"] >= own["p95"] - own["p05"]
        assert pooled["p05"] <= own["estimate"] <= pooled["p95"]
        assert p["interval_level"] == d["fare_clock"]["interval_level"]

    def replay(d):
        t = d["track_record"]
        score = {row["model"]: row for row in t["scores"] if row["split"] == "test"}
        assert t["splits"]["test"][0] == 2024 and "cannot be re-run in past years" in t["method"]["what_is_replayed"]
        # "the same as the midpoint itself and as a rule with no recovery: a three-way tie", to the cent the page shows
        assert round(score["fitted"]["mae"], 2) == round(score["guidance_mid"]["mae"], 2) == round(score["costs_only"]["mae"], 2)
        # "a midpoint given the same range scores no worse"
        like = t["fitted_against_midpoint"]["test"]
        assert like["like_for_like"]["midpoint_with_the_same_range_crps"] <= score["fitted"]["crps"] and like["n"] == score["fitted"]["n"]
        # "the one clear positive result": the fuel price at quarter end against the price United stated
        fuel = t["fuel_price_test"]
        assert fuel["mae_marked"] < 0.5 * fuel["mae_guided"] and fuel["marked_closer_in"] > fuel["n"] / 2
        assert fuel["by_split"]["select"]["n"] < fuel["n"]
        # "its gap to the midpoint did not predict the share move on results day": the range spans zero
        day = t["results_day_test"]["by_split"]["test"]
        assert day["lo"] < 0 < day["hi"] and day["n"] == score["fitted"]["n"]
        # "chosen on the guided quarters before 2024"; "treats fuel that moved after the middle of the quarter as not recovered"
        assert t["selection"]["chosen"] == "late_moves" and all(q < "2024" for q in t["selection"]["quarters"])
        assert "after the middle of the quarter" in t["method"]["variants"]["late_moves"]
        delta = t["delta_check"]
        assert delta["period"] == "2026Q3" and delta["airline"] == "DAL"
        assert abs(delta["calls"]["costs_only"] - delta["actual_eps"]) < abs(delta["calls"]["fitted"]["p50"] - delta["actual_eps"])

    def forward(d):
        q = d["q3_call"]
        target, guided = q["forecast"]["target"], q["forecast"]["guidance"]
        assert target["company"] == "UAL" and target["period"] == "2026Q3"       # "the third quarter of 2026"
        assert guided["made_on"] < q["frozen_at"][:10] <= target["report_expected_on"], "fixed before United reports"
        assert guided["fuel_assumed_usd_gal"] == d["cost_clock"]["predicted_2026q3"]["management"]["value"]
        chain = {link["link"]: link for link in q["chain"]}
        eps = chain["adj_eps_diluted"]
        # "takes the midpoint, subtracts the fuel cost it expects not to be recovered"
        assert abs(eps["guidance_mid"] - eps["unrecovered_per_share"] - eps["p50"]) < 0.005
        assert eps["guidance_mid"] == (guided["eps_low"] + guided["eps_high"]) / 2
        assert eps["earlier_misses_n"] == q["odds"]["n_samples"]
        assert abs(q["odds"]["below_range"] + q["odds"]["inside_range"] + q["odds"]["above_range"] - 1) < 1e-4
        # the table: United said "more than"; the unit revenue link does not feed the earnings figure
        assert chain["trasm_yoy"]["management_detail"]["qualifier"] == "more_than" and chain["trasm_yoy"]["not_used_by_eps_link"] is True
        assert "never defined" in chain["recovery_share"]["management_note"]
        # "sits above the estimate", "not machine-checked", "thinly traded"
        consensus = q["comparators"]["consensus_eps"]
        assert consensus["value"] > eps["p50"] and "not machine-checked" in consensus["read_via"]
        assert "thin" in q["comparators"]["market_odds_beat"]["note"].lower()
        assert q["sha256"] and q["frozen_file"].endswith("q3_call_frozen.json")

    def daily(d):
        text = " ".join(d["signal"]["plain_summary"])
        assert "not a second forecast" in text and "no trading rule is tested" in text

    return [(("cost_clock",), "the fuel-bill range is too narrow, and the method beat Delta's own planning figure", cost),
            (("fare_clock",), "the curve specified in advance lost; the one-year share is determined and the speed is not", fare),
            (("fare_clock",), "Delta paid more for fuel than planned, grew unit revenue and still missed its guided range", delta),
            (("capacity_response",), "no capacity response could be established", capacity),
            (("demand_signal",), "the traveler count fell and is a weak guide to United", demand),
            (("market_clock",), "the stock reacts the same day, shows no drift, and the trading rule lost money", market),
            (("daily_fares_2022",), "fares rose after fuel and came back; the evidence is weak", fares),
            (("peer_recovery", "fare_clock"), "pooling three airlines did not narrow the range", peers),
            (("track_record",), "the replay is a three-way tie, the fuel step is the positive result, and no recovery was closer for Delta", replay),
            (("q3_call", "cost_clock"), "the estimate for the third quarter of 2026, fixed before United reports", forward),
            (("signal",), "the daily path is the same method on earlier dates, with no trading rule tested", daily)]


@pytest.mark.parametrize("files, sentence, check", claims(), ids=[c[1] for c in claims()])
def test_what_the_prose_says_in_words_still_holds(files, sentence, check):
    data = {name: built(name) for name in files}
    if any(value is None for value in data.values()):
        pytest.skip(f"{', '.join(name for name, value in data.items() if value is None)} is not built yet")
    check(data)


def test_what_the_prose_says_about_sources_still_holds():
    assert all(record["picked_by"] == "rules" and record["verified"] is True for record in _lab("ual_guidance.json"))
    market = _lab("manifests/market.json")
    assert market["outputs"][0]["path"].endswith("fuel_daily.csv") and market["outputs"][1]["path"].endswith("prices_daily.csv")
    checks = market["cross_checks"]
    for check in (checks["eia_vs_fred_fuel"], checks["bls_vs_fred_cpi"]["CUSR0000SETG01"]):
        assert check["max_abs_diff"] == 0 and not check["mismatches"]
    assert len(_lab("dal_check.json")["documents"]) == 2
    quotes = _lab("manifests/fares_2022.json")["entries"][0]
    assert quotes["creator"] == "Dillon Wong" and quotes["licence_id"] == "CC-BY-4.0"
    assert "https://rareui.com" in (LAB / "LICENSE-gooey-nav.txt").read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    assert 'href="https://rareui.com"' in page and "Navigation adapted from" in page and "CC BY 4.0" in page
    # "After one rewrite it is assembled from the tool results without the model"; "a rules planner"
    chat = (ROOT / "app" / "lab" / "chat.py").read_text(encoding="utf-8")
    assert "one rewrite" in chat and "rules_plan" in chat and "typed_figures" in chat


if __name__ == "__main__":
    if sys.argv[1:] == ["--sync"]:
        before = PAGE.read_text(encoding="utf-8")
        after = synced(before)
        PAGE.write_text(after, encoding="utf-8")
        print(f"report.html: static text and references {'rewritten' if after != before else 'already current'}; "
              f"{word_count()} words of main text, {word_count('details')} in detail blocks")
    elif sys.argv[1:] == ["--words"]:
        print(f"{word_count()} main, {word_count('details')} in detail blocks")
    elif sys.argv[1:] == ["--prose"]:
        print(prose("all"))
    else:
        print("usage: python tests/test_lab_report.py --sync | --words | --prose")
