"""Space 目录的打包与解包。

导出结构（zip 根目录）::

    space.yaml
    meta.db
    raw/<原始文件>
    vectors/<LanceDB 文件>

导入时做了 zip slip 防护：任何绝对路径或带 ``..`` 的条目都会被拒绝；
同时限制解包总量，避免一个几百 KB 的压缩包在磁盘上炸成几十 GB。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from agentmem.errors import ValidationError

SPACE_YAML_NAME = "space.yaml"
META_DB_NAME = "meta.db"
RAW_DIR_NAME = "raw"
VECTORS_DIR_NAME = "vectors"

#: 这些目录之外的顶层条目会被忽略
ALLOWED_TOP_LEVEL = {SPACE_YAML_NAME, META_DB_NAME, RAW_DIR_NAME, VECTORS_DIR_NAME}

#: 解包后的总字节上限。压缩比可以做到上千倍，几百 KB 的包能炸出几十 GB；
#: 一个本地知识库的归档没有理由超过这个量级。
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

#: 单个条目上限（向量库的 LanceDB 数据文件是大头，留足余量）
MAX_ENTRY_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def export_space_zip(space_dir: Path) -> bytes:
    """把 Space 目录打包成 zip 字节流。

    Args:
        space_dir: ``data/spaces/<space_id>/`` 目录。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(space_dir.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(space_dir)
            if relative.parts[0] not in ALLOWED_TOP_LEVEL:
                continue
            if _is_sqlite_sidecar(relative):
                continue
            archive.write(path, arcname=relative.as_posix())
    return buffer.getvalue()


def import_space_zip(data: bytes, target_dir: Path) -> Path:
    """把 zip 解包到目标目录。

    Args:
        data: zip 字节流。
        target_dir: 目标 Space 目录，必须尚不存在或为空。

    Returns:
        目标目录。
    """
    if not data:
        raise ValidationError("导入内容为空")
    if target_dir.exists() and any(target_dir.iterdir()):
        raise ValidationError("目标 Space 目录已存在且非空", detail={"target": str(target_dir)})
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _check_uncompressed_size(archive)
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValidationError("压缩包内存在非法路径", detail={"entry": name})
                if relative.parts[0] not in ALLOWED_TOP_LEVEL:
                    continue
                destination = target_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open("wb") as sink:
                    sink.write(source.read())
    except zipfile.BadZipFile as exc:
        raise ValidationError("不是有效的 zip 文件") from exc
    if not (target_dir / META_DB_NAME).is_file():
        raise ValidationError("压缩包缺少 meta.db")
    return target_dir


def _check_uncompressed_size(archive: zipfile.ZipFile) -> None:
    """检查解包后的体量，挡住 zip bomb。

    只算「会被真正写盘」的条目：不相关的条目本来就会被跳过，拿它们报错反而
    让一个干净的归档因为夹带了别的东西而导入失败。头里的 file_size 是压缩包
    自己写的，但读取时 Python 会校验 CRC，谎报的大小撑不到最后。
    """
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        relative = Path(info.filename)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if relative.parts[0] not in ALLOWED_TOP_LEVEL:
            continue
        if info.file_size > MAX_ENTRY_BYTES:
            raise ValidationError(
                "压缩包内单个文件过大", detail={"entry": info.filename, "bytes": info.file_size}
            )
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise ValidationError("压缩包解包后体积过大，已拒绝导入", detail={"bytes": total})


def _is_sqlite_sidecar(relative: Path) -> bool:
    """WAL / SHM 属于临时文件，导出前会做 checkpoint，无需打包。"""
    return relative.name.endswith(("-wal", "-shm"))
