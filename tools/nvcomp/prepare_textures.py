"""Build a lossless nvCOMP cache from RDS5 texture stores."""

import argparse
import concurrent.futures
import ctypes
import os
from pathlib import Path
import struct
import tempfile
import zlib

import blake3
import numpy as np
from nvidia import nvcomp
import zstandard


HEADER = struct.Struct("<8sQQ16sQ")
RECORD = struct.Struct("<QIIIIQ16s")
MAGIC = b"VTXZ0001"
BLOCK_LIMIT = 4 << 20
BATCH_LIMIT = 16 << 20
FILETIME_EPOCH = 116444736000000000


def digest(data):
    return blake3.blake3(data).digest(length=16)


def requests(source, file_size):
    """Read the RDS5 physical and virtual boundaries for each request."""
    header = source.read(24)
    if header[:4] != b"RDS5":
        raise ValueError("Invalid texture store signature")
    count = struct.unpack_from("<I", header, 4)[0]
    if not 2 <= count <= min(file_size // 8, 4_000_000):
        raise ValueError("Invalid texture block count")
    table = source.read((count - 1) * 8)
    if len(table) != (count - 1) * 8:
        raise ValueError("Incomplete texture block table")
    prev = None
    for a, b in struct.iter_unpack("<II", table):
        if not (a or b):
            break
        offset = (a & 0x03FFFFFF) << 8
        virtual = 4096 + ((b & 0xFFFFF) << 14) + ((a >> 30) << 12)
        if prev is not None:
            start, base, flags, padding = prev
            size = offset - start - padding
            decoded = virtual - base
            if not (0 < size <= 2 * BLOCK_LIMIT and 0 < decoded <= BLOCK_LIMIT and offset <= file_size):
                raise ValueError("Invalid texture block extent")
            if flags:
                if flags & 0x20 and flags != 0x20:
                    raise ValueError("Unsupported two-stage texture store block")
                options = 1 if flags == 0x20 else flags >> 1
                yield start, size, decoded, options
        prev = offset, virtual, (a >> 24) & 0x3C, b >> 20


def archive_requests(source, file_size):
    """Index complete zlib streams inside a graphics RPF9 archive."""
    import mmap
    import re

    with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:4] != b"9FPR":
            raise ValueError("Invalid RPF9 archive signature")
        pos = struct.unpack_from("<I", data, 4)[0]
        signatures = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
        while match := signatures.search(data, pos):
            start = match.start()
            pos = start + 2
            stream = zlib.decompressobj()
            cursor = start
            length = 0
            try:
                while cursor < file_size and length <= BLOCK_LIMIT and cursor - start < 2 * BLOCK_LIMIT:
                    end = min(file_size, cursor + (256 if cursor == start else 65536))
                    length += len(stream.decompress(data[cursor:end], BLOCK_LIMIT + 1 - length))
                    cursor = end
                    if stream.eof:
                        size = end - start - len(stream.unused_data)
                        pos = start + size
                        if 0 < length <= BLOCK_LIMIT:
                            yield start, size, length, 1
                        break
            except zlib.error:
                continue


def decoder(path):
    """Load the emulator's byte-exact BCPack decoder."""
    fn = ctypes.CDLL(str(path.resolve())).victory_bcpack_decode
    fn.argtypes = [ctypes.c_uint8, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    fn.restype = ctypes.c_bool
    return fn


def unpack(item, bcpack):
    """Decode a source block and verify its expected extent."""
    entry, data = item
    _, size, length, options = entry
    if len(data) != size:
        raise ValueError("Incomplete source block")
    if options == 1:
        stream = zlib.decompressobj()
        raw = stream.decompress(data, length + 1)
        if not stream.eof or len(raw) != length:
            raise ValueError("Unexpected zlib output length")
    else:
        out = ctypes.create_string_buffer(length)
        if not bcpack(options >> 1, data, size, out, length):
            raise ValueError("BCPack decoder rejected the block")
        raw = out.raw
    return raw


def pack_store(path, folder, codec, workers, bcpack):
    """Publish one verified sidecar after all its blocks pass independent decoding."""
    st = path.stat()
    stamp = FILETIME_EPOCH + st.st_mtime_ns // 100
    target = folder / ("%s.%x.%x.vtxz" % (path.name.lower(), st.st_size, stamp))
    with path.open("rb") as source:
        identity = digest(source.read(4096))
        source.seek(0)
        if target.exists():
            with target.open("rb") as cached:
                head = cached.read(HEADER.size)
                if len(head) == HEADER.size:
                    magic, count, index, check, size = HEADER.unpack(head)
                    if magic == MAGIC and check == identity and size == st.st_size and index + count * RECORD.size == target.stat().st_size:
                        return 0
            raise ValueError("Existing sidecar failed validation: %s" % target)
        scan = archive_requests if path.suffix.lower() == ".rpf" else requests
        entries = iter(scan(source, st.st_size))
        records = bytearray()
        checker = zstandard.ZstdDecompressor()
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".partial", delete=False) as output:
            temp = Path(output.name)
        try:
            with temp.open("wb") as output:
                output.write(bytes(HEADER.size))
                while True:
                    batch = []
                    total = 0
                    while total < BATCH_LIMIT and len(batch) < 128:
                        entry = next(entries, None)
                        if entry is None:
                            break
                        source.seek(entry[0])
                        batch.append((entry, source.read(entry[1])))
                        total += entry[2]
                    if not batch:
                        break
                    raws = list(workers.map(unpack, batch, [bcpack] * len(batch)))
                    arrays = [np.frombuffer(raw, dtype=np.uint8) for raw in raws]
                    frames = codec.encode(nvcomp.as_arrays(arrays)).cpu()
                    for (entry, _), raw, frame in zip(batch, raws, frames, strict=True):
                        packed = np.asarray(frame).tobytes()
                        if checker.decompress(packed, max_output_size=len(raw)) != raw:
                            raise ValueError("NVIDIA frame failed byte comparison")
                        records.extend(RECORD.pack(*entry, len(packed), output.tell(), digest(raw)))
                        output.write(packed)
                index = output.tell()
                output.write(records)
                output.seek(0)
                output.write(HEADER.pack(MAGIC, len(records) // RECORD.size, index, identity, st.st_size))
            if path.stat().st_mtime_ns != st.st_mtime_ns or path.stat().st_size != st.st_size:
                raise ValueError("Source changed during cache preparation")
            os.rename(temp, target)
            print("%s: %d blocks, %d bytes" % (path.name, len(records) // RECORD.size, target.stat().st_size), flush=True)
            return target.stat().st_size
        finally:
            temp.unlink(missing_ok=True)


def main():
    """Build each offline store once."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=Path)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--bcpack", type=Path, required=True)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    bcpack = decoder(args.bcpack)
    codec = nvcomp.Codec(algorithm="Zstd", bitstream_kind=nvcomp.BitstreamKind.RAW)
    paths = sorted(args.game.rglob("*.ttpf"), key=lambda p: ("peds" not in p.stem, p.stat().st_size))
    graphics_archive = args.game / "textures.rpf"
    if graphics_archive.is_file():
        paths.append(graphics_archive)
    if not paths:
        parser.error("No texture stores or graphics archive found")
    # Windows releases the lock if the preparer exits.
    import msvcrt
    with (args.cache / ".prepare.lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            for path in paths:
                pack_store(path, args.cache, codec, workers, bcpack)


if __name__ == "__main__":
    main()
