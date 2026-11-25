# VIctory fork

This Rust fork targets NVIDIA hardware on Windows through Direct3D 12 and requires Shader Model 6.6. Run the original game executable. Implement compatibility changes in VIctory.

## Offline preparation and launch

- DirectStorage reads the original game archives. Test raw, zlib, and BCPack requests when changing that path.
- For shader changes, test both `translate --cache --pipelines` and `optimize-shaders --sdk`. Use the same `VICTORY_CACHE_DIR` for preparation and play. Preserve the original host shader when LLVM validation fails.
- The separate portable distribution starts with `Start-VIctory.cmd`. Resolve bundled tools and the per-game `cache` relative to that launcher. The game's `data` folder inside that cache contains saves.
- Test portable changes after extraction to a new folder. Cover first launch, cache reuse, and repair after removing a cached shader record. Use bundled dependencies for all three tests.

## Renderer and performance checks

- Build the optimized emulator with `cargo build --locked --profile aot -p victory`. Add `--features victory-hle/scripted-input` for automated replay.
- Compare the same scene, save, and input sequence. Record shader cache state and dropped draws. Check the rendered image when a change improves frame time.
- Set `VICTORY_PROFILE_FRAMES=1` to measure individual heavy frames. Report GPU waits separately from GPU execution. Readback work can overlap submission preparation.
- For texture prediction changes, use the [scene map replay procedure](docs/scene-map.md) to test predictions against a second run.

## Source and attribution

Keep game files, shader caches, captures, and credentials outside tracked source. Use example paths in documentation. Follow the [asset policy](docs/asset-policy.md) for test fixtures. Preserve authorship when importing upstream commits.
