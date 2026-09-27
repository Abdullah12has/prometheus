import tempfile
import unittest
from pathlib import Path
from tts_worker import local_path


class VoicePathTest(unittest.TestCase):
    def test_profiles_are_confined_to_private_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sample = root / 'voice.wav'
            sample.write_bytes(b'example')
            self.assertEqual(local_path(str(sample), root), sample)
            with self.assertRaises(ValueError):
                local_path(str(root.parent / 'other.wav'), root, output=True)
            with self.assertRaises(ValueError):
                local_path('https://example.com/voice.wav', root)
            with self.assertRaises(ValueError):
                local_path(str(root / 'missing.wav'), root)


if __name__ == '__main__':
    unittest.main()
