(() => {
  "use strict";

  const panel = document.querySelector("[data-cli-login]");
  if (!panel) return;

  const feedback = panel.querySelector("[data-login-feedback]");
  const refreshButton = panel.querySelector("[data-provider-refresh]");
  const engineInputs = Array.from(panel.querySelectorAll('input[name="engine"]'));
  const providerNames = { claude: "Claude Code", codex: "Codex" };
  let pollTimer = null;
  let refreshInFlight = null;
  // A person's pick wins even if the other provider reports ready later.  Only
  // an untouched picker is allowed to settle on a default by itself.
  let pickedByHand = false;
  let everChecked = false;

  const setFeedback = (message, isError = false) => {
    if (!feedback) return;
    feedback.textContent = message;
    feedback.classList.toggle("is-error", isError);
  };

  const statusPresentation = (status) => {
    if (status.logged_in) {
      return {
        state: "ready",
        label: "로그인됨",
        detail: "이 AI로 정밀 탐색을 실행할 수 있습니다.",
        button: "다시 로그인",
        disabled: false,
        selectable: true,
      };
    }
    if (!status.installed) {
      return {
        state: "error",
        label: "설치 필요",
        detail: "공식 CLI를 설치한 뒤 상태를 새로고침하세요.",
        button: "CLI 없음",
        disabled: true,
        selectable: false,
      };
    }
    if (status.login_in_progress) {
      return {
        state: "progress",
        label: "로그인 진행 중",
        detail: "새로 열린 CLI 창에서 로그인을 완료하세요.",
        button: "진행 중",
        disabled: true,
        selectable: false,
      };
    }

    const failures = {
      unsupported_auth_method: ["구독 로그인 필요", "API 키가 아닌 공식 구독 계정으로 다시 로그인하세요."],
      login_timed_out: ["시간 초과", "로그인 시간이 초과되었습니다. 다시 시도할 수 있습니다."],
      login_failed: ["로그인 실패", "로그인을 완료하지 못했습니다. 다시 시도해 주세요."],
      status_timeout: ["확인 지연", "CLI 상태 확인 시간이 초과되었습니다."],
      status_unavailable: ["상태 확인 실패", "CLI 상태를 확인하지 못했습니다."],
    };
    const [label, detail] = failures[status.code] || ["로그인 필요", "공식 CLI 구독 계정으로 로그인해 주세요."];
    return { state: "error", label, detail, button: "로그인", disabled: false, selectable: false };
  };

  const renderStatus = (status) => {
    const card = panel.querySelector(`[data-provider-card="${status.provider}"]`);
    if (!card) return;
    const view = statusPresentation(status);
    const statusNode = card.querySelector("[data-provider-status]");
    const detailNode = card.querySelector("[data-provider-detail]");
    const loginButton = card.querySelector("[data-login-provider]");
    const engineInput = card.querySelector('input[name="engine"]');
    card.classList.toggle("is-ready", view.state === "ready");
    card.classList.toggle("is-progress", view.state === "progress");
    if (statusNode) {
      statusNode.textContent = view.label;
      statusNode.dataset.state = view.state;
    }
    if (detailNode) detailNode.textContent = view.detail;
    if (loginButton) {
      loginButton.textContent = view.button;
      loginButton.disabled = view.disabled;
    }
    if (engineInput) {
      engineInput.disabled = !view.selectable;
      // A provider that just logged out must not stay silently selected and
      // then fail at submit time.
      if (!view.selectable && engineInput.checked) {
        engineInput.checked = false;
        engineInput.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  };

  const applyDefaultSelection = () => {
    if (pickedByHand) return;
    if (engineInputs.some((input) => input.checked && !input.disabled)) return;
    const ready = engineInputs.filter((input) => !input.disabled);
    // Only auto-select when there is no choice to make.  With two providers
    // available, picking one for the person is exactly the behaviour that hid
    // the decision in the first place.
    if (ready.length !== 1) return;
    ready[0].checked = true;
    ready[0].dispatchEvent(new Event("change", { bubbles: true }));
  };

  const schedulePoll = (statuses) => {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    if (statuses.some((status) => status.login_in_progress)) {
      pollTimer = setTimeout(() => void refreshStatuses(true), 2000);
    }
  };

  const refreshStatuses = (quiet = false) => {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async () => {
      if (refreshButton) refreshButton.disabled = true;
      if (!quiet) setFeedback("로그인 상태를 확인하고 있습니다.");
      try {
        const response = await fetch("/api/llm/providers", { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("로그인 상태를 확인하지 못했습니다.");
        const payload = await response.json();
        const statuses = Array.isArray(payload.providers)
          ? payload.providers.filter((status) => status && ["claude", "codex"].includes(status.provider))
          : [];
        statuses.forEach(renderStatus);
        everChecked = true;
        applyDefaultSelection();
        schedulePoll(statuses);
        if (!quiet) setFeedback("로그인 상태를 확인했습니다.");
      } catch (error) {
        setFeedback(error.message || "로그인 상태를 확인하지 못했습니다.", true);
      } finally {
        if (refreshButton) refreshButton.disabled = false;
        refreshInFlight = null;
      }
    })();
    return refreshInFlight;
  };

  engineInputs.forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) pickedByHand = true;
    });
  });

  panel.querySelectorAll("[data-login-provider]").forEach((button) => {
    button.addEventListener("click", async () => {
      const provider = button.dataset.loginProvider;
      if (!provider || !providerNames[provider]) return;
      button.disabled = true;
      setFeedback(`${providerNames[provider]} 로그인 창을 여는 중입니다.`);
      try {
        const response = await fetch(`/api/llm/providers/${provider}/login`, {
          method: "POST",
          headers: { "X-NCS-JD-Local-Action": "login", Accept: "application/json" },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.error?.message || `${providerNames[provider]} 로그인을 시작하지 못했습니다.`);
        }
        setFeedback(`${providerNames[provider]} 로그인 창을 열었습니다. 창에서 로그인을 완료하세요.`);
        // Whoever just logged in is the provider they mean to use.
        pickedByHand = false;
        await refreshStatuses(true);
      } catch (error) {
        setFeedback(error.message || `${providerNames[provider]} 로그인을 시작하지 못했습니다.`, true);
        await refreshStatuses(true);
      }
    });
  });

  refreshButton?.addEventListener("click", () => void refreshStatuses());
  window.addEventListener("focus", () => {
    if (everChecked && !panel.classList.contains("is-idle")) void refreshStatuses(true);
  });

  // Nothing is checked until the AI path is actually chosen.  Probing both CLIs
  // on page load is what made every provider read "연결됨" before the person
  // had picked anything.
  panel.addEventListener("ncs-jd:engine-picker-shown", () => {
    if (!everChecked) void refreshStatuses();
  });
})();
