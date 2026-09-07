#!/usr/bin/env python3
# coding: utf-8
from __future__ import division, print_function, unicode_literals

import os
import shutil
import subprocess as sp
import sys
import tempfile
import time
import unittest

from copyparty import hls
from copyparty.authsrv import AuthSrv
from copyparty.httpcli import HttpCli
from copyparty.util import SCWD
from tests import util as tu
from tests.util import Cfg

# sloppyparty: on-the-fly HLS video transcoding (copyparty/hls.py + the
# /.hls/ endpoints in httpcli). these tests pin the contracts between the
# fork's code and the upstream APIs it consumes, which have silently changed
# before (HAVE_FFMPEG became an argv-vector, th_r_ffv became a set) without
# any merge conflict; see the fork-maintenance notes

FF = b"/opt/ff/bin/ffmpeg"


def hdr(query):
    h = "GET /{} HTTP/1.1\r\nConnection: close\r\n\r\n"
    return h.format(query).encode("utf-8")


class FakeRun(object):
    """stand-in for util.runcmd; records every argv and answers like ffmpeg"""

    def __init__(self, rc=0, out=""):
        self.calls = []
        self.rc = rc
        self.out = out

    def __call__(self, argv, timeout=None, **ka):
        self.calls.append((list(argv), dict(ka)))
        return self.rc, self.out, ""


def boom(*a, **ka):
    raise OSError("exec format error")


class VN(object):
    def __init__(self, **flags):
        self.flags = flags


class TestHlsFfmpegArgv(unittest.TestCase):
    """
    mtag.HAVE_FFMPEG is a list (argv-vector) since upstream cea97ac6, so every
    ffmpeg invocation must use its first element as argv[0] -- a nested list
    made runcmd raise TypeError, which the old bare excepts swallowed as
    "ffmpeg lacks libx264/aac" -- and must run with cwd=SCWD like upstream
    (windows: never pick up binaries/dlls sitting next to the media)
    """

    def setUp(self):
        self.ff0 = hls.HAVE_FFMPEG
        self.run0 = hls.runcmd
        self.logs = []

    def tearDown(self):
        hls.HAVE_FFMPEG = self.ff0
        hls.runcmd = self.run0

    def log(self, src, msg, c=0):
        self.logs.append((src, msg, c))

    def assertArgv(self, fake):
        self.assertTrue(fake.calls, "ffmpeg was never invoked")
        for argv, ka in fake.calls:
            self.assertEqual(argv[0], FF)
            for x in argv:
                self.assertIsInstance(x, bytes)
            self.assertEqual(ka.get("cwd", "unset"), SCWD)

    def test_have_enc(self):
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = fake = FakeRun(0, "Encoder libx264 [libx264 H.264 / AVC]:\n")
        self.assertTrue(hls.ff_have_enc("libx264"))
        self.assertFalse(hls.ff_have_enc("aac"))
        self.assertArgv(fake)
        argv = fake.calls[0][0]
        self.assertEqual(argv[1:], [b"-hide_banner", b"-h", b"encoder=libx264"])

    def test_have_filter(self):
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = fake = FakeRun(0, "Filter zscale\n  slice threading supported\n")
        self.assertTrue(hls.ff_have_filter("zscale"))
        self.assertFalse(hls.ff_have_filter("libplacebo"))
        self.assertArgv(fake)

    def test_test_enc(self):
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = fake = FakeRun(0)
        self.assertTrue(hls.ff_test_enc("h264_nvenc"))
        self.assertArgv(fake)
        self.assertIn(b"h264_nvenc", fake.calls[0][0])
        hls.runcmd = FakeRun(1)
        self.assertFalse(hls.ff_test_enc("h264_nvenc"))

    def test_probe_hwenc(self):
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = fake = FakeRun(0, "Encoder h264_nvenc [NVIDIA NVENC H.264 encoder]:\n")
        self.assertEqual(hls.probe_hwenc(self.log), ["nvenc"])
        self.assertArgv(fake)

    def test_probe_tonemap(self):
        hls.HAVE_FFMPEG = [FF]
        out = "Filter libplacebo\nFilter tonemap_opencl\nFilter zscale\n"
        hls.runcmd = fake = FakeRun(0, out)
        self.assertEqual(hls.probe_tonemap(self.log), "placebo")
        self.assertArgv(fake)
        # the ffmpeg -vf argument must be a single bytes token
        for argv, _ in fake.calls:
            if b"-vf" in argv:
                self.assertIsInstance(argv[argv.index(b"-vf") + 1], bytes)

    def test_probe_readrate(self):
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = fake = FakeRun(0)
        self.assertEqual(hls.probe_readrate(self.log), 2)
        self.assertArgv(fake)
        self.assertIn(b"-readrate_initial_burst", fake.calls[0][0])
        hls.runcmd = FakeRun(1)
        self.assertEqual(hls.probe_readrate(self.log), 0)

    def test_probes_degrade_when_ffmpeg_is_broken(self):
        # the optional probes (hw encoders, tonemap, pacing) must never take
        # the whole feature down; they log and fall back to the plain x264 path
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = boom
        self.assertEqual(hls.probe_hwenc(self.log), [])
        self.assertEqual(hls.probe_tonemap(self.log), "")
        self.assertEqual(hls.probe_readrate(self.log), 0)
        self.assertTrue(any("failed" in x[1] for x in self.logs))

    def test_encoder_probe_raises_when_ffmpeg_is_broken(self):
        # ...but the mandatory encoder check must surface the real error to
        # svchub (which logs it) instead of pretending the encoder is missing
        hls.HAVE_FFMPEG = [FF]
        hls.runcmd = boom
        self.assertRaises(OSError, hls.ff_have_enc, "libx264")

    def test_missing_ffmpeg(self):
        hls.HAVE_FFMPEG = []
        hls.runcmd = fake = FakeRun(0, "Encoder libx264\nFilter zscale\n")
        self.assertFalse(hls.ff_have_enc("libx264"))
        self.assertFalse(hls.ff_have_filter("zscale"))
        self.assertFalse(hls.ff_test_enc("libx264"))
        self.assertEqual(hls.probe_hwenc(self.log), [])
        self.assertEqual(hls.probe_tonemap(self.log), "")
        self.assertEqual(hls.probe_readrate(self.log), 0)
        self.assertRaises(Exception, hls.ffmpeg_bin)
        self.assertEqual(fake.calls, [])

    def test_session_argv(self):
        hls.HAVE_FFMPEG = [FF]
        args = Cfg(
            v=[".::r"],
            a=[],
            vt_maxh=720,
            vt_vq=23,
            vt_preset="veryfast",
            vt_enc="auto",
            vt_tonemap="auto",
            vt_aq=128,
            vt_seg=4.0,
            vt_readrate=1.5,
            vt_hwenc=[],
            vt_tm="",
            vt_rr=2,
        )
        srv = hls.HlsSrv.__new__(hls.HlsSrv)
        srv.args = args
        srv.hw_bad = set()

        now = time.time()
        cd = os.path.join(tempfile.gettempdir(), "vt", "cd")
        rd = os.path.join(cd, "720")
        s = hls._HlsSess(cd, rd, "/media", "clip.mp4", 1234.0, 720, 0, now)
        argv = srv._session_argv(s, VN(), 1920, 1080, 0, "x264")
        self.assertEqual(argv[0], FF)
        for x in argv:
            self.assertIsInstance(x, bytes)
        self.assertIn(b"libx264", argv)
        self.assertIn(b"-readrate", argv)
        self.assertIn(b"-readrate_initial_burst", argv)
        self.assertNotIn(b"-ss", argv)
        self.assertEqual(argv[argv.index(b"-vf") + 1], b"scale=1280:720,format=yuv420p")

        # a session started by a seek: input seek + output timestamps at the true slot
        s = hls._HlsSess(cd, rd, "/media", "clip.mp4", 1234.0, 720, 5, now)
        argv = srv._session_argv(s, VN(), 1920, 1080, 0, "x264")
        self.assertEqual(argv[0], FF)
        self.assertEqual(argv[argv.index(b"-ss") + 1], b"20.000")
        self.assertEqual(argv[argv.index(b"-output_ts_offset") + 1], b"20.000")
        self.assertEqual(argv[argv.index(b"-start_number") + 1], b"5")

        # the vt_readrate volflag overrides the global (0 = unpaced)
        argv = srv._session_argv(s, VN(vt_readrate=0), 1920, 1080, 0, "x264")
        self.assertNotIn(b"-readrate", argv)

    def test_meta_and_ladder(self):
        args = Cfg(v=[".::r"], a=[], vt_maxh=720)
        # the ladder = standard rungs below the cap + the cap (source height
        # clamped to vt_maxh); this is the complete set of valid <height>/ paths
        self.assertEqual(hls.hls_ladder(args, VN(), 1080), [480, 720])
        self.assertEqual(hls.hls_ladder(args, VN(vt_maxh=0), 2160), [480, 720, 1080, 1440, 2160])
        self.assertEqual(hls.hls_ladder(args, VN(vt_maxh=1080), 1080), [480, 720, 1080])
        self.assertEqual(hls.hls_ladder(args, VN(), 720), [480, 720])
        self.assertEqual(hls.hls_ladder(args, VN(), 240), [240])
        self.assertEqual(hls.hls_ladder(args, VN(), 481), [480])  # odd cap rounds down

        td = tempfile.mkdtemp(prefix="cpp-hls-")
        try:
            self.assertEqual(hls.hls_meta(td), None)  # not probed yet
            self.assertEqual(hls.hls_nseg(td, 4.0), 0)
            with open(os.path.join(td, "meta.txt"), "wb") as f:
                f.write(b"1280 720 0 9.000000")
            self.assertEqual(hls.hls_meta(td), (1280, 720, 0, 9.0))
            self.assertEqual(hls.hls_nseg(td, 4.0), 3)
            self.assertEqual(hls.hls_nseg(td, 9.0), 1)
            self.assertEqual(hls.hls_nseg(td, 0), 0)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_hardkill(self):
        # a reaped session's ffmpeg must die without getting to write its
        # trailer (which would finalize a truncated segment); _hardkill also
        # has to work on a process that already exited
        p = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stderr=sp.PIPE)
        t0 = time.time()
        hls._hardkill(p)
        self.assertIsNotNone(p.poll())
        self.assertLess(time.time() - t0, 5)
        hls._hardkill(p)  # idempotent

        td = tempfile.mkdtemp(prefix="cpp-hls-")
        try:
            for fn in ("v00003.ts.tmp", "v00002.ts", "index.m3u8"):
                with open(os.path.join(td, fn), "wb") as f:
                    f.write(b"x")
            hls._rm_tmp(td)
            self.assertEqual(sorted(os.listdir(td)), ["index.m3u8", "v00002.ts"])
        finally:
            shutil.rmtree(td, ignore_errors=True)


class FakeQ(object):
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class FakeBroker(object):
    """the hub side of hlssrv.ensure/poke, as seen from an http worker"""

    def __init__(self, cachedir):
        self.cachedir = cachedir
        self.asks = []
        self.says = []

    def ask(self, dest, *a):
        self.asks.append((dest,) + a)
        return FakeQ(self.cachedir)

    def say(self, dest, *a):
        self.says.append((dest,) + a)


class TestHlsHttp(unittest.TestCase):
    """the /<video>/.hls/ endpoints, with the hub (transcoder) faked"""

    def setUp(self):
        self.td = tu.get_ramdisk()
        self.vol = os.path.join(self.td, "vfs")
        os.mkdir(self.vol)
        for fn in ("clip.mp4", "hello.txt"):
            with open(os.path.join(self.vol, fn), "wb") as f:
                f.write(b"not really a video")

    def tearDown(self):
        os.chdir(tempfile.gettempdir())
        shutil.rmtree(self.td)

    def log(self, src, msg, c=0):
        pass

    def setup(self, volflags=""):
        # what svchub hands the http-workers when transcoding is available:
        # have_x264/have_aac probed true, vt_exts = the transcodable formats
        self.args = Cfg(
            v=[self.vol + "::r" + volflags],
            a=[],
            have_x264=True,
            have_aac=True,
            vt_exts=set(["mp4", "mkv", "webm"]),
            vt_seg=4,
            vt_maxh=720,
        )
        self.asrv = AuthSrv(self.args, self.log)
        self.conn = tu.VHttpConn(self.args, self.asrv, self.log, b"")
        vn = self.asrv.vfs.all_vols[""]
        histpath = self.asrv.vfs.histtab[vn.realpath]
        st = os.stat(os.path.join(self.vol, "clip.mp4"))
        self.cachedir = hls.hls_path(histpath, "clip.mp4", int(st.st_mtime))
        self.broker = self.conn.hsrv.broker = FakeBroker(self.cachedir)

    def curl(self, url):
        conn = self.conn.setbuf(hdr(url))
        HttpCli(conn).run()
        h, b = conn.s._reply.split(b"\r\n\r\n", 1)
        h = h.decode("utf-8")
        return int(h.split(" ", 2)[1]), h.lower(), b

    def seed(self, relpath, data):
        ap = os.path.join(self.cachedir, relpath)
        d = os.path.dirname(ap)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(ap, "wb") as f:
            f.write(data)

    def test(self):
        self.setup()

        # the client is told which formats it may ask to transcode (js_htm is
        # the CGV1 blob the browser page embeds; the test harness stubs the
        # templates, so read it off the volume instead of the rendered page)
        js = self.asrv.vfs.all_vols[""].js_htm
        self.assertIn('"have_vcode": true', js)
        self.assertIn('"vcode_exts": ["mkv", "mp4", "webm"]', js)

        # not a video extension / no such file: 404 without touching the hub
        st, h, b = self.curl("hello.txt/.hls/master.m3u8")
        self.assertEqual(st, 404)
        st, h, b = self.curl("nope.mp4/.hls/master.m3u8")
        self.assertEqual(st, 404)
        self.assertEqual(self.broker.asks, [])

        # a segment of a never-probed source is refused (a real player always
        # fetches the playlists first, which is what probes the source)
        st, h, b = self.curl("clip.mp4/.hls/720/v00000.ts")
        self.assertEqual(st, 404)
        self.assertEqual(self.broker.asks, [])

        # master playlist: ensure(height=0) then serve the file the hub wrote
        master = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n720/index.m3u8\n"
        self.seed("master.m3u8", master)
        st, h, b = self.curl("clip.mp4/.hls/master.m3u8")
        self.assertEqual(st, 200)
        self.assertIn("content-type: application/vnd.apple.mpegurl", h)
        self.assertIn("no-store", h)
        self.assertEqual(b, master)
        self.assertEqual(self.broker.asks[-1][0], "hlssrv.ensure")
        self.assertEqual(self.broker.asks[-1][4:], (-1, 0))
        self.assertEqual(self.broker.says[-1], ("hlssrv.poke", self.cachedir))

        # filekey gets re-attached to every child uri
        st, h, b = self.curl("clip.mp4/.hls/master.m3u8?k=abc")
        self.assertEqual(st, 200)
        self.assertIn(b"\n720/index.m3u8?k=abc\n", b)
        self.assertIn(b"#EXT-X-VERSION:3\n", b)

        # rendition playlist: only served once complete (#EXT-X-ENDLIST); the
        # source is not probed yet (no meta.txt) so the height is not checked
        # here -- the hub validates it against the ladder when generating
        pl = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.000,\nv00000.ts\n#EXTINF:4.000,\nv00001.ts\n#EXTINF:1.000,\nv00002.ts\n#EXT-X-ENDLIST\n"
        self.seed("720/index.m3u8", pl)
        st, h, b = self.curl("clip.mp4/.hls/720/index.m3u8")
        self.assertEqual(st, 200)
        self.assertEqual(b, pl)
        self.assertEqual(self.broker.asks[-1][4:], (-1, 720))

        # the hub probed the source: 1280x720, sdr, 9 seconds
        self.seed("meta.txt", b"1280 720 0 9.000000")

        # a segment: ensure(idx, height) then plain file download
        seg = b"G" * 188 * 40
        self.seed("720/v00001.ts", seg)
        st, h, b = self.curl("clip.mp4/.hls/720/v00001.ts")
        self.assertEqual(st, 200)
        self.assertEqual(b, seg)
        self.assertEqual(self.broker.asks[-1][4:], (1, 720))

        # renditions that are not on the ladder of a 720p source (480+720) are
        # refused up front instead of spinning up an ffmpeg session for nothing
        n = len(self.broker.asks)
        for url in (
            "clip.mp4/.hls/999/index.m3u8",
            "clip.mp4/.hls/1080/index.m3u8",
            "clip.mp4/.hls/1080/v00000.ts",
            "clip.mp4/.hls/144/v00000.ts",
        ):
            st, h, b = self.curl(url)
            self.assertEqual(st, 404, url)
        self.assertEqual(len(self.broker.asks), n)
        self.seed("480/index.m3u8", pl)
        st, h, b = self.curl("clip.mp4/.hls/480/index.m3u8")
        self.assertEqual(st, 200)
        self.assertEqual(self.broker.asks[-1][4:], (-1, 480))  # on the ladder: asked

        # a segment index past the end of the video is refused too, instead of
        # spawning a doomed ffmpeg seek and waiting for a timeout
        n = len(self.broker.asks)
        st, h, b = self.curl("clip.mp4/.hls/720/v00003.ts")
        self.assertEqual(st, 404)
        self.assertEqual(len(self.broker.asks), n)
        st, h, b = self.curl("clip.mp4/.hls/720/v00001.ts")
        self.assertEqual(st, 200)

        # bad shapes never reach tx_hls (RE_HLS is strict; leading zeros would
        # make the poll dir differ from the one the hub writes to)
        for url in (
            "clip.mp4/.hls/720/v1.ts",
            "clip.mp4/.hls/720/v00001.mp4",
            "clip.mp4/.hls/index.m3u8",
            "clip.mp4/.hls/720/v00001.tsx",
            "clip.mp4/.hls/72/v00001.ts",
            "clip.mp4/.hls/0720/index.m3u8",
            "clip.mp4/.hls/0720/v00001.ts",
        ):
            n = len(self.broker.asks)
            st, h, b = self.curl(url)
            self.assertNotEqual(st, 200, url)
            self.assertEqual(len(self.broker.asks), n, url)

        # dot-dot is collapsed by the url normalization before routing, so
        # this is just the same segment again; the hub must only ever see the
        # clean volume-relative path of the source video
        st, h, b = self.curl("clip.mp4/.hls/720/../720/v00001.ts")
        self.assertEqual(st, 200)
        self.assertEqual(b, seg)
        self.assertEqual(self.broker.asks[-1][2], "clip.mp4")
        for ask in self.broker.asks:
            self.assertNotIn("..", ask[2])

    def test_disabled(self):
        # volflag dvcode (--no-vcode per volume) hides the endpoints
        self.setup(":c,dvcode")
        self.seed("master.m3u8", b"#EXTM3U\n")
        st, h, b = self.curl("clip.mp4/.hls/master.m3u8")
        self.assertEqual(st, 404)
        self.assertEqual(self.broker.asks, [])
        self.assertIn('"have_vcode": false', self.asrv.vfs.all_vols[""].js_htm)

        # and so does a hub without transcoding (no libx264/aac, or --no-vcode)
        self.setup()
        self.args.have_x264 = False
        st, h, b = self.curl("clip.mp4/.hls/master.m3u8")
        self.assertEqual(st, 404)
        self.args.have_x264 = True
        self.args.no_vcode = True
        st, h, b = self.curl("clip.mp4/.hls/master.m3u8")
        self.assertEqual(st, 404)
        self.assertEqual(self.broker.asks, [])
