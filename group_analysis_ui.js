/* Экспериментальный интерфейс анализа связей группы для server_http.py. */
(function () {
  "use strict";

  const core = window.FaceSearchGroupCore;
  const button = document.getElementById("group-analysis-button");
  const panel = document.getElementById("group-analysis-panel");
  const hideButton = document.getElementById("group-analysis-hide");
  const progress = document.getElementById("group-progress");
  const progressBar = document.getElementById("group-progress-bar");
  const progressMessage = document.getElementById("group-progress-message");
  const summary = document.getElementById("group-analysis-summary");
  const graphWrap = document.getElementById("group-graph-wrap");
  const graph = document.getElementById("group-graph");
  const profileList = document.getElementById("group-profile-list");
  const combinationsBox = document.getElementById("group-combinations");
  const notice = document.getElementById("group-analysis-notice");

  const minSimilarityInput = document.getElementById("group-min-similarity");
  const candidatesInput = document.getElementById("group-candidates");
  const friendWeightInput = document.getElementById("group-friend-weight");
  const commonWeightInput = document.getElementById("group-common-weight");
  const cityWeightInput = document.getElementById("group-city-weight");
  const checkVkFriendsInput = document.getElementById("group-check-vk-friends");

  if (!core || !button || !panel) return;

  let running = false;
  let activeJobId = "";
  let pollSequence = 0;
  let lastResult = null;

  function sync() {
    const queryId = core.getQueryId();
    const faceCount = core.getFaceCount();
    button.disabled = Boolean(core.isBusy()) || !queryId || faceCount < 2 || running;
    if (!running) button.textContent = "Анализ связей группы";
  }

  function clearPanel() {
    activeJobId = "";
    running = false;
    pollSequence += 1;
    lastResult = null;
    panel.hidden = true;
    summary.replaceChildren();
    graph.replaceChildren();
    graphWrap.hidden = true;
    profileList.replaceChildren();
    combinationsBox.replaceChildren();
    notice.textContent = "";
    progressBar.style.width = "0%";
    progress.setAttribute("aria-valuenow", "0");
    progressMessage.textContent = "Ожидание запуска анализа.";
    sync();
  }

  function setProgress(value, message) {
    const percent = Math.max(0, Math.min(100, Number(value) || 0));
    progressBar.style.width = percent + "%";
    progress.setAttribute("aria-valuenow", String(Math.round(percent)));
    progressMessage.textContent = String(message || "");
  }

  function numberFrom(input, label, min, max) {
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < min || value > max) {
      input.focus();
      throw new Error(label + " должно быть в диапазоне " + min + "–" + max + ".");
    }
    return value;
  }

  function readOptions() {
    return {
      min_similarity: numberFrom(minSimilarityInput, "Минимальное сходство", 0, 100),
      candidates_per_face: Math.trunc(numberFrom(candidatesInput, "Количество кандидатов", 1, 10)),
      friend_weight: numberFrom(friendWeightInput, "Вес дружбы", 0, 20),
      common_friend_weight: numberFrom(commonWeightInput, "Вес общих друзей", 0, 10),
      city_weight: numberFrom(cityWeightInput, "Вес города", 0, 10),
      check_vk_friends: Boolean(checkVkFriendsInput.checked)
    };
  }

  function svgNode(name, attrs, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, String(attrs[key]));
    });
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function renderGraph(combination) {
    const profiles = Array.isArray(combination && combination.profiles) ? combination.profiles : [];
    const relations = Array.isArray(combination && combination.relations) ? combination.relations : [];
    graph.replaceChildren();
    graphWrap.hidden = !profiles.length;
    if (!profiles.length) return;

    graph.setAttribute("viewBox", "0 0 600 360");
    graph.appendChild(svgNode("rect", { x: 0, y: 0, width: 600, height: 360, fill: "transparent" }));
    const positions = [];
    const radiusX = profiles.length <= 2 ? 150 : 215;
    const radiusY = profiles.length <= 2 ? 65 : 112;
    profiles.forEach(function (profile, index) {
      const angle = -Math.PI / 2 + index * 2 * Math.PI / profiles.length;
      positions.push({
        x: 300 + Math.cos(angle) * radiusX,
        y: 175 + Math.sin(angle) * radiusY,
        profile: profile
      });
    });

    relations.forEach(function (edge) {
      const fromKey = String(edge.from || "");
      const toKey = String(edge.to || "");
      const a = positions.find(function (p) { return String(p.profile.profile_key || "") === fromKey; });
      const b = positions.find(function (p) { return String(p.profile.profile_key || "") === toKey; });
      if (!a || !b) return;
      const types = Array.isArray(edge.types) ? edge.types : [];
      const friend = Boolean(edge.friend) || types.includes("friend");
      const common = Number(edge.common_friends || 0) > 0 || types.includes("common_friends");
      const sameCity = Boolean(edge.same_city) || types.includes("same_city");
      const color = friend ? "#16a34a" : (common ? "#8b5cf6" : "#d97706");
      const dash = friend ? "" : (common ? "6 5" : "2 5");
      const line = svgNode("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        stroke: color, "stroke-width": friend ? 3 : 2,
        "stroke-dasharray": dash, opacity: 0.9
      });
      const details = [];
      if (friend) details.push("Есть связь в друзьях VK");
      if (common) details.push("Общие друзья: " + Number(edge.common_friends || 0));
      if (sameCity) details.push("Совпадает город");
      line.appendChild(svgNode("title", {}, details.join(" · ")));
      graph.appendChild(line);
    });

    positions.forEach(function (position, index) {
      const profile = position.profile;
      const unmatched = Boolean(profile.unmatched) || !Number(profile.face_id || 0);
      const circle = svgNode("circle", {
        cx: position.x, cy: position.y, r: 24,
        fill: unmatched ? "#6b7280" : "#dbeafe",
        stroke: unmatched ? "#9ca3af" : "#2563eb",
        "stroke-width": 2
      });
      circle.appendChild(svgNode("title", {},
        String(profile.name || "Без имени") + " · сходство " + Number(profile.similarity || 0).toFixed(2) + "%"
      ));
      graph.appendChild(circle);
      graph.appendChild(svgNode("text", {
        x: position.x, y: position.y + 5, "text-anchor": "middle",
        "font-size": 14, "font-weight": 700, fill: unmatched ? "#ffffff" : "#1e3a8a"
      }, "Л" + (Number(profile.face_index || index) + 1)));
      let label = String(profile.name || "Без имени");
      if (label.length > 25) label = label.slice(0, 23) + "…";
      graph.appendChild(svgNode("text", {
        x: position.x, y: position.y + 42, "text-anchor": "middle",
        "font-size": 11, fill: "var(--text)"
      }, label));
      graph.appendChild(svgNode("text", {
        x: position.x, y: position.y + 56, "text-anchor": "middle",
        "font-size": 10, fill: "var(--muted)"
      }, unmatched ? "нет кандидата" : Number(profile.similarity || 0).toFixed(1) + "%"));
    });
  }

  function renderProfileCards(combination) {
    profileList.replaceChildren();
    const profiles = Array.isArray(combination && combination.profiles) ? combination.profiles : [];
    profiles.forEach(function (profile, index) {
      const card = document.createElement("article");
      card.className = "group-profile-card";
      const faceId = Number(profile.face_id || 0);
      if (faceId > 0) {
        const image = document.createElement("img");
        image.loading = "lazy";
        image.alt = "Фото кандидата: " + String(profile.name || "");
        image.src = "/api/face/" + encodeURIComponent(faceId) + "/image?crop=1";
        image.onerror = function () { image.style.display = "none"; };
        card.appendChild(image);
      }
      const name = document.createElement("div");
      name.className = "group-profile-name";
      name.textContent = "Лицо " + (Number(profile.face_index || index) + 1) + ": " + String(profile.name || "Без имени");
      card.appendChild(name);

      const meta = document.createElement("div");
      meta.className = "group-profile-meta";
      meta.textContent = profile.unmatched
        ? "Нет кандидата выше порога"
        : "Сходство лица: " + Number(profile.similarity || 0).toFixed(2) + "%" + (profile.city ? " · " + profile.city : "");
      card.appendChild(meta);

      const value = String(profile.profile_url || profile.source_url || "").trim();
      if (value) {
        try {
          const url = new URL(value, window.location.href);
          if (url.protocol === "http:" || url.protocol === "https:") {
            const link = document.createElement("a");
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = "Открыть профиль";
            card.appendChild(link);
          }
        } catch (error) {
          // Не показываем непроверенные ссылки.
        }
      }
      profileList.appendChild(card);
    });
  }

  function renderCombinationButtons(combinations, selectedIndex) {
    combinationsBox.replaceChildren();
    combinations.forEach(function (combo, index) {
      const buttonEl = document.createElement("button");
      buttonEl.type = "button";
      buttonEl.className = "group-combination";
      buttonEl.setAttribute("aria-pressed", String(index === selectedIndex));

      const info = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = "Вариант №" + (index + 1) + " · оценка " + Number(combo.score || 0).toFixed(2);
      const subtitle = document.createElement("small");
      subtitle.textContent =
        "Сходство в среднем: " + Number(combo.visual_average || 0).toFixed(2) +
        "% · социальный вклад: +" + Number(combo.social_bonus || 0).toFixed(2) +
        " · сопоставлено лиц: " + Number(combo.matched_faces || 0) + "/" + (combo.profiles || []).length;
      info.append(title, subtitle);
      buttonEl.appendChild(info);

      const action = document.createElement("span");
      action.textContent = index === selectedIndex ? "Выбран" : "Показать";
      buttonEl.appendChild(action);
      buttonEl.addEventListener("click", function () {
        renderCombination(combinations, index);
      });
      combinationsBox.appendChild(buttonEl);
    });
  }

  function renderCombination(combinations, index) {
    const combo = combinations[index];
    if (!combo) return;
    renderGraph(combo);
    renderProfileCards(combo);
    renderCombinationButtons(combinations, index);
  }

  function renderResult(result) {
    lastResult = result;
    const resultCombinations = Array.isArray(result.combinations) ? result.combinations : [];
    const overview = document.createElement("div");
    overview.className = "group-empty";
    overview.textContent =
      "Обнаружено лиц: " + Number(result.face_count || 0) +
      ". Кандидатов на лицо: " + (result.candidate_counts || []).join(", ") +
      ". Показано комбинаций: " + resultCombinations.length + ".";
    summary.replaceChildren(overview);
    if (resultCombinations.length) {
      renderCombination(resultCombinations, 0);
    } else {
      graphWrap.hidden = true;
      profileList.replaceChildren();
      combinationsBox.replaceChildren();
      const noMatches = document.createElement("div");
      noMatches.className = "group-empty";
      noMatches.textContent = "Не удалось собрать допустимую комбинацию профилей выше заданного порога.";
      summary.appendChild(noMatches);
    }
    const friendStatus = result.friend_status || {};
    notice.textContent = [friendStatus.message || "", result.notice || ""].filter(Boolean).join(" ");
    setProgress(100, "Анализ завершён.");
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function finish() {
    running = false;
    button.textContent = "Анализ связей группы";
    sync();
  }

  async function poll(jobId, sequence, queryId) {
    if (sequence !== pollSequence) return;
    try {
      const url = "/api/group-analysis/status?job_id=" + encodeURIComponent(jobId) +
        "&query_id=" + encodeURIComponent(queryId);
      const response = await fetch(url, { cache: "no-store" });
      let data = {};
      try { data = await response.json(); } catch (error) { /* обработка ниже */ }
      if (!response.ok) throw new Error(data.error || ("Ошибка HTTP " + response.status));

      setProgress(data.progress || 0, data.message || "Анализ выполняется…");
      if (data.state === "done") {
        renderResult(data.result || {});
        finish();
        return;
      }
      if (data.state === "error") throw new Error(data.error || "Групповой анализ завершился с ошибкой.");
      window.setTimeout(function () { poll(jobId, sequence, queryId); }, 700);
    } catch (error) {
      if (sequence !== pollSequence) return;
      setProgress(100, "Анализ не завершён.");
      const message = document.createElement("div");
      message.className = "group-empty";
      message.textContent = error.message || String(error);
      summary.replaceChildren(message);
      finish();
    }
  }

  async function start() {
    if (running || !core.getQueryId() || core.getFaceCount() < 2 || core.isBusy()) return;
    let options;
    try {
      options = readOptions();
    } catch (error) {
      core.setStatus(error.message || String(error), true);
      return;
    }

    const queryId = core.getQueryId();
    running = true;
    activeJobId = "";
    panel.hidden = false;
    summary.replaceChildren();
    graph.replaceChildren();
    graphWrap.hidden = true;
    profileList.replaceChildren();
    combinationsBox.replaceChildren();
    notice.textContent = "";
    setProgress(0, "Запуск группового анализа…");
    sync();
    button.textContent = "Анализ запускается…";
    const sequence = ++pollSequence;

    try {
      const response = await core.postJson("/api/group-analysis/start", {
        query_id: queryId,
        options: options
      });
      activeJobId = String(response.job_id || "");
      if (!activeJobId) throw new Error("Сервер не вернул идентификатор задачи.");
      setProgress(1, "Задача запущена. Поиск кандидатов и проверка связей могут занять некоторое время.");
      window.setTimeout(function () { poll(activeJobId, sequence, queryId); }, 250);
    } catch (error) {
      setProgress(100, "Не удалось запустить анализ.");
      const message = document.createElement("div");
      message.className = "group-empty";
      message.textContent = error.message || String(error);
      summary.replaceChildren(message);
      finish();
    }
  }

  button.addEventListener("click", start);
  hideButton.addEventListener("click", function () { panel.hidden = true; });
  window.FaceSearchGroupUI = {
    sync: sync,
    reset: clearPanel
  };
  sync();
})();
