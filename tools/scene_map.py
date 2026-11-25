"""Record and inspect title-specific associations between scenes and texture data."""

import argparse
from collections import Counter, deque
from contextlib import closing, nullcontext
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

LOG = logging.getLogger("scene_map")
EVENT = re.compile(r"^(\S+).*?victory_scene_map: sm (archive|region|visual|texture|upload) (\d+) (.+)$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, build TEXT NOT NULL, start TEXT NOT NULL, UNIQUE(build,start)
);
CREATE TABLE IF NOT EXISTS archives (
  build TEXT NOT NULL, archive TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(build,archive)
);
CREATE TABLE IF NOT EXISTS successors (
  run INTEGER NOT NULL REFERENCES runs, previous TEXT NOT NULL, next TEXT NOT NULL, hits INTEGER NOT NULL,
  PRIMARY KEY(run,previous,next)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS payloads (
  run INTEGER NOT NULL REFERENCES runs, asset TEXT NOT NULL, fingerprint TEXT NOT NULL,
  PRIMARY KEY(run,asset,fingerprint)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS by_payload ON payloads(run,fingerprint);
CREATE TABLE IF NOT EXISTS scenes (
  id INTEGER PRIMARY KEY, build TEXT NOT NULL, kind TEXT NOT NULL, cue TEXT NOT NULL,
  UNIQUE(build,kind,cue)
);
CREATE TABLE IF NOT EXISTS observations (
  run INTEGER NOT NULL REFERENCES runs, scene INTEGER NOT NULL REFERENCES scenes,
  kind TEXT NOT NULL, asset TEXT NOT NULL, slot INTEGER NOT NULL,
  hits INTEGER NOT NULL, first_frame INTEGER NOT NULL, last_frame INTEGER NOT NULL,
  PRIMARY KEY(run,scene,kind,asset,slot)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS scene_assets ON observations(scene,kind,asset);
"""


def connect(path):
    """Open the local map, with a one GiB limit on database pages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
        db.close()
        raise ValueError("Unsupported scene map version")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    page_size = db.execute("PRAGMA page_size").fetchone()[0]
    db.execute("PRAGMA max_page_count=%d" % ((1 << 30) // page_size))
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version=1")
    return db


def visual_key(bits, rgb, known, learn=True):
    """Select a similar nonuniform view from previous observations."""
    best = None
    for cue, old_bits, old_rgb in known:
        distance = (bits ^ old_bits).bit_count()
        color = max(abs(a - b) for a, b in zip(rgb, old_rgb))
        if distance <= 7 and color <= 16:
            candidate = (distance, color, cue)
            if best is None or candidate < best:
                best = candidate
    if best is not None:
        return best[2]
    if not learn:
        return None
    cue = "%016x/%d/%d/%d" % (bits, *rgb)
    known.append((cue, bits, rgb))
    return cue


def context_cues(path):
    """Read current game context from a telemetry file refreshed at least every five seconds."""
    try:
        if not -1 <= time.time() - path.stat().st_mtime <= 5:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        cues = []
        name = data.get("cutscene")
        if isinstance(name, str) and name.strip():
            name = name.strip()[:160]
            cues.append(("cutscene", name))
            elapsed = data.get("cutscene_time_s")
            if type(elapsed) in (float, int) and math.isfinite(elapsed) and elapsed >= 0:
                cues.append(("cutscene-time", "%s/%d" % (name, int(elapsed // 2))))
        world = data.get("world")
        if isinstance(world, str) and world.strip():
            for field in ("position", "camera_position"):
                pos = data.get(field)
                if isinstance(pos, list) and len(pos) == 3 and all(
                    type(v) in (float, int) and math.isfinite(v) for v in pos
                ):
                    cell = tuple(math.floor(v / 64) for v in pos)
                    cues.append((field, "%s/%d/%d/%d" % (world.strip()[:100], *cell)))
        return cues
    except (OSError, ValueError, OverflowError):
        return []


def is_map_package(path):
    """Identify map, terrain, and interior texture packages by their title-relative path."""
    return any(part in "/" + path for part in ("/ttdb/maps/", "/ttdb/terrain/", "/ttdb/interiors/"))


def remember(recent, asset, frame):
    """Keep at most one recent occurrence of each archive extent."""
    for entry in recent:
        if entry[0] == asset:
            recent.remove(entry)
            break
    recent.append((asset, frame))


class Recorder:
    """Accumulate observations from one execution and commit them each second."""

    def __init__(self, db, build, start, context):
        self.db = db
        self.build = build
        self.context = context
        self.run = db.execute("INSERT INTO runs(build,start) VALUES (?,?) RETURNING id", (build, start)).fetchone()[0]
        self.ids = {}
        self.views = []
        for scene, kind, cue in db.execute("SELECT id,kind,cue FROM scenes WHERE build=? ORDER BY id", (build,)):
            self.ids[kind, cue] = scene
            if kind == "view":
                bits, *colors = cue.split("/")
                self.views.append((cue, int(bits, 16), tuple(map(int, colors))))
        self.packages = {}
        self.cues = {}
        self.rows = {}
        self.recent = deque(maxlen=8)
        self.transitions = Counter()
        self.hits = Counter()
        self.last_flush = time.monotonic()
        self.last_context = -1
        db.commit()

    def set_cue(self, kind, cue, frame):
        """Activate the cue and reuse its title-scoped database ID."""
        key = (kind, cue)
        if key not in self.ids:
            self.db.execute("INSERT OR IGNORE INTO scenes(build,kind,cue) VALUES (?,?,?)", (self.build, *key))
            self.ids[key] = self.db.execute("SELECT id FROM scenes WHERE build=? AND kind=? AND cue=?",
                                            (self.build, *key)).fetchone()[0]
        self.cues[kind] = (self.ids[key], frame)

    def event(self, match):
        """Associate events with earlier cues within a bounded presented-frame interval."""
        _, kind, frame_text, text = match.groups()
        frame = int(frame_text)
        values = text.split()
        self.hits[kind] += 1
        now = time.monotonic()
        if now - self.last_flush >= 1:
            self.flush()
        if self.context and now - self.last_context >= 0.25:
            self.last_context = now
            for key in ("cutscene", "cutscene-time", "position", "camera_position"):
                self.cues.pop(key, None)
            for context in context_cues(self.context):
                self.set_cue(*context, frame)
        if kind == "archive":
            archive, path = text.split(" ", 1)
            self.db.execute("INSERT OR IGNORE INTO archives VALUES (?,?,?)", (self.build, archive, path))
            if is_map_package(path):
                self.packages[archive] = path
            return
        if kind == "region":
            self.set_cue("archive-region", "/".join(values), frame)
            if values[0] in self.packages:
                self.set_cue("map-package", self.packages[values[0]], frame)
            return
        if kind == "visual":
            bits = int(values[0], 16)
            rgb = tuple(map(int, values[1:4]))
            self.cues.pop("view", None)
            if int(values[4]) >= 24 and 4 <= bits.bit_count() <= 60:
                self.set_cue("view", visual_key(bits, rgb, self.views), frame)
            return
        if kind == "upload":
            slot = int(values[0])
            asset = "/".join(values[1:])
        else:
            slot = -1
            asset = "/".join(values[:6])
            self.transitions.update((old, asset) for old, seen in self.recent if old != asset and 0 <= frame - seen <= 8)
            remember(self.recent, asset, frame)
            if len(values) == 8:
                self.db.execute("INSERT OR IGNORE INTO payloads VALUES (?,?,?)",
                                (self.run, asset, "/".join(values[6:])))
        for source, (scene, since) in self.cues.items():
            if not 0 <= frame - since <= (8 if source == "view" else 16):
                continue
            key = (scene, kind, asset, slot)
            old = self.rows.get(key)
            self.rows[key] = (old[0] + 1, min(frame, old[1]), max(frame, old[2])) if old else (1, frame, frame)
        if kind == "texture" and values[0] in self.packages:
            self.set_cue("map-package", self.packages[values[0]], frame)
        if len(self.rows) >= 8192:
            self.flush()

    def flush(self):
        """Commit completed batches so a title crash retains earlier observations."""
        self.db.executemany("""
            INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(run,scene,kind,asset,slot) DO UPDATE SET
              hits=hits+excluded.hits,first_frame=min(first_frame,excluded.first_frame),
              last_frame=max(last_frame,excluded.last_frame)
        """, ((self.run, *key, *stats) for key, stats in self.rows.items()))
        self.db.executemany("""
            INSERT INTO successors VALUES (?,?,?,?) ON CONFLICT(run,previous,next)
            DO UPDATE SET hits=hits+excluded.hits
        """, ((self.run, old, new, count) for (old, new), count in self.transitions.items()))
        self.db.commit()
        self.transitions.clear()
        self.rows.clear()
        self.last_flush = time.monotonic()


def consume(db, game, lines, context, echo, event_log=None, expect_visual=False):
    """Attach one event stream to the executable digest and the first event timestamp."""
    with game.open("rb") as source:
        build = hashlib.file_digest(source, "sha256").hexdigest()
    recorder = None
    if event_log is not None:
        event_log.write("# victory-scene-map-v1 %s\n" % build)
    try:
        for line in lines:
            if line.startswith("# victory-scene-map-v1 ") and line.split()[-1] != build:
                raise ValueError("This event log belongs to a different game build")
            match = EVENT.match(line.rstrip())
            if not match:
                if echo:
                    sys.stderr.write(line)
                continue
            if event_log is not None:
                event_log.write(line)
            if recorder is None:
                start = match[1]
                if db.execute("SELECT 1 FROM runs WHERE build=? AND start=?", (build, start)).fetchone():
                    raise ValueError("This recording already exists in the map")
                recorder = Recorder(db, build, start, context)
            recorder.event(match)
    finally:
        if recorder is not None:
            recorder.flush()
            LOG.info("Run %d recorded %s", recorder.run, dict(recorder.hits))
    if recorder is None:
        raise ValueError("No scene map events reached the collector")
    if expect_visual and not recorder.hits["visual"]:
        LOG.warning("The emulator supplied no visual samples. Check the diagnostic build and the presentation format.")


def validate(db, training_run, game, log, after_frame, limit, selected=None):
    """Test prior-run predictions against the following eight frames of an independent replay."""
    with game.open("rb") as source:
        build = hashlib.file_digest(source, "sha256").hexdigest()
    train = db.execute("SELECT start FROM runs WHERE id=? AND build=?", (training_run, build)).fetchone()
    if not train or limit < 1:
        raise ValueError("Select a training run for this build and a positive prediction limit")
    rankings = {}
    known = []
    for kind, cue, asset in db.execute("""
        SELECT kind,cue,asset FROM (
          SELECT s.kind,s.cue,o.asset,row_number() OVER
            (PARTITION BY s.id ORDER BY o.hits DESC,o.asset) AS rank
          FROM observations o JOIN scenes s ON s.id=o.scene WHERE o.run=? AND o.kind='texture'
        ) WHERE rank<=? ORDER BY kind,cue,rank
    """, (training_run, limit)):
        ranking = rankings.setdefault((kind, cue), [])
        if kind == "view" and not ranking:
            bits, *colors = cue.split("/")
            known.append((cue, int(bits, 16), tuple(map(int, colors))))
        ranking.append(asset)
    global_top = {row[0] for row in db.execute("""
        SELECT o.asset FROM observations o JOIN scenes s ON o.scene=s.id
        WHERE o.run=? AND s.kind='archive-region' AND o.kind='texture'
        GROUP BY o.asset ORDER BY sum(o.hits) DESC,o.asset LIMIT ?
    """, (training_run, limit))}
    successors = {}
    for old, new, count in db.execute("""
        SELECT previous,next,hits FROM successors WHERE run=? ORDER BY previous,hits DESC,next
    """, (training_run,)):
        future = successors.setdefault(old, [])
        if len(future) < limit:
            future.append((new, count))
    recent = deque(maxlen=8)
    first_read = {}
    windows = []
    reads = {}
    active = {}
    packages = {}
    last_frame = 0
    first = True
    with log.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if line.startswith("# victory-scene-map-v1 ") and line.split()[-1] != build:
                raise ValueError("This event log belongs to a different game build")
            match = EVENT.match(line.rstrip())
            if not match:
                continue
            stamp, kind, number, text = match.groups()
            if first and stamp == train[0]:
                raise ValueError("Select a different replay for validation")
            first = False
            frame = int(number)
            last_frame = max(frame, last_frame)
            values = text.split()
            if kind == "archive":
                archive, path = text.split(" ", 1)
                if is_map_package(path):
                    packages[archive] = path
            elif kind in ("region", "texture"):
                if values[0] in packages:
                    active["map-package"] = (packages[values[0]], frame)
                if kind == "region":
                    active["archive-region"] = ("/".join(values), frame)
                else:
                    asset = "/".join(values[:6])
                    remember(recent, asset, frame)
                    first_read[asset] = min(frame, first_read.get(asset, frame))
                    if frame >= after_frame:
                        reads.setdefault(frame, set()).add(asset)
            elif kind == "visual" and frame >= after_frame and (selected is None or frame in selected):
                bits, r, g, b, spread = values
                bits = int(bits, 16)
                if int(spread) < 24 or not 4 <= bits.bit_count() <= 60:
                    continue
                cue = visual_key(bits, (int(r), int(g), int(b)), known, learn=False)
                view = rankings.get(("view", cue), [])
                scores = Counter()
                cues = [(key, name) for key, (name, seen) in active.items() if 0 <= frame - seen <= 16]
                if cue is not None:
                    cues.append(("view", cue))
                for key in cues:
                    for rank, asset in enumerate(rankings.get(key, ()), 1):
                        scores[asset] += 1 / rank
                combined = {item for item, _ in scores.most_common(limit)}
                stream_scores = Counter()
                for age, (asset, seen) in enumerate(reversed(recent), 1):
                    future = successors.get(asset, [])
                    if 0 <= frame - seen <= 8 and future:
                        total = sum(count for _, count in future)
                        for candidate, count in future:
                            stream_scores[candidate] += count / (total * age)
                stream = {item for item, _ in stream_scores.most_common(limit)}
                windows.append((frame, set(view), combined, stream))
    counts = Counter()
    combined_counts = Counter()
    stream_counts = Counter()
    for frame, view, combined, stream in windows:
        if frame + 8 > last_frame:
            continue
        actual = set().union(*(reads.get(n, set()) for n in range(frame + 1, frame + 9)))
        first_time = {asset for asset in actual if first_read[asset] > frame}
        for stats, predicted in ((counts, view), (combined_counts, combined), (stream_counts, stream)):
            stats["eligible_windows"] += 1
            if predicted:
                stats["recognized_windows"] += 1
                stats["actual_ranges_in_recognized_windows"] += len(actual)
                stats["predictions"] += len(predicted)
                stats["hits"] += len(predicted & actual)
                stats["first_time_actual"] += len(first_time)
                stats["first_time_hits"] += len(first_time & predicted)
                stats["global_first_time_hits"] += len(first_time & global_top)
                stats["global_predictions"] += len(global_top)
                stats["global_hits"] += len(global_top & actual)
    print(json.dumps({"visual": counts, "combined": combined_counts, "recent_extents": stream_counts}, sort_keys=True))


def main():
    """Run the live collector, import a prior trace, or inspect learned associations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("emulator", type=Path)
    run.add_argument("game", type=Path)
    run.add_argument("--visual", action="store_true", help="Sample presented images with a diagnostic build")
    run.add_argument("--event-log", type=Path, help="Write a new raw event file for later validation")
    run.add_argument("--context", type=Path, help="Live JSON with a cutscene name or a world position")
    load = commands.add_parser("import-log")
    load.add_argument("game", type=Path)
    load.add_argument("log", type=Path)
    commands.add_parser("summary")
    check = commands.add_parser("validate")
    check.add_argument("training_run", type=int)
    check.add_argument("game", type=Path)
    check.add_argument("log", type=Path)
    check.add_argument("--after-frame", type=int, default=0)
    check.add_argument("--frames", help="Comma-separated presented frames selected for inspection")
    check.add_argument("--limit", type=int, default=16)
    show = commands.add_parser("show")
    show.add_argument("scene", type=int)
    show.add_argument("--limit", type=int, default=25)
    slots = commands.add_parser("slots")
    slots.add_argument("scene", type=int)
    slots.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with closing(connect(args.database)) as db:
        if args.command == "run":
            env = os.environ.copy()
            env["VICTORY_SCENE_MAP"] = "1"
            env["VICTORY_SCENE_VISUAL"] = "1" if args.visual else "0"
            env["VICTORY_LOG"] = env.get("VICTORY_LOG", "warn") + ",victory_scene_map=info"
            command = [str(args.emulator.resolve()), "run", str(args.game.resolve()), "--execute", "--console", "series-x"]
            trace = args.event_log.open("x", encoding="utf-8") if args.event_log else nullcontext(None)
            with trace as event_log, subprocess.Popen(command, env=env, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") as child:
                try:
                    if child.stderr is not None:
                        consume(db, args.game, child.stderr, args.context, True, event_log, args.visual)
                except (OSError, ValueError, sqlite3.Error) as error:
                    LOG.error("Scene recording stopped. Game output will continue. %s", error)
                    if child.stderr is not None:
                        for line in child.stderr:
                            if not EVENT.match(line):
                                sys.stderr.write(line)
                    return child.wait() or 1
                except BaseException:
                    if child.poll() is None:
                        child.terminate()
                    raise
                return child.wait()
        if args.command == "import-log":
            with args.log.open(encoding="utf-8", errors="replace") as source:
                consume(db, args.game, source, None, False)
        elif args.command == "validate":
            frames = {int(n) for n in args.frames.split(",")} if args.frames else None
            validate(db, args.training_run, args.game, args.log, args.after_frame, args.limit, frames)
        elif args.command == "summary":
            print("scene_id\tkind\tcue\truns\tdistinct_assets")
            for row in db.execute("""
                SELECT s.id,s.kind,s.cue,count(distinct o.run),count(distinct o.asset)
                FROM scenes s JOIN observations o ON o.scene=s.id GROUP BY s.id
                ORDER BY 4 DESC,5 DESC,s.id
            """):
                print(*row, sep="\t")
        elif args.command == "slots":
            for row in db.execute("""
                SELECT DISTINCT o.run,o.slot,p.asset,o.asset FROM observations o JOIN payloads p
                ON p.run=o.run AND p.fingerprint=o.asset WHERE o.scene=? AND o.kind='upload'
                ORDER BY o.run,o.slot,p.asset LIMIT ?
            """, (args.scene, max(1, args.limit))):
                print(*row, sep="\t")
        elif args.command == "show":
            for row in db.execute("""
                SELECT kind,asset,count(distinct run),sum(hits) FROM observations WHERE scene=?
                GROUP BY kind,asset ORDER BY 3 DESC,4 DESC,asset LIMIT ?
            """, (args.scene, max(1, args.limit))):
                print(*row, sep="\t")
    return 0


if __name__ == "__main__":
    sys.exit(main())
