"""Strict managed-option parsing for the intentionally limited hardened profile."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Safe operator-visible configuration error (never includes option values)."""


DEFAULTS = {
    'obsidian_auth_token': '', 'vault_name': '', 'vault_password': '',
    'obsidian_sync_interval': 300, 'enable_git_sync': False, 'git_repos': '',
    'git_auth_token': '', 'git_sync_interval': 900, 'enable_remarkable': False,
    'remarkable_pairing_code': '', 'remarkable_sync_interval': 300,
    'remarkable_tag_prefix': '', 'remarkable_extra_tags': '',
    'ha_ocr_url': '', 'ha_ocr_token': '', 'ha_ocr_entity': '',
    'enable_mcp': False, 'mcp_auth_token': '', 'mcp_tls_cert': '',
    'mcp_tls_key': '', 'mcp_allowed_hosts': '', 'mcp_allowed_origins': '',
}
HOST = re.compile(r'(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?\Z')


def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(',') if x.strip()]


def validate_options(options: dict) -> dict:
    if type(options) is not dict:
        raise ConfigError('Managed options must be a JSON object.')
    if set(options) - DEFAULTS.keys():
        raise ConfigError('Unsupported managed option; remove obsolete OAuth, tunnel, path or mutation options.')
    cfg = DEFAULTS | options
    for key, default in DEFAULTS.items():
        value = cfg[key]
        if value is None and isinstance(default, str):
            cfg[key] = value = ''
        if type(value) is not type(default):
            raise ConfigError(f'{key} has an invalid type.')
        if isinstance(value, str):
            if '\0' in value or len(value.encode()) > (131072 if key.startswith('mcp_tls_') else (65536 if key == 'git_repos' else 4096)):
                raise ConfigError(f'{key} exceeds the text policy.')
            if key not in ('git_repos', 'mcp_tls_cert', 'mcp_tls_key') and ('\n' in value or '\r' in value):
                raise ConfigError(f'{key} must be a single line.')
        if key.endswith('_interval') and not 30 <= value <= 86400:
            raise ConfigError(f'{key} must be between 30 and 86400 seconds.')
    if not cfg['obsidian_auth_token'] or not cfg['vault_name'].strip():
        raise ConfigError('obsidian_auth_token and vault_name are required; configure a fresh addon instance before starting.')
    if cfg['vault_password'] != cfg['vault_password'].rstrip():
        raise ConfigError('Trailing whitespace in vault_password is unsupported by the official CLI stdin prompt; acquisition refused rather than changing the password.')
    if cfg['enable_git_sync'] and not cfg['git_repos'].strip():
        raise ConfigError('git_repos is required when Git sync is enabled.')
    # Git's own strict parser validates URLs, branches and independent namespaces.
    if cfg['ha_ocr_url']:
        u = urlsplit(cfg['ha_ocr_url'])
        try:
            valid_port = u.port is None or 1 <= u.port <= 65535
        except ValueError:
            valid_port = False
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.fragment or not valid_port:
            raise ConfigError('ha_ocr_url must be HTTPS without embedded credentials or a fragment.')
    for host in _csv(cfg['mcp_allowed_hosts']):
        if not HOST.fullmatch(host) or ('*' in host):
            raise ConfigError('mcp_allowed_hosts must contain exact host[:port] entries.')
        try:
            if urlsplit('https://' + host).port == 0:
                raise ValueError
        except ValueError:
            raise ConfigError('mcp_allowed_hosts contains an invalid port.') from None
    for origin in _csv(cfg['mcp_allowed_origins']):
        u = urlsplit(origin)
        if u.scheme != 'https' or not u.netloc or u.path or u.query or u.fragment or not HOST.fullmatch(u.netloc):
            raise ConfigError('mcp_allowed_origins must contain exact HTTPS origins without paths.')
    if cfg['enable_mcp']:
        token = cfg['mcp_auth_token']
        if re.fullmatch(r'[A-Za-z0-9._~+/-]{32,512}={0,2}', token) is None:
            raise ConfigError('mcp_auth_token requires 32 to 512 bearer-safe characters (letters, digits, . _ ~ + / - with optional trailing =).')
        if not cfg['mcp_tls_cert'] or not cfg['mcp_tls_key']:
            raise ConfigError('MCP requires operator-supplied mcp_tls_cert and mcp_tls_key PEM values.')
        if not _csv(cfg['mcp_allowed_hosts']):
            raise ConfigError('MCP requires an explicit mcp_allowed_hosts host policy.')
    return cfg


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('Duplicate managed option.')
        result[key] = value
    return result


def load_options(path: Path = Path('/data/options.json')) -> dict:
    """Never follow an options symlink; protect HA's root-owned secret file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != 0:
                raise ConfigError('Managed options must be a root-owned regular unlinked file.')
            if meta.st_size > 1048576:
                raise ConfigError('Managed options file is oversized.')
            if os.geteuid() == 0:
                os.fchmod(stream.fileno(), 0o600)
            options = json.load(stream, object_pairs_hook=_unique_object)
    except ConfigError:
        raise
    except (OSError, ValueError):
        raise ConfigError('Cannot securely read managed options.') from None
    return validate_options(options)
