# robot_household

日本語の相談から、仮想の家事ロボットを動かす点検プログラムを作るデモです。Toyota の公開 HSR-B モデルを Gazebo で動かし、ブラウザから操作します。実機用ドライバーは読み込みません。

- 相談しながら点検手順を生成・修正し、内容を確認してから実行できます。
- 赤い箱と青いボールの持ち上げ・元の位置への返却、待機、あいさつ、うなずきなどに対応します。
- 音声入力には OpenAI API、返答の読み上げには任意でローカルの VOICEVOX を使います。
- ボタン、対応する定型文、保存済みプログラムの読み込み・実行は API キーなしで利用できます。

本プロジェクトは **1 台の PC 内で使うシミュレーション用**です。アプリのインターネット公開、LAN 共有、実機制御を対象にしていません。

## 必要な環境

| 項目 | 構成 |
| --- | --- |
| シミュレーション | Ubuntu 24.04、ROS 2 Jazzy、Gazebo Harmonic |
| Windows からの利用 | WSL 2、登録名 `Ubuntu-24.04` のディストリビューション。Gazebo の画面を使う場合は WSLg などの GUI 環境 |
| Python | Ubuntu 24.04 の `python3` と `python3-venv`。ROS の Python パッケージを参照する仮想環境を作成 |
| ブラウザ | Windows の Chrome または Edge を想定。音声入力時はマイクの許可が必要 |
| 任意の外部サービス | 利用するモデルへのアクセスがある OpenAI API アカウントと API キー |
| 任意の読み上げ | ブラウザと同じ PC 上の VOICEVOX エンジン、`127.0.0.1:50021` |

Windows 用の起動スクリプトは WSL の登録名を `Ubuntu-24.04` に固定しています。別の登録名を使っている場合は、対象の Ubuntu 内で以下の Bash 手順を実行してください。Windows ネイティブの Python や macOS だけでは起動できません。

## 初回セットアップ

### 1. WSL と ROS / Gazebo を準備する

Windows では [Microsoft の WSL 導入手順](https://learn.microsoft.com/en-us/windows/wsl/install)に従って WSL 2 と Ubuntu 24.04 を準備します。未導入の場合の PowerShell コマンド例です。必要に応じて管理者権限と再起動が必要になります。

```powershell
wsl --install -d Ubuntu-24.04
wsl --list --verbose
```

Ubuntu 側で [ROS 2 Jazzy の公式導入手順](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html)に従い、ROS のパッケージ取得元を設定して ROS 2 を導入してください。その後、Ubuntu の Bash で必要なパッケージを追加します。

```bash
sudo apt update
sudo apt install ros-jazzy-ros-base ros-jazzy-ros-gz ros-jazzy-xacro git python3-venv
source /opt/ros/jazzy/setup.bash
```

ROS 2 Jazzy と Gazebo Harmonic は公式の対応構成です。`ros-jazzy-ros-gz` は対応する Gazebo と ROS の連携パッケージを導入します。詳しくは [Gazebo の ROS 連携手順](https://gazebosim.org/docs/harmonic/ros_installation/)を参照してください。

### 2. このフォルダでセットアップする

リポジトリを取得・展開し、`setup_environment.sh` があるディレクトリに移動してください。Windows のドライブ上に置いた場合は、Ubuntu から `/mnt/c/...` などのパスで参照できます。

```bash
cd /path/to/robot_household
bash setup_environment.sh
```

`/path/to/robot_household` は実際の配置先に置き換えます。スクリプトは以下を行います。

1. [hsr-project/hsrb_common](https://github.com/hsr-project/hsrb_common) の `jazzy` ブランチを `vendor/hsrb_common/` に取得し、コミット `c0b8ad5f813f1066b718f24911397d2a7f995cad` に固定します。
2. `.venv/` に仮想環境を作り、`requirements.txt` の Python パッケージを導入します。
3. `assets/` にモデル、ワールド、ROS と Gazebo の接続設定を生成します。

この準備には外部リポジトリ・パッケージのダウンロードが必要です。OpenAI API は呼び出しません。生成モデルには配置先に依存するパスが含まれるため、フォルダを移動した場合は `bash setup_environment.sh` を再実行してください。

既存の `vendor/hsrb_common/` が別コミットの場合は、上書きせずエラーで停止します。必要な変更を保管してから所定のコミットを使用してください。

## API キーとモデル

API を使う場合は、起動するプロセスの環境変数に `OPENAI_API_KEY` を設定します。以下は Ubuntu の Bash で、キーを画面やコマンド履歴に書かず、そのターミナルからの起動にだけ渡す例です。

```bash
read -r -s -p "OpenAI API key: " OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY
bash start_household.sh
```

`.env` の自動読み込み機能はありません。キーをソースコード、README、スクリーンショット、Git のコミットに含めないでください。環境変数を変更した場合はアプリを起動し直してください。

| 環境変数 | コード上の既定値・用途 |
| --- | --- |
| `OPENAI_API_KEY` | 未設定。プログラム生成・自由な表現の解釈・音声認識に使用 |
| `OPENAI_PROGRAM_MODEL` | `gpt-6.1-sol`。点検プログラムの相談・生成・修正 |
| `OPENAI_REALTIME_TRANSCRIBE_MODEL` | `gpt-live-transcribe`。相談入力欄へのリアルタイム文字起こし |
| `OPENAI_MODEL` | `gpt-4.1-mini`。単発指示で定型文に一致しなかった場合の解釈 |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe`。単発動作用の録音の文字起こし |
| `VOICEVOX_ENGINE_PATH` | Windows 用。任意の VOICEVOX エンジンの `run.exe` の絶対パス |

モデル名はこのソースの設定値です。アカウントの利用権限や API の対応状況によって使えない場合は、該当する API・出力形式に対応するモデルを指定してください。プログラム生成と単発指示の Responses API は `service_tier="default"` を指定します。プログラム生成では `gpt-6` で始まるモデルに `reasoning_effort="low"` を指定します。

Windows の `start_household.ps1` は、Windows 側に設定済みの `OPENAI_API_KEY`、`OPENAI_PROGRAM_MODEL`、`OPENAI_REALTIME_TRANSCRIBE_MODEL` を WSL に引き継ぎます。その他の OpenAI 設定を変える場合は、Ubuntu 側で環境変数を設定して `bash start_household.sh` から起動してください。

**API を呼ぶ操作には利用料金が発生します。** 音声のキャンセルやロボットの停止後も、既に送信した API 処理の料金が発生する場合があります。アプリは返された使用量を表示・記録しますが、料金の計算や予算超過の防止は行いません。[OpenAI の料金表](https://developers.openai.com/api/docs/pricing)とアカウント側の使用状況を確認してください。

## 起動と終了

Ubuntu の Bash から、セットアップしたフォルダで実行します。

```bash
bash start_household.sh
```

Gazebo の画面を開かずに動かす場合は次を使います。ブラウザの操作画面は通常どおり利用できます。

```bash
bash start_household.sh --headless
```

Windows の PowerShell から起動する場合は、このフォルダで実行します。先に Ubuntu 側で初回セットアップを完了してください。

```powershell
.\start_household.ps1
# Gazebo の画面が不要な場合
.\start_household.ps1 -Headless
```

「起動しました」と表示されたら、ブラウザで [操作画面](http://127.0.0.1:8765)を開きます。終了するには起動したターミナルで `Ctrl+C` を押してください。Windows 用スクリプトで起動・再利用した VOICEVOX エンジンは、他のローカルアプリでも利用できるよう終了後も残ります。

## 操作例

1. 「点検の相談」に「机の上の物を順番に持ち上げて、3秒待って元に戻して。最後にうなずいて」と入力して送信します。
2. 確認質問が出たら回答します。生成された手順や Python コードを確認し、「このプログラムを実行」を押します。相談・生成・保存だけではロボットは動きません。
3. 「青い物だけにして。待つ時間を5秒にして」などと追加指示して修正できます。
4. 必要に応じて名前を付けて保存し、後から読み込んで実行できます。

相談の音声入力は最大 60 秒です。「音声入力を終了」で文字起こしを確定し、内容を確認してから「送信」を押します。一方、「一つずつ動かす・音声で操作する」の音声入力は最大 20 秒で、認識された単発動作を直接実行します。

停止には「すぐ停止」を使ってください。音声の「止まって」は文字起こしを待つため、即時停止の代わりにはなりません。物体を持ったまま停止した場合は、単発操作で元の位置に戻してから点検プログラムを再実行してください。

## 任意：VOICEVOX による読み上げ

[VOICEVOX 公式サイト](https://voicevox.hiroshiba.jp/)から取得したアプリを Windows で起動するか、エンジンの場所を設定します。

```powershell
$env:VOICEVOX_ENGINE_PATH = 'C:\path\to\voicevox\run.exe'
.\start_household.ps1
```

`C:\path\to\voicevox\run.exe` は実際のエンジンのパスに置き換えます。既に `127.0.0.1:50021` で動作中の公式エンジンがある場合は再利用します。`tools/voicevox/run.exe` にエンジンを配置する方法もありますが、配布物には含めないでください。

読み上げる文章は、ブラウザから同じ PC のエンジンに送られます。VOICEVOX がなくてもテキストで操作できます。読み上げが始まらない場合は、画面の「読み上げる」を押してブラウザの再生を許可してください。既定の声は「ずんだもん」です。

音声を含む動画などを公開する場合は、使用した声に合わせたクレジット（例：`VOICEVOX:ずんだもん`）を表示し、[VOICEVOX の利用規約](https://voicevox.hiroshiba.jp/term/)と各音声ライブラリ・キャラクターの利用規約を確認してください。

## 通信と保存データ

| データ | 扱い |
| --- | --- |
| 相談内容・関連する会話履歴・修正対象のコード | プログラム生成時に必要な情報を OpenAI に送信 |
| 単発指示 | 対応する定型文はローカルで処理。それ以外は API キー設定時に指示と現在の保持対象を OpenAI に送信 |
| 音声 | 相談の音声入力は録音中に OpenAI に送信。単発動作の音声は録音終了後に送信 |
| `programs/` | 会話、入力した指示、生成コード、修正履歴、保存名をローカルに永続保存 |
| `logs/` | シミュレーター・サーバーの診断ログと `api_usage.jsonl`。API 使用量ログには本文・音声・キーを記録しない設計 |
| ブラウザのローカル保存 | 直前の相談の識別子、読み上げ設定などを保存 |

アプリは録音音声を履歴ファイルとして保存しません。ただし、単発音声のアップロード処理ではフレームワークが一時ファイルを使う場合があります。相談に送信した文字起こしの内容は会話履歴として保存されます。

Responses API には `store=False` を指定していますが、OpenAI 側でのデータ保持全般を無効にする設定ではありません。詳細は [OpenAI のデータ管理](https://developers.openai.com/api/docs/guides/your-data)を参照してください。

会話や生成物を消す場合は、アプリを終了し、必要なデータをバックアップしてから `programs/` の対象ファイルを削除してください。ブラウザ側の相談の識別子も消すには、そのサイトの保存データを削除します。ログを共有する際は内容を確認してください。

## ローカル利用のセキュリティ

サーバーは `127.0.0.1:8765` にだけ待ち受けます。HTTP と WebSocket の Host は `127.0.0.1:8765`、`localhost:8765`、`[::1]:8765` のみ許可し、変更を伴う HTTP リクエストと WebSocket 接続には、接続先と完全に一致する Origin を要求します。外部サイトや異なるポートからのブラウザ経由の操作・課金 API 呼び出しを拒否します。操作画面を別のページのフレーム内に埋め込むことも禁止しています。

Origin がない変更リクエストも拒否するため、ローカルの検証ツールから呼ぶ場合は `Origin: http://127.0.0.1:8765` を明示してください。通常のブラウザ操作では自動的に送信されます。

これらの検査は利用者認証ではありません。同じ PC 上のプロセスからのアクセスを制限するものではなく、複数利用者向けの認証、権限制御、課金上限はありません。`0.0.0.0` への変更、ポート転送、公開トンネル、リバースプロキシによる外部公開は行わないでください。

生成コードは制限した Python 構文を専用の処理系で解釈し、実行前に動作計画を検査します。任意の Python ファイルを実行する機能ではなく、`import`、ファイル操作、ネットワーク操作などは使えません。

## テスト

依存パッケージを導入済みの Ubuntu 環境で、このフォルダのルートから実行します。

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

セキュリティ対策の単体テストと FastAPI を通した結合テストです。ROS / Gazebo を起動せず、実際の OpenAI API も呼び出しません。Python 標準ライブラリだけで送信元検査を確認する場合は、以下を使えます。

```bash
python3 -B -m unittest discover -s tests -p 'test_local_access.py' -v
```

## シミュレーションの制限

- 対象は登録済みの赤い箱と青いボールです。色や形は登録情報で、カメラ認識・未知物体の認識・自律的な障害物回避はありません。
- 把持は手と物体を固定する補助方式です。摩擦による把持、実機の全身制御や車輪の挙動を再現するものではありません。
- 点検プログラムでは、物体を元の登録位置へ戻します。任意の場所への配置やトレイ収納は未対応です。
- 点検プログラムには、実動作 16 回、待機合計 60 秒、計画時間 600 秒などの上限があります。詳しくは `program_runtime.py` を参照してください。
- Python 依存パッケージはバージョン範囲指定で、完全固定ではありません。モデルの利用可否、GPU・WSLg・音声デバイスなどの環境によって追加調整が必要な場合があります。

## 問題が起きたとき

| 症状 | 確認すること |
| --- | --- |
| ROS / `gz` / `xacro` が見つからない | Ubuntu 24.04 の環境と必要なパッケージ、`/opt/ros/jazzy/setup.bash` を確認 |
| 起動時に `Run setup_environment.sh before starting` | Ubuntu の Bash で `bash setup_environment.sh` を実行 |
| 操作画面が開かない | 起動完了表示を待ち、`http://127.0.0.1:8765` とポートの競合を確認 |
| Gazebo の画面だけが開かない | WSLg / 描画環境を確認。`--headless` または `-Headless` で切り分け |
| 相談・音声入力が使えない | 起動プロセスへの API キー設定、モデルへのアクセス、ネットワーク、ブラウザのマイク権限を確認 |
| HTTP 400 / 403、WebSocket の接続拒否 | 指定の URL で開く。変更リクエストの Origin と Host、プロキシや別ポートの利用を確認 |
| VOICEVOX に接続できない | Windows でエンジンを起動し、パスと `127.0.0.1:50021` を確認 |

詳しい起動エラーは `logs/web_server.log`、`logs/simulation.log`、`logs/bridge.log` に記録されます。問題を報告する際は OS・起動方法・エラーの内容を添え、キーや私的な会話を取り除いてください。

## GitHub で共有する範囲

このフォルダのソースコード、Web 画面、設定・起動スクリプト、README とライセンスを共有する構成です。[`.gitignore`](.gitignore) は以下を除外します。

- `.venv/`、`__pycache__/`、`.pytest_cache/`
- `vendor/`、`assets/`、`tools/voicevox/`
- `programs/`、`logs/`
- `.env`、`.env.*`、`*.pem`、`*.key`、`*.p12`

`.gitignore` は既に追跡したファイルや過去のコミットを消しません。公開するコミットと履歴を確認し、秘密情報・会話・録音・生成物を含めないでください。ZIP の作成時も、フォルダ全体をそのまま圧縮すると除外対象が入ることがあります。

## ライセンスとクレジット

本プロジェクト独自のソースコード・ドキュメントは [MIT License](LICENSE) で提供します。

HSR-B のモデル・メッシュなどは Toyota Motor Corporation の [hsrb_common](https://github.com/hsr-project/hsrb_common) に由来し、**BSD-3-Clause-Clear** が適用されます。[HSR_LICENSE.txt](HSR_LICENSE.txt) の著作権表示と条件を保持してください。`vendor/` の取得物や HSR 由来の生成物を、本プロジェクトの MIT License で再許諾するものではありません。モデル生成時にも `assets/HSR_LICENSE.txt` が作成されます。

VOICEVOX 本体、エンジン、音声ライブラリ、および ROS、Gazebo、Python パッケージなどの依存物は、それぞれの権利者のライセンス・利用規約に従います。本プロジェクトの MIT License はこれらに適用されません。

本プロジェクトは Toyota、OpenAI、VOICEVOX の公式製品や推奨を示すものではありません。
