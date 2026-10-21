(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const labels = {
    greet:"あいさつ",nod:"うなずく",look_ball:"ボールを見る",raise_arm:"腕を上げる",home:"基本姿勢",
    pick_cube:"箱を持ち上げる",place_cube:"箱を置く",pick_ball:"ボールを持ち上げる",place_ball:"ボールを置く",
    open_hand:"手を開く",close_hand:"手を閉じる",forward:"前進",backward:"後退",turn_left:"左旋回",turn_right:"右旋回",stop:"停止"
  };
  let processing = false, recorder = null, microphone = null, recordingTimer = null;
  let discardRecording = false, requestGeneration = 0, apiConfigured = false;
  let lastRobotMessage = null, latestRequestMessage = false;
  let draftBusy = false, savedBusy = false, runPending = false, microphonePending = false, programVoiceBusy = false;
  let connected = false, robotBusy = false, robotReady = false, programRunning = false;
  let currentRevision = null, stateEpoch = 0, draftRequest = 0, commandEpoch = null;
  let conversation = null, conversationInitialized = false, activeRequest = null, retryMessage = null;
  let pendingMessage = "", pendingRun = null, stopping = false;
  let latestProgram = {};
  const revisions = new Map();
  const chatEvents = new Map(), runContexts = new Map();
  const conversationKey = "robot-household.active-conversation";
  const storageRead = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const storageWrite = (key, value) => { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (_) { /* Private browsing may disable storage. */ } };
  const fail = message => { el("error").textContent = message; el("error").hidden = false; };
  const clearError = () => { el("error").textContent = ""; el("error").hidden = true; };
  const programFail = message => { el("program-error").textContent = message; el("program-error").hidden = false; };
  const clearProgramError = () => { el("program-error").textContent = ""; el("program-error").hidden = true; };
  const display = value => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
  const robotSpeech = new RobotSpeech({
    toggle:el("robot-speech-auto"), stopButton:el("robot-speech-stop"),
    status:el("robot-speech-status"), error:el("robot-speech-error"),
    speaker:el("robot-speech-speaker"), speed:el("robot-speech-speed"),
    refreshButton:el("robot-speech-refresh"), credit:el("robot-speech-credit")
  });
  const programVoice = new ProgramVoice({
    input:el("program-instruction"), button:el("program-voice"), cancelButton:el("program-voice-cancel"),
    meter:el("program-voice-level"), levelText:el("program-voice-level-text"),
    status:el("program-voice-status"), error:el("program-voice-error"),
    onBusy:value => { programVoiceBusy = value; updateControls(); }
  });

  function renderProgramState() {
    const program = latestProgram;
    const selectedRun = currentRevision && program.revision_id === currentRevision.id;
    const labels = {idle:"未実行",running:"実行中",completed:"完了",succeeded:"完了",success:"完了",stopped:"停止",cancelled:"停止",failed:"失敗",error:"エラー"};
    el("program-status").textContent = selectedRun ?
      (program.running ? "実行中" : (labels[program.status] || program.status || "未実行")) :
      (program.running ? "別のプログラムを実行中" : "未実行（表示中の版）");
    if (!runPending) el("program-message").textContent = selectedRun ? display(program.error || program.message) : "";
    el("program-report").textContent = selectedRun ? display(program.report) : "";
    el("program-steps").textContent = program.run_id || program.steps?.length ? JSON.stringify(program, null, 2) : "まだ実行していません。";
  }

  function updateControls() {
    const recording = recorder?.state === "recording";
    const inputActive = Boolean(recording || microphonePending || programVoiceBusy);
    robotSpeech.setInputActive(inputActive);
    document.querySelectorAll("[data-speak-message]").forEach(button => { button.disabled = inputActive || stopping; });
    const locked = draftBusy || processing || programRunning || runPending || robotBusy || recording || microphonePending || programVoiceBusy || stopping;
    el("chat-send").disabled = locked || !connected || !apiConfigured || !conversationInitialized;
    el("new-conversation").disabled = locked || !connected || !conversationInitialized;
    el("program-instruction").disabled = draftBusy || stopping;
    el("program-instruction").readOnly = programVoiceBusy;
    document.querySelectorAll("[data-run-revision]").forEach(button => {
      button.disabled = locked || !connected || !robotReady || conversation?.status !== "ready" ||
        button.dataset.runRevision !== conversation?.revision_id;
    });
    el("save-program").disabled = locked || !connected || !currentRevision;
    el("save-name").disabled = locked;
    el("refresh-saved").disabled = savedBusy || locked || !connected;
    document.querySelectorAll("[data-load-revision]").forEach(button => { button.disabled = locked || !connected; });
    const manualLocked = draftBusy || processing || programRunning || runPending || robotBusy || microphonePending || programVoiceBusy || stopping;
    el("send").disabled = manualLocked || recording || !connected || !robotReady;
    el("record").disabled = recording ? false : manualLocked || !connected || !robotReady;
    document.querySelectorAll("[data-action]").forEach(button => {
      button.disabled = manualLocked || recording || !connected || !robotReady;
    });
    programVoice.setAvailable(!locked && connected && apiConfigured && conversationInitialized);
    // Stop is intentionally never disabled, including during API requests.
  }

  function addChatEvent(id, text, identity = conversation?.id, speak = false) {
    if (!identity) return;
    const events = chatEvents.get(identity) || [];
    if (events.some(item => item.id === id)) return;
    const afterMessage = identity === conversation?.id ? conversation.messages.at(-1)?.id : null;
    events.push({id, role:"assistant", text, kind:"message", after_message_id:afterMessage});
    chatEvents.set(identity, events);
    if (identity === conversation?.id) {
      renderChat();
      if (speak && !stopping) robotSpeech.speak(text, {automatic:true});
    }
  }

  function renderChat() {
    const messages = conversation?.messages || [];
    const items = [...messages];
    if (currentRevision && !items.some(item => item.kind === "program" && item.revision_id === currentRevision.id)) {
      items.unshift({
        id:"loaded-" + currentRevision.id, role:"assistant", kind:"program", revision_id:currentRevision.id,
        text:"保存されたプログラムを読み込みました。\n" + currentRevision.summary
      });
    }
    if (!items.length) items.push({
      id:"welcome", role:"assistant",
      text:"どんな点検をしましょうか？ 足りない内容があれば質問します。内容がそろったらプログラムを作ります。"
    });
    const events = chatEvents.get(conversation?.id) || [];
    const all = [];
    for (const item of items) {
      all.push(item);
      all.push(...events.filter(event => event.after_message_id === item.id));
    }
    all.push(...events.filter(event => !items.some(item => item.id === event.after_message_id)));
    if (pendingMessage) all.push({id:"pending", role:"user", text:pendingMessage, pending:true});
    // Reuse message nodes: polling and control changes must not repeat live announcements.
    const log = el("chat-log");
    const followLatest = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    const existing = new Map([...log.children].map(node => [node.dataset.messageId, node]));
    const ids = new Set(all.map(item => item.id));
    for (const child of [...log.children]) if (!ids.has(child.dataset.messageId)) child.remove();
    for (const item of all) {
      let node = existing.get(item.id);
      if (!node) {
        node = document.createElement("article");
        node.dataset.messageId = item.id;
        node.className = "chat-message" + (item.role === "user" ? " chat-message--user" : "") +
          (item.pending ? " chat-message--pending" : "");
        const speaker = document.createElement("p");
        speaker.className = "chat-speaker";
        speaker.textContent = item.role === "user" ? "あなた" : "ロボット";
        const text = document.createElement("p");
        text.textContent = item.text;
        node.append(speaker, text);
        if (item.role === "assistant") {
          const replay = document.createElement("button");
          replay.type = "button"; replay.className = "ui-btn ui-btn--secondary speech-replay";
          replay.dataset.speakMessage = item.id;
          replay.textContent = "読み上げる";
          replay.setAttribute("aria-label", "このロボットの返答を読み上げる");
          replay.addEventListener("click", () => {
            robotSpeech.unlock();
            robotSpeech.speak(item.text);
          });
          node.append(replay);
        }
        if (item.kind === "program" && item.revision_id) {
          const run = document.createElement("button");
          run.type = "button"; run.className = "ui-btn";
          run.dataset.runRevision = item.revision_id;
          run.addEventListener("click", () => runConversation(run));
          const note = document.createElement("p");
          note.className = "chat-note";
          note.dataset.revisionNote = item.revision_id;
          node.append(run, note);
        }
        if (item.pending) {
          const note = document.createElement("p");
          note.className = "chat-note"; note.textContent = "返答を待っています…";
          node.append(note);
        }
        log.append(node);
      }
      // Insert newly generated messages before older execution events when needed.
      const position = all.indexOf(item);
      if (log.children[position] !== node) log.insertBefore(node, log.children[position] || null);
      const run = node.querySelector("[data-run-revision]");
      if (run) {
        const latest = conversation?.status === "ready" && conversation.revision_id === run.dataset.runRevision;
        run.textContent = latest ? "このプログラムを実行" : "以前の版・実行できません";
        node.querySelector("[data-revision-note]").textContent = latest ?
          "内容を確認してから実行してください。変更したい点は、続けてメッセージで伝えられます。" :
          "質問への回答待ち、または新しい版に更新されています。";
      }
    }
    if (followLatest || pendingMessage) log.scrollTop = log.scrollHeight;
    updateControls();
  }

  function applyConversation(result) {
    if (!result.conversation?.id || !Array.isArray(result.conversation.messages)) throw new Error("会話を読み取れませんでした。ページを再読み込みしてください。");
    const changed = conversation?.id !== result.conversation.id;
    conversation = result.conversation;
    storageWrite(conversationKey, conversation.id);
    if (changed) {
      robotSpeech.cancel();
      el("chat-log").replaceChildren();
      try {
        const previousRun = JSON.parse(storageRead("robot-household.run." + conversation.id));
        if (previousRun?.run_id && previousRun?.revision_id) runContexts.set(previousRun.run_id, {...previousRun, conversation_id:conversation.id, autoSpeech:false});
      } catch (_) { /* A stale local bookmark is harmless. */ }
    }
    if (result.revision) chooseRevision(result.revision);
    else {
      currentRevision = null;
      el("program-summary").textContent = "まだプログラムはありません。";
      el("program-code").textContent = "まだ生成されていません。";
      el("revision-info").textContent = "";
      el("revision-history").replaceChildren();
      el("save-status").textContent = "";
      renderProgramState();
    }
    renderChat();
    el("program-input-status").textContent = conversation.status === "clarification" ?
      "ロボットの質問に、音声または文字で答えてください。" :
      conversation.status === "ready" ? "プログラムを用意しました。実行するか、変更したい点を伝えてください。" :
      "音声または文字で、点検の依頼を送信してください。";
  }

  function chooseRevision(revision) {
    if (!revision?.id || typeof revision.code !== "string") throw new Error("生成結果にプログラムがありません。もう一度お試しください。");
    currentRevision = revision;
    revisions.set(revision.id, revision);
    el("program-summary").textContent = revision.summary || "プログラムを読み込みました。コードを確認して実行できます。";
    el("program-code").textContent = revision.code;
    const date = revision.created_at ? new Date(revision.created_at).toLocaleString("ja-JP") : "";
    el("revision-info").textContent = "現在の版：" + revision.id + (date ? " ／ " + date : "") +
      (revision.parent_id ? " ／ 修正元：" + revision.parent_id : " ／ 新規生成");
    el("revision-history").replaceChildren();
    for (const item of [...revisions.values()].reverse()) {
      const li = document.createElement("li");
      const text = document.createElement("p");
      text.textContent = (item.id === revision.id ? "表示中： " : "") + (item.instruction || item.summary || item.id);
      li.append(text);
      if (item.id !== revision.id) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ui-btn ui-btn--secondary";
        button.dataset.loadRevision = item.id;
        button.textContent = "この版を読み込む";
        button.addEventListener("click", () => loadRevision(item.id));
        li.append(button);
      }
      el("revision-history").append(li);
    }
    renderProgramState();
    updateControls();
  }

  async function read(url, signal) {
    const response = await fetch(url, {cache:"no-store", signal});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(typeof result.detail === "string" ? result.detail : "取得できませんでした。接続を確認してください。");
      error.status = response.status;
      throw error;
    }
    return result;
  }

  async function initializeConversation() {
    if (conversationInitialized || draftBusy || stopping) return;
    const token = ++draftRequest;
    draftBusy = true;
    const controller = new AbortController();
    activeRequest = controller;
    el("program-input-status").textContent = "相談を読み込んでいます。";
    updateControls();
    try {
      const saved = storageRead(conversationKey);
      let result;
      if (saved) {
        try { result = await read("/api/programs/conversations/" + encodeURIComponent(saved), controller.signal); }
        catch (error) {
          if (error.status !== 404 && error.status !== 422) throw error;
          storageWrite(conversationKey, null);
        }
      }
      if (!result) result = await request("/api/programs/conversations", {base_revision_id:null}, false, controller.signal);
      if (token === draftRequest) {
        conversationInitialized = true;
        applyConversation(result);
      }
    } catch (error) {
      if (token === draftRequest && error.name !== "AbortError") programFail("相談を開始できませんでした。 " + error.message);
    } finally {
      if (token === draftRequest) { draftBusy = false; activeRequest = null; updateControls(); }
    }
  }

  async function sendMessage() {
    if (el("chat-send").disabled || !conversation) return;
    const text = el("program-instruction").value.trim();
    if (!text) { programFail("ロボットへのメッセージを入力してください。"); return; }
    robotSpeech.cancel();
    robotSpeech.unlock();
    const token = ++draftRequest;
    let returnFocus = false;
    const identity = conversation.id;
    const payload = retryMessage?.conversation_id === identity && retryMessage.text === text &&
      retryMessage.expected_version === conversation.version ? retryMessage : {
        conversation_id:identity, text, expected_version:conversation.version,
        request_id:crypto.randomUUID().replaceAll("-", "")
      };
    retryMessage = payload;
    pendingMessage = text;
    draftBusy = true;
    const controller = new AbortController();
    activeRequest = controller;
    clearProgramError();
    el("program-input-status").textContent = "ロボットが依頼を確認しています。内容がそろっていればプログラムを作ります。";
    renderChat();
    try {
      const {conversation_id, ...body} = payload;
      const result = await request("/api/programs/conversations/" + encodeURIComponent(identity) + "/messages", body, false, controller.signal);
      if (token !== draftRequest) return;
      pendingMessage = "";
      retryMessage = null;
      el("program-instruction").value = "";
      const previousMessages = new Set(conversation.messages.map(message => message.id));
      applyConversation(result);
      const reply = conversation.messages.findLast(message => message.role === "assistant" && !previousMessages.has(message.id));
      if (reply) robotSpeech.speak(reply.text, {automatic:true});
      el("save-status").textContent = result.revision ? "表示中の版を名前を付けて保存できます。" : "";
      returnFocus = true;
    } catch (error) {
      if (token !== draftRequest || error.name === "AbortError") return;
      pendingMessage = "";
      if (error.status === 409) {
        try {
          const result = await read("/api/programs/conversations/" + encodeURIComponent(identity), controller.signal);
          if (token !== draftRequest) return;
          applyConversation(result);
          retryMessage = null;
        } catch (_) { /* Keep the known conversation and draft when recovery also fails. */ }
      }
      if (token !== draftRequest) return;
      programFail(error.message);
      el("program-input-status").textContent = "送信を完了できませんでした。入力を残しています。内容を確認して再送してください。";
      renderChat();
    } finally {
      if (token === draftRequest) {
        draftBusy = false; activeRequest = null; pendingMessage = ""; updateControls();
        if (returnFocus) el("program-instruction").focus();
      }
    }
  }
  el("program-form").addEventListener("submit", event => { event.preventDefault(); sendMessage(); });
  el("program-instruction").addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault(); sendMessage();
    }
  });
  el("new-conversation").addEventListener("click", () => createConversation(null));

  function rememberRun(runId, revisionId, conversationId) {
    const context = {run_id:runId, revision_id:revisionId, conversation_id:conversationId, autoSpeech:true};
    runContexts.set(runId, context);
    storageWrite("robot-household.run." + conversationId, JSON.stringify(context));
    addChatEvent(runId + ":started", "プログラムの実行を開始しました。", conversationId);
  }

  function showRunProgress(program) {
    if (!program.run_id) return;
    if (pendingRun?.revision_id === program.revision_id && program.run_id !== pendingRun.previous_run_id && !runContexts.has(program.run_id)) {
      rememberRun(program.run_id, program.revision_id, pendingRun.conversation_id);
      pendingRun = null;
    }
    const context = runContexts.get(program.run_id);
    if (!context || context.revision_id !== program.revision_id) return;
    addChatEvent(program.run_id + ":started", "プログラムの実行を開始しました。", context.conversation_id);
    if (program.running) return;
    if (["completed","succeeded","success"].includes(program.status)) {
      addChatEvent(program.run_id + ":finished", "点検が完了しました。" +
        (program.report ? "\n" + display(program.report) : ""), context.conversation_id, context.autoSpeech);
    } else if (["failed","error"].includes(program.status)) {
      addChatEvent(program.run_id + ":finished", "点検を完了できませんでした。\n" +
        display(program.error || program.message || "実行中にエラーが発生しました。"), context.conversation_id, context.autoSpeech);
    } else if (["stopped","cancelled"].includes(program.status)) {
      addChatEvent(program.run_id + ":finished", "点検を停止しました。", context.conversation_id, context.autoSpeech);
    }
  }

  async function runConversation(button) {
    if (button.disabled || !currentRevision || conversation?.status !== "ready") return;
    robotSpeech.cancel();
    robotSpeech.unlock();
    const revisionId = currentRevision.id;
    const identity = conversation.id;
    const generation = requestGeneration;
    runPending = true;
    pendingRun = {revision_id:revisionId, conversation_id:identity, previous_run_id:latestProgram.run_id};
    stateEpoch++;
    clearProgramError();
    el("program-message").textContent = "プログラムの実行を依頼しています。";
    updateControls();
    try {
      const payload = {expected_version:conversation.version};
      if (Number.isInteger(commandEpoch)) payload.expected_epoch = commandEpoch;
      const result = await request("/api/programs/conversations/" + encodeURIComponent(identity) + "/run", payload);
      if (generation !== requestGeneration) return;
      stateEpoch++;
      if (result.accepted) {
        programRunning = true;
        if (result.run_id) { rememberRun(result.run_id, revisionId, identity); pendingRun = null; }
        el("program-report").textContent = "";
        el("program-status").textContent = "実行中";
      }
      el("program-message").textContent = result.message || (result.accepted ? "実行を開始しました。" : "実行は開始していません。");
      if (!result.accepted) addChatEvent("run-denied-" + crypto.randomUUID(), el("program-message").textContent, identity, true);
    } catch (error) {
      if (generation === requestGeneration) {
        programFail(error.message);
        el("program-message").textContent = "実行を開始できませんでした。";
      }
    } finally {
      if (generation === requestGeneration) runPending = false;
      updateControls();
      poll();
    }
  }

  async function refreshSaved() {
    if (savedBusy) return;
    savedBusy = true;
    el("saved-status").textContent = "保存したプログラムを確認しています。";
    updateControls();
    try {
      const result = await read("/api/programs/saved");
      el("saved-list").replaceChildren();
      for (const item of result.programs || []) {
        const li = document.createElement("li");
        const text = document.createElement("span");
        text.textContent = item.name;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ui-btn ui-btn--secondary";
        button.textContent = "読み込む";
        button.setAttribute("aria-label", item.name + "を読み込む");
        button.dataset.loadRevision = item.revision_id;
        button.addEventListener("click", () => loadRevision(item.revision_id, item.name));
        li.append(text, button);
        el("saved-list").append(li);
      }
      el("saved-status").textContent = result.programs?.length ? result.programs.length + "件の保存があります。読み込むだけでは実行しません。" : "保存したプログラムはまだありません。";
    } catch (error) { el("saved-status").textContent = error.message; }
    finally { savedBusy = false; updateControls(); }
  }
  el("refresh-saved").addEventListener("click", refreshSaved);

  async function createConversation(baseRevisionId, name) {
    if (draftBusy || programRunning || runPending || processing || robotBusy || microphonePending || programVoiceBusy || stopping || recorder?.state === "recording") return;
    robotSpeech.cancel();
    const token = ++draftRequest;
    draftBusy = true;
    clearProgramError();
    const controller = new AbortController();
    activeRequest = controller;
    el("program-input-status").textContent = "相談を準備しています。";
    updateControls();
    try {
      const result = await request("/api/programs/conversations", {base_revision_id:baseRevisionId}, false, controller.signal);
      if (token !== draftRequest) return;
      pendingMessage = ""; retryMessage = null;
      applyConversation(result);
      if (name) el("save-name").value = name;
      el("program-instruction").value = "";
      el("program-instruction").placeholder = baseRevisionId ?
        "例：青い物だけにして。待つ時間を5秒、うなずきを1回に変更して。" :
        "例：机の上の物を持ち上げて点検して。少し待ってから戻して。";
      el("save-status").textContent = name ? "保存した「" + name + "」を読み込みました。" : "";
    } catch (error) {
      if (token === draftRequest) {
        programFail(error.message);
        el("program-input-status").textContent = "読み込めませんでした。現在のプログラムを保持しています。";
      }
    } finally {
      if (token === draftRequest) { draftBusy = false; activeRequest = null; updateControls(); el("program-instruction").focus(); }
    }
  }
  function loadRevision(id, name) { return createConversation(id, name); }

  el("save-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (el("save-program").disabled || !currentRevision) return;
    const name = el("save-name").value.trim();
    if (!name) { programFail("保存する名前を入力してください。"); return; }
    const revisionId = currentRevision.id;
    const token = ++draftRequest;
    const controller = new AbortController();
    activeRequest = controller;
    draftBusy = true;
    clearProgramError();
    el("save-status").textContent = "保存しています。";
    updateControls();
    try {
      const result = await request("/api/programs/save", {revision_id:revisionId, name}, false, controller.signal);
      if (token !== draftRequest) return;
      el("save-status").textContent = result.message || "「" + name + "」を保存しました。";
      await refreshSaved();
    } catch (error) {
      if (token === draftRequest && error.name !== "AbortError") {
        programFail(error.message);
        el("save-status").textContent = "保存を確認できませんでした。";
      }
    } finally { if (token === draftRequest) { draftBusy = false; activeRequest = null; updateControls(); } }
  });

  async function request(url, body, multipart = false, signal) {
    const response = await fetch(url, {
      method:"POST", headers: multipart ? {} : {"Content-Type":"application/json"},
      body: multipart ? body : JSON.stringify(body), signal
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(typeof result.detail === "string" ? result.detail : "入力内容を確認してください。");
      error.status = response.status;
      throw error;
    }
    return result;
  }
  function busy(value) {
    processing = value;
    updateControls();
  }
  function showResult(result) {
    latestRequestMessage = true;
    if (result.text) el("last-input").textContent = result.text;
    el("reply").textContent = result.message || (result.accepted ? "指示を受け付けました。実行状態を確認しています。" : "動作は開始していません。");
  }
  async function submit(url, payload, input, multipart = false) {
    if (processing || programRunning || runPending || draftBusy || programVoiceBusy) return;
    robotSpeech.cancel();
    const generation = requestGeneration;
    clearError(); busy(true);
    el("last-input").textContent = input;
    el("input-status").textContent = multipart ? "音声を認識しています。" : "指示を処理しています。";
    try {
      const result = await request(url, payload, multipart);
      if (generation === requestGeneration) showResult(result);
    } catch (error) {
      if (generation === requestGeneration) fail(error.message);
    } finally {
      if (generation === requestGeneration) {
        busy(false);
        el("input-status").textContent = "";
      }
      poll();
    }
  }
  el("command-form").addEventListener("submit", event => {
    event.preventDefault();
    const text = el("command").value.trim();
    if (text) submit("/api/text", {text}, text);
  });
  document.querySelectorAll("[data-action]").forEach(button => {
    button.addEventListener("click", () => submit("/api/action", {action:button.dataset.action}, button.textContent));
  });
  el("stop").addEventListener("click", async () => {
    robotSpeech.cancel();
    for (const context of runContexts.values()) context.autoSpeech = false;
    requestGeneration++;
    stateEpoch++;
    const stopToken = ++draftRequest;
    activeRequest?.abort();
    activeRequest = null;
    draftBusy = false;
    pendingMessage = "";
    retryMessage = null;
    runPending = false;
    pendingRun = null;
    stopping = true;
    programVoice.cancel();
    discardRecording = true;
    if (recorder && recorder.state === "recording") recorder.stop();
    clearTimeout(recordingTimer);
    microphone?.getTracks().forEach(track => track.stop());
    clearError();
    clearProgramError();
    // Keep Stop enabled even during transcription, interpretation, and polling.
    el("reply").textContent = "停止を送信しています。";
    el("program-message").textContent = "停止を送信しています。";
    el("input-status").textContent = "";
    el("program-input-status").textContent = "停止を依頼しています。";
    renderChat();
    try {
      const result = await request("/api/action", {action:"stop"});
      if (stopToken !== draftRequest) return;
      stateEpoch++;
      showResult(result);
      el("program-message").textContent = result.message || "停止を依頼しました。";
      if (conversation) {
        // A response may have reached the server just before Stop; fetch its committed state.
        try {
          const recovered = await read("/api/programs/conversations/" + encodeURIComponent(conversation.id));
          if (stopToken === draftRequest) applyConversation(recovered);
        } catch (_) { /* Existing transcript remains available when reconnection fails. */ }
      }
      if (stopToken === draftRequest) el("program-input-status").textContent = "停止を依頼しました。未送信の入力は残しています。";
    } catch (error) {
      const message = "停止を確認できませんでした。シミュレータのターミナルも確認してください。 " + error.message;
      fail(message); programFail(message);
    }
    finally { if (stopToken === draftRequest) { stopping = false; busy(false); poll(); } }
  });
  function resetRecordingUI() {
    clearTimeout(recordingTimer);
    microphone?.getTracks().forEach(track => track.stop());
    microphone = null;
    el("record").textContent = "録音して指示する";
    el("record").setAttribute("aria-pressed", "false");
    updateControls();
  }
  el("record").addEventListener("click", async () => {
    if (recorder && recorder.state === "recording") { recorder.stop(); return; }
    if (processing || programRunning || runPending || draftBusy || robotBusy || programVoiceBusy) return;
    clearError();
    if (!apiConfigured) { fail("音声認識にはUbuntu側のOPENAI_API_KEYが必要です。ボタンと定型文は使えます。"); return; }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      fail("このブラウザでは録音できません。WindowsのChrome／Edgeでlocalhostの画面を開いてください。"); return;
    }
    microphonePending = true;
    updateControls();
    const recordingGeneration = requestGeneration;
    try {
      microphone = await navigator.mediaDevices.getUserMedia({audio:true});
      if (recordingGeneration !== requestGeneration) { resetRecordingUI(); return; }
      const mimeType = ["audio/webm;codecs=opus","audio/webm","audio/ogg;codecs=opus","audio/mp4"].find(type => MediaRecorder.isTypeSupported(type));
      if (!mimeType) throw new Error("対応する録音形式がありません。ChromeまたはEdgeを使ってください。");
      recorder = new MediaRecorder(microphone, {mimeType});
      const chunks = [];
      discardRecording = false;
      recorder.addEventListener("dataavailable", event => { if (event.data.size) chunks.push(event.data); });
      recorder.addEventListener("stop", () => {
        resetRecordingUI();
        if (discardRecording) { el("input-status").textContent = "録音を取り消しました。"; return; }
        const blob = new Blob(chunks, {type:mimeType});
        const extension = mimeType.includes("ogg") ? "ogg" : mimeType.includes("mp4") ? "mp4" : "webm";
        const form = new FormData();
        form.append("file", blob, "command." + extension);
        submit("/api/audio", form, "録音した指示", true);
      });
      recorder.addEventListener("error", () => { discardRecording = true; resetRecordingUI(); fail("録音中にエラーが発生しました。"); });
      recorder.start();
      el("record").textContent = "録音を終えて指示する";
      el("record").setAttribute("aria-pressed", "true");
      el("input-status").textContent = "録音中です。もう一度ボタンを押すと送信します（最大20秒）。";
      recordingTimer = setTimeout(() => { if (recorder?.state === "recording") recorder.stop(); }, 20000);
    } catch (error) { resetRecordingUI(); fail("マイクを使えませんでした。 " + error.message); }
    finally { microphonePending = false; updateControls(); }
  });
  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    const pollEpoch = stateEpoch;
    try {
      const response = await fetch("/api/state", {cache:"no-store"});
      if (!response.ok) throw new Error("状態取得エラー");
      const state = await response.json();
      if (pollEpoch !== stateEpoch) return;
      const wasConnected = connected;
      connected = true;
      robotBusy = Boolean(state.busy);
      robotReady = Boolean(state.ready);
      programRunning = Boolean(state.program?.running);
      latestProgram = state.program || {};
      commandEpoch = Number.isInteger(state.command_epoch) ? state.command_epoch : null;
      renderProgramState();
      showRunProgress(latestProgram);
      el("robot-status").textContent = state.error ? "エラー" : state.busy ?
        "実行中：" + (labels[state.current_action] || state.current_action || "動作") :
        state.ready ? "待機中" : "シミュレータの準備待ち";
      const held = state.holding;
      el("holding").textContent = held ? (String(held).includes("cube") ? "赤い箱" : String(held).includes("ball") ? "青いボール" : String(held)) : "なし";
      if (state.message !== lastRobotMessage) {
        if (state.message && !processing) {
          el("reply").textContent = state.message;
          latestRequestMessage = false;
        }
        lastRobotMessage = state.message;
      } else if (state.message && !processing && !latestRequestMessage) {
        el("reply").textContent = state.message;
      }
      if (state.error) fail(String(state.error));
      const {api, ...robot} = state;
      el("state-detail").textContent = JSON.stringify(robot, null, 2);
      apiConfigured = Boolean(api?.configured);
      if (!wasConnected && !draftBusy && !apiConfigured) el("program-input-status").textContent =
        "相談・生成にはAPIキーの設定が必要です。保存済みプログラムの読み込み・実行は使えます。";
      el("api-config").textContent = apiConfigured ?
        "APIキー：設定済み ／ プログラム生成：" + (api.program_model || "未報告") + " ／ 生成の処理方式：" + (api.program_service_tier || "未報告") +
        " ／ リアルタイム文字起こし：" + (api.realtime_transcribe_model || "未報告") + " ／ 単発指示解釈：" + api.model + " ／ 単発音声認識：" + api.transcribe_model :
        "APIキー：未設定。ボタンと定型文で操作できます。";
      el("usage").textContent = api?.recent_usage?.length ? JSON.stringify(api.recent_usage, null, 2) : "この起動中のAPI呼び出しはまだありません。";
      if (!wasConnected) refreshSaved();
      if (!conversationInitialized && !draftBusy && !stopping) initializeConversation();
    } catch (_) {
      connected = false;
      el("robot-status").textContent = "サーバーに接続できません";
      if (!draftBusy) el("program-input-status").textContent = "サーバーに接続できません。起動用の画面を確認してください。";
    } finally { polling = false; updateControls(); }
  }
  window.addEventListener("beforeunload", () => {
    robotSpeech.destroy();
    activeRequest?.abort();
    programVoice.cancel();
    microphone?.getTracks().forEach(track => track.stop());
  });
  window.addEventListener("resize", () => {
    const log = el("chat-log");
    log.scrollTop = log.scrollHeight;
  });
  renderChat();
  poll();
  setInterval(poll, 1000);
})();
