# Space Engineers GPS Navigator

A terminal tool for sharing Space Engineers GPS waypoints with your group through
one live MySQL database — add a GPS, it's instantly searchable by everyone else
pointed at the same database.

Current version: **2.4.3** (run `python3 se_gps_navigator.py --version`)

## Features

- **Shared, live database** — everyone reads/writes the same MySQL instance, no
  passing GPS strings around in Discord.
- **Paste-to-add** — paste a Space Engineers `GPS:...` string directly, or enter
  coordinates manually.
- **Uploader attribution** — every uploaded marker records its MySQL login as
  an `Uploaded by:` note visible in the navigator. The note is not included in
  copied GPS strings, preserving vanilla Space Engineers compatibility.
- **Mass paste / batch add** — from the Add GPS screen, type `multi` to paste a
  whole block of `GPS:...` strings at once (one per line, blank line to finish).
  Each one is added automatically — ore detection, multi-resource splitting, and
  duplicate merging all happen with no per-line prompts, so you can dump a big
  batch in one go.
- **Automatic ore detection** — recognizes Fe, Ni, Co, Si, Mg, Ag, Au, Pt, U,
  Ice, Stone from the GPS name/description.
- **Multi-resource splitting** — a deposit named e.g. `"Iron/Silicon/Gold"` is
  offered as three separate GPS signals instead of one ambiguous one.
- **Duplicate merging** — a new report of the same ore within **1.5 km** of an
  existing marker is merged into it instead of creating a near-duplicate. The
  position becomes a running weighted average and a `report_count` tracks how
  many times it's been confirmed.
- **Automatic clustering** — nearby markers (within 100 km) are grouped into a
  named cluster (Stargate-style names, e.g. `P3X-263`).
- **Proximity-aware naming** — clusters within **2000 km** of each other share
  the same `[Letter][Digit][Letter]` prefix and only differ in the trailing
  digits (e.g. `P3X-100` and `P3X-839`), so two names alone hint at whether the
  sites are near each other. A cluster far away from everything else gets a
  fresh random prefix.
- **Cluster GPS markers** — every cluster automatically gets its own GPS entry
  at its center point (named after the cluster, tagged `CLUSTER`), so you can
  search for or list the cluster itself like any other marker. It moves
  automatically as the cluster's center shifts.
- **Location tags** — mark a GPS as an Asteroid, Planet, or Station when adding
  it (stations skip the ore-detection prompts entirely, since they have no
  resource).
- **Search by ore or by name/cluster**, sorted by distance from a pasted
  current-position GPS string. Search loops back to "search again?" instead of
  dropping you back to the main menu.
- **"Where am I"** — paste your current position and find out which cluster
  you're in (or the nearest one, if you're not inside any), with an option to
  copy that cluster's own GPS marker straight to your clipboard.
- **List all entries** — numbered top-to-bottom in whatever sort order is
  active (by ore or by cluster); type a number to copy that entry's GPS
  straight to clipboard.
- **Rename / delete** entries and clusters after the fact. Renaming a cluster
  offers an auto **re-render** option (`r`) that regenerates its name using the
  current proximity-aware naming convention instead of typing one by hand.
- **Continuation prompts everywhere** — adding, searching, renaming, and
  deleting all ask "do another?" when you finish, instead of kicking you back
  to the main menu each time.
- **Resilient to DB hiccups** — automatically retries with backoff if the
  database doesn't respond right away or drops mid-operation, instead of
  crashing.
- **Self-updating** — can check a remote manifest for a newer version and pull
  it down from inside the app.

## Requirements

- Python 3.10+
- [`pymysql`](https://pypi.org/project/PyMySQL/): `pip install pymysql`
- Optional: [`pyperclip`](https://pypi.org/project/pyperclip/) for clipboard
  copy/paste support (`pip install pyperclip`)
- A MySQL (or MariaDB) server reachable from every player's machine

## Setup

### 1. Create the database and a user

On your MySQL server:

```sql
CREATE DATABASE se_gps_navigator;
CREATE USER 'se_gps'@'%' IDENTIFIED BY 'a-real-password-here';
GRANT ALL PRIVILEGES ON se_gps_navigator.* TO 'se_gps'@'%';
FLUSH PRIVILEGES;
```

The app creates its own tables on first run — no schema file to import. If
you're upgrading from an older version, the app also auto-migrates existing
tables (adds any missing columns, like the new cluster-marker link) the first
time it starts — no manual `ALTER TABLE` needed.

### 2. Run it

```bash
python3 se_gps_navigator.py
```

The **first time** it runs (or if the saved config still has the placeholder
password), it will interactively ask for the host, port, username, password,
and database name, and save them to `~/.se_gps_navigator/db_config.json`
(permissions locked to `600` — owner read/write only). Every player runs this
once, pointed at the same MySQL server.

You can skip the prompt entirely by setting environment variables instead
(useful for scripted/unattended setups):

```
SE_GPS_DB_HOST=203.0.113.10
SE_GPS_DB_PORT=3306
SE_GPS_DB_USER=se_gps
SE_GPS_DB_PASSWORD=a-real-password-here
SE_GPS_DB_NAME=se_gps_navigator
```

## Adding GPS entries

From the main menu, **[2] Add new GPS** offers two paths:

- **Single entry**: paste one `GPS:...` string (or leave blank for manual
  X/Y/Z entry), pick a location type, confirm the detected ore, and it's
  saved. Its `Uploaded by:` note uses the configured MySQL username and is
  visible in the navigator only. After saving, choose whether to add another
  GPS without an extra continuation prompt; the prior result remains visible.
- **Batch / mass paste**: type `multi` at the prompt, then paste as many
  `GPS:...` strings as you want, one per line. A blank line ends the list and
  everything gets added automatically (no per-entry prompts) — good for
  dumping a big haul at once. The configured MySQL username is recorded on
  every marker in the batch. Lines that aren't parseable are skipped and
  listed at the end so nothing silently vanishes.

## Users & roles

The app doesn't implement its own permission system — MySQL already has one,
and it's a better fit than reinventing it in the app. Create different MySQL
accounts per role and give each group of players the matching one:

```sql
-- Read-only: can search and list, cannot add/rename/delete
CREATE USER 'se_gps_readonly'@'%' IDENTIFIED BY 'pw1';
GRANT SELECT ON se_gps_navigator.* TO 'se_gps_readonly'@'%';

-- Standard member: can add GPS entries and read everything
CREATE USER 'se_gps_member'@'%' IDENTIFIED BY 'pw2';
GRANT SELECT, INSERT, UPDATE ON se_gps_navigator.* TO 'se_gps_member'@'%';

-- Admin: full control, including deletes
CREATE USER 'se_gps_admin'@'%' IDENTIFIED BY 'pw3';
GRANT ALL PRIVILEGES ON se_gps_navigator.* TO 'se_gps_admin'@'%';

FLUSH PRIVILEGES;
```

Each player configures the app with the account they were given. If someone
with a read-only account tries to add/rename/delete, the app recognizes the
MySQL permission error and shows a clear message instead of retrying uselessly
or crashing.

## Auto-update

The app can check a small JSON manifest for a newer version:

```json
{ "version": "2.4.3", "url": "https://raw.githubusercontent.com/<you>/<repo>/main/se_gps_navigator.py" }
```

Host that manifest wherever's convenient (a raw file in this repo works fine)
and point the app at it:

```
SE_GPS_UPDATE_URL=https://raw.githubusercontent.com/<you>/<repo>/main/manifest.json
```

If a newer version is found, the main menu shows an `[!] Update available`
notice and an extra menu option to download and install it in place (the
current file is backed up as `se_gps_navigator.py.bak` first). You'll need to
restart the app after updating. Leave `SE_GPS_UPDATE_URL` unset to disable
update checking entirely — it's opt-in.

## How clustering, naming & merging work

- **Clustering**: any two markers within 100 km are grouped into the same
  named cluster. New entries automatically join a cluster whose name is
  mentioned in the GPS name (e.g. typing `"DC-32 Gold"` when a `DC-32` cluster
  exists), or the nearest cluster within range, or start a brand-new one.
- **Proximity-aware naming**: when a brand-new cluster is created, the app
  looks for any other cluster within 2000 km. If one exists, the new cluster
  reuses that cluster's `[Letter][Digit][Letter]` prefix and only rolls a new
  3-digit suffix (e.g. `P3X-100` and a new site nearby becomes `P3X-472`). If
  nothing is within range, it gets a completely fresh random prefix. You can
  also re-roll an existing cluster's name at any time from **Edit/Delete →
  Rename a cluster**, by typing `r` instead of a new name.
- **Cluster markers**: every cluster keeps its own GPS entry at its exact
  center point, automatically created when the cluster is and automatically
  repositioned whenever the center shifts (new points, merges, deletions). It
  shows up in searches, listings, and "Where am I" like any other entry, so
  you always have a quick way to GPS straight to a site's middle.
- **Duplicate merging**: within a cluster, two reports of the *same ore type*
  within 1.5 km of each other are treated as the same physical deposit and
  merged into a single entry rather than kept as two markers.

## Database resilience

- If the database doesn't respond when connecting, the app retries a few
  times with increasing backoff before giving up with a clear error.
- If the connection drops mid-operation (e.g. the server restarts while a
  rename/delete/add is in flight), simple single-step operations (renames,
  deletes) automatically retry the whole step with a fresh connection — no
  re-entering data. The interactive "Add GPS" flow does the same for the
  initial connection, but if the drop happens *while* you're mid-wizard, it
  will tell you plainly and suggest checking "List all entries" before
  retrying, rather than silently guessing what already saved. The same
  applies to a batch/mass-paste run: anything already saved before a drop is
  called out explicitly.
- Permission errors (wrong role/account) are never retried — they fail fast
  with a clear message.

## Known limitations

- Multi-step add operations (e.g. a multi-resource split, or a batch/mass
  paste) aren't a single atomic database transaction. If the connection drops
  partway through, some of the entries may have already saved. This is called
  out explicitly when it happens rather than silently retried, since
  automatically redoing it could create duplicates.
- The auto-updater replaces the script file in place; it does not restart the
  running process automatically.

## License

MIT — do what you want with it.
