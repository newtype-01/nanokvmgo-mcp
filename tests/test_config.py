import json
import os
import pathlib
import ssl
import tempfile
import unittest
from unittest.mock import patch

from nanokvmgo_mcp.config import ConfigurationError, load_settings, parse_url, private_secret, save_settings


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = pathlib.Path(self.folder.name).resolve()

    def test_base_and_endpoint_use_identical_normalized_url(self):
        first = load_settings(self.root, {'url': 'https://device.example'})
        second = load_settings(self.root, {'url': 'https://device.example/api/mcp'})
        self.assertEqual(first, second)

    def test_custom_port_and_ipv6_keep_authority(self):
        host, port, origin, path = parse_url('https://[2001:db8::1]:9443/api/mcp')
        self.assertEqual((host, port, origin, path), ('2001:db8::1', 9443, 'https://[2001:db8::1]:9443', '/api/mcp'))

    def test_unsafe_or_ambiguous_urls_are_rejected(self):
        for url in ['http://device.example', 'https://user:password@device.example',
                    'https://device.example/api/mcp?token=secret', 'https://device.example/#token',
                    'https://device.example/other', 'https://device.example:bad', '', None]:
            with self.subTest(url_type=type(url).__name__):
                with self.assertRaises(ConfigurationError):
                    parse_url(url)

    def test_certificate_pin_is_per_device_and_validated(self):
        settings = load_settings(self.root, {'url': 'https://device.example', 'cert_sha256': ':'.join(['AB'] * 32)})
        self.assertEqual(settings.cert_sha256, 'ab' * 32)
        with self.assertRaises(ConfigurationError):
            load_settings(self.root, {'url': 'https://device.example', 'cert_sha256': 'short'})

    def test_ssl_always_verifies_certificates_and_hostnames(self):
        settings = load_settings(self.root, {'url': 'https://device.example'})
        context = settings.ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(context.hostname_checks_common_name)

    def test_explicit_legacy_common_name_does_not_disable_tls_checks(self):
        settings = load_settings(self.root, {'url': 'https://device.example', 'tls_server_name': 'localhost'})
        context = settings.ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.hostname_checks_common_name)

    def test_secret_mode_is_checked_and_values_are_not_in_errors(self):
        path = self.root / 'api-key'
        path.write_text('TEST_VALUE_NEVER_IN_ERRORS')
        path.chmod(0o644)
        if os.name == 'posix':
            with self.assertRaises(ConfigurationError) as error:
                private_secret(path, 'API key')
            self.assertNotIn('TEST_VALUE_NEVER_IN_ERRORS', str(error.exception))
        path.chmod(0o600)
        self.assertEqual(private_secret(path, 'API key'), 'TEST_VALUE_NEVER_IN_ERRORS')

    def test_environment_key_overrides_file_without_persisting_secret(self):
        settings = load_settings(self.root, {'url': 'https://device.example'})
        with patch.dict(os.environ, {'NANOKVM_API_KEY': 'offline-env-value'}):
            self.assertEqual(settings.api_key(), 'offline-env-value')
        self.assertFalse(settings.api_key_file.exists())

    def test_relative_paths_stay_relative_to_configuration_not_package(self):
        settings = load_settings(self.root, {'url': 'https://device.example', 'api_key_file': 'private/key'})
        self.assertEqual(settings.api_key_file, self.root / 'private/key')
        self.assertEqual(settings.cache_dir, self.root / 'cache')
        self.assertEqual(settings.output_dir, self.root / 'outputs')

    def test_save_is_private_and_invalid_config_does_not_replace_working_file(self):
        values = {'url': 'https://device.example'}
        path = save_settings(self.root, values)
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ConfigurationError):
            save_settings(self.root, {'url': 'http://device.example'})
        self.assertEqual(json.loads(path.read_text()), values)

    def test_bad_ca_returns_configuration_error(self):
        settings = load_settings(self.root, {'url': 'https://device.example', 'ca_cert': 'missing.pem'})
        with self.assertRaises(ConfigurationError):
            settings.ssl_context()

    def test_unknown_config_fields_are_not_silently_accepted(self):
        with self.assertRaises(ConfigurationError):
            load_settings(self.root, {'url': 'https://device.example', 'verify_tls': False})


if __name__ == '__main__':
    unittest.main()
