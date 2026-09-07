#!/usr/bin/env python3

import os
import re
import html
import hashlib
import requests
import psycopg2

from datetime import datetime, timezone


DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 OK-Lakes/1.0"
}


# ============================================================
# Edmond Current Projects StoryMap
# ============================================================

STORYMAP_ID = "7d9d0a7285f5458eb9f3ea955e5ec42d"

ITEM_URL = (
    f"https://www.arcgis.com/sharing/rest/content/items/"
    f"{STORYMAP_ID}?f=json"
)

DATA_URL = (
    f"https://www.arcgis.com/sharing/rest/content/items/"
    f"{STORYMAP_ID}/data?f=json"
)

SOURCE_URL = (
    f"https://storymaps.arcgis.com/stories/{STORYMAP_ID}"
)

SOURCE_NAME = (
    "City of Edmond Current Public and Capital Improvement Projects"
)

ARCADIA_TERMS = [
    "arcadia lake",
    "arcadia lake intake",
    "arcadia lake water treatment plant",
    "arcadia lake wtp",
    "spring creek park at arcadia lake",
]


def get_db():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )


def fetch_json(url):
    r = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )
    r.raise_for_status()
    return r.json()


def clean_html(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_key(value):
    value = clean_html(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:150]


def ensure_table(conn):
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS lake_project_alerts (
            lake_code TEXT NOT NULL,
            project_key TEXT NOT NULL,
            project_name TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            summary TEXT,
            status TEXT,
            source_modified TIMESTAMPTZ,
            content_hash TEXT NOT NULL,
            first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            contact_text TEXT,
            background TEXT,
            cost TEXT,
            funding TEXT,
            anticipated_completion TEXT,
            update_text TEXT,
            PRIMARY KEY (lake_code, project_key)
        );
    """)

    conn.commit()
    cur.close()


def node_text(node):
    if not isinstance(node, dict):
        return ""

    data = node.get("data")

    if not isinstance(data, dict):
        return ""

    pieces = []

    for key in (
        "text",
        "title",
        "caption",
        "description",
        "altText",
    ):
        value = data.get(key)

        if isinstance(value, str):
            cleaned = clean_html(value)

            if cleaned and cleaned not in pieces:
                pieces.append(cleaned)

    return " ".join(pieces)


def is_heading_node(node):
    if not isinstance(node, dict):
        return False

    data = node.get("data")

    if not isinstance(data, dict):
        return False

    node_type = str(data.get("type") or "").lower()

    if node_type in {
        "h1",
        "h2",
        "h3",
        "h4",
        "heading",
        "title",
        "subheading",
    }:
        return True

    raw = data.get("text")

    if isinstance(raw, str):
        return bool(
            re.search(
                r"<\s*h[1-4]\b",
                raw,
                flags=re.I
            )
        )

    return False


def heading_text(node):
    data = node.get("data", {})

    for key in ("text", "title", "caption"):
        value = data.get(key)

        if isinstance(value, str):
            cleaned = clean_html(value)

            if cleaned:
                return cleaned[:200]

    return ""


def ordered_node_ids(data):
    nodes = data.get("nodes", {})

    if not isinstance(nodes, dict):
        return []

    result = []
    visited = set()

    def visit(node_id):
        if node_id in visited:
            return

        node = nodes.get(node_id)

        if not isinstance(node, dict):
            return

        visited.add(node_id)
        result.append(node_id)

        children = node.get("children")

        if isinstance(children, list):
            for child in children:
                if isinstance(child, str):
                    visit(child)

        data_obj = node.get("data")

        if isinstance(data_obj, dict):
            children = data_obj.get("children")

            if isinstance(children, list):
                for child in children:
                    if isinstance(child, str):
                        visit(child)

    root = data.get("root")

    if isinstance(root, str):
        visit(root)

    for node_id in nodes:
        if node_id not in visited:
            result.append(node_id)

    return result


def build_sections(data):
    nodes = data.get("nodes", {})

    if not isinstance(nodes, dict):
        return []

    sections = []
    current = None

    for node_id in ordered_node_ids(data):
        node = nodes.get(node_id)

        if not isinstance(node, dict):
            continue

        if is_heading_node(node):
            title = heading_text(node)

            if not title:
                continue

            if current:
                sections.append(current)

            current = {
                "title": title,
                "parts": [],
                "node_ids": [node_id],
            }

            continue

        if current is None:
            continue

        text = node_text(node)

        if text:
            current["parts"].append(text)
            current["node_ids"].append(node_id)

    if current:
        sections.append(current)

    return sections


def is_arcadia_related(section):
    combined = (
        section["title"]
        + " "
        + " ".join(section["parts"])
    ).lower()

    return any(
        term in combined
        for term in ARCADIA_TERMS
    )


FIELD_LABELS = [
    "Point of Contact",
    "Background",
    "Construction Cost",
    "Cost",
    "Funding",
    "Anticipated Completion",
    "Update for this Month",
    "Update for this Quarter",
    "Current Update",
]


def extract_labeled_field(text, labels):
    label_pattern = "|".join(
        re.escape(label)
        for label in labels
    )

    stop_pattern = "|".join(
        re.escape(label)
        for label in FIELD_LABELS
    )

    pattern = (
        rf"(?:{label_pattern})\s*:\s*"
        rf"(.*?)"
        rf"(?=(?:{stop_pattern})\s*:|$)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.I | re.S
    )

    if not match:
        return None

    return re.sub(
        r"\s+",
        " ",
        match.group(1)
    ).strip() or None


def parse_fields(section):
    text = re.sub(
        r"\s+",
        " ",
        " ".join(section["parts"])
    ).strip()

    contact = extract_labeled_field(
        text,
        ["Point of Contact"]
    )

    background = extract_labeled_field(
        text,
        ["Background"]
    )

    cost = extract_labeled_field(
        text,
        ["Construction Cost", "Cost"]
    )

    funding = extract_labeled_field(
        text,
        ["Funding"]
    )

    completion = extract_labeled_field(
        text,
        ["Anticipated Completion"]
    )

    update_text = extract_labeled_field(
        text,
        [
            "Update for this Month",
            "Update for this Quarter",
            "Current Update",
        ]
    )

    summary_parts = []

    if background:
        summary_parts.append(background)

    if update_text:
        summary_parts.append(update_text)

    return {
        "contact_text": contact,
        "background": background,
        "cost": cost,
        "funding": funding,
        "anticipated_completion": completion,
        "update_text": update_text,
        "summary": " ".join(summary_parts) or text,
    }


def save_project(
    conn,
    project_key,
    project_name,
    summary,
    content_hash,
    source_modified,
    contact_text,
    background,
    cost,
    funding,
    anticipated_completion,
    update_text,
):
    cur = conn.cursor()

    cur.execute("""
        SELECT content_hash
        FROM lake_project_alerts
        WHERE lake_code = 'ARCA'
          AND project_key = %s;
    """, (project_key,))

    existing = cur.fetchone()

    if existing is None:
        cur.execute("""
            INSERT INTO lake_project_alerts (
                lake_code,
                project_key,
                project_name,
                source_name,
                source_url,
                summary,
                status,
                source_modified,
                content_hash,
                first_seen,
                last_seen,
                changed_at,
                is_active,
                contact_text,
                background,
                cost,
                funding,
                anticipated_completion,
                update_text
            )
            VALUES (
                'ARCA',
                %s, %s, %s, %s, %s,
                'RELATED PROJECT',
                %s, %s,
                NOW(), NOW(), NOW(),
                TRUE,
                %s, %s, %s, %s, %s, %s
            );
        """, (
            project_key,
            project_name,
            SOURCE_NAME,
            SOURCE_URL,
            summary,
            source_modified,
            content_hash,
            contact_text,
            background,
            cost,
            funding,
            anticipated_completion,
            update_text,
        ))

        print(
            f"[Active Projects] ADDED ARCA: "
            f"{project_name}"
        )

    else:
        changed = existing[0] != content_hash

        cur.execute("""
            UPDATE lake_project_alerts
            SET
                project_name = %s,
                source_name = %s,
                source_url = %s,
                summary = %s,
                status = 'RELATED PROJECT',
                source_modified = %s,
                content_hash = %s,
                last_seen = NOW(),

                changed_at =
                    CASE
                        WHEN content_hash <> %s
                        THEN NOW()
                        ELSE changed_at
                    END,

                is_active = TRUE,
                contact_text = %s,
                background = %s,
                cost = %s,
                funding = %s,
                anticipated_completion = %s,
                update_text = %s

            WHERE lake_code = 'ARCA'
              AND project_key = %s;
        """, (
            project_name,
            SOURCE_NAME,
            SOURCE_URL,
            summary,
            source_modified,
            content_hash,
            content_hash,
            contact_text,
            background,
            cost,
            funding,
            anticipated_completion,
            update_text,
            project_key,
        ))

        if changed:
            print(
                f"[Active Projects] CHANGED ARCA: "
                f"{project_name}"
            )

    conn.commit()
    cur.close()


def sync_arcadia(conn):
    print(
        "[Active Projects] Checking Edmond StoryMap..."
    )

    meta = fetch_json(ITEM_URL)
    data = fetch_json(DATA_URL)

    source_modified = None

    if meta.get("modified"):
        source_modified = datetime.fromtimestamp(
            meta["modified"] / 1000.0,
            tz=timezone.utc
        )

    seen_keys = set()

    for section in build_sections(data):
        if not is_arcadia_related(section):
            continue

        title = section["title"].strip()
        key = normalize_key(title)

        if not title or not key:
            continue

        fields = parse_fields(section)

        digest_input = "\n".join([
            title,
            fields["background"] or "",
            fields["cost"] or "",
            fields["funding"] or "",
            fields["anticipated_completion"] or "",
            fields["update_text"] or "",
        ])

        digest = hashlib.sha256(
            digest_input.encode("utf-8")
        ).hexdigest()

        seen_keys.add(key)

        save_project(
            conn=conn,
            project_key=key,
            project_name=title,
            summary=fields["summary"],
            content_hash=digest,
            source_modified=source_modified,
            contact_text=fields["contact_text"],
            background=fields["background"],
            cost=fields["cost"],
            funding=fields["funding"],
            anticipated_completion=fields["anticipated_completion"],
            update_text=fields["update_text"],
        )

    cur = conn.cursor()

    cur.execute("""
        SELECT project_key
        FROM lake_project_alerts
        WHERE lake_code = 'ARCA'
          AND source_name = %s
          AND is_active = TRUE;
    """, (SOURCE_NAME,))

    existing = {
        row[0]
        for row in cur.fetchall()
    }

    for key in existing - seen_keys:
        cur.execute("""
            UPDATE lake_project_alerts
            SET
                is_active = FALSE,
                last_seen = NOW(),
                changed_at = NOW()
            WHERE lake_code = 'ARCA'
              AND project_key = %s;
        """, (key,))

        print(
            f"[Active Projects] INACTIVE ARCA: {key}"
        )

    conn.commit()
    cur.close()

    print(
        f"[Active Projects] Arcadia active projects: "
        f"{len(seen_keys)}"
    )


def main():
    conn = get_db()

    try:
        ensure_table(conn)
        sync_arcadia(conn)

    finally:
        conn.close()

    print(
        "[Active Projects] Daily Arcadia project sync complete."
    )


if __name__ == "__main__":
    main()
