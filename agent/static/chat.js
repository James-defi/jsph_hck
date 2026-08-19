(() => {
  const panel = document.getElementById("chat-panel");
  const handle = document.getElementById("chat-resizer");
  const form = document.querySelector(".composer");
  const query = document.querySelector("#query");
  const log = document.getElementById("chat-log");
  const conversation = document.getElementById("conversation-id");
  const mic = document.querySelector(".mic-button");
  const micStatus = document.querySelector("#mic-status");

  const storedWidth = Number(localStorage.getItem("jarvel-chat-width"));
  if (panel && storedWidth >= 300) panel.style.width = `${storedWidth}px`;

  if (handle && panel) {
    let dragging = false;
    handle.addEventListener("mousedown", (event) => {
      event.preventDefault();
      dragging = true;
      handle.classList.add("is-dragging");
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("is-dragging");
      localStorage.setItem("jarvel-chat-width", String(panel.getBoundingClientRect().width | 0));
    });
    window.addEventListener("mousemove", (event) => {
      if (!dragging) return;
      const width = Math.min(Math.max(event.clientX, 300), Math.min(window.innerWidth * 0.72, 820));
      panel.style.width = `${width}px`;
    });
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function hideOnboarding() {
    document.querySelector(".onboarding")?.classList.add("is-hidden");
  }

  function pushTimeline(timeline) {
    window.__jarvelPendingTimeline = timeline || [];
    window.JarvelGlobe?.setTimeline(window.__jarvelPendingTimeline);
  }

  function modeIcon(mode) {
    const text = String(mode || "").toLowerCase();
    if (/(поезд|rail|train)/.test(text)) return "🚆";
    if (/(автобус|bus)/.test(text)) return "🚌";
    return "✈️";
  }

  function formatLegLine(leg) {
    if (leg.line) return `${modeIcon(leg.mode)} ${leg.line}`;
    const origin = leg.origin?.code || "";
    const destination = leg.destination?.code || "";
    const time = String(leg.time || "").replaceAll("—", "–").replaceAll("-", "–");
    const [departure, arrival] = time.includes("–") ? time.split("–").map((part) => part.trim()) : [time, ""];
    if (origin && destination && departure) {
      return `${modeIcon(leg.mode)} ${origin} ${departure} → ${destination} ${arrival}`.trim();
    }
    const route = leg.route || "";
    return `${modeIcon(leg.mode)} ${route}${time ? ` · ${time}` : ""}`.trim();
  }

  function formatWaitLine(wait, transfer) {
    const text = String(wait || "").trim();
    if (!text) return "";
    if (/прям|стыковки нет/i.test(text) || /^пересадк/i.test(text)) return text;
    return transfer ? `Пересадка: ${text}` : text;
  }

  function metricValue(scenario, labels) {
    const metrics = scenario.metrics || [];
    const wanted = Array.isArray(labels) ? labels : [labels];
    for (const label of wanted) {
      const found = metrics.find((item) => item.label === label);
      if (found && found.value) return found.value;
    }
    return metrics[0]?.value || "";
  }

  function formatChatHtml(value) {
    return escapeHtml(value)
      .replaceAll(/\*\*(.+?)\*\*/g, "$1")
      .replaceAll(/^#{1,6}\s+/gm, "")
      .replaceAll(/^\s*[-*•]\s+/gm, "")
      .replaceAll("\n", "<br>");
  }

    function renderResult(result) {
    const parts = [`<p class="chat-copy">${formatChatHtml(result.summary || "")}</p>`];
    const timeline = result.timeline || [];
    if (timeline.length) {
      const rows = timeline.map((traveler) => {
        const legs = traveler.legs || [];
        const lines = [`<div class="person-line">${escapeHtml(traveler.person || "Участник")}</div>`];
        legs.forEach((leg, index) => {
          if (index === legs.length - 1 && legs.length > 1 && traveler.wait) {
            lines.push(`<div class="wait-line">${escapeHtml(formatWaitLine(traveler.wait, true))}</div>`);
          }
          lines.push(`<div class="leg-line">${escapeHtml(formatLegLine(leg))}</div>`);
        });
        if (legs.length <= 1 && traveler.wait) {
          lines.push(`<div class="wait-line">${escapeHtml(formatWaitLine(traveler.wait, false))}</div>`);
        }
        return `<li>${lines.join("")}</li>`;
      }).join("");
      parts.push(`<ul class="chat-list">${rows}</ul>`);
    }

    const scenarios = result.scenarios || [];
    const chosen = scenarios.find((scenario) =>
      (scenario.booking_units || []).some((unit) => (unit.tariffs || []).length)
    ) || scenarios[0];
    let hasOfferLink = false;
    if (chosen) {
      const links = [];
      for (const unit of chosen.booking_units || []) {
        for (const tariff of unit.tariffs || []) {
          if (!tariff.variant_id) continue;
          hasOfferLink = true;
          const price = tariff.price || metricValue(chosen, ["Итого", "Цена"]);
          const label = [unit.title || "Открыть на Туту", price ? `— ${price}` : "", "↗"]
            .filter(Boolean)
            .join(" ");
          links.push(
            `<div><a class="offer-link" href="#" data-run-id="${escapeHtml(result.run_id)}" data-component-ref="${escapeHtml(unit.component_ref)}" data-variant-id="${escapeHtml(tariff.variant_id)}">${escapeHtml(label)}</a></div>`
          );
        }
      }
      if (links.length) {
        parts.push(links.join(""));
        parts.push('<p class="chat-footnote">Ссылка открывает Туту. Это не бронь: перед оплатой проверьте рейс, цену и пассажиров.</p>');
      }
    }
    if (!hasOfferLink && result.rejection_summary) {
      parts.push(`<p class="chat-footnote">${formatChatHtml(result.rejection_summary)}</p>`);
    }
    parts.push('<p class="offer-status" aria-live="polite"></p>');
    return parts.join("");
  }

  function bindOfferLinks(root) {
    const status = root.querySelector(".offer-status");
    root.querySelectorAll(".offer-link").forEach((link) => {
      link.addEventListener("click", async (event) => {
        event.preventDefault();
        if (link.dataset.busy === "1") return;
        link.dataset.busy = "1";
        if (status) status.textContent = "";
        try {
          const response = await fetch("/api/checkout", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              run_id: link.dataset.runId,
              component_ref: link.dataset.componentRef,
              variant_id: link.dataset.variantId,
            }),
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(payload.detail || "Не удалось открыть ссылку Туту.");
          const url = String(payload.url || "");
          if (!url) throw new Error("Туту не вернул ссылку.");
          window.open(url, "_blank", "noopener,noreferrer");
        } catch (error) {
          if (status) {
            status.textContent = error instanceof Error ? error.message : "Не удалось открыть ссылку Туту.";
          }
        } finally {
          delete link.dataset.busy;
        }
      });
    });
  }

  function appendUser(text) {
    const bubble = document.createElement("div");
    bubble.className = "bubble bubble-user";
    bubble.textContent = text;
    log.append(bubble);
  }

  function appendAssistant(html) {
    const bubble = document.createElement("article");
    bubble.className = "bubble bubble-assistant";
    bubble.innerHTML = html;
    log.append(bubble);
    bindOfferLinks(bubble);
    bubble.scrollIntoView({block: "start", behavior: "smooth"});
  }

  document.querySelectorAll(".example-query").forEach((button) => {
    button.addEventListener("click", () => {
      if (!query) return;
      query.value = button.dataset.query || "";
      query.focus();
      form?.requestSubmit();
    });
  });

  bindOfferLinks(document);

  let recorder = null;
  let chunks = [];
  const setMicStatus = (text, listening = false) => {
    if (!micStatus) return;
    micStatus.textContent = text || "";
    micStatus.classList.toggle("is-listening", Boolean(listening));
    form?.classList.toggle("is-listening", Boolean(listening));
  };
  const blobToBase64 = (blob) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const comma = dataUrl.indexOf(",");
      resolve(comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl);
    };
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(blob);
  });
  const audioFormat = (mime) => {
    const raw = String(mime || "audio/webm").toLowerCase();
    const subtype = raw.split(";")[0].split("/")[1] || "webm";
    return subtype;
  };
  const stopMic = () => {
    if (recorder && recorder.state !== "inactive") recorder.stop();
  };
  if (mic) {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      mic.disabled = true;
      mic.title = "Диктовка доступна по HTTPS или на localhost";
    } else {
      mic.addEventListener("click", async () => {
        if (recorder) {
          stopMic();
          return;
        }
        setMicStatus("Включаем микрофон…", true);
        try {
          const stream = await navigator.mediaDevices.getUserMedia({audio: true});
          chunks = [];
          recorder = new MediaRecorder(stream);
          recorder.addEventListener("dataavailable", (event) => {
            if (event.data && event.data.size) chunks.push(event.data);
          });
          recorder.addEventListener("stop", async () => {
            stream.getTracks().forEach((track) => track.stop());
            mic.classList.remove("is-recording");
            mic.setAttribute("aria-pressed", "false");
            mic.textContent = "Диктовка";
            mic.title = "Надиктовать запрос";
            setMicStatus("");
            const blob = new Blob(chunks, {type: recorder?.mimeType || "audio/webm"});
            recorder = null;
            if (!blob.size) {
              setMicStatus("Запись пустая. Попробуйте ещё раз.");
              return;
            }
            mic.disabled = true;
            setMicStatus("Распознаём речь…");
            try {
              const response = await fetch("/api/transcribe", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                  audio_base64: await blobToBase64(blob),
                  format: audioFormat(blob.type),
                }),
              });
              const payload = await response.json().catch(() => ({}));
              if (!response.ok) {
                throw new Error(typeof payload.detail === "string" ? payload.detail : "Не удалось распознать речь.");
              }
              const spoken = String(payload.text || "").trim();
              if (!spoken) throw new Error("Не удалось разобрать речь. Попробуйте ещё раз.");
              query.value = query.value.trim() ? `${query.value.trim()} ${spoken}` : spoken;
              query.focus();
              setMicStatus("Текст добавлен в поле. Проверьте и нажмите «Найти билеты».");
            } catch (error) {
              setMicStatus(error instanceof Error ? error.message : "Не удалось распознать речь.");
            } finally {
              mic.disabled = false;
            }
          });
          recorder.start();
          mic.classList.add("is-recording");
          mic.setAttribute("aria-pressed", "true");
          mic.textContent = "Слушаю";
          mic.title = "Остановить диктовку";
          setMicStatus("Агент слушает. Можно говорить. Нажмите ещё раз, чтобы остановить.", true);
        } catch {
          recorder = null;
          setMicStatus("Нет доступа к микрофону.");
        }
      });
    }
  }

  form?.addEventListener("submit", async (event) => {
    if (!query) return;
    const text = query.value.trim();
    if (!text) return;
    event.preventDefault();
    hideOnboarding();
    appendUser(text);
    query.value = "";
    const pending = document.createElement("article");
    pending.className = "bubble bubble-assistant";
    pending.innerHTML = "<p class='muted'>Ищу билеты…</p>";
    log.append(pending);
    log.scrollTop = log.scrollHeight;
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          query: text,
          conversation_id: conversation?.value || "",
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Не удалось получить ответ агента.");
      if (conversation && payload.conversation_id) conversation.value = payload.conversation_id;
      pending.remove();
      appendAssistant(renderResult(payload));
      pushTimeline(payload.timeline || []);
    } catch (error) {
      pending.innerHTML = `<section class="notice notice-error" role="alert"><strong>Не получилось выполнить запрос.</strong> ${escapeHtml(error instanceof Error ? error.message : "Попробуйте ещё раз.")}</section>`;
    }
  });
})();
