from __future__ import annotations

import io
import posixpath
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import py7zr
from py7zr.io import BytesIOFactory

from dataset_core.domain import ScanIssue, ScanResult
from dataset_core.errors import DatasetCoreError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
ANNOTATION_SUFFIXES = {".txt", ".json", ".xml"}
SUPPORTED_ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".txz",
    ".tar",
    ".zip",
    ".7z",
)


@dataclass(frozen=True, slots=True)
class ZipScanPolicy:
    """Safety limits applied consistently to every supported archive format."""

    max_files: int = 100_000
    max_uncompressed_bytes: int = 20 * 1024 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_annotation_bytes: int = 100 * 1024 * 1024
    max_directory_depth: int = 32
    max_file_name_chars: int = 255
    max_json_depth: int = 100


ArchiveScanPolicy = ZipScanPolicy
WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    name: str
    size: int
    compressed_size: int | None
    is_directory: bool
    is_file: bool
    is_symlink: bool
    source: Any


class ArchiveReader:
    """Format-neutral, read-only archive access for validated archive members."""

    def entries(self) -> list[ArchiveEntry]:
        raise NotImplementedError

    def read(self, entry: ArchiveEntry) -> bytes:
        raise NotImplementedError

    def prepare_for_reading(self, destination: str | Path, entries: dict[str, ArchiveEntry]) -> None:
        """Optionally materialize validated members for efficient repeated reads."""

    def materialization_bytes(self, entries: dict[str, ArchiveEntry]) -> int:
        """Return temporary-disk capacity required by prepare_for_reading()."""
        return 0

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> ArchiveReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _ZipArchive(ArchiveReader):
    def __init__(self, source: str | Path | BinaryIO) -> None:
        self._archive = zipfile.ZipFile(source)

    def entries(self) -> list[ArchiveEntry]:
        return [
            ArchiveEntry(
                name=entry.filename,
                size=entry.file_size,
                compressed_size=entry.compress_size,
                is_directory=entry.is_dir(),
                is_file=not entry.is_dir(),
                is_symlink=False,
                source=entry,
            )
            for entry in self._archive.infolist()
        ]

    def read(self, entry: ArchiveEntry) -> bytes:
        return self._archive.read(entry.source)

    def close(self) -> None:
        self._archive.close()


class _TarArchive(ArchiveReader):
    def __init__(self, source: str | Path | BinaryIO) -> None:
        if isinstance(source, (str, Path)):
            self._archive = tarfile.open(source, mode="r:*")
        else:
            self._archive = tarfile.open(fileobj=source, mode="r:*")

    def entries(self) -> list[ArchiveEntry]:
        return [
            ArchiveEntry(
                name=entry.name,
                size=entry.size,
                compressed_size=None,
                is_directory=entry.isdir(),
                is_file=entry.isfile(),
                is_symlink=entry.issym() or entry.islnk(),
                source=entry,
            )
            for entry in self._archive.getmembers()
        ]

    def read(self, entry: ArchiveEntry) -> bytes:
        stream = self._archive.extractfile(entry.source)
        if stream is None:
            raise DatasetCoreError("import.invalid_archive_entry", params={"path": entry.name})
        with stream:
            return stream.read()

    def close(self) -> None:
        self._archive.close()


class _SevenZipArchive(ArchiveReader):
    def __init__(self, source: str | Path | BinaryIO) -> None:
        self._archive = py7zr.SevenZipFile(source, mode="r")
        self._extracted_root: Path | None = None
        if self._archive.needs_password():
            self._archive.close()
            raise DatasetCoreError("import.password_protected_archive")

    def entries(self) -> list[ArchiveEntry]:
        return [
            ArchiveEntry(
                name=entry.filename,
                size=entry.uncompressed or 0,
                compressed_size=entry.compressed,
                is_directory=entry.is_directory,
                is_file=entry.is_file,
                is_symlink=entry.is_symlink,
                source=entry.filename,
            )
            for entry in self._archive.list()
        ]

    def prepare_for_reading(self, destination: str | Path, entries: dict[str, ArchiveEntry]) -> None:
        """Extract validated 7z members once into Worker-owned temporary storage.

        py7zr has no random-access member reader. Repeated ``extract(targets=…)``
        calls re-decompress solid archives from the beginning, which made a large
        dataset appear permanently pending. The caller supplies a TemporaryDirectory
        and the path is never persisted in the database.
        """
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        self._archive.reset()
        self._archive.extract(path=root, targets=[str(entry.source) for entry in entries.values()])
        self._extracted_root = root

    def materialization_bytes(self, entries: dict[str, ArchiveEntry]) -> int:
        return sum(entry.size for entry in entries.values())

    def read(self, entry: ArchiveEntry) -> bytes:
        if self._extracted_root is not None:
            path = self._extracted_root / PurePosixPath(safe_relative_path(entry.name))
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise DatasetCoreError("import.invalid_archive_entry", params={"path": entry.name}) from exc
        else:
            # This fallback is retained for callers that only need one member.
            # Import tasks call prepare_for_reading() before parsing/materialization.
            self._archive.reset()
            factory = BytesIOFactory(max(entry.size, 1))
            self._archive.extract(targets=[str(entry.source)], factory=factory)
            try:
                output = factory.get(entry.name)
            except KeyError as exc:
                raise DatasetCoreError("import.invalid_archive_entry", params={"path": entry.name}) from exc
            output.seek(0)
            content = output.read()
        if len(content) != entry.size:
            raise DatasetCoreError("import.invalid_archive_entry", params={"path": entry.name})
        return content

    def close(self) -> None:
        self._archive.close()


def is_supported_archive_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in SUPPORTED_ARCHIVE_SUFFIXES)


def archive_suffix(name: str) -> str:
    lowered = PurePosixPath(name.replace("\\", "/")).name.lower()
    for suffix in SUPPORTED_ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    raise DatasetCoreError("upload.archive_required")


def safe_relative_path(name: str) -> str:
    normalized = posixpath.normpath(name.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if normalized in {".", ""} or path.is_absolute() or ".." in path.parts:
        raise DatasetCoreError("import.unsafe_archive_path", params={"path": name})
    return path.as_posix()


def match_key(path: PurePosixPath) -> str:
    """Match conventional images/ and labels/ trees without host paths."""
    parts = list(path.with_suffix("").parts)
    for index, part in enumerate(parts):
        if part.lower() in {"images", "labels", "annotations"}:
            parts[index] = "__media__"
    return PurePosixPath(*parts).as_posix().lower()


def subset_for(path: PurePosixPath) -> str | None:
    parts = {part.lower() for part in path.parts}
    for subset in ("train", "val", "test"):
        if subset in parts:
            return subset
    return None


def detect_parser(names: list[str]) -> str | None:
    lowered = {PurePosixPath(name).name.lower() for name in names}
    if "data.yaml" in lowered or "data.yml" in lowered:
        return "yolo"
    if "annotations.json" in lowered or "instances.json" in lowered:
        return "coco"
    if any(name.lower().endswith(".xml") for name in names):
        return "voc"
    if any(name.lower().endswith(".json") for name in names):
        return "labelme"
    if any(name.lower().endswith(".txt") for name in names):
        return "yolo"
    return None


def validated_entries(archive: ArchiveReader, policy: ZipScanPolicy) -> dict[str, ArchiveEntry]:
    entries = [entry for entry in archive.entries() if not entry.is_directory]
    if len(entries) > policy.max_files:
        raise DatasetCoreError("import.too_many_files", params={"max_files": policy.max_files})
    total_size = sum(entry.size for entry in entries)
    if total_size > policy.max_uncompressed_bytes:
        raise DatasetCoreError("import.archive_too_large", params={"max_bytes": policy.max_uncompressed_bytes})

    normalized: dict[str, ArchiveEntry] = {}
    for entry in entries:
        if entry.is_symlink or not entry.is_file:
            raise DatasetCoreError("import.unsafe_archive_entry", params={"path": entry.name})
        if entry.compressed_size and entry.size / entry.compressed_size > policy.max_compression_ratio:
            raise DatasetCoreError("import.suspicious_compression_ratio", params={"path": entry.name})
        name = safe_relative_path(entry.name)
        path = PurePosixPath(name)
        if len(path.parts) > policy.max_directory_depth:
            raise DatasetCoreError("import.path_too_deep", params={"path": name, "max_depth": policy.max_directory_depth})
        if any(len(part) > policy.max_file_name_chars for part in path.parts):
            raise DatasetCoreError("import.file_name_too_long", params={"path": name, "max_chars": policy.max_file_name_chars})
        if any(":" in part or part.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES for part in path.parts):
            raise DatasetCoreError("import.unsafe_archive_path", params={"path": entry.name})
        if name in normalized:
            raise DatasetCoreError("import.duplicate_archive_path", params={"path": name})
        normalized[name] = entry
    return normalized


def _rewind(source: str | Path | BinaryIO) -> None:
    if not isinstance(source, (str, Path)):
        source.seek(0)


def open_archive(content: bytes | BinaryIO | str | Path) -> ArchiveReader:
    """Open a supported archive by content signature, never by extension alone."""
    source: str | Path | BinaryIO = io.BytesIO(content) if isinstance(content, bytes) else content
    try:
        if zipfile.is_zipfile(source):
            _rewind(source)
            return _ZipArchive(source)
        _rewind(source)
        if py7zr.is_7zfile(source):
            _rewind(source)
            return _SevenZipArchive(source)
        _rewind(source)
        try:
            return _TarArchive(source)
        except (tarfile.ReadError, tarfile.CompressionError, EOFError):
            _rewind(source)
    except (OSError, ValueError, py7zr.exceptions.Bad7zFile) as exc:
        raise DatasetCoreError("import.invalid_archive") from exc
    raise DatasetCoreError("import.unsupported_archive")


def scan_dataset_archive(content: bytes | BinaryIO | str | Path, *, policy: ZipScanPolicy | None = None) -> ScanResult:
    """Inspect ZIP, 7z and TAR-family datasets safely without extracting paths."""
    policy = policy or ZipScanPolicy()
    with open_archive(content) as archive:
        entries = validated_entries(archive, policy)
        images: dict[str, str] = {}
        annotations: dict[str, str] = {}
        issues: list[ScanIssue] = []
        subsets: Counter[str] = Counter()
        for name in entries:
            path = PurePosixPath(name)
            suffix = path.suffix.lower()
            key = match_key(path)
            subset = subset_for(path)
            if suffix in IMAGE_SUFFIXES:
                if key in images:
                    issues.append(ScanIssue("import.duplicate_image_stem", "warning", name))
                images[key] = name
                if subset:
                    subsets[subset] += 1
            elif suffix in ANNOTATION_SUFFIXES:
                annotations[key] = name

        pairs: list[tuple[str, str | None]] = []
        for key, image in sorted(images.items()):
            annotation = annotations.get(key)
            pairs.append((image, annotation))
            if annotation is None:
                issues.append(ScanIssue("import.missing_annotation", "warning", image))
        for key, annotation in sorted(annotations.items()):
            if key not in images:
                issues.append(ScanIssue("import.orphan_annotation", "warning", annotation))
        return ScanResult(detect_parser(list(entries)), len(images), len(annotations), tuple(pairs), tuple(issues), dict(subsets))


def scan_dataset_zip(content: bytes | BinaryIO | str | Path, *, policy: ZipScanPolicy | None = None) -> ScanResult:
    """Backward-compatible alias; now accepts every supported archive format."""
    return scan_dataset_archive(content, policy=policy)