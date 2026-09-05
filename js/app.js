const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const INIT_DATA = tg?.initData || "";

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": INIT_DATA,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = body.detail || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function showToast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}

function renderBottomNav(active) {
  const items = [
    { key: "index", href: "/index.html", icon: "🏠", label: "Главная" },
    { key: "clips", href: "/clips.html", icon: "💼", label: "Клипы" },
    { key: "earnings", href: "/earnings.html", icon: "₽", label: "Заработок" },
  ];
  const nav = document.createElement("div");
  nav.className = "bottom-nav";
  nav.innerHTML = items
    .map(
      (i) => `<a href="${i.href}" class="${i.key === active ? "active" : ""}">
        <span class="nav-icon">${i.icon}</span>${i.label}
      </a>`
    )
    .join("");
  document.body.appendChild(nav);
}

function offerProgressHtml(offer) {
  const total = offer.budget_total || 0;
  const paid = offer.budget_paid || 0;
  const pct = total > 0 ? Math.min(100, Math.round((paid / total) * 100)) : 0;
  if (total <= 0) return "";
  return `
    <div class="progress-row">
      <span>${paid.toFixed(0)} ₽ из ${total.toFixed(0)} ₽ · выплачено креаторам</span>
      <span class="percent">${pct}%</span>
    </div>
    <div class="progress-track"><div class="progress-fill" style="width:${pct}%;"></div></div>
  `;
}

function openOfferDetails(offer) {
  const overlay = document.createElement("div");
  overlay.className = "details-overlay";
  overlay.innerHTML = `
    <div class="details-sheet">
      <div class="details-header">
        <div style="font-weight:700;font-size:18px;">Детали задания</div>
        <span class="close-btn" onclick="this.closest('.details-overlay').remove()">✕</span>
      </div>
      ${offer.image_url ? `<img class="offer-cover" src="${offer.image_url}" />` : ""}
      <div style="font-size:18px;font-weight:700;margin-bottom:6px;">${offer.title}</div>
      <div class="offer-meta-row">
        <span class="meta-chip">💰 ${offer.price} ₽ / 1000 просмотров</span>
        <span class="meta-chip">👁 от ${offer.min_views || 0} просмотров</span>
        <span class="meta-chip">📌 ${offer.channel}</span>
      </div>
      ${offerProgressHtml(offer)}
      <div class="details-body" style="margin-top:14px;">${(offer.details || offer.description || "Описание пока не добавлено.").replace(/</g, "&lt;")}</div>
      <button class="btn btn-accent" style="margin-top:18px;" onclick="window.selectOfferFromDetails && window.selectOfferFromDetails(${offer.id}, '${offer.title.replace(/'/g, "")}')">Выбрать оффер</button>
    </div>
  `;
  document.body.appendChild(overlay);
}

async function maybeShowAdminLink() {
  try {
    const me = await api("/api/me");
    if (me.is_admin) {
      const link = document.createElement("a");
      link.href = "/admin.html";
      link.className = "admin-link";
      link.textContent = "🛠 Открыть админ-панель";
      const container = document.querySelector(".container");
      container.appendChild(link);
    }
    return me;
  } catch (e) {
    return null;
  }
}
