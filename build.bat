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
REM      (PATH'e eklemek GEREKMEZ; betik "py" launcher'i kullanir)
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

REM Python'u bul: once py launcher (PATH gerektirmez), yoksa PATH'teki python.
REM "where python" yetmez: Microsoft Store'un sahte python.exe kisayolunu da bulur.
set "PY_CMD="
py -3 -c "" >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD python -c "" >nul 2>nul && set "PY_CMD=python"
if not defined PY_CMD (
    echo HATA: Python bulunamadi. python.org kurulumuyla gelen "py" launcher'i
    echo veya PATH'te calisan bir python gerekli: https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

echo [1/5] Izole sanal ortam olusturuluyor (%VENV_DIR%)...
REM Sadece klasorun varligina bakmak yetmez: venv'i olusturan Python silinmis/tasinmissa
REM venv bozuktur ("No Python at ..."). Calismiyorsa silip yeniden olustur.
"%VENV_DIR%\Scripts\python.exe" -c "" >nul 2>nul
if not errorlevel 1 (
    echo       Mevcut sanal ortam bulundu, yeniden kullaniliyor.
) else (
    if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
    %PY_CMD% -m venv "%VENV_DIR%"
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

REM exe'nin Ozellikler > Ayrintilar surum bilgisi, koddaki APP_VERSION'dan uretilir
mkdir build
"%VENV_PY%" make_version_file.py build\version_info.txt
if errorlevel 1 (
    echo HATA: Surum bilgisi dosyasi uretilemedi.
    pause
    exit /b 1
)

echo [5/5] Tek dosya exe olusturuluyor (ffmpeg.exe ve ikon gomuluyor)...
if exist app_icon.ico (
    "%VENV_PY%" -m PyInstaller --noconfirm --onefile --windowed ^
        --name WavToFlac ^
        --version-file "build\version_info.txt" ^
        --icon "app_icon.ico" ^
        --add-binary "ffmpeg.exe;." ^
        --add-binary "ffprobe.exe;." ^
        --add-data "app_icon.ico;." ^
        wav_to_flac_converter.py
) else (
    "%VENV_PY%" -m PyInstaller --noconfirm --onefile --windowed ^
        --name WavToFlac ^
        --version-file "build\version_info.txt" ^
        --add-binary "ffmpeg.exe;." ^
        --add-binary "ffprobe.exe;." ^
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
