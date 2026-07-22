# Perubahan Versi Final

- Menghapus seluruh data daerah lama dan file backup yang berisi data operasional.
- Menghapus ketergantungan pada file dan nama wilayah tertentu.
- Membuat impor JSON/CSV melalui halaman admin.
- Membuat filter wilayah serta router mengikuti data secara otomatis.
- Mengubah pencocokan pelanggan menjadi exact berdasarkan username PPPoE.
- Menjadikan nama router, profile, titik awal peta, URL server, token API, dan blacklist sebagai konfigurasi dinamis.
- Memperbaiki kondisi dashboard tanpa data agar tetap menampilkan nilai 0.
- Memperbaiki antrean agar dapat dipisahkan per router dan memakai ID antrean unik.
- Menambahkan contoh data, dokumentasi penggunaan, dan pengujian otomatis.

## Audit lanjutan multi-daerah

- Mengubah tombol impor admin menjadi mode merge agar daerah lama tidak terhapus.
- Memperbaiki prioritas IP realtime dari MikroTik.
- Memperbaiki total/offline saat payload hanya berisi sesi aktif.
- Memperbaiki kartu statistik dan realtime chart dashboard.
- Memperbaiki pelacakan status marker untuk pelanggan tanpa ID.
- Menambahkan CSV dan JSON simulasi Tegalsruni pada folder examples.
