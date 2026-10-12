"""Explicit Microsoft speech backends; provider credentials never fall back."""

import os
import re
import time
from xml.sax.saxutils import escape, quoteattr

import httpx


def azure_synthesize(text, target, cfg):
    key = os.getenv('AZURE_SPEECH_KEY')
    region = cfg.azure_speech_region
    if not key or not region:
        raise ValueError('azure配音需要AZURE_SPEECH_KEY和AZURE_SPEECH_REGION；不能使用模型密钥替代')
    if not isinstance(region,str) or not re.fullmatch(r"[a-z0-9-]+",region):
        raise ValueError("Azure语音区域格式无效，不能发送密钥到任意主机")
    # Never accept an arbitrary credential host, even after model_copy updates.
    url = f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1'
    locale = '-'.join(cfg.voice.split('-')[:2])
    ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang={quoteattr(locale)}>'
            f'<voice name={quoteattr(cfg.voice)}><prosody rate={quoteattr(cfg.tts_rate)}>'
            f'{escape(text)}</prosody></voice></speak>')
    headers = {'Ocp-Apim-Subscription-Key': key, 'Content-Type': 'application/ssml+xml',
               'X-Microsoft-OutputFormat': 'audio-24khz-48kbitrate-mono-mp3', 'User-Agent': 'autoclip-director/1.1'}
    for attempt in range(3):
        try:
            response = httpx.post(url, content=ssml.encode('utf-8'), headers=headers, timeout=90, follow_redirects=False)
            if response.status_code == 200 and response.content:
                target.write_bytes(response.content)
                return
            if response.status_code not in (429, 500, 502, 503, 504):
                raise ValueError(f'Azure配音HTTP {response.status_code}；检查语音资源、区域、权限或余额')
        except httpx.TransportError:
            if attempt == 2: raise RuntimeError('Azure配音网络请求失败，已重试') from None
        if attempt < 2: time.sleep(2**attempt)
    raise RuntimeError('Azure配音服务限流或不可用，已重试；不自动更换后端')


# edge-tts imports its endpoint into communicate.py. Scope the override and
# serialize it so concurrent jobs cannot change each other's endpoint.
from threading import RLock
_EDGE_LOCK = RLock()


def edge_synthesize(text, target, boundaries, cfg):
    import asyncio
    import edge_tts
    import edge_tts.communicate as communicate
    from edge_tts.constants import TRUSTED_CLIENT_TOKEN
    endpoints = {
        "bing": "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?TrustedClientToken=",
        "msedge": "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1?Ocp-Apim-Subscription-Key=",
    }
    if cfg.edge_endpoint not in endpoints:
        raise ValueError("不支持的Edge配音接口")
    # This token comes from the public library, never from the user's .env.
    endpoint = endpoints[cfg.edge_endpoint] + TRUSTED_CLIENT_TOKEN

    async def speak():
        for attempt in range(3):
            try:
                await edge_tts.Communicate(text, cfg.voice, rate=cfg.tts_rate).save(str(target), str(boundaries))
                return
            except Exception as ex:
                target.unlink(missing_ok=True)
                boundaries.unlink(missing_ok=True)
                if attempt == 2:
                    code = getattr(ex, "status", None)
                    raise RuntimeError(f"Edge配音服务失败（{type(ex).__name__}，HTTP {code or '未知'}）；重试或明确选择azure/macos。") from None
                await asyncio.sleep(2 ** attempt)

    with _EDGE_LOCK:
        previous = communicate.WSS_URL
        try:
            communicate.WSS_URL = endpoint
            asyncio.run(speak())
        finally:
            communicate.WSS_URL = previous
