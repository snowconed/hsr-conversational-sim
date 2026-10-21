/* Realtime transcription only. Sending the chat message requires a separate click. */
(() => {
  "use strict";

  class ProgramVoice {
    constructor({input, button, cancelButton, meter, levelText, status, error, onBusy}) {
      Object.assign(this, {input, button, cancelButton, meter, levelText, status, error, onBusy});
      this.session = null;
      this.available = false;
      button.addEventListener("click", () => {
        if (this.session?.stage === "recording") this.finish();
        else if (!this.session && this.available) this.start();
      });
      cancelButton.addEventListener("click", () => this.cancel());
      this.render();
    }

    setAvailable(available) {
      this.available = available;
      this.render();
    }

    render() {
      const stage = this.session?.stage;
      this.button.textContent = stage === "recording" ? "音声入力を終了" :
        stage === "connecting" ? "マイク・接続を準備中" :
        stage === "finalizing" ? "文字起こしを確定中" : "音声入力を開始";
      this.button.disabled = stage ? stage !== "recording" : !this.available;
      this.button.setAttribute("aria-pressed", String(stage === "recording"));
      this.cancelButton.hidden = !stage;
      this.cancelButton.disabled = !stage;
    }

    isCurrent(session) { return this.session === session; }

    async start() {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      this.error.hidden = true;
      this.error.textContent = "";
      if (!navigator.mediaDevices?.getUserMedia || !AudioContextClass || !window.AudioWorkletNode) {
        this.showError("このブラウザでは音声入力を使えません。Windows の Chrome または Edge で http://127.0.0.1:8765 を開いてください。");
        return;
      }
      const session = {stage:"connecting", original:this.input.value, items:new Map(),
        stream:null, context:null, socket:null, source:null, worklet:null, gain:null,
        timer:null, watchdog:null, commitSent:false, completed:false, startedAt:0};
      this.session = session;
      this.status.textContent = "マイクの許可と文字起こしへの接続を待っています。";
      this.onBusy(true);
      this.render();
      try {
        // Create/resume in the click handler so browser autoplay policy permits recording.
        session.context = new AudioContextClass();
        const resumed = session.context.resume().then(() => null, error => error);
        const requestedStream = navigator.mediaDevices.getUserMedia({
          audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}
        });
        // Always stop a late permission result after cancellation.
        session.stream = await requestedStream;
        if (!this.isCurrent(session)) { this.releaseAudio(session); return; }
        const resumeError = await resumed;
        if (!this.isCurrent(session)) { this.releaseAudio(session); return; }
        if (resumeError) throw resumeError;
        await session.context.audioWorklet.addModule("/static/pcm-worklet.js");
        if (!this.isCurrent(session)) { this.releaseAudio(session); return; }
        this.connect(session);
      } catch (error) {
        if (!this.isCurrent(session)) { this.releaseAudio(session); return; }
        const message = error.name === "NotAllowedError" ?
          "マイクの使用が許可されていません。ブラウザのサイト設定でマイクを許可してください。" :
          error.name === "NotFoundError" ? "使用できるマイクが見つかりません。Windows のマイク接続を確認してください。" :
          "マイクを準備できませんでした。 " + error.message;
        this.fail(session, message);
      }
    }

    connect(session) {
      const url = new URL("/api/programs/transcription", location.href);
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(url);
      session.socket = socket;
      session.watchdog = setTimeout(() => this.fail(session, "文字起こしへの接続が時間切れになりました。もう一度お試しください。"), 25000);
      socket.addEventListener("message", event => {
        if (!this.isCurrent(session)) return;
        let message;
        try { message = JSON.parse(event.data); }
        catch (_) { this.fail(session, "文字起こしから正しい応答を受け取れませんでした。"); return; }
        if (message.type === "ready" && session.stage === "connecting") {
          if (message.sample_rate !== 24000) {
            this.fail(session, "文字起こしの音声形式が一致しません。サーバーを再起動してください。");
            return;
          }
          clearTimeout(session.watchdog);
          try { this.record(session, message.max_seconds); }
          catch (error) { this.fail(session, "録音を開始できませんでした。 " + error.message); }
        } else if (message.type === "transcript.delta") {
          const id = message.item_id || "speech";
          const item = session.items.get(id) || {text:"", completed:false};
          if (!item.completed) item.text += message.delta || "";
          session.items.set(id, item);
          this.showTranscript(session);
        } else if (message.type === "transcript.completed") {
          session.items.set(message.item_id || "speech", {text:message.text || "", completed:true});
          session.completed = true;
          this.showTranscript(session);
        } else if (message.type === "done") {
          if (!session.commitSent || !session.completed || !this.input.value.trim()) {
            this.fail(session, "音声を文字にできませんでした。マイクを確認して、もう一度お話しください。");
            return;
          }
          this.end(session, false);
          this.status.textContent = "文字起こしが確定しました。内容を確認・編集し、「送信」を押してください。";
        } else if (message.type === "error") {
          this.fail(session, message.message || "文字起こしでエラーが発生しました。");
        }
      });
      socket.addEventListener("error", () => this.fail(session, "音声入力に接続できませんでした。サーバーの起動と API キーの設定を確認してください。"));
      socket.addEventListener("close", () => {
        if (this.isCurrent(session)) this.fail(session, "文字起こしの確定前に接続が切れました。もう一度お試しください。");
      });
    }

    record(session, maxSeconds) {
      session.stage = "recording";
      session.startedAt = performance.now();
      this.input.value = "";
      session.source = session.context.createMediaStreamSource(session.stream);
      session.worklet = new AudioWorkletNode(session.context, "robot-pcm-recorder");
      session.gain = session.context.createGain();
      session.gain.gain.value = 0;
      session.source.connect(session.worklet);
      session.worklet.connect(session.gain);
      session.gain.connect(session.context.destination);
      session.worklet.port.onmessage = event => {
        if (!this.isCurrent(session)) return;
        const message = event.data;
        if (message.type === "pcm" && !session.commitSent && session.socket.readyState === WebSocket.OPEN) {
          if (session.socket.bufferedAmount > 1024 * 1024) {
            this.fail(session, "音声の送信が追いつきませんでした。接続を確認して、もう一度お試しください。");
            return;
          }
          session.socket.send(message.buffer);
        } else if (message.type === "level" && session.stage === "recording") {
          const level = Math.max(0, Math.min(1, message.rms * 5));
          this.meter.value = level;
          this.levelText.textContent = "入力レベル：" + Math.round(level * 100) + "%";
        } else if (message.type === "flushed" && session.stage === "finalizing" && !session.commitSent) {
          try {
            session.socket.send(JSON.stringify({type:"commit"}));
            session.commitSent = true;
            this.releaseAudio(session);
          } catch (_) {
            this.fail(session, "録音した音声を確定できませんでした。もう一度お試しください。");
          }
        }
      };
      session.stream.getAudioTracks().forEach(track => track.addEventListener("ended", () => {
        if (this.isCurrent(session) && session.stage === "recording") {
          this.fail(session, "録音中にマイクが切断されました。接続を確認してください。");
        }
      }));
      const seconds = Math.min(60, Math.max(1, Number(maxSeconds) || 60));
      session.timer = setTimeout(() => this.finish(true), seconds * 1000);
      this.status.textContent = "録音中です。「音声入力を終了」で確定します。途中の文字も入力欄に表示します（最大 " + seconds + " 秒）。";
      this.render();
    }

    showTranscript(session) {
      this.input.value = [...session.items.values()].map(item => item.text).join("\n");
      this.input.scrollTop = this.input.scrollHeight;
    }

    finish(timeLimit = false) {
      const session = this.session;
      if (session?.stage !== "recording") return;
      session.stage = "finalizing";
      clearTimeout(session.timer);
      session.source.disconnect();
      this.status.textContent = (timeLimit ? "録音の上限時間になりました。" : "録音を終了しました。") + "文字起こしの確定を待っています。";
      this.render();
      session.watchdog = setTimeout(() => this.fail(session, "文字起こしの確定が時間切れになりました。もう一度お試しください。"), 30000);
      // Flush the final partial PCM chunk before the server commits the input buffer.
      session.worklet.port.postMessage({type:"flush"});
    }

    cancel() {
      const session = this.session;
      if (!session) return;
      this.end(session, true);
      this.status.textContent = "音声入力を取り消し、開始前の入力に戻しました。";
    }

    fail(session, message) {
      if (!this.isCurrent(session)) return;
      this.end(session, true);
      this.status.textContent = "音声入力を完了できませんでした。開始前の入力に戻しました。";
      this.showError(message);
    }

    showError(message) {
      this.error.textContent = message;
      this.error.hidden = false;
    }

    releaseAudio(session) {
      session.stream?.getTracks().forEach(track => track.stop());
      session.stream = null;
      for (const node of [session.source, session.worklet, session.gain]) {
        try { node?.disconnect(); } catch (_) { /* Already disconnected. */ }
      }
      if (session.worklet) session.worklet.port.onmessage = null;
      if (session.context && session.context.state !== "closed") session.context.close().catch(() => {});
      this.meter.value = 0;
      this.levelText.textContent = "入力レベル：0%";
    }

    end(session, restore) {
      if (!this.isCurrent(session)) return;
      this.session = null; // Invalidate queued websocket/audio/permission callbacks first.
      clearTimeout(session.timer);
      clearTimeout(session.watchdog);
      if (restore) this.input.value = session.original;
      if (session.socket) {
        if (restore && session.socket.readyState === WebSocket.OPEN) {
          session.socket.send(JSON.stringify({type:"cancel"}));
        }
        session.socket.close();
      }
      this.releaseAudio(session);
      this.onBusy(false);
      this.render();
    }
  }

  window.ProgramVoice = ProgramVoice;
})();
