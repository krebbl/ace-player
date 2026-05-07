import asyncio
import os
import httpx

ACE_ENGINE_URL = os.environ.get("ACE_ENGINE_URL", "http://acestream:6878").rstrip("/")
START_RETRIES = 3
START_BACKOFF_S = [2, 5, 10]


async def start(content_id: str) -> dict:
    """Resolve a content_id to a playable URL via the engine HTTP API.

    See https://wiki.acestream.media/Engine_HTTP_API
    """
    url = f"{ACE_ENGINE_URL}/ace/manifest.m3u8"
    params = {"id": content_id, "format": "json", "use_api_events": "1"}
    last_err = None
    for attempt, backoff in enumerate(START_BACKOFF_S[:START_RETRIES]):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                body = r.json()
            if "response" in body:
                body = body["response"]
            if "playback_url" not in body:
                raise RuntimeError(f"engine response missing playback_url: {body}")
            return body
        except Exception as e:
            last_err = e
            if attempt < START_RETRIES - 1:
                await asyncio.sleep(backoff)
    raise RuntimeError(f"engine start failed after {START_RETRIES} attempts: {last_err}")


async def stop(command_url: str) -> None:
    if not command_url:
        return
    sep = "&" if "?" in command_url else "?"
    url = f"{command_url}{sep}method=stop"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(url)
    except Exception:
        pass


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{ACE_ENGINE_URL}/webui/api/service?method=get_version")
            return r.status_code == 200
    except Exception:
        return False
