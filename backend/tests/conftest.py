"""
Test isolation: point the app at a throwaway SQLite DB + scratch dirs so test
runs never pollute (or read stale state from) the developer's `jobhunter.db`.

Imported by pytest before any test module, so these env vars take effect before
`app.core.config.settings` is constructed.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="jobhunter-test-")

os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["GENERATED_DIR"] = os.path.join(_TMP, "generated")
os.environ["VAULT_KEY"] = "test-vault-encryption-key-32-chars!!"
os.environ["AI_API_KEY"] = ""
os.environ["LIVE_SCRAPING_ENABLED"] = "false"
