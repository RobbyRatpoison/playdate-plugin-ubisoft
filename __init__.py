import logging
import os
import sys
import time

log = logging.getLogger(__name__)


def _find_native_launcher():
    """Return path to UbisoftConnect.exe on Windows, or None."""
    if sys.platform != 'win32':
        return None
    from runners.windows import find_installed_exe
    exe = find_installed_exe('Ubisoft Connect', 'UbisoftConnect.exe')
    if exe:
        return exe
    # Fallback: standard install-path guesses, in case the registry
    # DisplayName doesn't match (older installs may still say 'Uplay').
    candidates = []
    for env in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'):
        base = os.environ.get(env, '')
        if base:
            candidates.append(os.path.join(
                base, 'Ubisoft', 'Ubisoft Game Launcher', 'UbisoftConnect.exe'))
            candidates.append(os.path.join(
                base, 'Ubisoft Game Launcher', 'UbisoftConnect.exe'))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _find_wine_launcher_config():
    """Return (prefix, wine_bin, exe_path) for the Wine-installed Ubisoft
    Connect client, or (None, None, None) if any piece is missing. Shared by
    launcher_status() and start_launcher()."""
    import json
    from config import CONFIG_PATH
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    launcher_cfg = cfg.get('launchers', {}).get('ubisoft', {})
    prefix   = launcher_cfg.get('prefix', '').strip()
    wine_bin = launcher_cfg.get('wine_bin', '').strip()

    from runners.wine import find_wine_binary
    if not wine_bin:
        wine_bin = find_wine_binary()
    if not wine_bin or not prefix or not os.path.isdir(prefix):
        return None, None, None

    for dirpath, _dirs, files in os.walk(prefix):
        if 'UbisoftConnect.exe' in files:
            return prefix, wine_bin, os.path.join(dirpath, 'UbisoftConnect.exe')
    return None, None, None


# Marker path segment for an actual installed game's executable -- Ubisoft
# Connect installs every game under its own managed 'games' subfolder (see
# watcher._UBI_GAMES_SUBPATH). Checking for this is far more robust than an
# exclusion list of known launcher/Wine-infra process names, which broke on
# the very first cold start: unrelated pressure-vessel container helper
# processes (srt-bwrap, pv-adverb) also carry this prefix's WINEPREFIX env
# and aren't part of any such list, so they were misclassified as a running
# game and blocked launch_game() from ever starting anything.
_GAME_PATH_MARKER = '/ubisoft game launcher/games/'


def _game_is_running(prefix):
    """Confirmed live against a real running game (Child of Light):
    matches C:/.../Ubisoft Game Launcher/games/Child of Light Demo/ChildofLight.exe.
    Known gap: a game takes a few seconds to spawn its own exe after its
    uplay://launch/ deep link fires, so a click landing in that narrow window
    won't be caught -- accepted as a rare edge case rather than papered over
    with a fixed grace-period timeout."""
    from runners.wine import list_prefix_processes
    for _pid, argv0 in list_prefix_processes(prefix):
        if _GAME_PATH_MARKER in argv0.replace('\\', '/').lower():
            return True
    return False


def _send_uplay_url(url):
    """Dispatch a uplay:// deep link (install/launch/uninstall all funnel
    through this) on the current platform. Returns an error dict on
    failure, or None on success -- shared so launch_game() and
    uninstall_game() get identical Wine-session/already-running-game
    handling instead of duplicating it."""
    try:
        if sys.platform == 'win32':
            os.startfile(url)
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.Popen(['open', url])
        else:  # Linux — Wine
            import json
            from config import CONFIG_PATH
            try:
                with open(CONFIG_PATH, 'r') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            launcher_cfg = cfg.get('launchers', {}).get('ubisoft', {})
            prefix   = launcher_cfg.get('prefix', '').strip()
            wine_bin = launcher_cfg.get('wine_bin', '').strip() or None

            if not prefix:
                return {
                    'status':  'error',
                    'message': 'Ubisoft Connect not configured. Open Plugins → Manage to set up Wine.',
                }
            if _game_is_running(prefix):
                return {
                    'status':  'error',
                    'message': 'A Ubisoft game is currently running — close it before launching, '
                               'installing, or uninstalling a different game.',
                }
            from runners.wine import launch_protocol_url
            # Ubisoft Connect's Chromium UI won't accept a uplay:// deep link
            # into an already-running instance under Wine -- the session has to
            # be ended and cold-started (it re-forwards the link on startup).
            launch_protocol_url(prefix, url, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'},
                                restart_session_if_running=True)
    except RuntimeError as e:
        return {'status': 'error', 'message': str(e)}
    except Exception as e:
        return {'status': 'error', 'message': f'Launch failed: {e}'}
    return None


class UbisoftPlugin:
    id       = 'ubisoft'
    name     = 'Ubisoft Connect'
    platform = 'ubisoft'
    label    = 'Ubisoft Connect'

    date_import_url = 'https://www.ubisoft.com/en-us/account/orders'

    def register(self, app):
        from .routes import bp
        app.register_blueprint(bp)
        log.info('Ubisoft Connect plugin registered')

    def on_startup(self):
        from .watcher import sync_ubisoft_install_status, start_ubisoft_watcher, _get_wine_install_base
        try:
            sync_ubisoft_install_status()
            log.info('Ubisoft install status synced on startup')
        except Exception as e:
            log.warning(f'Startup Ubisoft install sync failed: {e}')
        if sys.platform != 'win32':
            install_base = _get_wine_install_base()
            if install_base:
                start_ubisoft_watcher(install_base)

    def resync_installed(self):
        from .watcher import sync_ubisoft_install_status
        sync_ubisoft_install_status()

    def on_shutdown(self):
        from .watcher import stop_ubisoft_watcher
        stop_ubisoft_watcher()

    def on_uninstall(self):
        from .ubisoft import clear_ubi_tokens
        clear_ubi_tokens()

    def launch_game(self, appid):
        from database import get_db, ts_to_date, update_game_data

        db  = get_db()
        row = db.execute(
            "SELECT platform_id, installed FROM games WHERE appid = ?", (appid,)
        ).fetchone()
        db.close()

        if not row:
            return {'status': 'error', 'message': 'Ubisoft game not found'}

        space_id = (row['platform_id'] or '').strip()
        if not space_id:
            return {'status': 'error', 'message': 'Game has no space ID — try re-syncing'}

        from .watcher import resolve_launch_id
        launch_id = resolve_launch_id(space_id)
        if not launch_id:
            log.warning(f'Ubisoft launch: no local launch id for spaceId {space_id}, '
                        f'falling back to spaceId (deep link will likely be ignored by the client)')
            launch_id = space_id

        if row['installed']:
            url = f'uplay://launch/{launch_id}/0'
        else:
            url = f'uplay://install/{launch_id}'

        error = _send_uplay_url(url)
        if error:
            return error

        if row['installed']:
            now_ts = int(time.time())
            update_game_data(appid, last_played=now_ts)
            return {'status': 'success', 'last_played': ts_to_date(now_ts)}

        return {'status': 'success'}

    def uninstall_game(self, appid):
        from database import get_db

        db  = get_db()
        row = db.execute(
            "SELECT platform_id FROM games WHERE appid = ?", (appid,)
        ).fetchone()
        db.close()

        if not row:
            return {'status': 'error', 'message': 'Ubisoft game not found'}

        space_id = (row['platform_id'] or '').strip()
        if not space_id:
            return {'status': 'error', 'message': 'Game has no space ID — try re-syncing'}

        from .watcher import resolve_launch_id
        launch_id = resolve_launch_id(space_id) or space_id

        error = _send_uplay_url(f'uplay://uninstall/{launch_id}')
        if error:
            return error
        return {'status': 'success'}

    def launcher_status(self):
        if sys.platform == 'win32':
            native = _find_native_launcher()
            if native:
                return {'available': True, 'detail': 'Launcher detected'}
            return {'available': False, 'detail': 'Ubisoft Connect not installed'}

        # Linux: require Wine prefix with UbisoftConnect.exe
        import json
        from config import CONFIG_PATH
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        launcher_cfg = cfg.get('launchers', {}).get('ubisoft', {})
        prefix   = launcher_cfg.get('prefix', '').strip()
        wine_bin = launcher_cfg.get('wine_bin', '').strip()

        from runners.wine import find_wine_binary
        if not wine_bin:
            wine_bin = find_wine_binary()

        if not wine_bin:
            return {'available': False, 'detail': 'No Wine binary found'}
        if not prefix:
            return {'available': False, 'detail': 'Wine prefix not configured'}
        if not os.path.isdir(prefix):
            return {'available': False, 'detail': f'Prefix not found: {prefix}'}

        for _dirpath, _dirs, files in os.walk(prefix):
            if 'UbisoftConnect.exe' in files:
                return {'available': True, 'detail': 'Launcher ready'}

        return {
            'available': False,
            'detail': 'UbisoftConnect.exe not found in prefix — install Ubisoft Connect in Wine',
        }

    def start_launcher(self):
        """Open the bare Ubisoft Connect client (no specific game) -- lets the
        user log in through the real native app, which isn't subject to the
        "third-party-error" block the web-based login flow hits."""
        if sys.platform == 'win32':
            native = _find_native_launcher()
            if not native:
                return {'status': 'error', 'message': 'Ubisoft Connect is not installed'}
            try:
                os.startfile(native)
            except Exception as e:
                return {'status': 'error', 'message': f'Launch failed: {e}'}
            return {'status': 'success'}

        if sys.platform == 'darwin':
            return {'status': 'error', 'message': 'Not supported on macOS'}

        prefix, wine_bin, exe_path = _find_wine_launcher_config()
        if not exe_path:
            return {'status': 'error', 'message': 'Ubisoft Connect not found. Check Wine setup in Plugins settings.'}
        try:
            from runners.wine import run_in_prefix
            run_in_prefix(prefix, exe_path, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'},
                          restart_session_if_running=True)
        except RuntimeError as e:
            return {'status': 'error', 'message': str(e)}
        except Exception as e:
            return {'status': 'error', 'message': f'Launch failed: {e}'}
        return {'status': 'success'}

    def js_api(self):
        native = sys.platform == 'win32'
        return {
            'uninstall_url':     '/api/ubisoft/uninstall/{appid}',
            'uninstall_confirm': 'Uninstall this game?\n\n'
                                  'This tells Ubisoft Connect to uninstall it -- the actual removal '
                                  'happens in the client, not immediately.',
            'scrape_url':        '/api/ubisoft/scrape-single/{appid}',
            'scrape_method':     'POST',
            # No locale prefix (goes to the user's own language automatically)
            # and 'games' is plural -- confirmed against a real working link;
            # the previous '/en-us/game/{slug}' (singular, locale-prefixed) 404s.
            'store_url':         'https://www.ubisoft.com/games/{slug}',
            'store_label':       'View on Ubisoft Store ↗',
            'appid_label':       'Ubisoft Space ID:',
            'sync_label':        'Sync Ubisoft Library',
        }

    def manage_ui(self):
        native = _find_native_launcher()
        if native:
            launcher_section = {
                'title': 'Launcher',
                'items': [
                    {'type': 'text', 'content': 'Ubisoft Connect is installed — no additional setup needed.'},
                    {'type': 'button', 'label': 'Start Launcher', 'action': {
                        'type': 'call', 'fn': 'ubisoftStartLauncher',
                    }},
                ],
            }
        elif sys.platform == 'win32':
            launcher_section = {
                'title': 'Launcher',
                'items': [
                    {'type': 'text', 'content': 'Ubisoft Connect is not installed.'},
                    {'type': 'button', 'label': 'Download Ubisoft Connect', 'action': {
                        'type': 'open_url', 'url': 'https://www.ubisoft.com/en-us/ubisoft-connect/download',
                    }},
                ],
            }
        else:
            items = [
                {'type': 'text', 'content': 'Set the Wine binary and prefix where Ubisoft Connect is installed.'},
                {'type': 'launcher_config'},
            ]
            _prefix, _wine_bin, _exe = _find_wine_launcher_config()
            if _exe:
                items.append({'type': 'text', 'content':
                              'Ubisoft Connect is installed. Use "Start Launcher" to open it directly and '
                              'sign in through the real app -- useful if the automated login above gets stuck.'})
                items.append({'type': 'button', 'label': 'Start Launcher', 'action': {
                    'type': 'call', 'fn': 'ubisoftStartLauncher',
                }})
                items.append({'type': 'button', 'label': 'Open Folder', 'action': {
                    'type': 'call', 'fn': 'ubisoftOpenFolder',
                }})
                items.append({'type': 'status_output', 'key': 'folder'})
                items.append({'type': 'text', 'content':
                              'Once signed in to the real app, its own local game cache can be read directly '
                              '-- no PlayDate account connection needed for this.'})
                items.append({'type': 'button', 'label': 'Sync Library from Launcher', 'action': {
                    'type': 'call', 'fn': 'ubisoftSyncLocal',
                }})
                items.append({'type': 'status_output', 'key': 'sync_local'})
            launcher_section = {'title': 'Launcher', 'items': items}

        return {
            'sections': [
                {
                    'title': 'Account',
                    'auth': {
                        'endpoint': '/api/ubisoft/status',
                        'disconnected': [
                            {'type': 'text', 'content':
                                'Signing in is optional and rarely needed -- library sync, install, and '
                                'launch all work without it, using the Ubisoft Connect app\'s own local data '
                                '(see the Launcher section below). Ubisoft\'s automated login is blocked by '
                                'their own bot detection and isn\'t offered here; connecting an account only '
                                'helps with rare edge cases (e.g. a game whose name can\'t be resolved locally).'},
                            {'type': 'button', 'label': 'Connect Ubisoft Account', 'action': {
                                'type': 'oauth_paste',
                                'title': 'Connect Ubisoft Connect',
                                'url_endpoint': '/api/ubisoft/auth-url',
                                'callback_endpoint': '/api/ubisoft/connect',
                                'instructions': [
                                    'Sign in to your Ubisoft account at '
                                    '<a href="https://connect.ubisoft.com" target="_blank">connect.ubisoft.com</a> '
                                    'in your regular desktop browser.',
                                    'Open that browser\'s DevTools (F12) → Application → Local Storage → '
                                    'connect.ubisoft.com, find <code>loginData</code>, copy its value, and '
                                    'paste it below.',
                                ],
                                'input_placeholder': 'Paste loginData JSON value here…',
                                'open_label': 'Open Ubisoft Sign In',
                                'submit_label': 'Connect',
                            }},
                        ],
                        'connected': [
                            {'type': 'connected_label'},
                            {'type': 'button', 'label': 'Sync Library', 'action': {'type': 'call', 'fn': 'ubisoftSync'}},
                            {'type': 'button', 'label': 'Disconnect', 'variant': 'muted', 'action': {
                                'type': 'post', 'endpoint': '/api/ubisoft/disconnect',
                                'on_success': 'refresh_auth',
                            }},
                            {'type': 'status_output', 'key': 'main'},
                        ],
                    },
                },
                launcher_section,
            ],
        }

    def fetch_description(self, appid, platform_id):
        from .ubisoft import fetch_description
        return fetch_description(appid, platform_id)

    def rescrape(self, appid):
        from .ubisoft import scrape_single
        return scrape_single(appid) or None

    art_kinds = ('vertical', 'horizontal')   # no icon art in the catalog

    def art_urls(self, appid):
        from .ubisoft import art_urls
        return art_urls(appid)

    def fragments(self):
        return {
            'base_head_styles': 'ubisoft_base_head_styles.html',
            'tools_scripts':    'ubisoft_tools_scripts.html',
        }


plugin = UbisoftPlugin()
