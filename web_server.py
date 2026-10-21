"""Local Japanese command UI. Run in the simulation's WSL environment.

    python web_server.py

OPENAI_API_KEY stays in the server process. OPENAI_MODEL optionally changes the
interpreter (default gpt-4.1-mini); OPENAI_TRANSCRIBE_MODEL defaults to
gpt-4o-mini-transcribe. Standard buttons and exact common Japanese commands do
not call an API. Audio is transcribed and then treated as a text command.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Literal
import unicodedata

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from program_runtime import ProgramRunner
from program_service import ProgramStore, install_program_routes
from api_settings import program_settings, realtime_model
from realtime_transcription import install_transcription_routes
from conversation_service import ConversationStore, install_conversation_routes
from local_access import LocalAccessMiddleware


ROOT = Path(__file__).resolve().parent
PROGRAM_ROOT = ROOT / "programs"
Action = Literal[
    "greet", "nod", "look_ball", "open_hand", "close_hand", "raise_arm",
    "home", "pick_cube", "place_cube", "pick_ball", "place_ball",
    "forward", "backward", "turn_left", "turn_right", "stop",
]
ACTION_LABELS = {
    "greet": "あいさつ", "nod": "うなずく", "look_ball": "ボールを見る",
    "open_hand": "手を開く", "close_hand": "手を閉じる", "raise_arm": "腕を上げる",
    "home": "基本姿勢", "pick_cube": "箱を持ち上げる", "place_cube": "箱を置く",
    "pick_ball": "ボールを持ち上げる", "place_ball": "ボールを置く",
    "forward": "少し前進", "backward": "少し後退",
    "turn_left": "少し左旋回", "turn_right": "少し右旋回", "stop": "停止",
}
MAX_AUDIO_BYTES = 6 * 1024 * 1024


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)


def normalize(text: str) -> str:
    return re.sub(r"[\s。、！!？?]+", "", unicodedata.normalize("NFKC", text)).lower()


_COMMANDS = {
    "greet": ["こんにちは", "挨拶", "あいさつ", "挨拶して", "あいさつして", "手を振って"],
    "nod": ["うなずいて", "頷いて", "うなずく"],
    "look_ball": ["ボールを見て", "青いボールを見て", "ボールを見る"],
    "open_hand": ["手を開いて", "手を開く", "グリッパーを開いて"],
    "close_hand": ["手を閉じて", "手を閉じる", "グリッパーを閉じて"],
    "raise_arm": ["腕を上げて", "腕を上げる", "手を上げて"],
    "home": ["基本姿勢", "基本姿勢に戻って", "元の姿勢に戻って", "ホーム"],
    "pick_cube": ["箱を持ち上げて", "赤い箱を持ち上げて", "箱を持って", "箱を拾って", "箱をつかんで", "キューブを持ち上げて"],
    "place_cube": ["箱を置いて", "赤い箱を置いて", "箱を離して", "キューブを置いて"],
    "pick_ball": ["ボールを持ち上げて", "ボールを持って", "ボールを拾って", "ボールをつかんで", "青いボールを持ち上げて"],
    "place_ball": ["ボールを置いて", "ボールを離して", "青いボールを置いて"],
    "forward": ["前進", "前進して", "少し前進", "少し前に進んで", "前に進んで"],
    "backward": ["後退", "後退して", "少し後退", "少し後ろに下がって", "後ろに下がって"],
    "turn_left": ["左旋回", "左に回って", "少し左を向いて", "左を向いて"],
    "turn_right": ["右旋回", "右に回って", "少し右を向いて", "右を向いて"],
    "stop": ["停止", "止まって", "とまって", "ストップ", "中止", "やめて", "stop"],
}
EXACT_COMMANDS = {normalize(text): action for action, examples in _COMMANDS.items() for text in examples}
EXACT_COMMANDS.update({normalize(label): action for action, label in ACTION_LABELS.items()})


def local_action(text: str) -> str | None:
    """Exact matches only: e.g. '前に進まないで' must never trigger forward."""
    cleaned = normalize(text)
    if cleaned.endswith("ください"):
        cleaned = cleaned[:-4]
    return EXACT_COMMANDS.get(cleaned)


def _json_value(value):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class ApiUsage:
    def __init__(self):
        self.lock = threading.Lock()
        self.recent: list[dict] = []
        self.path = ROOT / "logs" / "api_usage.jsonl"

    def record(self, endpoint: str, requested_model: str, response=None, error=None,
               requested_service_tier=None, elapsed_ms=None):
        # Do not log prompts, audio, API keys, or exception messages.
        tier = requested_service_tier or ("default" if endpoint == "responses" else None)
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "requested_model": requested_model,
            "returned_model": getattr(response, "model", None),
            "requested_service_tier": tier,
            "returned_service_tier": getattr(response, "service_tier", None),
            "service_tier_note": (
                f"requested_{tier}; missing response field means unreported"
                if endpoint == "responses" else
                "transcription endpoint has no service_tier parameter; response may not report tier"
            ),
            "usage": _json_value(getattr(response, "usage", None)),
            "request_id": getattr(response, "_request_id", None),
            "status": "error" if error else getattr(response, "status", "completed"),
            "error_type": type(error).__name__ if error else None,
            "elapsed_ms": elapsed_ms,
        }
        self._append(record)

    def record_realtime(self, model, event, elapsed_ms):
        # Only upstream completed events reach this path, never browser claims.
        self._append({
            "time": datetime.now(timezone.utc).isoformat(),
            "endpoint": "realtime/transcription",
            "requested_model": model,
            "returned_model": event.get("model"),
            "requested_service_tier": None,
            "returned_service_tier": event.get("service_tier"),
            "service_tier_note": "transcription does not request a service tier; missing fields mean unreported",
            "usage": event.get("usage"),
            "status": "completed",
            "item_id": event.get("item_id"),
            "elapsed_ms": elapsed_ms,
        })

    def _append(self, record):
        with self.lock:
            self.recent.append(record)
            self.recent = self.recent[-20:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")

    def snapshot(self):
        with self.lock:
            return list(self.recent)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("OPENAI_API_KEY"):
        # Loading many SDK modules from /mnt/c competes with the ROS callback
        # thread for Python execution. Finish it before starting that thread.
        logging.getLogger("uvicorn.error").info("Preparing API libraries before robot connections.")
        await run_in_threadpool(warm_api_sdk)
    logging.getLogger("uvicorn.error").info("Preparing robot connections.")
    # Lazy import lets validation/API tests inject a harmless fake controller.
    from household_controller import HouseholdController

    app.state.controller = HouseholdController()
    app.state.command_epoch = 0
    app.state.interpret_lock = asyncio.Lock()
    app.state.voice_lock = asyncio.Lock()
    app.state.voice_active = False
    app.state.usage = ApiUsage()
    app.state.program_store = ProgramStore(PROGRAM_ROOT)
    app.state.conversation_store = ConversationStore(PROGRAM_ROOT / "conversations")
    app.state.program_runner = ProgramRunner(app.state.controller)
    app.state.program_revision_id = None
    try:
        yield
    finally:
        await run_in_threadpool(app.state.program_runner.close)
        await run_in_threadpool(app.state.controller.close)


app = FastAPI(title="家事ロボットの音声操作", lifespan=lifespan)
app.add_middleware(LocalAccessMiddleware)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/state")
async def state():
    current = await run_in_threadpool(app.state.controller.get_state)
    return {
        **current,
        "command_epoch": app.state.command_epoch,
        "voice_active": app.state.voice_active,
        "program": {
            **app.state.program_runner.snapshot(),
            "revision_id": app.state.program_revision_id,
        },
        "api": {
            "configured": bool(os.environ.get("OPENAI_API_KEY")),
            "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            "program_model": program_settings()["model"],
            "program_service_tier": program_settings()["service_tier"],
            "program_reasoning_effort": program_settings()["reasoning_effort"],
            "realtime_transcribe_model": realtime_model(),
            "transcribe_model": os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
            "service_tier": "default",
            "recent_usage": app.state.usage.snapshot(),
        },
    }


async def dispatch(action: str, epoch: int | None = None):
    if action not in ACTION_LABELS:
        raise HTTPException(400, "実行できない動作です。")
    if action == "stop":
        app.state.command_epoch += 1
        return app.state.program_runner.stop()
    if epoch is not None and epoch != app.state.command_epoch:
        return {"accepted": False, "action": action, "message": "停止操作により、解釈中だった指示を取り消しました。"}
    if app.state.voice_active:
        return {"accepted": False, "action": action, "message": "音声入力を終了してからロボットを操作してください。"}
    # These controller methods are fast queue operations. Keep the epoch check
    # and submission in one event-loop turn so a stop cannot slip between them.
    return app.state.program_runner.submit_manual(action)


@app.post("/api/action")
async def action(command: ActionInput):
    return await dispatch(command.action)


def warm_api_sdk():
    from openai import AsyncOpenAI
    from openai.resources.responses import AsyncResponses  # noqa: F401
    return AsyncOpenAI


def client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(503, "プログラム生成・音声認識にはOPENAI_API_KEYが必要です。環境変数を設定してアプリを起動し直してください。保存済みプログラムとボタンはAPIキーなしで使えます。")
    AsyncOpenAI = warm_api_sdk()
    # The program authoring route explicitly selects its own model and tier.
    # No hidden retries.
    return AsyncOpenAI(timeout=40.0, max_retries=0)


async def interpret_text(text: str, epoch: int):
    text = text.strip()
    if not text:
        raise HTTPException(400, "指示を入力してください。")
    matched = local_action(text)
    if matched:
        result = await dispatch(matched, epoch)
        return {**result, "text": text, "interpreted_action": matched, "source": "local"}
    if app.state.program_runner.snapshot()["running"]:
        return {
            "accepted": False, "text": text, "action": None, "source": "local",
            "message": "プログラムの実行が終了してから単発の指示を入力してください。停止ボタンはいつでも使えます。",
        }
    if not os.environ.get("OPENAI_API_KEY"):
        return {
            "accepted": False, "text": text, "action": None, "source": "local",
            "message": "動作を特定できませんでした。「箱を持ち上げて」「こんにちは」など、一つの動作を指定してください。",
        }
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    current = await run_in_threadpool(app.state.controller.get_state)
    tool = {
        "type": "function", "name": "select_robot_action",
        "description": "明確に要求された一つの登録済み動作を選ぶ。不明・複数の動作・非対応の指示ではnoneを選ぶ。",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTION_LABELS) + ["none"]},
                "message": {"type": "string", "description": "選んだ動作の短い説明、または確認質問。実行成功は宣言しない。"},
            },
            "required": ["action", "message"], "additionalProperties": False,
        },
    }
    try:
        async with client() as api:
            response = await api.responses.create(
                model=model, service_tier="default", store=False,
                instructions=(
                    "あなたはPC内シミュレーションの一動作選択器です。"
                    "ユーザーが今要求した動作だけを一つ選択してください。コード生成・実行は行いません。"
                    "複数段階の指示、曖昧な指示、否定文、質問だけ、条件付き動作、"
                    "未対応の距離や角度、じゃんけんなど未実装の動作はnoneとして確認してください。"
                    "ballは青い登録済みボール、cubeは赤い登録済みの箱です。色や種類が一致しない対象を要求されたらnoneで確認してください。"
                    "pickは登録済み位置の対象を補助付き把持で持ち上げる操作、placeは保持中の対象を定位置に置く操作です。"
                    "カメラで認識したとは言わないでください。homeは基本姿勢であり出発地点への移動ではありません。"
                    "forward/backward/turnはプログラム既定量の短い移動です。距離・角度を推測して実行しません。"
                    "成功した、完了した、つかめたという報告はしません。"
                    "動作一覧: " + json.dumps(ACTION_LABELS, ensure_ascii=False)
                    + "\n現在の保持対象: " + json.dumps(current.get("holding"), ensure_ascii=False)
                ),
                input=text,
                tools=[tool],
                tool_choice={"type": "function", "name": "select_robot_action"},
                parallel_tool_calls=False, max_output_tokens=220,
            )
    except HTTPException:
        raise
    except Exception as exc:
        app.state.usage.record("responses", model, error=exc)
        raise HTTPException(502, "指示の解釈に失敗しました。動作は開始していません。ボタンか定型文で試してください。") from None
    app.state.usage.record("responses", model, response=response)
    if response.status != "completed":
        raise HTTPException(502, "解釈が完了しなかったため、動作を開始していません。")
    calls = [item for item in response.output if item.type == "function_call" and item.name == "select_robot_action"]
    try:
        if len(calls) != 1:
            raise ValueError("one call required")
        selected = json.loads(calls[0].arguments)
        chosen = selected["action"]
        message = selected["message"]
        if chosen not in {*ACTION_LABELS, "none"} or not isinstance(message, str):
            raise ValueError("invalid action")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(502, "解釈結果を検査できなかったため、動作を開始していません。") from None
    if chosen == "none":
        return {"accepted": False, "text": text, "action": None, "message": message, "source": "openai"}
    result = await dispatch(chosen, epoch)
    return {**result, "text": text, "interpreted_action": chosen, "interpretation": message, "source": "openai"}


@app.post("/api/text")
async def text(command: TextInput):
    # An exact stop bypasses an occupied speech/GPT pipeline.
    if local_action(command.text) == "stop":
        return {**(await dispatch("stop")), "text": command.text, "source": "local"}
    if app.state.interpret_lock.locked() or app.state.voice_active:
        raise HTTPException(409, "前の指示を処理中です。停止ボタンはいつでも使えます。")
    async with app.state.interpret_lock:
        return await interpret_text(command.text, app.state.command_epoch)


@app.post("/api/audio")
async def audio(file: UploadFile = File(...)):
    request_epoch = app.state.command_epoch
    if app.state.interpret_lock.locked() or app.state.voice_active:
        raise HTTPException(409, "前の指示を処理中です。停止ボタンはいつでも使えます。")
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".webm", ".ogg", ".wav", ".mp4", ".m4a", ".mp3", ".flac"}:
        raise HTTPException(400, "WebMなどの対応する音声ファイルを送信してください。")
    payload = await file.read(MAX_AUDIO_BYTES + 1)
    await file.close()
    if request_epoch != app.state.command_epoch:
        raise HTTPException(409, "停止または別の実行操作により、音声指示を取り消しました。")
    if app.state.interpret_lock.locked() or app.state.voice_active or app.state.program_runner.snapshot()["running"]:
        raise HTTPException(409, "前の指示またはプログラムを実行中です。停止ボタンはいつでも使えます。")
    if not payload or len(payload) > MAX_AUDIO_BYTES:
        raise HTTPException(400, "音声が空か、6MBの上限を超えています。短く録音し直してください。")
    async with app.state.interpret_lock:
        epoch = request_epoch
        model = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
        try:
            async with client() as api:
                # /audio/transcriptions has no service_tier parameter in its
                # official schema. Never invent a returned tier for this API.
                transcript = await api.audio.transcriptions.create(
                    model=model,
                    file=("command" + extension, payload, file.content_type or "application/octet-stream"),
                    language="ja", response_format="json",
                )
        except HTTPException:
            raise
        except Exception as exc:
            app.state.usage.record("audio/transcriptions", model, error=exc)
            raise HTTPException(502, "音声を文字にできませんでした。動作は開始していません。") from None
        app.state.usage.record("audio/transcriptions", model, response=transcript)
        recognized = transcript.text.strip()
        if epoch != app.state.command_epoch:
            return {"accepted": False, "text": recognized, "message": "停止操作により、音声指示を取り消しました。"}
        if len(recognized) > 500:
            raise HTTPException(400, "指示が長すぎます。一つの短い動作を指定してください。")
        return await interpret_text(recognized, epoch)


install_program_routes(app, client_factory=lambda: client())
install_transcription_routes(app)
install_conversation_routes(app, client_factory=lambda: client())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
