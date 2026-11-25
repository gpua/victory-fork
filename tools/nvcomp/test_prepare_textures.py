"""Regression tests for offline graphics archives."""

import concurrent.futures
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from nvidia import nvcomp
import zstandard

import prepare_textures as packs


class ArchivePreparationTest(unittest.TestCase):
    def test_payload_stays_at_indexed_offset_after_header_read(self):
        """A separately mapped index must not move the payload reader on Windows."""
        raw = bytes(range(256)) * 513
        source = zlib.compress(raw)
        offset = 8192
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            archive = folder / "geometry.rpf"
            header = bytearray(offset)
            header[:4] = b"9FPR"
            struct.pack_into("<I", header, 4, 64)
            archive.write_bytes(header + source)
            codec = nvcomp.Codec(algorithm="Zstd", bitstream_kind=nvcomp.BitstreamKind.RAW)
            with concurrent.futures.ThreadPoolExecutor(1) as workers:
                packs.pack_store(archive, folder, codec, workers, None)
                packs.pack_store(archive, folder, codec, workers, None)
            output, = folder.glob("*.vtxz")
            with output.open("rb") as f:
                _, count, index, _, _ = packs.HEADER.unpack(f.read(packs.HEADER.size))
                self.assertEqual(count, 1)
                f.seek(index)
                start, stored, length, flags, size, position, check = packs.RECORD.unpack(f.read(packs.RECORD.size))
                self.assertEqual((start, stored, length, flags), (offset, len(source), len(raw), 1))
                self.assertEqual(check, packs.digest(raw))
                f.seek(position)
                self.assertEqual(zstandard.ZstdDecompressor().decompress(f.read(size)), raw)


if __name__ == "__main__":
    unittest.main()
