# VIctory portable release for Windows x64

Use an NVIDIA GPU with Direct3D 12 Shader Model 6.6 support. Extract the entire ZIP to a writable folder. The package includes the emulator, the Windows shader compiler, and the launcher's Python runtime.

1. Open `Start-VIctory.cmd`.
2. Paste the path to the game's original `.exe` and press Enter.
3. Leave the window open while VIctory prepares the shaders and starts the game.

VIctory reads textures, meshes, and other assets from the installed game. Keep the executable beside its game data.

The launcher stores shaders, pipelines, the preparation log, and temporary files under `cache\<game>-<id>\` beside the batch file. The `data` subfolder holds saves. To transfer saves from an earlier release, close both launchers and copy that game's `data` subfolder into its matching cache folder.

Use `Start-VIctory.cmd --prepare-only` to prepare shaders without starting the game. Later launches check the cache and rebuild missing shader records. The preparation log lists unsupported shaders.

By default the game supplies indirect light. Set `VICTORY_AMBIENT=0.6` before launch to test Belobug's experimental uniform light.
