import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field

import acestream

log = logging.getLogger("hls")

HLS_DIR = os.environ.get("HLS_DIR", "/tmp/hls")
HLS_IDLE_TIMEOUT = int(os.environ.get("HLS_IDLE_TIMEOUT", "120"))
PLAYLIST_NAME = "index.m3u8"


@dataclass
class Stream:
    content_id: str
    proc: asyncio.subprocess.Process
    command_url: str
    last_access: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)


class HLSManager:
    def __init__(self):
        self._streams: dict[str, Stream] = {}
        self._lock = asyncio.Lock()
        self._gc_task: asyncio.Task | None = None

    async def start(self):
        os.makedirs(HLS_DIR, exist_ok=True)
        # Wipe any leftover segment dirs from prior boots.
        for entry in os.listdir(HLS_DIR):
            shutil.rmtree(os.path.join(HLS_DIR, entry), ignore_errors=True)
        self._gc_task = asyncio.create_task(self._gc_loop())

    async def shutdown(self):
        if self._gc_task:
            self._gc_task.cancel()
        async with self._lock:
            ids = list(self._streams.keys())
        for cid in ids:
            await self.stop(cid)

    def stream_dir(self, content_id: str) -> str:
        return os.path.join(HLS_DIR, content_id)

    def playlist_path(self, content_id: str) -> str:
        return os.path.join(self.stream_dir(content_id), PLAYLIST_NAME)

    async def ensure(self, content_id: str) -> str:
        async with self._lock:
            existing = self._streams.get(content_id)
            if existing and existing.proc.returncode is None:
                existing.last_access = time.time()
                return self.playlist_path(content_id)
            if existing:
                self._streams.pop(content_id, None)

            info = await acestream.start(content_id)
            playback_url = info["playback_url"]
            command_url = info.get("command_url", "")

            out_dir = self.stream_dir(content_id)
            os.makedirs(out_dir, exist_ok=True)
            for f in os.listdir(out_dir):
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass

            # Re-encode video to H.264 main/4.0 + yuv420p so iPhone Safari
            # plays everything (the source might be MPEG-2 or HEVC, neither
            # of which Safari decodes inside an HLS .ts segment). Audio is
            # always re-muxed to AAC stereo — many IPTV sources ship
            # AC3/E-AC3/MP2 which browsers can't decode in HLS either.
            #
            # CPU cost: ~50-100 % of one core per 1080p stream at veryfast.
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-fflags", "+genpts+discardcorrupt",
                "-i", playback_url,
                "-map", "0:v:0?",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-profile:v", "main",
                "-level", "4.0",
                "-pix_fmt", "yuv420p",
                "-g", "96",
                "-sc_threshold", "0",
                "-c:a", "aac",
                "-ac", "2",
                "-b:a", "128k",
                "-f", "hls",
                "-hls_time", "4",
                "-hls_list_size", "6",
                "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
                "-hls_segment_filename", os.path.join(out_dir, "seg_%05d.ts"),
                self.playlist_path(content_id),
            ]
            log.info("starting ffmpeg for %s", content_id)
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            self._streams[content_id] = Stream(
                content_id=content_id, proc=proc, command_url=command_url
            )

        return self.playlist_path(content_id)

    async def wait_for_playlist(self, content_id: str, timeout: float = 30.0) -> bool:
        path = self.playlist_path(content_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
            await asyncio.sleep(0.5)
        return False

    def touch(self, content_id: str):
        s = self._streams.get(content_id)
        if s:
            s.last_access = time.time()

    async def stop(self, content_id: str):
        async with self._lock:
            s = self._streams.pop(content_id, None)
        if not s:
            return
        log.info("stopping ffmpeg for %s", content_id)
        if s.proc.returncode is None:
            try:
                s.proc.terminate()
                try:
                    await asyncio.wait_for(s.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    s.proc.kill()
                    await s.proc.wait()
            except ProcessLookupError:
                pass
        await acestream.stop(s.command_url)
        shutil.rmtree(self.stream_dir(content_id), ignore_errors=True)

    async def _gc_loop(self):
        try:
            while True:
                await asyncio.sleep(15)
                now = time.time()
                async with self._lock:
                    expired = [
                        cid for cid, s in self._streams.items()
                        if now - s.last_access > HLS_IDLE_TIMEOUT
                    ]
                for cid in expired:
                    log.info("idle gc: stopping %s", cid)
                    await self.stop(cid)
        except asyncio.CancelledError:
            pass

    def active(self) -> list[dict]:
        now = time.time()
        return [
            {
                "content_id": cid,
                "uptime_s": int(now - s.started_at),
                "idle_s": int(now - s.last_access),
            }
            for cid, s in self._streams.items()
        ]


manager = HLSManager()
