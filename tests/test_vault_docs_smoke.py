import unittest


class VaultDocSmokeTests(unittest.TestCase):
    def test_worker_protocol_doc_mentions_secret_ref_modes(self):
        with open("docs/rust-migration/worker-protocol-surface.md", "r", encoding="utf-8") as handle:
            content = handle.read()

        self.assertIn("env:NAME", content)
        self.assertIn("file:C:\\\\path\\\\to\\\\secret.txt", content)
