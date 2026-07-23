# PPPoE Monitoring – Versi Kosong dan Siap Daerah Lain

Aplikasi Flask untuk menerima data PPPoE dari MikroTik, menampilkan dashboard, daftar pelanggan, analitik, notifikasi, dan peta pelanggan.

Versi ini sudah dibuat **netral terhadap nama daerah**. Tidak ada data  daftar router yang ditulis tetap di kode. Data awal sengaja kosong dan dapat diisi melalui tombol **Impor Data** atau endpoint MikroTik.

## Yang sudah disesuaikan

- Data pelanggan, riwayat, notifikasi, dan antrean dimulai dari kosong.
- Pencocokan data pelanggan menggunakan **username PPPoE yang sama persis**, bukan kemiripan nama.
- Filter wilayah dibuat otomatis dari awalan username, misalnya `kediri-budi` menghasilkan filter `Kediri`.
- Filter router dibuat otomatis berdasarkan data yang masuk.
- Nama router tidak dibatasi pada router tertentu.
- Profile MikroTik tidak dibatasi pada paket tertentu.
- Peta memakai titik awal Indonesia dan otomatis menyesuaikan jika koordinat tersedia.
- Impor JSON/CSV dapat mengganti data daerah lama agar data tidak tercampur.
- URL penghapusan antrean MikroTik mengikuti alamat server saat ini atau `PUBLIC_BASE_URL`.
- API MikroTik dapat diberi token melalui environment variable.

## Menjalankan aplikasi

```bash
python -m venv venv
```

Windows:

```bash
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Linux/macOS:

```bash
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Buka:

- Dashboard: `http://localhost:5000/dashboard`
- Daftar pelanggan dan impor: `http://localhost:5000/admin`
- Peta: `http://localhost:5000/`
- Analitik: `http://localhost:5000/analytics`

## Impor data daerah

Pada halaman **Daftar Pelanggan**, tekan **Impor Data**, lalu pilih file `.csv` atau `.json`. Impor melalui tombol memakai mode `merge`, sehingga daerah lama tetap tersimpan. Username yang sama akan diperbarui, sedangkan username baru akan ditambahkan.


Kolom yang dikenali:

- `user` / `username_pppoe` / `username` — wajib
- `id` / `id_pelanggan`
- `nama` / `nama_asli`
- `lokasi` / `alamat`
- `latitude`
- `longitude`
- `router`
- `ip`
- `mac`
- `uptime`
- `status`
- `profile`
- `password`

Contoh tersedia pada:

- `examples/data_import.csv`
- `examples/data_import.json`

Data tanpa status akan dianggap `OFFLINE`. Data tanpa koordinat tetap dapat tampil di tabel, tetapi tidak dibuatkan marker pada peta.

### Impor melalui API

Ganti seluruh data:

```bash
curl -X POST -F "file=@examples/data_import.csv" \
  "http://localhost:5000/api/import-customers?mode=replace"
```

Gabungkan dengan data yang sudah ada:

```bash
curl -X POST -F "file=@examples/data_import.csv" \
  "http://localhost:5000/api/import-customers?mode=merge"
```

## Data dari MikroTik

Endpoint penerimaan data:

```text
POST /api/update-pppoe
```

Format JSON:

```json
{
  "router": "ROUTER DAERAH A",
  "pppoe": [
    {
      "user": "daeraha-budi",
      "ip": "10.10.0.2",
      "mac": "AA:BB:CC:DD:EE:01",
      "uptime": "1d2h",
      "status": "ONLINE"
    }
  ]
}
```

Contoh lengkap tersedia di `examples/mikrotik_payload.json`.

Saat router yang sama mengirim data lagi, data realtime router tersebut akan diperbarui. Metadata hasil impor seperti ID, alamat, dan koordinat tetap dipertahankan berdasarkan username PPPoE.

Username PPPoE harus unik di seluruh sistem. Nama `router` pada file impor dan payload MikroTik juga harus sama, tanpa perbedaan penulisan, agar data masuk ke kelompok router yang benar.

## Antrean perubahan MikroTik

MikroTik dapat mengambil antrean dengan:

```text
GET /api/get-queue?router=NAMA_ROUTER
```

Parameter `router` penting agar satu router hanya mengambil perintah miliknya. Setelah perintah selesai, script yang dihasilkan akan memanggil `/api/clear-queue` secara otomatis.

## Konfigurasi environment

Salin `.env.example` sebagai acuan. Aplikasi membaca environment variable langsung dari sistem.

- `APP_NAME`: nama aplikasi.
- `SECRET_KEY`: secret Flask.
- `PUBLIC_BASE_URL`: alamat server yang dapat dijangkau MikroTik, contoh `http://192.168.1.10:5000`.
- `MIKROTIK_API_TOKEN`: token opsional untuk endpoint update/antrean.
- `MAP_DEFAULT_LAT`, `MAP_DEFAULT_LNG`, `MAP_DEFAULT_ZOOM`: titik awal peta.
- `ROUTER_STALE_MINUTES`: router tidak dihitung lagi jika tidak mengirim data selama batas ini.
- `PPPOE_BLACKLIST_PREFIXES`: awalan username yang ingin diabaikan, dipisahkan koma. Default kosong.
- `PORT`: port aplikasi, default `5000`.
- `FLASK_DEBUG`: gunakan `1` hanya saat pengembangan.

Jika `MIKROTIK_API_TOKEN` diisi, kirim token dengan header:

```text
X-API-Token: TOKEN_ANDA
```

atau parameter `?token=TOKEN_ANDA` untuk kebutuhan RouterOS.

## Pengujian

```bash
python -m unittest discover -s tests -v
```

Pengujian memeriksa kondisi data kosong, impor daerah baru, penerimaan data MikroTik, pencocokan username exact, filter dinamis, dan antrean per router.
