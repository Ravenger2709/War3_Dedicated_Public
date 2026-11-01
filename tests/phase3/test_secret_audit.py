"""Secret audit regression, synthetic markers only, no real account."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('secret_audit',Path(__file__).resolve().parents[2]/'scripts/audit-production-secrets.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


def tar_bytes(name,data,mode='w'):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode=mode) as tar:
        entry=tarfile.TarInfo(name);entry.size=len(data);tar.addfile(entry,io.BytesIO(data))
    return out.getvalue()


class SecretAuditTests(unittest.TestCase):
    def test_qsettings_empty_quoted_password_is_not_a_secret_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);profile=root/'launcher.ini'
            profile.write_text('[General]\nPassword=""\nListLogins="synthetic-cache-marker"\n');profile.chmod(0o600)
            values=audit.profile_patterns(root)
            self.assertNotIn('""',values);self.assertIn('synthetic-cache-marker',values)

    def test_marker_crossing_chunk_boundary_and_utf16_are_detected(self):
        marker='Synthetic-Only-Secret-Value'
        for content in (b'x'*(1024*1024-3)+marker.encode(),marker.encode('utf-16le')):
            with self.assertRaisesRegex(ValueError,'^SECRET_CONTENT_FOUND$'):audit.Matcher([marker]).scan(io.BytesIO(content))

    def test_deleted_secret_in_compressed_historical_layer_is_detected(self):
        marker='Synthetic-Only-Secret-Value'
        layer=tar_bytes('some/renamed.bin',marker.encode(),mode='w:gz')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.tar';path.write_bytes(tar_bytes('blobs/sha256/synthetic',layer))
            with self.assertRaisesRegex(ValueError,'^SECRET_CONTENT_FOUND$'):audit.image_layers(path,audit.Matcher([marker]))

    def test_clean_layers_and_json_metadata_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.tar';path.write_bytes(tar_bytes('blobs/sha256/synthetic',tar_bytes('safe/file',b'public source',mode='w:gz')))
            matcher=audit.Matcher(['Synthetic-Only-Secret-Value'])
            self.assertEqual(audit.image_layers(path,matcher),(1,1));self.assertGreater(matcher.bytes_scanned,0)
