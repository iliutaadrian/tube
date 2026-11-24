"""In-process tests: no sockets, no network. Run: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import tube  # noqa: E402

TOKEN = "test-token"
CHANNEL = "UC" + "a" * 22


def vid(i):
    return f"sub{i:08d}"


def hit(prefix, i):
    return {"vid": f"{prefix}{i:08d}"[:11], "title": f"{prefix} video {i}", "channel": "Maker", "channel_id": "UCmaker",
            "duration": 600.0, "views": 1000 + i}


class TubeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.uploads = [{"vid": vid(i), "title": f"Upload {i}", "duration": 600.0} for i in range(5)]
        self.now = int(time.time())
        self.feed = {vid(0): (self.now - 3600, 10), vid(1): (self.now - 7200, 20), "short000001": (self.now, 5)}
        self.searches = []
        self.results = {"jigs": [hit("jig", i) for i in range(3)], "data": [hit("dat", i) for i in range(2)]}

        def search(query, limit):
            self.searches.append(query)
            if query not in self.results:
                raise RuntimeError("search exploded")
            return self.results[query][:limit]
        self.app = tube.App(
            {"data_dir": self.tmp.name, "token": TOKEN},
            list_channel=lambda url, limit: {"id": CHANNEL, "name": "Test Channel",
                                             "entries": self.uploads[:limit] if limit else self.uploads},
            rss=lambda channel_id: self.feed, search=search)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, q=None, body=None, token=TOKEN):
        return self.app.api(method, path, q or {}, body or {}, token)

    def ok(self, *args, **kwargs):
        status, out = self.call(*args, **kwargs)
        self.assertEqual(status, 200, out)
        return out

    def test_token_is_required(self):
        self.assertEqual(self.call("GET", "/api/home", token="")[0], 401)
        self.assertEqual(self.call("GET", "/api/home", token="wrong")[0], 401)

    def test_subscribe_dates_and_order(self):
        out = self.ok("POST", "/api/channels", body={"url": "@testchannel"})
        self.assertEqual(out["videos"], 5)
        feed = self.ok("GET", "/api/subs")["videos"]
        self.assertEqual([c["vid"] for c in feed], [vid(0), vid(1)], "main feed only shows exactly dated uploads")
        self.assertEqual(feed[0]["published"], self.now - 3600)
        channel = self.ok("GET", "/api/subs", {"channel": CHANNEL})["videos"]
        self.assertEqual([c["vid"] for c in channel], [vid(i) for i in range(5)], "channel page keeps upload order")
        self.assertIsNone(channel[4]["published"], "estimated dates are not shown")
        self.assertNotIn("short000001", [c["vid"] for c in channel], "Shorts in the feed are ignored")

    def test_refresh_adds_new_upload_and_keeps_order(self):
        self.ok("POST", "/api/channels", body={"url": CHANNEL})
        self.uploads.insert(0, {"vid": "subnew00001", "title": "Brand new", "duration": 300.0})
        self.feed["subnew00001"] = (self.now - 60, 1)
        self.assertIsNone(self.app.refresh_channel(CHANNEL))
        channel = self.ok("GET", "/api/subs", {"channel": CHANNEL})["videos"]
        self.assertEqual([c["vid"] for c in channel], ["subnew00001"] + [vid(i) for i in range(5)])

    def test_refresh_failure_is_recorded_not_raised(self):
        self.ok("POST", "/api/channels", body={"url": CHANNEL})

        def boom(url, limit):
            raise RuntimeError("yt-dlp exploded")
        self.app.list_channel = boom
        self.assertIn("exploded", self.app.refresh_channel(CHANNEL))
        channels = self.ok("GET", "/api/channels")["channels"]
        self.assertIn("exploded", channels[0]["error"])
        self.assertEqual(channels[0]["videos"], 5, "old videos survive a failed refresh")

    def test_bad_channel_input_is_rejected(self):
        for bad in ("", "--exec=rm", "https://evil.example/@x", "file:///etc/passwd"):
            self.assertEqual(self.call("POST", "/api/channels", body={"url": bad})[0], 400, bad)

    def test_unsubscribe_removes_videos(self):
        self.ok("POST", "/api/channels", body={"url": CHANNEL})
        self.ok("DELETE", f"/api/channels/{CHANNEL}")
        self.assertEqual(self.ok("GET", "/api/subs", {"channel": CHANNEL})["videos"], [])
        self.assertEqual(self.call("DELETE", f"/api/channels/{CHANNEL}")[0], 404)

    def test_interests_feed_takes_turns_and_drops_watched(self):
        jigs = self.ok("POST", "/api/interests", body={"name": "  jigs "})
        self.assertEqual((jigs["name"], jigs["error"]), ("jigs", None))
        self.ok("POST", "/api/interests", body={"name": "data"})
        for bad in ("", "x", "JIGS", 5):
            self.assertEqual(self.call("POST", "/api/interests", body={"name": bad})[0], 400, bad)
        feed = [c["vid"] for c in self.ok("GET", "/api/foryou")["videos"]]
        self.assertEqual(feed, ["jig00000000", "dat00000000", "jig00000001", "dat00000001", "jig00000002"])
        self.assertEqual(self.ok("GET", "/api/foryou")["videos"][0]["source"], "yt")
        only = self.ok("GET", "/api/foryou", {"interest": str(jigs["id"])})["videos"]
        self.assertEqual([c["vid"] for c in only], ["jig00000000", "jig00000001", "jig00000002"])
        self.ok("POST", "/api/state", body={"vid": "jig00000000", "watched": True})
        self.ok("POST", "/api/state", body={"vid": "dat00000000", "hidden": True})
        shelves = {s["id"]: s for s in self.ok("GET", "/api/home")["shelves"]}
        self.assertEqual([c["vid"] for c in shelves["foryou"]["videos"]], ["jig00000001", "dat00000001", "jig00000002"])
        related = self.ok("GET", "/api/video/jig00000001")["related"]
        self.assertEqual([c["vid"] for c in related], ["jig00000000", "jig00000002"])
        self.assertEqual({i["name"]: i["videos"] for i in self.ok("GET", "/api/interests")["interests"]},
                         {"data": 2, "jigs": 3})
        self.ok("DELETE", f"/api/interests/{jigs['id']}")
        self.assertEqual([c["vid"] for c in self.ok("GET", "/api/foryou")["videos"]], ["dat00000001"])
        self.assertEqual(self.call("DELETE", f"/api/interests/{jigs['id']}")[0], 404)

    def test_interest_refresh_replaces_results_and_survives_failure(self):
        jigs = self.ok("POST", "/api/interests", body={"name": "jigs"})
        self.app.refresh_interests()
        self.assertEqual(self.searches, ["jigs"], "a fresh interest is not searched again")
        self.results["jigs"] = [hit("new", 0), hit("jig", 1)]
        self.assertIsNone(self.app.refresh_interest(jigs["id"]))
        self.assertEqual([c["vid"] for c in self.ok("GET", "/api/foryou")["videos"]], ["new00000000", "jig00000001"])
        del self.results["jigs"]
        self.assertIn("exploded", self.app.refresh_interest(jigs["id"]))
        self.assertEqual(len(self.ok("GET", "/api/foryou")["videos"]), 2, "old results survive a failed search")
        self.assertIn("exploded", self.ok("GET", "/api/interests")["interests"][0]["error"])
        broken = self.ok("POST", "/api/interests", body={"name": "nothing"})
        self.assertIn("exploded", broken["error"])

    def test_search_is_live_and_results_are_playable(self):
        self.ok("POST", "/api/channels", body={"url": CHANNEL})
        self.results["Upload 1"] = [hit("yts", 0)]
        out = self.ok("GET", "/api/search", {"q": "  Upload 1 "})
        self.assertEqual([c["vid"] for c in out["subs"]], [vid(1)])
        self.assertEqual([c["vid"] for c in out["youtube"]], ["yts00000000"])
        self.assertEqual(self.ok("GET", "/api/video/yts00000000")["video"]["channel"], "Maker")
        failed = self.ok("GET", "/api/search", {"q": "100%_\\"})
        self.assertEqual((failed["subs"], failed["youtube"]), ([], []))
        self.assertIn("exploded", failed["error"])
        self.assertEqual(self.ok("GET", "/api/search", {"q": " "})["youtube"], [])
        self.assertEqual(self.call("GET", "/api/video/xxxxxxxxxxx")[0], 404)

    def test_state_round_trip_and_lists(self):
        self.ok("POST", "/api/channels", body={"url": CHANNEL})
        self.ok("POST", "/api/interests", body={"name": "jigs"})
        self.ok("POST", "/api/state", body={"vid": vid(0), "later": True})
        self.ok("POST", "/api/state", body={"vid": "jig00000000", "liked": True, "progress": 120, "duration": 600})
        later = self.ok("GET", "/api/list", {"kind": "later"})["videos"]
        self.assertEqual([(c["vid"], c["source"], c["later"]) for c in later], [(vid(0), "sub", True)])
        liked = self.ok("GET", "/api/list", {"kind": "liked"})["videos"]
        self.assertEqual([(c["vid"], c["source"], c["progress"]) for c in liked], [("jig00000000", "yt", 120.0)])
        self.ok("POST", "/api/state", body={"vid": vid(0), "later": False, "watched": True})
        self.assertEqual(self.ok("GET", "/api/list", {"kind": "later"})["videos"], [])
        self.assertEqual({c["vid"] for c in self.ok("GET", "/api/list", {"kind": "history"})["videos"]},
                         {vid(0), "jig00000000"})
        shelves = {s["id"]: s for s in self.ok("GET", "/api/home")["shelves"]}
        self.assertEqual([c["vid"] for c in shelves["subs"]["videos"]], [vid(1)], "watched uploads leave the shelf")
        self.assertEqual(self.call("POST", "/api/state", body={"vid": "nope"})[0], 400)
        self.assertEqual(self.call("GET", "/api/list", {"kind": "x"})[0], 400)

    def test_download_queue(self):
        self.ok("POST", "/api/interests", body={"name": "jigs"})
        video = "jig00000000"
        self.assertEqual(self.call("POST", "/api/download", body={"vid": video})[0], 400, "off by default")
        self.assertFalse(self.ok("GET", f"/api/video/{video}")["download"]["enabled"])
        self.app.download_dir = self.tmp.name
        target = os.path.join(self.tmp.name, "Maker jig video 0.mp4")
        calls = []

        def fake(v):
            calls.append(v)
            if len(calls) == 1:
                raise RuntimeError("network down")
            open(target, "w").close()
            return target
        self.app.download = fake
        self.assertEqual(self.call("POST", "/api/download", body={"vid": "xxxxxxxxxxx"})[0], 404)
        first = self.ok("POST", "/api/download", body={"vid": video})
        self.assertEqual((first["status"], first["ahead"]), ("queued", 0))
        with self.app.connect() as con:  # another video already downloading: this one waits behind it
            con.execute("INSERT INTO downloads (vid, status, queued_at) VALUES ('other000000', 'running', 1)")
        self.assertEqual(self.ok("GET", f"/api/download/{video}")["ahead"], 1)
        with self.app.connect() as con:
            con.execute("DELETE FROM downloads WHERE vid = 'other000000'")
        self.ok("POST", "/api/download", body={"vid": video})
        self.assertTrue(self.app.run_next_download())
        self.assertFalse(self.app.run_next_download(), "a double push queues once")
        status = self.ok("GET", f"/api/download/{video}")
        self.assertEqual((status["status"], status["error"]), ("failed", "network down"))
        self.ok("POST", "/api/download", body={"vid": video})
        self.assertTrue(self.app.run_next_download())
        self.assertEqual(self.ok("GET", f"/api/video/{video}")["download"]["status"], "done")
        self.assertEqual(self.ok("POST", "/api/download", body={"vid": video})["status"], "done")
        self.assertEqual(len(calls), 2, "a finished video is not fetched twice")
        self.assertEqual(self.app.downloaded_file(video), os.path.realpath(target), "Save to device serves this file")
        self.assertIsNone(self.app.downloaded_file("xxxxxxxxxxx"))
        with self.app.connect() as con:
            con.execute("UPDATE downloads SET path = '/etc/hosts' WHERE vid = ?", (video,))
        self.assertIsNone(self.app.downloaded_file(video), "nothing outside the download folder")
        with self.app.connect() as con:
            con.execute("UPDATE downloads SET path = ? WHERE vid = ?", (target, video))
        os.remove(target)
        self.assertIsNone(self.ok("GET", f"/api/download/{video}")["status"], "deleted from Plex: offer it again")
        self.assertEqual(self.ok("POST", "/api/download", body={"vid": video})["status"], "queued")

    def test_description_is_fetched_once(self):
        self.ok("POST", "/api/interests", body={"name": "jigs"})
        calls = []

        def fake(v):
            calls.append(v)
            return {"description": "Line one\nhttps://example.com", "upload_date": "20160806", "view_count": 5}
        self.app.describe = fake
        first = self.ok("GET", "/api/description/jig00000000")
        self.assertEqual(first, {"text": "Line one\nhttps://example.com", "upload_date": "20160806", "views": 5})
        self.assertEqual(self.ok("GET", "/api/description/jig00000000"), first)
        self.assertEqual(calls, ["jig00000000"])
        self.assertEqual(self.call("GET", "/api/description/xxxxxxxxxxx")[0], 404)

        def boom(v):
            raise RuntimeError("video unavailable")
        self.app.describe = boom
        self.assertEqual(self.call("GET", "/api/description/jig00000001")[0], 502)

    def test_channel_url(self):
        self.assertEqual(tube.channel_url("https://www.youtube.com/@kunchenguid/featured"),
                         "https://www.youtube.com/@kunchenguid/videos")
        self.assertEqual(tube.channel_url(CHANNEL), f"https://www.youtube.com/channel/{CHANNEL}/videos")


if __name__ == "__main__":
    unittest.main()
