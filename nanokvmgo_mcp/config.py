"""Per-user NanoKVM connection settings; no device or credentials in the package."""

import dataclasses
import json
import os
import pathlib
import re
import ssl
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def default_config_dir():
    explicit = os.environ.get('NANOKVM_CONFIG_DIR')
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    root = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', pathlib.Path.home() / '.config'))
    return (root / 'nanokvmgo-mcp').expanduser().resolve()


def parse_url(value):
    if not isinstance(value, str) or not value:
        raise ConfigurationError('Set a device URL with configure --url or NANOKVM_URL')
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError:
        raise ConfigurationError('Invalid device URL') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/', '/api/mcp')
            or not 1 <= port <= 65535 or any(c.isspace() for c in value)):
        raise ConfigurationError('Use an HTTPS device base URL or /api/mcp endpoint without credentials or query')
    host = parsed.hostname
    authority = '[' + host + ']' if ':' in host else host
    if port != 443:
        authority += ':' + str(port)
    return host, port, 'https://' + authority, '/api/mcp'


def fingerprint(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ConfigurationError('Certificate fingerprint must be SHA256 hex')
    result = value.replace(':', '').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', result):
        raise ConfigurationError('Certificate fingerprint must contain 64 SHA256 hex digits')
    return result


def private_secret(path, label):
    path = pathlib.Path(path)
    try:
        if os.name == 'posix' and path.stat().st_mode & 0o077:
            raise ConfigurationError(label + ' file must have mode0600; use chmod600')
        data = path.read_bytes()
    except OSError:
        raise ConfigurationError(label + ' file could not be read') from None
    if len(data) > 16384:
        raise ConfigurationError(label + ' file exceeds the size limit')
    try:
        return data.decode('utf-8').strip()
    except UnicodeError:
        raise ConfigurationError(label + ' file must contain UTF-8 text') from None


@dataclasses.dataclass(frozen=True)
class Settings:
    config_dir: pathlib.Path
    url: str
    host: str
    port: int
    origin: str
    api_path: str
    tls_server_name: str
    allow_common_name: bool
    ca_cert: pathlib.Path | None
    cert_sha256: str | None
    api_key_file: pathlib.Path
    cookie_file: pathlib.Path
    cache_dir: pathlib.Path
    output_dir: pathlib.Path
    phone_mode: bool = False

    def ssl_context(self):
        try:
            context = ssl.create_default_context(cafile=str(self.ca_cert) if self.ca_cert else None)
        except (OSError, ssl.SSLError):
            raise ConfigurationError('Configured CA certificate could not be loaded') from None
        # Explicit CN names support legacy self-signed device certificates.
        context.hostname_checks_common_name = self.allow_common_name
        return context

    def api_key(self):
        value = os.environ.get('NANOKVM_API_KEY')
        if value is None:
            value = private_secret(self.api_key_file, 'API key')
        if not value or len(value) > 16384 or any(c.isspace() for c in value):
            raise ConfigurationError('Missing or invalid NanoKVM API key')
        return value


def load_settings(config_dir=None, values=None):
    root = pathlib.Path(config_dir).expanduser().resolve() if config_dir else default_config_dir()
    if values is None:
        try:
            values = json.loads((root / 'config.json').read_text())
        except FileNotFoundError:
            values = {}
        except (OSError, ValueError):
            raise ConfigurationError('Configuration JSON could not be read') from None
    if not isinstance(values, dict):
        raise ConfigurationError('Configuration must be a JSON object')
    allowed = {'url', 'ca_cert', 'tls_server_name', 'cert_sha256', 'api_key_file',
               'cookie_file', 'cache_dir', 'output_dir', 'phone_mode'}
    if set(values) - allowed:
        raise ConfigurationError('Unknown configuration fields')
    value = os.environ.get('NANOKVM_URL', values.get('url'))
    host, port, origin, api_path = parse_url(value)
    server_name = os.environ.get('NANOKVM_TLS_SERVER_NAME', values.get('tls_server_name'))
    phone_mode = values.get('phone_mode', False)
    if type(phone_mode) is not bool:
        raise ConfigurationError('phone_mode must be true or false')
    if server_name is not None and (not isinstance(server_name, str) or not server_name
                                    or any(c.isspace() for c in server_name)):
        raise ConfigurationError('Invalid TLS server name')
    def path(key, default=None, env=None):
        raw = os.environ.get(env, values.get(key, default)) if env else values.get(key, default)
        if raw is None:
            return None
        if not isinstance(raw, (str, pathlib.Path)) or not str(raw):
            raise ConfigurationError('Invalid configuration path')
        result = pathlib.Path(raw).expanduser()
        return (result if result.is_absolute() else root / result).resolve()
    return Settings(root, origin + api_path, host, port, origin, api_path,
                    server_name or host, bool(server_name),
                    path('ca_cert', env='NANOKVM_CA_CERT'),
                    fingerprint(os.environ.get('NANOKVM_CERT_SHA256', values.get('cert_sha256'))),
                    path('api_key_file', 'api-key', 'NANOKVM_API_KEY_FILE'),
                    path('cookie_file', 'web-session.cookie', 'NANOKVM_COOKIE_FILE'),
                    path('cache_dir', 'cache'), path('output_dir', 'outputs'), phone_mode)


def save_settings(config_dir, values):
    root = pathlib.Path(config_dir).expanduser().resolve()
    load_settings(root, values)  # Validate before writing; do not connect or read secrets.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / 'config.json'
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix='.config-', dir=root)
    try:
        if os.name == 'posix':
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(values, output, indent=2)
            output.write('\n')
        os.replace(temporary, target)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    return target
