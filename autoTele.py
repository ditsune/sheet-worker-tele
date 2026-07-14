"""
AUTO COPY TELE - v5 (auto-derive shift config + reconciliation per ganti shift)
=================================================================================

SETUP:
  pip install gspread oauth2client requests --break-system-packages
  (atau tanpa flag itu kalau bukan di environment yang restricted)
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound, SpreadsheetNotFound, APIError
import time
import json
import logging
import os
import sys
from datetime import datetime, date
from logging.handlers import TimedRotatingFileHandler

try:
    import requests
except ImportError:
    requests = None

# ========== KONFIGURASI ==========
SOURCE_SHEET_ID = '1GV-bj_e6j1QGpGXWQF57wPkFl0ffG7MiF5Y2WKEOp9g'
TARGET_SHEET_ID = '1GmOzkKSjUaFkGGiQC4ITrFAzdrqYUhf_n24HnN-zP1U'
CREDENTIALS_FILE = 'credentials.json'

# --- Ganti tanggal source, kapan pun tanggal kalender beneran ganti ---
# (biasanya tengah malam kalau lagi shift malam). INI GAK ADA HUBUNGAN
# sama SHIFT_DATE/SHIFT di bawah -- source ngikutin tanggal kalender,
# shift ngikutin jadwal kerja. Keduanya bisa beda tanggal pas shift
# malam lewat tengah malam (source ganti, tapi target/shift tetap).
SOURCE_TAB_NAME = '2026-07-14'

# --- Ganti CUMA pas GANTI SHIFT (bukan pas ganti tanggal source) ---
# (v5) TARGET_TAB_NAME sekarang di-derive otomatis dari 2 variabel ini,
# jadi cukup ubah SHIFT_DATE + SHIFT tiap mulai shift baru:
#   - SHIFT_DATE : tanggal shift ini (format sama kayak nama tab, misal '9 juli')
#   - SHIFT      : 'pagi' atau 'malam'
# Contoh: mulai shift malam tgl 9 juli -> SHIFT_DATE='9 juli', SHIFT='malam'
#         -> TARGET_TAB_NAME jadi '9 juli malam'
SHIFT_DATE = '14 juli'
SHIFT = 'pagi'  # 'pagi' atau 'malam'

# --- (fix v6) Nama tab SHIFT SEBELUMNYA, buat cross-check dedup ---
# PENTING: ini BUKAN sekadar "shift pasangan di tanggal yang sama".
# Shift malam nyambung lewat tengah malam, jadi shift yang beneran
# terjadi PERSIS SEBELUM shift PAGI hari ini adalah shift MALAM di
# TANGGAL KEMARIN (beda tanggal!) -- bukan "malam" di tanggal yang
# sama kayak yang di-derive otomatis di versi sebelumnya (v5), yang
# jadi penyebab data lama gak ke-dedup dan ke-copy dobel.
#
# Contoh: mulai shift PAGI tgl 14 juli -> shift sebelumnya yang beneran
# kejadian itu "13 juli malam", BUKAN "14 juli malam" (yang malah belum
# terjadi / tab-nya belum ada). Makanya WAJIB diisi manual persis nama
# tab-nya tiap ganti shift, gak bisa di-derive otomatis dari SHIFT_DATE.
#
# Isi None kalau ini run pertama / gak ada shift sebelumnya yang perlu
# di-cross-check (misal abis SINGLE_SHIFT_DAY, atau start baru total).
PREVIOUS_SHIFT_TAB_NAME = '13 juli malam'

# --- Hari-hari khusus (tanggal X9 & X0 tiap siklus 10-hari) ---
# Di hari-hari ini cuma ADA 1 SHIFT yang kerja (yang satunya libur),
# jam operasional juga cuma 12 jam (10.00-22.00), dan sheet targetnya
# CUMA 1 -- namanya cuma tanggal doang TANPA suffix "pagi"/"malam"
# (misal "10 juli", bukan "10 juli malam"), dan gak ada tab lawan
# buat dedup-check karena shift lawannya emang gak masuk.
#
# Set True kalau hari ini termasuk salah satu dari 2 hari itu (dan
# lo yang kebagian masuk sendirian hari itu). Kalau ragu: shift MALAM
# selalu libur di hari PERTAMA dari 2 hari itu (tgl akhiran 9), shift
# PAGI selalu libur di hari KEDUA (tgl akhiran 0) -- jadi kalau
# tanggal hari ini akhiran 9 dan lo shift malam, atau akhiran 0 dan
# lo shift pagi, berarti HARI ITU LO LIBUR, bukan SINGLE_SHIFT_DAY.
# SINGLE_SHIFT_DAY = True itu kebalikannya: lo yang KERJA SENDIRIAN
# karena shift LAWAN yang libur.
SINGLE_SHIFT_DAY = False  # True kalau shift lawan libur, target tab cuma 1 (tanpa suffix)

_OTHER_SHIFT = {'pagi': 'malam', 'malam': 'pagi'}
if SHIFT not in _OTHER_SHIFT:
    raise ValueError(f'SHIFT harus "pagi" atau "malam", bukan "{SHIFT}"')

if SINGLE_SHIFT_DAY:
    TARGET_TAB_NAME = SHIFT_DATE  # tanpa suffix, misal "10 juli"
else:
    TARGET_TAB_NAME = f'{SHIFT_DATE} {SHIFT}'

# (fix v6) DUPLICATE_CHECK_EXTRA_TABS sekarang cuma dari
# PREVIOUS_SHIFT_TAB_NAME yang diisi manual (lihat penjelasan di atas),
# BUKAN di-derive otomatis dari SHIFT_DATE yang sama.
DUPLICATE_CHECK_EXTRA_TABS = [PREVIOUS_SHIFT_TAB_NAME] if PREVIOUS_SHIFT_TAB_NAME else []

STATE_FILE = 'checkpoint_state.json'
LOG_FILE = 'auto_copy.log'

DISCORD_WEBHOOK_URL = 'https://discord.com/api/webhooks/1525052640965300286/yElvPU6PdXP4YyDN4O_MbILinRwhIG2qRPUovZ0GT8SJ4naUbypextr-IkcR-r_qgtFr'
HEARTBEAT_INTERVAL_SEC = 3600  # 1 jam

POLL_INTERVAL_SEC = 10
MAX_RETRIES = 5
CONFIG_RECHECK_INTERVAL_SEC = 60  # dipakai pas nunggu fatal config error dibenerin
# ===================================

# ---------------- LOGGING ----------------
logger = logging.getLogger('auto_copy_tele')
logger.setLevel(logging.INFO)

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(_console)

_file = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14, encoding='utf-8')
_file.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(message)s'))
logger.addHandler(_file)

def log(msg, level='info'):
    getattr(logger, level)(msg)


# ---------------- CUSTOM EXCEPTION ----------------
class FatalConfigError(Exception):
    """Error permanen yang GAK bakal sembuh sendiri walau di-retry atau
    di-restart -- misal worksheet/spreadsheet gak ketemu (nama salah,
    tab kehapus, dsb). Butuh perbaikan manual di config atau di Sheet-nya
    langsung. Dipisah dari error transient (network/rate-limit) supaya
    gak di-retry percuma dan gak numpuk consecutive_errors."""
    pass


# ---------------- DISCORD WEBHOOK ----------------
def notify_discord(msg):
    if not DISCORD_WEBHOOK_URL or requests is None:
        return
    try:
        # Discord limit 2000 char per message, potong biar aman
        content = msg if len(msg) <= 1900 else msg[:1900] + '... (dipotong)'
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={'content': content},
            timeout=10
        )
        if resp.status_code >= 300:
            log(f'Discord webhook return status {resp.status_code}: {resp.text}', 'warning')
    except Exception as e:
        log(f'Gagal kirim notif Discord: {e}', 'warning')


# ---------------- RETRY WRAPPER ----------------
def with_retry(func, *args, max_retries=MAX_RETRIES, **kwargs):
    """Jalanin func dengan retry + exponential backoff kalau kena
    APIError (rate limit, server error) atau error koneksi.

    PENTING (fix v4): WorksheetNotFound / SpreadsheetNotFound TIDAK
    di-retry sama sekali -- itu error konfigurasi permanen (nama tab
    salah / tab belum dibuat / tab kehapus), retry gak akan pernah
    berhasil. Langsung dilempar sebagai FatalConfigError biar
    di-handle beda di layer atas."""
    delay = 1
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (WorksheetNotFound, SpreadsheetNotFound) as e:
            kind = 'Worksheet' if isinstance(e, WorksheetNotFound) else 'Spreadsheet'
            raise FatalConfigError(f'{kind} tidak ditemukan: "{e}"') from e
        except APIError as e:
            last_err = e
            log(f'API error (percobaan {attempt}/{max_retries}): {e}. Retry dalam {delay}s...', 'warning')
        except Exception as e:
            last_err = e
            log(f'Error koneksi (percobaan {attempt}/{max_retries}): {e}. Retry dalam {delay}s...', 'warning')
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise last_err


# ---------------- STATE (checkpoint berbasis ID) ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f'State file rusak ({e}), akan di-rebuild dari target sheet.', 'warning')
    return {'processed_ids': [], 'stats': {}}

def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, tmp[:-4])  # atomic write, hindari file kepotong kalau crash pas nulis

def get_client():
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive',
        'https://www.googleapis.com/auth/spreadsheets'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds)

def find_last_row_with_data(target_tab, kolom_index=3):
    col_data = with_retry(target_tab.col_values, kolom_index)
    for i in range(len(col_data) - 1, -1, -1):
        if col_data[i] and col_data[i].strip():
            return i + 1
    return 0

def get_target_existing_ids(target_tab):
    all_values = with_retry(target_tab.get_all_values)
    ids = set()
    for row in all_values:
        if len(row) >= 3 and row[2] and row[2].strip():
            ids.add(row[2].strip())
    return ids

def get_existing_ids_from_tabs(spreadsheet, tab_names):
    """(v4, fix DUPLICATE_CHECK_EXTRA_TABS) Union ID dari beberapa tab
    sekaligus -- dipakai buat cross-check duplikat antar shift, misal
    '9 juli pagi' & '9 juli malam' yang share SOURCE yang sama di hari
    itu. Kalau salah satu tab di-list gak ketemu, di-skip aja (warning)
    daripada bikin seluruh self-heal gagal."""
    ids = set()
    seen_tabs = set()
    for name in tab_names:
        if name in seen_tabs:
            continue
        seen_tabs.add(name)
        try:
            tab = with_retry(spreadsheet.worksheet, name)
        except FatalConfigError:
            log(f'Tab "{name}" (buat dedup check) gak ketemu, di-skip dari self-heal.', 'warning')
            continue
        tab_ids = get_target_existing_ids(tab)
        log(f'  - Tab "{name}": {len(tab_ids)} ID ke-scan.')
        ids |= tab_ids
    return ids


# ---------------- VALIDASI SETUP ----------------
def validate_setup(client):
    """(v4, baru) Cek semua spreadsheet/tab yang dibutuhin ada semua
    SEBELUM mulai proses apa pun. Return list of string masalah (kosong
    kalau semua oke). Dipanggil di awal run_auto() dan juga tiap kali
    lagi nunggu fatal config error dibenerin."""
    problems = []

    try:
        source_sheet = client.open_by_key(SOURCE_SHEET_ID)
    except Exception as e:
        problems.append(f'Gak bisa buka SOURCE spreadsheet ({SOURCE_SHEET_ID}): {e}')
        source_sheet = None

    if source_sheet:
        try:
            source_sheet.worksheet(SOURCE_TAB_NAME)
        except WorksheetNotFound:
            available = [ws.title for ws in source_sheet.worksheets()]
            problems.append(
                f'Tab source "{SOURCE_TAB_NAME}" gak ketemu di source sheet. '
                f'Tab yang ada di sana: {available}'
            )

    try:
        target_sheet = client.open_by_key(TARGET_SHEET_ID)
    except Exception as e:
        problems.append(f'Gak bisa buka TARGET spreadsheet ({TARGET_SHEET_ID}): {e}')
        target_sheet = None

    if target_sheet:
        available_target = [ws.title for ws in target_sheet.worksheets()]

        # TARGET_TAB_NAME wajib ada -- ini tab tujuan nulis data, kalau
        # gak ada script emang gak bisa jalan sama sekali.
        if TARGET_TAB_NAME not in available_target:
            problems.append(
                f'Tab "{TARGET_TAB_NAME}" gak ketemu di target sheet. '
                f'Tab yang ada di sana: {available_target}'
            )

        # (fix) DUPLICATE_CHECK_EXTRA_TABS (tab shift LAWAN, buat cross-
        # check dedup doang) SENGAJA gak dianggap fatal kalau belum ada.
        # Sebelumnya ini di-treat sama kayak TARGET_TAB_NAME (wajib ada),
        # padahal tab shift lawan wajar belum dibuat -- misal pas shift
        # PAGI mulai duluan, tab "<tanggal> malam" emang belum ada sama
        # sekali karena shift malam belum jalan. Akibatnya validate_setup
        # nolak start walau TARGET_TAB_NAME-nya sendiri udah bener ada.
        # get_existing_ids_from_tabs() di bawah udah bisa handle tab yang
        # gak ketemu (skip + warning), jadi di sini cukup warning juga,
        # bukan fatal.
        for tab in DUPLICATE_CHECK_EXTRA_TABS:
            if tab not in available_target:
                log(
                    f'⚠️ Tab dedup-check "{tab}" belum ada di target sheet '
                    f'(wajar kalau shift lawan belum mulai) -- akan di-skip '
                    f'dari cross-check duplikat, TIDAK menghentikan proses.',
                    'warning'
                )

    return problems


def wait_until_config_fixed(client):
    """(v4, baru) Loop nunggu sampai validate_setup() gak nemu masalah
    lagi. Dipanggil pas ada FatalConfigError, baik di awal startup
    maupun di tengah main loop (misal tab ke-rename/kehapus pas lagi
    jalan)."""
    while True:
        time.sleep(CONFIG_RECHECK_INTERVAL_SEC)
        problems = validate_setup(client)
        if not problems:
            log('✅ Config udah oke lagi, lanjut jalan normal.')
            notify_discord('✅ Config sudah dibenerin, Auto Copy Tele lanjut jalan normal.')
            return


# ---------------- CORE LOGIC ----------------
def auto_copy_tele(state, client):
    source_sheet = client.open_by_key(SOURCE_SHEET_ID)
    data_sheet = with_retry(source_sheet.worksheet, SOURCE_TAB_NAME)

    target_sheet = client.open_by_key(TARGET_SHEET_ID)
    target_tab = with_retry(target_sheet.worksheet, TARGET_TAB_NAME)

    all_data = with_retry(data_sheet.get_all_values)

    # ID-based checkpoint: kita gak peduli posisi row, kita cuma
    # peduli "ID mana yang BELUM pernah kita proses". Ini eliminasi
    # total kelas bug row-count-mismatch dari versi sebelum v3.
    processed_ids = set(state.get('processed_ids', []))

    # Reconciliation (v5, fix dari v4): sebelumnya self-heal cuma
    # trigger kalau processed_ids KOSONG TOTAL -- masalahnya state
    # file ini global & numpuk terus dari hari-hari sebelumnya, jadi
    # begitu ganti shift (misal dari "8 juli pagi" ke "8 juli malam"),
    # dia gak akan pernah kosong lagi -> ID yang dipindah MANUAL ke
    # tab shift yang baru gak pernah ke-detect -> resiko ke-copy dobel.
    #
    # Sekarang reconciliation jalan tiap kali TARGET_TAB_NAME beda dari
    # run terakhir (artinya ganti shift), ATAU state emang kosong
    # (first run / state file baru/corrupt). Hasilnya di-UNION ke
    # processed_ids yang udah ada (bukan replace), jadi history lama
    # tetep aman.
    shift_changed = state.get('last_target_tab') != TARGET_TAB_NAME
    if shift_changed or not processed_ids:
        reason = 'ganti shift terdeteksi' if shift_changed else 'state kosong'
        log(f'Reconciliation ({reason}): scan ID dari target sheet + extra tabs...')
        tabs_to_check = [TARGET_TAB_NAME] + DUPLICATE_CHECK_EXTRA_TABS
        scanned_ids = get_existing_ids_from_tabs(target_sheet, tabs_to_check)
        before = len(processed_ids)
        processed_ids |= scanned_ids
        state['processed_ids'] = list(processed_ids)
        state['last_target_tab'] = TARGET_TAB_NAME
        save_state(state)
        log(f'Reconciliation selesai: +{len(processed_ids) - before} ID baru ter-track '
            f'(total {len(processed_ids)}) dari {tabs_to_check}.')

    valid_rows = []
    # row 1 (index 0) di source SELALU header ("Invoice ID, Username,
    # Password, Nama Produk"), jadi di-skip -- kalau ini ikut ke-loop,
    # dia bakal dianggap "baris valid" karena kolom D-nya (header text)
    # gak kosong dan belum pernah diproses.
    for row in all_data[1:]:
        if len(row) >= 4 and row[3] and row[3].strip():
            invoice_id = row[3].strip()
            if invoice_id not in processed_ids:
                valid_rows.append(row)

    if not valid_rows:
        return None

    # Dedup dalam batch ini sendiri (kalau ada ID kembar di source)
    rows_to_paste = []
    seen = set()
    for row in valid_rows:
        invoice_id = row[3].strip()
        if invoice_id in seen:
            continue
        seen.add(invoice_id)
        rows_to_paste.append(row[3:7])

    target_last_row = find_last_row_with_data(target_tab, 3)
    start_row = target_last_row + 1
    end_row = start_row + len(rows_to_paste) - 1
    cell_range = f'C{start_row}:F{end_row}'

    with_retry(target_tab.update, range_name=cell_range, values=rows_to_paste, value_input_option='USER_ENTERED')

    # Update state SETELAH write sukses (biar kalau write gagal,
    # next retry masih nyoba ID yang sama -- idempotent by design)
    for row in rows_to_paste:
        processed_ids.add(row[0].strip())
    state['processed_ids'] = list(processed_ids)
    state['stats']['last_run'] = datetime.now().isoformat()
    state['stats']['total_processed'] = len(processed_ids)
    save_state(state)

    log(f'Target last row: {target_last_row}, Start paste: {start_row}, batch write ke {cell_range}')

    return {'copied': len(rows_to_paste), 'start_row': start_row}


def run_auto():
    log('=' * 50)
    log('Sheet Worker Tele - Created by Dits')
    log('=' * 50)

    # (fix baru) Log persis config yang lagi aktif + dari file mana
    # script ini kebaca, begitu start. Ini buat nyegah kasus kayak
    # kemarin: kelihatan udah edit SHIFT='pagi', tapi run yang beneran
    # jalan ternyata masih baca versi lama (beda file / belum ke-save /
    # .bat manggil auto.py di folder lain). Dengan baris ini, tiap kali
    # run keliatan jelas di log/console SHIFT & TARGET_TAB_NAME apa yang
    # BENERAN dipakai -- tinggal dicocokin sama yang lo maksud.
    log(f'SHIFT_DATE       : "{SHIFT_DATE}"')
    log(f'SHIFT            : "{SHIFT}"')
    log(f'SINGLE_SHIFT_DAY : {SINGLE_SHIFT_DAY}')
    log(f'TARGET_TAB_NAME  : "{TARGET_TAB_NAME}"')
    log(f'DUPLICATE_CHECK_EXTRA_TABS: {DUPLICATE_CHECK_EXTRA_TABS}')
    log('=' * 50)

    state = load_state()
    client = get_client()

    # --- Validasi setup SEBELUM mulai proses apa pun (fix v4) ---
    problems = validate_setup(client)
    if problems:
        msg = 'Setup bermasalah, script BELUM mulai proses:\n' + '\n'.join(f'- {p}' for p in problems)
        log(msg, 'error')
        notify_discord('🔴 ' + msg)
        log(f'Nunggu config dibenerin (cek ulang tiap {CONFIG_RECHECK_INTERVAL_SEC}s)...')
        wait_until_config_fixed(client)

    last_heartbeat = time.time()
    today_copied_count = 0
    current_day = date.today()

    notify_discord('✅ Auto Copy Tele START — script mulai jalan.')

    counter = 0
    consecutive_errors = 0

    while True:
        try:
            counter += 1

            # reset counter harian
            if date.today() != current_day:
                current_day = date.today()
                today_copied_count = 0

            result = auto_copy_tele(state, client)
            consecutive_errors = 0

            if result:
                today_copied_count += result['copied']
                log(f'✅ {result["copied"]} baris dicopy ke "{TARGET_TAB_NAME}" mulai row {result["start_row"]}')
            elif counter % 180 == 0:  # ~30 menit sekali kalau idle
                log('⏳ Masih idle, gak ada data baru dalam 30 menit terakhir.')

            # heartbeat
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL_SEC:
                notify_discord(
                    f'💓 Auto Copy Tele masih hidup.\n'
                    f'Total baris diproses hari ini: {today_copied_count}\n'
                    f'Total ID ter-track: {len(state.get("processed_ids", []))}'
                )
                last_heartbeat = time.time()

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            log('Dihentikan manual oleh user (CTRL+C).')
            notify_discord('🛑 Auto Copy Tele dihentikan manual.')
            break

        except FatalConfigError as e:
            # (fix v4) Error konfigurasi permanen -- BUKAN error transient.
            # Gak nambah consecutive_errors (jangan sampai trigger restart
            # loop via .bat, karena restart gak bakal nge-fix tab yang
            # emang gak ada). Tunggu sampai dibenerin manual, cek ulang
            # tiap CONFIG_RECHECK_INTERVAL_SEC, notif Discord cuma sekali
            # di awal biar gak spam.
            log(f'❌ Fatal config error: {e}', 'error')
            notify_discord(
                f'🔴 Fatal config error, script PAUSE sampai dibenerin:\n{e}\n'
                f'Cek ulang tiap {CONFIG_RECHECK_INTERVAL_SEC} detik.'
            )
            wait_until_config_fixed(client)
            continue

        except Exception as e:
            consecutive_errors += 1
            log(f'❌ Error: {e}', 'error')

            if consecutive_errors == 1 or consecutive_errors % 5 == 0:
                notify_discord(f'🚨 Auto Copy Tele error ({consecutive_errors}x berturut-turut):\n{e}')

            if consecutive_errors >= 20:
                log('Error 20x berturut-turut, exit biar wrapper .bat restart proses dari nol.', 'error')
                notify_discord('🔴 Auto Copy Tele: 20x error berturut-turut, proses akan di-restart otomatis.')
                raise SystemExit(1)  # exit code != 0 -> wrapper .bat akan restart

            time.sleep(POLL_INTERVAL_SEC)


if __name__ == '__main__':
    run_auto()