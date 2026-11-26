# VIctory fork

This Rust fork targets NVIDIA hardware on Windows through Direct3D 12 and requires Shader Model 6.6. Run the original game executable. Implement compatibility changes in VIctory.

## Offline preparation and launch

- DirectStorage reads the original game archives. Test raw, zlib, and BCPack requests when changing that path.
- For shader changes, test both `translate --cache --pipelines` and `optimize-shaders --sdk`. Use the same `VICTORY_CACHE_DIR` for preparation and play. Preserve the original host shader when LLVM validation fails.
- The separate portable distribution starts with `Start-VIctory.cmd`. Resolve bundled tools and the per-game `cache` relative to that launcher. The game's `data` folder inside that cache contains saves.
- Build distributable archives with `python scripts/package.py` from an x64 MSVC developer prompt. The [build instructions](README.md#build-the-portable-windows-zip) list dependencies and offline options.
- Package the normal-input AOT binary. Keep the archive allowlist and path remapping in the packaging script.
- Test portable changes after extraction to a new folder. Cover first launch, cache reuse, and repair after removing a cached shader record. Use bundled dependencies for all three tests.

## Renderer and performance checks

- Build the optimized emulator with `cargo build --locked --profile aot -p victory`. Add `--features victory-hle/scripted-input` for automated replay.
- Compare the same scene, save, and input sequence. Record shader cache state and dropped draws. Check the rendered image when a change improves frame time.
- Set `VICTORY_PROFILE_FRAMES=1` to measure individual heavy frames. Report GPU waits separately from GPU execution. Readback work can overlap submission preparation.
- Test a partial physical remap against reads of both neighboring pages. Windows briefly removes the entire original view during a split.
- Check video memory and free system RAM before retaining idle textures. Verify the explicit opt-out and the low-memory fallback.
- Match gameplay samples by visible camera position and draw count. Record repeated runs and overlapping build or test activity.
- For texture prediction changes, use the [scene map replay procedure](docs/scene-map.md) to test predictions against a second run.

## Source and attribution

Keep game files, shader caches, captures, and credentials outside tracked source. Use example paths in documentation. Follow the [asset policy](docs/asset-policy.md) for test fixtures. Preserve upstream authorship. Use the contributor's verified GitHub no-reply address for new port commits.

Before publishing, inspect tracked files, outgoing commit messages and identities, and the extracted ZIP for personal paths, host identifiers and credentials. Check native binaries and nested archives. Keep privacy scan results local.
