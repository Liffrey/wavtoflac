@echo off
REM ============================================================
REM  WAV -> FLAC Donusturucu - Tek EXE Build Betigi (ffmpeg gomulu)
REM  Bu betik sisteminizdeki Python kurulumuna DOKUNMAZ; kendi
REM  izole sanal ortamini (.buildvenv) olusturup gerekli paketleri
REM  SADECE o sanal ortama kurar. Global/mevcut Python kutuphaneleriniz
REM  etkilenmez.
REM ============================================================
REM
REM ONCEDEN YAPILMASI GEREKENLER:
REM   1) Windows'a Python 3.10+ kurun: https://www.python.org/downloads/windows/
REM      (Kurulumda "Add python.exe to PATH" secilmis olmali)
REM   2) Windows icin ffmpeg.exe indirin:
REM        https://www.gyan.dev/ffmpeg/builds/  ("release essentials" yeterli)
REM      Indirdiginiz zip icindeki bin\ffmpeg.exe dosyasini bu klasore
REM      (wav_to_flac_converter.py ile AYNI klasore) "ffmpeg.exe" adiyla kopyalayin.
REM   3) Bu betigi cift tiklayarak veya "build.bat" yazarak calistirin.
REM
REM SONUC: dist\WavToFlac.exe  -> tek dosya, ffmpeg icine gomulu, PATH'e
REM        ffmpeg kurmaniza gerek yok, sunucuda sadece bu tek exe'yi calistirin.

setlocal enabledelayedexpansion

set "VENV_DIR=.buildvenv"

if not exist ffmpeg.exe (
    echo.
    echo HATA: ffmpeg.exe bu klasorde bulunamadi.
    echo Lutfen https://www.gyan.dev/ffmpeg/builds/ adresinden Windows build'ini
    echo indirip, icindeki bin\ffmpeg.exe dosyasini bu klasore "ffmpeg.exe"
    echo adiyla kopyalayin, sonra bu betigi tekrar calistirin.
    echo.
    pause
    exit /b 1
)

if not exist app_icon.ico (
    echo UYARI: app_icon.ico bulunamadi, exe varsayilan Python ikonuyla derlenecek.
)

if not exist wav_to_flac_converter.py (
    echo HATA: wav_to_flac_converter.py bu klasorde bulunamadi.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo HATA: "python" komutu bulunamadi. Python kurulu ve PATH'e ekli oldugundan
    echo emin olun: https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

echo [1/5] Izole sanal ortam olusturuluyor (%VENV_DIR%)...
if exist "%VENV_DIR%" (
    echo       Mevcut sanal ortam bulundu, yeniden kullaniliyor.
) else (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo HATA: Sanal ortam olusturulamadi.
        pause
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo HATA: Sanal ortam icinde python.exe bulunamadi.
    pause
    exit /b 1
)

echo [2/5] Sanal ortamda pip guncelleniyor...
"%VENV_PY%" -m pip install --upgrade pip >nul

echo [3/5] Gerekli paketler SADECE sanal ortama kuruluyor (pyinstaller, PySide6)...
"%VENV_PY%" -m pip install --upgrade pyinstaller PySide6
if errorlevel 1 (
    echo HATA: Paket kurulumu basarisiz oldu.
    pause
    exit /b 1
)

echo [4/5] Onceki build klasorleri temizleniyor...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist WavToFlac.spec del /q WavToFlac.spec

echo [5/5] Tek dosya exe olusturuluyor (ffmpeg.exe ve ikon gomuluyor)...
if exist app_icon.ico (
    "%VENV_PY%" -m PyInstaller --noconfirm --onefile --windowed ^
        --name WavToFlac ^
        --icon "app_icon.ico" ^
        --add-binary "ffmpeg.exe;." ^
        --add-binary "ffprobe.exe;." ^
        --add-data "app_icon.ico;." ^
        wav_to_flac_converter.py
) else (
    "%VENV_PY%" -m PyInstaller --noconfirm --onefile --windowed ^
        --name WavToFlac ^
        --add-binary "ffmpeg.exe;." ^
        wav_to_flac_converter.py
)

if errorlevel 1 (
    echo HATA: PyInstaller build basarisiz oldu.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  TAMAMLANDI: dist\WavToFlac.exe
echo  Bu tek dosyayi sunucuya kopyalayip dogrudan calistirabilirsiniz.
echo  Ayrica ffmpeg kurmaniza / PATH'e eklemenize gerek yoktur.
echo.
echo  Kullanilan sanal ortam "%VENV_DIR%" klasorundedir; sisteminizdeki
echo  global Python kutuphaneleri hic etkilenmedi. Diskten tamamen
echo  silmek isterseniz "%VENV_DIR%" klasorunu elle kaldirabilirsiniz.
echo ============================================================
echo.
pause
