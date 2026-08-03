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
UBI_APP_ID     = '82b650c0-6cb3-40c0-9f41-25a53b62b206'
UBI_GENOME_ID  = '42d07c95-9914-4450-8b38-267c4e462b21'
# nextUrl used to be connect.ubisoft.com/ready, which 404s as of 2026-08 --
# Ubisoft evidently retired that landing page. Login itself validates nextUrl
# before showing the form and bounces to third-party-error immediately if the
# target isn't recognized, so a stale value breaks login outright, before the
# user even gets a chance to enter credentials. Omitted entirely now so
# Ubisoft redirects to whatever its own current default is instead of a
# hardcoded guess -- code_js's landing-page detection was broadened to match,
# since we no longer know the exact path in advance.
UBI_LOGIN_URL  = (
    'https://connect.ubisoft.com/login'
    f'?appId={UBI_APP_ID}&genomeId={UBI_GENOME_ID}&lang=en-US'
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


def exchange_code(raw_input):
    """
    Accept session data from the popup's code_js.
    Handles two formats:
      1. Direct sessions API response: {"ticket": "...", "rememberMeTicket": "...", ...}
      2. Legacy localStorage envelope: {"d": <loginData JSON>, "r": <rememberMe JSON>}
    Returns (True, username) on success, (False, error_msg) on failure.
    """
    raw = (raw_input or '').strip()
    log.info(f'Ubisoft exchange_code: received {len(raw)} chars: {raw[:300]!r}')
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # Direct sessions API response (from XHR code_js)
            if data.get('ticket'):
                return _store_session(data)
            # Legacy localStorage envelope {d, r}
            if 'd' in data or 'r' in data:
                login_data  = json.loads(data.get('d') or '{}')
                remember_me = json.loads(data.get('r') or '{}')
                combined    = {**login_data, **remember_me}
                if combined.get('ticket'):
                    return _store_session(combined)
                return False, 'localStorage data missing ticket — try logging in again'
    except Exception as e:
        log.warning(f'Ubisoft exchange_code: failed to parse session data: {e}')
    return False, 'Could not parse session data from Ubisoft login'


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
    """
    session = get_valid_session()
    if not session:
        return None

    from database import get_db
    db  = get_db()
    row = db.execute(
        "SELECT platform_id, name FROM games WHERE appid = ? AND platform = 'ubisoft'",
        (appid,)
    ).fetchone()
    db.close()

    if not row or not row['platform_id']:
        return None

    space_id = row['platform_id']
    game_name = row['name'] or ''
    meta = _fetch_product_metadata(session, space_id) or {}
    images = meta.pop('_images', None)
    name = meta.get('name') or game_name

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
