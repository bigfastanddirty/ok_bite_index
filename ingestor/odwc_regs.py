import os
import re
import time
import requests
import psycopg2
import html as html_lib
from html.parser import HTMLParser

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

OVERRIDES = {
    "HEYB": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/heyburn",
    "HULA": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/hulah",
    "KAWW": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/kaw",
    "KEYW": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/keystone",
    "TEXO": "https://www.wildlifedepartment.com/fishing/wheretofish/southeast/texoma",
    "TBIR": "https://www.wildlifedepartment.com/fishing/wheretofish/central/thunderbird",
    "OOLO": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/oologah",
    "PINE": "https://www.wildlifedepartment.com/fishing/wheretofish/southeast/pine-creek",
    "KERR": "https://www.wildlifedepartment.com/fishing/wheretofish/southeast/robert-s-kerr",
    "SKIA": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/skiatook",
    "TENK": "https://www.wildlifedepartment.com/fishing/wheretofish/northeast/tenkiller",
    "WAUR": "https://www.wildlifedepartment.com/fishing/wheretofish/southwest/waurika",
    "WIST": "https://www.wildlifedepartment.com/fishing/wheretofish/southeast/wister"
}

def normalize_name(text):
    text = re.sub(r"\b(lake|reservoir|res|ferry|o' the cherokees|the)\b", "", text, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", text.lower())

class DirectoryLinkParser(HTMLParser):
    def __init__(self, region, base_url):
        super().__init__()
        self.region = region
        self.base_url = base_url
        self.catalog = {}
        self.in_a = False
        self.current_href = None
        self.current_text = []
        self.has_next_page = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a":
            href = attrs_dict.get("href", "")
            if f"/fishing/wheretofish/{self.region}/" in href:
                self.in_a = True
                self.current_href = href if href.startswith("http") else self.base_url + href
                self.current_text = []
            rel = attrs_dict.get("rel", "")
            if rel == "next":
                self.has_next_page = True

    def handle_data(self, data):
        if self.in_a:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_a:
            clean_text = " ".join("".join(self.current_text).split()).strip().lower()
            slug = self.current_href.rstrip("/").split("/")[-1].lower()
            if slug and slug != self.region:
                self.catalog[slug] = self.current_href
            if clean_text:
                self.catalog[clean_text] = self.current_href
            self.in_a = False
            self.current_href = None
            self.current_text = []

def extract_regs_from_html(html_text):
    html_text = html_lib.unescape(html_text)

    pattern = re.compile(
        r"Area Specific Fishing Regulations.*?(?=LOCATION/DESCRIPTION|Driving Directions|CONTACT|FACILITIES|Fish Species|Recreational|block-layout-builder|<div class=\"block|\Z)",
        re.I | re.S
    )
    match = pattern.search(html_text)
    if match:
        raw_section = match.group(0)
        clean_text = re.sub(r"<[^>]+>", " ", raw_section)
        clean_text = clean_text.replace("\xa0", " ")
        lines = [" ".join(line.split()) for line in clean_text.splitlines() if line.strip()]

        filtered = []
        for line in lines:
            if re.search(r"^(Area Specific Fishing Regulations|Driving Directions|Recreational)", line, re.I):
                continue
            filtered.append(line)

        result = "\n".join(filtered).strip()
        if result and len(result) > 15:
            return result

    return "Statewide general limits apply (no special area restrictions listed)."

def crawl_odwc_directory():
    base_url = "https://www.wildlifedepartment.com"
    regions = ["central", "northeast", "northwest", "southeast", "southwest"]
    catalog = {}

    for region in regions:
        page = 0
        while True:
            url = f"{base_url}/fishing/wheretofish/{region}?page={page}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=12)
                if r.status_code != 200:
                    break
                parser = DirectoryLinkParser(region, base_url)
                parser.feed(r.text)
                if not parser.catalog:
                    break
                catalog.update(parser.catalog)
                if not parser.has_next_page:
                    break
                page += 1
            except Exception:
                break
    return catalog

def sync_odwc_regs(conn):
    print("[ODWC Sync] Initiating lake regulations scrape...", flush=True)
    cur = conn.cursor()
    cur.execute("SELECT lake_code, name FROM lakes ORDER BY name;")
    lakes = cur.fetchall()

    catalog = crawl_odwc_directory()

    for row in lakes:
        code = row[0] if isinstance(row, (tuple, list)) else row["lake_code"]
        name = row[1] if isinstance(row, (tuple, list)) else row["name"]

        target_url = OVERRIDES.get(code)
        if not target_url:
            norm_target = normalize_name(name)
            for k, u in catalog.items():
                if norm_target and (norm_target == normalize_name(k) or norm_target in normalize_name(k)):
                    target_url = u
                    break

        if not target_url:
            continue

        try:
            r = requests.get(target_url, headers=HEADERS, timeout=12)
            if r.status_code == 200:
                regs_text = extract_regs_from_html(r.text)
                cur.execute(
                    "UPDATE lakes SET special_regulations = %s WHERE lake_code = %s;",
                    (regs_text, code)
                )
        except Exception as e:
            print(f"[ODWC Sync] Error updating {code}: {e}", flush=True)

    conn.commit()
    cur.close()
    print("[ODWC Sync] Finished updating lake regulations in database.", flush=True)
