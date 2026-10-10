"""Offline checks on the lab front end (app/static/lab) and its placeholder data.

Nothing here starts a server or a browser. The checks are the ones a reviewer would
otherwise do by eye: every local file a page asks for exists, nothing is fetched from
another host at run time, placeholder data is flagged and stays out of the real data
folder, and third-party code carries its version, hash and licence.
"""
from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
LAB = STATIC / "lab"
DATA = STATIC / "data"
FIXTURES = DATA / "fixtures"

# Paths the server answers itself (app/server.py), not files under /static.
ROUTES = {"/", "/lab", "/report", "/research"}
# One fixture per output file in docs/lab_contract.md, plus the API responses the page draws.
CONTRACT_FILES = ("meta", "cost_clock", "fare_clock", "market_clock", "track_record", "q3_call", "signal", "evidence")
API_FIXTURES = ("scenario", "grid", "grid_target", "today", "chat_stream", "chat_report")
# The only addresses our own scripts may mention: an XML namespace and the credit link.
ALLOWED_IN_OWN_SCRIPTS = {"http://www.w3.org/2000/svg", "https://rareui.com"}
# Vendored libraries may name XML namespaces and their own home page in a banner.
ALLOWED_IN_VENDORED = re.compile(r"^(http://www\.w3\.org/|https://d3js\.org$|https://observablehq\.com|https://github\.com/)")

URL = re.compile(r"https?://[^\s\"'`)<>\\]+")
LOADING_TAGS = {"script": ("src",), "img": ("src", "srcset"), "source": ("src", "srcset"), "video": ("src", "poster"),
                "audio": ("src",), "iframe": ("src",), "embed": ("src",), "object": ("data",), "use": ("href", "xlink:href"),
                "image": ("href", "xlink:href")}
LOADING_RELS = {"stylesheet", "preload", "modulepreload", "prefetch", "icon", "apple-touch-icon", "manifest", "preconnect", "dns-prefetch"}


class Refs(HTMLParser):
    """Collects (tag, attribute, value, loads) for every reference in a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, str, bool]] = []
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self._inside: str | None = None

    def handle_starttag(self, tag, attrs):
        values = {name: value or "" for name, value in attrs}
        if tag == "link":
            rels = set(values.get("rel", "").lower().split())
            if "href" in values:
                self.refs.append((tag, "href", values["href"], bool(rels & LOADING_RELS)))
        elif tag in LOADING_TAGS:
            for name in LOADING_TAGS[tag]:
                if values.get(name):
                    self.refs.append((tag, name, values[name], True))
        elif tag in ("a", "area", "form"):
            name = "action" if tag == "form" else "href"
            if values.get(name):
                self.refs.append((tag, name, values[name], False))
        if "style" in values:
            self.inline_styles.append(values["style"])
        if tag == "script" and not values.get("src"):
            self._inside = "script"
        elif tag == "style":
            self._inside = "style"

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._inside = None

    def handle_data(self, data):
        if self._inside == "script":
            self.inline_scripts.append(data)
        elif self._inside == "style":
            self.inline_styles.append(data)


def pages() -> list[Path]:
    return sorted(LAB.glob("*.html"))


def parse(page: Path) -> Refs:
    parser = Refs()
    parser.feed(page.read_text(encoding="utf-8"))
    return parser


def local_file(reference: str, page: Path) -> Path | None:
    """The file a same-site reference points at, or None for a route, an anchor or another scheme."""
    target = reference.split("#", 1)[0].split("?", 1)[0]
    if not target or target in ROUTES or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("//"):
        return None
    if target.startswith("/static/"):
        return STATIC / target[len("/static/"):]
    if target.startswith("/"):
        return ROOT / "__no_such_route__" / target.lstrip("/")
    return page.parent / target


def own_scripts() -> list[Path]:
    return sorted(path for path in LAB.glob("*.js"))


def stylesheets() -> list[Path]:
    return sorted(LAB.glob("*.css"))


def versions() -> list[dict]:
    rows = []
    for line in (LAB / "vendor" / "VERSIONS").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        file, package, version, licence, licence_file, sha256 = [part.strip() for part in line.split("|")]
        rows.append({"file": file, "package": package, "version": version, "licence": licence, "licence_file": licence_file, "sha256": sha256})
    return rows


# ------------------------------------------------------------------- pages

def test_there_is_a_lab_page():
    assert (LAB / "index.html").is_file()
    assert pages(), "no lab pages found"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_local_reference_resolves_to_a_file(page):
    missing = []
    for tag, attribute, value, _ in parse(page).refs:
        for part in ([item.strip().split(" ")[0] for item in value.split(",")] if attribute == "srcset" else [value]):
            target = local_file(part, page)
            if target is not None and not target.is_file():
                missing.append(f"<{tag} {attribute}={part!r}>")
    assert not missing, f"{page.name} points at files that do not exist: {missing}"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_no_page_loads_anything_from_another_host(page):
    parsed = parse(page)
    outside = [f"<{tag} {attribute}={value!r}>" for tag, attribute, value, loads in parsed.refs
               if loads and (re.match(r"^(https?:)?//", value.strip(), re.I) or value.strip().lower().startswith("data:text/javascript"))]
    assert not outside, f"{page.name} loads from another host: {outside}"
    for block in parsed.inline_scripts + parsed.inline_styles:
        assert not URL.search(block), f"{page.name} has an address inside an inline script or style"
        assert "@import" not in block


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_external_links_do_not_hand_over_the_opener(page):
    text = page.read_text(encoding="utf-8")
    for match in re.finditer(r"<a\b[^>]*\bhref=\"https?://[^\"]*\"[^>]*>", text, re.I):
        assert "noopener" in match.group(0), f"{page.name}: {match.group(0)} needs rel=noopener"


@pytest.mark.parametrize("sheet", stylesheets(), ids=lambda p: p.name)
def test_stylesheets_load_only_local_files(sheet):
    text = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
    assert "@import" not in text, f"{sheet.name} imports another stylesheet"
    for reference in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", text):
        assert not re.match(r"^(https?:)?//", reference), f"{sheet.name} loads {reference}"
        if reference.startswith("data:"):
            continue
        target = local_file(reference, sheet)
        assert target is not None and target.is_file(), f"{sheet.name}: url({reference}) does not resolve to a file"


@pytest.mark.parametrize("sheet", stylesheets(), ids=lambda p: p.name)
def test_stylesheets_have_no_gradients(sheet):
    text = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"\b(linear|radial|conic)-gradient\(", text), f"{sheet.name} uses a gradient"


@pytest.mark.parametrize("script", own_scripts(), ids=lambda p: p.name)
def test_own_scripts_name_no_other_host(script):
    text = script.read_text(encoding="utf-8")
    found = {match.rstrip(".,;") for match in URL.findall(text)}
    assert found <= ALLOWED_IN_OWN_SCRIPTS, f"{script.name} mentions {sorted(found - ALLOWED_IN_OWN_SCRIPTS)}"
    # Requests go to this site only: every fetch target is a path or a variable built from one.
    assert not re.search(r"fetch\(\s*[\"'`](https?:)?//", text)
    assert not re.search(r"\bimport\s*\(", text), f"{script.name} imports a module at run time"


def test_vendored_scripts_name_only_namespaces_and_their_home_page():
    for row in versions():
        if not row["file"].endswith(".js"):
            continue
        text = (LAB / row["file"]).read_text(encoding="utf-8")
        strange = sorted({match for match in URL.findall(text) if not ALLOWED_IN_VENDORED.match(match)})
        assert not strange, f"{row['file']} mentions {strange}"


def test_no_emoji_in_the_front_end():
    emoji = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
    for path in [*pages(), *own_scripts(), *stylesheets()]:
        found = emoji.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} contains {found[:5]}"


# ------------------------------------------------------ the application shell

APP_SHEETS = ("base.css", "lab.css", "gooey-nav.css")
APP_SCRIPTS = ("lab.js", "charts.js", "gooey-nav.js")
BRITISH = re.compile(r"\b(behaviours?|travellers?|modell(ed|ing)|licences?|grey|coloured|colours?|centre|favourite|analyse[ds]?)\b", re.I)


def test_nothing_in_the_application_is_cut_off_with_an_ellipsis():
    for name in APP_SHEETS:
        text = re.sub(r"/\*.*?\*/", "", (LAB / name).read_text(encoding="utf-8"), flags=re.S)
        assert "text-overflow" not in text and "line-clamp" not in text, f"{name} truncates text"


def test_what_a_visitor_reads_uses_american_spelling():
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.found, self._skip = [], False

        def handle_starttag(self, tag, attrs):
            self._skip = tag in ("script", "style")
            self.found += [value for name, value in attrs if name in ("aria-label", "title", "placeholder", "content", "alt") and value]

        def handle_endtag(self, tag):
            self._skip = False

        def handle_data(self, data):
            if not self._skip:
                self.found.append(data)

    page = Text()
    page.feed((LAB / "index.html").read_text(encoding="utf-8"))
    british = [text.strip() for text in page.found if BRITISH.search(text)]
    assert not british, f"index.html: {british}"
    for name in APP_SCRIPTS:
        source = (LAB / name).read_text(encoding="utf-8")
        # Prose is a string literal with a space in it; identifiers, routes and file names are not.
        # A literal that starts like a CSS selector is one, not prose.
        prose = [m.group(0) for m in re.finditer(r'"(?:[^"\\\n]|\\.)*"', source) if " " in m.group(0) and m.group(0)[1] not in "#.["]
        british = [text[:80] for text in prose if BRITISH.search(text)]
        assert not british, f"{name}: {british}"
    assert ">Behavior<" in (LAB / "index.html").read_text(encoding="utf-8")


def test_the_three_views_and_their_switcher_are_in_the_page():
    page = (LAB / "index.html").read_text(encoding="utf-8")
    for view in ("simulator", "behaviour", "reality"):   # the hash routes keep their first spelling
        assert f'data-view="{view}"' in page and f'data-value="{view}"' in page
    assert 'id="view-nav"' in page and "gooey-nav.js" in page
    assert 'href="/report"' in page and 'href="/research"' not in page   # the earlier workstation is not linked
    assert 'id="fixture-pill"' in page and 'id="about-btn"' in page


# ------------------------------------------------------- third-party files

def test_versions_file_covers_every_vendored_and_font_file():
    listed = {row["file"] for row in versions()}
    shipped = {f"vendor/{path.name}" for path in (LAB / "vendor").iterdir() if path.suffix == ".js"}
    shipped |= {f"fonts/{path.name}" for path in (LAB / "fonts").iterdir() if path.suffix in (".woff2", ".woff", ".ttf", ".otf")}
    assert shipped, "no third-party files found"
    assert shipped == listed, f"VERSIONS and the folders disagree: {sorted(shipped ^ listed)}"


@pytest.mark.parametrize("row", versions(), ids=lambda r: r["file"])
def test_each_third_party_file_has_a_version_a_hash_and_a_licence(row):
    path = LAB / row["file"]
    assert path.is_file()
    assert re.fullmatch(r"\d+\.\d+\.\d+", row["version"]), f"{row['file']}: version {row['version']!r} is not exact"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], f"{row['file']} does not match the hash in VERSIONS"
    licence = LAB / row["licence_file"]
    assert licence.is_file() and licence.stat().st_size > 200, f"{row['file']}: licence file {row['licence_file']} is missing or empty"
    assert row["licence"]


def test_pinned_versions_are_the_ones_agreed():
    pinned = {row["package"]: row["version"] for row in versions()}
    assert pinned == {"d3": "7.9.0", "@observablehq/plot": "0.6.17", "geist": "1.7.2"}


def test_gooey_nav_port_keeps_its_credit_and_its_geometry():
    script = (LAB / "gooey-nav.js").read_text(encoding="utf-8")
    header = script.split("*/", 1)[0]
    assert "Rare UI" in header and "https://rareui.com" in header
    assert "Copyright (c) 2026 Swami Malode" in header
    licence = (LAB / "LICENSE-gooey-nav.txt").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Swami Malode" in licence and "Attribution" in licence
    # The numbers that define the motion and the shape; a change here is a change of design.
    assert re.search(r"stiffness:\s*200,\s*damping:\s*28,\s*mass:\s*1\b", script)
    assert re.search(r"NECK_BREAK\s*=\s*0\.22\b", script) and re.search(r"NECK_H\s*=\s*100\b", script)
    for size, radius, separation in (("xs", 8, 14), ("sm", 10, 16), ("md", 12, 20), ("lg", 14, 24)):
        assert re.search(rf"{size}:\s*\{{\s*radius:\s*{radius},\s*separation:\s*{separation}\s*\}}", script), size
    assert re.search(r"CLOSED\s*=\s*-1\b", script)
    # The original React source is not shipped, and the site shows the credit.
    assert not list(STATIC.rglob("*.tsx"))
    app = (LAB / "lab.js").read_text(encoding="utf-8")
    assert "Navigation adapted from" in app and "https://rareui.com" in app
    sheet = (LAB / "gooey-nav.css").read_text(encoding="utf-8")
    assert "--gooey-active" in sheet


# ------------------------------------------------------------------ tokens

def test_dark_theme_is_declared_the_same_way_for_the_system_and_for_the_toggle():
    text = re.sub(r"/\*.*?\*/", "", (LAB / "tokens.css").read_text(encoding="utf-8"), flags=re.S)
    system = re.search(r":root:not\(\[data-theme=\"light\"\]\)\s*\{(.*?)\}", text, re.S)
    toggle = re.search(r":root\[data-theme=\"dark\"\]\s*\{(.*?)\}", text, re.S)
    assert system and toggle
    declarations = lambda block: sorted(line.strip() for line in block.split(";") if line.strip())
    assert declarations(system.group(1)) == declarations(toggle.group(1))


def test_the_page_follows_the_system_theme_and_respects_reduced_motion():
    tokens = (LAB / "tokens.css").read_text(encoding="utf-8")
    base = (LAB / "base.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in tokens
    assert "prefers-reduced-motion: reduce" in base
    assert "prefers-reduced-motion" in (LAB / "gooey-nav.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------- fixtures

def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_every_contract_file_and_api_response_has_a_fixture():
    names = {path.stem for path in fixture_files()}
    missing = [name for name in (*CONTRACT_FILES, *API_FIXTURES) if name not in names]
    assert not missing, f"no fixture for {missing}"


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: p.name)
def test_every_fixture_is_flagged(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} must be an object so it can carry the flag"
    assert data.get("fixture") is True, f"{path.name} lacks \"fixture\": true"


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: p.name)
def test_fixtures_look_like_placeholders(path):
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if isinstance(data.get("headline"), str):
        assert "placeholder" in data["headline"].lower(), f"{path.name}: the headline must say it is a placeholder"
    # No real hash, and no address that could be mistaken for a source.
    for digest in re.findall(r"\b[0-9a-f]{64}\b", text):
        assert len(set(digest)) == 1, f"{path.name} carries what looks like a real SHA-256"
    for address in URL.findall(text):
        assert address.startswith("https://example.invalid/"), f"{path.name} points at {address}"


def test_no_real_data_file_is_a_fixture():
    for path in sorted(DATA.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        flagged = data.get("fixture") is True if isinstance(data, dict) else any(isinstance(item, dict) and item.get("fixture") is True for item in data)
        assert not flagged, f"{path.name} is in the real data folder but is flagged as a fixture"


def test_scenario_fixture_has_the_shape_the_page_reads():
    run = json.loads((FIXTURES / "scenario.json").read_text(encoding="utf-8"))
    assert set(run["modes"]) == {"costs_only", "management", "measured"}
    assert isinstance(run["fuel"]["reference_usd_gal"], (int, float))
    for mode in run["modes"].values():
        assert mode["status"] == "ok" and mode["quarters"]
        for quarter in mode["quarters"]:
            for metric in ("operating_income_musd", "operating_margin", "fcf_musd", "eps_change"):
                assert {"p10", "p50", "p90"} <= set(quarter[metric])
            assert {"operating_income_musd", "operating_margin", "fcf_musd"} <= set(quarter["no_shock"])
        assert {"operating_income_musd", "operating_income_change_musd", "eps_change", "fcf_musd"} <= set(mode["next_12m"])
        assert [step["step"] for step in mode["bridge"]][-1] == "net"


@pytest.mark.parametrize("name, direction", [("grid", "below"), ("grid_target", "at_or_above")])
def test_grid_fixtures_have_the_shape_the_page_reads(name, direction):
    grid = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert grid["direction"] == direction
    assert len(grid["cells"]) == len(grid["x"]["values"]) * len(grid["y"]["values"])
    assert all({"x", "y", "prob", "p50"} <= set(cell) and 0 <= cell["prob"] <= 1 for cell in grid["cells"])
    assert grid["boundary"] and all({"x", "y"} <= set(point) for point in grid["boundary"])


def test_chat_fixtures_have_the_shape_the_page_reads():
    stream = json.loads((FIXTURES / "chat_stream.json").read_text(encoding="utf-8"))
    assert {event["kind"] for event in stream["events"]} <= {"plan", "tool_call", "tool_result", "check"}
    answer = json.loads((FIXTURES / "chat_report.json").read_text(encoding="utf-8"))
    report = answer["report"]
    assert {"title", "summary", "sections", "charts", "assumptions", "evidence_ids", "verified", "unsupported"} <= set(report)
    runs = {run["run_id"] for run in answer["scenarios"]}
    for chart in report["charts"]:
        if chart.get("kind") == "scenario":
            assert chart["run_id"] in runs
        elif chart.get("file"):
            assert (FIXTURES / chart["file"]).is_file()
