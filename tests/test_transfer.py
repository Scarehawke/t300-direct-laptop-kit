from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from t300_mainline.transfer import (
    INCOMING_BUNDLE,
    TransferError,
    receive_bundle,
    validate_public_key,
)


def ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


class TransferTests(unittest.TestCase):
    def test_ed25519_public_key_is_parsed_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as directory:
            blob = ssh_string(b"ssh-ed25519") + ssh_string(b"x" * 32)
            encoded = base64.b64encode(blob).decode("ascii")
            path = Path(directory) / "deploy.pub"
            path.write_text("ssh-ed25519 %s laptop comment\n" % encoded, encoding="ascii")
            result = validate_public_key(path)
            expected = "SHA256:" + base64.b64encode(
                hashlib.sha256(blob).digest()
            ).decode("ascii").rstrip("=")
            self.assertEqual(result["fingerprint"], expected)
            self.assertEqual(result["key"], "ssh-ed25519 %s t300-deploy" % encoded)

    def test_receiver_quarantines_exactly_one_verified_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory)
            incoming.chmod(0o730)
            payload = b"deterministic bundle bytes"
            digest = hashlib.sha256(payload).hexdigest()
            header = {
                "schema_version": 1,
                "operation": "upload-config",
                "sha256": digest,
                "size": len(payload),
            }
            source = io.BytesIO(
                (json.dumps(header) + "\n").encode("ascii") + payload
            )
            output = io.BytesIO()
            result = receive_bundle(
                source, output, incoming, strict_owner=False
            )
            self.assertEqual(result["sha256"], digest)
            self.assertEqual((incoming / INCOMING_BUNDLE).read_bytes(), payload)
            self.assertEqual(
                json.loads(output.getvalue().decode("ascii"))["state"],
                "quarantined",
            )

    def test_receiver_rejects_trailing_bytes_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory)
            incoming.chmod(0o730)
            payload = b"expected"
            header = {
                "schema_version": 1,
                "operation": "upload-config",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            source = io.BytesIO(
                (json.dumps(header) + "\n").encode("ascii") + payload + b"extra"
            )
            with self.assertRaisesRegex(TransferError, "beyond"):
                receive_bundle(
                    source, io.BytesIO(), incoming, strict_owner=False
                )
            self.assertFalse((incoming / INCOMING_BUNDLE).exists())


if __name__ == "__main__":
    unittest.main()
