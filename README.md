# WAV to FLAC Converter

Windows icin PySide6 tabanli toplu WAV -> FLAC donusturucusu. Buyuk klasorlerde dosya listesini bellekte biriktirmeden tarar ve donusturur. Istege bagli olarak FLAC dogrulamasi yapar ve yalnizca basarili donusumden sonra kaynak WAV dosyasini siler.

## Ozellikler

- Alt klasorleri tarayarak WAV dosyalarini bulma
- Birden fazla `ffmpeg` islemiyle paralel donusturme
- Tarih veya gun esigine gore tek seferlik ya da surekli tarama
- Donusturme sonucunu `ffprobe` ile dogrulama
- Istege bagli kaynak WAV silme ve zaman damgalarini koruma
- Duraklatma, devam ettirme ve guvenli durdurma
- Ayarlari kaydetme, sinirli GUI logu ve diske donen tam log
- Windows icin PyInstaller ile tek EXE paketleme

## Gereksinimler

- Python 3.10 veya uzeri
- `PySide6`
- Gelistirme/komut satirindan calistirma icin `ffmpeg` PATH'te olmali veya uygulama dosyasinin yaninda bulunmali. Dogrulama ozelligi icin `ffprobe` da gereklidir.

## Calistirma

```powershell
python -m pip install PySide6
python wav_to_flac_converter.py
```

## Windows EXE olusturma

1. Python 3.10+ kurun.
2. Windows `ffmpeg.exe` ve `ffprobe.exe` dosyalarini `build.bat` ile ayni klasore koyun. Istege bagli olarak uygulama simgesi icin `app_icon.ico` dosyasini da ekleyin.
3. `build.bat` dosyasini calistirin. Betik `.buildvenv` sanal ortamini olusturur ve PyInstaller ile `dist\WavToFlac.exe` dosyasini derler.

Derleme betigi ffmpeg ikililerini EXE'ye gomdugu icin, bu ikililer kaynak depoya eklenmez. Dagitimdan once kullandiginiz FFmpeg derlemesinin lisans ve dagitim kosullarini kontrol edin.

## Loglar

Tam uygulama logu `%USERPROFILE%\.wav2flac_logs\wav2flac.log` konumuna yazilir. Ayrintili paketleme notlari icin [OKUBENI.txt](OKUBENI.txt) dosyasina bakin.

## Lisans

Lisans bilgisi icin [LICENSE](LICENSE) dosyasina bakin.