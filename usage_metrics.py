"""Minimal consent-based usage metrics. Never sends personal collection content.

A self-hosted endpoint is optional. Network errors do not affect the application.
"""
from __future__ import annotations
import json
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

_ALLOWED = {'launch', 'set_added', 'part_checked', 'consent_enabled'}
# Configure after deploying your own HTTPS metrics service. Disabled until then.
METRICS_ENDPOINT = ''

class UsageMetrics:
    def __init__(self, settings_path: Path, program_dir: Path):
        self.settings_path = Path(settings_path)
        self.program_dir = Path(program_dir)
        self._settings = self._read()
        self.enabled = self._settings.get('usage_metrics_consent') is True
        candidate = METRICS_ENDPOINT.strip()
        parsed = urlparse(candidate)
        self.endpoint = candidate if (parsed.scheme == 'https' and parsed.netloc and not parsed.username and not parsed.password) else ''

    def _read(self):
        try:
            value = json.loads(self.settings_path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_enabled(self, enabled: bool):
        data = self._read()
        data['usage_metrics_consent'] = bool(enabled)
        if enabled and not data.get('usage_install_id'):
            data['usage_install_id'] = str(uuid.uuid4())
        if not enabled:
            data.pop('usage_install_id', None)
        tmp = self.settings_path.with_suffix('.settings_tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.settings_path)
        self.enabled = bool(enabled)

    def track(self, event: str):
        if event not in _ALLOWED or not self.enabled or not self.endpoint:
            return False
        # Only an anonymous pseudonymous installation ID and an event name.
        # No key, set number, image, collection or filesystem path.
        data = self._read()
        ident = data.get('usage_install_id')
        if not ident or data.get('usage_metrics_consent') is not True:
            return False
        try:
            version = json.loads((self.program_dir/'version.json').read_text(encoding='utf8')).get('version','unknown')
        except (OSError, ValueError):
            version='unknown'
        payload = {'installation_id': ident, 'event': event, 'version': str(version)[:30]}
        endpoint=self.endpoint
        def send():
            try:
                import requests
                requests.post(endpoint, json=payload, timeout=3)
            except Exception:
                pass
        threading.Thread(target=send, name='lego-metrics', daemon=True).start()
        return True
