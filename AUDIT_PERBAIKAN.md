# Audit dan Perbaikan Kritis

Perbaikan dilakukan tanpa mengubah tampilan utama aplikasi:

1. Tombol impor pada dashboard sekarang selalu memakai mode **merge** agar daerah lama tidak terhapus.
2. Backend tetap menyediakan mode `replace` hanya untuk penggantian data yang disengaja melalui API.
3. Data IP dari MikroTik sekarang lebih diprioritaskan daripada nilai `-` dari file impor.
4. Jumlah total dan offline per router tetap benar walaupun script MikroTik hanya mengirim sesi yang sedang aktif.
5. Pencocokan nama router saat pembaruan dibuat tidak peka huruf besar/kecil.
6. Kartu statistik dashboard membaca data pelanggan terkini, bukan hanya history.
7. Realtime chart dashboard diperbaiki agar menambah data ke dataset yang benar.
8. Pelacakan perubahan status marker memakai username, sehingga pelanggan yang ID-nya `-` tidak saling tertukar.
9. Link IP pada popup peta tidak lagi membuat tautan `http://-`.
10. Analitik realtime membaca `pppoes.json`; history tetap digunakan untuk grafik puncak, rata-rata, dan log.

## Syarat integrasi MikroTik

- Endpoint: `POST /api/update-pppoe`
- JSON harus memiliki `router` dan array `pppoe`.
- Username harus sama persis dengan data hasil impor.
- Nama router harus sama dengan kolom `router` pada CSV.
- Untuk pemasangan di VM, isi `PUBLIC_BASE_URL` dengan alamat server yang dapat dijangkau MikroTik.
- Sebaiknya isi `MIKROTIK_API_TOKEN` dan kirim token dari script MikroTik.
