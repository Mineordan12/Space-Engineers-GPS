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

# ── Version ─────────────────────────────────────────────────────────

VERSION = "2.3.0"

# Raw URL to a small JSON manifest in the GitHub repo, e.g.:
#   {"version": "2.3.0", "url": "https://raw.githubusercontent.com/<you>/<repo>/main/se_gps_navigator.py"}
# Leave UPDATE_MANIFEST_URL blank to disable update checking entirely.
UPDATE_MANIFEST_URL = os.environ.get("SE_GPS_UPDATE_URL", "https://raw.githubusercontent.com/Mineordan12/Space-Engineers-GPS/refs/heads/main/se_gps_navigator.py")


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


# ── Config ──────────────────────────────────────────────────────────
#
# Connection settings are read from a small JSON config file and can
# be overridden with environment variables — handy if several people
# run this script from their own machines and don't want to hand-edit
# a file:
#
#   SE_GPS_DB_HOST      SE_GPS_DB_PORT     SE_GPS_DB_USER
#   SE_GPS_DB_PASSWORD  SE_GPS_DB_NAME

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


# ── DB resilience: retry-on-connect, retry-mid-operation ────────────

class DatabaseUnavailable(Exception):
    """Raised when the database can't be reached after retrying, or
    when the connected account doesn't have permission (not retried —
    that won't fix itself)."""
    pass


DB_CONNECT_RETRIES = 3       # attempts when first opening a connection
DB_ACTION_RETRIES = 2        # extra attempts if a connection drops mid-operation
DB_RETRY_BASE_DELAY = 2      # seconds; multiplied by the attempt number

# MySQL error numbers that mean "this account isn't allowed to do
# that" — retrying won't help, so fail fast with a clear message
# instead of silently retrying a doomed request 3 times.
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
    for attempt in range(1, retries + 2):  # +1 for the initial try
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


def _ensure_column(conn, column: str, ddl: str):
    """
    Generic migration helper: adds `column` to entries if it isn't there
    yet. Safe to call every run — no-ops once the column is present.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'entries' "
            "AND COLUMN_NAME = %s",
            (column,),
        )
        row = cur.fetchone()
        if row and row["cnt"] == 0:
            cur.execute(f"ALTER TABLE entries ADD COLUMN {ddl}")
    conn.commit()


def _ensure_report_count_column(conn):
    _ensure_column(conn, "report_count", "report_count INT NOT NULL DEFAULT 1")


def _ensure_location_type_column(conn):
    _ensure_column(conn, "location_type", "location_type VARCHAR(16) NOT NULL DEFAULT ''")


def _init_db_work(conn):
    with conn.cursor() as cur:
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)
    conn.commit()
    _ensure_report_count_column(conn)
    _ensure_location_type_column(conn)


def init_db():
    """Create tables if they don't exist yet. Safe to call every run."""
    run_db(_init_db_work)


STARGATE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # No I, O to avoid confusion
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

# ── Data ────────────────────────────────────────────────────────────

def _load_data_work(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, center_x, center_y, center_z FROM clusters")
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


# ── Stargate Name Generator ────────────────────────────────────────

def generate_stargate_name(existing_names: set) -> str:
    """
    Generate a unique Stargate-style name like P3X-263.
    Format: [Letter][Digit][Letter]-[3 digits]
    """
    attempts = 0
    while attempts < 10000:
        letter1 = random.choice(STARGATE_LETTERS)
        digit1 = random.choice(STARGATE_DIGITS)
        letter2 = random.choice(STARGATE_LETTERS)
        suffix = random.randint(100, 999)
        name = f"{letter1}{digit1}{letter2}-{suffix}"
        if name not in existing_names:
            return name
        attempts += 1
    # Fallback with extended suffix
    letter1 = random.choice(STARGATE_LETTERS)
    digit1 = random.choice(STARGATE_DIGITS)
    letter2 = random.choice(STARGATE_LETTERS)
    suffix = random.randint(1000, 9999)
    return f"{letter1}{digit1}{letter2}-{suffix}"


# ── GPS Parsing ─────────────────────────────────────────────────────

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
    """Format entry back to SE GPS string."""
    return f"GPS:{entry['name']}:{entry['x']:.2f}:{entry['y']:.2f}:{entry['z']:.2f}:"


# ── Ore Detection ───────────────────────────────────────────────────

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
            # word-boundary match so short aliases like "u" or "co" don't
            # false-positive inside unrelated words
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


# ── Distance ────────────────────────────────────────────────────────

def distance_3d(x1, y1, z1, x2, y2, z2) -> float:
    return math.sqrt((x2 - x1)**2 + (y2 - y1)**2 + (z2 - z1)**2)


def format_distance(meters: float) -> str:
    if meters >= 1_000_000:
        return f"{meters / 1_000_000:.2f} Mm"
    elif meters >= 1000:
        return f"{meters / 1000:.2f} km"
    else:
        return f"{meters:.1f} m"


# ── Clustering ──────────────────────────────────────────────────────

CLUSTER_RADIUS = 100_000  # 100km
DEDUP_RADIUS = 1_500  # 1.5km — same-ore markers this close get merged into one

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
        # Word-boundary-ish match that still works around hyphens/digits
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
    center, and writes the change to MySQL. Returns (cluster, matched_by_name).
    """
    matched_cluster = find_cluster_by_name(data, raw_name) if raw_name else None
    if matched_cluster:
        cluster = matched_cluster
        print(f"\n  [i] Matched existing cluster '{cluster['name']}' from name '{raw_name}'")
        cluster["entries"].append({"x": x, "y": y, "z": z})
        update_cluster_center(cluster)
        _persist_cluster_point(conn, cluster, x, y, z)
        return cluster, True

    cluster = get_cluster_for_position(data, x, y, z)
    if cluster:
        dist = distance_3d(x, y, z, cluster["center_x"], cluster["center_y"], cluster["center_z"])
        print(f"\n  [i] Within cluster '{cluster['name']}' ({format_distance(dist)} from center)")
        cluster["entries"].append({"x": x, "y": y, "z": z})
        update_cluster_center(cluster)
        _persist_cluster_point(conn, cluster, x, y, z)
        return cluster, False

    # New cluster. Cluster names are UNIQUE in the DB, so if two players
    # add a brand-new site at the same moment and happen to roll the same
    # Stargate name, retry with a fresh name instead of failing.
    existing_names = {e["name"] for e in data["entries"]}
    existing_cluster_names = {c["name"] for c in data.get("clusters", [])}
    for _ in range(5):
        stargate_name = generate_stargate_name(existing_names | existing_cluster_names)
        cluster = {
            "name": stargate_name,
            "center_x": x,
            "center_y": y,
            "center_z": z,
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
    recorded point/center in sync.
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

    return existing


# ── UI Helpers ──────────────────────────────────────────────────────

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


# ── Add GPS ─────────────────────────────────────────────────────────

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

    print("Paste a Space Engineers GPS string, or enter coordinates manually.")
    print("SE GPS format: GPS:Name:X:Y:Z:#FFFFFF:Description:")
    print("Leave blank for manual entry.\n")

    try:
        conn = get_connection()
    except DatabaseUnavailable as e:
        print(f"\n  [!] {e}")
        input("\n  Press Enter to continue...")
        return

    try:
        _add_gps_inner(conn, data)
    except (pymysql.err.OperationalError, pymysql.err.InterfaceError, DatabaseUnavailable) as e:
        print(f"\n  [!] Lost connection to the database while adding this GPS: {e}")
        print("  [i] Part of this action may have already been saved — check 'List all entries'")
        print("      before adding it again.")
        input("\n  Press Enter to continue...")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _add_gps_inner(conn, data: dict):
    pasted = input("> ").strip()

    if pasted:
        parsed = parse_se_gps_string(pasted)
        if parsed:
            print(f"\n  Parsed: {parsed['name']} @ {parsed['x']:.2f}, {parsed['y']:.2f}, {parsed['z']:.2f}")
            x, y, z = parsed["x"], parsed["y"], parsed["z"]
            raw_name = parsed["name"]
            description = parsed["description"]
        else:
            print("  [!] Could not parse GPS string. Switching to manual entry.\n")
            coords = input_coords()
            if coords is None:
                return
            x, y, z = coords
            raw_name = ""
            description = ""
    else:
        coords = input_coords()
        if coords is None:
            return
        x, y, z = coords
        raw_name = ""
        description = ""

    location_type = prompt_location_type()
    is_station = location_type == "Station"

    # Detect ore type(s) — a single deposit can hold more than one
    # resource. Stations don't have a resource, so skip this entirely.
    detected_ores = [] if is_station else detect_all_ore_types(raw_name + " " + description)

    if is_station:
        ore_type = "STATION"
    elif len(detected_ores) > 1:
        print(f"\n  Detected multiple resources here: {', '.join(detected_ores)}")
        split_choice = input(f"  Split into {len(detected_ores)} separate GPS signals? (Y/n): ").strip().lower()

        if split_choice not in ("n", "no"):
            saved = []
            merged = []
            cluster = None  # resolved lazily, only if a new entry is actually needed

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

            input("\n  Press Enter to continue...")
            return

        # Declined the split — fall through as a single entry using one ore
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

    # If a same-ore marker already exists nearby, merge into it instead of
    # creating a near-duplicate (e.g. two people reporting the same vein).
    nearby = find_nearby_same_ore(data, ore_type, x, y, z)
    if nearby:
        dist = distance_3d(x, y, z, nearby["x"], nearby["y"], nearby["z"])
        print(f"\n  [i] Existing {ore_type} marker '{nearby['name']}' is {format_distance(dist)} away — merging instead of duplicating.")
        updated = merge_into_existing(conn, data, nearby, x, y, z, description)
        print(f"  [✓] Updated: {updated['name']} ({updated['ore_type']}) @ {updated['x']:.2f}, {updated['y']:.2f}, {updated['z']:.2f}  (reports: {updated['report_count']})")
        copy_to_clipboard(format_se_gps_string(updated))
        input("\n  Press Enter to continue...")
        return

    # Resolve the cluster this point belongs to
    cluster, name_has_cluster = resolve_cluster(conn, data, raw_name, x, y, z)

    # Work out a suggested name for the entry itself
    if name_has_cluster:
        # The name already carries the cluster reference — use it as-is
        suggested_name = raw_name
    else:
        # Cluster name goes in front of the ore, e.g. "X3C-395 FE"
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

    print(f"\n  [✓] Saved: {final_name} ({ore_type}) @ {x:.2f}, {y:.2f}, {z:.2f}")
    copy_to_clipboard(format_se_gps_string(entry))
    input("\n  Press Enter to continue...")


# ── Search GPS ──────────────────────────────────────────────────────

def search_gps(data: dict):
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
        return
    cx, cy, cz = current["x"], current["y"], current["z"]
    print(f"\n  Current position: {cx:.2f}, {cy:.2f}, {cz:.2f}")

    print("\nSearch by ore type (Fe, Ni, Co, Si, Mg, Ag, Au, Pt, U, Ice)")
    print("or by name/cluster (e.g., 'P3X', 'Velnor').")
    query = input("\nWhat are you looking for? ").strip()
    if not query:
        return

    # Determine if ore search or name search
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
                # Fall back to a word-boundary text match (e.g. an
                # un-split multi-resource entry that still mentions this
                # ore in its name/description) — never a bare substring,
                # so "u" (uranium) can't match inside "au" (gold).
                hay = f"{entry['name']} {entry.get('description', '')}".lower()
                matched = any(
                    re.search(r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', hay)
                    for alias in ore_aliases
                )
        else:
            # Name/cluster search
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
        return

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

    # Offer to copy nearest
    if matches:
        nearest = matches[0]["entry"]
        gps_str = format_se_gps_string(nearest)
        print("-" * 60)
        copy_choice = input("  Copy nearest GPS to clipboard? (y/n): ").strip().lower()
        if copy_choice in ("y", "yes"):
            copy_to_clipboard(gps_str)

    input("\n  Press Enter to continue...")


# ── List All ────────────────────────────────────────────────────────

def list_all(data: dict):
    if not data["entries"]:
        clear()
        print_header("ALL GPS ENTRIES")
        print("  Database is empty.")
        input("\n  Press Enter to continue...")
        return

    sort_mode = "ore"  # "ore" or "cluster"

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

        for key in sorted(groups.keys()):
            entries = groups[key]
            print(f"\n  [{key}] — {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
            for e in entries:
                confirmed = f"  ({e['report_count']}x confirmed)" if e.get("report_count", 1) > 1 else ""
                tag = f"  [{e['location_type']}]" if e.get("location_type") else ""
                if sort_mode == "ore":
                    print(f"    • {e['name']}: {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}{tag}{confirmed}")
                else:
                    print(f"    • {e['name']} ({e.get('ore_type', 'Unknown')}): {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}{tag}{confirmed}")

        print(f"\n  Total: {len(data['entries'])} entries, {len(data.get('clusters', []))} clusters")
        choice = input("\n  [t] Toggle sort (ore/cluster)   [Enter] Back: ").strip().lower()
        if choice == "t":
            sort_mode = "cluster" if sort_mode == "ore" else "ore"
            continue
        return


# ── Rename ──────────────────────────────────────────────────────────

def _rename_entry_work(conn, entry_id: int, new_name: str):
    with conn.cursor() as cur:
        cur.execute("UPDATE entries SET name=%s WHERE id=%s", (new_name, entry_id))
    conn.commit()


def rename_entry(data: dict):
    clear()
    print_header("RENAME GPS ENTRY")

    if not data["entries"]:
        print("  No GPS entries yet.")
        input("\n  Press Enter to continue...")
        return

    for i, e in enumerate(data["entries"], 1):
        print(f"  [{i:>3}] {e['name']}  ({e.get('ore_type', 'Unknown')})  @ {e['x']:.2f}, {e['y']:.2f}, {e['z']:.2f}")

    choice = input("\n  Entry number to rename (or Enter to cancel): ").strip()
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
        return

    old_name = entry["name"]
    new_name = input(f"  New name for '{old_name}': ").strip()
    if not new_name:
        print("  [!] Name cannot be empty. Cancelled.")
        input("\n  Press Enter to continue...")
        return

    try:
        run_db(_rename_entry_work, entry["id"], new_name)
    except DatabaseUnavailable as e:
        print(f"\n  [!] Could not save the rename: {e}")
        input("\n  Press Enter to continue...")
        return

    entry["name"] = new_name
    print(f"\n  [✓] Renamed '{old_name}' -> '{new_name}'")
    input("\n  Press Enter to continue...")


def _rename_cluster_work(conn, data: dict, cluster_id: int, new_name: str, cascade: bool):
    with conn.cursor() as cur:
        cur.execute("UPDATE clusters SET name=%s WHERE id=%s", (new_name, cluster_id))
    conn.commit()
    updated = 0
    if cascade:
        old_name = next(c["name"] for c in data["clusters"] if c["id"] == cluster_id)
        updated = rename_cluster_in_entries(conn, data, old_name, new_name)
    return updated


def rename_cluster(data: dict):
    clear()
    print_header("RENAME CLUSTER")

    clusters = data.get("clusters", [])
    if not clusters:
        print("  No clusters yet.")
        input("\n  Press Enter to continue...")
        return

    for i, c in enumerate(clusters, 1):
        print(f"  [{i:>3}] {c['name']}  ({len(c.get('entries', []))} point(s))")

    choice = input("\n  Cluster number to rename (or Enter to cancel): ").strip()
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
        return

    old_name = cluster["name"]
    new_name = input(f"  New name for '{old_name}': ").strip()
    if not new_name:
        print("  [!] Name cannot be empty. Cancelled.")
        input("\n  Press Enter to continue...")
        return

    also = input(f"  Also update GPS entries starting with '{old_name}'? (Y/n): ").strip().lower()
    cascade = also not in ("n", "no")

    try:
        updated = run_db(_rename_cluster_work, data, cluster["id"], new_name, cascade)
    except DatabaseUnavailable as e:
        print(f"\n  [!] Could not save the rename: {e}")
        input("\n  Press Enter to continue...")
        return

    cluster["name"] = new_name
    suffix = f", updated {updated} entr{'y' if updated == 1 else 'ies'}" if updated else ""
    print(f"\n  [✓] Renamed cluster '{old_name}' -> '{new_name}'{suffix}")
    input("\n  Press Enter to continue...")


def _remove_cluster_point(conn, cluster: dict, x: float, y: float, z: float) -> bool:
    """Delete one recorded point from a cluster (used when deleting an
    entry) and recalc/persist the center, or remove the cluster
    entirely if that was its last point. Returns True if the cluster
    itself was removed."""
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
        return False

    with conn.cursor() as cur:
        cur.execute("DELETE FROM clusters WHERE id=%s", (cluster["id"],))
    conn.commit()
    return True


def _delete_entry_work(conn, data: dict, entry: dict):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM entries WHERE id=%s", (entry["id"],))
    conn.commit()

    cluster = get_cluster_for_entry(data, entry)
    if cluster:
        cluster_removed = _remove_cluster_point(conn, cluster, entry["x"], entry["y"], entry["z"])
        if cluster_removed and cluster in data["clusters"]:
            data["clusters"].remove(cluster)

    if entry in data["entries"]:
        data["entries"].remove(entry)


def delete_entry(data: dict):
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

    choice = input("\n  Entry number to delete (or Enter to cancel): ").strip()
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
        return

    confirm = input(f"  Type 'yes' to permanently delete '{entry['name']}': ").strip().lower()
    if confirm != "yes":
        print("  Cancelled.")
        input("\n  Press Enter to continue...")
        return

    try:
        run_db(_delete_entry_work, data, entry)
    except DatabaseUnavailable as e:
        print(f"\n  [!] Could not delete: {e}")
        input("\n  Press Enter to continue...")
        return

    print(f"\n  [✓] Deleted '{entry['name']}'.")
    input("\n  Press Enter to continue...")


def edit_names(data: dict):
    while True:
        clear()
        print_header("EDIT / DELETE")
        print("  [1] Rename a GPS entry")
        print("  [2] Rename a cluster")
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


# ── Where Am I ──────────────────────────────────────────────────────

def where_am_i(data: dict):
    """Paste your current position and find out which cluster (if any)
    you're in or near, plus the nearest cluster if you're outside all
    of them."""
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
        return
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

    input("\n  Press Enter to continue...")


# ── Main Menu ───────────────────────────────────────────────────────

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


# ── Entry Point ─────────────────────────────────────────────────────

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
