#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "beautifulsoup4>=4.12", "markdownify>=1.2"]
# ///
"""Archive a Fandom (or any MediaWiki) wiki as an Obsidian-ready Markdown folder.

    fandom2obsidian.py https://star-wars-rpg-ffg.fandom.com/wiki/ --out ~/Fandom/FFG

Every article becomes one note; internal links become [[wikilinks]] (redirects
resolve to their target, and become `aliases`); infoboxes become callouts;
citations become footnotes; categories become linked notes. Re-running only
re-fetches pages whose revision changed, so an interrupted run just resumes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from markdownify import MarkdownConverter

USER_AGENT = "fandom2obsidian/1.0 (personal offline wiki archive)"
STATE_FILE = ".fandom2obsidian.sqlite"
PROFILE_FILE = "fandom2obsidian.profile.json"
ATTACH_DIR = "_attachments"
MEMBERS_MARK = "%% fandom2obsidian: category members below, regenerated each run %%"

# Page furniture that is useless (or actively noisy) offline.
STRIP = [
    "script", "style", "noscript", ".toc", "#toc", ".mw-editsection", ".navbox",
    ".mw-empty-elt", ".noprint", ".printfooter", ".catlinks", ".mw-cite-backlink",
    ".pi-navigation", "#title-eraicons", ".mobileTopIcons", ".mobile-only",
]


# --------------------------------------------------------------------- HTTP
class RateLimiter:
    """Spaces requests at least `interval` seconds apart across all threads."""

    def __init__(self, interval: float):
        self.interval, self.next, self.lock = interval, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


class ApiError(Exception):
    pass


class Blocked(Exception):
    """Cloudflare is serving a bot challenge: retrying only digs the hole deeper."""


class Wiki:
    def __init__(self, base: str, delay: float):
        self.base = base
        self.api = base + "/api.php"
        self.limiter = RateLimiter(delay)
        self.cdn_limiter = RateLimiter(min(delay, 0.1))
        self.local = threading.local()

    @property
    def session(self) -> requests.Session:
        if not hasattr(self.local, "s"):
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self.local.s = s
        return self.local.s

    def _retry_wait(self, resp, attempt):
        try:
            wait = float(resp.headers.get("Retry-After"))
        except (TypeError, ValueError):
            wait = 2 ** attempt * 5
        wait = max(5.0, min(300.0, wait))
        print(f"\n  HTTP {resp.status_code}: server says slow down, pausing {wait:.0f}s", file=sys.stderr)
        # one rate-limit means everyone should back off, not just this thread
        with self.limiter.lock:
            self.limiter.next = max(self.limiter.next, time.monotonic() + wait)
        return wait

    def get(self, params: dict, tries: int = 10) -> dict:
        params = {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
        last = None
        for attempt in range(tries):
            self.limiter.wait()
            try:
                r = self.session.get(self.api, params=params, timeout=90)
            except requests.RequestException as e:
                last = e
                time.sleep(min(120, 2 ** attempt * 2))
                continue
            if r.status_code in (403, 429) and "Just a moment" in r.text[:3000]:
                raise Blocked()
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(self._retry_wait(r, attempt))
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                if data["error"].get("code") == "maxlag":
                    last = "maxlag"
                    time.sleep(self._retry_wait(r, attempt))
                    continue
                raise ApiError(data["error"])
            return data
        raise RuntimeError(f"giving up after {tries} tries: {last}")

    def query_all(self, params: dict):
        cont: dict = {}
        while True:
            data = self.get({"action": "query", **params, **cont})
            yield data
            if "continue" not in data:
                return
            cont = data["continue"]

    cdn_blocked = False

    def download(self, url: str, dest: Path) -> bool:
        if Wiki.cdn_blocked:
            return False
        for attempt in range(3):
            self.cdn_limiter.wait()
            try:
                r = self.session.get(url, timeout=90, headers={
                    "Accept": "image/png,image/jpeg,image/gif,image/svg+xml,image/*;q=0.5"})
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 403:
                if not Wiki.cdn_blocked:
                    Wiki.cdn_blocked = True
                    print("\n  image CDN refuses scripted downloads (403) - hotlinking images "
                          "instead; they show when online", file=sys.stderr)
                return False
            if r.status_code == 404:
                return False
            if r.status_code >= 400:
                time.sleep(self._retry_wait(r, attempt))
                continue
            atomic_write(dest, r.content)
            return True
        return False


# ---------------------------------------------------------------- utilities
def atomic_write(path: Path, data: str | bytes):
    tmp = path.with_name(path.name + ".part")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


_BAD = re.compile(r'[\\*"<>|?#^\[\]\x00-\x1f]')


def safe_name(title: str, maxlen: int = 180) -> str:
    """Wiki title -> a filename that Obsidian links and every OS accept."""
    name = re.sub(r"\s*[:/]\s*", " - ", title)
    name = name.replace('"', "'")
    name = _BAD.sub("_", name).strip(" .")
    name = re.sub(r"\s+", " ", name) or "_"
    while len(name.encode()) > maxlen:
        name = name[:-1]
    return name


def yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid YAML


def fn_id(raw: str) -> str:
    raw = unquote(raw).replace("&#95;", "_")
    raw = re.sub(r"^cite_note-", "", raw)
    return re.sub(r"[^A-Za-z0-9-]+", "-", raw).strip("-") or "n"


def esc_pipes(s: str) -> str:
    return re.sub(r"(?<!\\)\|", r"\\|", s)


def tag_for(host: str) -> str:
    return "fandom/" + re.sub(r"[^a-z0-9-]+", "-", host.split(".")[0].lower())


# ------------------------------------------------------------------ the site
class Site:
    """Everything a worker needs to turn wiki titles into note names."""

    def __init__(self, wiki: Wiki, args):
        d = wiki.get({"action": "query", "meta": "siteinfo",
                      "siprop": "general|namespaces|namespacealiases"})["query"]
        g = d["general"]
        self.sitename = g["sitename"]
        self.mainpage = g["mainpage"]
        self.server = "https:" + g["server"] if g["server"].startswith("//") else g["server"]
        self.ns_name: dict[int, str] = {}
        self.ns_lookup: dict[str, int] = {}
        for ns in d["namespaces"].values():
            nid = int(ns["id"])
            self.ns_name[nid] = ns["name"]
            for n in (ns["name"], ns.get("canonical", "")):
                if n:
                    self.ns_lookup[n.lower().replace("_", " ")] = nid
        for a in d.get("namespacealiases", []):
            self.ns_lookup[a["alias"].lower().replace("_", " ")] = int(a["id"])
        self.host = urlparse(wiki.base).netloc
        self.tag = tag_for(self.host)
        self.files: dict[str, str] = {}      # normalised title -> filename (incl. redirects)
        self.images = args.images
        self.image_width = args.image_width

    def normalize(self, raw: str) -> tuple[int, str]:
        raw = re.sub(r"[\s_]+", " ", raw).strip()
        ns, rest = 0, raw
        if ":" in raw:
            pre, post = raw.split(":", 1)
            nid = self.ns_lookup.get(pre.strip().lower())
            if nid is not None:
                ns, rest = nid, post.strip()
        if rest:
            rest = rest[0].upper() + rest[1:]
        return ns, (f"{self.ns_name[ns]}:{rest}" if ns else rest)


# ------------------------------------------------------------- stat blocks
# Wikis like the FFG RPG one publish stat blocks as images whose alt text is
# "Label value Label value ...". Parsed into frontmatter so Bases can sort on it.
STAT_LABELS = {  # label in alt text -> property name (longest labels matched first)
    "Defense (Fore/Port/Starboard/Aft)": "defense_arcs", "Hull Trauma Threshold": "hull_trauma",
    "System Strain Threshold": "system_strain", "Hard Points Required": "hard_points_required",
    "Hard Points": "hard_points", "Critical Rating": "crit", "Soak Value": "soak",
    "Wound Threshold": "wound_threshold", "Strain Threshold": "strain_threshold",
    "Melee Defense": "melee_defense", "Ranged Defense": "ranged_defense", "Skill": "skill",
    "Range": "range", "Encumbrance": "encumbrance", "Price": "price", "Rarity": "rarity",
    "Damage": "damage", "Special": "special", "Silhouette": "silhouette", "Speed": "speed",
    "Handling": "handling", "Defense": "defense", "Armor": "armor", "Soak": "soak",
    "Top Speed": "top_speed", "Airbrakes": "airbrakes", "Turning": "turning",
    "Traction": "traction", "Repair": "repair", "Acceleration": "acceleration",
    "Boost": "boost", "Cooling": "cooling", "Brawn": "brawn", "Agility": "agility", "Intellect": "intellect", "Cunning": "cunning",
    "Willpower": "willpower", "Presence": "presence",
}
_STAT_RE = re.compile(r"(?<![A-Za-z(/])(" + "|".join(
    re.escape(k) for k in sorted(STAT_LABELS, key=len, reverse=True)) + r")(?![\w/])")
TEXT_STATS = {"skill", "range", "defense_arcs"}


def parse_stats(alt: str) -> dict | None:
    hits = list(_STAT_RE.finditer(alt))
    if len(hits) < 3 or hits[0].start() != 0:
        return None  # an ordinary picture description
    raw: dict[str, str] = {}
    for i, m in enumerate(hits):
        key = STAT_LABELS[m.group(1)]
        if key == "special":  # always last, and its values may contain label-like words
            raw[key] = alt[m.end():].strip()
            break
        end = hits[i + 1].start() if i + 1 < len(hits) else len(alt)
        raw.setdefault(key, alt[m.end():end].strip())
    out: dict = {}
    first = STAT_LABELS[hits[0].group(1)]
    out["stat_block"] = {"skill": "weapon", "silhouette": "vehicle", "brawn": "creature",
                         "hard_points_required": "attachment", "defense": "armor",
                         "encumbrance": "gear"}.get(first, first)
    for key, val in raw.items():
        if not val or val in ("-", "—", "–"):
            continue
        if key == "special":
            out[key] = [x.strip() for x in val.split(",") if x.strip() and x.strip() not in ("-", "—")]
            continue
        if key == "price":
            out["restricted"] = out.get("restricted", False) or "(R)" in val
        if "(R)" in val:  # sometimes printed beside the rarity instead
            out["restricted"] = True
            val = val.replace("(R)", "").strip()
        if key in TEXT_STATS:
            out[key] = val
            continue
        num = re.fullmatch(r"([+-]?)(\d[\d,]*)\D?", val)
        if not num:
            out[f"{key}_note"] = val       # e.g. "See Entry", "Not for Sale"
            continue
        n = int(num.group(2).replace(",", "")) * (-1 if num.group(1) == "-" else 1)
        if key == "damage" and num.group(1) == "+":
            out["damage_adds_brawn"] = True  # melee: Brawn + n
        out[key] = n
    return out


# Every property worked out from a note's body (rather than from the wiki API).
# They are recomputed from scratch each time, so --relayout can rebuild them.
DERIVED_KEYS = ({v for v in STAT_LABELS.values()} | {v + "_note" for v in STAT_LABELS.values()}
                | {"stat_block", "restricted", "damage_adds_brawn"})
STAT_PRIORITY = ["weapon", "vehicle", "creature", "armor", "attachment", "gear"]
_ALT_RE = re.compile(r"^> \[!quote\] Image text\n> (.+)$|!\[([^\]\n]+?)(?:\|\d+)?\]\(", re.M)


def body_stats(body: str) -> dict:
    """Merge every stat block on the page; the most specific kind names it."""
    merged: dict = {}
    kinds = []
    for m in _ALT_RE.finditer(body):
        raw_alt = m.group(1) or m.group(2)
        alt = raw_alt.strip()
        if m.group(2) and len(raw_alt) >= 80:
            continue  # older notes cut image alts at 80 chars; the Image text callout has it whole
        st = parse_stats(alt)
        if not st:
            continue
        kinds.append(st.pop("stat_block"))
        for k, v in st.items():
            merged.setdefault(k, v)
    if kinds:
        kinds.sort(key=lambda k: STAT_PRIORITY.index(k) if k in STAT_PRIORITY else 99)
        merged = {"stat_block": kinds[0], **merged}
    return merged


def stats_yaml(stats: dict) -> list[str]:
    fm = []
    for key, val in stats.items():
        if isinstance(val, list):
            fm.append(f"{key}:")
            fm += [f"  - {yaml_str(v)}" for v in val]
        elif isinstance(val, bool):
            fm.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, int):
            fm.append(f"{key}: {val}")
        else:
            fm.append(f"{key}: {yaml_str(val)}")
    return fm


# --------------------------------------------------------------- conversion
class Page:
    """Converts one page of parser HTML into Obsidian Markdown."""

    def __init__(self, site: Site, title: str):
        self.site, self.title = site, title
        self.tokens: dict[str, str] = {}
        self.wanted_images: dict[str, str] = {}   # filename -> url
        self.image_slots: dict[str, tuple[str, str, str]] = {}  # token -> (file, local md, remote md)
        self.conv = _Conv(self)

    def token(self, md: str) -> str:
        key = f"ZQTOK{len(self.tokens)}QZ"
        self.tokens[key] = md
        return key

    def md(self, html_or_el) -> str:
        return self.conv.convert(str(html_or_el))

    def inline(self, el) -> str:
        text = self.md(el.decode_contents() if hasattr(el, "decode_contents") else el)
        text = self.untoken(text)
        lines = [re.sub(r"^[-*+] ", "• ", ln.strip()) for ln in text.strip().splitlines() if ln.strip()]
        return "<br>".join(lines)

    def untoken(self, text: str) -> str:
        for _ in range(3):  # tokens may nest (footnote inside infobox value)
            if "ZQTOK" not in text:
                break
            text = re.sub(r"ZQTOK\d+QZ", lambda m: self.tokens.get(m.group(0), ""), text)
        return text

    # ---- links
    def link(self, el, text: str) -> str:
        href = el.get("href") or ""
        if el.find("img") is not None or not text.strip():
            return text
        if "new" in (el.get("class") or []) or href.startswith("#"):
            return text
        text = " ".join(text.split())
        path = None
        if href.startswith("/wiki/"):
            path = href[6:]
        else:
            for prefix in (f"https://{self.site.host}/wiki/", f"http://{self.site.host}/wiki/",
                           f"//{self.site.host}/wiki/"):
                if href.startswith(prefix):
                    path = href[len(prefix):]
        if path is None or "?" in path:
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = self.site.server + href
            if not href.startswith("http"):
                return text
            return f"[{text}]({href.replace(' ', '%20').replace(')', '%29')})"
        page, _, frag = path.partition("#")
        ns, title = self.site.normalize(unquote(page))
        if ns < 0 or ns == 6:          # Special:, Media:, File:
            return text
        target = self.site.files.get(title)
        if target is None:
            if ns != 0:
                return text
            target = safe_name(title)   # not archived (yet): leave an unresolved link
        if frag:
            heading = re.sub(r"[\s_]+", " ", unquote(frag)).strip()
            heading = re.sub(r'[\[\]|#^]', "", heading)
            dest = f"{target}#{heading}"
        else:
            dest = target
        text = text.replace("\\[", "(").replace("\\]", ")").replace("]]", "] ]").replace("|", "¦")
        if text == dest:
            return f"[[{dest}]]"
        return f"[[{dest}|{text}]]"

    # ---- images
    def image(self, img, width: int | None = None) -> str:
        if self.site.images == "none":
            return ""
        src = img.get("data-src") or img.get("src") or ""
        if "/images/" not in src or not src.startswith(("http", "//")):
            return ""
        if src.startswith("//"):
            src = "https:" + src
        try:
            w = int(img.get("width") or 0)
        except ValueError:
            w = 0
        if w and w < 40:     # era/canon badges and similar icons
            return ""
        base = src.split("?")[0]
        m = re.match(r"(.*?/images/[0-9a-f]/[0-9a-f]{2}/[^/]+)(/revision/[^/]+)?", base)
        if not m:
            return ""
        orig = m.group(1) + (m.group(2) or "/revision/latest")
        name = img.get("data-image-name") or unquote(m.group(1).rsplit("/", 1)[1]).replace("_", " ")
        lower = name.lower()
        if lower.endswith((".svg", ".gif")) or self.site.image_width <= 0:
            url = orig
        else:
            url = f"{orig}/scale-to-width-down/{self.site.image_width}"
        disp = width or (min(w, 400) if w else None)
        size = f"|{disp}" if disp else ""
        alt = " ".join((img.get("alt") or "").split()).replace("]", "").replace("|", " ")
        # Some wikis put real content in images (e.g. stat blocks); keep their alt text readable
        text = ""
        stem = name.rsplit(".", 1)[0].replace("_", " ").lower()
        if (len(alt) > 40 and alt.lower() != stem) or parse_stats(alt):
            text = f"\n\n> [!quote] Image text\n> {alt}\n\n"
        if self.site.images == "remote":
            return f"![{alt}{size}]({url}){text}"
        fname = safe_name(name)
        self.wanted_images[fname] = url
        key = f"ZQIMG{len(self.image_slots)}QZ"
        self.image_slots[key] = (fname, f"![[{ATTACH_DIR}/{fname}{size}]]", f"![{alt}{size}]({url})")
        return key + text

    # ---- infobox
    def infobox(self, aside) -> str:
        title_el = aside.select_one(".pi-title")
        title = " ".join(title_el.get_text(" ").split()) if title_el else ""
        imgs, rows = [], []
        for item in aside.select(".pi-item"):
            cls = item.get("class") or []
            if "pi-image" in cls or "pi-image-collection" in cls:
                for img in item.find_all("img"):
                    md = self.image(img, width=250)
                    if md:
                        imgs.append(md)
                cap = item.select_one(".pi-caption")
                if cap and imgs:
                    imgs.append(f"*{self.inline(cap)}*")
            elif "pi-header" in cls:
                rows.append(f"| **{esc_pipes(self.inline(item))}** | |")
            elif "pi-data" in cls:
                label = item.select_one(".pi-data-label")
                value = item.select_one(".pi-data-value")
                if value is None:
                    continue
                lab = esc_pipes(self.inline(label)) if label else ""
                rows.append(f"| {lab} | {esc_pipes(self.inline(value))} |")
            elif "pi-smart-group" in cls:
                labs = item.select(".pi-smart-data-label")
                vals = item.select(".pi-smart-data-value")
                for lab, val in zip(labs, vals):
                    rows.append(f"| {esc_pipes(self.inline(lab))} | {esc_pipes(self.inline(val))} |")
        out = [f"> [!infobox]{' ' + title if title else ''}"]
        out += [f"> {i}" for i in imgs]
        if rows:
            if imgs:
                out.append(">")
            out += ["> | | |", "> |---|---|"] + [f"> {r}" for r in rows]
        return "\n".join(out)

    # ---- whole page
    def convert(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one(".mw-parser-output") or soup
        for c in root.find_all(string=lambda s: isinstance(s, Comment)):
            c.extract()
        for sel in STRIP:
            for el in root.select(sel):
                el.decompose()

        def block(md: str):
            p = soup.new_tag("p")
            p.string = self.token(md)
            return p

        # Obsidian can't render footnotes inside headings: move them just below
        for h in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            refs = h.select("sup.reference")
            if refs:
                p = soup.new_tag("p")
                p.append(soup.new_string("Source: "))
                for r in refs:
                    p.append(r.extract())
                h.insert_after(p)

        # citations -> footnotes, defined where the reference list was
        for ol in root.select("ol.references"):
            defs = []
            for li in ol.find_all("li", recursive=False):
                if not li.get("id"):
                    continue
                body = li.select_one(".reference-text") or li
                defs.append(f"[^{fn_id(li['id'])}]: {self.inline(body)}")
            wrap = ol.find_parent(class_="mw-references-wrap") or ol
            wrap.replace_with(block("\n".join(defs)))
        for sup in root.select("sup.reference"):
            a = sup.find("a", href=True)
            if a and a["href"].startswith("#cite_note"):
                sup.replace_with(self.token(f"[^{fn_id(a['href'][1:])}]"))
            else:
                sup.decompose()

        for aside in root.select("aside.portable-infobox"):
            aside.replace_with(block(self.infobox(aside)))

        # nested tables don't exist in Markdown: flatten inner ones to text
        for inner in root.select("td table, th table"):
            if inner.parent is not None:
                inner.replace_with(" · ".join(inner.stripped_strings))

        # thumbnails/galleries: image and caption on their own lines
        for cap in root.select("figcaption, .thumbcaption, .lightbox-caption, .gallerytext"):
            cap.name = "p"

        text = self.untoken(self.md(root))
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


class _Conv(MarkdownConverter):
    def __init__(self, page: Page):
        self.page = page
        super().__init__(heading_style="ATX", bullets="-", escape_misc=False,
                         table_infer_header=True, newline_style="backslash")

    def escape(self, text, parent_tags):
        # literal [brackets] in wiki text would otherwise start Markdown links
        return super().escape(text, parent_tags).replace("[", r"\[").replace("]", r"\]")

    def convert_a(self, el, text, parent_tags):
        return self.page.link(el, text)

    def convert_img(self, el, text, parent_tags):
        md = self.page.image(el)
        if md and (el.find_parent(["figure", "li"]) or el.find_parent(class_=["thumb", "gallery"])):
            return f"\n\n{md}\n\n"
        return md

    # Obsidian has no definition lists; wikis mostly use them for quotes and indents
    def convert_dl(self, el, text, parent_tags):
        return f"\n\n{text.strip()}\n\n"

    def convert_dt(self, el, text, parent_tags):
        return f"\n**{text.strip()}**\n"

    def convert_dd(self, el, text, parent_tags):
        body = "\n".join(f"> {ln}".rstrip() for ln in text.strip().splitlines())
        return f"\n{body}\n>\n" if body else ""

    def convert_td(self, el, text, parent_tags):
        return super().convert_td(el, esc_pipes(text), parent_tags)

    def convert_th(self, el, text, parent_tags):
        return super().convert_th(el, esc_pipes(text), parent_tags)


# ------------------------------------------------------------------ profile
# Optional per-wiki rules, kept as <out>/fandom2obsidian.profile.json:
#   folders:  ordered rules, first match wins: {"if": {...}, "folder": "A/B"}
#             conditions: namespace, title, stat_block, category (str or list = any of)
#   default_folder: where notes matching no rule go ("" = top level)
#   flags:    {"property": "Category name"} -> property: true/false
#   fields:   {"Label": "property"} lifts "- **Label:** value" lines into properties
# It works on finished notes, so --relayout can re-apply it without downloading.
def load_profile(out: Path, src: Path | None) -> dict:
    target = out / PROFILE_FILE
    if src:
        src = src.expanduser()
        pdir = src if src.is_dir() else src.parent
        pfile = src / "profile.json" if src.is_dir() else src
        shutil.copyfile(pfile, target)
        bases = pdir / "bases"
        if bases.is_dir():  # starter Bases: copied once, never overwritten (you may edit them)
            (out / "Bases").mkdir(exist_ok=True)
            for b in bases.glob("*.base"):
                if not (out / "Bases" / b.name).exists():
                    shutil.copyfile(b, out / "Bases" / b.name)
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))
    return {}


def note_path(out: Path, folder: str, filename: str) -> Path:
    return (out / folder / f"{filename}.md") if folder else (out / f"{filename}.md")


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _split_top(text: str) -> list[str]:
    """Split on commas that aren't inside (...) or [[...]]."""
    parts, depth, cur, i = [], 0, "", 0
    while i < len(text):
        two = text[i:i + 2]
        if two in ("[[", "]]"):
            depth += 1 if two == "[[" else -1
            cur += two
            i += 2
            continue
        ch = text[i]
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth <= 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    return [x.strip() for x in parts + [cur] if x.strip()]


def _plain(text: str) -> str:
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\^[^\]]*\]", "", text)
    return text.replace("*", "").replace("\\[", "[").replace("\\]", "]").strip()


def _field_value(raw: str, kind: str = ""):
    raw = re.sub(r"\s*\\$", "", raw.strip()).strip().rstrip(".").strip()
    if kind in ("list", "names"):  # "Charm 1, Knowledge (Education) 3" / "Adversary 1 (explanation)"
        if raw.lower().rstrip(".") in ("none", "-", "—", "n/a", ""):
            return None
        items = [_plain(x) for x in _split_top(raw)]
        if kind == "names":
            items = [re.sub(r"\s*\(.*$", "", x).split(";")[0].strip() for x in items]
        items = [x.rstrip(".").strip() for x in items if x.strip()]
        return [x[0].upper() + x[1:] for x in items if x] or None
    if kind == "int":
        m = re.search(r"-?\d[\d,]*", raw)
        return int(m.group(0).replace(",", "")) if m else None
    if kind == "price_rarity":  # "150,000 credits (R)/6", hand-typed so often messier
        clean = re.sub(r"\[\^[^\]]*\]|\*", "", raw).strip()
        out: dict = {"restricted": "(R)" in clean}
        clean = clean.replace("(R)", " ")
        price_part, _, rarity_part = clean.rpartition("/") if "/" in clean else (clean, "", "")
        m = re.search(r"\d[\d,. ]*\d|\d", price_part)
        if m:
            out["price"] = int(re.sub(r"[^\d]", "", m.group(0)))
            rest = (price_part[:m.start()] + price_part[m.end():]).lower()
            if re.search(r"\b(or|x|per|silh|each|pair|with|not|special|times)\b|[–-]\s*\d", rest):
                out["price_note"] = re.sub(r"\s*/\s*[^/]*$", "", clean).strip()  # qualified price
        elif price_part.strip():
            out["price_note"] = price_part.strip()
        r = re.search(r"\d+", rarity_part)
        if r:
            out["rarity"] = int(r.group(0))
        return out
    if kind == "text":
        return re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", raw).replace("*", "")
    links = re.findall(r"\[\[[^\]]+\]\]", raw)
    if links:
        return links
    if raw.lower() in ("yes", "no"):
        return raw.lower() == "yes"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return re.sub(r"[*_]", "", raw)


def apply_profile(profile: dict, text: str, ns: int) -> tuple[str, str]:
    """Return (note with derived properties, folder) for one finished note."""
    profile = profile or {}
    if not text.startswith("---\n") or "\n---\n" not in text:
        return text, profile.get("default_folder", "")
    head, body = text[4:].split("\n---\n", 1)
    lines = head.split("\n")
    fields = [(label, *(spec.split(":", 1) + [""])[:2])
              for label, specs in profile.get("fields", {}).items() for spec in _as_list(specs)]
    managed = (DERIVED_KEYS | set(profile.get("flags", {})) | set(profile.get("patterns", {}))
               | {p for _, p, _ in fields})
    managed |= {"price", "rarity", "restricted", "price_note"}
    kept, skipping = [], False
    for ln in lines:  # drop properties this profile owns, so re-applying is idempotent
        if skipping and ln.startswith("  - "):
            continue
        key = ln.split(":", 1)[0] if not ln.startswith(" ") else None
        skipping = key in managed
        if not skipping:
            kept.append(ln)
    head = "\n".join(kept)
    cats = set(re.findall(r'"\[\[(Category - [^\]|]+)\]\]"', head))
    m = re.search(r'^title: (".*")$', head, re.M)
    title = json.loads(m.group(1)) if m else ""

    extra: dict = body_stats(body)
    for label, prop, kind in fields:
        fm = re.search(rf"^\s*(?:[-*] )?\*\*{re.escape(label)}:\*\*\s*(.+)$", body, re.M)
        if not fm:
            continue
        if kind == "labels_after":  # names of the "- **Name:** ..." lines that follow it
            names = []
            for ln in body[fm.end():].lstrip("\n").split("\n"):
                nm = re.match(r"^\s*[-*] \*\*([^*]+?):\*\*", ln)
                if not nm:
                    break
                names.append(nm.group(1).strip())
            if names:
                extra.setdefault(prop, names)
            continue
        val = _field_value(fm.group(1), kind)
        if isinstance(val, dict):
            for k, v in val.items():
                extra.setdefault(k, v)
        elif val is not None and val != "":
            extra.setdefault(prop, val)
    for prop, pattern in profile.get("patterns", {}).items():  # first regex group, Title Cased
        pm = re.search(pattern, body, re.M)
        if pm:
            extra.setdefault(prop, pm.group(1).strip().title())
    for prop, cat in profile.get("flags", {}).items():
        extra[prop] = safe_name("Category:" + cat) in cats
    stat_block = extra.get("stat_block")
    if extra:
        new = stats_yaml(extra)
        i = head.rfind("\ntags:")
        head = head[:i] + "\n" + "\n".join(new) + head[i:] if i >= 0 else head + "\n" + "\n".join(new)

    folder = profile.get("default_folder", "")
    for rule in profile.get("folders", []):
        cond = rule.get("if", {})
        ok = True
        if "namespace" in cond and ns not in _as_list(cond["namespace"]):
            ok = False
        if ok and "title" in cond and title not in _as_list(cond["title"]):
            ok = False
        if ok and "stat_block" in cond and stat_block not in _as_list(cond["stat_block"]):
            ok = False
        if ok and "category" in cond and not any(
                safe_name("Category:" + c) in cats for c in _as_list(cond["category"])):
            ok = False
        if ok:
            folder = rule["folder"]
            break
    return "---\n" + head + "\n---\n" + body, folder.strip("/")


def split_members(text: str) -> tuple[str, str]:
    if MEMBERS_MARK in text:
        i = text.index(MEMBERS_MARK)
        return text[:i].rstrip("\n") + "\n", text[i:]
    return text, ""


def place_note(out: Path, old: Path | None, new: Path, text: str):
    new.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(new, text)
    if old and old != new and old.exists():
        old.unlink()
        try:  # tidy a folder a rule change left empty
            old.parent.rmdir() if old.parent != out else None
        except OSError:
            pass


# -------------------------------------------------------------------- state
def open_db(out: Path) -> sqlite3.Connection:
    db = sqlite3.connect(out / STATE_FILE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pages(
            pageid INTEGER PRIMARY KEY, title TEXT, ns INTEGER, revid INTEGER,
            done_revid INTEGER DEFAULT 0, filename TEXT, redirects TEXT DEFAULT '[]',
            categories TEXT DEFAULT '[]', seen INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS images(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    if "folder" not in {r[1] for r in db.execute("PRAGMA table_info(pages)")}:
        db.execute("ALTER TABLE pages ADD COLUMN folder TEXT DEFAULT ''")
    return db


def enumerate_pages(wiki: Wiki, namespaces, categories) -> dict:
    pages: dict[int, dict] = {}

    def absorb(data):
        for p in data.get("query", {}).get("pages", []):
            if p.get("missing") or p.get("invalid") or "pageid" not in p:
                continue
            e = pages.setdefault(p["pageid"], {"title": p["title"], "ns": p["ns"],
                                               "revid": 0, "redirects": set()})
            if p.get("lastrevid"):
                e["revid"] = p["lastrevid"]
            for r in p.get("redirects", []):
                e["redirects"].add(r["title"])
        print(f"\r  listed {len(pages):,} pages", end="", file=sys.stderr, flush=True)

    common = {"prop": "info|redirects", "rdlimit": "max", "rdprop": "title"}
    if categories:
        for cat in categories:
            if not cat.lower().startswith("category:"):
                cat = "Category:" + cat
            for d in wiki.query_all({"generator": "categorymembers", "gcmtitle": cat,
                                     "gcmlimit": "max", "gcmtype": "page|subcat", **common}):
                absorb(d)
    else:
        for ns in namespaces:
            for d in wiki.query_all({"generator": "allpages", "gapnamespace": ns,
                                     "gaplimit": "max", "gapfilterredir": "nonredirects", **common}):
                absorb(d)
    print(file=sys.stderr)
    return pages


def sync_index(db, out: Path, listed: dict, full: bool):
    rows = {r[0]: r for r in db.execute("SELECT pageid, title, filename, folder FROM pages")}
    taken = {r[2].lower() for r in rows.values() if r[2]}
    if full:
        db.execute("UPDATE pages SET seen = 0")
    for pid, e in sorted(listed.items(), key=lambda kv: kv[1]["title"]):
        redirects = json.dumps(sorted(e["redirects"]), ensure_ascii=False)
        old = rows.get(pid)
        if old and old[1] == e["title"]:
            db.execute("UPDATE pages SET revid=?, redirects=?, seen=1 WHERE pageid=?",
                       (e["revid"], redirects, pid))
            continue
        if old:  # page was renamed on the wiki: move to the new name
            taken.discard(old[2].lower())
            note_path(out, old[3] or "", old[2]).unlink(missing_ok=True)
        base = safe_name(e["title"])
        name, n = base, 2
        while name.lower() in taken:
            name, n = f"{base} ({n})", n + 1
        taken.add(name.lower())
        db.execute("""INSERT INTO pages(pageid,title,ns,revid,filename,redirects,done_revid,seen)
                      VALUES(?,?,?,?,?,?,0,1)
                      ON CONFLICT(pageid) DO UPDATE SET title=excluded.title, revid=excluded.revid,
                      filename=excluded.filename, redirects=excluded.redirects, done_revid=0, seen=1""",
                   (pid, e["title"], e["ns"], e["revid"], name, redirects))
    db.commit()


# ------------------------------------------------------------------ workers
_image_lock = threading.Lock()
_images_claimed: set[str] = set()


def fetch_one(wiki: Wiki, site: Site, out: Path, profile: dict, row) -> dict:
    pageid, title, filename, redirects, ns, old_folder = row
    try:
        data = wiki.get({"action": "parse", "pageid": pageid, "redirects": 1,
                         "prop": "text|categories|revid", "disableeditsection": 1,
                         "disabletoc": 1})["parse"]
    except ApiError as e:
        return {"pageid": pageid, "error": str(e)}
    page = Page(site, title)
    body = page.convert(data["text"])
    cats = sorted({site.normalize("Category:" + c["category"])[1]
                   for c in data.get("categories", []) if not c.get("hidden")})

    aliases = [t for t in [title, *json.loads(redirects)] if t != filename]
    fm = ["---", f"title: {yaml_str(title)}"]
    if aliases:
        fm.append("aliases:")
        fm += [f"  - {yaml_str(a)}" for a in aliases]
    fm.append(f"source: {yaml_str(site.server + '/wiki/' + title.replace(' ', '_'))}")
    fm.append(f"revision: {data.get('revid', 0)}")
    fm.append(f"archived: {dt.date.today().isoformat()}")
    if cats:
        fm.append("categories:")
        fm += [f"  - {yaml_str('[[' + site.files.get(c, safe_name(c)) + ']]')}" for c in cats]
    fm += ["tags:", f"  - {site.tag}", "---", ""]
    note = "\n".join(fm) + body

    got = []
    for fname, url in page.wanted_images.items():
        dest = out / ATTACH_DIR / fname
        with _image_lock:
            claimed = fname in _images_claimed
            _images_claimed.add(fname)
        # if another thread is mid-download we hotlink this once rather than wait
        if dest.exists() or (not claimed and wiki.download(url, dest)):
            got.append(fname)
    ok = set(got)
    for key, (fname, local_md, remote_md) in page.image_slots.items():
        note = note.replace(key, local_md if fname in ok else remote_md)

    note, folder = apply_profile(profile, note, ns)
    old_path = note_path(out, old_folder or "", filename)
    if old_path.exists():  # keep generated category listings across re-fetches
        _, members = split_members(old_path.read_text(encoding="utf-8"))
        if members:
            note = note.rstrip("\n") + "\n\n" + members
    place_note(out, old_path, note_path(out, folder, filename), note)
    return {"pageid": pageid, "revid": data.get("revid", 0), "categories": cats, "images": got,
            "folder": folder}


def write_category_notes(db, files: dict, tag: str, out: Path, profile: dict, cap: int):
    members: dict[str, list[tuple[str, str]]] = {}
    for title, filename, cats in db.execute("SELECT title, filename, categories FROM pages"):
        for c in json.loads(cats or "[]"):
            members.setdefault(c, []).append((title, filename))
    archived = {t: (f, d or "") for t, f, d in
                db.execute("SELECT title, filename, folder FROM pages WHERE ns = 14")}
    written = 0
    for cat, mem in members.items():
        mem.sort(key=lambda m: m[0].lower())
        lines = [MEMBERS_MARK, "", f"## Pages in this category ({len(mem):,})", ""]
        lines += [f"- [[{f}]]" if f == t else f"- [[{f}|{t}]]" for t, f in mem[:cap]]
        if len(mem) > cap:
            lines += ["", f"*…and {len(mem) - cap:,} more — see this note's backlinks.*"]
        listing = "\n".join(lines) + "\n"
        stub = "\n".join(["---", f"title: {yaml_str(cat)}", "tags:", f"  - {tag}",
                          f"  - {tag}/category", "---", "", f"# {cat.split(':', 1)[1]}", ""])
        if cat in archived:
            fname, folder = archived[cat]
            path = note_path(out, folder, fname)
            old_path = path
        else:  # no category page on the wiki: a stub note, placed by the profile
            fname = files.get(cat) or safe_name(cat)
            stub, folder = apply_profile(profile, stub, 14)
            path = note_path(out, folder, fname)
            old_path = next((p for p in [path, out / f"{fname}.md", *out.glob(f"**/{glob_escape(fname)}.md")]
                             if p.exists()), None)
        if old_path and old_path.exists():
            head, _ = split_members(old_path.read_text(encoding="utf-8"))
            head = head.rstrip("\n")
        else:
            head = stub.rstrip("\n")
        new = head + "\n\n" + listing
        if old_path != path or not path.exists() or path.read_text(encoding="utf-8") != new:
            place_note(out, old_path, path, new)
            written += 1
    return written


def glob_escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


def relayout(db, out: Path, profile: dict) -> int:
    """Re-apply the profile to notes already on disk: no network needed."""
    moved = 0
    rows = db.execute("SELECT pageid, ns, filename, folder FROM pages WHERE done_revid != 0").fetchall()
    for pageid, ns, filename, folder in rows:
        old = note_path(out, folder or "", filename)
        if not old.exists():
            old = out / f"{filename}.md"
            if not old.exists():
                continue
        text, members = split_members(old.read_text(encoding="utf-8"))
        text, new_folder = apply_profile(profile, text, ns)
        if members:
            text = text.rstrip("\n") + "\n\n" + members
        new = note_path(out, new_folder, filename)
        if new != old or old.read_text(encoding="utf-8") != text:
            place_note(out, old, new, text)
            moved += new != old
        db.execute("UPDATE pages SET folder=? WHERE pageid=?", (new_folder, pageid))
    db.commit()
    return moved


# --------------------------------------------------------------------- main
def wiki_base(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    # Fandom language wikis live at host/<lang>/wiki/...
    prefix = u.path.split("/wiki/")[0].rstrip("/") if "/wiki/" in u.path or u.path.endswith("/wiki") \
        else u.path.rstrip("/")
    prefix = re.sub(r"/wiki$", "", prefix)
    return f"{u.scheme}://{u.netloc}{prefix}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="any URL on the wiki, e.g. https://starwars.fandom.com/wiki/")
    ap.add_argument("--out", type=Path, help="output folder (default: ./<wiki name>)")
    ap.add_argument("--namespaces", default="0,14",
                    help="namespace ids to archive: 0=articles, 14=categories (default 0,14)")
    ap.add_argument("--category", action="append",
                    help="only archive members of this category (repeatable, not recursive)")
    ap.add_argument("--images", choices=["local", "remote", "none"], default="local",
                    help="download images (local), hotlink them (remote) or drop them (default local)")
    ap.add_argument("--image-width", type=int, default=800,
                    help="downscale downloaded images to this width; 0 = originals (default 800)")
    ap.add_argument("--workers", type=int, default=2, help="parallel page fetches (default 2)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="minimum seconds between API requests overall (default 0.5)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N pages (for testing)")
    ap.add_argument("--force", action="store_true", help="re-fetch every page, even unchanged ones")
    ap.add_argument("--category-list-max", type=int, default=1000,
                    help="cap on members listed in each category note (default 1000)")
    ap.add_argument("--profile", type=Path,
                    help="folder rules etc. for this wiki: a profile dir (profile.json + bases/) or "
                         "json file. Saved into the output folder, so later runs reuse it")
    ap.add_argument("--relayout", action="store_true",
                    help="only re-apply the profile to notes already downloaded (offline), then exit")
    args = ap.parse_args()

    if args.relayout:
        if not args.out:
            sys.exit("--relayout needs --out")
        out = args.out.expanduser().resolve()
        if not (out / STATE_FILE).exists():
            sys.exit(f"no archive in {out}")
        db = open_db(out)
        profile = load_profile(out, args.profile)
        moved = relayout(db, out, profile)
        files = {}
        n = write_category_notes(db, files, tag_for(urlparse(wiki_base(args.url)).netloc), out,
                                 profile, args.category_list_max)
        print(f"re-applied profile: {moved:,} notes moved, {n:,} category notes updated", file=sys.stderr)
        return

    wiki = Wiki(wiki_base(args.url), args.delay)
    try:
        site = Site(wiki, args)
    except Blocked:
        sys.exit("Fandom's Cloudflare is challenging this script as a bot. "
                 "Wait (an hour or more) and re-run.")
    out = (args.out or Path(safe_name(site.sitename))).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.images == "local":
        (out / ATTACH_DIR).mkdir(exist_ok=True)
    db = open_db(out)
    profile = load_profile(out, args.profile)
    print(f"{site.sitename} ({wiki.base}) -> {out}", file=sys.stderr)

    namespaces = [int(n) for n in args.namespaces.split(",") if n.strip()]
    listed = enumerate_pages(wiki, namespaces, args.category)
    sync_index(db, out, listed, full=not args.category)

    for title, filename, redirects in db.execute("SELECT title, filename, redirects FROM pages"):
        site.files[site.normalize(title)[1]] = filename
        for r in json.loads(redirects):
            site.files.setdefault(site.normalize(r)[1], filename)
    _images_claimed.update(r[0] for r in db.execute("SELECT name FROM images")
                           if (out / ATTACH_DIR / r[0]).exists())

    where = "seen = 1" + ("" if args.force else " AND done_revid != revid")
    todo = db.execute(f"SELECT pageid, title, filename, redirects, ns, folder FROM pages WHERE {where} "
                      "ORDER BY ns, title").fetchall()
    if args.limit:
        todo = todo[:args.limit]
    total = len(todo)
    print(f"{len(listed):,} pages listed, {total:,} to fetch", file=sys.stderr)

    done = errors = 0
    start = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futures = [pool.submit(fetch_one, wiki, site, out, profile, row) for row in todo]
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Blocked:
                raise
            except Exception as e:  # network gave up etc.: leave it for the next run
                errors += 1
                print(f"\n  error: {e}", file=sys.stderr)
                continue
            if "error" in res:
                errors += 1
                print(f"\n  page {res['pageid']}: {res['error']}", file=sys.stderr)
            else:
                db.execute("UPDATE pages SET done_revid=?, categories=?, folder=? WHERE pageid=?",
                           (res["revid"] or -1, json.dumps(res["categories"], ensure_ascii=False),
                            res["folder"], res["pageid"]))
                db.executemany("INSERT OR IGNORE INTO images(name) VALUES(?)",
                               [(i,) for i in res["images"]])
            done += 1
            if done % 20 == 0 or done == total:
                db.commit()
                rate = done / max(1e-6, time.monotonic() - start)
                eta = dt.timedelta(seconds=int((total - done) / rate)) if rate else "?"
                print(f"\r  {done:,}/{total:,}  {rate:.1f} pages/s  eta {eta}  errors {errors}   ",
                      end="", file=sys.stderr, flush=True)
    except (KeyboardInterrupt, Blocked) as e:
        if isinstance(e, Blocked):
            print("\nFandom's Cloudflare is now challenging this script as a bot. Stopping; "
                  "progress is saved. Wait (an hour or more) and re-run the same command.",
                  file=sys.stderr)
        else:
            print("\ninterrupted - progress saved, re-run the same command to resume", file=sys.stderr)
        pool.shutdown(wait=True, cancel_futures=True)
        db.commit()
        sys.exit(75 if isinstance(e, Blocked) else 130)
    pool.shutdown()
    db.commit()
    print(file=sys.stderr)

    if 14 in namespaces or not args.category:
        n = write_category_notes(db, site.files, site.tag, out, profile, args.category_list_max)
        print(f"category notes updated: {n:,}", file=sys.stderr)
    if args.profile and not args.force:
        moved = relayout(db, out, profile)  # a new profile also applies to unchanged notes
        if moved:
            print(f"re-applied profile: {moved:,} notes moved", file=sys.stderr)
    main_file = site.files.get(site.normalize(site.mainpage)[1])
    if main_file:
        print(f"start at: {main_file}.md", file=sys.stderr)
    if errors:
        print(f"{errors} pages failed - re-run to retry them", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Blocked:
        sys.exit("\nFandom's Cloudflare is challenging this script as a bot. "
                 "Wait (an hour or more) and re-run the same command.")
