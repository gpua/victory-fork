"""Build the Windows x64 release and assemble the portable launcher."""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-pc-windows-msvc"
EMBED = "python-3.12.10-embed-amd64.zip"
EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
CRT = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll")
DXC = ("dxcompiler.dll", "dxil.dll")
PYTHON = ("python.exe", "python3.dll", "python312.dll", "python312.zip",
          "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")
EXTENSIONS = ("_bz2.pyd", "_hashlib.pyd", "_lzma.pyd", "libcrypto-3.dll", "select.pyd", "unicodedata.pyd")


def runtime_dir(override, variable, pattern, names):
    """Locate one complete set of redistributable libraries."""
    if override:
        candidates = [override]
    elif os.environ.get(variable):
        candidates = sorted(Path(os.environ[variable]).glob(pattern), reverse=True)
    elif variable == "VCToolsRedistDir" and os.environ.get("VCToolsInstallDir"):
        tools = Path(os.environ["VCToolsInstallDir"])
        version = ".".join(tools.name.split(".")[:2])
        redist = tools.parent.parent.parent / "Redist" / "MSVC"
        candidates = sorted(redist.glob(f"{version}.*/{pattern}"), reverse=True)
    else:
        candidates = []
    for path in candidates:
        if all((path / name).is_file() for name in names):
            return path
    raise ValueError(f"Cannot locate {names[0]}. Use an x64 MSVC developer prompt or supply its directory.")


def build(target_dir):
    """Build the release binary with portable source paths."""
    env = os.environ.copy()
    home = Path.home()
    sysroot = subprocess.check_output(["rustc", "--print", "sysroot"], cwd=ROOT, text=True).strip()
    roots = {
        home: "/home/builder",
        ROOT: "/src/victory",
        Path(env.get("CARGO_HOME", home / ".cargo")): "/cargo",
        Path(sysroot): "/toolchain",
        target_dir: "/build",
        Path(tempfile.gettempdir()): "/tmp",
    }
    flags = []
    for path, alias in sorted(roots.items(), key=lambda entry: len(str(entry[0]))):
        flags.extend(["--remap-path-prefix", f"{path.resolve()}={alias}"])
    env.update(CARGO_ENCODED_RUSTFLAGS="\x1f".join(flags), CARGO_PROFILE_AOT_DEBUG="0",
               CARGO_PROFILE_AOT_STRIP="symbols")
    subprocess.run(["cargo", "build", "--locked", "--profile", "aot", "--target", TARGET,
                    "--target-dir", str(target_dir), "-p", "victory"], cwd=ROOT, env=env, check=True)
    exe = target_dir / TARGET / "aot" / "victory.exe"
    return exe, roots


def python_archive(override, work):
    """Obtain the pinned Python distribution and verify it before use."""
    path = override or work / EMBED
    if not override:
        subprocess.run(["curl.exe", "--fail", "--location", "--silent", "--show-error",
                        "--output", str(path), f"https://www.python.org/ftp/python/3.12.10/{EMBED}"], check=True)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != EMBED_SHA256:
            raise ValueError(f"Expected the official {EMBED}. The SHA-256 check failed.")
    return path


def check_payload(name, data, private_paths):
    """Reject local build paths and native binaries for another architecture."""
    if Path(name).suffix.lower() in {".dll", ".pyd", ".exe"}:
        pe = int.from_bytes(data[60:64], "little")
        if data[:2] != b"MZ" or data[pe:pe + 6] != b"PE\0\0\x64\x86":
            raise ValueError(f"Expected a Windows x64 binary for {name}.")
    lowered = data.lower()
    for path in private_paths:
        for spelling in (str(path.resolve()), path.resolve().as_posix()):
            if any(spelling.encode(codec).lower() in lowered for codec in ("utf-8", "utf-16le")):
                raise ValueError(f"A local build path remains in {name}.")


def main():
    """Build, verify, and publish the requested ZIP on the local filesystem."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-embed", type=Path, help="Local copy of the pinned Python embeddable ZIP")
    parser.add_argument("--dxc-dir", type=Path, help="Windows SDK directory containing the x64 DXC DLLs")
    parser.add_argument("--crt-dir", type=Path, help="MSVC directory containing the x64 redistributable CRT")
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "VIctory-Portable.zip")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info < (3, 11):
        parser.error("Run with Python 3.11 or later on Windows.")
    output = args.output.resolve()
    if output.suffix.lower() != ".zip" or (args.python_embed and args.python_embed.resolve() == output):
        parser.error("Select a separate output ZIP.")
    dxc = runtime_dir(args.dxc_dir, "WindowsSdkDir", "Redist/D3D/x64", DXC)
    crt = runtime_dir(args.crt_dir, "VCToolsRedistDir", "x64/Microsoft.VC*.CRT", CRT)
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target")).resolve()
    exe, roots = build(target_dir)
    portable = ROOT / "scripts" / "portable"
    files = {"bin/victory.exe": exe, "bin/launch.py": portable / "launch.py", "LICENSE": ROOT / "LICENSE",
             "Start-VIctory.cmd": portable / "Start-VIctory.cmd", "README.md": portable / "README.md"}
    files.update({f"bin/{name}": crt / name for name in CRT})
    files.update({f"bin/dxc/{name}": dxc / name for name in DXC})
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as stage:
        work = Path(stage)
        archive = work / "portable.zip"
        with zipfile.ZipFile(python_archive(args.python_embed, work)) as runtime:
            payload = {name: path.read_bytes() for name, path in files.items()}
            payload.update({f"bin/python/{name}": runtime.read(name) for name in PYTHON})
            payload.update({f"bin/python/DLLs/{name}": runtime.read(name) for name in EXTENSIONS})
        payload["bin/python/python312._pth"] = b"python312.zip\r\nDLLs\r\n.\r\n..\r\n"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for name, data in sorted(payload.items()):
                check_payload(name, data, roots)
                entry = zipfile.ZipInfo(name)
                entry.create_system = 0
                package.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with zipfile.ZipFile(archive) as package:
            if package.testzip() is not None:
                raise ValueError("The output ZIP failed the integrity check.")
        archive.replace(output)
    print(output)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        sys.exit(str(error))
