import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fix_takeout_dates as f  # noqa: E402
SCRIPT = os.path.join(ROOT, "fix_takeout_dates.py")


def sidecar(path, title, ts, lat=0, lon=0):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"title": title, "photoTakenTime": {"timestamp": str(ts)},
                   "geoData": {"latitude": lat, "longitude": lon}}, fh)


def make_jpeg_with_exif(path, stamp):
    import struct
    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
    tiff += struct.pack("<HHII", 0x8769, 4, 1, 26) + b"\x00\x00\x00\x00"
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, 20, 44) + b"\x00\x00\x00\x00"
    tiff += stamp.encode() + b"\x00"
    seg = b"Exif\x00\x00" + tiff
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg + b"\xff\xd9")


class Matching(unittest.TestCase):
    def idx(self, *names):
        return [(*f.normalise_key(n), n) for n in names]

    def find(self, media, *jsons):
        return f.find_json(media, self.idx(*jsons), {})

    def test_new_and_old_names(self):
        self.assertEqual(self.find("a.jpg", "a.jpg.supplemental-metadata.json"),
                         "a.jpg.supplemental-metadata.json")
        self.assertEqual(self.find("a.jpg", "a.jpg.json"), "a.jpg.json")

    def test_truncated_suffix(self):
        n = "PXL_20250825_090921099.mp4.supplemental-metada.json"
        self.assertEqual(self.find("PXL_20250825_090921099.mp4", n), n)

    def test_duplicates_dont_cross(self):
        a, b = "a.jpg.supplemental-metadata.json", "a.jpg.supplemental-metadata(1).json"
        self.assertEqual(self.find("a.jpg", a, b), a)
        self.assertEqual(self.find("a(1).jpg", a, b), b)

    def test_edited_copy_uses_original(self):
        self.assertEqual(self.find("a-edited.jpg", "a.jpg.json"), "a.jpg.json")

    def test_live_photo_video(self):
        self.assertEqual(self.find("IMG_1.MP4", "IMG_1.HEIC.json"), "IMG_1.HEIC.json")

    def test_no_false_match(self):
        self.assertIsNone(self.find("b.jpg", "a.jpg.json"))


class FilenameDates(unittest.TestCase):
    def local(self, *a):
        return int(datetime(*a).timestamp())

    def test_patterns(self):
        cases = {
            "IMG_20260213_165553.jpg": (2026, 2, 13, 16, 55, 53),
            "PXL_20250825_090921099.mp4": (2025, 8, 25, 9, 9, 21),
            "IMG20221211222953.jpg": (2022, 12, 11, 22, 29, 53),
            "IMG-20230101-WA0001.jpg": (2023, 1, 1, 12, 0, 0),
        }
        for name, dt in cases.items():
            self.assertEqual(f.date_from_filename(name)["ts"], self.local(*dt), name)

    def test_yymmdd_and_epoch(self):
        self.assertEqual(f.date_from_filename_extra("IMG_260106_193115.jpg")["ts"],
                         self.local(2026, 1, 6, 19, 31, 15))
        self.assertEqual(f.date_from_filename_extra("1708145044972.jpg")["ts"], 1708145044)
        self.assertEqual(f.date_from_filename_extra("1764101407.png")["ts"], 1764101407)

    def test_rejects_garbage(self):
        self.assertIsNone(f.date_from_filename("VID_29280508_103644_183.mp4"))
        self.assertIsNone(f.date_from_filename_extra("DSC00195.JPG"))
        self.assertIsNone(f.date_from_filename_extra("12345.jpg"))


class Exif(unittest.TestCase):
    def test_reads_date(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.jpg")
            make_jpeg_with_exif(p, "2010:05:17 14:30:00")
            self.assertEqual(f.read_exif_date(p)["ts"], int(datetime(2010, 5, 17, 14, 30).timestamp()))

    def test_plain_jpeg_has_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.jpg")
            with open(p, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            self.assertIsNone(f.read_exif_date(p))


class Helpers(unittest.TestCase):
    def test_show_strips_long_path_prefix(self):
        self.assertEqual(f.show("\\\\?\\C:\\a\\b"), "C:\\a\\b")
        self.assertEqual(f.show("/tmp/x"), "/tmp/x")

    def test_folder_year(self):
        self.assertIsNotNone(f.date_from_folder_year("/x/Photos from 2023"))
        self.assertIsNone(f.date_from_folder_year("/x/Family"))


class EndToEnd(unittest.TestCase):
    def run_script(self, root, *extra):
        return subprocess.run([sys.executable, SCRIPT, root, *extra],
                              capture_output=True, text=True)

    def test_sets_mtime_and_cross_folder_json(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "A"))
            os.makedirs(os.path.join(d, "B"))
            open(os.path.join(d, "A", "one.jpg"), "wb").close()
            open(os.path.join(d, "A", "two.jpg"), "wb").close()
            sidecar(os.path.join(d, "A", "one.jpg.supplemental-metadata.json"), "one.jpg", 1500000000)
            sidecar(os.path.join(d, "B", "two.jpg.json"), "two.jpg", 1510000000)
            r = self.run_script(d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(int(os.path.getmtime(os.path.join(d, "A", "one.jpg"))), 1500000000)
            self.assertEqual(int(os.path.getmtime(os.path.join(d, "A", "two.jpg"))), 1510000000)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "one.jpg")
            open(p, "wb").close()
            before = os.path.getmtime(p)
            sidecar(os.path.join(d, "one.jpg.json"), "one.jpg", 1500000000)
            self.run_script(d, "--dry-run")
            self.assertEqual(os.path.getmtime(p), before)

    def test_json_kept_when_exiftool_unusable(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "one.jpg"), "wb").close()
            j = os.path.join(d, "one.jpg.json")
            sidecar(j, "one.jpg", 1500000000)
            r = self.run_script(d, "--exif", "--exiftool", os.path.join(d, "missing-exiftool"),
                                "--delete-json")
            self.assertTrue(os.path.exists(j), "JSON must survive when ExifTool is unusable")
            self.assertIn("NOT deleted", r.stdout)

    def test_json_deleted_without_exif(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "one.jpg"), "wb").close()
            j = os.path.join(d, "one.jpg.json")
            sidecar(j, "one.jpg", 1500000000)
            self.run_script(d, "--delete-json")
            self.assertFalse(os.path.exists(j))


if __name__ == "__main__":
    unittest.main()
