"""
ubisoft.py — Ubisoft Connect integration for PlayDate.
Handles authentication, library sync, metadata, and cover art.

Auth uses Ubisoft's SSO (not standard OAuth2 bearer tokens).
Session tickets use the Ubi_v1 scheme; rememberMeTickets are used for refresh.
"""

import json
import logging
import os
import threading
import time

import requests
from config import BASE_DIR, load_config, _save_config_data
from database import next_negative_appid
from images import save_as_jpg

log = logging.getLogger(__name__)

# ── Ubisoft API constants ─────────────────────────────────────────────────────
# Used for API calls (Ubi-AppId header on sessions/products/etc requests) --
# distinct from the login-specific identifiers below.
UBI_APP_ID     = '82b650c0-6cb3-40c0-9f41-25a53b62b206'
UBI_GENOME_ID  = '42d07c95-9914-4450-8b38-267c4e462b21'

# Login uses a *different* appId/genomeId pair than the one used for API
# calls above -- confirmed against a real reference client (galaxy-integration-
# uplay) that these are deliberately separate identifiers, not a typo. Our
# previous login URL reused UBI_APP_ID/UBI_GENOME_ID here, which don't match
# either of that reference's known-good pairs -- very likely why login was
# bouncing straight to third-party-error before the form even loaded: login
# validates the appId/genomeId pair up front and rejects anything it doesn't
# recognize. nextUrl also restored (a prior fix had removed it entirely after
# the old hardcoded '/ready' target started 404ing -- but Ubisoft's own login
# validation treats *missing* nextUrl as invalid too, not just a stale one;
# 'https://connect.ubisoft.com/' is the reference client's current value).
_UBI_LOGIN_APP_ID    = '1068ef52-dfd2-4e62-8ac9-37a47e6c0b78'
_UBI_LOGIN_GENOME_ID = '954e66a0-be1b-4aa0-9690-fb75201e4e9e'
_UBI_LOGIN_NEXT_URL  = 'https://connect.ubisoft.com/'
UBI_LOGIN_URL  = (
    'https://connect.ubisoft.com/login'
    f'?appId={_UBI_LOGIN_APP_ID}&genomeId={_UBI_LOGIN_GENOME_ID}&lang=en-US'
    f'&nextUrl={requests.utils.quote(_UBI_LOGIN_NEXT_URL, safe="")}'
)

_SESSIONS_URL  = 'https://public-ubiservices.ubi.com/v3/profiles/sessions'
_INVENTORY_URL = 'https://inventory.api.ubisoft.com/v1/inventories'
_SPACES_URL    = 'https://public-ubiservices.ubi.com/v1/spaces'
_PRODUCTS_URL  = 'https://consumer-product.ubisoft.com/api/product/products'

_UBI_HEADERS = {
    'Ubi-AppId':                 UBI_APP_ID,
    'Ubi-RequestedPlatformType': 'uplay',
    'Ubi-LocaleCode':            'en-US',
    'Content-Type':              'application/json',
    'User-Agent':                'UbiServices_SDK_2022.Release.9_PC64_ansi_static',
}

VERTICAL_DIR   = os.path.join(BASE_DIR, 'static', 'img', 'library', 'vertical')
HORIZONTAL_DIR = os.path.join(BASE_DIR, 'static', 'img', 'library', 'horizontal')


# ── Token storage ─────────────────────────────────────────────────────────────

def load_ubi_tokens():
    data = (load_config() or {}).get('ubisoft', {})
    if data.get('ticket') and data.get('remember_me_ticket'):
        return data
    return None


def is_connected():
    return load_ubi_tokens() is not None


def get_display_name():
    tokens = load_ubi_tokens()
    return tokens.get('username') if tokens else None


def clear_ubi_tokens():
    data = load_config() or {}
    data.pop('ubisoft', None)
    _save_config_data(data)


# ── Auth flow ─────────────────────────────────────────────────────────────────

def get_auth_url():
    return UBI_LOGIN_URL


def login_with_credentials(username, password):
    """
    Direct HTTP Basic Auth against Ubisoft's own sessions endpoint -- no
    browser popup needed. Bypasses the fragile embedded-webview login flow
    entirely (which was hitting a hard third-party-error bounce before the
    form even loaded).
    Returns (True, username) on success, (False, error_msg) on failure.
    Not verified against a 2FA-enabled account -- if Ubisoft challenges for
    a second factor, this will fail with whatever error the API returns;
    the popup/paste options remain available as a fallback for that case.
    """
    import base64
    if not username or not password:
        return False, 'Username and password are required'
    b64 = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
    try:
        data = _post_sessions(f'Basic {b64}')
    except ValueError as e:
        return False, f'Login failed: {e}'
    return _store_session(data)


def _post_sessions(auth_header, body=None):
    """
    POST to Ubisoft sessions endpoint. Returns raw response dict or raises ValueError.
    auth_header: full value of the Authorization header (e.g. 'Ubi_v1 t=...' or 'Basic ...')
    """
    headers = dict(_UBI_HEADERS)
    headers['Authorization'] = auth_header
    resp = requests.post(_SESSIONS_URL, headers=headers, json=body or {}, timeout=15)
    if resp.status_code not in (200, 201):
        raise ValueError(f'Ubisoft sessions failed (HTTP {resp.status_code}): {resp.text[:200]}')
    return resp.json()


def _find_ticket_dict(obj, depth=3):
    """
    Recursively search a parsed JSON value for the first dict containing a
    'ticket' key -- Ubisoft's own pages have been observed writing session
    data under several different shapes/wrapper keys (a direct object, a
    {"d": ..., "r": ...} localStorage envelope, and an {"opId", "payload":
    {...}} IPC-style wrapper), and pinning to one exact shape breaks the
    moment the page changes which key it uses. Dict values that are
    themselves JSON-encoded strings are parsed and searched too, since the
    {d, r} envelope's values are strings, not nested objects.
    """
    if depth <= 0:
        return None
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get('ticket'):
        return obj
    for value in obj.values():
        found = _find_ticket_dict(value, depth - 1)
        if found:
            return found
    return None


def exchange_code(raw_input):
    """
    Accept session data pasted or captured from Ubisoft's login page.
    The exact shape/wrapper key varies (see _find_ticket_dict); this just
    needs *a* dict with a 'ticket' key somewhere in the parsed structure.
    For the {d, r} envelope specifically, 'r' (rememberMe) is merged in
    separately since it's a sibling object, not nested under the ticket dict.
    Returns (True, username) on success, (False, error_msg) on failure.
    """
    raw = (raw_input or '').strip()
    log.info(f'Ubisoft exchange_code: received {len(raw)} chars: {raw[:300]!r}')
    try:
        data = json.loads(raw)
    except Exception as e:
        log.warning(f'Ubisoft exchange_code: failed to parse session data: {e}')
        return False, 'Could not parse session data from Ubisoft login'

    found = _find_ticket_dict(data)
    if not found:
        log.warning(f'Ubisoft exchange_code: no ticket found in parsed data (keys: '
                    f'{list(data.keys()) if isinstance(data, dict) else type(data)})')
        return False, 'localStorage data missing ticket — try logging in again'

    if isinstance(data, dict) and ('d' in data or 'r' in data) and 'r' in data:
        try:
            remember_me = json.loads(data.get('r') or '{}')
            found = {**found, **{k: v for k, v in remember_me.items() if k not in found}}
        except Exception:
            pass

    return _store_session(found)


def _store_session(data):
    """Persist session data from a /v3/profiles/sessions response."""
    from datetime import datetime as _dt
    ticket   = data.get('ticket', '')
    rmt      = data.get('rememberMeTicket', '')
    user_id  = data.get('userId', '')
    username = data.get('nameOnPlatform', '') or data.get('username', '')
    # expiration is an ISO date string; serverTime is the reference point
    try:
        _fmt = "%Y-%m-%dT%H:%M:%S.%f"
        exp  = _dt.strptime(data['expiration'][:23], _fmt)
        srv  = _dt.strptime(data['serverTime'][:23], _fmt)
        expires_in = max(300, int((exp - srv).total_seconds()))
    except Exception:
        expires_in = 10800
    expires_at = int(time.time()) + expires_in

    if not ticket:
        return False, 'No ticket in Ubisoft response'

    cfg = load_config() or {}
    existing = cfg.get('ubisoft', {})
    existing.update({
        'ticket':            ticket,
        'ticket_expires_at': expires_at,
        'user_id':           user_id,
        'username':          username,
    })
    if rmt:
        existing['remember_me_ticket'] = rmt
    cfg['ubisoft'] = existing
    _save_config_data(cfg)
    log.info(f'Ubisoft auth: connected as {username!r} (userId={user_id!r})')
    return True, username


# ── Session management ────────────────────────────────────────────────────────

def get_valid_session():
    """
    Return a requests.Session with a valid Ubisoft ticket, or None if not connected.
    Auto-refreshes using rememberMeTicket when the session ticket is near expiry.
    """
    cfg    = load_config() or {}
    tokens = cfg.get('ubisoft', {})
    if not tokens.get('ticket') or not tokens.get('remember_me_ticket'):
        return None

    if int(time.time()) >= tokens.get('ticket_expires_at', 0) - 60:
        rmt = tokens['remember_me_ticket']
        try:
            data = _post_sessions(f'Ubi_v1 t={rmt}')
        except ValueError as e:
            log.warning(f'Ubisoft token refresh failed: {e}')
            return None

        ok, result = _store_session(data)
        if not ok:
            return None

        cfg    = load_config() or {}
        tokens = cfg.get('ubisoft', {})

    session = requests.Session()
    session.headers.update(_UBI_HEADERS)
    session.headers['Authorization'] = f'Ubi_v1 t={tokens["ticket"]}'
    return session


# ── Library sync ──────────────────────────────────────────────────────────────

_sync_state = {
    'running': False, 'phase': None, 'done': 0, 'total': 0, 'current_game': '',
    'new_games': 0, 'total_games': 0, 'duplicates_detected': 0, 'error': None,
}
_sync_lock   = threading.Lock()
_sync_cancel = threading.Event()


def get_sync_state():
    with _sync_lock:
        return dict(_sync_state)


def start_library_sync():
    with _sync_lock:
        if _sync_state['running']:
            return {'status': 'already_running'}
        _sync_cancel.clear()
        _sync_state.update({
            'running': True, 'phase': 'fetching_library', 'done': 0, 'total': 0,
            'current_game': '', 'new_games': 0, 'total_games': 0,
            'duplicates_detected': 0, 'error': None,
        })
    threading.Thread(target=_run_sync_library, daemon=True).start()
    return {'status': 'started'}


def cancel_library_sync():
    _sync_cancel.set()


def _run_sync_library():
    try:
        _do_sync_library()
    except Exception as e:
        log.error(f'Ubisoft library sync thread: {e}', exc_info=True)
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error', 'error': str(e)})


def _do_sync_library():
    from database import get_db, update_game_data as _update_game_data
    from datetime import datetime, timezone, date as _date

    session = get_valid_session()
    if not session:
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error',
                                'error': 'Not connected to Ubisoft — please reconnect'})
        return

    try:
        resp = session.get(_INVENTORY_URL, params={'withOwnership': 'true'}, timeout=20)
        resp.raise_for_status()
        inventory = resp.json()
    except Exception as e:
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error',
                                'error': f'Failed to fetch Ubisoft library: {e}'})
        return

    # inventories is a list of {spaceId, productId, ownership: {...}, ...}
    items = inventory if isinstance(inventory, list) else inventory.get('inventoryItems', [])
    if not items:
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'done', 'new_games': 0, 'total_games': 0})
        return

    log.info(f'Ubisoft sync: {len(items)} inventory items')

    db = get_db()
    try:
        existing = {row['platform_id'] for row in db.execute(
            "SELECT platform_id FROM games WHERE platform = 'ubisoft'"
        ).fetchall()}
        blacklisted = {
            row[0] for row in db.execute(
                "SELECT platform_id FROM blacklist WHERE platform_id IS NOT NULL"
            ).fetchall()
        }
    finally:
        db.close()

    # Deduplicate by spaceId; skip games already in the DB
    space_ids = []
    seen = set()
    for item in items:
        space_id = str(item.get('spaceId') or item.get('productId') or '')
        if not space_id or space_id in seen:
            continue
        seen.add(space_id)
        if space_id not in existing and space_id not in blacklisted:
            space_ids.append((space_id, item))

    total_new = len(space_ids)
    log.info(f'Ubisoft sync: {total_new} new games (skipped {len(items) - total_new} existing)')

    with _sync_lock:
        _sync_state.update({'phase': 'processing', 'done': 0, 'total': total_new, 'current_game': ''})

    today_ts        = int(datetime.now(timezone.utc).timestamp())
    today           = _date.today().isoformat()
    new_games_count = 0

    db = get_db()
    try:
        for space_id, item in space_ids:
            if _sync_cancel.is_set():
                with _sync_lock:
                    _sync_state.update({'running': False, 'phase': 'stopped',
                                        'new_games': new_games_count, 'total_games': len(items)})
                return

            # Use cached product name if available; will be filled by metadata fetch
            name = item.get('name') or item.get('productName') or space_id

            with _sync_lock:
                _sync_state['current_game'] = name

            next_appid = next_negative_appid(db)
            db.execute(
                """INSERT OR IGNORE INTO games
                   (appid, name, platform, platform_id, date_added,
                    completion_status, installed,
                    art_fetched, meta_fetched, cheevos_fetched,
                    protondb_fetched, hltb_fetched)
                   VALUES (?, ?, 'ubisoft', ?, ?,
                           'Never Played', 0,
                           '0', '0', '0', '0', '0')""",
                (next_appid, name, space_id, today_ts),
            )
            db.commit()
            log.info(f'Ubisoft sync: added {name!r} as appid {next_appid} (spaceId {space_id})')

            # Fetch metadata and art
            meta = None
            try:
                meta = _fetch_product_metadata(session, space_id)
                if meta:
                    if meta.get('name'):
                        name = meta['name']
                        db.execute("UPDATE games SET name = ? WHERE appid = ?", (name, next_appid))
                        db.commit()
                    meta['meta_fetched'] = today
                    _update_game_data(next_appid, **{k: v for k, v in meta.items() if not k.startswith('_')})
            except Exception as e:
                log.warning(f'Ubisoft metadata: failed for {name!r}: {e}')

            try:
                images  = (meta or {}).get('_images')
                art_ok  = _download_art(session, next_appid, space_id, name=name, images=images)
                if art_ok:
                    _update_game_data(next_appid, art_fetched=today)
            except Exception as e:
                log.warning(f'Ubisoft art: failed for {name!r}: {e}')

            new_games_count += 1
            with _sync_lock:
                _sync_state['done'] += 1

            if _sync_cancel.is_set():
                with _sync_lock:
                    _sync_state.update({'running': False, 'phase': 'stopped',
                                        'new_games': new_games_count, 'total_games': len(items)})
                return

            time.sleep(0.4)
    finally:
        db.close()

    try:
        from .watcher import sync_ubisoft_install_status
        sync_ubisoft_install_status()
    except Exception as e:
        log.warning(f'Ubisoft sync: install status refresh failed: {e}')

    from database import auto_detect_duplicates
    from plugins import get_platform_priority
    dupes = auto_detect_duplicates(platform_priority=get_platform_priority())

    with _sync_lock:
        _sync_state.update({
            'running': False, 'phase': 'done',
            'new_games': new_games_count, 'total_games': len(items),
            'duplicates_detected': dupes,
        })


# ── Local (no-auth) library sync ────────────────────────────────────────────
# Reads Ubisoft Connect's own local game-catalog cache directly (see
# config_parser.py) -- works entirely from the installed, logged-in native
# launcher, with no PlayDate-side account login required at all. Uses the
# same _sync_state/_sync_lock/_sync_cancel as the API-based sync above since
# only one can usefully run at a time and the UI has one status area.

# Public, unauthenticated Ubisoft CDN pattern for the small thumbnail image
# embedded in the local cache (confirmed live: matches Lutris's own
# UbisoftCover.url_pattern, a plain string-substitution URL with no auth).
_THUMB_CDN_URL = 'https://static3.cdn.ubi.com/orbit/uplay_launcher_3_0/assets/%s'


def start_local_library_sync():
    with _sync_lock:
        if _sync_state['running']:
            return {'status': 'already_running'}
        _sync_cancel.clear()
        _sync_state.update({
            'running': True, 'phase': 'fetching_library', 'done': 0, 'total': 0,
            'current_game': '', 'new_games': 0, 'total_games': 0,
            'duplicates_detected': 0, 'error': None,
        })
    threading.Thread(target=_run_local_sync_library, daemon=True).start()
    return {'status': 'started'}


def _run_local_sync_library():
    try:
        _do_local_sync_library()
    except Exception as e:
        log.error(f'Ubisoft local library sync thread: {e}', exc_info=True)
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error', 'error': str(e)})


def _do_local_sync_library():
    from database import get_db, update_game_data as _update_game_data
    from datetime import datetime, timezone, date as _date
    from .watcher import _get_configurations_path
    from .config_parser import read_configurations

    config_path = _get_configurations_path()
    if not config_path or not os.path.isfile(config_path):
        with _sync_lock:
            _sync_state.update({'running': False, 'phase': 'error',
                                'error': 'Ubisoft Connect launcher not installed, or never signed in'})
        return

    from .config_parser import is_placeholder_name, slugify

    # Some legacy titles (pre-dating Ubisoft's spaceId/Connect API system --
    # e.g. Rayman Origins, older Rainbow Six/Splinter Cell/Ghost Recon/Prince
    # of Persia entries) have no space_id in the local cache at all, only the
    # classic numeric install_id/launch_id. Requiring space_id here silently
    # dropped all of them from the sync entirely. They have no API presence
    # (no metadata/art beyond the local thumb, can't be enriched below), but
    # the numeric id is exactly what uplay://launch/ needs, so they're worth
    # importing with that id standing in for platform_id.
    # Entries cross-linked to another storefront (Steam, and presumably Epic
    # etc -- see config_parser._parse_entry) aren't real Ubisoft installs;
    # Ubisoft Connect can't actually install/launch them itself (it tries to
    # hand off to the linked store's own client, which doesn't exist inside
    # this Wine prefix, so the Install button silently does nothing) and
    # they duplicate a library entry the user already owns elsewhere.
    entries = [e for e in read_configurations(config_path)
               if e['name'] and (e['space_id'] or e['launch_id'])
               and not e['third_party_platform']]
    log.info(f'Ubisoft local sync: {len(entries)} usable entries in local cache')

    # Ubisoft's own local cache occasionally has a broken placeholder name
    # ('l1', 'GAMENAME') with no usable fallback in the YAML itself --
    # confirmed live for two real owned games (Rainbow Six Siege, Trials
    # Fusion). If we're authenticated, the products API has the real name;
    # opportunistic and best-effort, never blocks the sync if it's not.
    # Only applies to entries with a real space_id -- there's no API record
    # to query for the legacy space_id-less ones above.
    session = get_valid_session()
    if session:
        for entry in entries:
            if entry['space_id'] and is_placeholder_name(entry['name']):
                meta = _fetch_product_metadata(session, entry['space_id'])
                if meta and meta.get('name'):
                    log.info(f"Ubisoft local sync: enriched name for {entry['space_id']}: "
                             f"{entry['name']!r} -> {meta['name']!r}")
                    entry['name'] = meta['name']

    db = get_db()
    try:
        existing = {row['platform_id'] for row in db.execute(
            "SELECT platform_id FROM games WHERE platform = 'ubisoft'"
        ).fetchall()}
        blacklisted = {row[0] for row in db.execute(
            "SELECT platform_id FROM blacklist WHERE platform_id IS NOT NULL"
        ).fetchall()}
    finally:
        db.close()

    new_entries = [e for e in entries
                   if (e['space_id'] or e['launch_id']) not in existing
                   and (e['space_id'] or e['launch_id']) not in blacklisted]
    total_new = len(new_entries)
    log.info(f'Ubisoft local sync: {total_new} new games (skipped {len(entries) - total_new} existing)')

    with _sync_lock:
        _sync_state.update({'phase': 'processing', 'done': 0, 'total': total_new})

    today_ts        = int(datetime.now(timezone.utc).timestamp())
    today           = _date.today().isoformat()
    new_games_count = 0

    db = get_db()
    try:
        for entry in new_entries:
            if _sync_cancel.is_set():
                with _sync_lock:
                    _sync_state.update({'running': False, 'phase': 'stopped',
                                        'new_games': new_games_count, 'total_games': len(entries)})
                return

            platform_id = entry['space_id'] or entry['launch_id']
            name        = entry['name']
            slug        = slugify(name)

            with _sync_lock:
                _sync_state['current_game'] = name

            next_appid = next_negative_appid(db)
            db.execute(
                """INSERT OR IGNORE INTO games
                   (appid, name, platform, platform_id, platform_slug, date_added,
                    completion_status, installed,
                    art_fetched, meta_fetched, cheevos_fetched,
                    protondb_fetched, hltb_fetched)
                   VALUES (?, ?, 'ubisoft', ?, ?, ?,
                           'Never Played', 0,
                           '0', '0', '0', '0', '0')""",
                (next_appid, name, platform_id, slug, today_ts),
            )
            db.commit()
            log.info(f'Ubisoft local sync: added {name!r} as appid {next_appid} '
                     f'({"spaceId" if entry["space_id"] else "legacy launch_id"} {platform_id})')

            try:
                thumb_url = _THUMB_CDN_URL % entry['thumb_image'] if entry['thumb_image'] else None
                art_ok = _download_art(None, next_appid, entry['space_id'], name=name,
                                        images=([{'type': 'portrait', 'url': thumb_url}] if thumb_url else []))
                if art_ok:
                    _update_game_data(next_appid, art_fetched=today)
            except Exception as e:
                log.warning(f'Ubisoft local sync art: failed for {name!r}: {e}')

            new_games_count += 1
            with _sync_lock:
                _sync_state['done'] += 1

            if _sync_cancel.is_set():
                with _sync_lock:
                    _sync_state.update({'running': False, 'phase': 'stopped',
                                        'new_games': new_games_count, 'total_games': len(entries)})
                return
    finally:
        db.close()

    try:
        from .watcher import sync_ubisoft_install_status
        sync_ubisoft_install_status()
    except Exception as e:
        log.warning(f'Ubisoft local sync: install status refresh failed: {e}')

    from database import auto_detect_duplicates
    from plugins import get_platform_priority
    dupes = auto_detect_duplicates(platform_priority=get_platform_priority())

    with _sync_lock:
        _sync_state.update({
            'running': False, 'phase': 'done',
            'new_games': new_games_count, 'total_games': len(entries),
            'duplicates_detected': dupes,
        })


# ── Metadata ──────────────────────────────────────────────────────────────────

def _fetch_product_metadata(session, space_id):
    """
    Fetch product details for a spaceId. Returns a partial meta dict or None.
    Uses Ubisoft's product catalog API.
    """
    try:
        # Products endpoint: list of spaceIds as query params
        resp = session.get(
            _PRODUCTS_URL,
            params={'spaceIds': space_id, 'locale': 'en-US'},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f'Ubisoft products HTTP {resp.status_code} for spaceId {space_id}')
            return None

        products = resp.json()
        if not products:
            return None

        # Response is a list of product objects
        product = products[0] if isinstance(products, list) else products

        meta = {}

        name = (product.get('name') or product.get('localizedName') or '').strip()
        if name:
            meta['name'] = name

        desc = (product.get('description') or product.get('localizedDescription') or '').strip()
        if desc:
            meta['_description'] = desc

        publisher = (product.get('publisherName') or '').strip()
        if publisher:
            meta['publishers'] = publisher

        developer = (product.get('developerName') or '').strip()
        if developer:
            meta['developers'] = developer

        genres = product.get('genres') or []
        if isinstance(genres, list) and genres:
            meta['genres'] = ','.join(g.get('name', g) if isinstance(g, dict) else g for g in genres)

        # Release date
        release_str = product.get('releaseDate') or product.get('releaseDateStr') or ''
        if release_str:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(release_str.replace('Z', '+00:00'))
                meta['release_date'] = int(dt.timestamp())
            except Exception:
                pass

        # Store slug
        slug = (product.get('slug') or product.get('urlSlug') or '').strip()
        if slug:
            meta['platform_slug'] = slug

        # Stash raw image list for the art downloader (not persisted to DB)
        images = product.get('images') or product.get('keyImages') or []
        if images:
            meta['_images'] = images

        return meta or None

    except Exception as e:
        log.warning(f'Ubisoft product metadata failed for {space_id}: {e}')
        return None


def _download_art(session, appid, space_id, name='', images=None):
    """
    Download cover art for a Ubisoft game.

    Tries Ubisoft CDN URLs extracted from the product catalog first, then falls
    back to SteamGridDB when images are missing or CDN downloads fail.
    Pass pre-fetched `images` from _fetch_product_metadata to avoid a second API call.
    Returns True if at least one image was saved.
    """
    vert_path  = os.path.join(VERTICAL_DIR, f'{appid}.jpg')
    horiz_path = os.path.join(HORIZONTAL_DIR, f'{appid}.jpg')

    if os.path.exists(vert_path) and os.path.exists(horiz_path):
        return True

    # Fetch image list from products API if not pre-supplied
    if images is None:
        try:
            resp = session.get(
                _PRODUCTS_URL,
                params={'spaceIds': space_id, 'locale': 'en-US'},
                timeout=15,
            )
            if resp.status_code == 200:
                products = resp.json()
                product  = products[0] if isinstance(products, list) else products
                images   = product.get('images') or product.get('keyImages') or []
        except Exception as e:
            log.warning(f'Ubisoft: failed to fetch product images for {space_id}: {e}')
        images = images or []

    vertical_url   = None
    horizontal_url = None
    for img in images:
        img_type = (img.get('type') or img.get('format') or '').lower()
        url      = img.get('url') or img.get('uri') or ''
        if not url:
            continue
        if any(k in img_type for k in ('portrait', 'vertical', 'packshot', 'cover', 'keyart_portrait')):
            vertical_url = vertical_url or url
        if any(k in img_type for k in ('landscape', 'horizontal', 'header', 'banner', 'background')):
            horizontal_url = horizontal_url or url

    for url, dest in [(vertical_url, vert_path), (horizontal_url, horiz_path)]:
        if url and not os.path.exists(dest):
            try:
                r = requests.get(url, timeout=20)
                if r.status_code == 200 and len(r.content) > 1000:
                    save_as_jpg(r.content, dest)
            except Exception as e:
                log.warning(f'Ubisoft CDN art download failed for {space_id}: {e}')

    # Fall back to SGDB for any image still missing
    if (not os.path.exists(vert_path) or not os.path.exists(horiz_path)) and name:
        try:
            from images import _sgdb_search_game_id, download_vertical, download_horizontal
            sgdb_id = _sgdb_search_game_id(name)
            if not os.path.exists(vert_path):
                download_vertical(appid, sgdb_id=sgdb_id, game_name=name)
            if not os.path.exists(horiz_path):
                download_horizontal(appid, sgdb_id=sgdb_id, game_name=name)
        except Exception as e:
            log.warning(f'Ubisoft SGDB art fallback failed for {name!r}: {e}')

    return os.path.exists(vert_path) or os.path.exists(horiz_path)


# ── Single-game rescrape ──────────────────────────────────────────────────────

def scrape_single(appid):
    """
    Re-fetch metadata and art for one Ubisoft game.
    Returns a dict of updated fields, or None on failure.

    Prefers the authenticated products API (full metadata) when a valid
    session exists; falls back to the client's own local cache (name +
    thumbnail art only, via config_parser) otherwise, so the per-game Sync
    button and bulk rescrape both still do *something* useful without a
    successful Ubisoft login -- which is presently blocked by DataDome bot
    detection on the popup login flow (see project_ubisoft_datadome_blocked
    memory). Returns None only if neither source has anything for this game.
    """
    from database import get_db
    db  = get_db()
    row = db.execute(
        "SELECT platform_id, name FROM games WHERE appid = ? AND platform = 'ubisoft'",
        (appid,)
    ).fetchone()
    db.close()

    if not row or not row['platform_id']:
        return None

    space_id  = row['platform_id']
    game_name = row['name'] or ''

    session = get_valid_session()
    if session:
        meta   = _fetch_product_metadata(session, space_id) or {}
        images = meta.pop('_images', None)
        name   = meta.get('name') or game_name
    else:
        from .config_parser import is_placeholder_name
        from .watcher import find_local_entry
        meta   = {}
        images = None
        name   = game_name
        entry  = find_local_entry(space_id)
        if not entry:
            return None
        if entry['name'] and not is_placeholder_name(entry['name']):
            name = entry['name']
            meta['name'] = name
        if entry['thumb_image']:
            images = [{'type': 'portrait', 'url': _THUMB_CDN_URL % entry['thumb_image']}]
        if not meta and not images:
            return None

    from datetime import date
    meta['meta_fetched'] = date.today().isoformat()

    vert_path  = os.path.join(VERTICAL_DIR, f'{appid}.jpg')
    horiz_path = os.path.join(HORIZONTAL_DIR, f'{appid}.jpg')
    if not os.path.exists(vert_path) or not os.path.exists(horiz_path):
        try:
            art_ok = _download_art(session, appid, space_id, name=name, images=images)
            if art_ok:
                meta['art_fetched'] = meta['meta_fetched']
        except Exception as e:
            log.warning(f'Ubisoft scrape-single: art re-fetch failed for appid {appid}: {e}')

    return meta


def fetch_description(appid, platform_id):
    """Return a plain-text description for a Ubisoft game, or None."""
    session = get_valid_session()
    if not session:
        return None
    meta = _fetch_product_metadata(session, platform_id)
    return (meta or {}).get('_description') or None
