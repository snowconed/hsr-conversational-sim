"""One manual audio turn, relayed to OpenAI without exposing the API key.

The browser sends PCM16LE, mono, 24 kHz bytes, then {"type": "commit"}.
Transcription only fills an editable input; this module never generates or runs
a robot program. Audio and transcript text are not written to application logs.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import json
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from api_settings import realtime_model
from local_access import local_request_allowed


SAMPLE_RATE = 24000
MAX_SECONDS = 60
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS
MIN_AUDIO_BYTES = SAMPLE_RATE * 2 // 10
MAX_CHUNK_BYTES = 64 * 1024
CONNECT_TIMEOUT = 15
FINAL_TIMEOUT = 30
POLL_SECONDS = 0.1
DEFAULT_MODEL = "gpt-live-transcribe"


class TranscriptionError(Exception):
    """A deliberately public, sanitized error message."""


def transcription_model():
    return realtime_model()


def session_update(model):
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                "transcription": {
                    "model": model, "languages": ["ja"], "delay": "low",
                    "prompt": "家事ロボットの点検プログラムへの日本語の指示。",
                    "keywords": ["ボール", "箱", "点検", "持ち上げる", "うなずく"],
                },
                "turn_detection": None,
            }},
        },
    }


@asynccontextmanager
async def connect_upstream(api_key, model):
    """Injectable transport; model selection is sent in session.update."""
    from websockets.asyncio.client import connect

    async with connect(
        "wss://api.openai.com/v1/realtime?intent=transcription",
        additional_headers={"Authorization": "Bearer " + api_key},
        open_timeout=CONNECT_TIMEOUT, close_timeout=2,
        max_size=1024 * 1024, max_queue=16,
    ) as connection:
        yield connection


def _local_origin(websocket):
    """Keep the route safe even when installed without the main middleware."""
    return local_request_allowed(websocket.scope)


def _upstream_error(event):
    error = event.get("error") or {}
    code = error.get("code", "")
    if code in {"invalid_api_key", "invalid_authentication", "authentication_error"}:
        return TranscriptionError("OpenAI API キーを確認して、アプリを起動し直してください。")
    if code in {"insufficient_quota", "rate_limit_exceeded"}:
        return TranscriptionError("音声 API の利用上限に達しました。利用状況を確認してください。")
    if code in {"model_not_found", "model_not_available", "permission_denied"}:
        return TranscriptionError("設定したリアルタイム文字起こしモデルを利用できません。")
    return TranscriptionError("リアルタイム文字起こしでエラーが発生しました。再度お試しください。")


@dataclass
class _Turn:
    started: float = field(default_factory=time.monotonic)
    ready_at: float | None = None
    committed_at: float | None = None
    audio_bytes: int = 0
    upstream: object = None
    committed_id: str | None = None
    completions: dict = field(default_factory=dict)
    recorded_items: set = field(default_factory=set)
    transcript_chars: int = 0
    finalized: bool = False


async def _relay(websocket, app, model, api_key, factory, epoch):
    turn = _Turn()

    def still_current():
        if app.state.command_epoch != epoch:
            raise TranscriptionError("停止操作により音声入力を取り消しました。")

    async def send(payload):
        still_current()
        await websocket.send_json(payload)

    async def finish_if_ready():
        event = turn.completions.get(turn.committed_id)
        if turn.committed_at is None or event is None:
            return False
        text = event.get("transcript", "")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("音声から文字を読み取れませんでした。マイクを確認して、もう一度お話しください。")
        if len(text) > 4000:
            raise TranscriptionError("音声の指示が長すぎます。短く分けて入力してください。")
        await send({"type": "transcript.completed", "item_id": turn.committed_id, "text": text})
        turn.finalized = True
        return True

    async def upstream():
        async with factory(api_key, model) as connection:
            turn.upstream = connection
            await connection.send(json.dumps(session_update(model), ensure_ascii=False))
            while True:
                event = json.loads(await connection.recv())
                if not isinstance(event, dict):
                    raise TranscriptionError("音声 API から正しい応答を受信できませんでした。")
                still_current()
                event_type = event.get("type")
                if event_type == "error" or event_type == "conversation.item.input_audio_transcription.failed":
                    raise _upstream_error(event)
                if event_type == "session.updated" and turn.ready_at is None:
                    session = event.get("session", {})
                    if session.get("type") != "transcription":
                        raise TranscriptionError("文字起こし専用の接続を開始できませんでした。")
                    turn.ready_at = time.monotonic()
                    await send({
                        "type": "ready", "model": model,
                        "sample_rate": SAMPLE_RATE, "max_seconds": MAX_SECONDS,
                    })
                elif event_type == "input_audio_buffer.committed":
                    if turn.committed_at is None or turn.committed_id is not None:
                        raise TranscriptionError("音声の確定順序を確認できませんでした。")
                    turn.committed_id = event.get("item_id")
                    if not isinstance(turn.committed_id, str) or not turn.committed_id:
                        raise TranscriptionError("音声 API の確定結果を確認できませんでした。")
                    if await finish_if_ready():
                        return
                elif event_type == "conversation.item.input_audio_transcription.delta":
                    delta, item_id = event.get("delta"), event.get("item_id")
                    if not isinstance(delta, str) or not isinstance(item_id, str):
                        raise TranscriptionError("音声 API の文字データを確認できませんでした。")
                    turn.transcript_chars += len(delta)
                    if turn.transcript_chars > 8000:
                        raise TranscriptionError("音声の指示が長すぎます。短く分けて入力してください。")
                    await send({"type": "transcript.delta", "item_id": item_id, "delta": delta})
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    item_id = event.get("item_id")
                    if not isinstance(item_id, str):
                        raise TranscriptionError("音声 API の確定結果を確認できませんでした。")
                    if item_id not in turn.recorded_items:
                        # Only genuine completion events are passed to accounting;
                        # the logger must whitelist usage/model/tier, not transcript.
                        app.state.usage.record_realtime(
                            model, event, round((time.monotonic() - turn.started) * 1000)
                        )
                        turn.recorded_items.add(item_id)
                    if len(turn.recorded_items) > 4:
                        raise TranscriptionError("音声 API から予期しない複数の結果を受信しました。")
                    turn.completions[item_id] = event
                    if await finish_if_ready():
                        return

    async def browser():
        while True:
            message = await websocket.receive()
            still_current()
            if message["type"] == "websocket.disconnect":
                return
            chunk = message.get("bytes")
            if chunk is not None:
                if turn.ready_at is None:
                    raise TranscriptionError("音声入力の接続が完了するまでお待ちください。")
                if turn.committed_at is not None:
                    raise TranscriptionError("音声を確定した後には追加できません。もう一度録音してください。")
                if not chunk or len(chunk) % 2 or len(chunk) > MAX_CHUNK_BYTES:
                    raise TranscriptionError("音声データの形式を確認できませんでした。")
                turn.audio_bytes += len(chunk)
                if turn.audio_bytes > MAX_AUDIO_BYTES:
                    raise TranscriptionError("音声入力は 60 秒までです。短く分けて入力してください。")
                await turn.upstream.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }))
                continue
            raw = message.get("text", "")
            if len(raw) > 128:
                raise TranscriptionError("音声入力の操作を確認できませんでした。")
            try:
                command = json.loads(raw)
            except (ValueError, TypeError):
                raise TranscriptionError("音声入力の操作を確認できませんでした。") from None
            if not isinstance(command, dict) or set(command) != {"type"}:
                raise TranscriptionError("音声入力の操作を確認できませんでした。")
            if command["type"] == "cancel":
                return
            if command["type"] != "commit" or turn.ready_at is None or turn.committed_at is not None:
                raise TranscriptionError("音声入力の操作順序を確認できませんでした。")
            if turn.audio_bytes < MIN_AUDIO_BYTES:
                raise TranscriptionError("録音が短すぎます。マイクを確認して、もう一度お話しください。")
            turn.committed_at = time.monotonic()
            await turn.upstream.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def watchdog():
        while True:
            still_current()
            now = time.monotonic()
            if turn.ready_at is None and now - turn.started > CONNECT_TIMEOUT:
                raise TranscriptionError("音声 API への接続がタイムアウトしました。")
            if turn.committed_at is not None:
                if now - turn.committed_at > FINAL_TIMEOUT:
                    raise TranscriptionError("文字起こしの確定がタイムアウトしました。もう一度お試しください。")
            elif turn.ready_at is not None and now - turn.ready_at > MAX_SECONDS + 2:
                raise TranscriptionError("音声入力は 60 秒までです。短く分けて入力してください。")
            await asyncio.sleep(POLL_SECONDS)

    tasks = [asyncio.create_task(worker()) for worker in (upstream, browser, watchdog)]
    try:
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return turn.finalized


def install_transcription_routes(app: FastAPI, connect_factory=None):
    """Install route; inject an async transport factory for tests/live probes."""
    @app.websocket("/api/programs/transcription")
    async def transcription(websocket: WebSocket):
        if not _local_origin(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        locked = False
        finalized = False
        epoch = app.state.command_epoch
        try:
            if not os.environ.get("OPENAI_API_KEY"):
                raise TranscriptionError("OPENAI_API_KEY を設定してアプリを起動し直してください。")
            if not hasattr(app.state, "voice_lock"):
                app.state.voice_lock = asyncio.Lock()
            if (
                app.state.voice_lock.locked()
                or app.state.interpret_lock.locked()
                or app.state.program_runner.snapshot().get("running")
                or app.state.controller.get_state().get("busy")
            ):
                raise TranscriptionError("ほかの処理が終わってから音声入力を始めてください。")
            # No intervening await between the busy check and acquiring a free
            # asyncio.Lock: one voice turn owns this slot on the server loop.
            await app.state.voice_lock.acquire()
            locked = True
            app.state.voice_active = True
            finalized = await _relay(
                websocket, app, transcription_model(), os.environ["OPENAI_API_KEY"],
                connect_factory or connect_upstream, epoch,
            )
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as error:
            message = str(error) if isinstance(error, TranscriptionError) else (
                "音声 API との接続が切れました。ネットワークと API の設定を確認してください。"
            )
            with suppress(Exception):
                await websocket.send_json({"type": "error", "message": message})
        finally:
            if locked:
                app.state.voice_active = False
                app.state.voice_lock.release()
            # The UI may enable generation immediately on done. Release the
            # server slot first so that click cannot race transport cleanup.
            if finalized:
                with suppress(Exception):
                    await websocket.send_json(
                        {"type": "done"} if app.state.command_epoch == epoch else {
                            "type": "error", "message": "停止操作により音声入力を取り消しました。",
                        }
                    )
            with suppress(Exception):
                await websocket.close()
