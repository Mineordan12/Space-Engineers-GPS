#!/usr/bin/env python3
"""
Space Engineers GPS Navigator — Terminal Edition
Stargate-style naming convention (e.g., P3X-263)

Stores GPS coordinates in a shared MySQL database so multiple players
can add/search the same GPS list. Provides search/add functionality.
"""

import getpass
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import pyperclip
    CLIPBOARD_AVAILABLE = True
except ImportError:
    CLIPBOARD_AVAILABLE = False

try:
    import pymysql
    import pymysql.cursors
    import pymysql.err
except ImportError:
    print("This version needs PyMySQL. Install it with:\n    pip install pymysql")
    sys.exit(1)


VERSION = "2.4.3"


UPDATE_MANIFEST_URL = os.environ.get("SE_GPS_UPDATE_URL", "https://raw.githubusercontent.com/Mineordan12/Space-Engineers-GPS/main/manifest.json")


def _parse_version(v: str) -> tuple:
    """'2.10.1' -> (2, 10, 1), for a proper numeric comparison (not string)."""
    parts = []
    for p in v.strip().split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_update() -> dict | None:
    """
    Check UPDATE_MANIFEST_URL for a newer version. Returns the manifest
    dict if a newer version is available, else None. Never raises —
    network problems here should never block using the app.
    """
    if not UPDATE_MANIFEST_URL:
        return None
    try:
        req = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"User-Agent": "se-gps-navigator"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
        remote_version = manifest.get("version", "")
        if remote_version and _parse_version(remote_version) > _parse_version(VERSION):
            return manifest
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        pass
    return None


def perform_update(manifest: dict) -> bool:
    """
    Download the new script from manifest['url'] and replace this file
    on disk. Returns True on success. The running process still needs
    to be restarted to pick up the new code — it does not re-exec
    itself automatically, since that's risky to do mid-session.
    """
    url = manifest.get("url", "")
    if not url:
        print("  [!] Update manifest has no download URL.")
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "se-gps-navigator"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            new_code = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  [!] Download failed: {e}")
        return False

    this_file = Path(__file__).resolve()
    backup_file = this_file.with_suffix(this_file.suffix + ".bak")
    try:
        backup_file.write_bytes(this_file.read_bytes())
        this_file.write_bytes(new_code)
    except OSError as e:
        print(f"  [!] Could not write update: {e}")
        return False

    print(f"  [✓] Updated to a newer version. Old copy saved as {backup_file.name}")
    print("  [i] Restart the script to run the new version.")
    return True


CONFIG_FILE = Path.home() / ".se_gps_navigator" / "db_config.json"
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "se_gps",
    "password": "changeme",
    "database": "se_gps_navigator",
}


def _read_stored_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _write_stored_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def prompt_for_config() -> dict:
    """Interactively ask for MySQL connection details and save them."""
    print("=" * 60)
    print("  DATABASE SETUP")
    print("=" * 60)
    print(f"  No usable config found at {CONFIG_FILE}")
    print("  Let's set one up — this only needs to happen once.\n")

    host = input(f"  MySQL host/IP [{DEFAULT_CONFIG['host']}]: ").strip() or DEFAULT_CONFIG["host"]
    port_raw = input(f"  MySQL port [{DEFAULT_CONFIG['port']}]: ").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_CONFIG["port"]
    except ValueError:
        print("  [!] Invalid port, using default.")
        port = DEFAULT_CONFIG["port"]
    user = input(f"  MySQL username [{DEFAULT_CONFIG['user']}]: ").strip() or DEFAULT_CONFIG["user"]
    password = getpass.getpass("  MySQL password: ")
    database = input(f"  Database name [{DEFAULT_CONFIG['database']}]: ").strip() or DEFAULT_CONFIG["database"]

    config = {"host": host, "port": port, "user": user, "password": password, "database": database}
    _write_stored_config(config)
    print(f"\n  [✓] Saved to {CONFIG_FILE}\n")
    input("  Press Enter to continue...")
    return config


def _looks_unconfigured(config: dict, stored: dict) -> bool:
    """True if there's no real config to work with yet — either the
    file didn't exist, or it still has the placeholder password/host."""
    if not stored:
        return True
    if not config.get("host"):
        return True
    if config.get("password") in ("", "changeme"):
        return True
    return False


def load_config() -> dict:
    """
    Load MySQL connection settings. If nothing usable is on disk (no
    file yet, or it still has the placeholder password), prompt for
    the details interactively and save them — unless environment
    variables already fully supply a password, in which case those
    are used without prompting (handy for unattended/server setups).
    """
    stored = _read_stored_config()
    config = dict(DEFAULT_CONFIG)
    config.update(stored)

    def apply_env(cfg: dict) -> dict:
        cfg["host"] = os.environ.get("SE_GPS_DB_HOST", cfg["host"])
        cfg["port"] = int(os.environ.get("SE_GPS_DB_PORT", cfg["port"]))
        cfg["user"] = os.environ.get("SE_GPS_DB_USER", cfg["user"])
        cfg["password"] = os.environ.get("SE_GPS_DB_PASSWORD", cfg["password"])
        cfg["database"] = os.environ.get("SE_GPS_DB_NAME", cfg["database"])
        return cfg

    config = apply_env(config)
    env_supplied_password = bool(os.environ.get("SE_GPS_DB_PASSWORD"))

    if _looks_unconfigured(config, stored) and not env_supplied_password:
        config = prompt_for_config()
        config = apply_env(config)

    return config


_CONFIG = None


class DatabaseUnavailable(Exception):
    """Raised when the database can't be reached after retrying, or
    when the connected account doesn't have permission (not retried —
    that won't fix itself)."""
    pass


DB_CONNECT_RETRIES = 3
DB_ACTION_RETRIES = 2
DB_RETRY_BASE_DELAY = 2


DB_ACCESS_DENIED_ERRNOS = {1044, 1045, 1142, 1143, 1698}


def _is_access_denied(exc) -> bool:
    try:
        return bool(exc.args) and exc.args[0] in DB_ACCESS_DENIED_ERRNOS
    except Exception:
        return False


def get_connection():
    """
    Open a new MySQL connection, retrying a few times with backoff if
    the server doesn't respond right away (e.g. it's mid-restart).
    Raises DatabaseUnavailable if it still can't connect after
    retrying, or immediately (no retry) on a permissions error.
    """
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()

    last_err = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            return pymysql.connect(
                host=_CONFIG["host"],
                port=_CONFIG["port"],
                user=_CONFIG["user"],
                password=_CONFIG["password"],
                database=_CONFIG["database"],
                autocommit=False,
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=8,
            )
        except pymysql.err.OperationalError as e:
            last_err = e
            if _is_access_denied(e):
                raise DatabaseUnavailable(f"Access denied connecting to MySQL: {e}") from e
            if attempt < DB_CONNECT_RETRIES:
                wait = DB_RETRY_BASE_DELAY * attempt
                print(f"\n  [!] Database not responding (attempt {attempt}/{DB_CONNECT_RETRIES}): {e}")
                print(f"  [i] Retrying in {wait}s...")
                time.sleep(wait)

    raise DatabaseUnavailable(
        f"Could not connect to MySQL after {DB_CONNECT_RETRIES} attempts: {last_err}\n"
        f"  Check the settings in {CONFIG_FILE}"
    )


def run_db(work, *args, retries: int = DB_ACTION_RETRIES, **kwargs):
    """
    Run work(conn, *args, **kwargs) against a fresh MySQL connection.
    get_connection() already retries transient failures when first
    opening the connection; this additionally covers the connection
    dropping mid-operation (e.g. the DB server restarts while this
    call is in flight) by retrying the whole step with a brand-new
    connection. Safe to use for steps where all needed input has
    already been collected (renames, deletes, inserts) since nothing
    typed by the user is lost on retry. Always closes the connection.
    Raises DatabaseUnavailable if every attempt fails.
    """
    last_err = None
    for attempt in range(1, retries + 2):
        conn = None
        try:
            conn = get_connection()
            return work(conn, *args, **kwargs)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
            last_err = e
            if _is_access_denied(e):
                raise DatabaseUnavailable(f"Permission denied: {e}") from e
            if attempt <= retries:
                wait = DB_RETRY_BASE_DELAY * attempt
                print(f"\n  [!] Lost connection to the database mid-operation: {e}")
                print(f"  [i] Retrying ({attempt}/{retries}) in {wait}s...")
                time.sleep(wait)
                continue
            raise DatabaseUnavailable(f"Database kept failing mid-operation: {e}") from e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    raise DatabaseUnavailable(str(last_err))


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS clusters (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(64) NOT NULL UNIQUE,
        center_x DOUBLE NOT NULL,
        center_y DOUBLE NOT NULL,
        center_z DOUBLE NOT NULL
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_points (
        id INT AUTO_INCREMENT PRIMARY KEY,
        cluster_id INT NOT NULL,
        x DOUBLE NOT NULL,
        y DOUBLE NOT NULL,
        z DOUBLE NOT NULL,
        FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE CASCADE
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS entries (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        x DOUBLE NOT NULL,
        y DOUBLE NOT NULL,
        z DOUBLE NOT NULL,
        ore_type VARCHAR(32) DEFAULT 'Unknown',
        description TEXT,
        added_at DATETIME NOT NULL,
        cluster_id INT,
        report_count INT NOT NULL DEFAULT 1,
        location_type VARCHAR(16) NOT NULL DEFAULT '',
        FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE SET NULL
    ) ENGINE=InnoDB
    """,
]


def _ensure_column(conn, table: str, column: str, ddl: str):
    """
    Generic migration helper: adds `column` to `table` if it isn't
    there yet. Safe to call every run — no-ops once present.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND COLUMN_NAME = %s",
            (table, column),
        )
        row = cur.fetchone()
        if row and row["cnt"] == 0:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    conn.commit()


def _ensure_report_count_column(conn):
    _ensure_column(conn, "entries", "report_count", "report_count INT NOT NULL DEFAULT 1")


def _ensure_location_type_column(conn):
    _ensure_column(conn, "entries", "location_type", "location_type VARCHAR(16) NOT NULL DEFAULT ''")


def _ensure_marker_entry_column(conn):
    _ensure_column(conn, "clusters", "marker_entry_id", "marker_entry_id INT NULL")


def _init_db_work(conn):
    with conn.cursor() as cur:
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)
    conn.commit()
    _ensure_report_count_column(conn)
    _ensure_location_type_column(conn)
    _ensure_marker_entry_column(conn)


def init_db():
    """Create tables if they don't exist yet. Safe to call every run."""
    run_db(_init_db_work)


STARGATE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
STARGATE_DIGITS = "0123456789"

ORE_ALIASES = {
    "fe": ["fe", "iron"],
    "ni": ["ni", "nickel"],
    "co": ["co", "cobalt"],
    "si": ["si", "silicon"],
    "mg": ["mg", "magnesium"],
    "ag": ["ag", "silver"],
    "au": ["au", "gold"],
    "pt": ["pt", "platinum"],
    "u":  ["u", "uranium"],
    "ice": ["ice"],
    "stone": ["stone"],
}


def _load_data_work(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, center_x, center_y, center_z, marker_entry_id FROM clusters")
        cluster_rows = cur.fetchall()

        clusters = []
        for c in cluster_rows:
            cur.execute(
                "SELECT x, y, z FROM cluster_points WHERE cluster_id=%s", (c["id"],)
            )
            points = cur.fetchall()
            clusters.append({
                "id": c["id"],
                "name": c["name"],
                "center_x": c["center_x"],
                "center_y": c["center_y"],
                "center_z": c["center_z"],
                "marker_entry_id": c["marker_entry_id"],
                "entries": [{"x": p["x"], "y": p["y"], "z": p["z"]} for p in points],
            })

        cur.execute(
            "SELECT id, name, x, y, z, ore_type, description, added_at, cluster_id, "
            "report_count, location_type FROM entries"
        )
        entry_rows = cur.fetchall()
        entries = []
        for e in entry_rows:
            entries.append({
                "id": e["id"],
                "name": e["name"],
                "x": e["x"],
                "y": e["y"],
                "z": e["z"],
                "ore_type": e["ore_type"] or "Unknown",
                "description": e["description"] or "",
                "added_at": e["added_at"].isoformat() if e["added_at"] else "",
                "cluster_id": e["cluster_id"],
                "report_count": e["report_count"] or 1,
                "location_type": e["location_type"] or "",
            })

    return {"entries": entries, "clusters": clusters}


def load_data() -> dict:
    """Load the full, current GPS database from MySQL.

    Every mutation (add/rename) is written straight to the DB as it
    happens, so calling this always reflects what every other client
    has saved too — that's what makes the list "shared".
    """
    return run_db(_load_data_work)


def db_insert_entry(conn, entry: dict) -> int:
    """Insert one GPS entry row and return its new id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entries (name, x, y, z, ore_type, description, added_at, cluster_id, location_type) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entry["name"], entry["x"], entry["y"], entry["z"],
                entry.get("ore_type", "Unknown"), entry.get("description", ""),
                entry.get("added_at") or datetime.now(), entry.get("cluster_id"),
                entry.get("location_type", ""),
            ),
        )
        new_id = cur.lastrowid
    conn.commit()
    return new_id


NAME_GROUP_RADIUS = 1_000_000


def _find_group_prefix(data: dict, x: float, y: float, z: float, exclude_cluster_id=None) -> str | None:
    """
    Look across existing clusters for the nearest one within
    NAME_GROUP_RADIUS that already has a standard-format name
    ([Letter][Digit][Letter]-[digits]), and return its 3-character
    prefix. Returns None if nothing qualifying is nearby.
    """
    best_prefix = None
    best_dist = None
    for cluster in data.get("clusters", []):
        if exclude_cluster_id is not None and cluster.get("id") == exclude_cluster_id:
            continue
        m = re.match(r'^([A-Z]\d[A-Z])-\d+$', cluster.get("name", ""))
        if not m:
            continue
        dist = distance_3d(x, y, z, cluster["center_x"], cluster["center_y"], cluster["center_z"])
        if dist <= NAME_GROUP_RADIUS and (best_dist is None or dist < best_dist):
            best_prefix = m.group(1)
            best_dist = dist
    return best_prefix


def generate_stargate_name(existing_names: set, data: dict = None, x: float = None,
                            y: float = None, z: float = None, exclude_cluster_id=None) -> str:
    """
    Generate a unique Stargate-style name like P3X-263.
    Format: [Letter][Digit][Letter]-[3 digits]

    If `data`/`x`/`y`/`z` are supplied, any cluster within
    NAME_GROUP_RADIUS (2000km) of this position "donates" its
    [Letter][Digit][Letter] prefix — only the trailing digits differ —
    so two nearby cluster names hint at their proximity at a glance.
    Falls back to a fully random prefix when nothing qualifying is
    nearby (or when no position context is given at all).
    """
    prefix = None
    if data is not None and x is not None and y is not None and z is not None:
        prefix = _find_group_prefix(data, x, y, z, exclude_cluster_id=exclude_cluster_id)

    def _random_prefix() -> str:
        return (
            random.choice(STARGATE_LETTERS)
            + random.choice(STARGATE_DIGITS)
            + random.choice(STARGATE_LETTERS)
        )

    attempts = 0
    while attempts < 10000:
        use_prefix = prefix or _random_prefix()
        suffix = random.randint(100, 999)
        name = f"{use_prefix}-{suffix}"
        if name not in existing_names:
            return name
        attempts += 1


    use_prefix = prefix or _random_prefix()
    suffix = random.randint(1000, 9999)
    return f"{use_prefix}-{suffix}"


def parse_se_gps_string(text: str) -> dict | None:
    """
    Parse a Space Engineers GPS string.
    Format: GPS:Name:X:Y:Z:#FFFFFF:Description:
    """
    text = text.strip()
    if not text.startswith("GPS:"):
        return None

    parts = text.split(":")
    if len(parts) < 5:
        return None

    try:
        name = parts[1] if parts[1] else "Unnamed"
        x = float(parts[2])
        y = float(parts[3])
        z = float(parts[4])
        color = parts[5] if len(parts) > 5 else ""
        desc = ":".join(parts[6:]) if len(parts) > 6 else ""
        desc = desc.rstrip(":")

        return {
            "name": name,
            "x": x,
            "y": y,
            "z": z,
            "color": color,
            "description": desc
        }
    except (ValueError, IndexError):
        return None


def format_se_gps_string(entry: dict) -> str:
    """Format an entry as a vanilla-compatible Space Engineers GPS string."""
    return f"GPS:{entry['name']}:{entry['x']:.2f}:{entry['y']:.2f}:{entry['z']:.2f}:"


def get_database_username() -> str:
    """Return the configured MySQL username for GPS uploader attribution."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG["user"]


def add_uploader_to_description(description: str, uploader: str) -> str:
    """Add a readable uploader attribution to a GPS marker description."""
    attribution = f"Uploaded by: {uploader}"
    if attribution in description:
        return description
    return f"{description}; {attribution}".strip("; ")


def detect_all_ore_types(text: str) -> list[str]:
    """
    Detect every ore type mentioned in text, in ORE_ALIASES order.
    Handles deposits naming multiple resources, e.g. "Iron/Silicon/Gold".
    """
    if not text:
        return []
    text_lower = text.lower()
    found = []
    for ore_key, aliases in ORE_ALIASES.items():
        for alias in aliases:


            pattern = r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])'
            if re.search(pattern, text_lower):
                found.append(ore_key.upper())
                break
    return found


def detect_ore_type(text: str) -> str:
    """Detect the first/primary ore type from text."""
    ores = detect_all_ore_types(text)
    return ores[0] if ores else "Unknown"


def normalize_ore_query(query: str) -> list[str]:
    """Normalize an ore search query into matching terms."""
    query_lower = query.lower().strip()
    for ore_key, aliases in ORE_ALIASES.items():
        if query_lower in aliases or query_lower == ore_key:
            return aliases
    return [query_lower]


def resolve_ore_key(query: str) -> str | None:
    """Return the canonical ore key (e.g. 'u', 'au') if query matches one
    exactly (as the key itself or one of its aliases), else None."""
    query_lower = query.lower().strip()
    for ore_key, aliases in ORE_ALIASES.items():
        if query_lower == ore_key or query_lower in aliases:
            return ore_key
    return None


def distance_3d(x1, y1, z1, x2, y2, z2) -> float:
    return math.sqrt((x2 - x1)**2 + (y2 - y1)**2 + (z2 - z1)**2)


def format_distance(meters: float) -> str:
    if meters >= 1_000_000:
        return f"{meters / 1_000_000:.2f} Mm"
    elif meters >= 1000:
        return f"{meters / 1000:.2f} km"
    else:
        return f"{meters:.1f} m"


CLUSTER_RADIUS = 100_000
DEDUP_RADIUS = 1_500

def get_cluster_for_position(data: dict, x: float, y: float, z: float) -> dict | None:
    """Find existing cluster within 100km, or return None."""
    for cluster in data.get("clusters", []):
        cx, cy, cz = cluster["center_x"], cluster["center_y"], cluster["center_z"]
        if distance_3d(x, y, z, cx, cy, cz) <= CLUSTER_RADIUS:
            return cluster
    return None


def update_cluster_center(cluster: dict):
    """Recalculate cluster center from its entries."""
    entries = cluster.get("entries", [])
    if not entries:
        return
    xs = [e["x"] for e in entries]
    ys = [e["y"] for e in entries]
    zs = [e["z"] for e in entries]
    cluster["center_x"] = sum(xs) / len(xs)
    cluster["center_y"] = sum(ys) / len(ys)
    cluster["center_z"] = sum(zs) / len(zs)


def get_cluster_for_entry(data: dict, entry: dict) -> dict | None:
    """Find which cluster an entry belongs to, via its cluster_id column."""
    cid = entry.get("cluster_id")
    if cid is None:
        return None
    for cluster in data.get("clusters", []):
        if cluster.get("id") == cid:
            return cluster
    return None


def find_cluster_by_name(data: dict, text: str) -> dict | None:
    """
    Look for an existing cluster whose name is referenced (whole-word,
    case-insensitive) inside `text`.

    e.g. text="DC-32 Gold" matches a cluster named "DC-32".
    """
    if not text:
        return None
    text_lower = text.lower()
    for cluster in data.get("clusters", []):
        name = cluster.get("name", "")
        if not name:
            continue

        pattern = r'(?<![A-Za-z0-9])' + re.escape(name.lower()) + r'(?![A-Za-z0-9])'
        if re.search(pattern, text_lower):
            return cluster
    return None


def rename_cluster_in_entries(conn, data: dict, old_name: str, new_name: str) -> int:
    """
    Update GPS entry names that start with old_name (e.g. "X3C-395 FE")
    to use new_name instead, writing each change to MySQL. Returns how
    many entries were changed.
    """
    if not old_name:
        return 0
    pattern = r'^' + re.escape(old_name) + r'(?![A-Za-z0-9])'
    count = 0
    with conn.cursor() as cur:
        for e in data["entries"]:
            updated, n = re.subn(pattern, new_name, e["name"])
            if n:
                e["name"] = updated
                cur.execute("UPDATE entries SET name=%s WHERE id=%s", (updated, e["id"]))
                count += 1
    conn.commit()
    return count


def _create_cluster_marker(conn, data: dict, cluster: dict) -> int:
    """
    Create the GPS entry that represents this cluster's own center
    point (name matches the cluster, ore_type 'CLUSTER'), and register
    it as the cluster's marker_entry_id. Appends it to data["entries"]
    in place so it's immediately visible this session.
    """
    entry = {
        "name": cluster["name"],
        "x": cluster["center_x"],
        "y": cluster["center_y"],
        "z": cluster["center_z"],
        "ore_type": "CLUSTER",
        "description": "Auto-generated cluster center marker",
        "added_at": datetime.now(),
        "cluster_id": cluster["id"],
        "location_type": "Cluster Center",
    }
    marker_id = db_insert_entry(conn, entry)
    entry["id"] = marker_id
    entry["report_count"] = 1
    with conn.cursor() as cur:
        cur.execute("UPDATE clusters SET marker_entry_id=%s WHERE id=%s", (marker_id, cluster["id"]))
    conn.commit()
    cluster["marker_entry_id"] = marker_id
    data.setdefault("entries", []).append(entry)
    return marker_id


def _sync_cluster_marker(conn, data: dict, cluster: dict):
    """
    Push the cluster's current center to its marker GPS entry (and the
    in-memory copy, if loaded). Creates the marker if this cluster
    doesn't have one yet (e.g. it predates this feature).
    """
    marker_id = cluster.get("marker_entry_id")
    if not marker_id:
        _create_cluster_marker(conn, data, cluster)
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE entries SET x=%s, y=%s, z=%s WHERE id=%s",
            (cluster["center_x"], cluster["center_y"], cluster["center_z"], marker_id),
        )
    conn.commit()
    for e in data.get("entries", []):
        if e.get("id") == marker_id:
            e["x"], e["y"], e["z"] = cluster["center_x"], cluster["center_y"], cluster["center_z"]
            break


def _persist_cluster_point(conn, cluster: dict, x: float, y: float, z: float):
    """Record a new point on an existing cluster and push its recalculated
    center to the DB (center_x/y/z on `cluster` must already be updated)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cluster_points (cluster_id, x, y, z) VALUES (%s,%s,%s,%s)",
            (cluster["id"], x, y, z),
        )
        cur.execute(
            "UPDATE clusters SET center_x=%s, center_y=%s, center_z=%s WHERE id=%s",
            (cluster["center_x"], cluster["center_y"], cluster["center_z"], cluster["id"]),
        )
    conn.commit()


def resolve_cluster(conn, data: dict, raw_name: str, x: float, y: float, z: float) -> tuple[dict, bool]:
    """
    Figure out which cluster a point belongs to, in priority order:
      1) a cluster referenced by name in raw_name
      2) an existing cluster within CLUSTER_RADIUS
      3) a brand-new auto-named cluster
    Registers the point into the cluster's entries, recalculates its
    center, writes the change to MySQL, and keeps the cluster's own
    GPS marker in sync. Returns (cluster, matched_by_name).
    """
    matched_cluster = find_cluster_by_name(data, raw_name) if raw_name else None
    if matched_cluster:
        cluster = matched_cluster
        print(f"\n  [i] Matched existing cluster '{cluster['name']}' from name '{raw_name}'")
        cluster["entries"].append({"x": x, "y": y, "z": z})
        update_cluster_center(cluster)
        _persist_cluster_point(conn, cluster, x, y, z)
        _sync_cluster_marker(conn, data, cluster)
        return cluster, True

    cluster = get_cluster_for_position(data, x, y, z)
    if cluster:
        dist = distance_3d(x, y, z, cluster["center_x"], cluster["center_y"], cluster["center_z"])
        print(f"\n  [i] Within cluster '{cluster['name']}' ({format_distance(dist)} from center)")
        cluster["entries"].append({"x": x, "y": y, "z": z})
        update_cluster_center(cluster)
        _persist_cluster_point(conn, cluster, x, y, z)
        _sync_cluster_marker(conn, data, cluster)
        return cluster, False


    existing_names = {e["name"] for e in data["entries"]}
    existing_cluster_names = {c["name"] for c in data.get("clusters", [])}
    for _ in range(5):
        stargate_name = generate_stargate_name(existing_names | existing_cluster_names, data=data, x=x, y=y, z=z)
        cluster = {
            "name": stargate_name,
            "center_x": x,
            "center_y": y,
            "center_z": z,
            "marker_entry_id": None,
            "entries": [{"x": x, "y": y, "z": z}],
        }
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clusters (name, center_x, center_y, center_z) VALUES (%s,%s,%s,%s)",
                    (cluster["name"], x, y, z),
                )
                cluster["id"] = cur.lastrowid
                cur.execute(
                    "INSERT INTO cluster_points (cluster_id, x, y, z) VALUES (%s,%s,%s,%s)",
                    (cluster["id"], x, y, z),
                )
            conn.commit()
            break
        except pymysql.err.IntegrityError:
            conn.rollback()
            existing_cluster_names.add(stargate_name)
            continue
    else:
        raise RuntimeError("Could not generate a unique cluster name after several attempts.")

    data["clusters"].append(cluster)
    _create_cluster_marker(conn, data, cluster)
    print(f"\n  [+] Created new cluster: {stargate_name}")
    return cluster, False


def find_nearby_same_ore(data: dict, ore_type: str, x: float, y: float, z: float) -> dict | None:
    """
    Look for an existing entry of the same ore type within DEDUP_RADIUS.
    Used so two people marking the same deposit end up as one GPS
    signal instead of two near-duplicate markers.
    """
    best = None
    best_dist = None
    for e in data["entries"]:
        if e.get("ore_type", "Unknown").upper() != ore_type.upper():
            continue
        dist = distance_3d(x, y, z, e["x"], e["y"], e["z"])
        if dist <= DEDUP_RADIUS and (best is None or dist < best_dist):
            best = e
            best_dist = dist
    return best


def _update_cluster_point(conn, cluster: dict, old_x, old_y, old_z, new_x, new_y, new_z):
    """Move one recorded point within a cluster (used when a merge shifts
    an entry's coordinates) and recalculate/persist the cluster center."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_points SET x=%s, y=%s, z=%s "
            "WHERE cluster_id=%s AND x=%s AND y=%s AND z=%s LIMIT 1",
            (new_x, new_y, new_z, cluster["id"], old_x, old_y, old_z),
        )
    for p in cluster.get("entries", []):
        if p["x"] == old_x and p["y"] == old_y and p["z"] == old_z:
            p["x"], p["y"], p["z"] = new_x, new_y, new_z
            break
    update_cluster_center(cluster)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE clusters SET center_x=%s, center_y=%s, center_z=%s WHERE id=%s",
            (cluster["center_x"], cluster["center_y"], cluster["center_z"], cluster["id"]),
        )
    conn.commit()


def merge_into_existing(conn, data: dict, existing: dict, x: float, y: float, z: float, description: str) -> dict:
    """
    Fold a new same-ore report into an existing nearby entry instead of
    creating a duplicate: weighted-average the position (so an entry
    that's already been confirmed several times moves less per new
    report), bump its report count, and keep the parent cluster's
    recorded point/center (and its GPS marker) in sync.
    """
    old_x, old_y, old_z = existing["x"], existing["y"], existing["z"]
    report_count = existing.get("report_count", 1)

    new_x = (old_x * report_count + x) / (report_count + 1)
    new_y = (old_y * report_count + y) / (report_count + 1)
    new_z = (old_z * report_count + z) / (report_count + 1)
    new_report_count = report_count + 1

    new_description = existing.get("description", "") or ""
    if description and description not in new_description:
        new_description = f"{new_description}; {description}".strip("; ")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE entries SET x=%s, y=%s, z=%s, report_count=%s, description=%s WHERE id=%s",
            (new_x, new_y, new_z, new_report_count, new_description, existing["id"]),
        )
    conn.commit()

    existing["x"], existing["y"], existing["z"] = new_x, new_y, new_z
    existing["report_count"] = new_report_count
    existing["description"] = new_description

    cluster = get_cluster_for_entry(data, existing)
    if cluster:
        _update_cluster_point(conn, cluster, old_x, old_y, old_z, new_x, new_y, new_z)
        _sync_cluster_marker(conn, data, cluster)

    return existing


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def print_header(title: str):
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print()


def input_coords(prompt: str = "Enter coordinates") -> tuple[float, float, float]:
    """Get X Y Z from user."""
    while True:
        raw = input(f"{prompt} (X Y Z or X,Y,Z): ").strip()
        if not raw:
            return None
        parts = [p.strip() for p in raw.replace(",", " ").split()]
        if len(parts) != 3:
            print("  [!] Please enter exactly 3 numbers.")
            continue
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            print("  [!] Invalid numbers. Try again.")


def copy_to_clipboard(text: str):
    if CLIPBOARD_AVAILABLE:
        pyperclip.copy(text)
        print(f"  [✓] Copied to clipboard: {text}")
    else:
        print(f"  [!] pyperclip not installed. Copy manually:")
        print(f"      {text}")


LOCATION_TYPES = {"1": "Asteroid", "2": "Planet", "3": "Station"}


def prompt_location_type() -> str:
    print("\nWhat kind of location is this?")
    print("  [1] Asteroid")
    print("  [2] Planet")
    print("  [3] Station")
    print("  [Enter] Not specified")
    choice = input("  Select: ").strip()
    return LOCATION_TYPES.get(choice, "")


def add_gps(data: dict):
    clear()
    print_header("ADD NEW GPS")

    while True:
        print("Paste a Space Engineers GPS string, or enter coordinates manually.")
        print("SE GPS format: GPS:Name:X:Y:Z:#FFFFFF:Description:")
        print("Leave blank for manual entry.")
        print("Type 'multi' to paste several GPS strings at once (one per line).\n")

        pasted = input("> ").strip()
        uploader = get_database_username()

        if pasted.lower() == "multi":
            _add_gps_batch(data, uploader)
        else:
            try:
                conn = get_connection()
            except DatabaseUnavailable as e:
                print(f"\n  [!] {e}")
            else:
                try:
                    _add_gps_inner(conn, data, pasted, uploader)
                except (pymysql.err.OperationalError, pymysql.err.InterfaceError, DatabaseUnavailable) as e:
                    print(f"\n  [!] Lost connection to the database while adding this GPS: {e}")
                    print("  [i] Part of this action may have already been saved — check 'List all entries'")
                    print("      before adding it again.")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

        again = input("\n  Add another GPS? (Y/n): ").strip().lower()
        if again in ("n", "no"):
            return


def _add_gps_batch(data: dict, uploader: str):
    """
    Mass-paste mode: read GPS strings one per line until a blank line,
    then add each one automatically (auto ore detection, auto
    multi-resource splitting, auto dedup/clustering) with no further
    prompts per line — meant for dumping a big batch at once.
    """
    print("\n  Paste one GPS string per line. Leave a blank line when done.\n")
    lines = []
    while True:
        line = input("> ").strip()
        if not line:
            break
        lines.append(line)

    if not lines:
        print("  [!] No GPS strings entered.")
        return

    try:
        conn = get_connection()
    except DatabaseUnavailable as e:
        print(f"\n  [!] {e}")
        return

    added = []
    merged = []
    skipped = []

    try:
        for line in lines:
            parsed = parse_se_gps_string(line)
            if not parsed:
                skipped.append(line)
                continue

            x, y, z = parsed["x"], parsed["y"], parsed["z"]
            raw_name = parsed["name"]
            description = add_uploader_to_description(parsed["description"], uploader)

            detected_ores = detect_all_ore_types(raw_name + " " + description)
            ores_to_process = detected_ores if detected_ores else ["Unknown"]
            multi = len(ores_to_process) > 1

            cluster = None
            for ore in ores_to_process:
                nearby = find_nearby_same_ore(data, ore, x, y, z)
                if nearby:
                    updated = merge_into_existing(conn, data, nearby, x, y, z, description)
                    merged.append(updated)
                    continue

                name_has_cluster = False
                if cluster is None:
                    cluster, name_has_cluster = resolve_cluster(conn, data, raw_name, x, y, z)

                if not multi and name_has_cluster:
                    entry_name = raw_name
                elif not multi and ore == "Unknown":
                    entry_name = raw_name or cluster["name"]
                else:
                    entry_name = f"{cluster['name']} {ore}"

                entry = {
                    "name": entry_name,
                    "x": x, "y": y, "z": z,
                    "ore_type": ore,
                    "description": description,
                    "added_at": datetime.now(),
                    "cluster_id": cluster["id"],
                    "location_type": "",
                }
                entry["id"] = db_insert_entry(conn, entry)
                entry["report_count"] = 1
                data["entries"].append(entry)
                added.append(entry)
    except (pymysql.err.OperationalError, pymysql.err.InterfaceError, DatabaseUnavailable) as e:
        print(f"\n  [!] Lost connection to the database mid-batch: {e}")
        print(f"  [i] {len(added)} entrie(s) saved before the drop — check 'List all entries'.")
        try:
            conn.close()
        except Exception:
            pass
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(f"\n  [✓] Added {len(added)} new GPS signal(s).")
    if merged:
        print(f"  [i] Merged {len(merged)} report(s) into existing nearby markers.")
    if skipped:
        print(f"  [!] Skipped {len(skipped)} line(s) that weren't valid GPS strings:")
        for s in skipped:
            print(f"      {s}")


def _add_gps_inner(conn, data: dict, pasted: str, uploader: str):
    if pasted:
        parsed = parse_se_gps_string(pasted)
        if parsed:
            print(f"\n  Parsed: {parsed['name']} @ {parsed['x']:.2f}, {parsed['y']:.2f}, {parsed['z']:.2f}")
            x, y, z = parsed["x"], parsed["y"], parsed["z"]
            raw_name = parsed["name"]
            description = add_uploader_to_description(parsed["description"], uploader)
        else:
            print("  [!] Could not parse GPS string. Switching to manual entry.\n")
            coords = input_coords()
            if coords is None:
                return
            x, y, z = coords
            raw_name = ""
            description = add_uploader_to_description("", uploader)
    else:
        coords = input_coords()
        if coords is None:
            return
        x, y, z = coords
        raw_name = ""
        description = add_uploader_to_description("", uploader)

    location_type = prompt_location_type()
    is_station = location_type == "Station"


    detected_ores = [] if is_station else detect_all_ore_types(raw_name + " " + description)

    if is_station:
        ore_type = "STATION"
    elif len(detected_ores) > 1:
        print(f"\n  Detected multiple resources here: {', '.join(detected_ores)}")
        split_choice = input(f"  Split into {len(detected_ores)} separate GPS signals? (Y/n): ").strip().lower()

        if split_choice not in ("n", "no"):
            saved = []
            merged = []
            cluster = None

            for ore in detected_ores:
                nearby = find_nearby_same_ore(data, ore, x, y, z)
                if nearby:
                    dist = distance_3d(x, y, z, nearby["x"], nearby["y"], nearby["z"])
                    updated = merge_into_existing(conn, data, nearby, x, y, z, description)
                    merged.append((ore, dist, updated))
                    continue

                if cluster is None:
                    cluster, _ = resolve_cluster(conn, data, raw_name, x, y, z)

                entry = {
                    "name": f"{cluster['name']} {ore}",
                    "x": x,
                    "y": y,
                    "z": z,
                    "ore_type": ore,
                    "description": description,
                    "added_at": datetime.now(),
                    "cluster_id": cluster["id"],
                    "location_type": location_type,
                }
                entry["id"] = db_insert_entry(conn, entry)
                entry["report_count"] = 1
                data["entries"].append(entry)
                saved.append(entry)

            if merged:
                print(f"\n  [i] Merged {len(merged)} resource(s) into nearby existing markers (within {format_distance(DEDUP_RADIUS)}):")
                for ore, dist, e in merged:
                    print(f"      {ore} was {format_distance(dist)} from '{e['name']}' -> now {e['report_count']} reports @ {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}")

            if saved:
                print(f"\n  [✓] Saved {len(saved)} new GPS signal(s):")
                for e in saved:
                    print(f"      {e['name']} ({e['ore_type']}) @ {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}")

            all_relevant = saved + [e for _, _, e in merged]
            gps_strings = [format_se_gps_string(e) for e in all_relevant]
            if gps_strings:
                if CLIPBOARD_AVAILABLE:
                    copy_choice = input("\n  Copy all to clipboard (one per line)? (y/n): ").strip().lower()
                    if copy_choice in ("y", "yes"):
                        copy_to_clipboard("\n".join(gps_strings))
                else:
                    print("\n  [!] pyperclip not installed. Copy manually:")
                    for s in gps_strings:
                        print(f"      {s}")

            return


        ore_type = detected_ores[0]
        print()
        ore_override = input(f"  Ore type to use (Enter for {ore_type}): ").strip().upper()
        if ore_override:
            ore_type = ore_override
    else:
        auto_ore = detected_ores[0] if detected_ores else "Unknown"
        print(f"\n  Auto-detected ore type: {auto_ore}")
        ore_override = input("  Ore type (or Enter to accept): ").strip().upper()
        ore_type = ore_override if ore_override else auto_ore


    nearby = find_nearby_same_ore(data, ore_type, x, y, z)
    if nearby:
        dist = distance_3d(x, y, z, nearby["x"], nearby["y"], nearby["z"])
        print(f"\n  [i] Existing {ore_type} marker '{nearby['name']}' is {format_distance(dist)} away — merging instead of duplicating.")
        updated = merge_into_existing(conn, data, nearby, x, y, z, description)
        print(f"  [✓] Updated: {updated['name']} ({updated['ore_type']}) @ {updated['x']:.2f}, {updated['y']:.2f}, {updated['z']:.2f}  (reports: {updated['report_count']})")
        copy_to_clipboard(format_se_gps_string(updated))
        return


    cluster, name_has_cluster = resolve_cluster(conn, data, raw_name, x, y, z)


    if name_has_cluster:

        suggested_name = raw_name
    else:

        suggested_name = f"{cluster['name']} {ore_type}"

    print(f"\n  Suggested name: {suggested_name}")
    custom_name = input("  Custom name (or Enter to accept): ").strip()
    final_name = custom_name if custom_name else suggested_name

    entry = {
        "name": final_name,
        "x": x,
        "y": y,
        "z": z,
        "ore_type": ore_type,
        "description": description,
        "added_at": datetime.now(),
        "cluster_id": cluster["id"],
        "location_type": location_type,
    }
    entry["id"] = db_insert_entry(conn, entry)
    entry["report_count"] = 1
    data["entries"].append(entry)

    print(f"\n  [✓] Saved: {final_name} ({ore_type}) @ {x:.2f}, {y:.2f}, {z:.2f}")
    copy_to_clipboard(format_se_gps_string(entry))


def search_gps(data: dict):
    while True:
        clear()
        print_header("SEARCH GPS")

        if not data["entries"]:
            print("  No GPS entries in database yet.")
            print("  Use 'Add GPS' to create some first.\n")
            input("  Press Enter to continue...")
            return

        print("Paste your current position as a Space Engineers GPS string.")
        print("SE GPS format: GPS:Name:X:Y:Z:#FFFFFF:Description:\n")
        pasted = input("> ").strip()
        if not pasted:
            return
        current = parse_se_gps_string(pasted)
        if not current:
            print("\n  [!] Could not parse that GPS string.")
            input("\n  Press Enter to continue...")
            continue
        cx, cy, cz = current["x"], current["y"], current["z"]
        print(f"\n  Current position: {cx:.2f}, {cy:.2f}, {cz:.2f}")

        print("\nSearch by ore type (Fe, Ni, Co, Si, Mg, Ag, Au, Pt, U, Ice)")
        print("or by name/cluster (e.g., 'P3X', 'Velnor').")
        query = input("\nWhat are you looking for? ").strip()
        if not query:
            return


        ore_key = resolve_ore_key(query)
        is_ore_search = ore_key is not None
        ore_aliases = ORE_ALIASES[ore_key] if ore_key else []

        matches = []
        for entry in data["entries"]:
            if is_ore_search:
                entry_ore = entry.get("ore_type", "Unknown").upper()
                if entry_ore == ore_key.upper():
                    matched = True
                else:


                    hay = f"{entry['name']} {entry.get('description', '')}".lower()
                    matched = any(
                        re.search(r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', hay)
                        for alias in ore_aliases
                    )
            else:

                matched = query.lower() in entry["name"].lower() or query.lower() in entry.get("description", "").lower()

            if matched:
                dist = distance_3d(cx, cy, cz, entry["x"], entry["y"], entry["z"])
                matches.append({"entry": entry, "distance": dist})

        matches.sort(key=lambda m: m["distance"])

        clear()
        print_header(f"SEARCH RESULTS: {query.upper()}")
        print(f"  From position: {cx:.2f}, {cy:.2f}, {cz:.2f}")
        print(f"  Found {len(matches)} match(es)\n")

        if not matches:
            print("  No matches found.")
            input("\n  Press Enter to continue...")
        else:
            for i, m in enumerate(matches[:10], 1):
                e = m["entry"]
                gps_str = format_se_gps_string(e)
                confirmed = f"  |  confirmed {e['report_count']}x" if e.get("report_count", 1) > 1 else ""
                tag = f"  |  {e['location_type']}" if e.get("location_type") else ""
                print(f"  #{i}  {e['name']}  |  {e.get('ore_type', 'Unknown')}  |  {format_distance(m['distance'])}{tag}{confirmed}")
                print(f"       Coords: {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}")
                print(f"       GPS:    {gps_str}")
                if e.get("description"):
                    print(f"       Note:   {e['description'][:50]}")
                print()

            nearest = matches[0]["entry"]
            gps_str = format_se_gps_string(nearest)
            print("-" * 60)
            copy_choice = input("  Copy nearest GPS to clipboard? (y/n): ").strip().lower()
            if copy_choice in ("y", "yes"):
                copy_to_clipboard(gps_str)

            input("\n  Press Enter to continue...")

        again = input("\n  Search again? (Y/n): ").strip().lower()
        if again in ("n", "no"):
            return


def list_all(data: dict):
    if not data["entries"]:
        clear()
        print_header("ALL GPS ENTRIES")
        print("  Database is empty.")
        input("\n  Press Enter to continue...")
        return

    sort_mode = "ore"

    while True:
        clear()
        print_header("ALL GPS ENTRIES")
        mode_label = "Ore Type" if sort_mode == "ore" else "Cluster"
        print(f"  Sorted by: {mode_label}\n")

        groups = {}
        if sort_mode == "ore":
            for e in data["entries"]:
                key = e.get("ore_type", "Unknown")
                groups.setdefault(key, []).append(e)
        else:
            for e in data["entries"]:
                cluster = get_cluster_for_entry(data, e)
                key = cluster["name"] if cluster else "Unclustered"
                groups.setdefault(key, []).append(e)

        numbered = []
        for key in sorted(groups.keys()):
            entries = groups[key]
            print(f"\n  [{key}] — {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
            for e in entries:
                numbered.append(e)
                idx = len(numbered)
                confirmed = f"  ({e['report_count']}x confirmed)" if e.get("report_count", 1) > 1 else ""
                tag = f"  [{e['location_type']}]" if e.get("location_type") else ""
                if sort_mode == "ore":
                    print(f"    {idx:>3}. {e['name']}: {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}{tag}{confirmed}")
                else:
                    print(f"    {idx:>3}. {e['name']} ({e.get('ore_type', 'Unknown')}): {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}{tag}{confirmed}")

        print(f"\n  Total: {len(data['entries'])} entries, {len(data.get('clusters', []))} clusters")
        choice = input("\n  [#] Copy that entry's GPS   [t] Toggle sort   [Enter] Back: ").strip().lower()
        if choice == "t":
            sort_mode = "cluster" if sort_mode == "ore" else "ore"
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(numbered):
                e = numbered[idx - 1]
                copy_to_clipboard(format_se_gps_string(e))
            else:
                print("  [!] Invalid number.")
            input("\n  Press Enter to continue...")
            continue
        return


def _rename_entry_work(conn, entry_id: int, new_name: str):
    with conn.cursor() as cur:
        cur.execute("UPDATE entries SET name=%s WHERE id=%s", (new_name, entry_id))
    conn.commit()


def rename_entry(data: dict):
    while True:
        clear()
        print_header("RENAME GPS ENTRY")

        if not data["entries"]:
            print("  No GPS entries yet.")
            input("\n  Press Enter to continue...")
            return

        for i, e in enumerate(data["entries"], 1):
            print(f"  [{i:>3}] {e['name']}  ({e.get('ore_type', 'Unknown')})  @ {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}")

        choice = input("\n  Entry number to rename (or Enter to go back): ").strip()
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if idx < 0:
                raise IndexError
            entry = data["entries"][idx]
        except (ValueError, IndexError):
            print("  [!] Invalid entry number.")
            input("\n  Press Enter to continue...")
            continue

        old_name = entry["name"]
        new_name = input(f"  New name for '{old_name}': ").strip()
        if not new_name:
            print("  [!] Name cannot be empty. Cancelled.")
            input("\n  Press Enter to continue...")
            continue

        try:
            run_db(_rename_entry_work, entry["id"], new_name)
        except DatabaseUnavailable as e:
            print(f"\n  [!] Could not save the rename: {e}")
            input("\n  Press Enter to continue...")
            continue

        entry["name"] = new_name
        print(f"\n  [✓] Renamed '{old_name}' -> '{new_name}'")

        again = input("\n  Rename another entry? (Y/n): ").strip().lower()
        if again in ("n", "no"):
            return


def _regenerate_name_for_cluster(data: dict, cluster: dict) -> str:
    """
    Auto-generate a fresh proximity-grouped name for an existing
    cluster (used by the 'r' re-render option), without colliding
    with any currently-used name — including its own current one.
    """
    existing_names = {e["name"] for e in data["entries"]}
    existing_names |= {c["name"] for c in data["clusters"] if c["id"] != cluster["id"]}
    return generate_stargate_name(
        existing_names,
        data=data,
        x=cluster["center_x"], y=cluster["center_y"], z=cluster["center_z"],
        exclude_cluster_id=cluster["id"],
    )


def _rename_cluster_work(conn, data: dict, cluster_id: int, new_name: str, cascade: bool):
    with conn.cursor() as cur:
        cur.execute("UPDATE clusters SET name=%s WHERE id=%s", (new_name, cluster_id))
    conn.commit()

    cluster = next((c for c in data["clusters"] if c["id"] == cluster_id), None)
    old_name = cluster["name"] if cluster else None

    if cluster and cluster.get("marker_entry_id"):
        with conn.cursor() as cur:
            cur.execute("UPDATE entries SET name=%s WHERE id=%s", (new_name, cluster["marker_entry_id"]))
        conn.commit()
        for e in data["entries"]:
            if e.get("id") == cluster["marker_entry_id"]:
                e["name"] = new_name
                break

    updated = 0
    if cascade and old_name:
        updated = rename_cluster_in_entries(conn, data, old_name, new_name)
    return updated


def rename_cluster(data: dict):
    while True:
        clear()
        print_header("RENAME CLUSTER")

        clusters = data.get("clusters", [])
        if not clusters:
            print("  No clusters yet.")
            input("\n  Press Enter to continue...")
            return

        for i, c in enumerate(clusters, 1):
            print(f"  [{i:>3}] {c['name']}  ({len(c.get('entries', []))} point(s))")

        choice = input("\n  Cluster number to rename (or Enter to go back): ").strip()
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if idx < 0:
                raise IndexError
            cluster = clusters[idx]
        except (ValueError, IndexError):
            print("  [!] Invalid cluster number.")
            input("\n  Press Enter to continue...")
            continue

        old_name = cluster["name"]
        raw = input(f"  New name for '{old_name}' (or 'r' to auto re-render it): ").strip()
        if not raw:
            print("  [!] Name cannot be empty. Cancelled.")
            input("\n  Press Enter to continue...")
            continue

        if raw.lower() == "r":
            new_name = _regenerate_name_for_cluster(data, cluster)
            print(f"  [i] Re-rendered name: {new_name}")
        else:
            new_name = raw

        also = input(f"  Also update GPS entries starting with '{old_name}'? (Y/n): ").strip().lower()
        cascade = also not in ("n", "no")

        try:
            updated = run_db(_rename_cluster_work, data, cluster["id"], new_name, cascade)
        except DatabaseUnavailable as e:
            print(f"\n  [!] Could not save the rename: {e}")
            input("\n  Press Enter to continue...")
            continue

        cluster["name"] = new_name
        suffix = f", updated {updated} entr{'y' if updated == 1 else 'ies'}" if updated else ""
        print(f"\n  [✓] Renamed cluster '{old_name}' -> '{new_name}'{suffix}")

        again = input("\n  Rename another cluster? (Y/n): ").strip().lower()
        if again in ("n", "no"):
            return


def _remove_cluster_point(conn, data: dict, cluster: dict, x: float, y: float, z: float) -> bool:
    """Delete one recorded point from a cluster (used when deleting an
    entry) and recalc/persist the center (and sync its GPS marker), or
    remove the cluster (and its marker) entirely if that was its last
    point. Returns True if the cluster itself was removed."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM cluster_points WHERE cluster_id=%s AND x=%s AND y=%s AND z=%s LIMIT 1",
            (cluster["id"], x, y, z),
        )
    for i, p in enumerate(cluster.get("entries", [])):
        if p["x"] == x and p["y"] == y and p["z"] == z:
            del cluster["entries"][i]
            break

    if cluster.get("entries"):
        update_cluster_center(cluster)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clusters SET center_x=%s, center_y=%s, center_z=%s WHERE id=%s",
                (cluster["center_x"], cluster["center_y"], cluster["center_z"], cluster["id"]),
            )
        conn.commit()
        _sync_cluster_marker(conn, data, cluster)
        return False

    marker_id = cluster.get("marker_entry_id")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM clusters WHERE id=%s", (cluster["id"],))
        if marker_id:
            cur.execute("DELETE FROM entries WHERE id=%s", (marker_id,))
    conn.commit()
    if marker_id:
        data["entries"] = [e for e in data.get("entries", []) if e.get("id") != marker_id]
    return True


def _delete_entry_work(conn, data: dict, entry: dict):
    is_marker = entry.get("ore_type") == "CLUSTER"

    with conn.cursor() as cur:
        cur.execute("DELETE FROM entries WHERE id=%s", (entry["id"],))
    conn.commit()

    if is_marker:
        cluster = get_cluster_for_entry(data, entry)
        if cluster:
            with conn.cursor() as cur:
                cur.execute("UPDATE clusters SET marker_entry_id=NULL WHERE id=%s", (cluster["id"],))
            conn.commit()
            cluster["marker_entry_id"] = None
    else:
        cluster = get_cluster_for_entry(data, entry)
        if cluster:
            cluster_removed = _remove_cluster_point(conn, data, cluster, entry["x"], entry["y"], entry["z"])
            if cluster_removed and cluster in data["clusters"]:
                data["clusters"].remove(cluster)

    if entry in data["entries"]:
        data["entries"].remove(entry)


def delete_entry(data: dict):
    while True:
        clear()
        print_header("DELETE GPS ENTRY")

        if not data["entries"]:
            print("  No GPS entries yet.")
            input("\n  Press Enter to continue...")
            return

        for i, e in enumerate(data["entries"], 1):
            confirmed = f"  ({e['report_count']}x confirmed)" if e.get("report_count", 1) > 1 else ""
            tag = f"  [{e['location_type']}]" if e.get("location_type") else ""
            print(f"  [{i:>3}] {e['name']}  ({e.get('ore_type', 'Unknown')})  @ {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}{tag}{confirmed}")

        choice = input("\n  Entry number to delete (or Enter to go back): ").strip()
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if idx < 0:
                raise IndexError
            entry = data["entries"][idx]
        except (ValueError, IndexError):
            print("  [!] Invalid entry number.")
            input("\n  Press Enter to continue...")
            continue

        confirm = input(f"  Type 'yes' to permanently delete '{entry['name']}': ").strip().lower()
        if confirm != "yes":
            print("  Cancelled.")
            input("\n  Press Enter to continue...")
            continue

        try:
            run_db(_delete_entry_work, data, entry)
        except DatabaseUnavailable as e:
            print(f"\n  [!] Could not delete: {e}")
            input("\n  Press Enter to continue...")
            continue

        print(f"\n  [✓] Deleted '{entry['name']}'.")

        again = input("\n  Delete another entry? (Y/n): ").strip().lower()
        if again in ("n", "no"):
            return


def edit_names(data: dict):
    while True:
        clear()
        print_header("EDIT / DELETE")
        print("  [1] Rename a GPS entry")
        print("  [2] Rename a cluster (or auto re-render its name)")
        print("  [3] Delete a GPS entry")
        print("  [4] Back")
        print()

        choice = input("  Select: ").strip()
        if choice == "1":
            rename_entry(data)
        elif choice == "2":
            rename_cluster(data)
        elif choice == "3":
            delete_entry(data)
        elif choice == "4":
            return
        else:
            print("  [!] Invalid choice.")
            input("\n  Press Enter to continue...")


def where_am_i(data: dict):
    """Paste your current position and find out which cluster (if any)
    you're in or near, plus the nearest cluster if you're outside all
    of them. Offers to copy that cluster's own GPS marker."""
    while True:
        clear()
        print_header("WHERE AM I")

        clusters = data.get("clusters", [])
        if not clusters:
            print("  No clusters recorded yet.")
            input("\n  Press Enter to continue...")
            return

        print("Paste your current position as a Space Engineers GPS string.")
        print("SE GPS format: GPS:Name:X:Y:Z:#FFFFFF:Description:\n")
        pasted = input("> ").strip()
        if not pasted:
            return
        current = parse_se_gps_string(pasted)
        if not current:
            print("\n  [!] Could not parse that GPS string.")
            input("\n  Press Enter to continue...")
            continue
        cx, cy, cz = current["x"], current["y"], current["z"]

        ranked = []
        for cluster in clusters:
            dist = distance_3d(cx, cy, cz, cluster["center_x"], cluster["center_y"], cluster["center_z"])
            ranked.append((dist, cluster))
        ranked.sort(key=lambda t: t[0])

        clear()
        print_header("WHERE AM I")
        print(f"  Position: {cx:.2f}, {cy:.2f}, {cz:.2f}\n")

        nearest_dist, nearest_cluster = ranked[0]
        entry_count = len(nearest_cluster.get("entries", []))

        if nearest_dist <= CLUSTER_RADIUS:
            print(f"  [✓] You're inside cluster '{nearest_cluster['name']}'")
            print(f"      {format_distance(nearest_dist)} from its center, {entry_count} known GPS marker(s) here.")
        else:
            print("  You're not within any recorded cluster right now.")
            print(f"  Nearest is '{nearest_cluster['name']}', {format_distance(nearest_dist)} away ({entry_count} marker(s)).")

        if len(ranked) > 1:
            print("\n  Other nearby clusters:")
            for dist, cluster in ranked[1:4]:
                print(f"    • {cluster['name']} — {format_distance(dist)} away, {len(cluster.get('entries', []))} marker(s)")

        marker_id = nearest_cluster.get("marker_entry_id")
        marker_entry = next((e for e in data["entries"] if e.get("id") == marker_id), None) if marker_id else None
        if marker_entry:
            gps_str = format_se_gps_string(marker_entry)
        else:
            gps_str = format_se_gps_string({
                "name": nearest_cluster["name"],
                "x": nearest_cluster["center_x"],
                "y": nearest_cluster["center_y"],
                "z": nearest_cluster["center_z"],
            })

        copy_choice = input(f"\n  Copy '{nearest_cluster['name']}' cluster GPS to clipboard? (y/n): ").strip().lower()
        if copy_choice in ("y", "yes"):
            copy_to_clipboard(gps_str)

        again = input("\n  Check another position? (Y/n): ").strip().lower()
        if again in ("n", "no"):
            return


def safe_load_data(previous: dict) -> dict:
    """Refresh data from the DB, but never crash the menu loop — if the
    DB is unreachable, show a clear message and keep showing the last
    known data instead."""
    try:
        return load_data()
    except DatabaseUnavailable as e:
        print(f"\n  [!] Could not refresh from the database: {e}")
        print("  [i] Showing the last data loaded. Try the action again in a moment.")
        input("\n  Press Enter to continue...")
        return previous


def main_menu():
    update_manifest = check_for_update()

    try:
        data = load_data()
    except DatabaseUnavailable as e:
        print(f"\n  [!] {e}")
        sys.exit(1)

    while True:
        clear()
        print_header(f"SPACE ENGINEERS GPS NAVIGATOR  v{VERSION}")
        print(f"  Database: {len(data['entries'])} entries | {len(data.get('clusters', []))} clusters")
        print(f"  MySQL:    {_CONFIG['host']}:{_CONFIG['port']}/{_CONFIG['database']}")
        if update_manifest:
            print(f"  [!] Update available: v{update_manifest.get('version')} (option [7])")
        print()
        print("  [1] Search for GPS")
        print("  [2] Add new GPS")
        print("  [3] List all entries")
        print("  [4] Edit / Delete")
        print("  [5] Where am I")
        print("  [6] Exit")
        if update_manifest:
            print("  [7] Update to latest version")
        print()

        choice = input("  Select: ").strip()

        if choice == "1":
            search_gps(data)
            data = safe_load_data(data)
        elif choice == "2":
            add_gps(data)
            data = safe_load_data(data)
        elif choice == "3":
            list_all(data)
        elif choice == "4":
            edit_names(data)
            data = safe_load_data(data)
        elif choice == "5":
            where_am_i(data)
        elif choice == "6":
            print("\n  Good hunting, Engineer.\n")
            sys.exit(0)
        elif choice == "7" and update_manifest:
            confirm = input(f"  Download and install v{update_manifest.get('version')}? (y/n): ").strip().lower()
            if confirm in ("y", "yes"):
                if perform_update(update_manifest):
                    input("\n  Press Enter to exit...")
                    sys.exit(0)
            input("\n  Press Enter to continue...")
        else:
            print("  [!] Invalid choice.")


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"Space Engineers GPS Navigator v{VERSION}")
        sys.exit(0)

    try:
        init_db()
        main_menu()
    except DatabaseUnavailable as e:
        print(f"\n  [!] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Aborted.")
        sys.exit(0)
