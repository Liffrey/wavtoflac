"""build.bat cagirir: APP_VERSION'dan exe'nin Ozellikler > Ayrintilar (ve Gorev
Yoneticisi'ndeki ad) bilgisini PyInstaller --version-file formatinda uretir.
Kullanim: python make_version_file.py <cikti_dosyasi>"""
import re
import sys

src = open("wav_to_flac_converter.py", encoding="utf-8").read()
version = re.search(r'^APP_VERSION = "(\d+\.\d+\.\d+)"', src, re.M).group(1)
nums = tuple(int(n) for n in version.split(".")) + (0,)

info = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}),
  kids=[
    StringFileInfo([StringTable('041F04B0', [
      StringStruct('FileDescription', 'WAV → FLAC Toplu Dönüştürücü'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductName', 'WAV → FLAC Toplu Dönüştürücü'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('InternalName', 'WavToFlac'),
      StringStruct('OriginalFilename', 'WavToFlac.exe'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x041F, 1200])]),
  ]
)
"""
open(sys.argv[1], "w", encoding="utf-8").write(info)
print(f"Surum bilgisi: {version}")
