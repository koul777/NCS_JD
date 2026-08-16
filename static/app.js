(() => {
  "use strict";

  const connected = document.body.dataset.backendState === "connected";
  const form = document.getElementById("generator-form");
  const announcementInput = document.getElementById("announcement-file");
  const announcementText = document.getElementById("announcement-text");
  const jobTitleInput = document.getElementById("job-title");
  const templateInput = document.getElementById("template-file");
  const runModeInputs = Array.from(document.querySelectorAll('input[name="run_mode"]'));
  const engineInputs = Array.from(document.querySelectorAll('input[name="engine"]'));
  const enginePicker = document.querySelector("[data-engine-picker]");
  const submitButton = form?.querySelector('button[type="submit"]');
  const statusNode = document.querySelector("[data-status]");
  const errorNode = document.querySelector("[data-error]");
  const progress = document.querySelector("[data-progress]");
  const result = document.querySelector("[data-result]");
  let lastDownload = null;

  const setStatus = (message) => {
    if (statusNode) statusNode.textContent = message;
    if (errorNode) errorNode.textContent = "";
  };

  const setError = (message) => {
    if (errorNode) errorNode.textContent = message;
    if (statusNode) statusNode.textContent = "";
  };

  const localTimestamp = (date = new Date()) => {
    const pad = (value) => String(Math.abs(Math.trunc(value))).padStart(2, "0");
    const offset = -date.getTimezoneOffset();
    const sign = offset < 0 ? "-" : "+";
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
      `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
      `${sign}${pad(offset / 60)}:${pad(offset % 60)}`
    );
  };

  const filenameFromHeader = (header) => {
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header || "");
    if (encoded) {
      try { return decodeURIComponent(encoded[1]); } catch { /* use fallback */ }
    }
    const plain = /filename="([^"]+)"/i.exec(header || "");
    return plain?.[1] || "NCS_직무기술서.hwpx";
  };

  const download = (blob, filename) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const setProgress = (activeIndex) => {
    if (!progress) return;
    progress.hidden = false;
    const steps = Array.from(progress.querySelectorAll("li"));
    steps.forEach((step, index) => {
      step.classList.toggle("is-active", index === activeIndex);
      step.classList.toggle("is-done", index < activeIndex);
    });
  };

  const bindFileName = (input, key, emptyText) => {
    input?.addEventListener("change", () => {
      const node = document.querySelector(`[data-file-name="${key}"]`);
      if (node) node.textContent = input.files?.[0]?.name || emptyText;
      result.hidden = true;
    });
  };
  bindFileName(announcementInput, "announcement", "파일을 선택하세요");
  bindFileName(templateInput, "template", "없으면 표준 양식 사용");
  // Two controls, one decision: the run mode says whether an AI is involved at
  // all, and the engine says which one.  Everything downstream reads the single
  // provider name they resolve to.
  const usesAgent = () =>
    runModeInputs.find((input) => input.checked)?.value === "agent";
  const selectedEngine = () => engineInputs.find((input) => input.checked)?.value || "";
  const selectedProvider = () => (usesAgent() ? selectedEngine() || "agent" : "off");
  const syncProvider = () => {
    const agent = usesAgent();
    if (enginePicker) {
      // The engines stay on screen even when the local path is chosen: hiding
      // them made the feature look deleted.  Idle means visible but inert --
      // the controls are dead and no login state has been fetched yet.
      const wasIdle = enginePicker.classList.contains("is-idle");
      enginePicker.classList.toggle("is-idle", !agent);
      if (!agent) {
        engineInputs.forEach((input) => {
          input.checked = false;
          input.disabled = true;
        });
        enginePicker
          .querySelectorAll("[data-login-provider], [data-provider-refresh]")
          .forEach((button) => { button.disabled = true; });
      } else if (wasIdle) {
        // provider-login.js re-enables each control from the real login state;
        // until that lands the controls stay dead rather than falsely ready.
        enginePicker.querySelector("[data-provider-refresh]")?.removeAttribute("disabled");
        enginePicker.dispatchEvent(new CustomEvent("ncs-jd:engine-picker-shown"));
      }
    }
    const label = submitButton?.querySelector("strong");
    if (label) {
      label.textContent = agent ? "공고문 읽고 확인하기" : "NCS 직무기술서 만들기";
    }
  };
  runModeInputs.forEach((input) => input.addEventListener("change", syncProvider));
  engineInputs.forEach((input) => input.addEventListener("change", syncProvider));
  syncProvider();
  announcementInput?.addEventListener("change", () => {
    if (announcementInput.files?.length && announcementText) announcementText.value = "";
  });
  announcementText?.addEventListener("input", () => {
    if (announcementText.value.trim() && announcementInput) {
      announcementInput.value = "";
      const node = document.querySelector('[data-file-name="announcement"]');
      if (node) node.textContent = "붙여넣은 공고문 사용";
    }
  });

  // ---------------------------------------------------------------- agent flow
  // The agent path is two-step on purpose: a run costs minutes, so a person
  // confirms what was read out of the announcement before it starts.
  const review = document.querySelector("[data-review]");
  const reviewTitle = document.querySelector("[data-review-title]");
  const reviewDuties = document.querySelector("[data-review-duties]");
  const reviewQualifications = document.querySelector("[data-review-qualifications]");
  const reviewPreferences = document.querySelector("[data-review-preferences]");
  const reviewContext = document.querySelector("[data-review-context]");
  const reviewCount = document.querySelector("[data-review-count]");
  const agentPanel = document.querySelector("[data-agent-progress]");
  const agentLog = document.querySelector("[data-agent-log]");
  const agentBar = document.querySelector("[data-agent-bar]");
  const agentHeadline = document.querySelector("[data-agent-headline]");
  const agentElapsed = document.querySelector("[data-agent-elapsed]");
  const agentResult = document.querySelector("[data-agent-result]");
  const reviewSchema = document.querySelector("[data-review-schema]");
  let elapsedTimer = null;
  let agentTitle = "";
  // Labels read off an uploaded institution form, and the form itself, so the
  // export can rebuild that form rather than the standard layout.
  let agentLabels = [];
  let agentTemplate = null;
  let lastAgentPayload = null;

  const lines = (value) => value.split("\n").map((line) => line.trim()).filter(Boolean);

  const syncDutyCount = () => {
    if (reviewCount && reviewDuties) reviewCount.textContent = `${lines(reviewDuties.value).length}건`;
  };
  reviewDuties?.addEventListener("input", syncDutyCount);

  const showReviewGate = (extraction) => {
    const role = (extraction.role_candidates || [])
      .slice()
      .sort((a, b) => (b.duties?.length || 0) - (a.duties?.length || 0))[0];
    if (!role) {
      setError("공고문에서 직무수행내역을 찾지 못했습니다. 내용을 직접 붙여넣어 주세요.");
      return false;
    }
    reviewTitle.value = jobTitleInput?.value?.trim() || role.role_title?.text || "";
    reviewDuties.value = (role.duties || []).map((item) => item.text).join("\n");
    reviewQualifications.value = (role.qualifications || []).map((item) => item.text).join("\n");
    if (reviewPreferences) {
      reviewPreferences.value = (role.preferences || []).map((item) => item.text).join("\n");
    }
    syncDutyCount();
    review.hidden = false;
    review.scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  };

  const showSchema = (schema) => {
    if (!reviewSchema) return;
    const note = document.querySelector("[data-review-schema-note]");
    const list = document.querySelector("[data-review-schema-labels]");
    if (!schema || !schema.labels?.length) {
      reviewSchema.hidden = true;
      return;
    }
    if (note) {
      const dropped = schema.detected_field_count - schema.usable_label_count;
      note.textContent =
        `${schema.source_format.toUpperCase()} 양식에서 항목 ${schema.usable_label_count}개를 읽었습니다` +
        (dropped > 0 ? ` (항목명으로 보기 어려운 ${dropped}개는 제외).` : ".") +
        " 잘못 읽었으면 취소하고 표준 양식으로 진행하세요.";
    }
    if (list) {
      list.replaceChildren();
      schema.labels.forEach((label) => {
        const item = document.createElement("li");
        item.textContent = label;
        list.append(item);
      });
    }
    reviewSchema.hidden = false;
  };

  const addLogEntry = (event) => {
    if (!agentLog) return;
    const item = document.createElement("li");
    item.className = `agent-log-item is-${event.kind}`;
    const label = document.createElement("span");
    label.textContent = event.label;
    item.append(label);
    if (event.detail) {
      const detail = document.createElement("em");
      detail.textContent = event.detail;
      item.append(detail);
    }
    agentLog.append(item);
    item.scrollIntoView({ block: "nearest" });
  };

  const renderAgentResult = (payload, summaryOverride) => {
    const summary = document.querySelector("[data-agent-summary]");
    if (summary) {
      summary.textContent =
        summaryOverride ||
        `능력단위 ${payload.unit_codes.length}개 · 도구 호출 ${payload.tool_calls}회 · ` +
        `${Math.round(payload.duration_ms / 1000)}초 소요`;
    }
    const notesWrapper = document.querySelector("[data-agent-notes]");
    const notesList = document.querySelector("[data-agent-notes-list]");
    if (notesList) {
      notesList.replaceChildren();
      payload.notes.forEach((note) => {
        const item = document.createElement("li");
        item.textContent = note;
        notesList.append(item);
      });
      if (notesWrapper) notesWrapper.hidden = payload.notes.length === 0;
    }
    const fields = document.querySelector("[data-agent-fields]");
    if (fields) {
      fields.replaceChildren();
      payload.field_values.forEach(([label, value]) => {
        const block = document.createElement("article");
        block.className = "agent-field";
        const heading = document.createElement("h3");
        heading.textContent = label;
        // Editable because the export sends back exactly what is shown here;
        // a reviewer correcting a line must not have to redo the whole run.
        const body = document.createElement("textarea");
        body.dataset.agentFieldValue = label;
        body.value = value;
        body.rows = Math.min(14, Math.max(2, value.split("\n").length));
        // Textareas clip overflowing text on paper, so a mirror carries the
        // full value into print and is kept in step with every edit.
        const printable = document.createElement("p");
        printable.className = "agent-print-value";
        printable.textContent = value;
        body.addEventListener("input", () => {
          printable.textContent = body.value;
          body.rows = Math.min(14, Math.max(2, body.value.split("\n").length));
        });
        block.append(heading, body, printable);
        fields.append(block);
      });
    }
    lastAgentPayload = payload;
    agentTitle = payload.field_values.find(([label]) => label === "채용분야")?.[1] || "";
    agentResult.hidden = false;
  };

  const collectAgentFields = () =>
    Array.from(document.querySelectorAll("[data-agent-field-value]")).map((node) => ({
      label: node.dataset.agentFieldValue,
      value: node.value,
    }));

  const exportAgentDraft = async () => {
    const fields = collectAgentFields();
    const statusNode = document.querySelector("[data-agent-export-status]");
    const button = document.querySelector("[data-agent-export]");
    if (!fields.length) return;
    const title = agentTitle.trim() || reviewTitle?.value?.trim() || "직무기술서";
    if (statusNode) statusNode.textContent = "HWPX를 만들고 있습니다.";
    if (button) button.disabled = true;
    try {
      const response = await fetch("/api/agent-draft/export/hwpx", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_title: title,
          fields,
          ...(agentTemplate ? { template: agentTemplate } : {}),
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error?.message || "HWPX를 만들지 못했습니다.");
      }
      const blob = await response.blob();
      const filename = filenameFromHeader(response.headers.get("Content-Disposition"));
      // Deliberately not stored as `lastDownload`: pressing this button again
      // re-exports, which is what a reviewer who just edited a field wants.
      download(blob, filename);
      if (statusNode) statusNode.textContent = `${filename} 내려받기 완료.`;
    } catch (error) {
      if (statusNode) statusNode.textContent = error.message || "HWPX를 만들지 못했습니다.";
    } finally {
      if (button) button.disabled = false;
    }
  };

  document.querySelector("[data-agent-export]")?.addEventListener("click", exportAgentDraft);

  // A run costs minutes, so the reviewed draft is saveable as a plain local file
  // and can be reopened later without touching the announcement again.
  const DRAFT_SCHEMA = "ncs_jd.agent_draft/v1";

  const safeFilename = (value) => value.replace(/[\\/:*?"<>|]/g, "_").trim() || "직무기술서";

  const saveAgentDraft = () => {
    const fields = collectAgentFields();
    const statusNode = document.querySelector("[data-agent-export-status]");
    if (!fields.length || !lastAgentPayload) return;
    const title = agentTitle.trim() || reviewTitle?.value?.trim() || "직무기술서";
    const draft = {
      schema: DRAFT_SCHEMA,
      saved_at: localTimestamp(),
      job_title: title,
      unit_codes: lastAgentPayload.unit_codes,
      notes: lastAgentPayload.notes,
      // Kept with the draft so a reopened file exports into the same institution form.
      template: agentTemplate,
      field_values: fields.map(({ label, value }) => [label, value]),
    };
    const blob = new Blob([JSON.stringify(draft, null, 2)], { type: "application/json" });
    download(blob, `${safeFilename(title)}_초안.json`);
    if (statusNode) {
      statusNode.textContent = "초안을 저장했습니다. 위쪽 '저장한 초안 열기'로 이어서 검토할 수 있습니다.";
    }
  };

  const parseSavedDraft = (text) => {
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error("JSON 파일을 읽지 못했습니다.");
    }
    if (!data || data.schema !== DRAFT_SCHEMA) {
      throw new Error("이 프로그램에서 저장한 초안 파일이 아닙니다.");
    }
    const strings = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === "string") : []);
    const fieldValues = (Array.isArray(data.field_values) ? data.field_values : []).filter(
      (pair) => Array.isArray(pair) && typeof pair[0] === "string" && typeof pair[1] === "string",
    );
    if (!fieldValues.length) throw new Error("초안 파일에 항목이 없습니다.");
    const template = data.template;
    const usableTemplate =
      template && typeof template.filename === "string" && typeof template.content_base64 === "string"
        ? { filename: template.filename, content_base64: template.content_base64 }
        : null;
    return {
      field_values: fieldValues.map(([label, value]) => [label, value]),
      unit_codes: strings(data.unit_codes),
      notes: strings(data.notes),
      tool_calls: 0,
      duration_ms: 0,
      job_title: typeof data.job_title === "string" ? data.job_title : "",
      saved_at: typeof data.saved_at === "string" ? data.saved_at : "",
      template: usableTemplate,
    };
  };

  const restoreAgentDraft = async (file) => {
    const note = document.querySelector("[data-restore-note]");
    if (note) note.textContent = "";
    try {
      const draft = parseSavedDraft(await file.text());
      review.hidden = true;
      if (agentPanel) agentPanel.hidden = true;
      if (progress) progress.hidden = true;
      if (result) result.hidden = true;
      agentTemplate = draft.template;
      agentLabels = draft.field_values.map(([label]) => label);
      const savedAt = draft.saved_at ? ` · ${draft.saved_at.slice(0, 16).replace("T", " ")} 저장` : "";
      renderAgentResult(draft, `저장한 초안 · 능력단위 ${draft.unit_codes.length}개${savedAt}`);
      if (!agentTitle) agentTitle = draft.job_title;
      setStatus("저장한 초안을 불러왔습니다. 고친 뒤 다시 내려받을 수 있습니다.");
      if (note) note.textContent = `${file.name} 불러옴`;
      agentResult.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      if (note) note.textContent = error.message || "초안을 불러오지 못했습니다.";
    }
  };

  document.querySelector("[data-agent-save]")?.addEventListener("click", saveAgentDraft);
  document.querySelector("[data-agent-print]")?.addEventListener("click", () => window.print());
  document.querySelector("[data-agent-restore]")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void restoreAgentDraft(file);
  });

  const runAgentDraft = async () => {
    const duties = lines(reviewDuties?.value || "");
    const title = reviewTitle?.value?.trim() || "";
    if (!title) { setError("직무명을 입력해 주세요."); return; }
    if (!duties.length) { setError("직무수행내역을 한 건 이상 입력해 주세요."); return; }

    review.hidden = true;
    agentResult.hidden = true;
    if (agentLog) agentLog.replaceChildren();
    agentPanel.hidden = false;
    agentBar?.classList.add("is-running");
    agentPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    setStatus("NCS 근거를 탐색하고 있습니다. 창을 닫지 마세요.");
    submitButton.disabled = true;

    const startedAt = Date.now();
    elapsedTimer = setInterval(() => {
      if (agentElapsed) agentElapsed.textContent = `${Math.round((Date.now() - startedAt) / 1000)}초`;
    }, 1000);

    try {
      const response = await fetch("/api/agent-draft", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_title: title,
          duties,
          qualifications: lines(reviewQualifications?.value || ""),
          preferences: lines(reviewPreferences?.value || ""),
          organization_context: reviewContext?.value?.trim() || "",
          template_labels: agentLabels,
          provider: selectedEngine(),
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error?.message || "에이전트 초안을 시작하지 못했습니다.");
      }
      // Read the NDJSON stream so each tool call appears as it happens rather
      // than the whole run landing at once.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          if (!part.trim()) continue;
          const event = JSON.parse(part);
          if (event.event === "progress") {
            addLogEntry(event);
            if (agentHeadline) agentHeadline.textContent = event.label;
          } else if (event.event === "result") {
            renderAgentResult(event);
            setStatus("정밀 탐색이 완료되었습니다. 내용을 검토해 주세요.");
          } else if (event.event === "error") {
            throw new Error(event.message || "에이전트 초안을 만들지 못했습니다.");
          }
        }
      }
      if (buffer.trim()) {
        const event = JSON.parse(buffer);
        if (event.event === "result") {
          renderAgentResult(event);
          setStatus("정밀 탐색이 완료되었습니다. 내용을 검토해 주세요.");
        } else if (event.event === "error") {
          throw new Error(event.message || "에이전트 초안을 만들지 못했습니다.");
        } else if (event.event === "progress") {
          addLogEntry(event);
          if (agentHeadline) agentHeadline.textContent = event.label;
        }
      }
    } catch (error) {
      setError(error.message || "에이전트 초안을 만들지 못했습니다.");
    } finally {
      clearInterval(elapsedTimer);
      agentBar?.classList.remove("is-running");
      submitButton.disabled = false;
    }
  };

  document.querySelector("[data-review-confirm]")?.addEventListener("click", runAgentDraft);
  document.querySelector("[data-review-cancel]")?.addEventListener("click", () => {
    review.hidden = true;
    setStatus("");
  });

  const toBase64 = async (file) => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    // Chunked because spreading a multi-MB array overflows the call stack.
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  };

  const readTemplateSchema = async (template) => {
    agentLabels = [];
    agentTemplate = null;
    showSchema(null);
    if (!template) return;
    setStatus("올린 양식의 항목을 읽고 있습니다.");
    const body = new FormData();
    body.append("template", template, template.name);
    const response = await fetch("/api/template/schema", { method: "POST", body });
    if (!response.ok) {
      // A form we cannot read is not fatal: the standard layout still applies.
      setStatus("양식 항목을 읽지 못해 표준 양식으로 진행합니다.");
      return;
    }
    const schema = await response.json();
    if (!schema.labels?.length) return;
    agentLabels = schema.labels;
    agentTemplate = { filename: template.name, content_base64: await toBase64(template) };
    showSchema(schema);
  };

  const startAgentFlow = async (announcement, pastedText, template) => {
    agentResult.hidden = true;
    await readTemplateSchema(template);
    setStatus("공고문에서 직무수행내역을 읽고 있습니다.");
    const body = new FormData();
    if (announcement) {
      body.append("announcement", announcement, announcement.name);
    } else {
      body.append("announcement", new File([pastedText], "pasted-announcement.txt", { type: "text/plain" }));
    }
    const response = await fetch("/api/extract", { method: "POST", body });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error?.message || "공고문을 읽지 못했습니다.");
    }
    if (showReviewGate(await response.json())) setStatus("읽은 내용을 확인한 뒤 시작해 주세요.");
  };

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!connected) {
      setError("NCS 백엔드 연결을 확인해 주세요.");
      return;
    }
    const announcement = announcementInput?.files?.[0];
    const pastedText = announcementText?.value?.trim() || "";
    const provider = selectedProvider();
    const template = templateInput?.files?.[0];
    if (!announcement && !pastedText) {
      setError("채용 공고문을 선택하거나 내용을 붙여넣어 주세요.");
      return;
    }
    if (provider === "agent") {
      setError("정밀 탐색에 사용할 AI를 고르고 로그인해 주세요.");
      return;
    }
    if (!crypto?.randomUUID) {
      setError("문서 ID를 만들 수 없습니다. 로컬 주소로 다시 접속해 주세요.");
      return;
    }

    if (usesAgent()) {
      submitButton.disabled = true;
      result.hidden = true;
      if (progress) progress.hidden = true;
      try {
        await startAgentFlow(announcement, pastedText, template);
      } catch (error) {
        setError(error.message || "공고문을 읽지 못했습니다.");
      } finally {
        submitButton.disabled = false;
      }
      return;
    }

    const body = new FormData();
    if (announcement) body.append("announcement", announcement, announcement.name);
    else body.append("announcement_text", pastedText);
    const jobTitle = jobTitleInput?.value?.trim();
    if (jobTitle) body.append("job_title", jobTitle);
    if (template) body.append("template", template, template.name);
    body.append("document_id", crypto.randomUUID());
    body.append("created_at", localTimestamp());
    body.append("provider", provider);

    submitButton.disabled = true;
    result.hidden = true;
    setStatus("공고문에서 직무수행내역을 읽고 있습니다.");
    setProgress(0);
    const timers = [
      setTimeout(() => { setProgress(1); setStatus("직무수행내역과 NCS 세분류·능력단위를 연결하고 있습니다."); }, 2500),
      setTimeout(() => {
        setProgress(2);
        setStatus("NCS 근거만으로 결정적 초안을 조립하고 있습니다.");
      }, 7000),
      setTimeout(() => {
        setProgress(3);
        setStatus("직무기술서 양식에 맞춰 HWPX를 만들고 있습니다.");
      }, 14000),
    ];

    try {
      const response = await fetch("/api/generate-job-description", { method: "POST", body });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const code = payload.error?.code ? ` (${payload.error.code})` : "";
        throw new Error(`${payload.error?.message || "직무기술서를 만들지 못했습니다."}${code}`);
      }
      const blob = await response.blob();
      const filename = filenameFromHeader(response.headers.get("Content-Disposition"));
      lastDownload = { blob, filename };
      download(blob, filename);
      setProgress(4);
      setStatus("NCS 직무기술서 생성이 완료되었습니다.");
      document.querySelector("[data-result-filename]").textContent = filename;
      document.querySelector("[data-result-units]").textContent = response.headers.get("X-NCS-JD-Selected-Units") || "0";
      document.querySelector("[data-result-subcategories]").textContent = response.headers.get("X-NCS-JD-Selected-Subcategories") || "0";
      // Report what actually ran, not what was requested: scope selection falls
      // back silently and form mapping only runs when a form was uploaded.
      const scopeMode = response.headers.get("X-NCS-JD-Scope-Selection") || "deterministic";
      const mapping = response.headers.get("X-NCS-JD-Template-Mapping") || "off";
      const engineNames = { claude: "Claude Code", codex: "Codex" };
      const used = [];
      if (engineNames[scopeMode]) used.push(`${engineNames[scopeMode]} 능력단위 선정`);
      if (engineNames[mapping]) used.push(`${engineNames[mapping]} 양식 필드 대응`);
      document.querySelector("[data-result-provider]").textContent =
        used.length ? used.join(" · ") : "외부 AI 미사용";
      const templateUsed = response.headers.get("X-HWPX-Template-Used") === "true";
      const templateMode = response.headers.get("X-HWPX-Template-Mode") || "standard_generate";
      document.querySelector("[data-result-template]").textContent = templateUsed
        ? `업로드한 예시 양식 반영 완료 (${templateMode})`
        : "표준 양식으로 생성됨";
      result.hidden = false;
    } catch (error) {
      progress.hidden = true;
      setError(error.message || "직무기술서를 만들지 못했습니다.");
    } finally {
      timers.forEach(clearTimeout);
      submitButton.disabled = false;
    }
  });

  document.querySelector("[data-download-again]")?.addEventListener("click", () => {
    if (lastDownload) download(lastDownload.blob, lastDownload.filename);
  });

})();
