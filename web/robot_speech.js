/* Local VOICEVOX output only. This module never records or executes robot actions. */
(() => {
  "use strict";
  const ENGINE = "http://127.0.0.1:50021";
  const SETTINGS = "robot-household.speech-settings";
  const unavailable = "VOICEVOX に接続できません。Windows で VOICEVOX を起動するか、ロボットの起動用スクリプトを実行し直してから「接続を確認」を押してください。返答は文字でも確認できます。";
  const playbackBlocked = "音声の再生が許可されていません。読みたい返答の「読み上げる」を押してください。ブラウザのサイト設定で音声も許可できます。";

  class RobotSpeech {
    constructor({toggle, stopButton, status, error, speaker, speed, refreshButton, credit, onBusy = () => {}}) {
      Object.assign(this, {toggle, stopButton, status, error, speaker, speed, refreshButton, credit, onBusy});
      this.context = null;
      this.source = null;
      this.controller = null;
      this.engineController = null;
      this.generation = 0;
      this.busy = false;
      this.inputActive = false;
      this.destroyed = false;
      this.resumePromise = Promise.resolve(false);
      let saved = {};
      try { saved = JSON.parse(localStorage.getItem(SETTINGS)) || {}; } catch (_) { /* Storage is optional. */ }
      toggle.checked = saved.automatic !== false;
      this.selectedSpeaker = Number.isInteger(saved.speaker) && saved.speaker >= 0 ? saved.speaker : 3;
      this.selectedSpeed = [0.8, 1, 1.2, 1.4].includes(Number(saved.speed)) ? Number(saved.speed) : 1;
      speed.value = String(this.selectedSpeed);
      toggle.addEventListener("change", () => {
        this.cancel();
        this.clearError();
        if (toggle.checked) this.unlock();
        this.saveSettings();
      });
      stopButton.addEventListener("click", () => this.cancel());
      refreshButton.addEventListener("click", () => { this.unlock(); this.checkEngine(true); });
      speaker.addEventListener("change", () => {
        this.cancel();
        this.selectedSpeaker = Number(speaker.value);
        this.updateCredit();
        this.saveSettings();
      });
      speed.addEventListener("change", () => {
        this.cancel();
        this.selectedSpeed = Number(speed.value);
        this.saveSettings();
      });
      this.setBusy(false);
      this.status.textContent = "VOICEVOX の接続を確認しています。";
      this.checkEngine();
    }

    saveSettings() {
      try {
        localStorage.setItem(SETTINGS, JSON.stringify({
          automatic:this.toggle.checked, speaker:this.selectedSpeaker, speed:this.selectedSpeed
        }));
      } catch (_) { /* Reading aloud also works without browser storage. */ }
    }

    updateCredit() {
      const option = this.speaker.selectedOptions[0];
      this.credit.textContent = "音声：VOICEVOX:" + (option?.dataset.voiceName || "ずんだもん");
    }

    setBusy(value) {
      this.busy = value;
      this.stopButton.disabled = !value;
      this.onBusy(value);
    }

    clearError() { this.error.textContent = ""; this.error.hidden = true; }
    showError(message) { this.error.textContent = message; this.error.hidden = false; }

    // Call directly from a click/submit handler, before awaiting network activity.
    unlock() {
      if (this.destroyed) return;
      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) return;
        if (!this.context || this.context.state === "closed") this.context = new AudioContextClass();
        this.resumePromise = this.context.state === "running" ? Promise.resolve(true) :
          this.context.resume().then(() => this.context?.state === "running", () => false);
      } catch (_) { this.resumePromise = Promise.resolve(false); }
    }

    setInputActive(value) {
      if (value && !this.inputActive) this.cancel();
      this.inputActive = value;
    }

    cancel() {
      this.generation++;
      this.controller?.abort();
      this.controller = null;
      if (this.source) {
        this.source.onended = null;
        try { this.source.stop(); } catch (_) { /* Already finished. */ }
        try { this.source.disconnect(); } catch (_) { /* Already disconnected. */ }
        this.source = null;
      }
      this.setBusy(false);
      this.status.textContent = this.toggle.checked ? "新しい返答を自動で読み上げます。" : "自動読み上げはオフです。「読み上げる」で個別に再生できます。";
    }

    async engineFetch(path, options, controller, timeoutMs = 15000, format = "json") {
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(ENGINE + path, {...options, signal:controller.signal, cache:"no-store"});
        if (!response.ok) throw new Error("VOICEVOX の応答を取得できませんでした。「接続を確認」で声を読み込み直してください。");
        return await (format === "wav" ? response.arrayBuffer() : response.json());
      } finally { clearTimeout(timeout); }
    }

    async checkEngine(showFailure = false) {
      this.engineController?.abort();
      const controller = new AbortController();
      this.engineController = controller;
      this.refreshButton.disabled = true;
      try {
        const version = await this.engineFetch("/version", {}, controller, 5000);
        const speakers = await this.engineFetch("/speakers", {}, controller, 5000);
        if (this.destroyed || this.engineController !== controller) return;
        const options = [];
        for (const voice of Array.isArray(speakers) ? speakers : []) {
          for (const style of voice.styles || []) {
            if (!Number.isInteger(style.id) || (style.type && style.type !== "talk")) continue;
            const option = document.createElement("option");
            option.value = String(style.id);
            option.dataset.voiceName = voice.name;
            option.textContent = voice.name + "（" + style.name + "）";
            options.push(option);
          }
        }
        if (!options.length) throw new Error("VOICEVOX に読み上げ用の声がありません。音声モデルの設定を確認してください。");
        this.speaker.replaceChildren(...options);
        if (!options.some(option => Number(option.value) === this.selectedSpeaker)) {
          this.selectedSpeaker = options.some(option => option.value === "3") ? 3 : Number(options[0].value);
        }
        this.speaker.value = String(this.selectedSpeaker);
        this.updateCredit();
        this.saveSettings();
        this.clearError();
        if (!this.busy) this.status.textContent = "VOICEVOX " + String(version) + " に接続しました。";
      } catch (error) {
        if (this.destroyed || this.engineController !== controller) return;
        if (!this.busy) this.status.textContent = "VOICEVOX が見つかりません。Windows の音声エンジンを起動して「接続を確認」を押してください。";
        if (showFailure) this.showError(error.name === "TypeError" || error.name === "AbortError" ? unavailable : error.message);
      } finally {
        if (this.engineController === controller) {
          this.engineController = null;
          this.refreshButton.disabled = false;
        }
      }
    }

    async speak(text, {automatic = false} = {}) {
      if (this.destroyed || this.inputActive || (automatic && !this.toggle.checked)) return;
      // Only the conversational prose is spoken; code remains in its folded panel.
      const speechText = String(text || "").replace(/```[\s\S]*?```/g, "").replace(/`[^`]*`/g, "").trim().slice(0, 2000);
      if (!speechText) return;
      this.cancel();
      this.clearError();
      const token = this.generation;
      const controller = new AbortController();
      this.controller = controller;
      const current = () => !this.destroyed && token === this.generation && !this.inputActive;
      this.setBusy(true);
      this.status.textContent = "ロボットの音声を準備しています。";
      try {
        if (!this.context) throw new Error(playbackBlocked);
        const ready = await Promise.race([this.resumePromise, new Promise(resolve => setTimeout(() => resolve(false), 1500))]);
        if (!current()) return;
        if (!ready || this.context.state !== "running") throw new Error(playbackBlocked);
        const speaker = this.selectedSpeaker;
        const query = await this.engineFetch("/audio_query?text=" + encodeURIComponent(speechText) +
          "&speaker=" + speaker, {method:"POST"}, controller);
        if (!current()) return;
        query.speedScale = this.selectedSpeed;
        const wav = await this.engineFetch("/synthesis?speaker=" + speaker, {
          method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(query)
        }, controller, 60000, "wav");
        if (!current()) return;
        const buffer = await this.context.decodeAudioData(wav);
        if (!current()) return;
        if (this.context.state !== "running") throw new Error(playbackBlocked);
        const source = this.context.createBufferSource();
        source.buffer = buffer;
        source.connect(this.context.destination);
        this.source = source;
        source.onended = () => {
          if (!current()) return;
          source.disconnect();
          this.source = null;
          this.controller = null;
          this.setBusy(false);
          this.status.textContent = "読み上げが終わりました。";
        };
        source.start();
        this.status.textContent = "ロボットが話しています。音声入力を始めると読み上げを止めます。";
      } catch (error) {
        if (!current()) return;
        this.controller = null;
        this.setBusy(false);
        this.status.textContent = "読み上げを完了できませんでした。返答は文字で確認できます。";
        this.showError(error.name === "TypeError" || error.name === "AbortError" ? unavailable : error.message);
      }
    }

    destroy() {
      this.destroyed = true;
      this.cancel();
      this.engineController?.abort();
      if (this.context && this.context.state !== "closed") this.context.close().catch(() => {});
    }
  }

  window.RobotSpeech = RobotSpeech;
})();
