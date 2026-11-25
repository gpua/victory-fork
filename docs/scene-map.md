# Scene texture map

The mapper learns which texture data VIctory receives during a scene. It stores metadata in a local SQLite database. It can collect data during ordinary play or import a recorded event log. It requires Python 3.11 or newer. It uses the Python standard library.

Build the emulator with diagnostics to collect visual cues. Run the following commands from the repository root. Substitute the local executable, database, and game paths.

```powershell
cargo build --profile diag -p victory --features diagnostics
python tools/scene_map.py --database scene-map.sqlite run target/diag/victory.exe GAME.exe --visual
python tools/scene_map.py --database scene-map.sqlite summary
python tools/scene_map.py --database scene-map.sqlite show 12
python tools/scene_map.py --database scene-map.sqlite slots 12
```

The standard release build collects archive and upload cues. Visual mode uses an existing diagnostic readback every eighth present. It adds overhead. Leave visual mode off for production frame timing.

## What the map records

- Each executable digest has its own scene namespace. A texture range includes a package identifier, byte offset, input and output lengths, compression options, and intermediate length. The package identifier includes its logical path, file size, and modification time.
- Archive-region cues identify a recent non-texture read in a 4 MiB archive region. A map-package cue identifies an active map, terrain, or interior package. Several world locations can share a package.
- Visual cues use a small image signature and mean color. A flat frame clears the last visual cue. Nearby frames can share a cue. Similar-looking scenes can also match.
- The mapper records decoded BCPack reads and full indexed uploads that have an existing renderer fingerprint. `show` ranks assets by the number of separate runs and then by observation count. `slots` lists per-run descriptor slots whose size and sampled content match a decoded range. Several ranges can contain identical data. Multi-request textures can lack a matching single range.
- The database records observations while the game runs. It commits at least once per second when events arrive. Each database has a 1 GiB page limit. An optional raw event log uses additional space.
- The map records storage and upload activity. A shader can sample only part of an uploaded texture. The mapper does not infer per-pixel use from an upload.

## Named cutscenes and world coordinates

A game or telemetry provider can update a JSON file atomically during play. Pass its path with `--context context.json`. Refresh the file at least once every five seconds while the values apply. The fields below are optional. The game defines the world units.

```json
{"cutscene":"arrival","cutscene_time_s":4.1,"world":"city","position":[85,13,2],"camera_position":[-12,90,4]}
```

The map records cutscene time in two-second intervals. It assigns player and camera positions to separate 64-unit cells. The tested game supplied package and visual cues during playback. Exact coordinates and game-authored cutscene names require a telemetry provider.

## Test a prediction

Add `--event-log run2.events` during a second run. Use the first run's database ID as the training run. Select the first frame of the scene under test.

```powershell
python tools/scene_map.py --database scene-map.sqlite validate 1 GAME.exe run2.events --after-frame 1300 --limit 16
```

Validation freezes the training run's visual signatures and ranked assets. For each recognized image, it checks requests during the next eight presented frames. It reports hits, predictions, eligible windows, and recognized windows. A global frequency baseline uses the same recognized windows and prediction limit. The report also scores combined scene cues and recent request sequences. It counts first-time requests separately. Use `--frames` with a comma-separated frame list to check particular shots. Each hit counts once per range per window. The mapper does not submit speculative uploads or read game memory to discover player coordinates.


## Compare GPU texture retention

Set `VICTORY_TEXTURE_RETAIN=1` to keep idle GPU copies of guest-filled textures when local memory and system RAM have headroom. The emulator uses its ordinary reclaim policy once free local memory reaches 25% of the budget or 2 GiB, whichever is greater. It also uses ordinary reclaim below 4 GiB of free system RAM, or when a budget query fails. Guest releases, alias handling, and command-list lifetimes still apply. The standard setting uses age-based reclamation.

## Direct texture uploads

Set `VICTORY_GPU_TEXTURE_UPLOAD=1` to test CPU writes to GPU-local staging buffers. VIctory checks D3D12 Options16 and the adapter's local memory budget. If allocation or mapping fails, the emulator uses a system-memory upload buffer. The guest keeps its source bytes in RAM. The GPU copies the staged data into the final texture subresource. CPU reads of GPU-written resources still require synchronization.

The log reports `texture staging uses CPU-visible video memory` when the first allocation succeeds. Compare corresponding visible shots. Changes in overall frame time depend on the scene.
