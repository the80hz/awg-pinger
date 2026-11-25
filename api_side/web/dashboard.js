"use strict";

const byId = (id) => document.getElementById(id);
const clock = new Intl.DateTimeFormat("ru", { dateStyle: "short", timeStyle: "short" });
const staleAfter = 50 * 60 * 1000;
const labels = { ok: "Работает", down: "Сбой", stale: "Данные устарели", missing: "Нет данных" };
let snapshot = { clients: [], results: [] };

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function health(result) {
  if (!result) return "missing";
  if (Date.now() - Date.parse(result.checked_at) > staleAfter) return "stale";
  return result.ok ? "ok" : "down";
}

function render() {
  const { clients, results } = snapshot;
  const grouped = new Map();
  for (const item of results) {
    if (!grouped.has(item.server_id)) grouped.set(item.server_id, new Map());
    grouped.get(item.server_id).set(item.client_id, item);
  }

  byId("sources").replaceChildren(...clients.map((client) => {
    const checks = results.filter((item) => item.client_id === client);
    const fresh = checks.filter((item) => !["missing", "stale"].includes(health(item)));
    const latest = Math.max(...checks.map((item) => Date.parse(item.checked_at)));
    const summary = checks.length ? (fresh.length ? "На связи" : "Данные устарели") : "Ожидание данных";
    const status = checks.length ? (fresh.length ? "ok" : "stale") : "missing";
    const card = element("article", "", "source");
    card.append(
      element("strong", client),
      element("span", summary, "state " + status),
      element("small", checks.length ? `Проверок: ${checks.length} · Сбоев: ${fresh.filter((item) => !item.ok).length}` : "Проверки ещё не поступили"),
      element("small", Number.isFinite(latest) ? "Последний отчёт " + clock.format(latest) : "")
    );
    return card;
  }));

  const head = element("tr", "");
  for (const title of ["Узел", ...clients]) {
    const column = element("th", title);
    column.scope = "col";
    head.append(column);
  }
  byId("columns").replaceChildren(head);

  const query = byId("search").value.trim().toLowerCase();
  const issuesOnly = byId("issues").checked;
  const rows = [];
  for (const [id, checks] of [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const name = checks.values().next().value.server_name || id;
    if (!`${name} ${id}`.toLowerCase().includes(query)) continue;
    if (issuesOnly && !clients.some((client) => health(checks.get(client)) !== "ok")) continue;
    const row = element("tr", "");
    const title = element("th", name);
    title.scope = "row";
    row.append(title);
    for (const client of clients) {
      const item = checks.get(client);
      const status = health(item);
      const cell = element("td", "");
      cell.append(element("span", labels[status], "state " + status));
      if (item) {
        const date = Date.parse(item.checked_at);
        const timing = item.duration_ms === null ? "" : ` · ${item.duration_ms} мс`;
        cell.append(element("small", clock.format(date) + timing));
        if (!item.ok) cell.append(element("small", item.comment));
      }
      row.append(cell);
    }
    rows.push(row);
  }
  byId("rows").replaceChildren(...rows);
  byId("empty").hidden = rows.length > 0;
  byId("empty").textContent = results.length ? "Нет проверок по этому фильтру." : "Ожидаем первые результаты.";
}

async function refresh() {
  const button = byId("refresh");
  if (button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch("/dashboard/data", { cache: "no-store" });
    if (!response.ok) throw new Error("Unavailable");
    snapshot = await response.json();
    render();
    byId("updated").textContent = "Обновлено " + clock.format(new Date());
    byId("error").hidden = true;
  } catch {
    byId("error").textContent = "Не удалось обновить данные.";
    byId("error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

byId("search").addEventListener("input", render);
byId("issues").addEventListener("change", render);
byId("refresh").addEventListener("click", refresh);
render();
refresh();
setInterval(refresh, 30_000);
