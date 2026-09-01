import io
import os
import re
import unicodedata

from PIL import Image, ImageDraw, ImageFont
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ============================================================
# CONFIG
# ============================================================
CREDENTIALS_FILE = 'credentials.json'

PERSONAL_INFO_SS_ID = '1XVwxahQCx6DkVceeQn7nvvukobHgOMmVGlDA7w7MgyM'
STATS_SS_ID = '1X2aHrrqzusSr8wbhwAPiCUUYaK7qyfzq9O9OwA1UVE0'
MATCH_STATS_SS_ID = '1T4VG3O1Zn56PNaYtwtwWxuaDT920UnTcDRNZxXEmODA'

NEW_BACKGROUNDS_FOLDER_ID = '1_7aISOAf4WvFsCwnyGubBttwi7b4RKVP'
OLD_BACKGROUNDS_FOLDER_ID = '1pfkZY9CVI99HxAvcxU34jyVCYGfDyOGO'
ASSETS_FOLDER_ID = '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'

FONT_NAME = 'Etna'
OUTPUT_DIR = 'output/players_cards'
_FONT_LOCAL = os.path.join('output', '_etna.ttf')
IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp')

TEXT_COLOR = (255, 255, 255)
STROKE_COLOR = (0, 0, 0)

X_COLS = [0.135, 0.380, 0.620, 0.865]
LABEL_Y_ROWS = [0.523, 0.640, 0.752, 0.867]
_gaps = [LABEL_Y_ROWS[i+1] - LABEL_Y_ROWS[i] for i in range(len(LABEL_Y_ROWS)-1)]
_avg_gap = sum(_gaps) / len(_gaps)

Y_ROWS = [
    (LABEL_Y_ROWS[i] + LABEL_Y_ROWS[i+1]) / 2 if i < len(LABEL_Y_ROWS) - 1
    else LABEL_Y_ROWS[i] + (_avg_gap / 2)
    for i in range(len(LABEL_Y_ROWS))
]

NUMBER_FONT_SIZE_PCT = 0.040
STROKE_WIDTH_PCT = 0.005

GK_PREFIX = '(G)'

# ============================================================
# SERVICE AUTH
# ============================================================
def get_creds():
    scope = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)

def get_gspread_client():
    return gspread.authorize(get_creds())

def get_drive_service():
    return build('drive', 'v3', credentials=get_creds())

# ============================================================
# HELPERS
# ============================================================
def _norm(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())

def download_file_bytes(drive, file_id):
    request = drive.files().get_media(fileId=file_id)
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return file_stream.getvalue()

def list_folder_files(drive, folder_id):
    out, page = [], None
    while True:
        resp = drive.files().list(
            q="'%s' in parents and trashed = false" % folder_id,
            fields='nextPageToken, files(id,name,mimeType)', pageToken=page).execute()
        out.extend(resp.get('files', []))
        page = resp.get('nextPageToken')
        if not page: break
    return out

def ensure_font(drive):
    if os.path.exists(_FONT_LOCAL): return _FONT_LOCAL
    os.makedirs('output', exist_ok=True)
    files = list_folder_files(drive, ASSETS_FOLDER_ID)
    target = _norm(FONT_NAME)
    f = next((fl for fl in files if _norm(os.path.splitext(fl['name'])[0]) == target), None)
    if not f: return None
    try:
        with open(_FONT_LOCAL, 'wb') as fh:
            fh.write(download_file_bytes(drive, f['id']))
        return _FONT_LOCAL
    except Exception as e:
        print(f"⚠️ Error downloading custom font: {e}")
        return None

def is_gk_filename(filename):
    """Detects if a template filename marks a goalkeeper (starts with (G))."""
    return filename.strip().upper().startswith(GK_PREFIX.upper())

def strip_gk_prefix(filename):
    """Removes the (G) prefix from a filename before name-normalizing it."""
    name = filename.strip()
    if is_gk_filename(name):
        name = name[len(GK_PREFIX):].strip()
    return name

# ============================================================
# DATA INGESTION (Roster & Stats)
# ============================================================
def get_active_roster(client):
    ss = client.open_by_key(PERSONAL_INFO_SS_ID)
    rows = ss.worksheet('Personal Info').get_all_values()

    name_col = 0
    status_col = 19

    active_players = {}
    for r in rows[1:]:
        if len(r) <= name_col: continue
        name = r[name_col].strip()
        status = r[status_col].strip().lower() if len(r) > status_col else ''
        if name and status in ('active', 'basketball'):
            active_players[_norm(name)] = name

    print(f"Retrieved {len(active_players)} active/basketball players from Roster.")
    return active_players

def parse_season_score(tab_name):
    m = re.search(r'(Spring|Fall)\s+(\d{4})', tab_name, re.I)
    if not m: return 0
    season, year = m.group(1).lower(), int(m.group(2))
    score = year * 10
    if season == 'fall': score += 5
    return score

def load_stats_sheets(client):
    """Load stats maps for regular 6-a-side groups, including GK-specific columns."""
    ss = client.open_by_key(STATS_SS_ID)
    sheets = ss.worksheets()

    reg_seasons = [s.title for s in sheets if re.match(r'^(Spring|Fall)\s+\d{4}', s.title, re.I)]
    latest_reg = max(reg_seasons, key=parse_season_score) if reg_seasons else None

    print(f"Detected Current Season -> Reg: '{latest_reg}'")

    data_maps = {}
    for tab in ['All Time', latest_reg]:
        if not tab: continue
        rows = ss.worksheet(tab).get_all_values()

        idx = {
            'name': 0, 'mp': 1, 'goals': 2, 'assists': 4,
            'gk_matches': 6, 'goals_allowed': 7, 'goals_allowed_per_match': 8,
            'cs': 9, 'motm': 10, 'wins': 11, 'win_pct': 12,
            'yellow': 13, 'red': 14
        }

        tab_map = {}
        for r in rows[1:]:
            if not r or not r[0].strip(): continue
            name_key = _norm(r[0])

            win_pct_raw = row_val_or_default(r, idx['win_pct'], '0')
            try:
                v = float(win_pct_raw.replace('%', ''))
                win_pct_str = f"{int(round(v * 100))}%" if v <= 1.0 else f"{int(round(v))}%"
            except ValueError:
                win_pct_str = '0%'

            gapm_raw = row_val_or_default(r, idx['goals_allowed_per_match'], '0')
            try:
                gapm_val = round(float(gapm_raw), 1)
            except ValueError:
                gapm_val = 0.0

            tab_map[name_key] = {
                'name': r[idx['name']].strip(),
                'mp': str(row_val_or_default(r, idx['mp'])),
                'goals': str(row_val_or_default(r, idx['goals'])),
                'assists': str(row_val_or_default(r, idx['assists'])),
                'gk_matches': str(row_val_or_default(r, idx['gk_matches'])),
                'goals_allowed': str(row_val_or_default(r, idx['goals_allowed'])),
                'goals_allowed_per_match': str(gapm_val),
                'cs': str(row_val_or_default(r, idx['cs'])),
                'motm': str(row_val_or_default(r, idx['motm'])),
                'wins': str(row_val_or_default(r, idx['wins'])),
                'win_pct': win_pct_str,
                'yellow': str(row_val_or_default(r, idx['yellow'])),
                'red': str(row_val_or_default(r, idx['red']))
            }
        data_maps[tab] = tab_map

    return data_maps, latest_reg

def row_val_or_default(row, index, default='0'):
    return row[index] if index < len(row) else default

# ============================================================
# CARD COMPILER
# ============================================================
def build_card(bg_bytes, font_path, s_stats, a_stats, is_gk):
    img = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
    W, H = img.size
    draw = ImageDraw.Draw(img)

    font_size = int(H * NUMBER_FONT_SIZE_PCT)
    stroke_w = int(H * STROKE_WIDTH_PCT)

    if font_path and os.path.exists(font_path):
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()

    if is_gk:
        # Row 1: Matches Goalie | MotM  ||  Matches Goalie | MotM
        # Row 2: Goals Allowed/Match | Clean Sheets  ||  same (All Time)
        metrics = [
            [s_stats.get('gk_matches', '0'), s_stats.get('motm', '0'), a_stats.get('gk_matches', '0'), a_stats.get('motm', '0')],
            [s_stats.get('goals_allowed_per_match', '0'), s_stats.get('cs', '0'), a_stats.get('goals_allowed_per_match', '0'), a_stats.get('cs', '0')],
            [s_stats.get('wins', '0'), s_stats.get('win_pct', '0%'), a_stats.get('wins', '0'), a_stats.get('win_pct', '0%')],
            [s_stats.get('yellow', '0'), s_stats.get('red', '0'), a_stats.get('yellow', '0'), a_stats.get('red', '0')]
        ]
    else:
        metrics = [
            [s_stats.get('mp', '0'), s_stats.get('motm', '0'), a_stats.get('mp', '0'), a_stats.get('motm', '0')],
            [s_stats.get('goals', '0'), s_stats.get('assists', '0'), a_stats.get('goals', '0'), a_stats.get('assists', '0')],
            [s_stats.get('wins', '0'), s_stats.get('win_pct', '0%'), a_stats.get('wins', '0'), a_stats.get('win_pct', '0%')],
            [s_stats.get('yellow', '0'), s_stats.get('red', '0'), a_stats.get('yellow', '0'), a_stats.get('red', '0')]
        ]

    for r_idx, row_metrics in enumerate(metrics):
        y_pos = int(H * Y_ROWS[r_idx])
        for c_idx, val in enumerate(row_metrics):
            x_pos = int(W * X_COLS[c_idx])
            draw.text((x_pos, y_pos), val, font=font, fill=TEXT_COLOR,
                      anchor="mm", stroke_width=stroke_w, stroke_fill=STROKE_COLOR)

    return img.convert('RGB')

# ============================================================
# CARD GENERATION CORE
# ============================================================
def generate_and_save_card(drive, norm_name, display_name, bg_map, font_path, data_maps, latest_reg, is_gk):
    a_stats = data_maps['All Time'].get(norm_name)
    s_stats = data_maps[latest_reg].get(norm_name, {}) if latest_reg else {}

    if not a_stats:
        a_stats = {
            'name': display_name, 'mp': '0', 'goals': '0', 'assists': '0',
            'gk_matches': '0', 'goals_allowed': '0', 'goals_allowed_per_match': '0',
            'cs': '0', 'motm': '0', 'wins': '0', 'win_pct': '0%', 'yellow': '0', 'red': '0'
        }
        s_stats = {}

    role_label = 'Goalkeeper' if is_gk else '6-a-side'
    print(f"Generating Card: {a_stats['name']} ({role_label})")

    try:
        bg_file_record = bg_map[norm_name]
        bg_bytes = download_file_bytes(drive, bg_file_record['id'])

        card_img = build_card(bg_bytes, font_path, s_stats, a_stats, is_gk)
        safe_filename = a_stats['name'].strip()
        out_path = os.path.join(OUTPUT_DIR, f"{safe_filename}_card.png")
        card_img.save(out_path, 'PNG', quality=95)
        return out_path
    except Exception as err:
        print(f"❌ Failed to build card for {display_name}: {err}")
        return None

# ============================================================
# PRODUCTION PIPELINE
# ============================================================
def run_player_cards_pipeline():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Connecting to Google Services (Service Account)...")
    client = get_gspread_client()
    drive = get_drive_service()

    print("Loading fonts...")
    font_path = ensure_font(drive)

    active_roster = get_active_roster(client)
    data_maps, latest_reg = load_stats_sheets(client)

    print("Reading new backgrounds folder...")
    new_templates = list_folder_files(drive, NEW_BACKGROUNDS_FOLDER_ID)
    new_bg_map = {}
    new_gk_set = set()
    for t in new_templates:
        raw_name, ext = os.path.splitext(t['name'])
        if t.get('mimeType', '').startswith('image/') or (not ext) or ext.lower() in IMG_EXT:
            gk_flag = is_gk_filename(raw_name)
            clean_name = strip_gk_prefix(raw_name)
            norm_key = _norm(clean_name)
            new_bg_map[norm_key] = t
            if gk_flag:
                new_gk_set.add(norm_key)

    print("Reading old backgrounds folder...")
    old_templates = list_folder_files(drive, OLD_BACKGROUNDS_FOLDER_ID)
    old_bg_map = {}
    for t in old_templates:
        raw_name, ext = os.path.splitext(t['name'])
        if t.get('mimeType', '').startswith('image/') or (not ext) or ext.lower() in IMG_EXT:
            clean_name = strip_gk_prefix(raw_name)
            old_bg_map[_norm(clean_name)] = t

    generated_paths = []

    # ------------------------------------------------------------
    # TASK 1: Compare Folder logic (Active roster, in NEW but not in OLD)
    # ------------------------------------------------------------
    print("\n--- Running Task 1: Roster Folder Comparison ---")
    for norm_name, display_name in active_roster.items():
        if norm_name in new_bg_map and norm_name not in old_bg_map:
            is_gk = norm_name in new_gk_set
            out_path = generate_and_save_card(drive, norm_name, display_name, new_bg_map, font_path, data_maps, latest_reg, is_gk)
            if out_path:
                generated_paths.append(out_path)

    # ------------------------------------------------------------
    # TASK 2: Match Trigger logic (From Match Log Sheet)
    # ------------------------------------------------------------
    print("\n--- Running Task 2: Match Sheet Processing ---")
    match_ss = client.open_by_key(MATCH_STATS_SS_ID)
    worksheets = match_ss.worksheets()

    for ws in worksheets:
        if 'vets' in ws.title.lower() or ws.title.lower() in ('personal info', 'all time'):
            continue

        print(f"Checking matches in team sheet: '{ws.title}'...")
        rows = ws.get_all_values()
        if not rows:
            continue

        headers = [h.strip().lower() for h in rows[0]]
        try:
            players_col_idx = headers.index('players who played')
            stats_col_idx = headers.index('stats')
            card_col_idx = headers.index('card')
        except ValueError:
            print(f"ℹ️ Sheet '{ws.title}' skipped (missing required columns).")
            continue

        # Optional goalkeeper column: "Goalkeeper full name (add all with a comma between if more than one)"
        gk_col_idx = None
        for h_idx, h_val in enumerate(headers):
            if h_val.startswith('goalkeeper'):
                gk_col_idx = h_idx
                break

        for row_num, row_vals in enumerate(rows[1:], start=2):
            if len(row_vals) <= max(players_col_idx, stats_col_idx, card_col_idx):
                continue

            stats_val = row_vals[stats_col_idx].strip().lower()
            card_val = row_vals[card_col_idx].strip().lower()

            if stats_val != 'friendly' and card_val != 'completed':
                players_raw = row_vals[players_col_idx].strip()
                if not players_raw:
                    continue

                match_players = [p.strip() for p in players_raw.split(',') if p.strip()]

                gk_names_in_row = set()
                if gk_col_idx is not None and len(row_vals) > gk_col_idx:
                    gk_raw = row_vals[gk_col_idx].strip()
                    if gk_raw:
                        gk_names_in_row = {_norm(g.strip()) for g in gk_raw.split(',') if g.strip()}

                print(f"Processing row {row_num} in '{ws.title}' for: {match_players}")

                for player_name in match_players:
                    norm_player_name = _norm(player_name)
                    if norm_player_name in new_bg_map:
                        is_gk = (norm_player_name in new_gk_set) or (norm_player_name in gk_names_in_row)
                        out_path = generate_and_save_card(drive, norm_player_name, player_name, new_bg_map, font_path, data_maps, latest_reg, is_gk)
                        if out_path:
                            generated_paths.append(out_path)

                ws.update_cell(row_num, card_col_idx + 1, 'Completed')
                print(f"Row {row_num} in '{ws.title}' marked as 'Completed'.")

    print(f"\nCompleted Generation: {len(generated_paths)} cards created/updated in total.")
    print("Pipeline Execution Complete.")

if __name__ == '__main__':
    run_player_cards_pipeline()
