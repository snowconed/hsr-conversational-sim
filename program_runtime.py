"""A small, bounded Python interpreter for the household demonstration.

Source is parsed with ``ast`` and interpreted into a finite plan before motion.
It is never passed to Python's exec/eval/compile or an external process.
"""

import ast
from dataclasses import dataclass
import math
import threading
import time
import uuid


MAX_SOURCE_BYTES = 12000
MAX_AST_NODES = 800
MAX_DEPTH = 16
MAX_EVALUATIONS = 3000
MAX_ITERATIONS = 100
MAX_ACTIONS = 16
MAX_STEPS = 60
MAX_WAIT_SECONDS = 10
MAX_TOTAL_WAIT_SECONDS = 60
MAX_RUNTIME_SECONDS = 600
ACTION_TIMEOUTS = {
    "pick_cube": 90, "place_cube": 90, "pick_ball": 90, "place_ball": 90,
    "nod": 45, "greet": 75, "home": 45, "raise_arm": 45,
}

PROGRAM_API = """
日本語の指示から、このデモ専用の Python サブセットを生成してください。
使用できる API:
  robot.objects_on_table(): 登録済み物体の一覧。順序は cube, ball。
    cube: id="cube", name="箱", color="red", shape="box"
    ball: id="ball", name="ボール", color="blue", shape="sphere"
    色・形は登録情報。画像認識ではない。位置はシミュレーターから取得する。
  robot.pick(obj.id): 物体を補助付き把持で持ち上げ、完了を待つ。
  robot.return_to_original_position(obj.id): 対応する物体を登録済みの
    元の台上位置へ戻し、完了を待つ。任意の場所やトレイには置けない。
  robot.wait(seconds): 0～10 秒待つ。待機時間の合計は 60 秒まで。
  robot.nod(), robot.greet(), robot.home(), robot.raise_arm()
    これらの動作は物体を持っていないときだけ使用できる。
  robot.report(text): 結果の文章。全プログラムが成功した後だけ表示される。
構文: 単一変数への代入、+= / -=、for、if / else、pass。
式: 数値・文字列・真偽値、変数、obj.id/name/color/shape、比較、
  and/or/not、数値の + - *、range(0～10 の整数)、len(物体一覧)、
  f"{count}個" のような f-string（変換指定や書式指定は禁止）。
for は物体一覧または range(n) を使う。ネストも可能。
import、関数・クラス定義、while、例外処理、内包表記、添字、
  ファイル・ネットワーク操作、任意の属性や呼び出しは使用できない。
robot、range、len、二重アンダースコアを含む名前は代入禁止。
手は空の状態から開始する。持ち上げた物は対応する return_to_original_position
で戻してから次の物を持つ。点検プログラムは空の手で終了すること。
上限: ソース UTF-8 12000 バイト、AST 800 ノード、構文の深さ 16、
ループの総反復数 100、実動作 16 回、待機・報告を含む手順 60 個、
数値の絶対値 1000000、文字列 500 文字。
各 pick/place は最大 90 秒、nod/home/raise_arm は 45 秒、greet は 75 秒。
これらの上限と待機時間を足した計画時間は合計 600 秒以内にすること。
実行が失敗・中断したら後続の手順は実行しない。
対象の色や形を指示された場合は if を使い、回数は for/range を使う。
"""


class ProgramValidationError(ValueError):
    """The entire program was rejected before any controller command."""


class _Cancelled(Exception):
    pass


@dataclass(frozen=True)
class _Object:
    id: str
    name: str
    color: str
    shape: str


OBJECTS = (
    _Object("cube", "箱", "red", "box"),
    _Object("ball", "ボール", "blue", "sphere"),
)
_RESERVED = {"robot", "range", "len"}
_METHODS = {
    "objects_on_table": 0, "pick": 1, "return_to_original_position": 1,
    "wait": 1, "nod": 0, "greet": 0, "home": 0, "raise_arm": 0, "report": 1,
}


def _error(node, message):
    line = getattr(node, "lineno", None)
    prefix = f"{line} 行目: " if line else ""
    raise ProgramValidationError(prefix + message)


def _check_name(node):
    if "__" in node.id or len(node.id) > 64:
        _error(node, "この変数名は使用できません。")


def _target(node):
    if not isinstance(node, ast.Name):
        _error(node, "代入先には単一の変数名を指定してください。")
    _check_name(node)
    if node.id in _RESERVED:
        _error(node, f"{node.id} は変更できません。")


def _validate_tree(tree):
    """Validate every branch, including branches not taken in the dry run."""
    allowed = (
        ast.Module, ast.Assign, ast.AugAssign, ast.For, ast.If, ast.Expr, ast.Pass,
        ast.Name, ast.Constant, ast.Attribute, ast.Call, ast.Compare, ast.BoolOp,
        ast.UnaryOp, ast.BinOp, ast.JoinedStr, ast.FormattedValue,
        ast.Load, ast.Store, ast.Add, ast.Sub, ast.Mult, ast.UAdd, ast.USub,
        ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
        ast.Gt, ast.GtE,
    )
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        _error(tree, "プログラムが長すぎます（構文要素は 800 個まで）。")

    def visit(node, depth=0):
        if depth > MAX_DEPTH:
            _error(node, "構文の入れ子が深すぎます。")
        if not isinstance(node, allowed):
            _error(node, f"この構文は使用できません: {type(node).__name__}")
        if isinstance(node, ast.Name):
            _check_name(node)
        elif isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                _error(node, "この定数は使用できません。")
            _bounded(node.value, node)
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                _error(node, "一度に代入できる変数は 1 個です。")
            _target(node.targets[0])
        elif isinstance(node, ast.AugAssign):
            _target(node.target)
            if not isinstance(node.op, (ast.Add, ast.Sub)):
                _error(node, "複合代入には += または -= を使ってください。")
        elif isinstance(node, ast.For):
            _target(node.target)
            if node.orelse:
                _error(node, "for の else は使用できません。")
        elif isinstance(node, ast.Expr):
            if not isinstance(node.value, ast.Call):
                _error(node, "式の行には robot の操作を記述してください。")
            if not isinstance(node.value.func, ast.Attribute):
                _error(node, "この行には robot の操作が必要です。")
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                _error(node, "属性を重ねて参照することはできません。")
            if node.value.id == "robot":
                if node.attr not in _METHODS:
                    _error(node, f"未対応のロボット操作です: {node.attr}")
            elif node.attr not in {"id", "name", "color", "shape"}:
                _error(node, f"この物体属性は使用できません: {node.attr}")
        elif isinstance(node, ast.Call):
            if node.keywords:
                _error(node, "キーワード引数は使用できません。")
            if isinstance(node.func, ast.Name):
                if node.func.id not in {"range", "len"} or len(node.args) != 1:
                    _error(node, "range(n) または len(物体一覧) だけ使用できます。")
            elif isinstance(node.func, ast.Attribute):
                if not isinstance(node.func.value, ast.Name) or node.func.value.id != "robot":
                    _error(node, "robot 以外のメソッドは呼び出せません。")
                expected = _METHODS.get(node.func.attr)
                if expected is None or len(node.args) != expected:
                    _error(node, "ロボット操作の名前または引数の数が不正です。")
            else:
                _error(node, "この呼び出しは使用できません。")
        elif isinstance(node, ast.FormattedValue):
            if node.conversion != -1 or node.format_spec is not None:
                _error(node, "f-string の変換指定や書式指定は使用できません。")
        for child in ast.iter_child_nodes(node):
            visit(child, depth + 1)

    visit(tree)


def _bounded(value, node):
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 1000000:
            _error(node, "数値は有限で絶対値 1000000 以下にしてください。")
    elif isinstance(value, str) and len(value) > 500:
        _error(node, "文字列は 500 文字以内にしてください。")
    return value


def _number(value, node):
    if type(value) not in (int, float):
        _error(node, "数値が必要です。")
    return value


class _Planner:
    def __init__(self):
        self.variables = {}
        self.steps = []
        self.holding = None
        self.evaluations = 0
        self.iterations = 0
        self.actions = 0
        self.wait_seconds = 0
        self.estimated_seconds = 0

    def tick(self, node):
        self.evaluations += 1
        if self.evaluations > MAX_EVALUATIONS:
            _error(node, "プログラムの処理回数が上限を超えています。")

    def expression(self, node):
        self.tick(node)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self.variables:
                _error(node, f"変数 {node.id} はまだ定義されていません。")
            return self.variables[node.id]
        if isinstance(node, ast.Attribute):
            obj = self.expression(node.value)
            if not isinstance(obj, _Object):
                _error(node, "物体以外の属性は参照できません。")
            return getattr(obj, node.attr)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr != "objects_on_table":
                    _error(node, "動作の呼び出しは単独の行に書いてください。")
                return OBJECTS
            value = self.expression(node.args[0])
            if node.func.id == "range":
                if type(value) is not int or not 0 <= value <= 10:
                    _error(node, "range の回数は 0～10 の整数にしてください。")
                return tuple(range(value))
            if not isinstance(value, tuple):
                _error(node, "len には物体一覧または range の結果を指定してください。")
            return len(value)
        if isinstance(node, ast.UnaryOp):
            value = self.expression(node.operand)
            if isinstance(node.op, ast.Not):
                return not bool(value)
            value = _number(value, node)
            return _bounded(value if isinstance(node.op, ast.UAdd) else -value, node)
        if isinstance(node, ast.BinOp):
            left = _number(self.expression(node.left), node)
            right = _number(self.expression(node.right), node)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            else:
                value = left * right
            return _bounded(value, node)
        if isinstance(node, ast.Compare):
            left = self.expression(node.left)
            for op, other in zip(node.ops, node.comparators):
                right = self.expression(other)
                if not isinstance(left, (str, int, float, bool, type(None))) or not isinstance(
                    right, (str, int, float, bool, type(None))
                ):
                    _error(node, "比較には数値、文字列または真偽値を使ってください。")
                if isinstance(op, ast.Eq):
                    matches = left == right
                elif isinstance(op, ast.NotEq):
                    matches = left != right
                else:
                    if type(left) not in (int, float) or type(right) not in (int, float):
                        _error(node, "大小比較には数値を指定してください。")
                    matches = (
                        left < right if isinstance(op, ast.Lt) else
                        left <= right if isinstance(op, ast.LtE) else
                        left > right if isinstance(op, ast.Gt) else left >= right
                    )
                if not matches:
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            for item in node.values:
                value = self.expression(item)
                if isinstance(node.op, ast.And) and not value:
                    return False
                if isinstance(node.op, ast.Or) and value:
                    return True
            return isinstance(node.op, ast.And)
        if isinstance(node, ast.JoinedStr):
            pieces = []
            for item in node.values:
                value = self.expression(item.value) if isinstance(item, ast.FormattedValue) else item.value
                if type(value) not in (str, int, float, bool, type(None)):
                    _error(node, "f-string には文字列や数値を指定してください。")
                pieces.append(str(value))
            return _bounded("".join(pieces), node)
        _error(node, "この式は使用できません。")

    def call(self, node):
        method = node.func.attr
        args = [self.expression(arg) for arg in node.args]
        step = {"line": node.lineno}
        if method in {"pick", "return_to_original_position"}:
            item = args[0]
            if item not in {"cube", "ball"}:
                _error(node, "対象は登録済みの cube または ball にしてください。")
            if method == "pick":
                if self.holding is not None:
                    _error(node, "次の物体を持つ前に、持っている物体を元に戻してください。")
                self.holding = item
                action = "pick_" + item
            else:
                if self.holding != item:
                    _error(node, "戻す対象と、持っている物体が一致しません。")
                self.holding = None
                action = "place_" + item
            step.update(kind="action", action=action, object_id=item)
        elif method in {"nod", "greet", "home", "raise_arm"}:
            if self.holding is not None:
                _error(node, "この動作の前に持っている物体を元に戻してください。")
            step.update(kind="action", action=method)
        elif method == "wait":
            seconds = _number(args[0], node)
            if not 0 <= seconds <= MAX_WAIT_SECONDS:
                _error(node, "1 回の待機時間は 0～10 秒にしてください。")
            self.wait_seconds += seconds
            self.estimated_seconds += seconds
            if self.wait_seconds > MAX_TOTAL_WAIT_SECONDS:
                _error(node, "待機時間の合計は 60 秒までです。")
            step.update(kind="wait", seconds=seconds)
        elif method == "report":
            if not isinstance(args[0], str):
                _error(node, "報告には文字列を指定してください。")
            step.update(kind="report", text=args[0])
        else:
            _error(node, "objects_on_table() は for または代入の中で使用してください。")
        if step["kind"] == "action":
            self.actions += 1
            self.estimated_seconds += ACTION_TIMEOUTS[step["action"]]
            if self.actions > MAX_ACTIONS:
                _error(node, "動作は 16 回以内にしてください。")
        if self.estimated_seconds > MAX_RUNTIME_SECONDS:
            _error(node, "動作ごとの最大時間を合計すると 600 秒を超えます。処理を減らしてください。")
        self.steps.append(step)
        if len(self.steps) > MAX_STEPS:
            _error(node, "手順は待機・報告を含めて 60 個以内にしてください。")

    def statements(self, statements):
        for node in statements:
            self.tick(node)
            if isinstance(node, ast.Assign):
                self.variables[node.targets[0].id] = self.expression(node.value)
            elif isinstance(node, ast.AugAssign):
                left = _number(self.expression(node.target), node)
                right = _number(self.expression(node.value), node)
                self.variables[node.target.id] = _bounded(
                    left + right if isinstance(node.op, ast.Add) else left - right, node
                )
            elif isinstance(node, ast.For):
                values = self.expression(node.iter)
                if not isinstance(values, tuple):
                    _error(node, "for には物体一覧または range(n) を指定してください。")
                for value in values:
                    self.iterations += 1
                    if self.iterations > MAX_ITERATIONS:
                        _error(node, "ループの総反復数は 100 回までです。")
                    self.variables[node.target.id] = value
                    self.statements(node.body)
            elif isinstance(node, ast.If):
                self.statements(node.body if self.expression(node.test) else node.orelse)
            elif isinstance(node, ast.Expr):
                self.call(node.value)


def validate_program(code):
    """Return a fully expanded plan, or raise ProgramValidationError.

    Validation uses both registered objects. Runtime additionally checks measured
    object availability before any motion; unknown positions are never omitted.
    """
    if not isinstance(code, str) or not code.strip():
        raise ProgramValidationError("プログラムを入力してください。")
    if len(code.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ProgramValidationError("プログラムは UTF-8 12000 バイト以内にしてください。")
    try:
        tree = ast.parse(code, mode="exec")
        _validate_tree(tree)
        planner = _Planner()
        planner.statements(tree.body)
        if planner.holding:
            raise ProgramValidationError("点検の最後に、持っている物体を元の位置へ戻してください。")
        if not planner.steps:
            raise ProgramValidationError("実行する動作・待機・報告がありません。")
        return {
            "valid": True, "steps": planner.steps, "action_count": planner.actions,
            "wait_seconds": planner.wait_seconds,
            "estimated_max_seconds": planner.estimated_seconds,
            "object_source": "登録情報とシミュレーターの位置情報（画像認識ではありません）",
        }
    except (SyntaxError, RecursionError, OverflowError, TypeError) as error:
        if isinstance(error, SyntaxError):
            raise ProgramValidationError(f"{error.lineno} 行目: Python の構文を確認してください。") from error
        raise ProgramValidationError("プログラムの式または構造が対応範囲を超えています。") from error


class ProgramRunner:
    """One program or manual action at a time, with serialized cancellation."""

    def __init__(self, controller):
        self.controller = controller
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.worker = None
        self.closed = False
        self.state = {
            "running": False, "status": "idle", "run_id": None,
            "message": "プログラムはまだ実行されていません。",
            "error": None, "report": "", "steps": [],
        }

    def snapshot(self):
        with self.lock:
            return {**self.state, "steps": [dict(step) for step in self.state["steps"]]}

    def start(self, code):
        plan = validate_program(code)
        with self.lock:
            if self.closed:
                return {"accepted": False, "message": "実行器は終了しています。"}
            if self.state["running"]:
                return {"accepted": False, "message": "プログラムを実行中です。"}
            current = self.controller.get_state()
            if not current.get("ready") or current.get("busy"):
                return {"accepted": False, "message": "ロボットの準備完了と動作終了をお待ちください。"}
            if current.get("holding") or current.get("attachment_pending") or current.get("attachment_uncertain"):
                return {"accepted": False, "message": "手動操作で持っている物体を置き、保持状態を確認してください。"}
            positions = current.get("object_positions", {})
            for item in {step["object_id"] for step in plan["steps"] if "object_id" in step}:
                position = positions.get(item)
                if (
                    not isinstance(position, (list, tuple)) or len(position) != 3
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in position)
                ):
                    return {"accepted": False, "message": f"{item} の現在位置を取得できません。"}
            run_id = uuid.uuid4().hex
            self.cancel = threading.Event()
            self.state = {
                "running": True, "status": "running", "run_id": run_id,
                "message": "点検プログラムを実行しています。",
                "error": None, "report": "",
                "steps": [{**step, "status": "pending"} for step in plan["steps"]],
            }
            self.worker = threading.Thread(
                target=self._run, args=(self.cancel,), name="inspection-program", daemon=True
            )
            self.worker.start()
            return {"accepted": True, "run_id": run_id, "message": "プログラムの実行を開始しました。"}

    def submit_manual(self, action):
        if action == "stop":
            return self.stop()
        with self.lock:
            if self.closed or self.state["running"]:
                return {
                    "accepted": False, "action": action,
                    "message": "プログラムの実行が終了してから手動操作してください。",
                }
            return self.controller.submit(action)

    def stop(self):
        # The same lock guards every submission, so no new command can cross
        # this cancellation boundary even if stop lands between plan steps.
        with self.lock:
            self.cancel.set()
            if self.state["running"]:
                self.state["status"] = "cancelling"
                self.state["message"] = "プログラムを停止しています。"
                self.state["report"] = ""
            return self.controller.stop()

    def close(self):
        with self.lock:
            self.closed = True
        self.stop()
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=3)

    def _check(self, cancel, deadline):
        if cancel.is_set():
            raise _Cancelled()
        if time.monotonic() >= deadline:
            raise RuntimeError("プログラムの実行時間が上限を超えたため停止しました。")

    def _action(self, step, cancel, deadline):
        action = step["action"]
        with self.lock:
            self._check(cancel, deadline)
            previous = self.controller.get_state().get("history", [])
            previous_head = dict(previous[0]) if previous else None
            response = self.controller.submit(action)
            if not response.get("accepted"):
                raise RuntimeError(response.get("message", "ロボットが動作を受け付けませんでした。"))
        action_deadline = min(deadline, time.monotonic() + ACTION_TIMEOUTS[action])
        while True:
            self._check(cancel, action_deadline)
            current = self.controller.get_state()
            history = current.get("history", [])
            if not current.get("busy") and history and history[0] != previous_head:
                result = history[0]
                if result.get("action") != action:
                    raise RuntimeError("実行結果と指示した動作が一致しないため停止しました。")
                if result.get("status") == "cancelled":
                    raise _Cancelled()
                if result.get("status") != "completed":
                    raise RuntimeError(result.get("message") or "ロボットの動作が失敗しました。")
                if action.startswith("pick_") and current.get("holding") != action[5:]:
                    raise RuntimeError("物体を保持できたことを確認できませんでした。")
                if action.startswith("place_") and current.get("holding") is not None:
                    raise RuntimeError("物体を置けたことを確認できませんでした。")
                return result.get("message", "動作が完了しました。")
            if not current.get("ready"):
                raise RuntimeError("ロボットの位置情報を確認できないため停止しました。")
            cancel.wait(.03)

    def _run(self, cancel):
        deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        reports = []
        index = None
        try:
            for index in range(len(self.state["steps"])):
                with self.lock:
                    self._check(cancel, deadline)
                    step = self.state["steps"][index]
                    step["status"] = "running"
                    self.state["message"] = f"{index + 1} / {len(self.state['steps'])} 手順を実行しています。"
                result = None
                if step["kind"] == "action":
                    result = self._action(step, cancel, deadline)
                elif step["kind"] == "wait":
                    if cancel.wait(step["seconds"]):
                        raise _Cancelled()
                else:
                    reports.append(step["text"])
                with self.lock:
                    self._check(cancel, deadline)
                    step["status"] = "completed"
                    if result:
                        step["message"] = result
            with self.lock:
                self._check(cancel, deadline)
                self.state.update(
                    running=False, status="completed", message="プログラムが完了しました。",
                    report="\n".join(reports),
                )
        except Exception as error:
            with self.lock:
                cancelled = isinstance(error, _Cancelled) or cancel.is_set()
                self.controller.stop()
                status = "cancelled" if cancelled else "failed"
                if index is not None and self.state["steps"][index]["status"] == "running":
                    self.state["steps"][index]["status"] = status
                for step in self.state["steps"]:
                    if step["status"] == "pending":
                        step["status"] = "skipped"
                self.state.update(
                    running=False, status=status, report="",
                    error=None if cancelled else str(error),
                    message="プログラムを停止しました。" if cancelled else "プログラムの実行に失敗しました。",
                )
