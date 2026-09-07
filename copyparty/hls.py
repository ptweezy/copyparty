# coding: utf-8
from __future__ import print_function, unicode_literals

import hashlib
import math
import os
import signal
import subprocess as sp
import threading
import time

from queue import Full, Queue

from .__init__ import ANYWIN, MACOS, TYPE_CHECKING, WINDOWS
from .bos import bos
from .mtag import HAVE_FFMPEG, TH_BWRAP, bwrap, bwrap_fail, ffprobe
from .util import (
    HAVE_PSUTIL,
    NICEB,
    SCWD,
    Daemon,
    afsenc,
    atomic_move,
    fsenc,
    min_ex,
    runcmd,
    ub64enc,
    vsplit,
)

if True:  # pylint: disable=using-constant-test
    from typing import Any, Optional

if TYPE_CHECKING:
    from .authsrv import VFS
    from .svchub import SvcHub


# ffmpeg quality-preset validation (keep in sync with --vt-preset)
X264_PRESETS = set(
    "ultrafast superfast veryfast faster fast medium slow slower veryslow placebo".split()
)


def ffmpeg_bin() -> bytes:
    # mtag.HAVE_FFMPEG is an argv-vector (a list holding the absolute path of
    # the binary, or empty when unavailable), not a bare path, and svchub may
    # empty it at runtime (--unsafe-tools check). resolve it on every use so a
    # cached stale path can never be spawned; callers that want a soft "not
    # available" answer must check HAVE_FFMPEG for truthiness before calling
    if not HAVE_FFMPEG:
        raise Exception("ffmpeg is not available")
    return HAVE_FFMPEG[0]


def _ff_help(topic: str) -> str:
    # stdout+stderr of "ffmpeg -h <topic>"; raises if ffmpeg cannot be run
    cmd = [ffmpeg_bin(), b"-hide_banner", b"-h", topic.encode("ascii")]
    _, so, se = runcmd(cmd, cwd=SCWD, timeout=10)
    return (so or "") + (se or "")


def ff_have_enc(name: str) -> bool:
    # true if the local ffmpeg has this encoder; raises if ffmpeg is broken
    # (the caller decides whether that is fatal for the feature)
    if not HAVE_FFMPEG:
        return False
    return ("Encoder " + name) in _ff_help("encoder=" + name)


def ff_have_filter(name: str) -> bool:
    # true if the local ffmpeg has this filter; raises if ffmpeg is broken
    if not HAVE_FFMPEG:
        return False
    return ("Filter " + name) in _ff_help("filter=" + name)


# abstract name -> ffmpeg h264 encoder; order = auto-preference (first validated
# wins). qsv/vaapi need host-specific device+filter setup so they are opt-in
# only (via --vt-enc), never auto-selected here
HWENC = [
    ("vt", "h264_videotoolbox"),  # macos / apple silicon
    ("nvenc", "h264_nvenc"),      # nvidia
    ("qsv", "h264_qsv"),          # intel quicksync
    ("amf", "h264_amf"),          # amd (windows)
]
ENC2FF = dict(HWENC + [("x264", "libx264")])

# x264 speed-preset name -> each hw encoder's own speed control
_NVENC_P = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
    "fast": "p4", "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}
_AMF_Q = {
    "ultrafast": "speed", "superfast": "speed", "veryfast": "speed",
    "faster": "balanced", "fast": "balanced", "medium": "balanced",
    "slow": "quality", "slower": "quality", "veryslow": "quality",
}

# HDR->SDR tonemap methods, best quality first; validated at startup
_TM_PREF = ["placebo", "opencl", "zscale"]


def _b(v: Any) -> bytes:
    return str(v).encode("ascii")


def enc_argv(enc: str, crf: int, preset: str, prof: str) -> list[bytes]:
    # -crf/-preset are libx264-only; every hw encoder has its own quality knob,
    # so translate the abstract (crf, preset) into each encoder's real flags.
    # qp-based hw encoders reuse crf as-is (same direction: lower=better);
    # videotoolbox needs it inverted onto its 0..100 (higher=better) scale
    p = _b(prof)
    if enc == "vt":
        q = max(1, min(100, 100 - crf * 2))
        return [b"-c:v", b"h264_videotoolbox", b"-q:v", _b(q), b"-profile:v", p]
    if enc == "nvenc":
        return [b"-c:v", b"h264_nvenc", b"-preset", _b(_NVENC_P.get(preset, "p4")),
                b"-rc", b"vbr", b"-cq", _b(crf), b"-profile:v", p]
    if enc == "qsv":
        return [b"-c:v", b"h264_qsv", b"-preset", _b(preset),
                b"-global_quality", _b(crf), b"-profile:v", p]
    if enc == "amf":
        return [b"-c:v", b"h264_amf", b"-rc", b"cqp", b"-qp_i", _b(crf),
                b"-qp_p", _b(crf), b"-quality", _b(_AMF_Q.get(preset, "balanced")),
                b"-profile:v", p]
    # x264 (and any unknown value) -> software libx264
    return [b"-c:v", b"libx264", b"-preset", _b(preset), b"-crf", _b(crf),
            b"-profile:v", p]


def ff_test_enc(ffenc: str) -> bool:
    # a listed encoder may still fail at runtime (no gpu, no driver, device busy,
    # build stub) -- prove it works by encoding one lavfi frame to null
    if not HAVE_FFMPEG:
        return False
    cmd = [
        ffmpeg_bin(), b"-nostdin", b"-v", b"error", b"-hide_banner",
        b"-f", b"lavfi", b"-i", b"color=c=black:s=64x64:d=1",
        b"-frames:v", b"1", b"-c:v", ffenc.encode("ascii"),
        b"-f", b"null", b"-",
    ]
    rc, _, _ = runcmd(cmd, cwd=SCWD, timeout=15)
    return rc == 0


def probe_hwenc(log: Any) -> list[str]:
    # abstract names of hw h264 encoders that both exist and actually encode a
    # frame; result is cached on the hub (args.vt_hwenc), like args.have_x264
    ret = []
    for abbr, ffenc in HWENC:
        try:
            if ff_have_enc(ffenc) and ff_test_enc(ffenc):
                ret.append(abbr)
                log("hls", "hw-encoder ok: %s (%s)" % (abbr, ffenc), 6)
        except Exception as ex:
            # a broken probe only loses this one encoder (x264 is unaffected)
            log("hls", "hw-encoder probe failed for %s: %r" % (ffenc, ex), 6)
    return ret


def tm_devargs(method: str) -> list[bytes]:
    # global ffmpeg options that must precede -i for gpu tonemap methods
    if method == "opencl":
        return [b"-init_hw_device", b"opencl=ocl", b"-filter_hw_device", b"ocl"]
    if method == "placebo":
        return [b"-init_hw_device", b"vulkan=vk", b"-filter_hw_device", b"vk"]
    return []


def tm_vf(method: str, ow: int, oh: int) -> str:
    # HDR (PQ/HLG, BT.2020) -> SDR (BT.709) tonemap, scaled to ow x oh, 8-bit
    sc = "scale=%d:%d" % (ow, oh)
    if method == "placebo":
        # libplacebo scales + tonemaps + converts on-gpu in one pass (BT.2390)
        return ("libplacebo=w=%d:h=%d:tonemapping=bt.2390:colorspace=bt709"
                ":color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p"
                % (ow, oh))
    if method == "opencl":
        return ",".join([
            "format=p010,hwupload",
            "tonemap_opencl=tonemap=hable:transfer=bt709:matrix=bt709"
            ":primaries=bt709:format=nv12",
            "hwdownload,format=nv12", sc, "format=yuv420p"])
    # zscale (cpu, universal): linearize PQ -> tonemap(hable) -> BT.709 -> scale
    return ",".join([
        "zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
        "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv",
        sc, "format=yuv420p"])


def probe_tonemap(log: Any) -> str:
    # pick the best HDR->SDR tonemap method the local ffmpeg can actually run;
    # validate with a synthetic HDR frame (vulkan/opencl init can fail at runtime
    # even when the filter is compiled in). cached on the hub (args.vt_tm)
    if not HAVE_FFMPEG:
        return ""
    src = ("color=c=gray:s=64x64:d=1,format=yuv420p10le"
           ",setparams=color_primaries=bt2020:color_trc=smpte2084"
           ":colorspace=bt2020nc")
    filt = {"placebo": "libplacebo", "opencl": "tonemap_opencl", "zscale": "zscale"}
    for m in _TM_PREF:
        try:
            if not ff_have_filter(filt[m]):
                continue
            cmd = [ffmpeg_bin(), b"-nostdin", b"-v", b"error", b"-hide_banner"]
            cmd += tm_devargs(m)
            cmd += [b"-f", b"lavfi", b"-i", src.encode("ascii"),
                    b"-vf", tm_vf(m, 64, 64).encode("ascii"),
                    b"-frames:v", b"1", b"-f", b"null", b"-"]
            rc, _, _ = runcmd(cmd, cwd=SCWD, timeout=20)
            if rc == 0:
                log("hls", "hdr tonemap method: %s" % (m,), 6)
                return m
        except Exception as ex:
            log("hls", "tonemap probe failed for %s: %r" % (m, ex), 6)
    log("hls", "no working HDR tonemap filter; HDR transcodes will look "
        "washed-out (need ffmpeg with libplacebo/opencl/zimg)", 3)
    return ""


def probe_readrate(log: Any) -> int:
    # how much realtime-pacing this ffmpeg supports (cached on the hub as
    # args.vt_rr): 0 = none, 1 = -readrate, 2 = -readrate + initial-burst.
    # a streaming session encodes forward continuously; without pacing it races
    # to EOF and burns cpu on video the viewer may never reach, so we throttle
    # it to ~realtime and use the initial burst to fill the player's start buffer
    if not HAVE_FFMPEG:
        return 0
    src = [b"-f", b"lavfi", b"-i", b"color=c=black:s=64x64:d=0.2"]
    tail = [b"-frames:v", b"1", b"-f", b"null", b"-"]
    try:
        pre = [ffmpeg_bin(), b"-nostdin", b"-v", b"error", b"-hide_banner"]
        rc, _, _ = runcmd(
            pre + [b"-readrate", b"2", b"-readrate_initial_burst", b"1"] + src + tail,
            cwd=SCWD,
            timeout=15,
        )
        if rc == 0:
            log("hls", "realtime-pacing: readrate + initial-burst", 6)
            return 2
        rc, _, _ = runcmd(pre + [b"-readrate", b"2"] + src + tail, cwd=SCWD, timeout=15)
        if rc == 0:
            log("hls", "realtime-pacing: readrate (no initial-burst)", 6)
            return 1
    except Exception as ex:
        log("hls", "readrate probe failed: %r" % (ex,), 6)
    log("hls", "ffmpeg has no -readrate; transcode sessions will not be paced "
        "and may use more cpu (upgrade ffmpeg to >= 4.x / 6.1 for pacing)", 4)
    return 0


def _hardkill(p: Any) -> None:
    # stop a session's ffmpeg WITHOUT letting it finish: SIGTERM (what
    # util.killtree sends first) is a graceful stop for ffmpeg, which then
    # writes its trailer, and the hls muxer's trailer renames the half-done
    # .tmp segment into a final v%05d.ts -- which we would then cache and
    # serve as if it were complete. SIGKILL leaves only the .tmp behind.
    # with --th-bwrap p is the sandbox, so take out its children as well
    try:
        if HAVE_PSUTIL:
            import psutil

            for c in psutil.Process(p.pid).children(recursive=True):
                try:
                    c.kill()
                except Exception:
                    pass
        elif not ANYWIN:
            pids = []
            chk = [p.pid]
            while chk:
                pid, chk = chk[0], chk[1:]
                if pid != p.pid:
                    pids.append(pid)
                _, t, _ = runcmd(["pgrep", "-P", str(pid)])
                chk += [int(x) for x in t.strip().split("\n") if x]
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass


def _rm_tmp(rdir: str) -> None:
    # leftovers of a hard-killed ffmpeg (hls temp_file segments in progress)
    try:
        for fn in os.listdir(rdir):
            if fn.endswith(".tmp"):
                try:
                    os.unlink(os.path.join(rdir, fn))
                except Exception:
                    pass
    except Exception:
        pass


def _drain(p: Any, lines: list[str]) -> None:
    # keep the tail of ffmpeg's stderr (-v error, so only actual problems);
    # runs in its own thread so a chatty ffmpeg never blocks on a full pipe
    try:
        for ln in iter(p.stderr.readline, b""):
            lines.append(ln.decode("utf-8", "replace").rstrip())
            if len(lines) > 40:
                del lines[0]
    except Exception:
        pass
    try:
        p.stderr.close()
    except Exception:
        pass


def is_hdr(streams: list) -> bool:
    # true if the first video stream is HDR (PQ/HLG transfer or BT.2020 gamut)
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        trc = (s.get("color_transfer") or "").lower()
        prim = (s.get("color_primaries") or "").lower()
        return trc in ("smpte2084", "arib-std-b67") or "2020" in prim
    return False


def hls_path(histpath: str, rem: str, mtime: float) -> str:
    # cache dir (not file) for the hls transcode of rem; same sharding as
    # thumb_path, but a per-entry folder holding index.m3u8 + segments
    rd, fn = vsplit(rem)
    if not rd:
        rd = "\ntop"

    h = hashlib.sha512(afsenc(rd + "\nhls")).digest()
    b64 = ub64enc(h).decode("ascii")[:24]
    rd = ("%s/%s/" % (b64[:2], b64[2:4])).lower() + b64

    h = hashlib.sha512(afsenc(fn)).digest()
    fn = ub64enc(h).decode("ascii")[:24]

    return "%s/vt/%s/%s.%x" % (histpath, rd, fn, int(mtime))


def hls_meta(cachedir: str) -> Optional[tuple[int, int, int, float]]:
    # (width, height, is_hdr, duration) of the source as probed by HlsSrv._meta
    # into <cachedir>/meta.txt; None until a playlist has been requested once.
    # the http-workers use it to refuse impossible requests without asking the
    # hub (unknown rendition heights, segments past the end of the video)
    try:
        with open(os.path.join(cachedir, "meta.txt"), "rb") as f:
            a = f.read().decode("utf-8").split(" ")
            return int(a[0]), int(a[1]), int(a[2]), float(a[3])
    except Exception:
        return None


def hls_ladder(args: Any, vn: "VFS", resh: int) -> list[int]:
    # abr rendition heights for a source of this height: standard rungs below
    # the cap, plus the cap itself (source height, clamped to vt_maxh); this
    # is the complete set of <height>/ paths that exist for a video
    maxh = int(vn.flags.get("vt_maxh", args.vt_maxh))
    cap = min(resh, maxh) if maxh else resh
    cap -= cap % 2
    rungs = [h for h in (480, 720, 1080, 1440, 2160) if h < cap]
    rungs.append(cap)
    return sorted(set(h for h in rungs if h >= 144))


def hls_nseg(cachedir: str, seg: float) -> int:
    # number of segments in the VOD playlists of this cache entry, or 0 when
    # the source has not been probed yet (same formula as _gen_playlist)
    meta = hls_meta(cachedir)
    if not meta or meta[3] <= 0 or seg <= 0:
        return 0
    return int(math.ceil(meta[3] / seg))


def hls_cfg(args: Any, vn: "VFS") -> str:
    # transcode-config fingerprint; a change wipes the vc cache (see clean)
    ret = []
    for k in ("vt_maxh", "vt_vq", "vt_preset", "vt_aq", "vt_seg", "vt_enc", "vt_tonemap"):
        ret.append("%s(%s)\n" % (k, vn.flags.get(k)))
    return "".join(ret)


# forward-seek this many segments past a session's request frontier before we
# treat a request as a seek (kill + restart here) rather than normal read-ahead
_SESS_LOOKAHEAD = 4


class _HlsSess(object):
    # one forward-running ffmpeg transcode ("streaming session"), keyed per
    # (cachedir, height). it encodes continuously from segment `lo` to EOF via
    # the hls muxer, so all segment boundaries share one encoder+audio state and
    # join seamlessly; the http-workers just read the finished v*.ts files
    def __init__(
        self, cachedir: str, rdir: str, ptop: str, rem: str, mtime: float,
        height: int, lo: int, now: float
    ) -> None:
        self.cachedir = cachedir
        self.rdir = rdir
        self.ptop = ptop
        self.rem = rem
        self.mtime = mtime
        self.height = height
        self.lo = lo             # first segment index this session produces
        self.hi = lo             # highest requested index (playhead frontier)
        self.last_want = now     # last time a segment was requested (idle reap)
        self.proc: Optional[Any] = None  # ffmpeg Popen once spawned
        self.dead = False        # set by reap/seek/shutdown; monitor then kills
        self.prev: Optional["_HlsSess"] = None  # session we replaced (seek)
        self.gone = threading.Event()  # set once our ffmpeg has fully exited


# manages on-the-fly HLS video transcodes; lives in the hub process.
# the playlist is a full-duration VOD manifest generated up front (so the
# browser shows a normal seekbar). the master/rendition playlists are quick
# one-shot jobs on the worker queue; segments are produced by a continuous
# per-stream ffmpeg session (see _HlsSess) started on demand. the ensure
# broker-verb only records intent and returns fast; http-workers read the
# finished files straight off the shared .hist cache
class HlsSrv(object):
    def __init__(self, hub: "SvcHub") -> None:
        self.hub = hub
        self.args = hub.args
        self.asrv = hub.asrv
        self.log_func = hub.log

        self.mutex = threading.Lock()
        self.busy: dict[str, bool] = {}
        self.hw_bad: set[str] = set()  # hw encoders that failed at runtime
        self.stopping = False
        self.nthr = max(1, self.args.vt_jobs)

        # active streaming sessions, keyed "cachedir\nheight"; at most nthr run
        # concurrently (one ffmpeg each), the rest get reaped LRU / on idle
        self.sessions: dict[str, _HlsSess] = {}

        self.q: Queue[Optional[tuple[str, str, str, float, int, int]]] = Queue(self.nthr * 4)
        for n in range(self.nthr):
            Daemon(self.worker, "hls-%d" % (n,))

        Daemon(self._reaper, "hls-reap")

    def log(self, msg: str, c: int = 0) -> None:
        self.log_func("hls", msg, c)

    def shutdown(self) -> None:
        self.stopping = True
        with self.mutex:
            procs = [s.proc for s in self.sessions.values() if s.proc]
            for s in self.sessions.values():
                s.dead = True
            self.sessions.clear()
        for p in procs:
            _hardkill(p)
        for _ in range(self.nthr):
            try:
                self.q.put_nowait(None)
            except Full:
                pass

    # broker-verb; enqueue the master playlist (height 0), a rendition playlist
    # (height>0, idx<0), or a segment (height>0, idx>=0) unless it is already
    # cached or queued. returns the cachedir (fast; the http-worker then polls
    # the shared filesystem for the finished file)
    def ensure(
        self, ptop: str, rem: str, mtime: float, idx: int, height: int = 0
    ) -> str:
        histpath = self.asrv.vfs.histtab.get(ptop)
        if not histpath:
            self.log("no histpath for %r" % (ptop,), 3)
            return ""

        cachedir = hls_path(histpath, rem, mtime)
        # height 0 = the abr master playlist (lives at cachedir); each rendition
        # caches independently in a per-height subdir
        if not height:
            target = os.path.join(cachedir, "master.m3u8")
        elif idx < 0:
            target = os.path.join(cachedir, str(height), "index.m3u8")
        else:
            target = os.path.join(cachedir, str(height), "v%05d.ts" % (idx,))
        try:
            if bos.path.getsize(target) > 0:
                _poke_dirs(cachedir)
                return cachedir
        except Exception:
            pass

        if height and idx >= 0:
            # a segment: driven by the continuous streaming session, not the
            # one-shot queue (fast: records intent / (re)starts ffmpeg async)
            self._want_seg(cachedir, ptop, rem, mtime, idx, height)
            return cachedir

        # master playlist (height 0) or rendition playlist (idx<0): quick jobs
        key = "%s\n%d\n%d" % (cachedir, height, idx)
        with self.mutex:
            if key not in self.busy:
                try:
                    self.q.put_nowait((cachedir, ptop, rem, mtime, idx, height))
                    self.busy[key] = True
                except Full:
                    pass

        return cachedir

    # broker-verb; keep the on-disk cache alive during playback (cleaner)
    def poke(self, cachedir: str) -> None:
        _poke_dirs(cachedir)

    def worker(self) -> None:
        while not self.stopping:
            job = self.q.get()
            if job is None:
                break

            cachedir, ptop, rem, mtime, idx, height = job
            key = "%s\n%d\n%d" % (cachedir, height, idx)
            try:
                # only master/rendition playlists reach the queue; segments are
                # produced by the streaming sessions (see _want_seg)
                if not height:
                    self._gen_master(cachedir, ptop, rem, mtime)
                else:
                    self._gen_playlist(cachedir, ptop, rem, mtime, height)
            except Exception:
                self.log("transcode failed for %r h%d #%d:\n%s"
                         % (rem, height, idx, min_ex()), 3)
            finally:
                with self.mutex:
                    self.busy.pop(key, None)

    def _vn(self, ptop: str) -> "VFS":
        allvols = list(self.asrv.vfs.all_vols.values())
        vn = next((x for x in allvols if x.realpath == ptop), None)
        if not vn:
            vn = self.asrv.vfs.all_aps[0][1][0]
        return vn

    def _meta(
        self, cachedir: str, ptop: str, rem: str, vn: "VFS"
    ) -> tuple[int, int, int, float]:
        # source dims + hdr flag + duration; probed once and cached to meta.txt
        # so the master, every rendition playlist, and each segment reuse it
        mp = os.path.join(cachedir, "meta.txt")
        meta = hls_meta(cachedir)
        if meta:
            return meta

        abspath = os.path.join(ptop, rem)
        to = int(vn.flags.get("convt", self.args.th_convt) or 60)
        tags, _, streams, _ = ffprobe(abspath, max(20, to))
        if "vc" not in tags:
            raise Exception("no video stream")

        dur = float((tags.get(".dur") or [0, 0])[1] or 0)
        if dur <= 0:
            raise Exception("could not determine duration")

        resw = _tagint(tags, ".resw")
        resh = _tagint(tags, ".resh")
        hdr = 1 if is_hdr(streams) else 0

        chmod = bos.MKD_700 if self.args.free_umask else bos.MKD_755
        bos.makedirs(cachedir, vf=chmod)

        # <histpath>/vt/cfg.txt lets the cleaner invalidate the whole vt cache
        # when a quality knob changes (see th_srv.clean)
        histpath = self.asrv.vfs.histtab.get(ptop) or vn.histpath
        cfgp = os.path.join(histpath, "vt", "cfg.txt")
        if not bos.path.exists(cfgp):
            with open(cfgp, "wb") as f:
                f.write(hls_cfg(self.args, vn).encode("utf-8"))

        with open(mp, "wb") as f:
            f.write(("%d %d %d %f" % (resw, resh, hdr, dur)).encode("utf-8"))

        return resw, resh, hdr, dur

    def _ladder(self, vn: "VFS", resh: int) -> list[int]:
        return hls_ladder(self.args, vn, resh)

    def _gen_master(self, cachedir: str, ptop: str, rem: str, mtime: float) -> None:
        vn = self._vn(ptop)
        resw, resh, _, _ = self._meta(cachedir, ptop, rem, vn)

        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for h in self._ladder(vn, resh):
            w = int(round(resw * h / float(resh)))
            w -= w % 2
            # rough h264 bitrate hint (~pixel count); the player measures real
            # bandwidth after the first segment, this only seeds the initial pick
            bw = max(800000, int(6000000 * (h / 1080.0) ** 2))
            lines.append("#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d" % (bw, w, h))
            lines.append("%d/index.m3u8" % (h,))
        lines.append("")

        buf = "\n".join(lines).encode("utf-8")
        tmp = os.path.join(cachedir, "master.m3u8.tmp")
        with open(tmp, "wb") as f:
            f.write(buf)
        atomic_move(self.log, tmp, os.path.join(cachedir, "master.m3u8"), vn.flags)

    def _gen_playlist(
        self, cachedir: str, ptop: str, rem: str, mtime: float, height: int
    ) -> None:
        vn = self._vn(ptop)
        seg = float(vn.flags.get("vt_seg", self.args.vt_seg)) or 4.0
        _, resh, _, dur = self._meta(cachedir, ptop, rem, vn)
        if height not in self._ladder(vn, resh):
            t = "rendition %dp is not in the ladder of this %dp source"
            raise Exception(t % (height, resh))

        rdir = os.path.join(cachedir, str(height))
        chmod = bos.MKD_700 if self.args.free_umask else bos.MKD_755
        bos.makedirs(rdir, vf=chmod)

        nseg = int(math.ceil(dur / seg))
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:%d" % (int(math.ceil(seg)),),
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for i in range(nseg):
            d = seg if i < nseg - 1 else (dur - (nseg - 1) * seg)
            lines.append("#EXTINF:%.3f," % (d,))
            lines.append("v%05d.ts" % (i,))
        lines.append("#EXT-X-ENDLIST")
        lines.append("")

        buf = "\n".join(lines).encode("utf-8")
        tmp = os.path.join(rdir, "index.m3u8.tmp")
        with open(tmp, "wb") as f:
            f.write(buf)
        atomic_move(self.log, tmp, os.path.join(rdir, "index.m3u8"), vn.flags)

    def _want_seg(
        self, cachedir: str, ptop: str, rem: str, mtime: float, idx: int, height: int
    ) -> None:
        # record that segment `idx` (height `height`) was requested, and make
        # sure a streaming session is running that will produce it. cheap: the
        # slow probe + ffmpeg spawn happens in the session's own thread
        skey = "%s\n%d" % (cachedir, height)
        now = time.time()
        with self.mutex:
            prev = None
            s = self.sessions.get(skey)
            if s and not s.dead:
                # a session already covers this rendition; keep riding it unless
                # the request is a seek outside its forward window (back before
                # its start, or far past the read-ahead frontier)
                if s.lo <= idx <= s.hi + _SESS_LOOKAHEAD:
                    s.hi = max(s.hi, idx)
                    s.last_want = now
                    return
                self._reap(skey, s)
                prev = s
            elif s:
                self._reap(skey, s)
                prev = s

            if len(self.sessions) >= self.nthr:
                self._reap_lru()

            rdir = os.path.join(cachedir, str(height))
            s = _HlsSess(cachedir, rdir, ptop, rem, mtime, height, idx, now)
            s.prev = prev  # its ffmpeg may still be writing into rdir
            self.sessions[skey] = s
            Daemon(self._run_session, "hls-s%d-%d" % (height, idx), (skey, s))

    def _reap(self, skey: str, s: "_HlsSess") -> None:
        # caller holds self.mutex; flag the session dead and drop it from the
        # registry. its monitor thread notices `dead` and kills the ffmpeg
        s.dead = True
        if self.sessions.get(skey) is s:
            del self.sessions[skey]

    def _reap_lru(self) -> None:
        # caller holds self.mutex; free a slot by reaping the least-recently
        # wanted session (the stream that has gone quiet the longest)
        if not self.sessions:
            return
        skey = min(self.sessions, key=lambda k: self.sessions[k].last_want)
        self._reap(skey, self.sessions[skey])

    def _reaper(self) -> None:
        # stop sessions whose viewer has paused/closed/seeked away (no request
        # for vt_idle seconds); cached segments stay on disk for instant replay
        idle = max(5, int(getattr(self.args, "vt_idle", 20)))
        while not self.stopping:
            time.sleep(2)
            now = time.time()
            with self.mutex:
                for skey, s in list(self.sessions.items()):
                    if s.dead or now - s.last_want > idle:
                        self._reap(skey, s)

    def _run_session(self, skey: str, s: "_HlsSess") -> None:
        # session thread: probe once, spawn one forward-running ffmpeg (hls
        # muxer), then babysit it until it exits, is reaped, or the hub stops.
        # a runtime failure of a hw encoder gets one retry with libx264
        p = None
        killed = False
        try:
            vn = self._vn(s.ptop)
            resw, resh, hdr, _ = self._meta(s.cachedir, s.ptop, s.rem, vn)
            if s.height not in self._ladder(vn, resh):
                t = "rendition %dp is not in the ladder of this %dp source"
                raise Exception(t % (s.height, resh))
            chmod = bos.MKD_700 if self.args.free_umask else bos.MKD_755
            bos.makedirs(s.rdir, vf=chmod)
            _, oh = self._out_dims(resw, resh, s.height)
            enc = self._pick_enc(vn, oh)
            if s.prev is not None:
                # (seek) the session we replaced may still be encoding into
                # this rdir; wait until its ffmpeg is really gone so the two
                # never write the same .tmp segment
                s.prev.gone.wait(10)
                s.prev = None
            while True:
                argv = self._session_argv(s, vn, resw, resh, hdr, enc)
                if s.dead or self.stopping:
                    return

                p = self._spawn(argv)
                with self.mutex:
                    if s.dead:
                        killed = True
                        _hardkill(p)
                        return
                    s.proc = p

                rc = self._babysit(s, p)
                if rc is None:
                    killed = True  # reaped / seeked away / shutdown
                    return
                if not rc:
                    return  # reached EOF; the trailer finalized the last segment

                serr = "\n".join(p.errlog)[-2000:]  # type: ignore
                t = "ffmpeg failed (rc=%d) for %r h%d enc=%s:\n%s"
                self.log(t % (rc, s.rem, s.height, enc, serr or "(no stderr)"), 3)
                if TH_BWRAP:
                    bwrap_fail(serr)
                if enc == "x264":
                    return
                if not serr or ENC2FF[enc] in serr:
                    # the hw encoder itself is broken (no gpu/driver, busy,
                    # stub build); blacklist it hub-wide so other streams
                    # skip it too. anything else (corrupt source, disk full)
                    # is not the encoder's fault, so it stays available
                    t = "hw-encoder %s failed at runtime; disabling it"
                    self.log(t % (enc,), 3)
                    with self.mutex:
                        self.hw_bad.add(enc)
                enc = "x264"  # redo this session in software
                p = None
        except Exception:
            self.log("hls session failed for %r h%d:\n%s"
                     % (s.rem, s.height, min_ex()), 3)
        finally:
            if p is not None:
                try:
                    if p.poll() is None:
                        killed = True
                        _hardkill(p)
                except Exception:
                    pass
            if killed:
                _rm_tmp(s.rdir)
            with self.mutex:
                s.dead = True
                if self.sessions.get(skey) is s:
                    del self.sessions[skey]
            s.gone.set()

    def _babysit(self, s: "_HlsSess", p: Any) -> Optional[int]:
        # wait for the session's ffmpeg; returns its exit code, or None if we
        # had to kill it (session reaped/seeked away, or hub shutting down)
        while not self.stopping:
            rc = p.poll()
            if rc is not None:
                return rc
            if s.dead:
                _hardkill(p)
                return None
            _poke_dirs(s.rdir)  # keep the cache alive while encoding
            time.sleep(0.3)
        _hardkill(p)
        return None

    def _spawn(self, argv: list[bytes]) -> Any:
        # non-blocking ffmpeg launch (we keep the handle to kill on seek/idle);
        # mirrors the niceness/oom/cwd handling of runcmd. argv[0] is the
        # absolute path from mtag.HAVE_FFMPEG, so no CMD_EXEB .exe suffixing is
        # needed; cwd=SCWD (%systemroot% on windows) keeps ffmpeg from picking
        # up hostile dlls/binaries in the volume it is reading, same as runcmd
        ka: dict[str, Any] = {}
        if WINDOWS:
            ka["creationflags"] = 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
        elif NICEB:
            argv = [NICEB] + argv
        p = sp.Popen(argv, stdout=sp.DEVNULL, stderr=sp.PIPE, cwd=SCWD, **ka)
        p.errlog = []  # type: ignore
        Daemon(_drain, "hls-err", (p, p.errlog))  # type: ignore
        if not ANYWIN and not MACOS:
            try:
                with open("/proc/%d/oom_score_adj" % (p.pid,), "wb") as f:
                    f.write(b"300\n")
            except Exception:
                pass
        return p

    def _pick_enc(self, vn: "VFS", oh: int = 0) -> str:
        # honor the vt_enc flag/arg against the hub's validated hw set + the
        # runtime blacklist; anything unavailable safely resolves to libx264
        want = str(vn.flags.get("vt_enc", self.args.vt_enc) or "auto")
        good = [x for x in getattr(self.args, "vt_hwenc", []) if x not in self.hw_bad]
        if want == "x264":
            return "x264"
        if want == "auto":
            # conservative cross-platform default: hw-encoder quality varies a
            # lot by vendor/generation (some are notably worse per-bit than
            # x264), so only auto-pick hw when the frame is big enough that
            # software x264 might not sustain realtime (4K). good hw (e.g. apple
            # videotoolbox) benchmarks on par with x264 even at 1080p, so set
            # --vt-enc vt/nvenc to use it everywhere. oh=output height
            if oh > 1440 and good:
                return good[0]
            return "x264"
        return want if want in good else "x264"

    def _out_dims(self, resw: int, resh: int, height: int) -> tuple[int, int]:
        # output dims for a rendition: the target height clamped to the source,
        # width following to keep aspect (both even). used by the encoder pick
        # and the scale filter
        if not (resw and resh):
            return resw, resh
        oh = min(resh, height) if height else resh
        oh -= oh % 2
        ow = int(round(resw * oh / float(resh)))
        ow -= ow % 2
        return ow, oh

    def _session_argv(
        self, s: "_HlsSess", vn: "VFS", resw: int, resh: int, hdr: int, enc: str
    ) -> list[bytes]:
        # build the forward-running ffmpeg for a streaming session: one hls-muxer
        # encode from segment s.lo to EOF. -force_key_frames + -hls_time keep the
        # cuts on IDR frames exactly seg apart, and because it is a single encode
        # every segment boundary shares one encoder+audio state (seamless). the
        # temp_file flag makes each v%05d.ts appear atomically once finished, so
        # http-workers never read a half-written segment
        fl = vn.flags
        seg = float(fl.get("vt_seg", self.args.vt_seg)) or 4.0
        crf = int(fl.get("vt_vq", self.args.vt_vq))
        preset = str(fl.get("vt_preset", self.args.vt_preset))
        aq = int(fl.get("vt_aq", self.args.vt_aq))
        if preset not in X264_PRESETS:
            preset = "veryfast"

        ow, oh = self._out_dims(resw, resh, s.height)

        method = getattr(self.args, "vt_tm", "") or ""
        if str(fl.get("vt_tonemap", self.args.vt_tonemap) or "auto") in ("off", "n", "no"):
            method = ""

        dev: list[bytes] = []
        if hdr and method and ow and oh:
            # HDR source -> tonemap PQ/HLG BT.2020 down to SDR BT.709 (scales too)
            dev = tm_devargs(method)
            vf = tm_vf(method, ow, oh)
        else:
            # SDR (or tonemap unavailable/disabled): scale + 8-bit
            parts = []
            if ow and oh and (ow, oh) != (resw, resh):
                parts.append("scale=%d:%d" % (ow, oh))
            parts.append("format=yuv420p")
            vf = ",".join(parts)

        # realtime pacing so the session tracks the viewer instead of racing to
        # EOF; the initial burst fills the player's start buffer at full speed
        rate: list[bytes] = []
        rr = float(fl.get("vt_readrate", self.args.vt_readrate) or 0)
        rrlvl = int(getattr(self.args, "vt_rr", 0))
        if rr > 0 and rrlvl:
            rate = [b"-readrate", ("%g" % rr).encode("ascii")]
            if rrlvl >= 2:
                burst = max(seg * 4, 20.0)
                rate += [b"-readrate_initial_burst", ("%g" % burst).encode("ascii")]

        start = s.lo * seg
        bap_in = fsenc(os.path.join(s.ptop, s.rem))
        segpat = fsenc(os.path.join(s.rdir, "v%05d.ts"))
        livepl = fsenc(os.path.join(s.rdir, ".live.m3u8"))
        kf = ("expr:gte(t,n_forced*%g)" % seg).encode("ascii")

        # fmt: off
        argv = bwrap(ffmpeg_bin(), bap_in, segpat) + [
            b"-nostdin",
            b"-v", b"error",
            b"-hide_banner",
            b"-nostats",
        ] + dev + rate
        if s.lo:
            # seek start (a fresh session after a seek): fast input seek, then
            # place the output back at its true slot on the timeline
            argv += [b"-ss", ("%.3f" % start).encode("ascii")]
        argv += [b"-i", bap_in]
        if s.lo:
            argv += [b"-output_ts_offset", ("%.3f" % start).encode("ascii")]
        argv += [
            b"-map", b"0:v:0",
            b"-map", b"0:a:0?",
            b"-vf", vf.encode("ascii"),
        ] + enc_argv(enc, crf, preset, "high") + [
            b"-force_key_frames", kf,
            b"-pix_fmt", b"yuv420p",
            b"-c:a", b"aac",
            b"-b:a", ("%dk" % aq).encode("ascii"),
            b"-ac", b"2",
            b"-f", b"hls",
            b"-hls_time", ("%g" % seg).encode("ascii"),
            b"-hls_flags", b"independent_segments+temp_file",
            b"-hls_list_size", b"0",
            b"-hls_segment_type", b"mpegts",
            b"-start_number", ("%d" % s.lo).encode("ascii"),
            b"-hls_segment_filename", segpat,
            livepl,
        ]
        # fmt: on
        return argv


def _tagint(tags: dict, key: str) -> int:
    try:
        return int(tags[key][1])
    except Exception:
        return 0


def _poke_dirs(path: str, n: int = 4) -> None:
    now = time.time()
    for _ in range(n):
        try:
            os.utime(path, (now, now))
        except Exception:
            pass
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
