// Session state and HTTP access.
let credential = sessionStorage.getItem("accmind_token") || "",
  user = null,
  refreshTimer = null,
  selectedDocumentId = null,
  previewUrl = null;
let packageOffset = 0,
  legacyOffset = 0;
const packageLimit = 20,
  legacyLimit = 30;
const byId = (id) => document.getElementById(id);
function notice(message = "") {
  byId("notice").textContent = message;
}
async function api(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
    Authorization: `Bearer ${credential}`,
  };
  const r = await fetch("/api/v1" + path, { ...options, headers });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw Error(
      typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail),
    );
  }
  return r;
}
function makeNode(tag, text, cls) {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
const labels = {
  accepted: "Ожидает обработки",
  queued: "Ожидает обработки",
  processing: "Обрабатывается",
  extracting: "Распознаётся",
  validating: "Проверяются реквизиты",
  indexing: "Подготовка к поиску",
  ready: "Готов",
  failed: "Ошибка обработки",
};

// Document and package lists.
function renderDocument(documentRecord) {
  const el = makeNode("div", null, "document");
  el.append(
    makeNode("strong", documentRecord.filename),
    makeNode(
      "p",
      labels[documentRecord.status] || documentRecord.status,
      "status",
    ),
  );
  const b = makeNode("button", "Реквизиты и файл");
  b.onclick = () => showDocument(documentRecord);
  el.append(b);
  if (documentRecord.status === "failed" && user.can_upload) {
    const retry = makeNode("button", "Повторить обработку");
    retry.onclick = async () => {
      try {
        await api(`/documents/${documentRecord.id}/retry`, { method: "POST" });
        await refresh();
      } catch (e) {
        notice(e.message);
      }
    };
    el.append(retry);
  }
  return el;
}
function renderPackage(packageRecord, openIds) {
  const details = makeNode("details", null, "upload-package");
  details.dataset.packageId = packageRecord.id;
  if (openIds.has(packageRecord.id)) details.open = true;
  const packageStatus = [
    `${packageRecord.total} документов`,
    `готово ${packageRecord.ready}`,
    `обрабатывается ${packageRecord.processing}`,
    `с ошибкой ${packageRecord.failed}`,
  ].join(" · ");
  const summary = makeNode("summary");
  summary.append(
    makeNode("strong", packageRecord.name),
    makeNode("span", packageStatus, "package-count"),
  );
  details.append(summary);
  const docs = makeNode("div", null, "package-documents");
  for (const documentRecord of packageRecord.documents || []) {
    docs.append(renderDocument(documentRecord));
  }
  details.append(docs);
  return details;
}
function queryPath(path, params) {
  const url = new URLSearchParams(params);
  return `${path}${path.includes("?") ? "&" : "?"}${url.toString()}`;
}
async function fetchPages(path, limit, count) {
  const result = [];
  for (let offset = 0; offset < count; offset += limit) {
    const page = await (await api(queryPath(path, { limit, offset }))).json();
    if (Array.isArray(page)) result.push(...page);
    else result.push(...page.items);
  }
  return result;
}
async function refresh() {
  const openIds = new Set(
    [...document.querySelectorAll(".upload-package[open]")].map(
      (x) => x.dataset.packageId,
    ),
  );
  const loadedPackages = Math.max(packageLimit, packageOffset),
    loadedLegacy = Math.max(legacyLimit, legacyOffset);
  const [packages, legacy, packageMeta, legacyNext] = await Promise.all([
    fetchPages("/packages", packageLimit, loadedPackages),
    fetchPages("/documents?unbatched=true", legacyLimit, loadedLegacy),
    (await api("/packages?limit=1&offset=0")).json(),
    (
      await api(`/documents?unbatched=true&limit=1&offset=${loadedLegacy}`)
    ).json(),
  ]);
  byId("packages").replaceChildren(
    ...packages.map((packageRecord) => renderPackage(packageRecord, openIds)),
  );
  byId("legacy-documents").replaceChildren(...legacy.map(renderDocument));
  packageOffset = Math.ceil(packages.length / packageLimit) * packageLimit;
  legacyOffset = Math.ceil(legacy.length / legacyLimit) * legacyLimit;
  byId("more-packages").hidden = packages.length >= packageMeta.total;
  byId("more-legacy").hidden = legacyNext.length === 0;
}
async function refreshFirstPage() {
  const openIds = new Set(
    [...document.querySelectorAll(".upload-package[open]")].map(
      (x) => x.dataset.packageId,
    ),
  );
  const [packages, legacy, packageMeta, legacyNext] = await Promise.all([
    (await api(`/packages?limit=${packageLimit}&offset=0`)).json(),
    (
      await api(`/documents?unbatched=true&limit=${legacyLimit}&offset=0`)
    ).json(),
    (await api("/packages?limit=1&offset=0")).json(),
    (
      await api(`/documents?unbatched=true&limit=1&offset=${legacyOffset}`)
    ).json(),
  ]);
  const oldPackages = [...byId("packages").children],
    oldLegacy = [...byId("legacy-documents").children];
  byId("packages").replaceChildren(
    ...packages.items.map((packageRecord) =>
      renderPackage(packageRecord, openIds),
    ),
    ...oldPackages.slice(packages.items.length),
  );
  byId("legacy-documents").replaceChildren(
    ...legacy.map(renderDocument),
    ...oldLegacy.slice(legacy.length),
  );
  byId("more-packages").hidden = packages.items.length >= packageMeta.total;
  byId("more-legacy").hidden = legacyNext.length === 0;
}
async function morePackages() {
  const page = await (
    await api(`/packages?limit=${packageLimit}&offset=${packageOffset}`)
  ).json();
  const openIds = new Set(
    [...document.querySelectorAll(".upload-package[open]")].map(
      (x) => x.dataset.packageId,
    ),
  );
  byId("packages").append(
    ...page.items.map((packageRecord) => renderPackage(packageRecord, openIds)),
  );
  packageOffset += page.items.length;
  byId("more-packages").hidden = packageOffset >= page.total;
}
async function moreLegacy() {
  const page = await (
    await api(
      `/documents?unbatched=true&limit=${legacyLimit}&offset=${legacyOffset}`,
    )
  ).json();
  byId("legacy-documents").append(...page.map(renderDocument));
  legacyOffset += page.length;
  byId("more-legacy").hidden = page.length < legacyLimit;
}
// Original files and cited fragments.
function showDocument(documentRecord) {
  selectedDocumentId = documentRecord.id;
  byId("source-title").textContent = documentRecord.filename;
  byId("source-text").textContent = JSON.stringify(
    documentRecord.fields,
    null,
    2,
  );
  resetPreview();
  byId("source").showModal();
}
function resetPreview() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  byId("preview").removeAttribute("src");
  byId("preview").hidden = true;
}
function showCitation(citation) {
  selectedDocumentId = citation.document_id;
  byId("source-title").textContent =
    `${citation.filename} · страница ${citation.page}`;
  byId("source-text").textContent = citation.text;
  resetPreview();
  byId("source").showModal();
}
// Questions, answers and their sources.
function renderMessage(question, result) {
  byId("messages").append(makeNode("div", question, "message question"));
  const answer = makeNode("div", null, "message");
  const readableAnswer = String(result.answer || "")
    .replace(/\s*Источники:\s*(?:\[[0-9a-fA-F-]{36}\]\s*)+/gi, "")
    .replace(/\s*\[[0-9a-fA-F-]{36}\]/g, "")
    .trim();
  answer.append(makeNode("div", readableAnswer));
  const sources = makeNode("div", null, "sources");
  for (const citation of result.citations || []) {
    const b = makeNode(
      "button",
      `${citation.filename} · стр. ${citation.page}`,
    );
    b.onclick = () => showCitation(citation);
    sources.append(b);
  }
  answer.append(sources);
  if (result.stages) {
    const details = makeNode("details");
    details.append(makeNode("summary", "Как найден ответ"));
    details.append(
      makeNode(
        "pre",
        JSON.stringify(
          {
            trace_id: result.trace_id,
            stages: result.stages,
            paths: result.paths,
            calculations: result.calculations,
          },
          null,
          2,
        ),
      ),
    );
    answer.append(details);
  }
  byId("messages").append(answer);
  answer.scrollIntoView({ block: "nearest" });
}
// Login and event handlers.
async function signIn() {
  try {
    user = await (await api("/me")).json();
    sessionStorage.setItem("accmind_token", credential);
    byId("identity").textContent =
      { accountant: "Бухгалтер", reader: "Читатель" }[user.id] || user.id;
    byId("login").hidden = true;
    byId("workspace").hidden = false;
    byId("logout").hidden = false;
    byId("upload-form").hidden = !user.can_upload;
    byId("token").value = "";
    notice(
      user.missing_configuration.length
        ? "Модель ещё не настроена: " + user.missing_configuration.join(", ")
        : "",
    );
    byId("messages").replaceChildren();
    await refresh();
    if (
      !byId("packages").children.length &&
      byId("legacy-documents").children.length
    )
      byId("legacy-package").open = true;
    for (const item of await (await api("/history")).json())
      renderMessage(item.question, item.response);
    clearInterval(refreshTimer);
    refreshTimer = setInterval(
      () => refreshFirstPage().catch((e) => notice(e.message)),
      5000,
    );
  } catch (e) {
    credential = "";
    sessionStorage.removeItem("accmind_token");
    user = null;
    byId("workspace").hidden = true;
    byId("login").hidden = false;
    byId("logout").hidden = true;
    notice(e.message);
  }
}
if (credential) signIn();
byId("login-form").onsubmit = async (e) => {
  e.preventDefault();
  credential = byId("token").value.trim();
  await signIn();
};
byId("logout").onclick = () => {
  credential = "";
  sessionStorage.removeItem("accmind_token");
  user = null;
  clearInterval(refreshTimer);
  byId("workspace").hidden = true;
  byId("login").hidden = false;
  byId("logout").hidden = true;
  byId("identity").textContent = "";
  byId("messages").replaceChildren();
  byId("packages").replaceChildren();
  byId("legacy-documents").replaceChildren();
  byId("source").close();
  resetPreview();
  notice();
};
byId("refresh").onclick = () => refresh().catch((e) => notice(e.message));

function uploadResultMessage(result) {
  if (!result.accepted.length) {
    return "Все выбранные документы уже были загружены.";
  }

  const parts = [
    `Добавлено ${result.accepted.length} документов в пакет «${result.name}».`,
  ];
  if (result.duplicates.length) {
    parts.push(
      ` Уже загруженных файлов пропущено: ${result.duplicates.length}.`,
    );
  }
  if (result.storage_failed) {
    parts.push(` Не удалось сохранить: ${result.storage_failed}.`);
  }
  return parts.join("");
}

byId("upload-form").onsubmit = async (e) => {
  e.preventDefault();
  const files = byId("file").files;
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  form.append("group", byId("group").value);
  form.append("name", byId("package-name").value);
  byId("upload-button").disabled = true;
  try {
    const result = await (
      await api("/packages", { method: "POST", body: form })
    ).json();
    notice(uploadResultMessage(result));
    byId("file").value = "";
    byId("package-name").value = "";
    await refresh();
  } catch (e) {
    notice(e.message);
  } finally {
    byId("upload-button").disabled = false;
  }
};
byId("drop").ondragover = (e) => e.preventDefault();
byId("drop").ondrop = (e) => {
  e.preventDefault();
  byId("file").files = e.dataTransfer.files;
};
byId("more-packages").onclick = () =>
  morePackages().catch((e) => notice(e.message));
byId("more-legacy").onclick = () =>
  moreLegacy().catch((e) => notice(e.message));
byId("question-form").onsubmit = async (e) => {
  e.preventDefault();
  const question = byId("question").value.trim();
  if (!question) return;
  byId("ask").disabled = true;
  byId("ask").textContent = "Ищу ответ…";
  try {
    const result = await (
      await api("/queries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      })
    ).json();
    renderMessage(question, result);
    byId("question").value = "";
    notice();
  } catch (e) {
    notice(e.message);
  } finally {
    byId("ask").disabled = false;
    byId("ask").textContent = "Отправить";
  }
};
byId("close-source").onclick = () => {
  byId("source").close();
  resetPreview();
};
byId("open-file").onclick = async () => {
  try {
    const r = await api(`/documents/${selectedDocumentId}/source`);
    resetPreview();
    previewUrl = URL.createObjectURL(await r.blob());
    byId("preview").src = previewUrl;
    byId("preview").hidden = false;
  } catch (e) {
    notice(e.message);
  }
};
