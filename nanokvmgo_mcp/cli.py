"""Command-line configuration and stdio MCP entrypoint."""

import argparse
import json
import os
import pathlib
import sys

from .config import ConfigurationError, default_config_dir, load_settings, save_settings


def main():
    parser = argparse.ArgumentParser(prog='nanokvmgo-mcp')
    parser.add_argument('--config-dir', type=pathlib.Path)
    from . import __version__
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('serve', help='Run the enhanced stdio MCP bridge')
    configure = commands.add_parser('configure', help='Save connection settings without exposing credentials')
    configure.add_argument('--url', required=True)
    configure.add_argument('--api-key-file', type=pathlib.Path)
    configure.add_argument('--cookie-file', type=pathlib.Path)
    configure.add_argument('--ca-cert', type=pathlib.Path)
    configure.add_argument('--tls-server-name')
    configure.add_argument('--fingerprint')
    configure.add_argument('--cache-dir', type=pathlib.Path)
    configure.add_argument('--output-dir', type=pathlib.Path)
    configure.add_argument('--phone-mode', action='store_true', default=None)
    commands.add_parser('check', help='Validate local settings and credentials without contacting the device')
    args = parser.parse_args()
    root = args.config_dir.expanduser().resolve() if args.config_dir else default_config_dir()
    try:
        if args.command == 'configure':
            existing = root / 'config.json'
            try:
                values = json.loads(existing.read_text()) if existing.is_file() else {}
            except ValueError:
                raise ConfigurationError('Existing configuration could not be read') from None
            if not isinstance(values, dict):
                raise ConfigurationError('Existing configuration must be an object')
            values['url'] = args.url
            for key in ['api_key_file', 'cookie_file', 'ca_cert', 'tls_server_name', 'cache_dir', 'output_dir']:
                value = getattr(args, key)
                if value is not None:
                    values[key] = str(value.expanduser().resolve()) if isinstance(value, pathlib.Path) else value
            if args.fingerprint:
                values['cert_sha256'] = args.fingerprint
            if args.phone_mode is not None:
                values['phone_mode'] = args.phone_mode
            target = save_settings(root, values)
            print('Saved configuration: ' + str(target))
            return 0
        if args.command == 'check':
            settings = load_settings(root)
            settings.ssl_context()
            settings.api_key()
            print(json.dumps({'ok': True, 'device_configured': True,
                              'phone_cookie_present': settings.cookie_file.is_file(),
                              'ocr_supported': sys.platform == 'darwin'}))
            return 0
        os.environ['NANOKVM_CONFIG_DIR'] = str(root)
        from .launcher import serve
        return serve(install_signals=True)
    except (ConfigurationError, OSError):
        print('NanoKVM configuration unavailable or invalid; run configure/check and inspect local settings.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
