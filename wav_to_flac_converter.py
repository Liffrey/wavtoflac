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
import wave
import queue
import shutil
import logging
import threading
import subprocess
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from PySide6 import QtCore, QtGui, QtWidgets

APP_VERSION = "1.1.1"
SETTINGS_ORG = "BugraSysadminTools"
SETTINGS_APP = "WavToFlacConverter"

# --windowed exe'nin konsolu yok; bayraksiz Windows her ffmpeg/ffprobe icin yeni
# konsol (cmd penceresi + conhost.exe) acar. CREATE_NO_WINDOW pencereyi gizler ama
# conhost.exe yine olusur; DETACHED_PROCESS hic konsol olusturmaz (stdio pipe'lari calisir).
NO_WINDOW = subprocess.DETACHED_PROCESS if os.name == "nt" else 0


# --------------------------------------------------------------------------
# ffmpeg konumunu bul (PyInstaller ile gomulmus onefile/onedir exe destegi)
# --------------------------------------------------------------------------

def find_tool(name):
    """ffmpeg/ffprobe'u once exe/script yaninda (veya _MEIPASS icinde),
    sonra PATH'te arar. Bulunamazsa None doner."""
    return get_resource_path(name + (".exe" if os.name == "nt" else "")) or shutil.which(name)


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


def read_header_duration(path):
    """PCM WAV / FLAC suresini dosya basligindan okur (ffprobe process'i
    acmadan). Okunamazsa (orn. u-law WAV, RF64) None doner."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
        if len(head) == 26 and head[:4] == b"fLaC" and head[4] & 0x7F == 0:
            # STREAMINFO: 20 bit ornekleme hizi | 3+5 bit | 36 bit toplam ornek
            x = int.from_bytes(head[18:26], "big")
            rate, total = x >> 44, x & 0xFFFFFFFFF
            return total / rate if rate and total else None
        with wave.open(path, "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return None


def durations_match(d_src, d_dst):
    if d_src is None or d_dst is None:
        return False
    return abs(d_src - d_dst) <= max(0.05, d_src * 0.001)  # 50ms veya binde bir


# --------------------------------------------------------------------------
# Donusturme motoru
# --------------------------------------------------------------------------

class ConversionEngine(QtCore.QObject):
    """Tarama + donusturme is akisini yoneten motor. QObject'tir cunku
    sinyalleri GUI thread'ine guvenli sekilde (queued connection) tasir,
    fakat agir islem asla GUI thread'inde calismaz."""

    pass_started = QtCore.Signal(str)      # cutoff aciklamasi
    all_finished = QtCore.Signal()

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
        self._log("DURAKLATILDI (çalışan işlemler bitiriliyor, yeni dosya alınmıyor).")

    def resume(self):
        self.pause_event.clear()
        self._log("DEVAM EDİLİYOR.")

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
            self._log(f"HATA CSV DOSYASI AÇILAMADI: {e}")
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
                creationflags=NO_WINDOW,
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
        # Hizli yol: basliklar uyusuyorsa dosya basina 2 ffprobe process'i acilmaz.
        # Uyusmazsa/okunamazsa eski davranis: ffprobe karar verir.
        if durations_match(read_header_duration(src_path), read_header_duration(dst_path)):
            return True
        return durations_match(self._get_duration(src_path), self._get_duration(dst_path))

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
                f"ÖZET -> Taranan: {counters_copy['scanned']}  Dönüştürülen: {counters_copy['converted']}  "
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

        # Once gecici dosyaya yaz, basarida yeniden adlandir: program/ffmpeg yarida
        # kesilirse yarim .flac kalmaz (yoksa sonraki tur onu "zaten var" diye atlardi).
        tmp_path = dst_path + ".tmp"
        ffmpeg_bin = self.ffmpeg_path or "ffmpeg"
        cmd = [
            ffmpeg_bin, "-y", "-nostdin", "-loglevel", "error",
            "-i", src_path,
            "-c:a", "flac", "-compression_level", str(self.compression_level),
            "-f", "flac", tmp_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=NO_WINDOW,
            )
        except FileNotFoundError:
            self._inc("errors")
            self._log("HATA: ffmpeg bulunamadı. ffmpeg.exe konumunu veya PATH ayarlarını kontrol edin.")
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
            result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0
        )

        if not conversion_ok:
            self._inc("errors")
            err = result.stderr.decode("utf-8", errors="ignore").strip()
            # hatalar verbose ayarindan bagimsiz olarak her zaman gosterilir
            self._log(f"HATA: {src_path} - {err[:300]}")
            self._write_error_csv(src_path, "donusturme_hatasi", err[:500])
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            self._maybe_log_milestone()
            return

        # ffmpeg basariyla bitti. Dogrulama SADECE orijinal silinecekse yapilir ve
        # o durumda her zaman yapilir (basliktan okundugu icin maliyeti yok denecek kadar az).
        verified_ok = True
        if self.delete_original:
            verified_ok = self._verify_conversion(src_path, tmp_path)

        if not verified_ok:
            self._inc("errors")
            self._log(f"DOĞRULAMA HATASI (süre uyuşmuyor, silinmedi): {src_path}")
            self._write_error_csv(
                src_path, "dogrulama_hatasi",
                "FLAC suresi WAV ile eslesmiyor; olusturulan FLAC silindi, orijinal WAV korundu."
            )
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            self._maybe_log_milestone()
            return

        # basarili + (varsa) dogrulandi. Hata olursa worker'in guvenlik agi
        # yakalar; WAV henuz silinmedigi icin veri kaybi olmaz.
        os.replace(tmp_path, dst_path)
        self._inc("converted")

        if self.preserve_timestamps:
            try:
                st = os.stat(src_path)
                os.utime(dst_path, (st.st_atime, st.st_mtime))
            except OSError as e:
                self._log(f"ZAMAN DAMGASI HATASI: {dst_path} - {e}")
                self._write_error_csv(dst_path, "zaman_damgasi_hatasi", str(e))

        if self.verbose_file_log:
            self._log(f"DÖNÜŞTÜRÜLDÜ: {src_path}")

        if self.delete_original:
            try:
                os.remove(src_path)
                if self.verbose_file_log:
                    self._log(f"SİLİNDİ (orijinal): {src_path}")
            except OSError as e:
                # silme hatasi onemli - verbose kapali olsa da her zaman goster
                self._log(f"SİLME HATASI: {src_path} - {e}")
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
                    self._log(f"BEKLENMEYEN İÇ HATA (worker çökmedi, devam ediyor): {item} - {e}")
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
            self._log(f"TARAMA HATASI (beklenmeyen, tarama erken sonlandırıldı): {e}")
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
                self.pass_started.emit(cutoff_str)
                self._log(f"Tarama başladı. Eşik tarih: {cutoff_str} (bu tarihten eski dosyalar dönüştürülecek)")

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
                    f"TUR TAMAMLANDI. Taranan: {c['scanned']}  Dönüştürülen: {c['converted']}  "
                    f"Atlanan: {c['skipped']}  Hata: {c['errors']}"
                )

                if not self.continuous or self.stop_event.is_set():
                    break

                self._log(f"Sonraki tur için {self.interval_minutes} dakika bekleniyor...")
                self.stop_event.wait(self.interval_minutes * 60)  # Durdur'da aninda uyanir
        except Exception as e:
            # Kritik guvenlik agi: --windowed olarak paketlenmis exe'de konsol
            # OLMADIGI icin yakalanmayan bir exception hicbir yerde gorunmeden
            # sessizce kaybolur. Burada yakalayip GUI log paneline (kullanicinin
            # gorebilecegi TEK yer) acikca yaziyoruz.
            self._log(f"KRİTİK HATA (işlem beklenmedik şekilde durdu): {e}")
        finally:
            self._log("İşlem durduruldu.")
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

<h3>Klasör</h3>
<p>WAV dosyalarının bulunduğu kök klasör. Alt klasörlerde de arama yapması
için <b>"Alt klasörleri de tara"</b> kutucuğunu işaretli bırakın.</p>

<h3>Çalışma Modu &ndash; Tek seferlik</h3>
<p>Seçtiğiniz <b>tarihten daha eski</b> (dosyanın son değiştirilme tarihine
göre) tüm WAV dosyalarını bir kez tarar, dönüştürür ve işlem biter.
Örneğin 01.01.2025 seçerseniz, o tarihten önce değiştirilmiş WAV'lar
işlenir; daha yeni dosyalara dokunulmaz.</p>

<h3>Çalışma Modu &ndash; Sürekli çalış</h3>
<p>Sabit bir tarih yerine <b>"şu günden eski"</b> mantığıyla çalışır.
Örneğin "10 gün" girerseniz, program her turda "şu an - 10 gün" eşiğini
yeniden hesaplar ve o tarihten eski tüm dosyaları dönüştürür. Bir tur
bitince <b>Tarama aralığı</b> kadar bekler, sonra eşiği yeniden hesaplayıp
tekrar tarar. Bu, sonsuz bir döngüdür; siz <b>Durdur</b> butonuna basana
kadar çalışmaya devam eder. Sunucu her zaman açık kalıp sürekli yeni
gelen dosyaları belirli bir yaştan sonra otomatik sıkıştırmak için
idealdir.</p>

<h3>Paralel işlem</h3>
<p>Aynı anda kaç dosyanın <b>eşzamanlı</b> olarak ffmpeg ile
dönüştürüleceğini belirler; her biri kendi ffmpeg sürecini çalıştırır.
Varsayılan değer CPU çekirdek sayısıdır.</p>
<ul>
<li>Yerel SSD'de çalışıyorsanız: çekirdek sayısı civarı (hatta biraz
üstü) verimlidir.</li>
<li>Ağ paylaşımı (SMB/NFS) veya yavaş disk kullanıyorsanız: daha düşük
bir değer (örn. 4-8) disk kuyruğunun tıkanmasını önleyip genelde daha
hızlı sonuç verir.</li>
</ul>
<p>Emin değilseniz küçük bir örnek grupla (birkaç bin dosya) farklı
değerleri deneyip sunucunuzdaki gerçek darboğazı (CPU mu disk mi)
görmeniz önerilir.</p>

<h3>Sıkıştırma (0-8)</h3>
<p>FLAC sıkıştırma seviyesidir. FLAC her zaman <b>kayıpsızdır</b>
(lossless) &ndash; hangi seviyeyi seçerseniz seçin ses kalitesinde HİÇBİR
fark olmaz; sadece dosya boyutu ve dönüştürme süresi değişir.</p>
<ul>
<li><b>0</b>: en hızlı, en az sıkıştırma (en büyük dosya)</li>
<li><b>8</b>: en yavaş, en fazla sıkıştırma (en küçük dosya)</li>
</ul>
<p>0 ile 8 arasındaki boyut farkı genelde sadece %5-10 civarındadır, ama
8. seviye 0. seviyeye göre birkaç kat daha uzun sürebilir. Milyonlarca
dosyada <b>2-5 arası</b> önerilir; depolama alanı kritikse daha yüksek
seçilebilir ama toplam süre ciddi artar.</p>

<h3>Özet log aralığı</h3>
<p>Kaç dosya işlendiğinde ("Taranan / Dönüştürülen / Atlanan / Hata")
özet satırının log ekranına yazılacağını belirler. Bu satırlar "Her
dosyayı ayrı satırda logla" KAPALI olsa bile her zaman gösterilir. Ayrıca
her turun sonunda otomatik olarak bir TUR TAMAMLANDI özeti yazılır.</p>

<h3>Orijinal zaman damgasını FLAC'a aktar</h3>
<p>İşaretliyse, oluşturulan FLAC dosyasının değiştirilme/erişim tarihi
orijinal WAV dosyasıyla aynı yapılır (dosya "yeni oluşturulmuş" gibi
görünmez, arşiv/denetim tarihini korur). Kapatılırsa FLAC dosyası kendi
oluşturulma anındaki tarihi taşır.</p>

<h3>Her dosyayı ayrı satırda logla</h3>
<p>Açıkken her dönüştürülen/atlanan/silinen dosya için ayrı bir log satırı
yazılır (örn. <code>DÖNÜŞTÜRÜLDÜ: C:\\...\\ornek.wav</code>). Milyonlarca
dosyada bu, ekran performansını ve okunabilirliği düşürebilir.
Kapatıldığında bu tekil satırlar yazılmaz; ancak <b>hatalar her zaman
gösterilir</b> ve özet satırları yazılmaya devam eder.</p>

<h3>Dönüştürülen orijinal WAV dosyalarını sil</h3>
<p><b>Geri alınamaz.</b> İşaretlenirse, bir dosya başarıyla FLAC'a
dönüştürüldükten SONRA orijinal WAV dosyası diskten silinir. Silmeden önce
her FLAC <b>doğrulanır</b>: WAV ve FLAC süreleri önce dosya başlıklarından
(hızlı), gerekirse <b>ffprobe</b> ile karşılaştırılır. Süreler uyuşmuyorsa
veya doğrulanamıyorsa oluşan FLAC silinir, <b>orijinal WAV korunur</b> ve
durum hata olarak kaydedilir. Başlat'a basıldığında silme için ayrıca onay
istenir. İşaretli değilse WAV dosyaları FLAC'ın yanında olduğu gibi kalır
(disk alanı kazancı olmaz, sadece FLAC kopyası da oluşur).</p>

<h3>Duraklat / Devam Et</h3>
<p><b>Duraklat</b>'a basıldığında, o an işleme alınmamış dosyalar için
yeni işlem başlatılmaz; hâlihazırda çalışmakta olan ffmpeg işlemleri
kesilmeden tamamlanır (böylece yarım dosya oluşmaz). Tarama işlemi de
kuyruk dolacağı için kendiliğinden durur. <b>Devam Et</b>'e basıldığında
kaldığı yerden devam eder. <b>Durdur</b> ise duraklatmadan farklı olarak
mevcut turu tamamen sonlandırır ve (sürekli modda) sonraki turları
başlatmaz.</p>

<h3>Hız / Tahmini Süre göstergesi</h3>
<p>İstatistiklerin altında gösterilen "Hız" değeri, son birkaç saniyedeki
işlenen dosya sayısına bakarak hesaplanan dosya/saniye değeridir.
"Tahmini kalan süre" ise SADECE o ana kadar taranmış ama henüz işlenmemiş
dosyalar (bilinen kuyruk) için bir tahmindir; tarama devam ettiği sürece
gerçek toplam dosya sayısı önceden bilinmediği için (bu, milyonlarca
dosyada bellek tasarrufu sağlayan tasarımın bir sonucudur) bu değer
zamanla değişebilir.</p>

<h3>Hata Raporu (CSV)</h3>
<p>Dönüştürme, doğrulama veya silme sırasında oluşan HER hata otomatik
olarak <code>%USERPROFILE%\\.wav2flac_logs\\hatalar.csv</code> dosyasına
(tarih, dosya yolu, hata türü, detay sütunlarıyla) satır satır eklenir.
Bu dosyayı Yardım menüsündeki "Hata Raporunu (CSV) Aç" ile Excel/Notepad
gibi bir programda açabilirsiniz. İşlem devam ederken de dosya sürekli
güncellenir, ayrıca bir "dışa aktarma" adımına gerek yoktur.</p>

<h3>Diskteki tam log dosyası</h3>
<p>Ekrandaki log gösterimi belli bir satır sayısıyla sınırlıdır (bellek
için), ancak <b>tüm</b> işlemler ayrıca diske de yazılır:
<code>%USERPROFILE%\\.wav2flac_logs\\wav2flac.log</code> (dönen/rotating
dosya). Bu klasörü Yardım menüsündeki "Log Klasörünü Aç" ile
açabilirsiniz.</p>

<h3>Ayarların hatırlanması</h3>
<p>Girdiğiniz klasör, mod, tarih/gün, paralel işlem sayısı, sıkıştırma
seviyesi ve tüm diğer seçenekler uygulama kapatıldığında otomatik
kaydedilir; bir sonraki açılışta kaldığınız yerden devam edersiniz.</p>
"""


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"WAV → FLAC Toplu Dönüştürücü v{APP_VERSION}")
        self.setMinimumWidth(620)

        icon_path = get_resource_path("app_icon.ico")
        if icon_path:
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self.logger = self._build_logger()
        self.engine = ConversionEngine(self.logger)
        self.engine.pass_started.connect(self._on_pass_started)
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

        log_folder_action = QtGui.QAction("Log Klasörünü Aç", self)
        log_folder_action.triggered.connect(self._open_log_folder)
        help_menu.addAction(log_folder_action)

        error_csv_action = QtGui.QAction("Hata Raporunu (CSV) Aç", self)
        error_csv_action.triggered.connect(self._open_error_csv_file)
        help_menu.addAction(error_csv_action)

        help_menu.addSeparator()

        about_action = QtGui.QAction("Hakkında...", self)
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
                self, "Hata raporu bulunamadı",
                "Henüz bir hata kaydı oluşmadı. İşlem çalıştığında hatalar "
                f"burada birikecek:\n{csv_path}",
            )
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(csv_path))

    def _show_about(self):
        QtWidgets.QMessageBox.about(
            self,
            "Hakkında",
            f"<b>WAV &rarr; FLAC Toplu Dönüştürücü</b><br>Sürüm {APP_VERSION}<br><br>"
            "Büyük hacimli WAV arşivlerini kayıpsız FLAC formatına dönüştürmek "
            "için RAM ve hız odaklı tasarlanmış bir araç. Bugra <br><br>"
            "Dönüştürme motoru: ffmpeg",
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
        path = find_tool("ffmpeg")
        self.engine.ffmpeg_path = path
        if path is None:
            self._append_startup_log("UYARI: ffmpeg bulunamadı. Dönüştürme işlemi çalışmayacaktır.")
            QtWidgets.QMessageBox.warning(
                self,
                "ffmpeg bulunamadı",
                "ffmpeg ne exe ile birlikte (gömülü) ne de PATH içinde bulunamadı.\n"
                "Dönüştürme işlemi çalışmayacaktır.\n\n"
                "Bu exe ffmpeg ile birlikte paketlenmediyse, ffmpeg.exe'yi programın "
                "yanına koyun veya PATH'e ekleyin (https://ffmpeg.org/download.html).",
            )
        else:
            self._append_startup_log(f"ffmpeg bulundu: {path}")

    def _check_ffprobe(self):
        path = find_tool("ffprobe")
        self.engine.ffprobe_path = path
        if path is None:
            self._append_startup_log(
                "UYARI: ffprobe bulunamadı. Süresi dosya başlığından okunamayan WAV'lar "
                "(örn. u-law) doğrulanamaz; silme açıksa bu dosyalar silinmez."
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
        self.path_edit.setPlaceholderText("WAV dosyalarının bulunduğu klasör...")
        browse_btn = QtWidgets.QPushButton("Gözat...")
        browse_btn.clicked.connect(self._browse_folder)
        path_row.addWidget(QtWidgets.QLabel("Klasör:"))
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self.recursive_chk = QtWidgets.QCheckBox("Alt klasörleri de tara")
        self.recursive_chk.setChecked(True)
        layout.addWidget(self.recursive_chk)

        # --- Mod secimi ---
        mode_group = QtWidgets.QGroupBox("Çalışma Modu")
        mode_layout = QtWidgets.QVBoxLayout(mode_group)

        self.mode_once_radio = QtWidgets.QRadioButton("Tek seferlik: belirli tarihten eski dosyaları dönüştür")
        self.mode_continuous_radio = QtWidgets.QRadioButton("Sürekli çalış: X günden eski dosyaları periyodik olarak dönüştür")
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
        cont_row.addWidget(QtWidgets.QLabel("Şu günden eski dosyalar:"))
        self.days_spin = QtWidgets.QSpinBox()
        self.days_spin.setRange(1, 3650)
        self.days_spin.setValue(10)
        self.days_spin.setSuffix(" gün")
        cont_row.addWidget(self.days_spin)
        cont_row.addSpacing(15)
        cont_row.addWidget(QtWidgets.QLabel("Tarama aralığı:"))
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

        # --- Secenekler (eski Secenekler + Guvenlik + Loglama gruplari tek grupta) ---
        opts_group = QtWidgets.QGroupBox("Seçenekler")
        opts_layout = QtWidgets.QVBoxLayout(opts_group)

        row1 = QtWidgets.QHBoxLayout()
        row1.addWidget(QtWidgets.QLabel("Paralel işlem:"))
        self.workers_spin = QtWidgets.QSpinBox()
        self.workers_spin.setRange(1, 64)
        self.workers_spin.setValue(max(1, os.cpu_count() or 4))
        row1.addWidget(self.workers_spin)
        row1.addSpacing(15)
        row1.addWidget(QtWidgets.QLabel("Sıkıştırma (0-8):"))
        self.compression_spin = QtWidgets.QSpinBox()
        self.compression_spin.setRange(0, 8)
        self.compression_spin.setValue(5)
        row1.addWidget(self.compression_spin)
        row1.addSpacing(15)
        row1.addWidget(QtWidgets.QLabel("Özet log aralığı:"))
        self.summary_interval_spin = QtWidgets.QSpinBox()
        self.summary_interval_spin.setRange(1, 1000000)
        self.summary_interval_spin.setValue(500)
        self.summary_interval_spin.setSuffix(" dosyada bir")
        self.summary_interval_spin.setToolTip(
            "Kaç dosya işlendiğinde 'Taranan/Dönüştürülen/Atlanan/Hata' özet satırı "
            "yazılacağını belirler. Bu satırlar her zaman gösterilir."
        )
        row1.addWidget(self.summary_interval_spin)
        row1.addStretch(1)
        opts_layout.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        self.preserve_ts_chk = QtWidgets.QCheckBox("Orijinal zaman damgasını FLAC'a aktar")
        self.preserve_ts_chk.setChecked(True)
        self.preserve_ts_chk.setToolTip(
            "İşaretliyse, oluşturulan FLAC dosyasının değiştirilme/erişim "
            "tarihi orijinal WAV dosyasıyla aynı yapılır."
        )
        row2.addWidget(self.preserve_ts_chk)
        row2.addSpacing(15)
        self.verbose_log_chk = QtWidgets.QCheckBox("Her dosyayı ayrı satırda logla")
        self.verbose_log_chk.setChecked(True)
        self.verbose_log_chk.setToolTip(
            "DÖNÜŞTÜRÜLDÜ / ATLANDI / SİLİNDİ satırlarını yazar. Milyonlarca dosyada "
            "kapatılması önerilir; hatalar ve özet sayımlar her zaman gösterilir."
        )
        row2.addWidget(self.verbose_log_chk)
        row2.addStretch(1)
        opts_layout.addLayout(row2)

        self.delete_chk = QtWidgets.QCheckBox("Dönüştürülen orijinal WAV dosyalarını SİL (geri alınamaz)")
        self.delete_chk.toggled.connect(self._update_delete_style)
        self.delete_chk.setToolTip(
            "Silmeden önce her FLAC, WAV ile süre karşılaştırılarak doğrulanır; "
            "uyuşmazsa WAV korunur ve hata olarak kaydedilir. Başlat'ta ayrıca onay istenir."
        )
        opts_layout.addWidget(self.delete_chk)

        layout.addWidget(opts_group)

        # --- Baslat / Durdur / Duraklat ---
        btn_row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Başlat")
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
        self.stats_label = QtWidgets.QLabel("Taranan: 0   Dönüştürülen: 0   Atlanan: 0   Hata: 0")
        layout.addWidget(self.stats_label)

        self.speed_label = QtWidgets.QLabel("")
        self.speed_label.setStyleSheet("color: #888;")
        layout.addWidget(self.speed_label)

        # --- Log konsolu ---
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)  # GUI bellegini sinirla
        self.log_view.setMinimumHeight(140)
        self.log_view.setStyleSheet(CONSOLE_STYLE)
        layout.addWidget(self.log_view, 1)

        self.resize(720, 680)

    def _update_delete_style(self, on):
        # Geri alinamaz tek secenek; isaretliyken kirmizi+kalin. Stylesheet yerine palet:
        # stylesheet onay kutusunun Windows 11 gorunumunu bozuyor.
        pal = QtWidgets.QApplication.palette(self.delete_chk)
        if on:  # sadece WindowText: ButtonText onay isaretini de boyuyor
            pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#e5534b"))
        self.delete_chk.setPalette(pal)
        font = self.delete_chk.font()
        font.setBold(on)
        self.delete_chk.setFont(font)

    def _update_mode_widgets(self):
        once = self.mode_once_radio.isChecked()
        self.date_edit.setEnabled(once)
        self.days_spin.setEnabled(not once)
        self.interval_spin.setEnabled(not once)

    def _browse_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Klasör seç")
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
        self.preserve_ts_chk.setChecked(s.value("preserve_timestamps", True, type=bool))
        self.verbose_log_chk.setChecked(s.value("verbose_log", True, type=bool))
        self.summary_interval_spin.setValue(s.value("summary_interval", 500, type=int))

        self._update_mode_widgets()

    # ---------------- start / stop ----------------

    def _on_start(self):
        root = self.path_edit.text().strip()
        if not root or not os.path.isdir(root):
            QtWidgets.QMessageBox.warning(self, "Geçersiz klasör", "Lütfen geçerli bir klasör seçin.")
            return
        ffmpeg_path = find_tool("ffmpeg")
        if ffmpeg_path is None:
            QtWidgets.QMessageBox.critical(self, "ffmpeg bulunamadı", "ffmpeg (gömülü veya PATH'te) bulunamadı.")
            return

        if self.delete_chk.isChecked():
            scope = " (alt klasörler dahil)" if self.recursive_chk.isChecked() else ""
            resp = QtWidgets.QMessageBox.warning(
                self, "Orijinal dosyalar silinecek",
                "Dönüştürülüp doğrulanan WAV dosyaları KALICI OLARAK silinecek:\n"
                f"{root}{scope}\n\nDevam edilsin mi?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if resp != QtWidgets.QMessageBox.Yes:
                return

        self.engine.root_path = root
        self.engine.recursive = self.recursive_chk.isChecked()
        self.engine.delete_original = self.delete_chk.isChecked()
        self.engine.worker_count = self.workers_spin.value()
        self.engine.compression_level = self.compression_spin.value()
        self.engine.continuous = self.mode_continuous_radio.isChecked()
        self.engine.days_threshold = self.days_spin.value()
        self.engine.interval_minutes = self.interval_spin.value()
        self.engine.ffmpeg_path = ffmpeg_path
        self.engine.ffprobe_path = find_tool("ffprobe")
        self.engine.preserve_timestamps = self.preserve_ts_chk.isChecked()
        self.engine.verbose_file_log = self.verbose_log_chk.isChecked()
        self.engine.summary_log_interval = self.summary_interval_spin.value()

        if not self.engine.continuous:
            qd = self.date_edit.date()
            cutoff_dt = datetime(qd.year(), qd.month(), qd.day())
            self.engine.fixed_cutoff_ts = cutoff_dt.timestamp()

        self._save_settings()

        self.log_view.clear()
        self._append_log(f"Başlatıldı. Klasör: {root}")

        self._rate_samples.clear()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Duraklat")
        self._set_inputs_enabled(False)

        self.engine.start()

    def _on_stop(self):
        self._append_log("Durdurma isteği gönderildi, mevcut işlemler tamamlanıp duracak...")
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
            self.preserve_ts_chk,
        ):
            w.setEnabled(enabled)
        if enabled:
            self._update_mode_widgets()

    # ---------------- sinyaller ----------------

    @QtCore.Slot(str)
    def _on_pass_started(self, cutoff_str):
        self._rate_samples.clear()

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

        # Calisma bittikten sonra da guncelle; yoksa son 250ms'lik sayimlar ekranda eksik kalir.
        c = self.engine.snapshot_counters()
        self.stats_label.setText(
            f"Taranan: {c['scanned']}   Dönüştürülen: {c['converted']}   "
            f"Atlanan: {c['skipped']}   Hata: {c['errors']}"
        )

        if self.engine.is_running():
            processed = c["converted"] + c["skipped"] + c["errors"]
            now = time.time()
            self._rate_samples.append((now, processed))

            if self.engine.is_paused():
                self.speed_label.setText("Duraklatıldı.")
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
                        f"Hız: {rate:.1f} dosya/sn   "
                        f"Tahmini kalan süre (bilinen kuyruk için, ~{backlog} dosya): {eta_str}"
                    )
                else:
                    self.speed_label.setText(f"Hız: {rate:.1f} dosya/sn")
            else:
                self.speed_label.setText("Hız hesaplanıyor...")

    def closeEvent(self, event):
        if self.engine.is_running():
            resp = QtWidgets.QMessageBox.question(
                self, "İşlem sürüyor",
                "Dönüştürme devam ediyor. Çıkarsanız yarım kalan dosyalar bir "
                "sonraki çalıştırmada baştan dönüştürülür.\n\nÇıkılsın mı?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if resp != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self.engine.stop()
        self._save_settings()
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
