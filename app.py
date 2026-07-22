from flask import Flask, render_template, request, redirect, flash, url_for, jsonify
import os
from datetime import datetime, timedelta
import calendar
import csv
import io
import json
from collections import Counter
from threading import RLock
from urllib.parse import urlencode
from uuid import uuid4
from merge_pppoe import main as merge_pppoe_automatically
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "development-only-change-this-key")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_IMPORT_BYTES", str(10 * 1024 * 1024)))

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = Path(os.environ.get("PPPOE_ROOT", os.environ.get("JSS_ROOT", BASE_DIR)))
DATA_DIR = ROOT_DIR / "data"
RUNTIME_DIR = ROOT_DIR / "runtime"

APP_NAME = os.environ.get("APP_NAME", "PPPoE Monitoring")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MIKROTIK_API_TOKEN = os.environ.get("MIKROTIK_API_TOKEN", "").strip()
MAP_DEFAULT_LAT = float(os.environ.get("MAP_DEFAULT_LAT", "-2.5489"))
MAP_DEFAULT_LNG = float(os.environ.get("MAP_DEFAULT_LNG", "118.0149"))
MAP_DEFAULT_ZOOM = int(os.environ.get("MAP_DEFAULT_ZOOM", "5"))
ROUTER_STALE_MINUTES = int(os.environ.get("ROUTER_STALE_MINUTES", "15"))
BLACKLIST_PREFIXES = tuple(
    prefix.strip().casefold()
    for prefix in os.environ.get("PPPOE_BLACKLIST_PREFIXES", "").split(",")
    if prefix.strip()
)

NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
NOTIFICATIONS_BAK_FILE = f"{NOTIFICATIONS_FILE}.bak"
OUTAGES_FILE = DATA_DIR / "outages.json"
BACKUP_DIR = RUNTIME_DIR / "backups"
HISTORY_FILE = RUNTIME_DIR / "history.json"
QUEUE_FILE = DATA_DIR / "queue_user_pppoe.json"
USER_PPPOE_FILE = DATA_DIR / "user_pppoe.json"
USER_LOG_FILE = RUNTIME_DIR / "user_log.json"
USER_TEMP_FILE = DATA_DIR / "user_temp.json"
USER_PPPOES_FILE = DATA_DIR / "pppoes.json"
PPPOES_TEMP_FILE = DATA_DIR / "pppoes_temp.json"
MANUAL_FILE = DATA_DIR / "user_manual.json"

DATA_LOCK = RLock()
router_state_cache = {}
global_history_buffer = None


def _ensure_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    defaults = {
        NOTIFICATIONS_FILE: [],
        QUEUE_FILE: [],
        USER_TEMP_FILE: {},
        USER_PPPOES_FILE: [],
        PPPOES_TEMP_FILE: [],
        MANUAL_FILE: [],
        HISTORY_FILE: [],
        USER_LOG_FILE: [],
    }
    for file_path, default_value in defaults.items():
        if not file_path.exists():
            file_path.write_text(json.dumps(default_value, indent=2), encoding="utf-8")


def _read_json(file_path, default):
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _api_authorized():
    if not MIKROTIK_API_TOKEN:
        return True
    supplied = request.headers.get("X-API-Token") or request.args.get("token", "")
    return supplied == MIKROTIK_API_TOKEN


def _unauthorized():
    return jsonify({"status": "error", "message": "Token API tidak valid."}), 401


def _normalize_status(value):
    normalized = str(value or "OFFLINE").strip().upper()
    return "ONLINE" if normalized in {"ONLINE", "ON", "ACTIVE", "UP", "TRUE", "1"} else "OFFLINE"


def _safe_float(value):
    if value in (None, "", "-"):
        return "-"
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return "-"


def _record_username(record):
    return str(record.get("user") or record.get("username_pppoe") or record.get("username") or "").strip()


def _normalize_import_record(record):
    username = _record_username(record)
    if not username:
        return None
    customer_id = record.get("id") or record.get("id_pelanggan") or "-"
    latitude = _safe_float(record.get("latitude"))
    longitude = _safe_float(record.get("longitude"))
    return {
        "router": str(record.get("router") or "Belum ditentukan").strip(),
        "id": customer_id,
        "id_pelanggan": customer_id,
        "user": username,
        "nama_asli": str(record.get("nama_asli") or record.get("nama") or "").strip(),
        "lokasi": str(record.get("lokasi") or record.get("alamat") or "-").strip(),
        "latitude": latitude,
        "longitude": longitude,
        "mac": str(record.get("mac") or "-").strip(),
        "ip": str(record.get("ip") or "-").strip(),
        "uptime": str(record.get("uptime") or "-").strip(),
        "status": _normalize_status(record.get("status")),
        "profile": str(record.get("profile") or "").strip(),
        "password": str(record.get("password") or "").strip(),
    }


def _merge_records(existing, incoming):
    merged = {}
    order = []
    for row in list(existing) + list(incoming):
        username = _record_username(row)
        if not username:
            continue
        key = username.casefold()
        if key not in merged:
            order.append(key)
        merged[key] = row
    return [merged[key] for key in order]


def _parse_import_payload():
    if request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            for key in ("pppoe", "customers", "data"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise ValueError("JSON harus berupa array data pelanggan.")
        return payload

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        raise ValueError("Pilih file JSON atau CSV terlebih dahulu.")

    content = uploaded.read().decode("utf-8-sig")
    if uploaded.filename.lower().endswith(".json"):
        payload = json.loads(content)
        if isinstance(payload, dict):
            for key in ("pppoe", "customers", "data"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise ValueError("Isi JSON harus berupa array data pelanggan.")
        return payload

    if uploaded.filename.lower().endswith(".csv"):
        sample = content[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(io.StringIO(content), dialect=dialect))

    raise ValueError("Format file harus .json atau .csv.")


def _active_filters(pppoes):
    areas = {}
    routers = set()
    for row in pppoes:
        username = _record_username(row)
        if "-" in username:
            prefix = username.split("-", 1)[0].strip()
            if prefix:
                areas[prefix.casefold()] = prefix.replace("_", " ").title()
        router = str(row.get("router") or "").strip()
        if router and router != "Belum ditentukan":
            routers.add(router)
    area_filters = [{"value": key, "label": areas[key]} for key in sorted(areas)]
    return area_filters, sorted(routers, key=str.casefold)


@app.context_processor
def inject_app_config():
    return {
        "app_name": APP_NAME,
        "map_default_lat": MAP_DEFAULT_LAT,
        "map_default_lng": MAP_DEFAULT_LNG,
        "map_default_zoom": MAP_DEFAULT_ZOOM,
    }


_ensure_storage()

def load_data():
    data = _read_json(USER_PPPOES_FILE, [])
    if isinstance(data, dict):
        data = data.get("pppoe", [])
    return data if isinstance(data, list) else []

def load_notifications():
    """Load notifications from primary file; fall back to backup on decode error."""
    try:
        with open(NOTIFICATIONS_FILE, 'r') as f:
            notifications = json.load(f)
        if not isinstance(notifications, list):
            raise ValueError("Notifications file does not contain a list")
        for notif in notifications:
            if not isinstance(notif, dict):
                raise ValueError("Invalid notification format")
            if 'id' not in notif or 'message' not in notif or 'timestamp' not in notif:
                raise ValueError("Notification missing required fields")
        return notifications
    except FileNotFoundError:
        print("Notifications file not found, trying to recover from backup...")
        return _recover_notifications_from_backup()
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Notifications file corrupted: {e}, trying to recover from backup...")
        return _recover_notifications_from_backup()

def _recover_notifications_from_backup():
    """Mencoba memulihkan notifikasi dari backup terbaru"""
    try:
        import glob
        backup_pattern = f'{BACKUP_DIR}/notifications-*.json'
        backup_files = glob.glob(backup_pattern)
        if not backup_files:
            print("No notification backups found, starting fresh")
            return []
        backup_files.sort(key=os.path.getmtime, reverse=True)
        latest_backup = backup_files[0]
        print(f"Recovering notifications from: {latest_backup}")
        with open(latest_backup, 'r') as f:
            notifications = json.load(f)
        save_notifications(notifications)
        print(f"Successfully recovered {len(notifications)} notifications")
        return notifications
    except Exception as e:
        print(f"Failed to recover notifications from backup: {e}")
        return []

def _atomic_write_json(file_path, data):
    dir_name = os.path.dirname(file_path) or '.'
    temp_path = os.path.join(dir_name, f".tmp-{os.path.basename(file_path)}")
    os.makedirs(dir_name, exist_ok=True)
    with open(temp_path, 'w', encoding='utf-8') as tf:
        json.dump(data, tf, indent=2, ensure_ascii=False)
        tf.flush()
        try:
            os.fsync(tf.fileno())
        except Exception:
            pass
    try:
        with open(f"{file_path}.bak", 'w', encoding='utf-8') as bf:
            json.dump(data, bf, indent=2, ensure_ascii=False)
    except Exception:
        pass
    os.replace(temp_path, file_path)

def save_notifications(notifications):
    _atomic_write_json(NOTIFICATIONS_FILE, notifications)

def add_notification(message, notification_type="info", ont_id=None, ont_name=None, timestamp=None):
    notifications = load_notifications()
    next_id = (max((n.get('id', 0) for n in notifications), default=0) + 1)
    new_notification = {
        "id": next_id, "message": message, "type": notification_type,
        "timestamp": (timestamp or datetime.now().isoformat()),
        "ont_id": ont_id, "ont_name": ont_name, "read": False
    }
    notifications.append(new_notification)
    _backup_notifications(notifications)
    save_notifications(notifications)
    return new_notification

def _backup_notifications(notifications):
    try:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = f'{BACKUP_DIR}/notifications-{timestamp}.json'
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(backup_path, 'w') as f:
            json.dump(notifications, f, indent=2)
        _cleanup_old_backups('notifications-*.json', 10)
    except Exception as e:
        print(f"Warning: Failed to backup notifications: {e}")

def _cleanup_old_backups(pattern, keep_count):
    try:
        import glob
        backup_files = glob.glob(f'{BACKUP_DIR}/{pattern}')
        backup_files.sort(key=os.path.getmtime, reverse=True)
        for old_file in backup_files[keep_count:]:
            try:
                os.remove(old_file)
            except:
                pass
    except Exception as e:
        print(f"Warning: Failed to cleanup old backups: {e}")

def save_and_backup(onts):
    _atomic_write_json(USER_PPPOES_FILE, onts)
    _atomic_write_json(PPPOES_TEMP_FILE, onts)

@app.route('/')
def map_view():
    return render_template('map.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/analytics')
def analytics_page():
    """Menampilkan halaman analitik baru."""
    return render_template('analytics.html')

@app.route('/healthz')
def healthz():
    """Endpoint sederhana untuk cek kesehatan service."""
    return jsonify({"status": "ok"})

@app.route('/admin')
def admin():
    pppoes = load_data()
    area_filters, routers = _active_filters(pppoes)
    return render_template('list.html', pppoes=pppoes, area_filters=area_filters, routers=routers)


@app.route('/api/import-customers', methods=['POST'])
def import_customers():
    """Impor data daerah baru dari JSON/CSV dan gabungkan berdasarkan username."""
    global global_history_buffer
    try:
        raw_rows = _parse_import_payload()
        normalized_rows = []
        invalid_count = 0
        for row in raw_rows:
            if not isinstance(row, dict):
                invalid_count += 1
                continue
            normalized = _normalize_import_record(row)
            if normalized:
                normalized_rows.append(normalized)
            else:
                invalid_count += 1

        if not normalized_rows:
            return jsonify({"success": False, "message": "Tidak ada username PPPoE yang valid pada file."}), 400

        mode = (request.args.get("mode") or request.form.get("mode") or "merge").strip().lower()
        if mode not in {"replace", "merge"}:
            return jsonify({"success": False, "message": "Mode impor harus replace atau merge."}), 400

        with DATA_LOCK:
            if mode == "merge":
                existing_manual = _read_json(MANUAL_FILE, [])
                existing_temp = _read_json(PPPOES_TEMP_FILE, [])
                manual_rows = _merge_records(existing_manual if isinstance(existing_manual, list) else [], normalized_rows)
                temp_rows = _merge_records(existing_temp if isinstance(existing_temp, list) else [], normalized_rows)
            else:
                manual_rows = normalized_rows
                temp_rows = normalized_rows
                _atomic_write_json(USER_TEMP_FILE, {})
                _atomic_write_json(HISTORY_FILE, [])
                _atomic_write_json(USER_LOG_FILE, [])
                _atomic_write_json(QUEUE_FILE, [])
                _atomic_write_json(NOTIFICATIONS_FILE, [])
                global_history_buffer = []

            _atomic_write_json(MANUAL_FILE, manual_rows)
            _atomic_write_json(PPPOES_TEMP_FILE, temp_rows)
            merged = merge_pppoe_automatically(DATA_DIR)

        return jsonify({
            "success": True,
            "message": f"Berhasil mengimpor {len(normalized_rows)} data pelanggan.",
            "imported": len(normalized_rows),
            "invalid": invalid_count,
            "total": len(merged),
            "mode": mode,
        })
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        print(f"[IMPORT ERROR] {exc}")
        return jsonify({"success": False, "message": "Impor gagal diproses."}), 500

@app.route('/notifications')
def notifications():
    notifications_list = load_notifications()
    notifications_list.sort(key=lambda x: x['timestamp'], reverse=True)
    return render_template('notifications.html', notifications=notifications_list)

@app.route('/api/notifications', methods=['GET', 'POST'])
def api_notifications():
    if request.method == 'POST':
        data = request.get_json()
        add_notification(data.get('message', ''), data.get('type', 'info'), None, None, timestamp=data.get('timestamp'))
        return jsonify({"success": True})
    notifications_list = load_notifications()
    notifications_list.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify(notifications_list)

@app.route('/api/notifications/mark-read/<int:notification_id>', methods=['POST'])
def mark_notification_read(notification_id):
    notifications_list = load_notifications()
    for notification in notifications_list:
        if notification['id'] == notification_id:
            notification['read'] = True
            break
    save_notifications(notifications_list)
    return jsonify({"success": True})

@app.route('/api/notifications/clear-all', methods=['POST'])
def clear_all_notifications():
    try:
        current_notifications = load_notifications()
        if current_notifications:
            _backup_notifications(current_notifications)
        save_notifications([])
        return jsonify({"success": True, "message": "Semua notifikasi berhasil dihapus."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/notifications/restore-backup', methods=['POST'])
def restore_notifications_from_backup():
    try:
        current_notifications = load_notifications()
        if current_notifications:
            _backup_notifications(current_notifications)
        restored_notifications = _recover_notifications_from_backup()
        if restored_notifications:
            return jsonify({"success": True, "message": f"Berhasil memulihkan {len(restored_notifications)} notifikasi dari backup", "count": len(restored_notifications)})
        else:
            return jsonify({"success": False, "message": "Tidak ada backup yang tersedia untuk dipulihkan"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/edit/<id>', methods=['GET', 'POST'])    
def edit_pppoe(id):
    # 1. BACA DATABASE MASTER SECARA AMAN
    data_master = load_data()
    
    if isinstance(data_master, dict):
        pppoe_list = data_master.get("pppoe", [])
    elif isinstance(data_master, list):
        pppoe_list = data_master
    else:
        pppoe_list = []
        
    # Mencari user berdasarkan ID int
    pppoe_user = next((p for p in pppoe_list if str(p.get('user')) == id or str(p.get('name')) == id or str(p.get('id')) == id), None)
    
    if not pppoe_user:
        return "User PPPoE tidak ditemukan", 404
        
    if request.method == 'POST':
        # Tangkap data baru dari form edit (Sesuai dengan tag name="" di HTML terbaru)
        target_router = request.form.get('router_name', pppoe_user.get('router', ''))
        username_pppoe = request.form.get('username_pppoe') # Ini Primary Key (Readonly dari HTML)
        
        new_id_pelanggan = request.form.get('id_pelanggan', '')
        new_nama = request.form.get('nama', pppoe_user.get('nama_asli', ''))
        new_lokasi = request.form.get('lokasi', '')
        new_ip = request.form.get('ip', '')
        new_latitude = request.form.get('latitude', '')
        new_longitude = request.form.get('longitude', '')
        submitted_password = request.form.get('password', '').strip()
        new_password = submitted_password or str(pppoe_user.get('password') or '')
        new_profile = request.form.get('profile', '').strip() or str(pppoe_user.get('profile') or '')

        # ---------------------------------------------------------
        # A. LOGIKA BARU: MASUKKAN INSTRUKSI EDIT KE ANTREAN (QUEUE)
        # ---------------------------------------------------------
        queue_data = []
        if os.path.exists(QUEUE_FILE):
            try:
                with open(QUEUE_FILE, 'r') as qf:
                    queue_data = json.load(qf)
                    if not isinstance(queue_data, list):
                        queue_data = []
            except (json.JSONDecodeError, FileNotFoundError):
                queue_data = []

        # Susun instruksi edit
        queue_item = {
            "queue_id": uuid4().hex,
            "id": new_id_pelanggan,
            "icon": 119,
            "action": "edit",
            "lokasi": new_lokasi,
            "latitude": new_latitude,
            "longitude": new_longitude,
            "mac": pppoe_user.get('mac', ''),
            "router": target_router,
            "username_pppoe": username_pppoe, # MikroTik akan mencari akun berdasarkan nama ini
            "password": submitted_password,
            "profile": new_profile,
            "ip": new_ip,
            "timestamp": datetime.now().isoformat()
        }

        queue_data = [
            q for q in queue_data
            if not (
                str(q.get("username_pppoe") or "").casefold() == str(username_pppoe or "").casefold()
                and str(q.get("router") or "").casefold() == str(target_router or "").casefold()
            )
        ]
        queue_data.append(queue_item)

        try:
            _atomic_write_json(QUEUE_FILE, queue_data)
            print(f"--- [QUEUE EDIT SUCCESS] Perintah 'edit' {username_pppoe} disimpan ke antrean ---")
        except Exception as e:
            print(f"[QUEUE ERROR] Gagal menulis file antrean: {e}")

        # ---------------------------------------------------------
        # B. UPDATE DATA KE DATABASE LOKAL WEB
        # ---------------------------------------------------------
        pppoe_user['id'] = new_id_pelanggan
        pppoe_user['nama_asli'] = new_nama
        pppoe_user['lokasi'] = new_lokasi
        pppoe_user['ip'] = new_ip
        pppoe_user['latitude'] = new_latitude
        pppoe_user['longitude'] = new_longitude
        pppoe_user['password'] = new_password
        pppoe_user['profile'] = new_profile
            
        # Bungkus kembali ke bentuk dict sebelum disimpan
        save_and_backup(pppoe_list)

        # ---------------------------------------------------------
        # C. LOGIKA BARU: KUNCI DATA MANUAl/KOORDINAT ABADI
        # ---------------------------------------------------------
        manual_entry = {
            "router": target_router,
            "icon": 119,
            "id": new_id_pelanggan,
            "user": username_pppoe,              
            "lokasi": new_lokasi,              
            "latitude": _safe_float(new_latitude),
            "longitude": _safe_float(new_longitude),
            "mac": pppoe_user.get('mac', '-'),
            "ip": new_ip,
            "uptime": pppoe_user.get('uptime', '-'),
            "status": pppoe_user.get('status', 'OFFLINE'),
            "nama_asli": new_nama,
            "profile": new_profile,
            "password": new_password
        }

        try:
            if os.path.exists(MANUAL_FILE):
                with open(MANUAL_FILE, 'r') as mf:
                    try:
                        manual_data = json.load(mf)
                        if not isinstance(manual_data, list): 
                            manual_data = []
                    except:
                        manual_data = []
            else:
                manual_data = []

            # Hapus data lama agar tidak menumpuk duplikat
            manual_data = [
                m for m in manual_data
                if _record_username(m).casefold() != str(username_pppoe or "").casefold()
            ]
            manual_data.append(manual_entry)

            # Simpan secara permanen ke dalam database server lokal
            _atomic_write_json(MANUAL_FILE, manual_data)
            merge_pppoe_automatically(DATA_DIR)
            print(f"--- [MANUAL SECURE SUCCESS] Koordinat & ID {username_pppoe} berhasil disimpan ---")
        except Exception as e:
            print(f"[MANUAL ERROR] Gagal mengunci koordinat manual: {e}")
        
        add_notification(
            f"User PPPoE diperbarui mohon tunggu 5 menit untuk perubahan di mikrotik: {username_pppoe}", 
            "info", 
            pppoe_user.get('id', 'N/A'), 
            username_pppoe
        )   
        
        flash(f"User {username_pppoe} berhasil diperbarui! Perubahan akan disinkronkan ke MikroTik dalam waktu 5 menit!", "success")
        return redirect(url_for('admin')) 
        
    return render_template('form.html', pppoe=pppoe_user)
        
@app.route('/api/clear-queue', methods=['GET'])
def queue_done():
    if not _api_authorized():
        return _unauthorized()

    # 1. Ambil ID antrean yang dikirim oleh MikroTik via parameter URL
    queue_id = request.args.get('id')
    
    if not queue_id:
        return jsonify({"status": "error", "message": "ID tidak ditemukan"}), 400
    
    try:
        # 2. Baca file antrean yang ada
        if not os.path.exists(QUEUE_FILE):
            return jsonify({"status": "error", "message": "File antrean tidak ada"}), 404
            
        with open(QUEUE_FILE, 'r') as f:
            queue_data = json.load(f)
        
        updated_queue = [
            q for q in queue_data
            if str(q.get("queue_id") or q.get("id")) != str(queue_id)
        ]
        
        _atomic_write_json(QUEUE_FILE, updated_queue)
            
        print(f"[SUCCESS] Antrean #{queue_id} berhasil dihapus dari daftar.")
        return jsonify({"status": "success", "message": "Antrean dihapus"}), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/get-queue', methods=['GET'])
def get_queue():
    if not _api_authorized():
        return _unauthorized()

    try:
        queue_data = _read_json(QUEUE_FILE, [])
        if not isinstance(queue_data, list):
            queue_data = []

        requested_router = request.args.get("router", "").strip().casefold()
        base_url = PUBLIC_BASE_URL or request.url_root.rstrip("/")
        rsc_commands = []

        for item in queue_data:
            item_router = str(item.get("router") or "").strip()
            if requested_router and item_router.casefold() != requested_router:
                continue

            username = str(item.get('username_pppoe') or '').strip()
            password = str(item.get('password') or '')
            profile = str(item.get('profile') or '')
            remote_ip = str(item.get('ip') or '-').strip()
            queue_id = str(item.get('queue_id') or item.get('id') or '').strip()

            if not username:
                continue

            set_parts = []
            if password:
                set_parts.append(f'password="{password}"')
            if profile:
                set_parts.append(f'profile="{profile}"')
            if remote_ip and remote_ip != "-":
                set_parts.append(f'remote-address="{remote_ip}"')

            if set_parts:
                rsc_commands.append(
                    f'/ppp secret set [find name="{username}"] ' + " ".join(set_parts) + ';'
                )
                rsc_commands.append(':delay 1s;')

            if queue_id:
                query = {"id": queue_id}
                if MIKROTIK_API_TOKEN:
                    query["token"] = MIKROTIK_API_TOKEN
                clear_url = f"{base_url}/api/clear-queue?{urlencode(query)}"
                rsc_commands.append(f'/tool fetch mode=http url="{clear_url}" keep-result=no;')

        return "\n".join(rsc_commands), 200, {'Content-Type': 'text/plain; charset=utf-8'}

    except Exception as e:
        print(f"[GET QUEUE ERROR] Gagal menyusun script rsc: {e}")
        return f"# Error: {str(e)}", 500, {'Content-Type': 'text/plain; charset=utf-8'}


def simpan_ke_file(data_baru, target_file):
    """Fungsi dinamis untuk membaca file lama, menambah data baru, lalu menyimpannya kembali"""
    folder_target = os.path.dirname(target_file)
    if folder_target and not os.path.exists(folder_target):
        os.makedirs(folder_target, exist_ok=True)
        
    riwayat = []
    
    if os.path.exists(target_file):
        try:
            with open(target_file, "r") as file:
                riwayat = json.load(file)
                if not isinstance(riwayat, list):
                    riwayat = []
        except Exception:
            riwayat = []
            
    riwayat.append(data_baru)
    
    if len(riwayat) > 500:
        riwayat.pop(0)
        
    with open(target_file, "w") as file:
        json.dump(riwayat, file, indent=4)
        
    return riwayat

def baca_dari_file(target_file):
    """PERBAIKAN: Fungsi dinamis untuk membaca data dari file JSON apa saja"""
    if os.path.exists(target_file):
        try:
            with open(target_file, "r") as file:
                return json.load(file)
        except Exception:
            return []
    return []


def init_history_buffer():
    """Fungsi pembantu untuk memuat data dari file ke array HANYA 1x saat server nyala."""
    global global_history_buffer
    if global_history_buffer is None:
        try:
            with open(HISTORY_FILE, 'r') as file:
                global_history_buffer = json.load(file)
                if not isinstance(global_history_buffer, list):
                    global_history_buffer = []
        except (FileNotFoundError, json.JSONDecodeError):
            global_history_buffer = []

@app.route('/api/update-pppoe', methods=['POST'])
def update_pppoe():
    global global_history_buffer
    if not _api_authorized():
        return _unauthorized()

    DATA_LOCK.acquire()
    try:
        data_terurai = None
        try:
            if request.is_json:
                data_terurai = request.get_json(silent=True)
            if not data_terurai:
                raw_body = request.get_data(as_text=True).strip()
                if raw_body.startswith('{'):
                    data_terurai = json.loads(raw_body)
                elif request.form and 'data_json' in request.form:
                    data_terurai = json.loads(request.form.get('data_json'))
        except Exception as e:
            print(f"[ERROR] Gagal membaca stream HTTP: {e}")

        if not data_terurai:
            return jsonify({"status": "failed", "message": "Format JSON rusak/kosong."}), 400
            
        raw_user_list = data_terurai.get("pppoe", [])
        if not isinstance(raw_user_list, list):
            return jsonify({"status": "failed", "message": "Field pppoe harus berupa array."}), 400
        waktu_sekarang = datetime.now().isoformat()
        
        filtered_user = []
        
        for user in raw_user_list:
            if not isinstance(user, dict):
                continue
            username = str(user.get('user') or '').strip().casefold()
            if username and not (BLACKLIST_PREFIXES and username.startswith(BLACKLIST_PREFIXES)):
                filtered_user.append(user)
        # Ekstraksi Nama Router
        nama_router = "Unknown_Router"
        if data_terurai.get("router"):
            nama_router = str(data_terurai.get("router")).strip()
        elif raw_user_list and isinstance(raw_user_list, list):
            nama_router = str(raw_user_list[0].get("router", "Unknown_Router")).strip()
            
        total_on = len([u for u in filtered_user if _normalize_status(u.get('status')) == "ONLINE"])

        # Total router dihitung dari gabungan username hasil impor dan kiriman
        # MikroTik. Dengan begitu, script yang hanya mengirim sesi aktif tetap
        # menghasilkan jumlah offline dan total pelanggan yang benar.
        incoming_usernames = {
            _record_username(user).casefold()
            for user in filtered_user
            if _record_username(user)
        }
        router_key = nama_router.casefold()
        manual_rows = _read_json(MANUAL_FILE, [])
        if not isinstance(manual_rows, list):
            manual_rows = []
        known_router_usernames = {
            _record_username(row).casefold()
            for row in manual_rows
            if isinstance(row, dict)
            and str(row.get("router") or "").strip().casefold() == router_key
            and _record_username(row)
        }
        total_global = len(incoming_usernames | known_router_usernames)

        # =================================================================
        # PERSIAPAN: Map Ulang List User
        # =================================================================
        mapped_user_list = []
        for user in filtered_user:
            mapped_user_list.append({
                "router": nama_router,
                "user": user.get("user", "-"),
                "ip": user.get("ip", "-"),
                "mac": user.get("mac", "-"),
                "uptime": user.get("uptime", "-"),
                "status": _normalize_status(user.get("status"))
            })

        # =================================================================
        # 2. SHARED MEMORY STATE
        # =================================================================
        try:
            with open(USER_TEMP_FILE, 'r') as f:
                state_terkini = json.load(f)
        except:
            state_terkini = {}

        state_terkini[nama_router] = {
            "on": total_on,
            "total": total_global,
            "last_seen": waktu_sekarang
        }
        stale_before = datetime.now() - timedelta(minutes=ROUTER_STALE_MINUTES)
        for router_name in list(state_terkini):
            try:
                last_seen = datetime.fromisoformat(state_terkini[router_name].get("last_seen", ""))
                if last_seen < stale_before:
                    del state_terkini[router_name]
            except (TypeError, ValueError):
                del state_terkini[router_name]

        _atomic_write_json(USER_TEMP_FILE, state_terkini)

        sum_on = sum(int(r.get("on", 0)) for r in state_terkini.values())
        sum_total = sum(int(r.get("total", 0)) for r in state_terkini.values())
        sum_off = max(0, sum_total - sum_on)

        # =================================================================
        # 3. LAYER HISTORY_FILE
        # =================================================================
        init_history_buffer()
        melebur = False
        if global_history_buffer:
            last_entry = global_history_buffer[-1]
            try:
                last_ts = datetime.fromisoformat(last_entry.get("timestamp"))
                now_ts = datetime.fromisoformat(waktu_sekarang)
                if abs((now_ts - last_ts).total_seconds()) < 300:
                    melebur = True
                    last_entry.update({"pppoe_users": sum_on, "pppoe_offlines": sum_off, "total_pppoe": sum_total, "timestamp": waktu_sekarang})
            except: pass

        if not melebur:
            global_history_buffer.append({"pppoe_users": sum_on, "pppoe_offlines": sum_off, "total_pppoe": sum_total, "timestamp": waktu_sekarang})

        global_history_buffer = global_history_buffer[-10000:]
        _atomic_write_json(HISTORY_FILE, global_history_buffer)

        # =================================================================
        # 4. LAYER USER_LOG_FILE
        # =================================================================
        try:
            with open(USER_LOG_FILE, 'r') as file:
                user_log_data = json.load(file)
                if not isinstance(user_log_data, list): user_log_data = []
        except: user_log_data = []

        user_log_data.append({"timestamp": waktu_sekarang, "router": nama_router, "pppoe": mapped_user_list})
        user_log_data = user_log_data[-1000:]
        _atomic_write_json(USER_LOG_FILE, user_log_data)

        # =================================================================
        # 5. LAYER USER_PPPOES_FILE (Penyimpanan Master Semua User)
        # =================================================================
        try:
            if os.path.exists(PPPOES_TEMP_FILE):
                with open(PPPOES_TEMP_FILE, 'r') as file:
                    existing_data = json.load(file)
                    master_list = existing_data if isinstance(existing_data, list) else []
            else:
                master_list = []
        except Exception as e:
            print(f"[WARN] Gagal membaca data lama PPPOES_TEMP_FILE: {e}")
            master_list = []

        master_list = [
            u for u in master_list
            if str(u.get("router") or "").strip().casefold() != router_key
        ]
        master_list.extend(mapped_user_list)
            
        _atomic_write_json(PPPOES_TEMP_FILE, master_list)

        try:
            merge_pppoe_automatically(DATA_DIR)
            print(f"[AUTO MERGE BERHASIL] Berhasil menjalankan merge otomatis!")
        except Exception as e:
            print(f"[AUTO MERGE ERROR] Gagal menjalankan merge otomatis: {e}")
            
        print(f"[SUCCESS] {nama_router} Masuk. Total Akumulasi Semua Router: {sum_on} User")
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        print(f"[ERROR] API Crash: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        DATA_LOCK.release()

@app.route('/api/history', methods=['GET'])
def get_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as file:
                return jsonify(json.load(file)), 200
        except Exception:
            return jsonify([]), 500
    return jsonify([]), 200

@app.route('/api/pppoes', methods=['GET'])
def get_user_actives():
    if os.path.exists(USER_PPPOES_FILE):
        try:
            with open(USER_PPPOES_FILE, "r") as file:
                return jsonify(json.load(file)), 200
        except Exception:
            return jsonify([]), 500
    
    return jsonify([]), 200
    
@app.route('/api/analytics-data', methods=['GET'])
def get_analytics_data():
    """Membaca history.json untuk grafik, dan user_log.json untuk tabel raw log."""
    month_filter = request.args.get('month', '').strip()
    
    try:
        with open(HISTORY_FILE, 'r') as f:
            history_data = json.load(f)
            if not isinstance(history_data, list):
                history_data = []
    except (FileNotFoundError, json.JSONDecodeError):
        history_data = []

    try:
        with open(USER_LOG_FILE, 'r') as f:
            user_log_data = json.load(f)
            if not isinstance(user_log_data, list):
                user_log_data = []
    except (FileNotFoundError, json.JSONDecodeError):
        user_log_data = []

    daily_data = {}
    monthly_data = {}
    pppoe_counts = []

    # 1. Ekstraksi Data dari Log Historis (Amankan parsing Timestamp)
    for entry in history_data:
        if not isinstance(entry, dict):
            continue
            
        try:
            ts_string = entry.get('timestamp', '').strip()
            if not ts_string:
                continue
                
            if "T" in ts_string:
                ts_string = ts_string.replace("T", " ")
            
            if "." in ts_string:
                dt = datetime.strptime(ts_string.split(".")[0], '%Y-%m-%d %H:%M:%S')
            else:
                dt = datetime.strptime(ts_string, '%Y-%m-%d %H:%M:%S')
                
        except Exception as e:
            print(f"[DEBUG LOG] Gagal parsing waktu pada baris data: {e}")
            continue

        day = dt.strftime('%Y-%m-%d')
        month = dt.strftime('%Y-%m')

        p_count = int(entry.get('pppoe_users', 0))
        pppoe_counts.append(p_count)

        daily_data.setdefault(day, []).append(p_count)
        monthly_data.setdefault(month, []).append(p_count)

    # 2. Pembuatan Ringkasan Harian
    daily_summary_all = []
    for day, counts_list in daily_data.items():
        if counts_list:
            daily_summary_all.append({
                "date": day,
                "pppoe_peak": max(counts_list),
                "pppoe_avg": round(sum(counts_list) / len(counts_list)),
                "peak": max(counts_list),
                "trough": min(counts_list),
                "average": round(sum(counts_list) / len(counts_list))
            })
    daily_summary_all.sort(key=lambda x: x['date'])

    # 3. Pembuatan Ringkasan Bulanan
    monthly_summary = []
    for month, counts_list in monthly_data.items():
        if counts_list:
            monthly_summary.append({
                "month": month,
                "pppoe_peak": max(counts_list),
                "pppoe_avg": round(sum(counts_list) / len(counts_list)),
                "peak": max(counts_list),
                "trough": min(counts_list),
                "average": round(sum(counts_list) / len(counts_list))
            })

    current_year = datetime.now().year
    existing_months = {m['month'] for m in monthly_summary}
    for m in range(1, 13):
        key = f"{current_year}-{m:02d}"
        if key not in existing_months:
            monthly_summary.append({
                "month": key, "pppoe_peak": 0, "pppoe_avg": 0,
                "peak": 0, "trough": 0, "average": 0
            })
    monthly_summary.sort(key=lambda x: x['month'])

    # 4. Filter Berdasarkan Kalender Bulanan
    normalized_filter = ""
    no_data_for_month = False
    if month_filter:
        if len(month_filter) == 2 and month_filter.isdigit():
            normalized_filter = f"{current_year}-{month_filter}"
        elif len(month_filter) == 7 and '-' in month_filter:
            normalized_filter = month_filter
        else:
            return jsonify({"error": "Format filter bulan tidak valid."}), 400
    
    daily_summary = daily_summary_all
    if normalized_filter:
        daily_summary = [rec for rec in daily_summary_all if rec['date'].startswith(normalized_filter)]
        try:
            y, m = normalized_filter.split('-')
            y, m = int(y), int(m)
            # Pastikan "import calendar" ada di baris paling atas app.py
            _, days_in_month = calendar.monthrange(y, m)
            existing_map = {rec['date']: rec for rec in daily_summary}
            completed = []
            for d in range(1, days_in_month + 1):
                date_str = f"{y}-{m:02d}-{d:02d}"
                if date_str in existing_map:
                    completed.append(existing_map[date_str])
                else:
                    completed.append({
                        "date": date_str, "pppoe_peak": 0, "pppoe_avg": 0,
                        "peak": 0, "trough": 0, "average": 0
                    })
            completed.sort(key=lambda x: x['date'])
            daily_summary = completed
            no_data_for_month = False
        except Exception:
            no_data_for_month = len(daily_summary) == 0

    # 5. Ambil jumlah realtime dari data pelanggan terkini (pppoes.json).
    # History tetap digunakan untuk grafik, puncak, terendah, dan rata-rata.
    current_pppoes = _read_json(USER_PPPOES_FILE, [])
    if isinstance(current_pppoes, dict):
        current_pppoes = (
            current_pppoes.get('pppoe')
            or current_pppoes.get('pppoes')
            or current_pppoes.get('data')
            or []
        )
    if not isinstance(current_pppoes, list):
        current_pppoes = []

    valid_current_pppoes = [
        row for row in current_pppoes
        if isinstance(row, dict) and _record_username(row)
    ]
    current_online = sum(
        1 for row in valid_current_pppoes
        if _normalize_status(row.get('status')) == 'ONLINE'
    )
    current_total = len(valid_current_pppoes)
    current_offline = max(0, current_total - current_online)

    latest_timestamp = None
    if history_data and isinstance(history_data[-1], dict):
        latest_timestamp = history_data[-1].get('timestamp')
    if not latest_timestamp:
        latest_timestamp = datetime.now().isoformat()

    realtime = {
        'timestamp': latest_timestamp,
        'online': current_online,
        'offline': current_offline,
        'total': current_total
    }

    # Ambil 100 baris terakhir saja dari arsip agar browser tidak lag
    active_users_list = user_log_data[-100:] if user_log_data else []

    analytics_payload = {
        "summary": {
            "peak_users": max(pppoe_counts) if pppoe_counts else 0,
            "trough_users": min(pppoe_counts) if pppoe_counts else 0,
            "average_users": round(sum(pppoe_counts) / len(pppoe_counts), 2) if pppoe_counts else 0,
            "unique_devices": realtime['online']  
        },
        "daily_summary": daily_summary,
        "monthly_summary": monthly_summary,
        "realtime": realtime,
        "raw_logs": active_users_list,
        "month_filter": normalized_filter,
        "no_data_for_month": no_data_for_month
    }

    return jsonify(analytics_payload)

if __name__ == '__main__':
    debug_mode = os.environ.get("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes"}
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=debug_mode)