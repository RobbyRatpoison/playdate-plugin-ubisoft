"""
watcher.py — Install status watcher for Ubisoft Connect games.

Uses the same local 'configurations' cache config_parser.py reads for the
no-auth library sync: each entry's registry_key resolves (via the Wine
prefix's own registry) to a real install directory when that game is
actually installed. This replaced an earlier version that guessed at a
'configurations/<spaceId>.json' directory layout under ProgramData -- that
guess never matched the real file (a single binary cache under
AppData/Local, not a directory of per-game JSON files under ProgramData).
"""

import json
import logging
import os
import sys

from runners.watcher import PluginInstallWatcher
from runners.wine import wine_user_dir

log = logging.getLogger(__name__)

_UBI_CONFIG_SUBPATH = os.path.join(
    'AppData', 'Local', 'Ubisoft Game Launcher', 'cache', 'configuration', 'configurations'
)
_UBI_GAMES_SUBPATH = os.path.join(
    'drive_c', 'Program Files (x86)', 'Ubisoft', 'Ubisoft Game Launcher', 'games'
)


def _get_wine_prefix():
    try:
        from config import CONFIG_PATH
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        return cfg.get('launchers', {}).get('ubisoft', {}).get('prefix', '').strip() or None
    except Exception:
        return None


def _get_configurations_path():
    if sys.platform == 'win32':
        local_appdata = os.environ.get('LOCALAPPDATA')
        return os.path.join(local_appdata, 'Ubisoft Game Launcher', 'cache',
                             'configuration', 'configurations') if local_appdata else None
    prefix = _get_wine_prefix()
    if not prefix:
        return None
    user_dir = wine_user_dir(prefix)
    return os.path.join(user_dir, _UBI_CONFIG_SUBPATH) if user_dir else None


def find_local_entry(space_id):
    """Return the client's own local cache entry for this spaceId (dict with
    install_id, launch_id, space_id, name, thumb_image, exe_relative,
    registry_key -- see config_parser.parse_configurations), or None if this
    game has no entry yet in that cache."""
    from .config_parser import read_configurations
    config_path = _get_configurations_path()
    if not config_path or not os.path.isfile(config_path):
        return None
    for entry in read_configurations(config_path):
        if entry['space_id'] == space_id:
            return entry
    return None


def resolve_launch_id(space_id):
    """Map a spaceId (GUID, used for API calls and stored as platform_id) to
    the numeric id the uplay:// protocol handler actually expects for
    uplay://launch/<id>/0 and uplay://install/<id> -- confirmed live that
    passing the spaceId GUID directly is silently ignored by the client
    (it opens but never acts on the deep link). Returns None if this game
    has no entry yet in the client's own local cache."""
    entry = find_local_entry(space_id)
    return entry['launch_id'] if entry and entry['launch_id'] else None


def _get_wine_install_base():
    """Real Ubisoft games install directory -- receives filesystem events
    when a game installs/uninstalls, unlike the old ProgramData guess."""
    prefix = _get_wine_prefix()
    return os.path.join(prefix, _UBI_GAMES_SUBPATH) if prefix else None


def sync_ubisoft_install_status():
    """Update installed flags for all Ubisoft games by checking whether
    each one's registry_key (from the local configurations cache) resolves
    to a real install directory in the Wine prefix."""
    from database import get_db
    from .config_parser import read_configurations, resolve_install_dir, invalidate_registry_cache

    prefix = _get_wine_prefix()
    config_path = _get_configurations_path()
    if not prefix or not config_path or not os.path.isfile(config_path):
        return

    invalidate_registry_cache(prefix)
    entries = read_configurations(config_path)
    # Keyed by space_id when the entry has one, else its launch_id -- matches
    # how platform_id is stored for legacy space_id-less games (see
    # ubisoft._do_local_sync_library). Without this fallback, those games
    # could never be detected as installed and would always get the
    # `install` deep link instead of `launch`, even once actually installed.
    installed_ids = {
        (e['space_id'] or e['launch_id']) for e in entries
        if (e['space_id'] or e['launch_id']) and e['registry_key']
        and resolve_install_dir(prefix, e['registry_key'])
    }

    db = get_db()
    try:
        rows = db.execute(
            "SELECT appid, platform_id, installed FROM games WHERE platform = 'ubisoft'"
        ).fetchall()
        for row in rows:
            new_flag = 1 if row['platform_id'] in installed_ids else 0
            if new_flag != row['installed']:
                db.execute("UPDATE games SET installed = ? WHERE appid = ?", (new_flag, row['appid']))
        db.commit()
    finally:
        db.close()


_watcher = PluginInstallWatcher('ubisoft', sync_ubisoft_install_status)


def start_ubisoft_watcher(watch_path: str):
    _watcher.start(watch_path)


def stop_ubisoft_watcher():
    _watcher.stop()
