# VIctory for NVIDIA GPUs

This fork runs Xbox Series titles on Windows through Direct3D 12. The game
renderer selects an NVIDIA hardware adapter with Shader Model 6.6 support.
It can translate shaders and build supported pipelines before the game starts.

**This repository contains no game files, code or assets.** You need your own
copy of the game. It is not affiliated with or endorsed by the game's
developer or publisher.

## Renderer performance

VIctory batches consecutive indirect draws that use the same pipeline, argument buffer, and command state.
It also checks buffer descriptors incrementally and uploads the mip levels that a placed 2D texture view can sample.
A later view can request more detailed mip levels.

Set `VICTORY_INDIRECT_MERGE=0` to compare individual host draws. Set `VICTORY_INDIRECT_WINDOW=0` to clamp each request separately.
Set `VICTORY_CLAMP_SCOPED=0` to test the global barrier path. Buffers that alias guest memory use that path automatically.

VIctory can replace the screen-probe temporal resolve output with constant indirect light when the ray builder is inactive.
The default keeps the game's resolve output. Set `VICTORY_AMBIENT=0.6` to test Belobug's tint.
A numeric value of zero writes black indirect light. Direct light and material colors still come from the game.

Pixel shaders use quad reads for neighboring pixels at triangle edges. Shaders that use a partial interpolation can read its first result.
Screen-space pipelines have a dedicated compilation worker. Completed DirectStorage writes mark the corresponding guest memory ranges for refresh.

## Credits

- Belobug contributed the screen-probe light fallback, pixel shader fixes, screen-pass scheduling, and streamed-write fixes adapted here.
  [Radeon fork and original authors](https://github.com/Belobug/VIctory-Belobug-AMD/tree/4cbe9d76cf3c1fdca7f9a8d8364eacc33e7aa597)

- Vitutu's renderer ports provide indirect batching, scoped clamp barriers, incremental descriptor checks, and indexed buffer transfers.
  The indirect batching and clamp changes originated in ZigZagBoi1's ScarlettPort. [victory-perf](https://github.com/vitutu1/victory-perf/commit/b230f2a)
- The partial-mip upload path comes from the victory-world-runtime contributors through Vitutu's port.
  [Upstream change and contributor credits](https://github.com/vitutu1/victory-perf/commit/1412d43)

- **[VIctory](https://github.com/Marvisak/VIctory)** by Marvisak and its
  contributors is the base of everything here: the loader, the runtime
  services, the Direct3D 12 back end and the shader recompiler. Licensed
  GPL-3.0-or-later. This fork uses the same license.
- Marvis implemented [offline shader translation](https://github.com/Marvisak/VIctory/commit/c47b5297ec550cc8871e0523e8d64403ccbaea90)
  and [pipeline precompilation](https://github.com/Marvisak/VIctory/commit/9c58a1c35a384552bde9b4c9b169731f282a15a1).
  t4l03 adapted both features for this codebase and added persistent cache records
  and pipeline selection from run history in [PR #282](https://github.com/fxceless/victory-fork/pull/282).
- **[XCL](https://github.com/ivall/xcl)** by ivall and its contributors
  (GPL-2.0-only) was used as a reference for how the same title behaves under
  another layer. Fixes inspired by it were written anew in VIctory's code.
- This fork's changes were developed with [Claude Code](https://claude.com/claude-code).

## Status reported by the parent fork (2026-11-25)

**Not playable yet: 2.5 to 4 frames per second, and shadowed areas are black.**
Tested on one machine: Windows 11, Intel i7-14700KF, NVIDIA RTX 4070 12 GB,
32 GB RAM. The Series X profile is used throughout.

| Stage | State in this fork |
|-------|--------------------|
| Boot, menus, start screen | works, 30 fps (as upstream) |
| Opening cutscene | plays from start to finish, in order, lit: title cards, credits cards, night and dusk shots |
| Saves | the game autosaves a checkpoint and loads it again |
| First gameplay scene (the prologue) | renders: characters, textures, sky, sunlit exterior; HUD, pause menu and pad input work |
| Light in shadow | missing: the game's ray-traced bounce light and ray-traced lamp and sun shadows do not run yet, so anything the sun does not hit directly is black |
| Frame rate in the world | 2-4 fps: 420-450 ms per frame by default, 240-350 ms with the newer switches (`VICTORY_RELAY=2`, or `VICTORY_LATE_WAIT=3 VICTORY_WIDE_BACK=1 VICTORY_CARD_BUFFERS_MIN_MB=1 VICTORY_CARD_BUFFERS_MB=1024`); it started at 3-6 seconds per frame |
| Stability | no driver hang or device loss in normal runs (a hang introduced by the wave64 update was found and fixed: its barrier form for 64-lane programs is now off unless `VICTORY_WAVE_WIDE=1`) |

### What this fork changed (summary)

Rendering:
- Meshlet culling read its scene buffers as the wrong kind of view, so the world drew almost nothing; fixed.
- Per-draw descriptor tables could run out, after which draws used slots never made for them and the card hung; fixed.
- Light packed by the game's compute shaders was read back through empty copies (sky, probes, fog); fixed for every mip level and layer.
- The fog reconstruction, shadow-mask, hair, blend-shape skinning, fire and several material programs did not translate; fixed.
- Gathers at a named mip level (with and without an offset) read level 0; fixed.
- Implicit-level samples outside pixel shaders produced invalid DXIL in 730 compute programs; fixed.
- Rectangle lists, depth and stencil twins for depth textures written by compute, texture read-backs.
- Ray tracing, in progress and off by default: the game's own structure-builder programs translate, title memory can be read by raw address from shaders, the hardware ray/box and ray/triangle test is emulated in DXIL (checked against a reference on the GPU), and 59 of the 64 trace programs translate. Nothing is lit by it yet. See [docs/fork-ray-tracing.md](docs/fork-ray-tracing.md).

Speed and memory:
- Waits for memory values, write-watch queries and page queries no longer walk page tables per call.
- Large title buffers get a copy in card memory, with shader writes found by hashing on the card.
- Submissions can be sent ahead of the wait for the card (`VICTORY_LATE_WAIT`), with a relay thread that lands results as the card finishes (`VICTORY_RELAY`): in a first five-minute run the card's idle time behind the submitting thread fell from about 136 to 30 ms per frame.
- Title memory can be opened as a graphics heap so that buffers are used in place instead of copied (`VICTORY_INPLACE`, first stage only; a probe confirmed the approach on the test GPU).
- The bindless texture queue no longer uploads stale views; the view table no longer grows without bound; the pipeline library is mapped instead of read into memory.
- An intermittent device loss was traced to the pipeline library file being replaced while mapped; fixed.

- Translated shaders get clean-up passes over the finished module (dead code, constant folding, reuse of values, branch facts); the pack's host instruction count falls by about 43 %, worth about 20 ms of GPU time per frame on the test machine (`VICTORY_SHADER_OPT=0` turns them off).

Most of the newer speed changes sit behind environment switches that are off
by default (`VICTORY_LATE_WAIT`, `VICTORY_RELAY`, `VICTORY_WIDE_BACK`,
`VICTORY_CARD_BUFFERS_MIN_MB`, `VICTORY_INPLACE`, `VICTORY_BVH_BUILDER`, ...).
Each is described in a comment where it is read.

### Known problems

- Everything in shadow is black (see above).
- The interior of the first gameplay room is sometimes not drawn after the cutscene hands over to gameplay.
- Around 24 GB of memory is in use during play.
- A few style lints are switched off for this fork in the workspace `Cargo.toml` until the newer code is tidied.
- Many changes here are specific to this title and this GPU, and some are marked `shortcut:` in the code where they approximate what the console does.

Everything below this line is upstream VIctory's own README, unchanged; its
status section describes upstream, not this fork.

---

# VIctory

Runs the title (Xbox Series X|S) natively on Windows, from files
the user supplies.

The title's x86-64 code runs unchanged inside our process; VIctory supplies
the platform underneath it and converts the title's data, ahead of time where
it can and on first use where it must (shaders). See
[docs/architecture.md](docs/architecture.md).

## Status (2026-11-23)

**Not playable yet.** The title boots, runs its menus at its own 30 fps and
loads the world, where rendering fails.

| Stage | State |
|-------|-------|
| Boot, first-boot screens (accessibility, language, settings, legal) | works, 30 fps |
| Intro video with sound | works |
| Start screen, profile and saves | works |
| Loading into the world (about 50 s) | works |
| The world (first mission's intro) | reached and runs with indirect draws; its title card renders, the scene after it is still dark; 0.5-1.8 s per frame |

Tested on one machine: Windows 11, AMD RX 6700 XT.

> **Warning.** Entering the world on a real GPU can currently hang it, and
> Windows then resets the display driver (every monitor goes black for a
> moment). To look further, use a diagnostics build with
> `VICTORY_D3D12_WARP=1`, which renders on the software adapter.

What each part does today and what it still needs:

| Subsystem | Today | Needs |
|-----------|-------|-------|
| Loader | Runs the title's executable and its DLLs in our process; crash reports name the faulting code and are kept in `crashes.log` | – |
| Runtime services | One signed-in offline user, licences, packages, saves that survive a kill at any step, text entry dialog | Speech is silent; the dialog and relaunch are untested live |
| Network | Offline by design: the title cannot reach the network | – |
| Files and streaming | Archives and streamed data read, zlib and BCPack decoded in parallel | Loading is limited by writes into title memory |
| Input | XInput pads, keyboard, keyboard stand-in pad | Trigger rumble |
| Audio | Menu, video and loading sound; XMA voices and Opus streams decode | Reverb is silent; Opus results unverified against the title |
| GPU: menus | Draws, compute and presentation on host D3D12; only changed title memory is copied | Frame pacing drifts on one screen in release builds |
| GPU: world | Mesh pipelines, shader-written buffers, descriptor lifetimes, indirect execution (arguments clamped), pipelines made by worker threads and kept on disk | Most of the scene is still missing (untranslated shaders drop draws); frames take seconds; real queries, depth bounds, HDR and scaling are missing |
| Shaders | Console GPU code recompiled to DXIL at first use, cached on disk. Menus: all. World: mesh 8,191 of 8,506, vertex/pixel 10,617 of 12,346, compute 1,643 of 2,539 | 64-bit image atomics, merged geometry stages, the ray-tracing builder's shaders; some compute programs need a GPU with 64-lane waves |
| Textures | Console tiled layouts and detiling for the modes the title uses | Render-target metadata planes |
| Series S mode | Runs through the menus to the loading screen | The world, as above |

## NVIDIA texture streaming

VIctory packs small texture uploads into aligned regions of persistently mapped D3D12 buffers.
Command lists retain each region until the GPU completes the submission.
Set `VICTORY_UPLOAD_POOL=0` to compare dedicated upload buffers.

The renderer spends up to 64 ms per pass on queued bindless textures by default.
A backlog of at least 1,024 due slots raises the budget to 192 ms.
One upload can exceed the deadline. Pending slots remain eligible on later frames.
Set `VICTORY_TEXTURE_STAGE_MS=250` to restore the previous time budget.
The byte budget still applies. DirectStorage workers can resolve physical pages while the emulator changes virtual mappings.

## Getting started

Requirements: Windows 10/11 x64, a CPU with AVX2, an NVIDIA Direct3D 12 GPU with
shader model 6.6 (and mesh shaders for the world), Rust (pinned in
`rust-toolchain.toml`).

The title also needs several GiB of available Windows commit at startup.
Check **Committed** under **Task Manager → Performance → Memory**. If
`crashes.log` reports Windows error 1455, close other applications or increase
the Windows paging file size, then retry.

```powershell
cargo xtask install-hooks                # asset-policy check on every commit
copy victory.example.toml victory.toml   # then set your paths (outside the repo)
cargo test --workspace                   # needs no game data
cargo run -p victory -- config           # check your paths
```

Build the executable before the first warm-up. Set `VICTORY_GAME_DIR` to
the directory that contains your game. Set `VICTORY_CACHE_DIR` to a
writable directory outside the checkout. Use the same directory and shader
settings for warm-up and play.

```powershell
cargo build --locked --release -p victory
$game = $env:VICTORY_GAME_DIR
$exe = Join-Path $game 'game_scarlett_master.exe'
$inputs = @(
    (Join-Path $game 'shaders.rpf')
    $exe
    (Join-Path $game 'XboxBvhBuilder.dll')
    (Join-Path $game 'amd_fidelityfx_upscaler_xbox.dll')
)
.\target\release\victory.exe translate --cache --pipelines @inputs
.\target\release\victory.exe run $exe --execute
```

For shader translation alone, omit `--pipelines`. To limit the pipeline
build to keys recorded during earlier runs, add `--used-pipelines`.
Both modes translate all supported shaders in the supplied files.
The summary shows how many translations succeeded and how many the translator refused.

Shader entries survive application rebuilds when the translator and its
inputs stay the same. GPU capabilities and shader settings also affect
cache reuse. The D3D12 pipeline library depends on the GPU and its driver.

### Offline LLVM optimization

The `aot` profile uses Rust's LLVM backend to optimize the emulator across crates.
The guest executable still runs its original x86-64 instructions.

After shader warm-up, run the following commands in an x64 Windows SDK developer shell.
Use the same cache directory for both commands.

```powershell
cargo build --locked --profile aot -p victory
$sdk = Split-Path (Get-Command dxc.exe).Source
$emulator = '.\target\aot\victory.exe'
& $emulator optimize-shaders --sdk $sdk
$env:VICTORY_LLVM_SHADERS = '1'
& $emulator run $exe --execute
```

The command optimizes translated DXIL with the SDK's LLVM passes.
It assembles each result and checks it with the SDK validator.
A separate cache stores accepted shaders by their original host bytecode.
Play uses the regular translation for missing or damaged entries.
Set `VICTORY_LLVM_SHADERS=0` to compare the same build with regular shaders.
To prebuild optimized host pipelines, run `translate --cache --pipelines` again with the switch enabled.

The portable Windows package includes a [launcher](scripts/portable/launch.py) and the DXIL compiler.
Extract it to a writable folder and open `Start-VIctory.cmd`. Select the original game executable.
The launcher prepares shaders and pipelines in its per-game `cache` folder. Saves live in its `data` subfolder.
Use `Start-VIctory.cmd --prepare-only` to populate the cache without starting play.

Zettrox's launcher starts manual play without scripted input or frame capture.
Pass the game path to `scripts/play.ps1 -Game`. Select the AOT executable with `-Exe`.
The launcher accepts the fork's cache and save directories. [Upstream launcher](https://github.com/Zettrox/ztrx/commit/90b529f878a470fec9bf07b7be316bc769a12232).

Use `-BoundTextureUploads` to try a 32 MiB texture budget per frame with backlog acceleration disabled.
This adapts xcl's byte budget to VIctory's existing texture queue. [Zondybtw's xcl change](https://github.com/xcl-dev/xcl/commit/fb9d05910c783ee1999e5d57770ce37728d603fd).

The shader cache treats CRLF and LF source files as equivalent.
This adapts einfachama's cache identity change for xcl. [Upstream change](https://github.com/xcl-dev/xcl/commit/8c1faf1dc35388941a52cb8b8ab768f2637f3560).

The window caption shows the framerate. Saves, settings and `crashes.log`
are kept under the data folder (default `%LOCALAPPDATA%\victory\data`), derived
data under the cache folder.

Input: XInput pads. With none connected the keyboard is a pad while the
title's window has focus:

| Keys | Pad |
|------|-----|
| W A S D / I J K L | left / right stick |
| arrows | D-pad |
| Enter, Backspace, X, Y | A, B, X, Y |
| Q / E, Z / C | shoulders, triggers |
| F / H | stick clicks |
| Esc, Tab | Menu, View |

## Commands

```
victory inspect <exe|dll> [--imports] [--exports]
victory census [paths...] [--missing] [--strict]
victory lib-exports <import.lib> [--list]
victory shader-info [paths...] [--verbose] [--disasm]
victory translate [paths...] [--validate] [--pipelines] [--list] [--jobs N]
victory translate [paths...] --cache [--pipelines [--used-pipelines]] [--jobs N]
victory run <exe> [--execute] [--unimplemented trap|zero] [--strict]
            [--console series-x|series-s] [-- <title arguments>]
victory config
```

Logging: `VICTORY_LOG=victory_loader=trace,info` (a release build logs
info and above).

For automated performance runs with production rendering, build with
`cargo build --release -p victory --features victory-hle/scripted-input`.
Set `VICTORY_KEYS=18:Enter,40:Enter` to replay the two inputs in the emulator.

To collect archive ranges and GPU texture indices across game sessions, use the [scene texture mapper](docs/scene-map.md).

## Debugging

A release build has no debugging aids. Build them in with

```powershell
cargo build --profile diag -p victory --features diagnostics   # target\diag\victory.exe
```

| Variable | Effect |
|----------|--------|
| `VICTORY_LOG=<target>=debug,...` | debug and trace output |
| `VICTORY_LOG=victory_gpu::bindless=debug` | reports microseconds per bindless pass for descriptor lookup and range collection, buffer refresh, and texture processing |
| `VICTORY_STACKS=20,60` | every thread's stack at those seconds |
| `VICTORY_WATCH=<rva>,...` | logs a title u32 when it changes |
| `VICTORY_KEYS=30:Enter,35:X` | presses keys at those seconds, focus or not |
| `VICTORY_CRASH_SAVE=<rva>:<len>,...` | saves title memory to the cache folder on a crash |
| `VICTORY_CAPTURE=<dir>` | saves presented frames (powers of two, or `VICTORY_CAPTURE_FRAMES=<list>`) |
| `VICTORY_DUMP_TARGETS=1` | with the two above, saves every render target of those frames |
| `VICTORY_D3D12_DEBUG=1` | host debug layer |
| `VICTORY_D3D12_GBV=1` | with the above, GPU-based validation (bad descriptor use reported, not faulted; slow) |
| `VICTORY_D3D12_DRED=1` | on a GPU failure, the allocation hit and the commands in flight |
| `VICTORY_PIPELINE_WAIT_MS=24` | reduce the per-frame wait for background pipeline creation from the default of 500 ms. A draw can be omitted until its pipeline is ready |
| `VICTORY_SKIP_PIPELINES=<hash>,...` | omit draws and dispatches, including indirect ones, whose pipeline contains a listed 16-digit shader hash |
| `VICTORY_GPU_TIMING=1` | with the above, each list of a submission run and timed on its own; slow lists logged with their slowest commands |
| `VICTORY_D3D12_WARP=1` | host device on the software adapter (slow; cannot reset the driver) |
| `VICTORY_SHADER_LOOP_LIMIT=<n>` | translated shader loops stop after `n` iterations per lane instead of 2^20 (own shader cache entries); if a lower `n` makes world frames much faster, loops hitting the limit are what makes them slow |
| `VICTORY_AUDIO_CAPTURE=<file.wav>` | records what the title plays (our process only) |
| `VICTORY_XMA_DUMP=<dir>` | saves the first 32 XMA voices' context and input |

Every build appends crashes and host GPU failures to `crashes.log` in the
title's data folder.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
