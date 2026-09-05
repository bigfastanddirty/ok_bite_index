import argparse
import html as html_lib
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

import psycopg2
import requests
from psycopg2.extras import Json, RealDictCursor


DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

BASE_URL = "https://www.wildlifedepartment.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Lake-code overrides copied from the existing ODWC regulations scraper.
# Add entries here if a database lake name does not resolve cleanly from the
# ODWC Where to Fish directory.
OVERRIDES = {
    "HEYB": f"{BASE_URL}/fishing/wheretofish/northeast/heyburn",
    "HULA": f"{BASE_URL}/fishing/wheretofish/northeast/hulah",
    "KAWW": f"{BASE_URL}/fishing/wheretofish/northeast/kaw",
    "KEYW": f"{BASE_URL}/fishing/wheretofish/northeast/keystone",
    "TEXO": f"{BASE_URL}/fishing/wheretofish/southeast/texoma",
    "TBIR": f"{BASE_URL}/fishing/wheretofish/central/thunderbird",
    "OOLO": f"{BASE_URL}/fishing/wheretofish/northeast/oologah",
    "PINE": f"{BASE_URL}/fishing/wheretofish/southeast/pine-creek",
    "KERR": f"{BASE_URL}/fishing/wheretofish/southeast/robert-s-kerr",
    "SKIA": f"{BASE_URL}/fishing/wheretofish/northeast/skiatook",
    "TENK": f"{BASE_URL}/fishing/wheretofish/northeast/tenkiller",
    "WAUR": f"{BASE_URL}/fishing/wheretofish/southwest/waurika",
    "WIST": f"{BASE_URL}/fishing/wheretofish/southeast/wister",
}


def normalize_name(text):
    text = text or ""
    text = re.sub(
        r"\b(lake|reservoir|res|ferry|o' the cherokees|the)\b",
        "",
        text,
        flags=re.I,
    )
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
                self.current_href = urljoin(self.base_url, href)
                self.current_text = []

            rel = attrs_dict.get("rel", "")
            if isinstance(rel, str) and "next" in rel.split():
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


class SpeciesSectionParser(HTMLParser):
    """
    Collect anchor text contained in the ODWC "Fish Species of Interest"
    section. Collection begins when that heading closes and stops when the
    next h1/h2/h3 heading starts.

    ODWC currently renders each species as a link, which makes this safer
    than scraping every text node in the section.
    """

    def __init__(self):
        super().__init__()
        self.in_heading = False
        self.heading_tag = None
        self.heading_text = []

        self.in_species_section = False

        self.in_a = False
        self.anchor_text = []

        self.species = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in ("h1", "h2", "h3"):
            # Once the desired section has started, a new major heading ends it.
            if self.in_species_section:
                self.in_species_section = False

            self.in_heading = True
            self.heading_tag = tag
            self.heading_text = []
            return

        if self.in_species_section and tag == "a":
            self.in_a = True
            self.anchor_text = []

    def handle_data(self, data):
        if self.in_heading:
            self.heading_text.append(data)

        if self.in_species_section and self.in_a:
            self.anchor_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if self.in_heading and tag == self.heading_tag:
            heading = " ".join("".join(self.heading_text).split()).strip()

            if re.search(r"\bFish\s+Species\s+of\s+Interest\b", heading, re.I):
                self.in_species_section = True

            self.in_heading = False
            self.heading_tag = None
            self.heading_text = []
            return

        if self.in_species_section and tag == "a" and self.in_a:
            text = html_lib.unescape(
                " ".join("".join(self.anchor_text).split()).strip()
            )

            if text and text not in self.species:
                self.species.append(text)

            self.in_a = False
            self.anchor_text = []


def crawl_odwc_directory(session):
    regions = ["central", "northeast", "northwest", "southeast", "southwest"]
    catalog = {}

    for region in regions:
        page = 0

        while True:
            url = f"{BASE_URL}/fishing/wheretofish/{region}?page={page}"

            try:
                response = session.get(url, timeout=12)

                if response.status_code != 200:
                    print(
                        f"[Directory] {region} page {page}: HTTP "
                        f"{response.status_code}",
                        flush=True,
                    )
                    break

                parser = DirectoryLinkParser(region, BASE_URL)
                parser.feed(response.text)

                if not parser.catalog:
                    break

                catalog.update(parser.catalog)

                if not parser.has_next_page:
                    break

                page += 1

            except requests.RequestException as exc:
                print(
                    f"[Directory] Failed {region} page {page}: {exc}",
                    flush=True,
                )
                break

    return catalog


def resolve_odwc_url(lake_code, lake_name, catalog):
    if lake_code in OVERRIDES:
        return OVERRIDES[lake_code]

    target = normalize_name(lake_name)
    if not target:
        return None

    # Prefer exact normalized matches.
    for key, url in catalog.items():
        if target == normalize_name(key):
            return url

    # Fall back to containment, matching the behavior of the existing
    # regulations scraper.
    for key, url in catalog.items():
        candidate = normalize_name(key)
        if target and candidate and (target in candidate or candidate in target):
            return url

    return None


def extract_species_from_html(html_text):
    parser = SpeciesSectionParser()
    parser.feed(html_text)

    cleaned = []

    for item in parser.species:
        species = re.sub(r"\s+", " ", item).strip(" \t\r\n-–—|")

        if not species:
            continue

        # Guard against accidental navigation/UI links if ODWC changes markup.
        if len(species) > 80:
            continue

        if species.lower() in {
            "learn more",
            "submit your catch",
            "upload your catch",
        }:
            continue

        if species not in cleaned:
            cleaned.append(species)

    return cleaned


def get_target_species_column_type(conn):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'lakes'
          AND column_name = 'target_species'
        LIMIT 1;
        """
    )
    row = cur.fetchone()
    cur.close()

    if not row:
        raise RuntimeError(
            "Column lakes.target_species does not exist in the current "
            "PostgreSQL schema."
        )

    return row["data_type"], row["udt_name"]


def serialize_species_for_db(species, data_type, udt_name):
    """
    Support the likely representations without requiring a schema change:
      json/jsonb -> JSON array
      text[]     -> PostgreSQL text array
      text       -> comma-separated string
    """
    if udt_name in ("json", "jsonb") or data_type in ("json", "jsonb"):
        return Json(species)

    if udt_name == "_text" or data_type == "ARRAY":
        return species

    if data_type in (
        "text",
        "character varying",
        "character",
    ):
        return ", ".join(species)

    raise RuntimeError(
        "Unsupported lakes.target_species column type: "
        f"data_type={data_type!r}, udt_name={udt_name!r}"
    )


def sync_species(dry_run=False, only_lake=None):
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        connect_timeout=8,
    )

    try:
        data_type, udt_name = get_target_species_column_type(conn)
        print(
            "[ODWC Species] target_species type: "
            f"{data_type} / {udt_name}",
            flush=True,
        )

        cur = conn.cursor(cursor_factory=RealDictCursor)

        if only_lake:
            cur.execute(
                """
                SELECT lake_code, name
                FROM lakes
                WHERE UPPER(lake_code) = UPPER(%s)
                ORDER BY name;
                """,
                (only_lake,),
            )
        else:
            cur.execute(
                """
                SELECT lake_code, name
                FROM lakes
                ORDER BY name;
                """
            )

        lakes = cur.fetchall()

        if not lakes:
            print("[ODWC Species] No matching lakes found.", flush=True)
            cur.close()
            return 1

        session = requests.Session()
        session.headers.update(HEADERS)

        print("[ODWC Species] Crawling ODWC lake directory...", flush=True)
        catalog = crawl_odwc_directory(session)
        print(
            f"[ODWC Species] Directory catalog entries: {len(catalog)}",
            flush=True,
        )

        updated = 0
        unchanged = 0
        no_url = 0
        no_species = 0
        errors = 0

        for lake in lakes:
            code = lake["lake_code"]
            name = lake["name"]

            target_url = resolve_odwc_url(code, name, catalog)

            if not target_url:
                no_url += 1
                print(
                    f"[{code}] SKIP - no ODWC page matched for {name}",
                    flush=True,
                )
                continue

            try:
                response = session.get(target_url, timeout=15)
                response.raise_for_status()

                species = extract_species_from_html(response.text)

                if not species:
                    no_species += 1
                    print(
                        f"[{code}] SKIP - Fish Species of Interest not found "
                        f"or empty: {target_url}",
                        flush=True,
                    )
                    continue

                db_value = serialize_species_for_db(
                    species,
                    data_type,
                    udt_name,
                )

                # Avoid pointless writes if the stored value already represents
                # the same species list.
                cur.execute(
                    "SELECT target_species FROM lakes WHERE lake_code = %s;",
                    (code,),
                )
                current_row = cur.fetchone()
                current_value = (
                    current_row["target_species"] if current_row else None
                )

                if isinstance(current_value, str):
                    current_compare = [
                        x.strip()
                        for x in current_value.split(",")
                        if x.strip()
                    ]
                elif isinstance(current_value, (list, tuple)):
                    current_compare = list(current_value)
                else:
                    current_compare = current_value

                if current_compare == species:
                    unchanged += 1
                    print(
                        f"[{code}] OK unchanged - {', '.join(species)}",
                        flush=True,
                    )
                    continue

                if dry_run:
                    print(
                        f"[{code}] DRY RUN - would store: "
                        f"{', '.join(species)}",
                        flush=True,
                    )
                else:
                    cur.execute(
                        """
                        UPDATE lakes
                        SET target_species = %s
                        WHERE lake_code = %s;
                        """,
                        (db_value, code),
                    )
                    conn.commit()

                    print(
                        f"[{code}] UPDATED - {', '.join(species)}",
                        flush=True,
                    )

                updated += 1

                # Be polite to ODWC and avoid hammering the site.
                time.sleep(0.25)

            except Exception as exc:
                errors += 1
                conn.rollback()
                print(
                    f"[{code}] ERROR - {name}: {exc}",
                    flush=True,
                )

        cur.close()

        print("", flush=True)
        print("[ODWC Species] Sync summary", flush=True)
        print(f"  Lakes examined : {len(lakes)}", flush=True)
        print(f"  Updated        : {updated}", flush=True)
        print(f"  Unchanged      : {unchanged}", flush=True)
        print(f"  No ODWC URL    : {no_url}", flush=True)
        print(f"  No species     : {no_species}", flush=True)
        print(f"  Errors         : {errors}", flush=True)

        return 0 if errors == 0 else 2

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scrape ODWC Fish Species of Interest for lakes in PostgreSQL "
            "and store them in lakes.target_species."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and print results without updating PostgreSQL.",
    )
    parser.add_argument(
        "--lake",
        metavar="LAKE_CODE",
        help="Process only one lake code, e.g. --lake ARCA.",
    )

    args = parser.parse_args()

    return sync_species(
        dry_run=args.dry_run,
        only_lake=args.lake,
    )


if __name__ == "__main__":
    sys.exit(main())
