"""Check cross-run scene mapping with relocatable texture descriptors."""

import json
import os
from pathlib import Path
import tempfile
import unittest

import scene_map


class SceneMapTest(unittest.TestCase):
    def test_two_runs_keep_slots_local_and_reject_blank_frames(self):
        """The same payload can occupy different slots in two executions."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = root / "game"
            game.write_bytes(b"build")
            db = scene_map.connect(root / "map.sqlite")
            self.addCleanup(db.close)
            for run, slot in enumerate((11, 92)):
                events = [
                    "archive 0 ab data/ttdb/maps/map.ttpf",
                    "region 7 ab 16",
                    "visual 8 ffff0000ffff0000 25 26 27 51",
                    "texture 9 ab 100 32 64 2 0 1234 64",
                    "upload 10 %d 1234 64" % slot,
                    "visual 16 0000000000000000 0 0 0 0",
                    "upload 17 64 9999 512",
                ]
                lines = ["run%d INFO victory_scene_map: sm %s\n" % (run, value) for value in events]
                scene_map.consume(db, game, lines, None, False)
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
            pairs = db.execute("""
                SELECT DISTINCT o.run,o.slot FROM observations o JOIN scenes s ON s.id=o.scene
                JOIN payloads p ON (p.run,p.fingerprint)=(o.run,o.asset)
                WHERE s.kind='view' AND o.kind='upload' ORDER BY o.run
            """).fetchall()
            self.assertEqual(pairs, [(1, 11), (2, 92)])
            self.assertEqual(db.execute("""
                SELECT count(*) FROM observations o JOIN scenes s ON s.id=o.scene
                WHERE s.kind='view' AND o.asset='9999/512'
            """).fetchone()[0], 0)
            with self.assertRaisesRegex(ValueError, "already exists"):
                scene_map.consume(db, game, lines, None, False)
            log = root / "events"
            log.write_text("".join(lines), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different replay"):
                scene_map.validate(db, 2, game, log, 0, 16)
            db.close()

    def test_cutscene_and_camera_are_independent(self):
        """The context can identify a camera cut separately from the player."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "context.json"
            path.write_text(json.dumps({"cutscene": "arrival", "cutscene_time_s": 4.1, "world": "city",
                                        "position": [-1, 0, 0], "camera_position": [128, 0, 0]}), encoding="utf-8")
            cues = dict(scene_map.context_cues(path))
            self.assertEqual(cues, {"cutscene": "arrival", "cutscene-time": "arrival/2",
                                    "position": "city/-1/0/0", "camera_position": "city/2/0/0"})
            os.utime(path, (1, 1))
            self.assertEqual(scene_map.context_cues(path), [])

    def test_visual_validation_does_not_train_on_unknown_images(self):
        """A held-out image cannot change the training dictionary."""
        views = []
        scene_map.visual_key(0x12345678, (32, 64, 96), views)
        self.assertIsNone(scene_map.visual_key(0xff00ff00, (250, 245, 2), views, learn=False))
        self.assertEqual(len(views), 1)


if __name__ == "__main__":
    unittest.main()
