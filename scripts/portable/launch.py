"""Prepare a local VIctory cache and start the selected game."""
import argparse
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys

BIN = Path(__file__).resolve().parent
ROOT = BIN.parent
EMU = BIN / "victory.exe"
SDK = BIN / "dxc"


def shader_inputs(game):
    """Find shader inputs beside the selected game executable."""
    shaders = [game]
    extensions = {".dll", ".sgd5", ".cso", ".dxbc", ".dxil", ".shaderpack"}
    for directory, subdirs, names in os.walk(game.parent, onerror=raise_io):
        subdirs[:] = [name for name in subdirs if Path(directory) / name not in {ROOT / "cache", BIN, ROOT}]
        for name in names:
            path = Path(directory) / name
            if path == game:
                continue
            if path.suffix.lower() in extensions or "shader" in name.lower():
                if path.stat().st_size <= 2 << 30:
                    shaders.append(path)
    return sorted(shaders)


def raise_io(error):
    raise error


def shader_outputs(runtime):
    """Inventory the content-addressed shader records and the host pipeline library."""
    files = {}
    pending = [runtime / "shaders"]
    while pending:
        directory = pending.pop()
        prefix = str(directory.relative_to(runtime))
        with os.scandir(directory) as entries:
            for item in entries:
                if item.is_dir(follow_symlinks=False):
                    pending.append(Path(item.path))
                elif len(item.name) == 62:
                    files[os.path.join(prefix, item.name)] = item.stat().st_size
    library = runtime / "pipelines/host-pipelines.bin"
    if library.is_file() and library.stat().st_size:
        files[str(library.relative_to(runtime))] = -1
    return files


def prepare_shaders(inputs, game, runtime, env):
    """Compile and optimize shader inputs when their saved receipt is incomplete."""
    receipt = runtime.parent / "shader-receipt.json"
    tools = [EMU, SDK / "dxcompiler.dll", SDK / "dxil.dll"]
    signature = [[p.name, p.stat().st_size, p.stat().st_mtime_ns] for p in tools]
    signature += [[str(p.relative_to(game.parent)), p.stat().st_size, p.stat().st_mtime_ns] for p in inputs]
    try:
        old = json.loads(receipt.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        old = {}
    if isinstance(old, dict) and old.get("version") == 1 and old.get("inputs") == signature:
        actual = shader_outputs(runtime) if (runtime / "shaders").is_dir() else {}
        expected = old.get("outputs")
        if isinstance(expected, dict) and all(actual.get(k) == v for k, v in expected.items()):
            print("Offline shaders ready.", flush=True)
            return
    (runtime / "shaders").mkdir(parents=True, exist_ok=True)
    flags = ["--cache", "--pipelines"]
    if (runtime / "pipelines/early-pipelines.txt").is_file():
        flags.append("--used-pipelines")
    log = runtime.parent / "shader-prepare.log"
    print("Compiling and optimizing offline shaders. Log: %s" % log, flush=True)
    with log.open("w", encoding="utf-8") as output:
        targets = inputs if len(subprocess.list2cmdline([str(p) for p in inputs])) < 28000 else [game.parent]
        for args in (["translate", *flags, *targets], ["optimize-shaders", "--sdk", SDK]):
            subprocess.run([EMU, *args], env=env, cwd=runtime.parent, stdout=output,
                           stderr=subprocess.STDOUT, check=True)
    state = {"version": 1, "inputs": signature, "outputs": shader_outputs(runtime)}
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    temporary.replace(receipt)
    print("Offline shader preparation complete.", flush=True)


def main():
    """Prompt for the executable, prepare its cache, and start the emulator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", nargs="?", help="Path to the game executable")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare the cache and exit")
    args = parser.parse_args()
    value = args.game if args.game is not None else input("Game executable (paste its full path): ")
    game = Path(value.strip().strip('"')).expanduser().resolve()
    if game.suffix.lower() != ".exe" or not game.is_file():
        raise ValueError("Select an existing game .exe file.")
    for binary in (EMU, SDK / "dxcompiler.dll", SDK / "dxil.dll"):
        if not binary.is_file():
            raise OSError("Missing bundled file: %s. Extract the complete package." % binary)
    with game.open("rb") as stream:
        key = hashlib.file_digest(stream, "sha256").hexdigest()[:16]
    cache = ROOT / "cache" / ("%s-%s" % (game.stem, key))
    runtime = cache / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    temp = cache / "temp"
    temp.mkdir(exist_ok=True)
    data = cache / "data"
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), VICTORY_GAME_DIR=str(game.parent), VICTORY_DATA_DIR=str(data),
               VICTORY_CACHE_DIR=str(runtime),
               VICTORY_DXIL=str(SDK / "dxil.dll"), VICTORY_LLVM_SHADERS="1", VICTORY_RELAY="2",
               VICTORY_TEXTURE_STAGE_MS="24", VICTORY_TEXTURE_STAGE_MB="32", VICTORY_TEXTURE_CATCHUP="0",
               VICTORY_CARD_BUFFERS_MIN_MB="1", VICTORY_CARD_BUFFERS_MB="2048",
               VICTORY_CARD_WINDOWS_MB="1024", VICTORY_CARD_REHASH_RUNS="1", VICTORY_MEMORY_MAP="0")
    for key in ("VICTORY_KEYS", "VICTORY_CAPTURE", "VICTORY_CAPTURE_FRAMES", "VICTORY_DUMP_TARGETS"):
        env.pop(key, None)
    print("Cache: %s" % cache, flush=True)
    with (cache / "launch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise OSError("Another launcher is using this game's cache.") from error
        prepare_shaders(shader_inputs(game), game, runtime, env)
        if args.prepare_only:
            return 0
        print("Starting %s" % game.name, flush=True)
        return subprocess.run([EMU, "run", game, "--execute"], env=env, cwd=game.parent).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, ImportError, EOFError, subprocess.CalledProcessError) as error:
        print("Launch failed: %s" % error, file=sys.stderr)
        sys.exit(1)
