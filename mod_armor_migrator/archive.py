"""
Standalone reader/writer for Helldivers 2 / Stingray LEGACY package format
(magic 0xF0000004), read-only DSAR (0x52415344) decompression, and Slim
`bundles.nxa` package reconstruction.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Optional, Tuple

from .constants import LEGACY_MAGIC, DSAR_MAGIC


# ---------- LZ4 (optional, only needed for DSAR archives) -------------------

def _load_lz4_block():
    try:
        from lz4 import block  # type: ignore
        return block
    except Exception:
        pass
    # Fall back to vendored copy bundled with the Blender plugin, if present.
    try:
        import importlib.util
        import sys
        for name in ("lz4_311", "lz4_310"):
            here = os.path.dirname(os.path.abspath(__file__))
            vendored = os.path.normpath(
                os.path.join(here, "..", "utils", name, "block.py")
            )
            if os.path.exists(vendored):
                spec = importlib.util.spec_from_file_location(
                    f"_lz4_vendored.{name}.block", vendored
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                return module
    except Exception:
        pass
    return None


# ---------- TOC structures --------------------------------------------------

@dataclass
class TocFileType:
    type_id: int = 0
    num_files: int = 0
    unk1: int = 0
    unk2: int = 16
    unk3: int = 64

    SIZE = 32

    @classmethod
    def unpack(cls, data: bytes, off: int) -> "TocFileType":
        unk1, type_id, num_files, unk2, unk3 = struct.unpack_from(
            "<QQQII", data, off
        )
        return cls(type_id=type_id, num_files=num_files, unk1=unk1, unk2=unk2, unk3=unk3)

    def pack(self) -> bytes:
        return struct.pack(
            "<QQQII", self.unk1, self.type_id, self.num_files, self.unk2, self.unk3
        )


@dataclass
class TocEntry:
    file_id: int = 0
    type_id: int = 0
    toc_data_offset: int = 0
    stream_offset: int = 0
    gpu_offset: int = 0
    unknown1: int = 0
    unknown2: int = 0
    toc_size: int = 0
    stream_size: int = 0
    gpu_size: int = 0
    unknown3: int = 16
    unknown4: int = 64
    entry_index: int = 0

    toc_data: bytes = b""
    gpu_data: bytes = b""
    stream_data: bytes = b""

    SIZE = 80

    @classmethod
    def unpack_header(cls, data: bytes, off: int) -> "TocEntry":
        (
            file_id, type_id, toc_off, stream_off, gpu_off,
            u1, u2, toc_sz, stream_sz, gpu_sz, u3, u4, idx,
        ) = struct.unpack_from("<QQQQQQQIIIIII", data, off)
        return cls(
            file_id=file_id, type_id=type_id,
            toc_data_offset=toc_off, stream_offset=stream_off, gpu_offset=gpu_off,
            unknown1=u1, unknown2=u2,
            toc_size=toc_sz, stream_size=stream_sz, gpu_size=gpu_sz,
            unknown3=u3, unknown4=u4, entry_index=idx,
        )

    def pack_header(self) -> bytes:
        return struct.pack(
            "<QQQQQQQIIIIII",
            self.file_id, self.type_id,
            self.toc_data_offset, self.stream_offset, self.gpu_offset,
            self.unknown1, self.unknown2,
            len(self.toc_data), len(self.stream_data), len(self.gpu_data),
            self.unknown3, self.unknown4, self.entry_index,
        )


@dataclass
class StreamToc:
    """In-memory representation of a single LEGACY-format package
    (`<name>` + `<name>.gpu_resources` + `<name>.stream`)."""

    types: List[TocFileType] = field(default_factory=list)
    entries: List[TocEntry] = field(default_factory=list)
    unknown: int = 0
    unk4_data: bytes = field(default_factory=lambda: b"\x00" * 56)
    name: str = ""

    # ------- load -------

    @classmethod
    def from_files(cls, toc_path: str,
                   bundle_index: Optional["BundleIndex"] = None) -> "StreamToc":
        toc_data, gpu_data, stream_data = _load_triple(toc_path, bundle_index=bundle_index)
        return cls.from_buffers(toc_data, gpu_data, stream_data, name=os.path.basename(toc_path))

    @classmethod
    def from_buffers(cls, toc_data: bytes, gpu_data: bytes, stream_data: bytes,
                     name: str = "") -> "StreamToc":
        if len(toc_data) < 72:
            raise ValueError(f"toc too small: {len(toc_data)} bytes")
        magic, num_types, num_files, unknown = struct.unpack_from("<IIII", toc_data, 0)
        if magic != LEGACY_MAGIC:
            raise ValueError(f"bad magic 0x{magic:08X} (expected 0x{LEGACY_MAGIC:08X})")
        toc = cls(name=name, unknown=unknown, unk4_data=bytes(toc_data[16:72]))

        off = 72
        for i in range(num_types):
            toc.types.append(TocFileType.unpack(toc_data, off))
            off += TocFileType.SIZE

        for i in range(num_files):
            entry = TocEntry.unpack_header(toc_data, off)
            off += TocEntry.SIZE
            entry.toc_data = bytes(toc_data[entry.toc_data_offset:entry.toc_data_offset + entry.toc_size])
            if entry.gpu_size:
                entry.gpu_data = bytes(gpu_data[entry.gpu_offset:entry.gpu_offset + entry.gpu_size])
            if entry.stream_size:
                entry.stream_data = bytes(stream_data[entry.stream_offset:entry.stream_offset + entry.stream_size])
            toc.entries.append(entry)

        return toc

    # ------- helpers -------

    def by_type(self) -> Dict[int, List[TocEntry]]:
        out: Dict[int, List[TocEntry]] = {}
        for t in self.types:
            out[t.type_id] = []
        for e in self.entries:
            out.setdefault(e.type_id, []).append(e)
        return out

    def find(self, file_id: int, type_id: int) -> Optional[TocEntry]:
        for e in self.entries:
            if e.file_id == file_id and e.type_id == type_id:
                return e
        return None

    # ------- save (LEGACY format) -------

    def write(self, toc_path: str) -> None:
        toc_buf, gpu_buf, stream_buf = self.serialize()
        with open(toc_path, "wb") as f:
            f.write(toc_buf)
        with open(toc_path + ".gpu_resources", "wb") as f:
            f.write(gpu_buf)
        with open(toc_path + ".stream", "wb") as f:
            f.write(stream_buf)

    def serialize(self):
        # Refresh type table (group entries by their type_id, preserving order).
        by_type: Dict[int, List[TocEntry]] = {}
        type_order: List[int] = []
        for e in self.entries:
            if e.type_id not in by_type:
                by_type[e.type_id] = []
                type_order.append(e.type_id)
            by_type[e.type_id].append(e)
        self.types = [TocFileType(type_id=tid, num_files=len(by_type[tid])) for tid in type_order]

        ordered = [e for tid in type_order for e in by_type[tid]]
        num_types = len(self.types)
        num_files = len(ordered)

        # ---- Pass 1: lay out toc / gpu / stream regions ----
        toc_header_size = 72 + num_types * TocFileType.SIZE + num_files * TocEntry.SIZE
        data_cursor = toc_header_size
        gpu_cursor = 0
        stream_cursor = 0

        for idx, entry in enumerate(ordered, start=1):
            entry.entry_index = idx
            entry.toc_data_offset = data_cursor
            data_cursor += len(entry.toc_data)

            if entry.gpu_data:
                gpu_cursor = ceil(gpu_cursor / 64) * 64
                entry.gpu_offset = gpu_cursor
                gpu_cursor += len(entry.gpu_data)
            else:
                entry.gpu_offset = 0

            if entry.stream_data:
                stream_cursor = ceil(stream_cursor / 64) * 64
                entry.stream_offset = stream_cursor
                stream_cursor += len(entry.stream_data)
            else:
                entry.stream_offset = 0

        # ---- Pass 2: serialize ----
        toc_buf = bytearray()
        toc_buf += struct.pack("<IIII", LEGACY_MAGIC, num_types, num_files, self.unknown)
        toc_buf += self.unk4_data.ljust(56, b"\x00")[:56]
        for t in self.types:
            toc_buf += t.pack()
        for entry in ordered:
            toc_buf += entry.pack_header()
        # Per-entry data, in the same order
        for entry in ordered:
            assert len(toc_buf) == entry.toc_data_offset
            toc_buf += entry.toc_data

        # Minimum size constraint observed by the SDK: 256 bytes per file.
        min_size = 256 * num_files
        if len(toc_buf) < min_size:
            toc_buf.extend(b"\x00" * (min_size - len(toc_buf)))

        gpu_buf = bytearray()
        for entry in ordered:
            if entry.gpu_data:
                if len(gpu_buf) < entry.gpu_offset:
                    gpu_buf.extend(b"\x00" * (entry.gpu_offset - len(gpu_buf)))
                gpu_buf[entry.gpu_offset:entry.gpu_offset + len(entry.gpu_data)] = entry.gpu_data

        stream_buf = bytearray()
        for entry in ordered:
            if entry.stream_data:
                if len(stream_buf) < entry.stream_offset:
                    stream_buf.extend(b"\x00" * (entry.stream_offset - len(stream_buf)))
                stream_buf[entry.stream_offset:entry.stream_offset + len(entry.stream_data)] = entry.stream_data

        return bytes(toc_buf), bytes(gpu_buf), bytes(stream_buf)


# ---------- Bundle index ----------------------------------------------------

@dataclass(frozen=True)
class BundleEntry:
    original_archive_offset: int
    start_offset: int
    bundle_index: int


@dataclass(frozen=True)
class BundlePackage:
    size: int
    entries: Tuple[BundleEntry, ...]


@dataclass
class BundleIndex:
    """Index for Slim installs where packages live inside `bundles.*.nxa`."""

    data_dir: str
    packages: Dict[str, BundlePackage]
    chunk_offsets: Dict[str, Dict[int, int]]

    @classmethod
    def from_data_dir(cls, data_dir: str) -> "BundleIndex":
        bundle_toc = _decompress_dsar(os.path.join(data_dir, "bundles.nxa"))
        chunk_offsets = _read_bundle_chunk_offsets(data_dir)
        packages = _read_bundle_packages(bundle_toc)
        return cls(data_dir=data_dir, packages=packages, chunk_offsets=chunk_offsets)

    def has_package(self, package_name: str) -> bool:
        return os.path.basename(package_name) in self.packages

    def load_package(self, package_name: str) -> bytes:
        package = self.packages.get(os.path.basename(package_name))
        if package is None:
            return b""
        return self._reconstruct_package(package)

    def load_triple(self, package_path: str) -> Tuple[bytes, bytes, bytes]:
        return (
            self.load_package(package_path),
            self.load_package(package_path + ".gpu_resources"),
            self.load_package(package_path + ".stream"),
        )

    def _reconstruct_package(self, package: BundlePackage) -> bytes:
        package_data = bytearray(package.size)
        for index, entry in enumerate(package.entries):
            item_size = _bundle_entry_size(package, index)
            data = self._read_resource(entry, item_size)
            end = entry.original_archive_offset + len(data)
            package_data[entry.original_archive_offset:end] = data
        return bytes(package_data)

    def _read_resource(self, entry: BundleEntry, size: int) -> bytes:
        bundle_name = f"bundles.{entry.bundle_index:02d}.nxa"
        bundle_path = os.path.join(self.data_dir, bundle_name)
        return _read_bundle_range(bundle_path, self.chunk_offsets[bundle_name], entry.start_offset, size)


def _read_bundle_chunk_offsets(data_dir: str) -> Dict[str, Dict[int, int]]:
    offsets: Dict[str, Dict[int, int]] = {}
    for name in os.listdir(data_dir):
        if re.fullmatch(r"bundles\.\d\d\.nxa", name):
            offsets[name] = _read_single_bundle_offsets(os.path.join(data_dir, name))
    return offsets


def _read_single_bundle_offsets(path: str) -> Dict[int, int]:
    with open(path, "rb") as f:
        f.seek(8)
        num_chunks = _read_int(f)
        f.seek(0x20)
        raw_offsets = struct.unpack(f"<{'Q24x' * num_chunks}", f.read(0x20 * num_chunks))
    return {offset: index for index, offset in enumerate(raw_offsets)}


def _read_bundle_packages(bundle_toc: bytes) -> Dict[str, BundlePackage]:
    packages: Dict[str, BundlePackage] = {}
    num_packages = _bytes_to_int(bundle_toc[0x10:0x14])
    for index in range(num_packages):
        name, size, entries = _read_bundle_package(bundle_toc, index)
        packages[name] = BundlePackage(size=size, entries=tuple(entries))
    return packages


def _read_bundle_package(bundle_toc: bytes, index: int) -> Tuple[str, int, List[BundleEntry]]:
    offset = 0x18 + index * 0x18
    size = _bytes_to_int(bundle_toc[offset:offset + 8])
    name = _read_null_string(bundle_toc, _bytes_to_int(bundle_toc[offset + 8:offset + 12]))
    count = _bytes_to_int(bundle_toc[offset + 12:offset + 16])
    entries_offset = _bytes_to_int(bundle_toc[offset + 16:offset + 20])
    entries = [_read_bundle_entry(bundle_toc, entries_offset, i) for i in range(count)]
    return name, size, sorted(entries, key=lambda e: e.original_archive_offset)


def _read_bundle_entry(bundle_toc: bytes, entries_offset: int, index: int) -> BundleEntry:
    offset = entries_offset + 0x10 * index
    return BundleEntry(
        original_archive_offset=_bytes_to_int(bundle_toc[offset:offset + 8]),
        start_offset=_bytes_to_int(bundle_toc[offset + 8:offset + 12]),
        bundle_index=bundle_toc[offset + 0x0F],
    )


def _bundle_entry_size(package: BundlePackage, index: int) -> int:
    entry = package.entries[index]
    if index + 1 == len(package.entries):
        return package.size - entry.original_archive_offset
    return package.entries[index + 1].original_archive_offset - entry.original_archive_offset


def _read_bundle_range(path: str, chunk_offsets: Dict[int, int], start_offset: int, size: int) -> bytes:
    data = bytearray()
    current_size = 0
    while current_size < size:
        resource = _read_bundle_resource(path, chunk_offsets, start_offset + current_size)
        data.extend(resource)
        current_size += len(resource)
    return bytes(data[:size])


def _read_bundle_resource(path: str, chunk_offsets: Dict[int, int], resource_offset: int) -> bytes:
    chunk_index = chunk_offsets[resource_offset]
    parts: List[bytes] = []
    with open(path, "rb") as f:
        num_chunks = _read_bundle_num_chunks(f)
        while chunk_index < num_chunks:
            chunk, chunk_type = _read_bundle_chunk(f, chunk_index)
            if chunk_type & 0x02 and parts:
                break
            parts.append(chunk)
            chunk_index += 1
    return b"".join(parts)


def _read_bundle_chunk(f, chunk_index: int) -> Tuple[bytes, int]:
    block = _load_lz4_block()
    f.seek(0x20 + 0x20 * chunk_index)
    _, comp_off, unc_sz, comp_sz, comp_type, chunk_type = struct.unpack("<QQIIBB6x", f.read(0x20))
    f.seek(comp_off)
    chunk = f.read(comp_sz)
    if comp_type == 3:
        if block is None:
            _raise_missing_lz4()
        chunk = block.decompress(chunk, uncompressed_size=unc_sz)
    return chunk, chunk_type


def _read_bundle_num_chunks(f) -> int:
    f.seek(8)
    return _read_int(f)


def _read_null_string(data: bytes, offset: int) -> str:
    end = data.index(0, offset)
    return data[offset:end].decode()


def _read_int(f) -> int:
    return int.from_bytes(f.read(4), "little")


def _bytes_to_int(data: bytes) -> int:
    return int.from_bytes(data, "little")


# ---------- DSAR / LEGACY raw loaders ---------------------------------------

def _decompress_dsar(path: str) -> bytes:
    block = _load_lz4_block()
    with open(path, "rb") as f:
        data = f.read()
    magic, _, num_chunks = struct.unpack_from("<III", data, 0)
    if magic != DSAR_MAGIC:
        raise ValueError(f"not a DSAR file: 0x{magic:08X}")
    parts: List[bytes] = []
    for i in range(num_chunks):
        off = 0x20 + i * 0x20
        unc_off, comp_off, unc_sz, comp_sz, comp_type, _ct = struct.unpack_from(
            "<QQIIBB", data, off
        )
        chunk = data[comp_off:comp_off + comp_sz]
        if comp_type == 3:  # LZ4
            if block is None:
                _raise_missing_lz4()
            chunk = block.decompress(chunk, uncompressed_size=unc_sz)
        parts.append(chunk)
    return b"".join(parts)


def _raise_missing_lz4() -> None:
    raise RuntimeError(
        "DSAR archive needs LZ4 decompression but `lz4` package is not "
        "installed and no vendored copy was found. Install with "
        "`pip install lz4` or copy the plugin's utils/lz4_311 folder "
        "alongside this tool."
    )


def _read_or_empty(path: str) -> bytes:
    if not os.path.exists(path):
        return b""
    with open(path, "rb") as f:
        return f.read()


def _detect_kind(path: str, bundle_index: Optional[BundleIndex] = None) -> str:
    if not os.path.exists(path):
        if bundle_index is not None and bundle_index.has_package(path):
            return "bundled"
        raise FileNotFoundError(path)
    with open(path, "rb") as f:
        magic = int.from_bytes(f.read(4), "little")
    if magic == LEGACY_MAGIC:
        return "legacy"
    if magic == DSAR_MAGIC:
        return "dsar"
    raise ValueError(f"unknown package magic 0x{magic:08X} in {path}")


def _load_triple(path: str, bundle_index: Optional[BundleIndex] = None):
    kind = _detect_kind(path, bundle_index=bundle_index)
    if kind == "legacy":
        toc = _read_or_empty(path)
        gpu = _read_or_empty(path + ".gpu_resources")
        stream = _read_or_empty(path + ".stream")
    elif kind == "bundled":
        assert bundle_index is not None
        toc, gpu, stream = bundle_index.load_triple(path)
    else:
        toc = _decompress_dsar(path)
        gpu = _decompress_dsar(path + ".gpu_resources") if os.path.exists(path + ".gpu_resources") else b""
        stream = _decompress_dsar(path + ".stream") if os.path.exists(path + ".stream") else b""
    return toc, gpu, stream


# ---------- Lightweight FileID index (for source autodetect) ---------------

def list_file_ids(toc_path: str,
                  bundle_index: Optional[BundleIndex] = None) -> Dict[int, List[int]]:
    """Return {type_id: [file_id, ...]} from an archive without loading bodies."""
    kind = _detect_kind(toc_path, bundle_index=bundle_index)
    if kind == "legacy":
        with open(toc_path, "rb") as f:
            header = f.read(72)
        magic, num_types, num_files, _ = struct.unpack_from("<IIII", header, 0)
        if magic != LEGACY_MAGIC:
            return {}
        with open(toc_path, "rb") as f:
            f.seek(72 + num_types * TocFileType.SIZE)
            entry_bytes = f.read(num_files * TocEntry.SIZE)
    elif kind == "bundled":
        assert bundle_index is not None
        data = bundle_index.load_package(toc_path)
        magic, num_types, num_files, _ = struct.unpack_from("<IIII", data, 0)
        if magic != LEGACY_MAGIC:
            return {}
        start = 72 + num_types * TocFileType.SIZE
        entry_bytes = data[start:start + num_files * TocEntry.SIZE]
    else:
        data = _decompress_dsar(toc_path)
        magic, num_types, num_files, _ = struct.unpack_from("<IIII", data, 0)
        if magic != LEGACY_MAGIC:
            return {}
        start = 72 + num_types * TocFileType.SIZE
        entry_bytes = data[start:start + num_files * TocEntry.SIZE]

    out: Dict[int, List[int]] = {}
    for i in range(0, len(entry_bytes), TocEntry.SIZE):
        file_id, type_id = struct.unpack_from("<QQ", entry_bytes, i)
        out.setdefault(type_id, []).append(file_id)
    return out
