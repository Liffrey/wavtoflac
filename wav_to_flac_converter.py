#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WAV -> FLAC Toplu Donusturucu
=============================

Milyonlarca WAV dosyasini, verilen bir tarihten (veya "N gunden eski")
esik degerine gore taşimadan/yer degistirmeden FLAC'a donusturen,
RAM ve hiz icin optimize edilmis PySide6 GUI araci.

Tasarim notlari (RAM / hiz optimizasyonu icin):
  - Dosya listesi HICBIR ZAMAN tamamen bellege alinmaz. Tarayici (scanner)
    thread'i os.scandir ile klasorleri gezer ve bulunan uygun dosyalari
    SINIRLI boyutlu (bounded) bir queue.Queue'ya koyar. Kuyruk dolduysa
    tarayici bekler (blocking put) -> bellek kullanimi worker sayisi ile
    sinirli kalir, dosya sayisi milyonlarca da olsa artmaz.
  - Donusturme islemi harici "ffmpeg" process'i ile yapilir (Python
    tarafinda ses verisini bellege yuklemez, streaming calisir).
  - Worker sayisi kadar paralel ffmpeg process'i calisir (coklu cekirdek).
  - GUI'ye HER dosya icin ayri Qt sinyali atilmaz (milyonlarca sinyal GUI'yi
    kilitler). Bunun yerine thread-safe bir buffer'a yazilir, ana thread'de
    calisan bir QTimer bu buffer'i periyodik (varsayilan 250ms) olarak
    okuyup ekrana basar. Ekrandaki log satiri sayisi da
    setMaximumBlockCount ile sinirlanir; tam log diske (rotating file)
    yazilir, hicbir kayit kaybolmaz.
  - "Surekli calis" modunda: kullanicinin verdigi gun sayisina gore esik
    (now - N gun) her turda yeniden hesaplanir, tur biter, belirlenen
    dakika kadar beklenir, tekrar taranir. Sonsuz dongu, Durdur ile
    guvenli sekilde kesilir (calismakta olan ffmpeg islemleri iptal
    edilmeden tamamlanir, boylece yarim/bozuk dosya olusmaz).

Gereksinimler:
  pip install PySide6
  ffmpeg kurulu ve PATH icinde olmali (https://ffmpeg.org/download.html)

Calistirma:
  python wav_to_flac_converter.py
"""

import os
import sys
import csv
import time
import queue
import shutil
import logging
import threading
import subprocess
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from PySide6 import QtCore, QtGui, QtWidgets

SETTINGS_ORG = "BugraSysadminTools"
SETTINGS_APP = "WavToFlacConverter"


# --------------------------------------------------------------------------
# ffmpeg konumunu bul (PyInstaller ile gomulmus onefile/onedir exe destegi)
# --------------------------------------------------------------------------

def get_ffmpeg_path():
    """ffmpeg calistirilabilirini su sirayla arar:
    1) PyInstaller onefile exe icinde gomulu oldugu gecici klasor (_MEIPASS)
    2) PyInstaller onedir exe'nin bulundugu klasor
    3) Bu script'in bulundugu klasor (gelistirme ortami)
    4) Sistem PATH'i
    Bulunamazsa None doner."""
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, ffmpeg_name))
        candidates.append(os.path.join(os.path.dirname(sys.executable), ffmpeg_name))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, ffmpeg_name))

    for c in candidates:
        if os.path.isfile(c):
            return c

    return shutil.which("ffmpeg")


def get_ffprobe_path():
    """ffprobe calistirilabilirini get_ffmpeg_path ile ayni mantikla arar.
    ffprobe, ffmpeg ile ayni klasorde gelir (ayni zip/build icinde)."""
    ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"

    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, ffprobe_name))
        candidates.append(os.path.join(os.path.dirname(sys.executable), ffprobe_name))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, ffprobe_name))

    for c in candidates:
        if os.path.isfile(c):
            return c

    return shutil.which("ffprobe")


def get_resource_path(filename):
    """PyInstaller ile gomulu kaynak dosyalarini (orn. ikon) bulur; gelistirme
    ortaminda script yanindaki dosyaya bakar."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = os.path.join(meipass, filename)
            if os.path.isfile(candidate):
                return candidate
        candidate = os.path.join(os.path.dirname(sys.executable), filename)
        if os.path.isfile(candidate):
            return candidate
    else:
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.isfile(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------
# Yardimci: dosya sistemi tarayici (jenerator degil, bounded queue'ya yazar)
# --------------------------------------------------------------------------

def iter_wav_entries(root, recursive, stop_event):
    """os.scandir ile iteratif (rekursiyon limiti olmadan) WAV dosyalarini
    dolasir. DirEntry.stat() cache'inden faydalanarak ekstra syscall'dan
    kacinir."""
    stack = [root]
    while stack:
        if stop_event.is_set():
            return
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if stop_event.is_set():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                stack.append(entry.path)
                            continue
                        if entry.is_file(follow_symlinks=False) and \
                                entry.name.lower().endswith(".wav"):
                            yield entry
                    except OSError:
                        # erisilemeyen tekil dosya/klasor - atla
                        continue
        except OSError:
            # erisilemeyen klasor (izin vb.) - atla
            continue


# --------------------------------------------------------------------------
# Donusturme motoru
# --------------------------------------------------------------------------

class ConversionEngine(QtCore.QObject):
    """Tarama + donusturme is akisini yoneten motor. QObject'tir cunku
    sinyalleri GUI thread'ine guvenli sekilde (queued connection) tasir,
    fakat agir islem asla GUI thread'inde calismaz."""

    pass_started = QtCore.Signal(str)      # cutoff aciklamasi
    pass_finished = QtCore.Signal()
    all_finished = QtCore.Signal()
    stats_changed = QtCore.Signal(dict)    # periyodik ozet (ana thread'den okunur)

    def __init__(self, logger):
        super().__init__()
        self.logger = logger

        self.root_path = ""
        self.recursive = True
        self.delete_original = False
        self.worker_count = max(1, os.cpu_count() or 4)
        self.compression_level = 5
        self.continuous = False
        self.days_threshold = 10
        self.interval_minutes = 60
        self.fixed_cutoff_ts = None  # tek seferlik mod icin
        self.ffmpeg_path = None
        self.ffprobe_path = None

        # Guvenlik / butunluk ayarlari
        self.verify_before_delete = True   # ffprobe ile sure karsilastirmasi
        self.preserve_timestamps = True    # orijinal mtime/atime'i FLAC'a aktar

        # Loglama ayarlari
        self.verbose_file_log = True     # her DONUSTURULDU/ATLANDI satirini goster
        self.summary_log_interval = 500  # her N islenen dosyada bir ozet satiri (her zaman)

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()  # set = duraklatildi
        self._control_thread = None

        self._lock = threading.Lock()
        self._log_buffer = []
        self._counters = {"scanned": 0, "converted": 0, "skipped": 0, "errors": 0}
        self._last_milestone = 0

        self.current_pass_start_ts = None

        # Hata CSV raporu
        self._csv_lock = threading.Lock()
        self._csv_path = None
        self._csv_file = None
        self._csv_writer = None

    # -------------------- disardan cagrilan API --------------------

    def start(self):
        self.stop_event.clear()
        self.pause_event.clear()
        self._control_thread = threading.Thread(target=self._run, daemon=True)
        self._control_thread.start()

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()  # duraklatilmis olsa bile durdurma aninda kilitlenmemesi icin

    def pause(self):
        self.pause_event.set()
        self._log("DURAKLATILDI (calisan islemler bitirilip yeni dosya alinmiyor).")

    def resume(self):
        self.pause_event.clear()
        self._log("DEVAM EDILIYOR.")

    def is_paused(self):
        return self.pause_event.is_set()

    def is_running(self):
        return self._control_thread is not None and self._control_thread.is_alive()

    def drain_log_buffer(self):
        """Ana thread'in QTimer'i tarafindan cagrilir; buffer'i bosaltip
        dondurur (kilitli, hizli islem)."""
        with self._lock:
            if not self._log_buffer:
                return []
            buf, self._log_buffer = self._log_buffer, []
            return buf

    def snapshot_counters(self):
        with self._lock:
            return dict(self._counters)

    def reset_counters(self):
        with self._lock:
            for k in self._counters:
                self._counters[k] = 0
            self._last_milestone = 0

    # -------------------- ic yardimcilar --------------------

    def _log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            self._log_buffer.append(line)
        # her zaman diske de yaz (kalici, tam log)
        self.logger.info(msg)

    def _inc(self, key, n=1):
        with self._lock:
            self._counters[key] += n

    def _compute_cutoff_ts(self):
        if self.continuous:
            return (datetime.now() - timedelta(days=self.days_threshold)).timestamp()
        return self.fixed_cutoff_ts

    # -------------------- hata CSV raporu --------------------

    def _open_error_csv(self):
        log_dir = os.path.join(os.path.expanduser("~"), ".wav2flac_logs")
        os.makedirs(log_dir, exist_ok=True)
        self._csv_path = os.path.join(log_dir, "hatalar.csv")
        is_new = not os.path.exists(self._csv_path) or os.path.getsize(self._csv_path) == 0
        try:
            self._csv_file = open(self._csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            if is_new:
                self._csv_writer.writerow(["Tarih_Saat", "Dosya_Yolu", "Hata_Turu", "Detay"])
                self._csv_file.flush()
        except OSError as e:
            self._log(f"HATA CSV DOSYASI ACILAMADI: {e}")
            self._csv_file = None
            self._csv_writer = None

    def _write_error_csv(self, path, error_type, detail):
        with self._csv_lock:
            if self._csv_writer is None:
                return
            try:
                self._csv_writer.writerow(
                    [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path, error_type, detail]
                )
                self._csv_file.flush()
            except Exception:
                pass

    def _close_error_csv(self):
        with self._csv_lock:
            if self._csv_file:
                try:
                    self._csv_file.close()
                except Exception:
                    pass
            self._csv_file = None
            self._csv_writer = None

    def get_error_csv_path(self):
        if self._csv_path:
            return self._csv_path
        return os.path.join(os.path.expanduser("~"), ".wav2flac_logs", "hatalar.csv")

    # -------------------- dogrulama (ffprobe) --------------------

    def _get_duration(self, path):
        if not self.ffprobe_path:
            return None
        try:
            result = subprocess.run(
                [self.ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            )
            text = result.stdout.decode("utf-8", errors="ignore").strip()
            if not text:
                return None
            return float(text)
        except Exception:
            return None

    def _verify_conversion(self, src_path, dst_path):
        """WAV ve FLAC surelerini karsilastirir. Esit degilse (toleransin
        disinda) False doner; boylece bozuk/eksik bir donusturme, orijinal
        WAV silinmeden once yakalanmis olur."""
        if not self.ffprobe_path:
            # ffprobe yoksa dogrulama fiziken yapilamaz; baslangicta zaten
            # uyarilmis olur, burada islemi durdurmamak icin gecerli sayilir.
            return True
        d_src = self._get_duration(src_path)
        d_dst = self._get_duration(dst_path)
        if d_src is None or d_dst is None:
            return False
        tolerance = max(0.05, d_src * 0.001)  # 50ms veya binde bir, hangisi buyukse
        return abs(d_src - d_dst) <= tolerance

    def _maybe_log_milestone(self):
        """Verbose kapali olsa bile 'kac dosya donusturuldu' bilgisini
        duzenli araliklarla CLI logda gostermek icin ozet satiri basar."""
        should_log = False
        counters_copy = None
        with self._lock:
            total = self._counters["converted"] + self._counters["skipped"] + self._counters["errors"]
            interval = max(1, self.summary_log_interval)
            if total - self._last_milestone >= interval:
                self._last_milestone = total
                should_log = True
                counters_copy = dict(self._counters)
        if should_log:
            self._log(
                f"OZET -> Taranan: {counters_copy['scanned']}  Donusturulen: {counters_copy['converted']}  "
                f"Atlanan: {counters_copy['skipped']}  Hata: {counters_copy['errors']}"
            )

    # -------------------- worker / scanner --------------------

    def _convert_one(self, src_path):
        dst_path = os.path.splitext(src_path)[0] + ".flac"

        if os.path.exists(dst_path):
            self._inc("skipped")
            if self.verbose_file_log:
                self._log(f"ATLANDI (FLAC zaten var): {src_path}")
            self._maybe_log_milestone()
            return

        ffmpeg_bin = self.ffmpeg_path or "ffmpeg"
        cmd = [
            ffmpeg_bin, "-y", "-nostdin", "-loglevel", "error",
            "-i", src_path,
            "-c:a", "flac", "-compression_level", str(self.compression_level),
            dst_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            self._inc("errors")
            self._log("HATA: ffmpeg bulunamadi. PATH ayarlarini veya ffmpeg.exe konumunu kontrol edin.")
            self._write_error_csv(src_path, "ffmpeg_bulunamadi", "ffmpeg calistirilamadi")
            self.stop_event.set()
            self._maybe_log_milestone()
            return
        except Exception as e:
            self._inc("errors")
            self._log(f"BEKLENMEYEN HATA: {src_path} - {e}")
            self._write_error_csv(src_path, "beklenmeyen_hata", str(e))
            self._maybe_log_milestone()
            return

        conversion_ok = (
            result.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0
        )

        if not conversion_ok:
            self._inc("errors")
            err = result.stderr.decode("utf-8", errors="ignore").strip()
            # hatalar verbose ayarindan bagimsiz olarak her zaman gosterilir
            self._log(f"HATA: {src_path} - {err[:300]}")
            self._write_error_csv(src_path, "donusturme_hatasi", err[:500])
            if os.path.exists(dst_path):
                try:
                    os.remove(dst_path)
                except OSError:
                    pass
            self._maybe_log_milestone()
            return

        # ffmpeg basariyla bitti. Dogrulama SADECE orijinal silinecekse anlamlidir
        # (amaci, silmeden once bozuk bir donusturmeyi yakalamaktir); silme kapaliysa
        # dogrulamayi calistirmiyoruz - hem gereksiz ffprobe yukunu onler hem de
        # gecerli bir FLAC'in sinirda bir sure farkiyla bosuna silinmesini engeller.
        verified_ok = True
        if self.delete_original and self.verify_before_delete:
            verified_ok = self._verify_conversion(src_path, dst_path)

        if not verified_ok:
            self._inc("errors")
            self._log(f"DOGRULAMA HATASI (sure uyusmuyor, silinmedi): {src_path}")
            self._write_error_csv(
                src_path, "dogrulama_hatasi",
                "FLAC suresi WAV ile eslesmiyor; olusturulan FLAC silindi, orijinal WAV korundu."
            )
            if os.path.exists(dst_path):
                try:
                    os.remove(dst_path)
                except OSError:
                    pass
            self._maybe_log_milestone()
            return

        # basarili + (varsa) dogrulandi
        self._inc("converted")

        if self.preserve_timestamps:
            try:
                st = os.stat(src_path)
                os.utime(dst_path, (st.st_atime, st.st_mtime))
            except OSError as e:
                self._log(f"ZAMAN DAMGASI HATASI: {dst_path} - {e}")
                self._write_error_csv(dst_path, "zaman_damgasi_hatasi", str(e))

        if self.verbose_file_log:
            self._log(f"DONUSTURULDU: {src_path}")

        if self.delete_original:
            try:
                os.remove(src_path)
                if self.verbose_file_log:
                    self._log(f"SILINDI (orijinal): {src_path}")
            except OSError as e:
                # silme hatasi onemli - verbose kapali olsa da her zaman goster
                self._log(f"SILME HATASI: {src_path} - {e}")
                self._write_error_csv(src_path, "silme_hatasi", str(e))

        self._maybe_log_milestone()

    def _worker_loop(self, file_queue):
        while True:
            item = file_queue.get()
            try:
                if item is None:
                    return
                # Duraklatma: kuyruktan aldiktan sonra, islemeden once bekle.
                # Bu sirada tarayici (scanner) kuyruk dolu oldugu icin kendiliginden
                # duracaktir (bounded queue), ekstra senkronizasyon gerekmez.
                while self.pause_event.is_set() and not self.stop_event.is_set():
                    time.sleep(0.2)
                if self.stop_event.is_set():
                    continue
                # Guvenlik agi: _convert_one icinde beklenmeyen (kod hatasi, isletim
                # sistemi kaynakli TOCTOU durumu, izin/antivirus engeli vb.) bir
                # exception olusursa bile bu worker thread'i OLMEMELI ve dosya
                # sessizce sayaclarin/loglarin/CSV'nin disinda kalmamali. Aksi halde
                # worker sessizce cokup kalan turde paralellik dusurur ve o tek
                # dosya hicbir yerde raporlanmadan kaybolur.
                try:
                    self._convert_one(item)
                except Exception as e:
                    self._inc("errors")
                    self._log(f"BEKLENMEYEN IC HATA (worker cokmedi, devam ediyor): {item} - {e}")
                    self._write_error_csv(item, "beklenmeyen_ic_hata", str(e))
                    self._maybe_log_milestone()
            finally:
                file_queue.task_done()

    def _scanner_loop(self, file_queue, cutoff_ts):
        # try/finally KRITIK: tarama sirasinda beklenmedik (OSError disi) bir
        # hata olursa bile worker'lara bitis sinyali (None) MUTLAKA gonderilmeli.
        # Aksi halde worker'lar sonsuza kadar file_queue.get() icinde bloke kalir
        # ve bu bloke olan cagriyi stop_event de kesemez -> kalici donma olusur,
        # Durdur butonu bile calismaz hale gelir.
        try:
            for entry in iter_wav_entries(self.root_path, self.recursive, self.stop_event):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_mtime < cutoff_ts:
                    self._inc("scanned")
                    file_queue.put(entry.path)  # dolu ise burada bekler (bounded)
        except Exception as e:
            self._log(f"TARAMA HATASI (beklenmeyen, tarama erken sonlandirildi): {e}")
        finally:
            for _ in range(self.worker_count):
                file_queue.put(None)

    # -------------------- ana kontrol dongusu --------------------

    def _run(self):
        self._open_error_csv()
        try:
            while not self.stop_event.is_set():
                cutoff_ts = self._compute_cutoff_ts()
                cutoff_str = datetime.fromtimestamp(cutoff_ts).strftime("%Y-%m-%d %H:%M")
                self.reset_counters()
                self.current_pass_start_ts = time.time()
                self.pass_started.emit(cutoff_str)
                self._log(f"Tarama basladi. Esik tarih: {cutoff_str} (bu tarihten eski dosyalar donusturulecek)")

                file_queue = queue.Queue(maxsize=self.worker_count * 50)
                workers = [
                    threading.Thread(target=self._worker_loop, args=(file_queue,), daemon=True)
                    for _ in range(self.worker_count)
                ]
                for w in workers:
                    w.start()

                scanner = threading.Thread(
                    target=self._scanner_loop, args=(file_queue, cutoff_ts), daemon=True
                )
                scanner.start()
                scanner.join()
                for w in workers:
                    w.join()

                c = self.snapshot_counters()
                # tur sonu ozeti - verbose ayarindan bagimsiz, her zaman gosterilir
                self._log(
                    f"TUR TAMAMLANDI. Taranan: {c['scanned']}  Donusturulen: {c['converted']}  "
                    f"Atlanan: {c['skipped']}  Hata: {c['errors']}"
                )
                self.pass_finished.emit()

                if not self.continuous or self.stop_event.is_set():
                    break

                self._log(f"Sonraki tur icin {self.interval_minutes} dakika bekleniyor...")
                waited = 0
                while waited < self.interval_minutes * 60 and not self.stop_event.is_set():
                    time.sleep(1)
                    waited += 1
        except Exception as e:
            # Kritik guvenlik agi: --windowed olarak paketlenmis exe'de konsol
            # OLMADIGI icin yakalanmayan bir exception hicbir yerde gorunmeden
            # sessizce kaybolur. Burada yakalayip GUI log paneline (kullanicinin
            # gorebilecegi TEK yer) acikca yaziyoruz.
            self._log(f"KRITIK HATA (islem beklenmedik sekilde durdu): {e}")
        finally:
            self._log("Islem durduruldu.")
            self._close_error_csv()
            self.all_finished.emit()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

CONSOLE_STYLE = """
QPlainTextEdit {
    background-color: #0c0f0a;
    color: #7CFC7C;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 11px;
    border: 1px solid #333;
}
"""

SETTINGS_HELP_HTML = """
<style>
  body { font-family: Segoe UI, Arial, sans-serif; font-size: 13px; }
  h3 { color: #1a6b7a; margin-bottom: 2px; }
  p  { margin-top: 2px; margin-bottom: 12px; }
  code { background:#eee; padding:1px 4px; border-radius:3px; }
</style>

<h3>Klasor</h3>
<p>WAV dosyalarinin bulundugu kok klasor. Alt klasorlerde de arama yapmasi
icin <b>"Alt klasorleri de tara"</b> kutucugunu isaretli birakin.</p>

<h3>Calisma Modu &ndash; Tek seferlik</h3>
<p>Sectiginiz <b>tarihten daha eski</b> (dosyanin son degistirilme tarihine
gore) tum WAV dosyalarini bir kez tarar, donusturur ve islem biter.
Ornegin 01.01.2025 secerseniz, o tarihten once degistirilmis WAV'lar
islenir; daha yeni dosyalara dokunulmaz.</p>

<h3>Calisma Modu &ndash; Surekli calis</h3>
<p>Sabit bir tarih yerine <b>"su gunden eski"</b> mantigiyla calisir.
Ornegin "10 gun" girerseniz, program her turda "su an - 10 gun" esigini
yeniden hesaplar ve o tarihten eski tum dosyalari donusturur. Bir tur
bitince <b>Tarama araligi</b> kadar bekler, sonra esigi yeniden hesaplayip
tekrar tarar. Bu, sonsuz bir dongudur; siz <b>Durdur</b> butonuna basana
kadar calismaya devam eder. Sunucu her zaman acik kalip surekli yeni
gelen dosyalari belirli bir yastan sonra otomatik sikistirmak icin
idealdir.</p>

<h3>Kaynak (orijinal) WAV dosyalarini sil</h3>
<p>Isaretlenirse, bir dosya <b>basariyla</b> FLAC'a donusturuldukten
(ffmpeg sifir hata koduyla bitip cikti dosyasi dogrulandiktan) SONRA
orijinal WAV dosyasi diskten silinir. Donusturme basarisiz olursa
orijinal dosyaya dokunulmaz. Isaretli degilse WAV dosyalari FLAC'in
yaninda oldugu gibi kalir (disk alani kazanci olmaz, sadece FLAC kopyasi
da olusur).</p>

<h3>Paralel islem (worker sayisi)</h3>
<p>Ayni anda kac dosyanin <b>eszamanli</b> olarak ffmpeg ile
donusturulecegini belirler; her worker kendi ffmpeg surecini calistirir.
Varsayilan deger CPU cekirdek sayisidir.</p>
<ul>
<li>Yerel SSD'de calisiyorsaniz: cekirdek sayisi civari (hatta biraz
ustu) verimlidir.</li>
<li>Ag paylasimi (SMB/NFS) veya yavas disk kullaniyorsaniz: daha dusuk
bir deger (orn. 4-8) disk kuyrugunun tikanmasini onleyip genelde daha
hizli sonuc verir.</li>
</ul>
<p>Emin degilseniz kucuk bir ornek grupla (birkac bin dosya) farkli
degerleri deneyip sunucunuzdaki gercek darbogazi (CPU mu disk mi)
gormeniz onerilir.</p>

<h3>Sikistirma (0-8)</h3>
<p>ffmpeg'in FLAC sikistirma seviyesidir. FLAC her zaman <b>kayipsizdir</b>
(lossless) &ndash; hangi seviyeyi secerseniz secin ses kalitesinde HICBIR
fark olmaz; sadece dosya boyutu ve donusturme suresi degisir.</p>
<ul>
<li><b>0</b>: en hizli, en az sikistirma (en buyuk dosya)</li>
<li><b>8</b>: en yavas, en fazla sikistirma (en kucuk dosya)</li>
</ul>
<p>0 ile 8 arasindaki boyut farki genelde sadece %5-10 civarindadir, ama
8. seviye 0. seviyeye gore birkac kat daha uzun surebilir. Milyonlarca
dosyada <b>2-5 arasi</b> onerilir; depolama alani kritikse daha yuksek
secilebilir ama toplam sure ciddi artar.</p>

<h3>Tum dosya hareketlerini logla</h3>
<p>Acikken her donusturulen/atlanan/silinen dosya icin ayri bir log satiri
yazilir (orn. <code>DONUSTURULDU: C:\\...\\ornek.wav</code>). Milyonlarca
dosyada bu, ekran performansini ve okunabilirligi dusurebilir.</p>
<p>Kapatildiginda bu tekil satirlar yazilmaz; ancak <b>hatalar her zaman
gosterilir</b> ve asagidaki "Ozet log araligi" ayari sayesinde ilerleme
duzenli olarak goruntulenmeye devam eder.</p>

<h3>Ozet log araligi</h3>
<p>Kac dosya islendiginde ("Taranan / Donusturulen / Atlanan / Hata")
ozet satirinin log ekranina yazilacagini belirler. Bu satirlar "Tum
dosya hareketlerini logla" KAPALI olsa bile her zaman gosterilir, boylece
"kac dosya donusturuldu" bilgisini her zaman takip edebilirsiniz. Ayrica
her turun sonunda da otomatik olarak bir TUR TAMAMLANDI ozeti yazilir.</p>

<h3>Diskteki tam log dosyasi</h3>
<p>Ekrandaki log gosterimi belli bir satir sayisiyla sinirlidir (bellek
icin), ancak <b>tum</b> islemler ayrica diske de yazilir:
<code>%USERPROFILE%\\.wav2flac_logs\\wav2flac.log</code> (donen/rotating
dosya). Bu klasoru Yardim menusundeki "Log Klasorunu Ac" ile
acabilirsiniz.</p>

<h3>Silmeden once FLAC'i dogrula</h3>
<p><b>SADECE "Kaynak (orijinal) WAV dosyalarini sil" isaretliyken etkilidir</b>
(diger durumda kutucuk otomatik olarak devre disi/gri gorunur, cunku
zaten silinecek bir sey yoktur). Isaretliyse, ffmpeg basariyla bitse bile
silme islemi hemen yapilmaz: program <b>ffprobe</b> ile WAV ve olusan
FLAC dosyalarinin sureleri karsilastirir. Sureler uyusmuyorsa (bozuk/eksik
donusturme durumu), olusan hatali FLAC dosyasi silinir, <b>orijinal WAV
korunur</b> ve durum hata olarak kaydedilir. Bu, geri donusu olmayan veri
kaybina karsi ek bir guvenlik katmanidir. ffprobe bulunamazsa bu ozellik
calisamaz; Baslat'a bastiginizda sizi uyarir ve dogrulamayi o calisma
icin devre disi birakir.</p>

<h3>Orijinal zaman damgasini FLAC'a aktar</h3>
<p>Isaretliyse, olusturulan FLAC dosyasinin degistirilme/erisim tarihi
orijinal WAV dosyasiyla ayni yapilir (dosya "yeni olusturulmus" gibi
gorunmez, arsiv/denetim tarihini korur). Kapatilirsa FLAC dosyasi kendi
olusturulma anindaki tarihi tasir.</p>

<h3>Duraklat / Devam Et</h3>
<p><b>Duraklat</b>'a basildiginda, o an isleme alinmamis dosyalar icin
yeni islem baslatilmaz; halihazirda calismakta olan ffmpeg islemleri
kesilmeden tamamlanir (boylece yarim dosya olusmaz). Tarama islemi de
kuyruk dolacagi icin kendiliginden durur. <b>Devam Et</b>'e basildiginda
kaldigi yerden devam eder. <b>Durdur</b> ise duraklatmadan farkli olarak
mevcut turu tamamen sonlandirir ve (surekli modda) sonraki turlari
baslatmaz.</p>

<h3>Hiz / Tahmini Sure gostergesi</h3>
<p>Istatistiklerin altinda gosterilen "Hiz" degeri, son birkac saniyedeki
islenen dosya sayisina bakarak hesaplanan dosya/saniye degeridir.
"Tahmini kalan sure" ise SADECE o ana kadar taranmis ama henuz islenmemis
dosyalar (bilinen kuyruk) icin bir tahmindir; tarama devam ettigi surece
gercek toplam dosya sayisi onceden bilinmedigi icin (bu, milyonlarca
dosyada bellek tasarrufu saglayan tasarimin bir sonucudur) bu deger
zamanla degisebilir/guncellenebilir.</p>

<h3>Hata Raporu (CSV)</h3>
<p>Donusturme, dogrulama veya silme sirasinda olusan HER hata otomatik
olarak <code>%USERPROFILE%\\.wav2flac_logs\\hatalar.csv</code> dosyasina
(tarih, dosya yolu, hata turu, detay sutunlariyla) satir satir eklenir.
Bu dosyayi Yardim menusundeki "Hata Raporunu (CSV) Ac" ile Excel/Notepad
gibi bir programda acabilirsiniz. Islem devam ederken de dosya surekli
guncellenir, ayrica bir "disa aktarma" adimina gerek yoktur.</p>

<h3>Ayarlarin hatirlanmasi</h3>
<p>Girdiginiz klasor, mod, tarih/gun, worker sayisi, sikistirma seviyesi
ve tum diger secenekler uygulama kapatildiginda otomatik kaydedilir;
bir sonraki acilista kaldiginiz yerden devam edersiniz. Ayri bir "kaydet"
islemi yapmaniza gerek yoktur.</p>
"""



class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WAV -> FLAC Toplu Donusturucu")
        self.setMinimumWidth(620)

        icon_path = get_resource_path("app_icon.ico")
        if icon_path:
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self.logger = self._build_logger()
        self.engine = ConversionEngine(self.logger)
        self.engine.pass_started.connect(self._on_pass_started)
        self.engine.pass_finished.connect(self._on_pass_finished)
        self.engine.all_finished.connect(self._on_all_finished)

        self._rate_samples = deque(maxlen=20)  # (zaman, islenen_sayisi) - hiz/ETA hesabi icin

        self._build_menu()
        self._build_ui()
        self._load_settings()

        # GUI'ye toplu (batched) log/istatistik aktarimi icin timer
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.setInterval(250)
        self.ui_timer.timeout.connect(self._flush_ui)
        self.ui_timer.start()

        self._check_ffmpeg()
        self._check_ffprobe()

    # ---------------- menu ----------------

    def _build_menu(self):
        help_menu = self.menuBar().addMenu("&Yardım")

        settings_action = QtGui.QAction("Ayarlar Rehberi...", self)
        settings_action.setShortcut("F1")
        settings_action.triggered.connect(self._show_settings_help)
        help_menu.addAction(settings_action)

        help_menu.addSeparator()

        log_folder_action = QtGui.QAction("Log Klasorunu Ac", self)
        log_folder_action.triggered.connect(self._open_log_folder)
        help_menu.addAction(log_folder_action)

        error_csv_action = QtGui.QAction("Hata Raporunu (CSV) Ac", self)
        error_csv_action.triggered.connect(self._open_error_csv_file)
        help_menu.addAction(error_csv_action)

        help_menu.addSeparator()

        about_action = QtGui.QAction("Hakkinda...", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _open_log_folder(self):
        log_dir = os.path.join(os.path.expanduser("~"), ".wav2flac_logs")
        os.makedirs(log_dir, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(log_dir))

    def _open_error_csv_file(self):
        csv_path = self.engine.get_error_csv_path()
        log_dir = os.path.dirname(csv_path)
        os.makedirs(log_dir, exist_ok=True)
        if not os.path.exists(csv_path):
            QtWidgets.QMessageBox.information(
                self, "Hata raporu bulunamadi",
                "Henuz bir hata kaydi olusmadi. Islem calistiginda hatalar "
                f"burada birikecek:\n{csv_path}",
            )
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(csv_path))

    def _show_about(self):
        QtWidgets.QMessageBox.about(
            self,
            "Hakkinda",
            "<b>WAV &rarr; FLAC Toplu Donusturucu</b><br><br>"
            "Buyuk hacimli WAV arsivlerini kayipsiz FLAC formatina donusturmek "
            "icin RAM ve hiz odakli tasarlanmis bir arac. Bugra <br><br>"
            "Donusturme motoru: ffmpeg",
        )

    def _show_settings_help(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Ayarlar Rehberi")
        dlg.resize(620, 560)
        v = QtWidgets.QVBoxLayout(dlg)

        text = QtWidgets.QTextBrowser()
        text.setOpenExternalLinks(True)
        text.setHtml(SETTINGS_HELP_HTML)
        v.addWidget(text)

        btn = QtWidgets.QPushButton("Kapat")
        btn.clicked.connect(dlg.accept)
        v.addWidget(btn, alignment=QtCore.Qt.AlignRight)

        dlg.exec()

    # ---------------- logger ----------------

    def _append_startup_log(self, msg):
        self.log_view.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _build_logger(self):
        logger = logging.getLogger("wav2flac")
        logger.setLevel(logging.INFO)
        log_dir = os.path.join(os.path.expanduser("~"), ".wav2flac_logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, "wav2flac.log"),
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger

    # ---------------- ffmpeg kontrolu ----------------

    def _check_ffmpeg(self):
        path = get_ffmpeg_path()
        self.engine.ffmpeg_path = path
        if path is None:
            self._append_startup_log("UYARI: ffmpeg bulunamadi. Donusturme islemi calismayacaktir.")
            QtWidgets.QMessageBox.warning(
                self,
                "ffmpeg bulunamadi",
                "ffmpeg ne exe ile birlikte (gomulu) ne de PATH icinde bulunamadi.\n"
                "Donusturme islemi calismayacaktir.\n\n"
                "Bu exe ffmpeg ile birlikte paketlenmediyse, ffmpeg'i kurup PATH'e "
                "ekleyin (https://ffmpeg.org/download.html).",
            )
        else:
            self._append_startup_log(f"ffmpeg bulundu: {path}")

    def _check_ffprobe(self):
        path = get_ffprobe_path()
        self.engine.ffprobe_path = path
        if path is None:
            self._append_startup_log(
                "UYARI: ffprobe bulunamadi. 'Silmeden once dogrula' secenegi "
                "kullanilamayacak (ffmpeg donusturmesi bundan etkilenmez)."
            )
        else:
            self._append_startup_log(f"ffprobe bulundu: {path}")

    # ---------------- UI ----------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setSpacing(8)

        # --- Klasor secimi ---
        path_row = QtWidgets.QHBoxLayout()
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setPlaceholderText("WAV dosyalarinin bulundugu klasor...")
        browse_btn = QtWidgets.QPushButton("Gozat...")
        browse_btn.clicked.connect(self._browse_folder)
        path_row.addWidget(QtWidgets.QLabel("Klasor:"))
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self.recursive_chk = QtWidgets.QCheckBox("Alt klasorleri de tara")
        self.recursive_chk.setChecked(True)
        layout.addWidget(self.recursive_chk)

        # --- Mod secimi ---
        mode_group = QtWidgets.QGroupBox("Calisma Modu")
        mode_layout = QtWidgets.QVBoxLayout(mode_group)

        self.mode_once_radio = QtWidgets.QRadioButton("Tek seferlik: belirli tarihten eski dosyalari donustur")
        self.mode_continuous_radio = QtWidgets.QRadioButton("Surekli calis: X gunden eski dosyalari periyodik olarak donustur")
        self.mode_once_radio.setChecked(True)
        mode_layout.addWidget(self.mode_once_radio)

        once_row = QtWidgets.QHBoxLayout()
        once_row.addSpacing(20)
        once_row.addWidget(QtWidgets.QLabel("Tarih:"))
        self.date_edit = QtWidgets.QDateEdit(calendarPopup=True)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        self.date_edit.setDate(QtCore.QDate.currentDate().addDays(-30))
        once_row.addWidget(self.date_edit)
        once_row.addStretch(1)
        mode_layout.addLayout(once_row)

        mode_layout.addWidget(self.mode_continuous_radio)

        cont_row = QtWidgets.QHBoxLayout()
        cont_row.addSpacing(20)
        cont_row.addWidget(QtWidgets.QLabel("Su gunden eski dosyalar:"))
        self.days_spin = QtWidgets.QSpinBox()
        self.days_spin.setRange(1, 3650)
        self.days_spin.setValue(10)
        self.days_spin.setSuffix(" gun")
        cont_row.addWidget(self.days_spin)
        cont_row.addSpacing(15)
        cont_row.addWidget(QtWidgets.QLabel("Tarama araligi:"))
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(60)
        self.interval_spin.setSuffix(" dk")
        cont_row.addWidget(self.interval_spin)
        cont_row.addStretch(1)
        mode_layout.addLayout(cont_row)

        layout.addWidget(mode_group)

        self.mode_once_radio.toggled.connect(self._update_mode_widgets)
        self._update_mode_widgets()

        # --- Secenekler ---
        opts_group = QtWidgets.QGroupBox("Secenekler")
        opts_layout = QtWidgets.QHBoxLayout(opts_group)

        self.delete_chk = QtWidgets.QCheckBox("Donusturulen orijinal WAV dosyalarini sil")
        opts_layout.addWidget(self.delete_chk)
        opts_layout.addSpacing(15)
        opts_layout.addWidget(QtWidgets.QLabel("Paralel islem:"))
        self.workers_spin = QtWidgets.QSpinBox()
        self.workers_spin.setRange(1, 64)
        self.workers_spin.setValue(max(1, os.cpu_count() or 4))
        opts_layout.addWidget(self.workers_spin)

        opts_layout.addSpacing(15)
        opts_layout.addWidget(QtWidgets.QLabel("Sikistirma (0-8):"))
        self.compression_spin = QtWidgets.QSpinBox()
        self.compression_spin.setRange(0, 8)
        self.compression_spin.setValue(5)
        opts_layout.addWidget(self.compression_spin)

        opts_layout.addStretch(1)
        layout.addWidget(opts_group)

        # --- Guvenlik / Butunluk ---
        safety_group = QtWidgets.QGroupBox("Guvenlik / Butunluk")
        safety_layout = QtWidgets.QHBoxLayout(safety_group)

        self.verify_chk = QtWidgets.QCheckBox("Silmeden once FLAC'i dogrula (ffprobe ile sure karsilastirmasi)")
        self.verify_chk.setChecked(True)
        self.verify_chk.setToolTip(
            "SADECE 'Kaynak WAV dosyalarini sil' isaretliyken etkilidir. ffmpeg "
            "basariyla bitse bile, WAV ve FLAC surelerini ffprobe ile karsilastirarak "
            "bozuk/eksik donusturmeleri yakalar. Uyusmazlik varsa hatali FLAC silinir, "
            "orijinal WAV KORUNUR ve hata olarak kaydedilir. Her dosya icin ekstra bir "
            "islem gerektirdiginden hafif bir hiz maliyeti vardir. Silme kapaliyken "
            "zaten silinecek bir sey olmadigindan bu adim calistirilmaz."
        )
        safety_layout.addWidget(self.verify_chk)

        safety_layout.addSpacing(15)
        self.preserve_ts_chk = QtWidgets.QCheckBox("Orijinal zaman damgasini FLAC'a aktar")
        self.preserve_ts_chk.setChecked(True)
        self.preserve_ts_chk.setToolTip(
            "Isaretliyse, olusturulan FLAC dosyasinin degistirilme/erisim "
            "tarihi orijinal WAV dosyasiyla ayni yapilir (arsiv/denetim "
            "amacli tarih bilgisini korur)."
        )
        safety_layout.addWidget(self.preserve_ts_chk)

        safety_layout.addStretch(1)
        layout.addWidget(safety_group)

        self.delete_chk.toggled.connect(self._update_verify_enabled)
        self._update_verify_enabled()

        # --- Loglama secenekleri ---
        log_group = QtWidgets.QGroupBox("Loglama")
        log_layout = QtWidgets.QHBoxLayout(log_group)

        self.verbose_log_chk = QtWidgets.QCheckBox("Tum dosya hareketlerini logla (DONUSTURULDU / ATLANDI satirlari)")
        self.verbose_log_chk.setChecked(True)
        self.verbose_log_chk.setToolTip(
            "Kapatirsaniz her dosya icin ayri satir yazilmaz (milyonlarca dosyada "
            "ekran/performans icin onerilir). Hatalar ve ozet sayimlar her zaman gosterilir."
        )
        log_layout.addWidget(self.verbose_log_chk)

        log_layout.addSpacing(15)
        log_layout.addWidget(QtWidgets.QLabel("Ozet log araligi:"))
        self.summary_interval_spin = QtWidgets.QSpinBox()
        self.summary_interval_spin.setRange(1, 1000000)
        self.summary_interval_spin.setValue(500)
        self.summary_interval_spin.setSuffix(" dosyada bir")
        self.summary_interval_spin.setToolTip(
            "Kac dosya islendiginde 'Taranan/Donusturulen/Atlanan/Hata' ozet satiri "
            "yazilacagini belirler. Bu satirlar 'Tum dosya hareketlerini logla' "
            "kapali olsa bile her zaman gosterilir."
        )
        log_layout.addWidget(self.summary_interval_spin)

        log_layout.addStretch(1)
        layout.addWidget(log_group)

        # --- Baslat / Durdur / Duraklat ---
        btn_row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Baslat")
        self.start_btn.clicked.connect(self._on_start)
        self.pause_btn = QtWidgets.QPushButton("Duraklat")
        self.pause_btn.clicked.connect(self._on_pause_toggle)
        self.pause_btn.setEnabled(False)
        self.stop_btn = QtWidgets.QPushButton("Durdur")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        # --- Istatistikler ---
        self.stats_label = QtWidgets.QLabel("Taranan: 0   Donusturulen: 0   Atlanan: 0   Hata: 0")
        layout.addWidget(self.stats_label)

        self.speed_label = QtWidgets.QLabel("")
        self.speed_label.setStyleSheet("color: #555;")
        layout.addWidget(self.speed_label)

        # --- Log konsolu ---
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)  # GUI bellegini sinirla
        self.log_view.setStyleSheet(CONSOLE_STYLE)
        layout.addWidget(self.log_view, 1)

        self.resize(680, 560)

    def _update_mode_widgets(self):
        once = self.mode_once_radio.isChecked()
        self.date_edit.setEnabled(once)
        self.days_spin.setEnabled(not once)
        self.interval_spin.setEnabled(not once)

    def _update_verify_enabled(self):
        # Dogrulama sadece silme acikken anlamlidir (bkz. verify_chk tooltip)
        delete_on = self.delete_chk.isChecked()
        self.verify_chk.setEnabled(delete_on)
        if not delete_on:
            self.verify_chk.setToolTip(
                "Etkin degil: 'Kaynak WAV dosyalarini sil' isaretli olmadigi surece "
                "dogrulamanin bir etkisi yok, zaten silinecek bir sey yok."
            )
        else:
            self.verify_chk.setToolTip(
                "ffmpeg basariyla bitse bile, WAV ve FLAC surelerini ffprobe ile "
                "karsilastirarak bozuk/eksik donusturmeleri yakalar. Uyusmazlik "
                "varsa hatali FLAC silinir, orijinal WAV KORUNUR ve hata olarak "
                "kaydedilir. Her dosya icin ekstra bir islem gerektirdiginden "
                "hafif bir hiz maliyeti vardir."
            )

    def _browse_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Klasor sec")
        if d:
            self.path_edit.setText(d)

    # ---------------- ayarlari hatirlama (QSettings) ----------------

    def _save_settings(self):
        s = QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)
        s.setValue("path", self.path_edit.text())
        s.setValue("recursive", self.recursive_chk.isChecked())
        s.setValue("mode_continuous", self.mode_continuous_radio.isChecked())
        s.setValue("date", self.date_edit.date().toString("yyyy-MM-dd"))
        s.setValue("days", self.days_spin.value())
        s.setValue("interval", self.interval_spin.value())
        s.setValue("delete_original", self.delete_chk.isChecked())
        s.setValue("workers", self.workers_spin.value())
        s.setValue("compression", self.compression_spin.value())
        s.setValue("verify_before_delete", self.verify_chk.isChecked())
        s.setValue("preserve_timestamps", self.preserve_ts_chk.isChecked())
        s.setValue("verbose_log", self.verbose_log_chk.isChecked())
        s.setValue("summary_interval", self.summary_interval_spin.value())

    def _load_settings(self):
        s = QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)
        if s.contains("path"):
            self.path_edit.setText(s.value("path", "", type=str))
        self.recursive_chk.setChecked(s.value("recursive", True, type=bool))

        is_continuous = s.value("mode_continuous", False, type=bool)
        self.mode_continuous_radio.setChecked(is_continuous)
        self.mode_once_radio.setChecked(not is_continuous)

        date_str = s.value("date", "", type=str)
        if date_str:
            qd = QtCore.QDate.fromString(date_str, "yyyy-MM-dd")
            if qd.isValid():
                self.date_edit.setDate(qd)

        self.days_spin.setValue(s.value("days", 10, type=int))
        self.interval_spin.setValue(s.value("interval", 60, type=int))
        self.delete_chk.setChecked(s.value("delete_original", False, type=bool))
        self.workers_spin.setValue(s.value("workers", max(1, os.cpu_count() or 4), type=int))
        self.compression_spin.setValue(s.value("compression", 5, type=int))
        self.verify_chk.setChecked(s.value("verify_before_delete", True, type=bool))
        self.preserve_ts_chk.setChecked(s.value("preserve_timestamps", True, type=bool))
        self.verbose_log_chk.setChecked(s.value("verbose_log", True, type=bool))
        self.summary_interval_spin.setValue(s.value("summary_interval", 500, type=int))

        self._update_mode_widgets()

    # ---------------- start / stop ----------------

    def _on_start(self):
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QtWidgets.QMessageBox.warning(self, "Gecersiz klasor", "Lutfen gecerli bir klasor secin.")
            return
        ffmpeg_path = get_ffmpeg_path()
        if ffmpeg_path is None:
            QtWidgets.QMessageBox.critical(self, "ffmpeg bulunamadi", "ffmpeg (gomulu veya PATH'te) bulunamadi.")
            return

        # Dogrulama sadece silme acikken anlamlidir (engine tarafinda da ayni
        # kural uygulanir); silme kapaliyken gereksiz ffprobe uyarisi cikmasin.
        verify_requested = self.verify_chk.isChecked() and self.delete_chk.isChecked()
        ffprobe_path = get_ffprobe_path()
        if verify_requested and ffprobe_path is None:
            resp = QtWidgets.QMessageBox.warning(
                self, "ffprobe bulunamadi",
                "Dogrulama secili ama ffprobe bulunamadi (genelde ffmpeg ile ayni "
                "klasorde gelir). Dogrulama bu calisma icin KAPATILACAK, ffmpeg "
                "donusturmesi normal sekilde devam edecek.\n\nDevam edilsin mi?",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel,
            )
            if resp != QtWidgets.QMessageBox.Ok:
                return
            verify_requested = False

        self.engine.root_path = root
        self.engine.recursive = self.recursive_chk.isChecked()
        self.engine.delete_original = self.delete_chk.isChecked()
        self.engine.worker_count = self.workers_spin.value()
        self.engine.compression_level = self.compression_spin.value()
        self.engine.continuous = self.mode_continuous_radio.isChecked()
        self.engine.days_threshold = self.days_spin.value()
        self.engine.interval_minutes = self.interval_spin.value()
        self.engine.ffmpeg_path = ffmpeg_path
        self.engine.ffprobe_path = ffprobe_path
        self.engine.verify_before_delete = verify_requested
        self.engine.preserve_timestamps = self.preserve_ts_chk.isChecked()
        self.engine.verbose_file_log = self.verbose_log_chk.isChecked()
        self.engine.summary_log_interval = self.summary_interval_spin.value()

        if not self.engine.continuous:
            qd = self.date_edit.date()
            cutoff_dt = datetime(qd.year(), qd.month(), qd.day())
            self.engine.fixed_cutoff_ts = cutoff_dt.timestamp()

        self._save_settings()

        self.log_view.clear()
        self._append_log(f"Baslatildi. Klasor: {root}")

        self._rate_samples.clear()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Duraklat")
        self._set_inputs_enabled(False)

        self.engine.start()

    def _on_stop(self):
        self._append_log("Durdurma istegi gonderildi, mevcut islemler tamamlanip duracak...")
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.engine.stop()

    def _on_pause_toggle(self):
        if self.engine.is_paused():
            self.engine.resume()
            self.pause_btn.setText("Duraklat")
        else:
            self.engine.pause()
            self.pause_btn.setText("Devam Et")

    def _set_inputs_enabled(self, enabled):
        for w in (
            self.path_edit, self.recursive_chk, self.mode_once_radio,
            self.mode_continuous_radio, self.date_edit, self.days_spin,
            self.interval_spin, self.delete_chk, self.workers_spin,
            self.compression_spin, self.verbose_log_chk, self.summary_interval_spin,
            self.verify_chk, self.preserve_ts_chk,
        ):
            w.setEnabled(enabled)
        if enabled:
            self._update_mode_widgets()

    # ---------------- sinyaller ----------------

    @QtCore.Slot(str)
    def _on_pass_started(self, cutoff_str):
        self._rate_samples.clear()

    @QtCore.Slot()
    def _on_pass_finished(self):
        pass

    @QtCore.Slot()
    def _on_all_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Duraklat")
        self.speed_label.setText("")
        self._set_inputs_enabled(True)

    # ---------------- periyodik GUI guncelleme ----------------

    def _append_log(self, line):
        self.log_view.appendPlainText(line)

    def _flush_ui(self):
        buf = self.engine.drain_log_buffer()
        if buf:
            # tek seferde toplu ekle (her satir icin ayri repaint yerine)
            self.log_view.appendPlainText("\n".join(buf))

        if self.engine.is_running():
            c = self.engine.snapshot_counters()
            self.stats_label.setText(
                f"Taranan: {c['scanned']}   Donusturulen: {c['converted']}   "
                f"Atlanan: {c['skipped']}   Hata: {c['errors']}"
            )

            processed = c["converted"] + c["skipped"] + c["errors"]
            now = time.time()
            self._rate_samples.append((now, processed))

            if self.engine.is_paused():
                self.speed_label.setText("Duraklatildi.")
            elif len(self._rate_samples) >= 2:
                old_t, old_p = self._rate_samples[0]
                dt = now - old_t
                dp = processed - old_p
                rate = dp / dt if dt > 0 else 0.0
                backlog = max(0, c["scanned"] - processed)
                if rate > 0.01:
                    eta_seconds = backlog / rate
                    eta_str = str(timedelta(seconds=int(eta_seconds)))
                    self.speed_label.setText(
                        f"Hiz: {rate:.1f} dosya/sn   "
                        f"Tahmini kalan sure (bilinen kuyruk icin, ~{backlog} dosya): {eta_str}"
                    )
                else:
                    self.speed_label.setText(f"Hiz: {rate:.1f} dosya/sn")
            else:
                self.speed_label.setText("Hiz hesaplaniyor...")

    def closeEvent(self, event):
        self._save_settings()
        if self.engine.is_running():
            self.engine.stop()
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    icon_path = get_resource_path("app_icon.ico")
    if icon_path:
        app.setWindowIcon(QtGui.QIcon(icon_path))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
