"""多硬盘卷管理辅助。

catalog.repository.Catalog 已负责卷的注册/解析/状态；本模块只做 macOS
环境下的卷身份识别：用 `diskutil info` 读取卷 UUID / 卷名 / 设备名，
为注册数据卷提供稳定逻辑 ID 的建议。识别失败时优雅降级（返回 None），
绝不因此阻塞注册——用户始终可以显式指定 volume_id。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VolumeIdentity:
    volume_uuid: str | None
    volume_name: str | None
    device: str | None
    file_system: str | None


def detect_volume_identity(mount_point: Path) -> VolumeIdentity | None:
    """探测挂载点的物理卷身份。非 macOS / diskutil 不可用时返回 None。"""
    try:
        out = subprocess.run(
            ["diskutil", "info", "-plist", str(mount_point)],
            capture_output=True, text=True, timeout=10, check=True).stdout
        import plistlib
        info = plistlib.loads(out.encode())
        return VolumeIdentity(
            volume_uuid=info.get("VolumeUUID"),
            volume_name=info.get("VolumeName"),
            device=info.get("DeviceIdentifier"),
            file_system=info.get("FilesystemType"),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def suggest_volume_id(mount_point: Path) -> tuple[str, str]:
    """返回 (建议 volume_id, 说明)。优先卷 UUID（跨挂载路径稳定），
    其次卷名；都不行则用路径摘要，并提示用户显式指定。"""
    ident = detect_volume_identity(mount_point)
    if ident and ident.volume_uuid:
        return f"vol-{ident.volume_uuid.lower()[:12]}", "基于 macOS 卷 UUID（稳定）"
    if ident and ident.volume_name:
        return f"vol-{ident.volume_name.lower().replace(' ', '-')}", "基于卷名（重命名后会变，建议改为显式 ID）"
    digest = json.dumps(str(mount_point.resolve()))
    return f"vol-{abs(hash(digest)) % 10**8:08d}", "基于路径摘要（不稳定，建议显式指定 volume_id）"
