"""Menggabungkan data realtime MikroTik dengan data pelanggan hasil impor/edit.

Pencocokan dilakukan secara tepat berdasarkan username PPPoE. Modul ini sengaja
netral terhadap nama daerah agar proyek dapat dipakai kembali di wilayah lain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


def _read_list(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if isinstance(raw, dict):
        for key in ("pppoe", "customers", "data"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break

    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


def _username(row: dict[str, Any]) -> str:
    value = row.get("user") or row.get("username_pppoe") or row.get("username")
    return str(value or "").strip()


def _key(username: str) -> str:
    return username.strip().casefold()


def _number_or_dash(value: Any) -> float | str:
    if value in (None, "", "-"):
        return "-"
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return "-"


def _status(value: Any) -> str:
    normalized = str(value or "OFFLINE").strip().upper()
    return "ONLINE" if normalized in {"ONLINE", "ON", "ACTIVE", "UP", "TRUE", "1"} else "OFFLINE"


def _coordinate(latitude: float | str, longitude: float | str) -> str:
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return f"{latitude},{longitude}"
    return "-"


def _first_meaningful(*values: Any, default: Any = "-") -> Any:
    """Ambil nilai pertama yang bukan kosong dan bukan tanda strip."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip() in {"", "-"}:
            continue
        return value
    return default


def _combine(live: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    username = _username(live) or _username(metadata)
    latitude = _number_or_dash(_first_meaningful(metadata.get("latitude"), live.get("latitude")))
    longitude = _number_or_dash(_first_meaningful(metadata.get("longitude"), live.get("longitude")))

    customer_id = _first_meaningful(
        metadata.get("id"),
        metadata.get("id_pelanggan"),
        live.get("id"),
        live.get("id_pelanggan"),
    )
    location = _first_meaningful(
        metadata.get("lokasi"), metadata.get("alamat"), live.get("lokasi")
    )
    real_name = _first_meaningful(
        metadata.get("nama_asli"), metadata.get("nama"), live.get("nama_asli"), default=""
    )

    return {
        "router": _first_meaningful(metadata.get("router"), live.get("router"), default="Belum ditentukan"),
        "icon": metadata.get("icon", live.get("icon", 119)),
        "id": customer_id,
        "id_pelanggan": customer_id,
        "user": username,
        "nama_asli": real_name,
        "lokasi": location,
        "latitude": latitude,
        "longitude": longitude,
        "coordinate": _coordinate(latitude, longitude),
        "mac": _first_meaningful(live.get("mac"), metadata.get("mac")),
        "ip": _first_meaningful(live.get("ip"), metadata.get("ip")),
        "uptime": _first_meaningful(live.get("uptime"), metadata.get("uptime")),
        "status": _status(live.get("status", metadata.get("status"))),
        "profile": _first_meaningful(metadata.get("profile"), live.get("profile"), default=""),
        "password": _first_meaningful(metadata.get("password"), live.get("password"), default=""),
    }


def process_merge(
    pppoe_list: Iterable[dict[str, Any]],
    manual_list: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Gabungkan data berdasarkan username exact, tanpa aturan nama daerah."""

    metadata_by_user: dict[str, dict[str, Any]] = {}
    for row in manual_list:
        username = _username(row)
        if username:
            metadata_by_user[_key(username)] = row

    # Data realtime terakhir menang jika ada username ganda.
    live_by_user: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in pppoe_list:
        username = _username(row)
        if not username:
            continue
        normalized = _key(username)
        if normalized not in live_by_user:
            order.append(normalized)
        live_by_user[normalized] = row

    result: list[dict[str, Any]] = []
    for normalized in order:
        result.append(_combine(live_by_user[normalized], metadata_by_user.get(normalized, {})))

    # Data hasil impor tetap dapat dilihat sebelum router mengirim data realtime.
    for normalized, metadata in metadata_by_user.items():
        if normalized not in live_by_user:
            result.append(_combine({}, metadata))

    result.sort(key=lambda row: (str(row.get("router", "")).casefold(), str(row.get("user", "")).casefold()))
    return result


def main(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    if data_dir is None:
        root = Path(os.environ.get("PPPOE_ROOT", Path(__file__).resolve().parent))
        data_path = root / "data"
    else:
        data_path = Path(data_dir)

    pppoe_list = _read_list(data_path / "pppoes_temp.json")
    manual_list = _read_list(data_path / "user_manual.json")
    processed = process_merge(pppoe_list, manual_list)
    _atomic_write(data_path / "pppoes.json", processed)
    return processed


if __name__ == "__main__":
    main()
