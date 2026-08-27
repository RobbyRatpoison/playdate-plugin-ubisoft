import logging

from flask import Blueprint, request, jsonify
from database import get_db, update_game_data

log = logging.getLogger(__name__)

bp = Blueprint('ubisoft', __name__, url_prefix='/api/ubisoft',
               template_folder='templates')


@bp.route('/status')
def ubi_status():
    from .ubisoft import is_connected, get_display_name
    if not is_connected():
        return jsonify({'connected': False, 'username': None})
    return jsonify({'connected': True, 'username': get_display_name()})


@bp.route('/auth-url')
def ubi_auth_url():
    from .ubisoft import get_auth_url
    return jsonify({'url': get_auth_url()})


@bp.route('/connect', methods=['POST'])
def ubi_connect():
    import json as _json
    from .ubisoft import exchange_code
    raw = ((request.json or {}).get('code') or '').strip()
    if not raw:
        return jsonify({'status': 'error', 'message': 'No session data received'}), 400
    # Diagnostic payload from code_js when no ticket was found.
    try:
        diag = _json.loads(raw)
        if isinstance(diag, dict) and diag.get('_pd_diag'):
            log.info(f'Ubisoft auth diagnostic: href={diag.get("href")!r} ck={diag.get("ck")!r}')
            ls = diag.get('ls') or {}
            ss = diag.get('ss') or {}
            for k, v in ls.items():
                log.info(f'  localStorage[{k!r}] = {v!r}')
            for k, v in ss.items():
                log.info(f'  sessionStorage[{k!r}] = {v!r}')
            if not ls and not ss:
                log.info('  (no localStorage/sessionStorage items found)')
            return jsonify({'status': 'error',
                            'message': 'Login detected but session not captured — check playdate.log'}), 400
    except Exception:
        pass
    ok, result = exchange_code(raw)
    if ok:
        return jsonify({'status': 'success', 'username': result})
    return jsonify({'status': 'error', 'message': result}), 400


@bp.route('/basic-login', methods=['POST'])
def ubi_basic_login():
    from .ubisoft import login_with_credentials
    body = request.json or {}
    username = (body.get('username') or '').strip()
    password = body.get('password') or ''
    ok, result = login_with_credentials(username, password)
    if ok:
        return jsonify({'status': 'success', 'username': result})
    return jsonify({'status': 'error', 'message': result}), 400


@bp.route('/disconnect', methods=['POST'])
def ubi_disconnect():
    from .ubisoft import clear_ubi_tokens
    clear_ubi_tokens()
    return jsonify({'status': 'success'})


@bp.route('/start-launcher', methods=['POST'])
def ubi_start_launcher():
    import plugins
    plugin = plugins.get('ubisoft')
    if plugin is None:
        return jsonify({'status': 'error', 'message': 'Plugin not loaded'}), 500
    return jsonify(plugin.start_launcher())


@bp.route('/sync', methods=['POST'])
def ubi_sync():
    from .ubisoft import start_library_sync
    result = start_library_sync()
    return jsonify(result)


@bp.route('/uninstall/<int:appid>', methods=['POST'])
def ubi_uninstall(appid):
    import plugins
    plugin = plugins.get('ubisoft')
    if plugin is None:
        return jsonify({'status': 'error', 'message': 'Plugin not loaded'}), 500
    return jsonify(plugin.uninstall_game(appid))


@bp.route('/sync-local', methods=['POST'])
def ubi_sync_local():
    from .ubisoft import start_local_library_sync
    result = start_local_library_sync()
    return jsonify(result)


@bp.route('/sync/status')
def ubi_sync_status():
    from .ubisoft import get_sync_state
    return jsonify(get_sync_state())


@bp.route('/sync/cancel', methods=['POST'])
def ubi_sync_cancel():
    from .ubisoft import cancel_library_sync
    cancel_library_sync()
    return jsonify({'status': 'ok'})


@bp.route('/scrape-single/<int:appid>', methods=['POST'])
def ubi_scrape_single(appid):
    from .ubisoft import scrape_single
    from database import ts_to_date

    meta = scrape_single(appid)
    if meta is None:
        return jsonify({'status': 'error', 'message': 'Metadata fetch failed'}), 502

    update_game_data(appid, **meta)

    data_out = dict(meta)
    if data_out.get('release_date'):
        data_out['release_date'] = ts_to_date(data_out['release_date']) or ''
    data_out.pop('_description', None)
    return jsonify({'status': 'success', 'data': data_out})
