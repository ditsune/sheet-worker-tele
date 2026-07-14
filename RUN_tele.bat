@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Wrapper auto-restart buat auto.py
REM
REM  Kenapa perlu ini: kalau script Python crash/exit karena error
REM  yang gak ke-handle (misal koneksi internet putus total lebih
REM  dari 20x retry, atau ada bug baru), tanpa wrapper ini terminal
REM  bakal keliatan "selesai" gitu aja tanpa restart otomatis.
REM
REM  Cara pake: double-click file ini (atau jalanin dari terminal),
REM  BUKAN "python auto_copy_tele_v3.py" langsung.
REM ============================================================

set SCRIPT_NAME=autoTele.py
set RESTART_COUNT=0
set MAX_RESTART_PER_HOUR=10

:loop
echo.
echo ============================================================
echo  [%date% %time%] Menjalankan %SCRIPT_NAME% ...
echo ============================================================
echo.

python "%SCRIPT_NAME%"
set EXIT_CODE=%ERRORLEVEL%

set /a RESTART_COUNT+=1

echo.
echo ============================================================
echo  [%date% %time%] Script berhenti dengan exit code %EXIT_CODE%
echo  Restart ke-%RESTART_COUNT%. Tunggu 5 detik sebelum restart...
echo ============================================================
echo.

REM Kalau exit code 0 (CTRL+C manual / exit normal), jangan auto-restart
if %EXIT_CODE% EQU 0 (
    echo Script berhenti secara normal ^(exit code 0^), tidak di-restart.
    echo Tutup window ini atau jalankan ulang manual kalau mau lanjut.
    pause
    exit /b 0
)

timeout /t 5 /nobreak > nul
goto loop