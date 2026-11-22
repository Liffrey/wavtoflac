"""Calistirma: python -m unittest test_wav_to_flac  (ffmpeg gerekir; yoksa ilgili testler atlanir)"""
import csv
import logging
import os
import struct
import tempfile
import unittest
import wave

from wav_to_flac_converter import ConversionEngine, csv_separator, find_tool, read_header_duration

FFMPEG = find_tool("ffmpeg")


def make_wav(path, seconds=1.5, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", (i * 37) % 2000 - 1000) for i in range(int(seconds * rate))))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = os.path.join(self.tmp.name, "a.wav")
        self.flac = os.path.join(self.tmp.name, "a.flac")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        e = ConversionEngine(logging.getLogger("test"))
        e.ffmpeg_path = FFMPEG
        e.ffprobe_path = find_tool("ffprobe")
        e.delete_original = True
        return e

    def test_error_csv_excel_format(self):
        path = os.path.join(self.tmp.name, "hatalar.csv")
        with open(path, "w", encoding="utf-8") as f:  # v1.1.1 bicimi: BOM'suz, virgullu
            f.write("Tarih_Saat,Dosya_Yolu,Hata_Turu,Detay\r\n")
        e = self.engine()
        for _ in range(2):  # ikinci acilis eklemeli, ikinci BOM yazmamali
            e._csv_path = path
            e._open_error_csv()
            e._write_error_csv(r"C:\kayıtlar\görüşme.wav", "test", "çğış")
            e._close_error_csv()
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "hatalar_eski.csv")))
        with open(path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=csv_separator()))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1:], [r"C:\kayıtlar\görüşme.wav", "test", "çğış"])

    def test_wav_header_duration(self):
        make_wav(self.wav)
        self.assertAlmostEqual(read_header_duration(self.wav), 1.5)
        self.assertIsNone(read_header_duration(__file__))  # ne WAV ne FLAC

    @unittest.skipUnless(FFMPEG, "ffmpeg yok")
    def test_convert_verify_delete(self):
        make_wav(self.wav)
        e = self.engine()
        e._convert_one(self.wav)
        self.assertEqual(e.snapshot_counters()["converted"], 1)
        self.assertFalse(os.path.exists(self.wav))
        self.assertFalse(os.path.exists(self.flac + ".tmp"))
        self.assertAlmostEqual(read_header_duration(self.flac), 1.5)

    @unittest.skipUnless(FFMPEG, "ffmpeg yok")
    def test_broken_wav_is_kept(self):
        with open(self.wav, "wb") as f:
            f.write(b"bozuk dosya" * 100)
        e = self.engine()
        e._convert_one(self.wav)
        self.assertEqual(e.snapshot_counters()["errors"], 1)
        self.assertTrue(os.path.exists(self.wav))
        self.assertFalse(os.path.exists(self.flac))
        self.assertFalse(os.path.exists(self.flac + ".tmp"))


if __name__ == "__main__":
    unittest.main()
