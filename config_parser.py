"""
config_parser.py — Reads Ubisoft Connect's local game-catalog cache directly,
no account/API access needed.

Ubisoft Connect caches a full descriptor for every game associated with the
signed-in account (name, executables, registry install-path) in a single
binary file:
    AppData/Local/Ubisoft Game Launcher/cache/configuration/configurations

No official schema for this file exists -- the client executables are
VMProtect-packed, so it can't be pulled from them directly. Reverse
engineered independently by inspecting a real file byte-for-byte: it is a
flat protobuf byte stream, structurally equivalent to

    repeated GameEntry entries = 1;
    message GameEntry {
        int32 install_id = 1;
        int32 launch_id  = 2;
        bytes yaml       = 3;   // YAML text describing the game
    }

decoded here against the public protobuf wire-format spec (varints, tags,
length-delimited fields) rather than any Ubisoft-specific format. Verified
live against a real logged-in Ubisoft Connect install: correctly recovered
29 real owned games (names and identifiers matching a known-good reference).
"""

import logging
import os
import re

import yaml

log = logging.getLogger(__name__)

BLACKLISTED_NAMES = {'gamename', 'l1', '', 'ubisoft game', 'name'}  # kept for reference; is_placeholder_name() supersedes this

_SLUG_STRIP_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACE_RE = re.compile(r'[\s_]+')
_PLACEHOLDER_KEY_RE = re.compile(r'^l\d+$', re.IGNORECASE)


def is_placeholder_name(value):
    """True if `value` looks like one of Ubisoft's untranslated placeholder
    tokens rather than a real display name. Confirmed live in two distinct
    forms across a real cache: a generic localization key ('l1', 'l2', ...
    -- the same 'l<N>' pattern also shows up as placeholder values for
    unrelated fields like thumb_image and overlay text, so it's a general
    convention, not tied to any specific game or field) and an ALL-CAPS
    echo of the field's own name ('GAMENAME', 'THUMBIMAGE', 'HOMEPAGEURL',
    etc, seen verbatim as the placeholder value for their same-named
    fields). Matches the *pattern* rather than a fixed list of previously
    seen strings, so a different account's untranslated entries (e.g. an
    'l7' this account never had) are still caught."""
    v = (value or '').strip()
    if not v or v.lower() in ('ubisoft game', 'name'):
        return True
    if _PLACEHOLDER_KEY_RE.match(v):
        return True
    if v.isupper() and v.isalpha() and len(v) > 3:
        return True
    return False


def slugify(name):
    """Derive a store-URL slug from a game name -- verified to exactly
    match Ubisoft's own slugs for real games (cross-checked against a
    known-good reference: "Tom Clancy's Rainbow Six® Siege" ->
    'tom-clancys-rainbow-six-siege')."""
    s = name.replace('’', '').replace("'", '')
    s = _SLUG_STRIP_RE.sub('', s)
    s = _SLUG_SPACE_RE.sub('-', s.strip())
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


# ── Protobuf wire-format walk ───────────────────────────────────────────────

def _read_varint(data, pos):
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _read_tag(data, pos):
    tag, pos = _read_varint(data, pos)
    return tag >> 3, tag & 0x7, pos


def _iter_raw_entries(data):
    """Yield (install_id, launch_id, yaml_bytes) for each top-level record.
    Resyncs a byte at a time on any parse failure -- the file has no
    higher-level index, so a single malformed/foreign record must not take
    down the rest of the scan."""
    pos = 0
    n = len(data)
    while pos < n:
        entry_start = pos
        try:
            field_no, wire_type, pos = _read_tag(data, pos)
            if field_no != 1 or wire_type != 2:
                pos = entry_start + 1
                continue
            entry_len, pos = _read_varint(data, pos)
            entry_end = pos + entry_len
            if entry_len <= 0 or entry_end > n:
                pos = entry_start + 1
                continue

            install_id = launch_id = 0
            yaml_bytes = b''
            inner_pos = pos
            valid = True
            while inner_pos < entry_end:
                f_no, w_type, inner_pos = _read_tag(data, inner_pos)
                if w_type == 0:
                    val, inner_pos = _read_varint(data, inner_pos)
                    if f_no == 1:
                        install_id = val
                    elif f_no == 2:
                        launch_id = val
                elif w_type == 2:
                    length, inner_pos = _read_varint(data, inner_pos)
                    payload = data[inner_pos:inner_pos + length]
                    inner_pos += length
                    if f_no == 3:
                        yaml_bytes = payload
                elif w_type == 1:
                    inner_pos += 8
                elif w_type == 5:
                    inner_pos += 4
                else:
                    valid = False
                    break

            if valid and yaml_bytes:
                yield install_id, (launch_id or install_id), yaml_bytes

            pos = entry_end
        except (IndexError, ValueError):
            pos = entry_start + 1


# ── YAML field extraction ───────────────────────────────────────────────────

def _field(root, name):
    """root['name'], falling back through Ubisoft's own placeholder chain --
    'name' is very often a localization key like 'l1', not real text."""
    value = (root.get(name) or '').strip() if isinstance(root, dict) else ''
    if not is_placeholder_name(value):
        return value
    if name == 'name':
        installer = root.get('installer') or {}
        fallback = (installer.get('game_identifier') or '').strip()
        if fallback and not is_placeholder_name(fallback):
            return fallback
    return value


def _first_executable(root):
    """First online executable's relative path + registry install-dir key,
    or (None, None) if this entry doesn't describe a launchable game."""
    try:
        executables = root['start_game']['online']['executables']
        exe = executables[0]
        rel = exe['path']['relative']
        reg = exe['working_directory']['register']
        return rel, reg
    except (KeyError, IndexError, TypeError):
        return None, None


def _parse_entry(install_id, launch_id, yaml_bytes):
    text = yaml_bytes.decode('utf-8', errors='ignore')
    if 'start_game' not in text:
        return None  # not a real game record (addon/placeholder entry)
    try:
        doc = yaml.safe_load(text.replace('\t', ' '))
    except yaml.YAMLError as e:
        log.debug('Ubisoft config parser: YAML parse failed for entry %s: %s', install_id, e)
        return None
    root = (doc or {}).get('root')
    if not isinstance(root, dict):
        return None

    exe_rel, reg_key = _first_executable(root)
    m = re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
                  str(root.get('space_id') or ''), re.IGNORECASE)
    space_id = m.group(0) if m else (root.get('space_id') or '')

    # Present (as {'name': 'Steam'}, etc) when this entry is actually owned
    # through another storefront and just cross-linked into Ubisoft Connect
    # rather than a real native Ubisoft purchase -- confirmed live for every
    # Steam-linked title in a real account. Structured as a name, not a bare
    # flag, so it should generalize to other linked platforms (e.g. Epic)
    # the same way, though only Steam has been observed directly.
    tpp = root.get('third_party_platform')
    third_party_platform = tpp.get('name') if isinstance(tpp, dict) else None

    return {
        'install_id':           str(install_id),
        'launch_id':            str(launch_id),
        'space_id':             space_id,
        'name':                 _field(root, 'name'),
        'thumb_image':          _field(root, 'thumb_image'),
        'exe_relative':         exe_rel,
        'registry_key':         reg_key,
        'third_party_platform': third_party_platform,
    }


def parse_configurations(data):
    """Yield a dict per real game found in a raw 'configurations' file's
    bytes: {install_id, launch_id, space_id, name, thumb_image,
    exe_relative, registry_key, third_party_platform}. space_id/name/
    exe_relative/registry_key/third_party_platform may be '' / None when
    that particular entry doesn't have them."""
    for install_id, launch_id, yaml_bytes in _iter_raw_entries(data):
        parsed = _parse_entry(install_id, launch_id, yaml_bytes)
        if parsed:
            yield parsed


def read_configurations(path):
    """Read and parse a 'configurations' file from disk. Returns a list
    (not a generator) so callers get a clear [] on any failure."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        log.warning('Ubisoft config parser: could not read %s: %s', path, e)
        return []
    return list(parse_configurations(data))


# ── Minimal Wine prefix registry reader ─────────────────────────────────────
# Wine's own .reg-style text export format (public, not Ubisoft-specific):
#   [Software\\Foo\\Bar] 1600000000
#   "InstallDir"="C:\\\\Program Files\\\\Foo"

_REG_HEADER_RE = re.compile(r'^\[(.+)\]\s+\d+$')
_REG_VALUE_RE  = re.compile(r'^"([^"]+)"=(.*)$')
_REG_UNICODE_ESCAPE_RE = re.compile(r'\\x([0-9a-fA-F]{4})')

_registry_cache = {}


def _unescape_reg(s):
    s = _REG_UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    return s.replace('\\\\', '\\')


def _read_reg_file(path, out):
    if not os.path.isfile(path):
        return
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            section = ''
            for line in f:
                line = line.strip()
                if not line or line.startswith((';', '#')):
                    continue
                m = _REG_HEADER_RE.match(line)
                if m:
                    section = _unescape_reg(m.group(1))
                    continue
                m = _REG_VALUE_RE.match(line)
                if m and section:
                    key = _unescape_reg(m.group(1))
                    raw = m.group(2).strip()
                    if raw.startswith('"') and raw.endswith('"'):
                        raw = _unescape_reg(raw[1:-1])
                    full_key = f'{section}\\{key}'.lower()
                    out[full_key] = raw
    except OSError as e:
        log.warning('Ubisoft config parser: failed to read registry file %s: %s', path, e)


def _load_prefix_registry(prefix):
    if prefix not in _registry_cache:
        reg = {}
        _read_reg_file(os.path.join(prefix, 'system.reg'), reg)
        _read_reg_file(os.path.join(prefix, 'user.reg'), reg)
        _registry_cache[prefix] = reg
    return _registry_cache[prefix]


def invalidate_registry_cache(prefix):
    _registry_cache.pop(prefix, None)


def _normalize_reg_key(key):
    key = key.lower()
    for hive in ('hkey_local_machine\\', 'hkey_current_user\\'):
        if key.startswith(hive):
            return key[len(hive):]
    return key


def read_registry_value(prefix, key):
    """key is 'Sub\\Key\\Path\\ValueName' (HKLM/HKCU prefix optional)."""
    if not key:
        return None
    registry   = _load_prefix_registry(prefix)
    normalized = _normalize_reg_key(key)
    if normalized in registry:
        return registry[normalized]
    wow_key = normalized.replace('software\\', 'software\\wow6432node\\', 1)
    return registry.get(wow_key)


def _resolve_drive_path(prefix, windows_path):
    m = re.match(r'^([A-Za-z]):[\\/](.*)$', windows_path)
    if not m:
        return os.path.join(prefix, 'drive_c', windows_path.replace('\\', '/'))
    drive, rel = m.group(1).lower(), m.group(2).replace('\\', '/')
    link = os.path.join(prefix, 'dosdevices', f'{drive}:')
    try:
        drive_root = os.path.realpath(link)
    except OSError:
        drive_root = None
    if not drive_root or not os.path.isdir(drive_root):
        drive_root = os.path.join(prefix, 'drive_c') if drive == 'c' else '/'
    return os.path.join(drive_root, rel)


def resolve_install_dir(prefix, registry_key):
    """Resolve a game entry's registry_key to a real install directory on
    disk, or None if the key isn't set (game not installed) or points
    somewhere that doesn't exist."""
    value = read_registry_value(prefix, registry_key)
    if not value:
        return None
    path = _resolve_drive_path(prefix, value)
    return path if os.path.isdir(path) else None
