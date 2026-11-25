# Offline texture packs

VIctory can serve DirectStorage requests from a local pack. Each Zstandard frame contains the exact guest bytes for one compressed texture request. The preparer uses the RDS5 chunk table to find those requests. [SixNG texture store layout](https://github.com/holdmysocks/SixNG/blob/HEAD/crates/sixng-core/src/ttpf.rs)

On Windows, install NumPy, blake3, zstandard, and the NVIDIA nvCOMP Python wheel for your CUDA driver in a project environment. The preparation step requires an NVIDIA GPU. [NVIDIA nvCOMP](https://github.com/NVIDIA/nvcomp)

From the VIctory checkout, run these commands in an MSVC shell. Set the two paths to the installation and a separate output directory.

```powershell
$game = 'D:\Games\Title'
$packs = 'D:\OfflineTextures'
cargo build --locked --release -p victory-formats --example offline_bcpack
python tools/nvcomp/prepare_textures.py $game $packs --bcpack target/release/examples/offline_bcpack.dll
$env:VICTORY_TEXTURE_PACKS = $packs
```

Start VIctory from the same shell. An existing launcher can also set `VICTORY_TEXTURE_PACKS`. Remove this variable to compare with the original reads. Shader preparation uses its usual cache and settings.

The preparer expands zlib blocks and calls VIctory's decoder for BCPack blocks. nvCOMP compresses each batch on the GPU. A second decoder checks every byte before the preparer publishes the file. A repeat run reuses complete packs. The preparer also indexes complete zlib streams of up to 4 MiB in the root textures.rpf graphics archive. Use repeated `--archive` arguments to include additional RPF9 archives with level geometry, terrain, animation, or acceleration structures. The runtime can reuse only exact file requests. Raw blocks and larger archive streams use their original storage path. [RPF9 archive research](https://github.com/JoshRob297/rpf9-research)

During play, a file match requires the source name, length, modification time, and header fingerprint. For each read, the offset, source length, output length, and codec must match. VIctory decodes the frame in host memory and verifies its BLAKE3 digest before the guest receives the bytes. A missing entry or failed verification selects the original decode path. Texture packs are read-only during play. Set `VICTORY_PROFILE_FRAMES=1` for a separate profiling run. VIctory logs individual heavy presents with CPU preparation, GPU waits, upload bytes, and readback costs. Worker timers can overlap. Keep all game files and generated packs outside the checkout.
