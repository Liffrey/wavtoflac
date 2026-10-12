# WAV to FLAC Converter

A Windows desktop application for batch WAV-to-FLAC conversion, built with PySide6. It scans and converts large directory trees without keeping the full file list in memory. Optional FLAC verification helps ensure the source WAV is deleted only after a successful conversion.

## Features

- Find WAV files in a directory and its subdirectories
- Convert files in parallel using multiple `ffmpeg` processes
- Run once or continuously, using a date or age threshold
- Verify converted audio with `ffprobe`
- Optionally delete source WAV files and preserve timestamps
- Pause, resume, and safely stop conversion
- Save application settings and write rotating logs to disk
- Package as a single Windows executable with PyInstaller

## Requirements

- Python 3.10 or newer
- `PySide6`
- For running from source, `ffmpeg` must be on `PATH` or beside the application. `ffprobe` is also needed for conversion verification.

## Run from source

```powershell
python -m pip install PySide6
python wav_to_flac_converter.py
```

## Build the Windows executable

1. Install Python 3.10 or newer.
2. Place the Windows `ffmpeg.exe` and `ffprobe.exe` binaries beside `build.bat`. Optionally, place `app_icon.ico` there as well.
3. Run `build.bat`. It creates an isolated `.buildvenv` environment and builds `dist\WavToFlac.exe` with PyInstaller.

The build script bundles the FFmpeg binaries into the executable, so they are intentionally excluded from this source repository. Review the license and redistribution terms of your FFmpeg build before distributing the executable.

## Logs

The full application log is written to `%USERPROFILE%\.wav2flac_logs\wav2flac.log`. See [OKUBENI.txt](OKUBENI.txt) for additional packaging notes.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.