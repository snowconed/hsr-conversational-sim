"""Japanese inspection-program authoring, immutable revisions, and named saves."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from program_runtime import PROGRAM_API, ProgramValidationError, validate_program
from api_settings import program_settings


ID_PATTERN = r"^[a-f0-9]{32}$"
MAX_CODE = 12000


class GenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=2000)
    base_revision_id: str | None = Field(default=None, pattern=ID_PATTERN)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(pattern=ID_PATTERN)
    expected_epoch: int | None = Field(default=None, ge=0)


class SaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=80)


class GeneratedProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    summary: str = Field(min_length=1, max_length=1500)
    code: str = Field(max_length=MAX_CODE)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ProgramStore:
    """Names are metadata, never paths. New saves never overwrite old saves."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.RLock()

    def _path(self, kind, identity, suffix=".json"):
        if not isinstance(identity, str) or not re.fullmatch(ID_PATTERN, identity):
            raise ValueError("プログラムのIDが正しくありません。")
        return self.root / kind / (identity + suffix)

    @staticmethod
    def _write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, identity):
        path = self._path("revisions", identity)
        with self.lock:
            try:
                revision = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise KeyError("指定したプログラムが見つかりません。") from None
            except (ValueError, OSError):
                raise ValueError("保存されたプログラムを読み取れません。") from None
            if not isinstance(revision, dict) or revision.get("id") != identity:
                raise ValueError("保存されたプログラムの形式が正しくありません。")
            if not isinstance(revision.get("code"), str) or len(revision["code"]) > MAX_CODE:
                raise ValueError("保存されたコードの形式が正しくありません。")
            return revision

    def create(self, *, code, summary, instruction, parent_id, model):
        validation = validate_program(code)
        with self.lock:
            if parent_id:
                self.get(parent_id)
            revision = {
                "id": uuid4().hex, "code": code, "summary": summary,
                "instruction": instruction, "parent_id": parent_id,
                "created_at": utc_now(), "model": model, "validation": validation,
            }
            self._write(self._path("revisions", revision["id"]),
                        json.dumps(revision, ensure_ascii=False, indent=2) + "\n")
            return revision

    def save(self, identity, name):
        name = name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("保存名は1〜80文字で入力してください。")
        with self.lock:
            revision = self.get(identity)
            validate_program(revision["code"])
            saved = {
                "id": uuid4().hex, "name": name, "revision_id": identity,
                "created_at": utc_now(),
            }
            # Commit the visible metadata last. An interrupted write never
            # advertises a successful save whose Python file does not exist.
            self._write(self._path("saved", saved["id"], ".py"), revision["code"])
            self._write(self._path("saved", saved["id"]),
                        json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
            return saved

    def list_saved(self):
        with self.lock:
            result = []
            for path in (self.root / "saved").glob("*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(item, dict) or item.get("id") != path.stem
                            or not isinstance(item.get("name"), str)
                            or not isinstance(item.get("created_at"), str)
                            or not re.fullmatch(ID_PATTERN, item.get("revision_id", ""))
                            or not path.with_suffix(".py").is_file()):
                        continue
                    result.append(item)
                except (ValueError, OSError, TypeError):
                    continue
            return sorted(result, key=lambda item: item["created_at"], reverse=True)


GENERATION_RULES = """あなたはHSR仮想ロボットの点検用Pythonプログラムを作成・修正します。
日本語の依頼を、下記で定義したPythonの範囲とrobot APIだけで実装してください。
生成・修正は実行ではありません。summaryは日本語で予定動作を短く説明し、成功や実行済みと言わないでください。
追加指示のときはbase_programのコードを修正し、変更を頼まれていない条件・順序・待機・報告は維持します。
曖昧で対象や動作を決められない、非対応の能力や構文が必要、制限内で実現できないときは
status='clarification', code='', summary=日本語の確認質問または対応できない理由にします。
青いボール(id ball,color blue,shape sphere)と赤い箱(id cube,color red,shape box)が登録されています。
色と形は登録情報、位置はシミュレーター情報です。カメラ認識とは言わないでください。
物体はひとつずつ持ち上げ、必ず元の場所に戻してから次の物体やジェスチャーへ進みます。
プログラム終了時は空の手に戻してください。元の場所は登録された机上の定位置です。
トレイ収納、未知の物体、移動距離の指定、音声会話、画像認識はこの版では未対応です。
コードはMarkdownフェンスを含めず、トップレベルの文として返してください。
報告には処理後のカウンターを使えます。回数・対象・待機時間などユーザーが指定した条件を尊重してください。
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "clarification"]},
        "summary": {"type": "string"},
        "code": {"type": "string"},
    },
    "required": ["status", "summary", "code"],
    "additionalProperties": False,
}


def install_program_routes(app, client_factory):
    def get_revision(identity):
        try:
            return app.state.program_store.get(identity)
        except KeyError as error:
            raise HTTPException(404, error.args[0]) from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/programs/generate")
    async def generate(command: GenerateInput):
        instruction = command.instruction.strip()
        if not instruction:
            raise HTTPException(400, "作りたいプログラムや変更内容を入力してください。")
        if app.state.interpret_lock.locked():
            raise HTTPException(409, "前の入力を処理中です。少し待ってから試してください。")
        if getattr(app.state, "voice_active", False):
            raise HTTPException(409, "音声入力を終了してから生成・修正してください。")
        base = get_revision(command.base_revision_id) if command.base_revision_id else None
        async with app.state.interpret_lock:
            epoch = app.state.command_epoch
            settings = program_settings()
            model = settings["model"]
            tier = settings["service_tier"]
            options = {"reasoning": {"effort": settings["reasoning_effort"]}} if model.startswith("gpt-6") else {}
            payload = {"instruction": instruction, "base_program": None}
            if base:
                payload["base_program"] = {
                    "code": base["code"], "summary": base["summary"],
                    "previous_instruction": base["instruction"],
                }
            started = time.monotonic()
            try:
                async with client_factory() as api:
                    response = await api.responses.create(
                        model=model, service_tier=tier, store=False,
                        instructions=GENERATION_RULES + "\n" + PROGRAM_API,
                        input=json.dumps(payload, ensure_ascii=False),
                        text={"format": {
                            "type": "json_schema", "name": "inspection_program",
                            "strict": True, "schema": OUTPUT_SCHEMA,
                        }},
                        max_output_tokens=4096, **options,
                    )
            except HTTPException:
                raise
            except Exception as error:
                app.state.usage.record("responses", model, error=error,
                                       requested_service_tier=tier,
                                       elapsed_ms=round((time.monotonic() - started) * 1000))
                raise HTTPException(502, "プログラムの生成に失敗しました。現在のコードは変更していません。") from None
            elapsed_ms = round((time.monotonic() - started) * 1000)
            app.state.usage.record("responses", model, response=response,
                                   requested_service_tier=tier, elapsed_ms=elapsed_ms)
            if epoch != app.state.command_epoch:
                raise HTTPException(409, "停止操作により、生成中のプログラムを取り消しました。")
            if getattr(response, "status", None) != "completed":
                raise HTTPException(502, "生成が完了しなかったため、現在のコードを変更していません。")
            try:
                generated = GeneratedProgram.model_validate_json(response.output_text)
                if generated.status == "clarification":
                    raise HTTPException(422, generated.summary)
                if generated.status != "ready" or not generated.code.strip():
                    raise ValueError("コードが空か、形式が正しくありません。")
                # This validates every supported AST node and dry-runs the
                # program; it never invokes the real robot.
                revision = app.state.program_store.create(
                    code=generated.code.strip() + "\n", summary=generated.summary,
                    instruction=instruction, parent_id=command.base_revision_id,
                    model=getattr(response, "model", model),
                )
            except HTTPException:
                raise
            except ProgramValidationError as error:
                raise HTTPException(422, "生成コードを実行前検査で見送りました: " + str(error)) from None
            except (ValueError, KeyError, AttributeError, TypeError):
                raise HTTPException(502, "生成結果の形式を確認できませんでした。現在のコードは変更していません。") from None
            except OSError:
                raise HTTPException(500, "生成コードの保存に失敗しました。空き容量と書き込み権限を確認してください。") from None
            return {
                "revision": revision, "message": "プログラムを生成しました。「実行」で動作を開始します。",
                "generation": {
                    "elapsed_ms": elapsed_ms, "requested_model": model,
                    "returned_model": getattr(response, "model", None),
                    "requested_service_tier": tier,
                    "returned_service_tier": getattr(response, "service_tier", None),
                },
            }

    @app.get("/api/programs/revisions/{identity}")
    async def revision(identity: str):
        return {"revision": get_revision(identity)}

    @app.get("/api/programs/revisions/{identity}/code")
    async def download_code(identity: str):
        item = get_revision(identity)
        return PlainTextResponse(
            item["code"], media_type="text/x-python; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="inspection-{identity}.py"'},
        )

    @app.post("/api/programs/run")
    async def run(command: RunInput):
        if command.expected_epoch is not None and command.expected_epoch != app.state.command_epoch:
            raise HTTPException(409, "停止または別の実行操作があったため、実行を取り消しました。状態を確認して再度実行してください。")
        if app.state.interpret_lock.locked() or getattr(app.state, "voice_active", False):
            raise HTTPException(409, "入力の処理が終わってから実行してください。")
        item = get_revision(command.revision_id)
        try:
            result = app.state.program_runner.start(item["code"])
        except ProgramValidationError as error:
            raise HTTPException(422, str(error)) from None
        if result.get("accepted"):
            app.state.command_epoch += 1
            app.state.program_revision_id = item["id"]
        return result

    @app.post("/api/programs/save")
    async def save(command: SaveInput):
        get_revision(command.revision_id)
        try:
            saved = app.state.program_store.save(command.revision_id, command.name)
        except (ValueError, ProgramValidationError) as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(500, "保存できませんでした。空き容量と書き込み権限を確認してください。") from None
        return {"saved": saved, "message": "名前を付けて保存しました。再起動後も読み込めます。"}

    @app.get("/api/programs/saved")
    async def saved_programs():
        return {"programs": app.state.program_store.list_saved()}
