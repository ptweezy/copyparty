#!/usr/bin/env python3
# sloppyparty: live smoke test of the on-the-fly HLS video transcoder.
#
# Builds a 40s 720p fixture with ffmpeg, starts the server (sloppyparty.conf) on
# the given port, and walks the whole chain like a browser would: the page flags
# (have_vcode, vcode_exts) -> master playlist -> 480/720 rendition playlists ->
# segments (ffprobed: h264 + aac at the right size) -> forward/backward seeks
# (every segment must be a full 4s one) -> bogus renditions / shapes / indexes
# (fast 404s) -> plain 404 paths. Prints RESULT: PASS or FAIL plus the filtered
# server log. Needs ffmpeg+ffprobe with libx264 and aac on PATH.
#
# Run this after every upstream merge: the fork's HLS code consumes upstream
# APIs that have changed without any merge conflict before (see tests/test_hls.py
# for the unit-level contracts).
#
# Usage: python packaging/ci/e2e-hls.py <dir-for-server-log> <port>
#   (pick a port above 8000; the fixture and cache are deleted afterwards)
import os, re, shutil, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
SP = sys.argv[1]; port = int(sys.argv[2]) if len(sys.argv) > 2 else 18924
ff = shutil.which("ffmpeg"); fp = shutil.which("ffprobe")
assert ff and fp, "ffmpeg/ffprobe not on PATH"
tmp = tempfile.mkdtemp(prefix="sp-hls-")
vid = os.path.join(tmp, "clip.mp4")
with open(os.path.join(tmp, "hello.txt"), "w") as f: f.write("hi\n")
subprocess.check_call([ff, "-v", "error", "-y",
    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=40",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", vid])
print("fixture:", vid, os.path.getsize(vid), "bytes")
logp = os.path.join(SP, "hls-server.log"); log = open(logp, "w", encoding="utf-8")
cmd = [sys.executable, "-m", "copyparty", "-c", "sloppyparty.conf", "-i", "127.0.0.1", "-p", str(port), "-v", tmp + "::r"]
p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUNBUFFERED="1"))
ok = False
for _ in range(600):
    if p.poll() is not None: break
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); ok = True; break
    except OSError: time.sleep(0.1)
print("server listening:", ok, "| early exit rc:", p.poll())
base = "http://127.0.0.1:%d" % port
fails = []
def get(path, timeout=90, want=200):
    try:
        r = urllib.request.urlopen(base + path, timeout=timeout)
        return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()
def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond: fails.append(msg)
try:
    if ok:
        t0 = time.time()
        st, ct, body = get("/")
        html = body.decode("utf-8", "replace")
        m = re.search(r'"have_vcode":\s*(true|false)', html)
        check(m is not None and m.group(1) == "true", "browser page advertises have_vcode=true (got %r)" % (m.group(1) if m else None))
        check('"vcode_exts"' in html, "browser page carries the transcodable extension list (vcode_exts)")

        st, ct, body = get("/clip.mp4/.hls/master.m3u8")
        check(st == 200, "master.m3u8 -> %d %s (%.1fs)" % (st, ct, time.time() - t0))
        check("mpegurl" in ct, "master.m3u8 content-type is mpegurl (%s)" % ct)
        master = body.decode("utf-8", "replace")
        rends = [ln for ln in master.splitlines() if ln and not ln.startswith("#")]
        print("    master renditions:", rends)
        check(rends == ["480/index.m3u8", "720/index.m3u8"], "abr ladder for a 720p source is 480 + 720")

        for h in ("720", "480"):
            t1 = time.time()
            st, ct, body = get("/clip.mp4/.hls/%s/index.m3u8" % h)
            pl = body.decode("utf-8", "replace")
            segs = [ln for ln in pl.splitlines() if ln and not ln.startswith("#")]
            check(st == 200 and "#EXT-X-ENDLIST" in pl, "%sp playlist -> %d, VOD complete, %d segments %s (%.1fs)" % (h, st, len(segs), segs, time.time() - t1))
            check(len(segs) == 10, "%sp: 40s / 4s segments = 10 segments" % h)
            for seg in segs[:3]:
                t2 = time.time()
                st, ct, body = get("/clip.mp4/.hls/%s/%s" % (h, seg))
                check(st == 200 and len(body) > 10000, "%sp %s -> %d, %d bytes (%.1fs)" % (h, seg, st, len(body), time.time() - t2))
                sp_ = os.path.join(tmp, "seg_%s_%s" % (h, seg))
                with open(sp_, "wb") as f: f.write(body)
                probe = subprocess.run([fp, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "compact=p=0:nk=1", sp_], capture_output=True, text=True).stdout.strip().splitlines()
                print("    ffprobe %s/%s: %s" % (h, seg, probe))
                vline = [x for x in probe if "|video|" in x]
                aline = [x for x in probe if "|audio" in x]
                check(vline and "h264" in vline[0] and vline[0].endswith("|" + h), "%s/%s video is h264 at %sp" % (h, seg, h))
                check(aline and "aac" in aline[0], "%s/%s audio is aac" % (h, seg))

        # seek far ahead (new session), ride it, then seek back (a replacement
        # session that must wait for the previous one to be gone); every
        # segment must be a full 4s one, never a truncated leftover
        for seg, note in (("v00007.ts", "forward seek -> new session"), ("v00008.ts", "rides the seek session"), ("v00005.ts", "backward seek -> replacement session"), ("v00001.ts", "cached from the first session")):
            t5 = time.time()
            st, ct, body = get("/clip.mp4/.hls/720/" + seg)
            check(st == 200 and len(body) > 10000, "720p %s (%s) -> %d, %d bytes (%.1fs)" % (seg, note, st, len(body), time.time() - t5))
            sp_ = os.path.join(tmp, "seek_" + seg)
            with open(sp_, "wb") as f: f.write(body)
            dur = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", sp_], capture_output=True, text=True).stdout.strip()
            try: dur = float(dur)
            except Exception: dur = -1
            check(3.5 <= dur <= 4.6, "720p %s duration %.2fs (a full 4s segment, not truncated)" % (seg, dur))
        # bogus renditions / shapes are refused fast, real ones still work
        for path, want in (("/clip.mp4/.hls/0720/index.m3u8", 404), ("/clip.mp4/.hls/999/index.m3u8", 404), ("/clip.mp4/.hls/1080/v00000.ts", 404), ("/clip.mp4/.hls/480/v00000.ts", 200)):
            t6 = time.time()
            st, ct, body = get(path)
            check(st == want and time.time() - t6 < 10, "%s -> %d (want %d, %.1fs)" % (path, st, want, time.time() - t6))

        st, ct, body = get("/hello.txt/.hls/master.m3u8")
        check(st == 404, "non-video extension -> 404 not 500 (got %d)" % st)
        st, ct, body = get("/nope.mp4/.hls/master.m3u8")
        check(st == 404, "missing video -> 404 (got %d)" % st)
        t3 = time.time()
        try:
            st, ct, body = get("/clip.mp4/.hls/720/v99999.ts", timeout=120)
            print("    out-of-range segment -> %d after %.1fs" % (st, time.time() - t3))
            check(st == 404 and time.time() - t3 < 5, "out-of-range segment -> immediate 404 (got %d)" % st)
        except Exception as ex:
            print("    out-of-range segment -> %r after %.1fs" % (ex, time.time() - t3))
            check(False, "out-of-range segment: connection dropped: %r" % (ex,))
        t4 = time.time()
        st, ct, body = get("/clip.mp4/.hls/720/v00000.ts")
        check(st == 200 and len(body) > 10000, "cached segment still served after the bad seek -> %d (%.1fs)" % (st, time.time() - t4))
finally:
    p.terminate()
    try: p.wait(timeout=10)
    except subprocess.TimeoutExpired: p.kill(); p.wait()
    log.close()
print("--- server log (filtered) ---")
for line in open(logp, encoding="utf-8", errors="replace"):
    if re.search(r"hls|vcode|transcod|ffmpeg|error|traceback|warn|listening", line, re.I) and "quickedit" not in line.lower():
        print("  " + line.rstrip()[:200])
print("RESULT:", "PASS" if not fails else "FAIL (%d)" % len(fails))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
