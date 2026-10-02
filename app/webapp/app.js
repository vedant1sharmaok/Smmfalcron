/* FALARON Mini App. No framework, no build step. All server text is rendered with textContent. */
(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  const initData = tg ? tg.initData : "";

  const $ = (s) => document.querySelector(s);
  const view = $("#view");
  const state = { me: null, cfg: null, tab: "shop", poll: null };

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); };
  const money = (paise) => (state.cfg ? state.cfg.symbol : "₹") + (Number(paise) / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

  let toastTimer;
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 2800);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": initData },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON error */ }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = typeof d === "string" ? d : Array.isArray(d) ? "Please check the values you entered." : "Something went wrong (" + res.status + ")";
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return data;
  }

  function stopPolling() { if (state.poll) { clearInterval(state.poll); state.poll = null; } }

  async function refreshMe() {
    state.me = await api("/api/me");
    $("#balance").textContent = money(state.me.wallet.available_paise);
  }

  function setTab(tab) {
    stopPolling(); state.tab = tab;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    ({ shop: showShop, orders: showOrders, wallet: showWallet })[tab]();
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

  function gate(msg) {
    clear(view);
    view.append(h("div", { class: "card" }, h("p", {}, msg)));
  }

  /* ---------------- shop ---------------- */
  async function showShop() {
    clear(view);
    if (!state.me.terms_accepted) {
      view.append(h("div", { class: "card" },
        h("h2", {}, "One quick step"),
        h("p", {}, "Please open the bot chat and tap “I agree — continue” to accept the Terms and Privacy Policy. Then come back here."),
        tg ? h("button", { class: "primary", onclick: () => tg.close() }, "Back to chat") : null));
      return;
    }
    const flags = state.cfg.flags || {};
    if (flags.global_maintenance || flags.read_only || flags.kill_new_orders) {
      view.append(h("div", { class: "card mut" }, "New orders are temporarily paused. You can still browse."));
    }
    try {
      const cat = await api("/api/catalog");
      if (cat.stars.length) {
        view.append(h("h2", {}, "Featured"));
        cat.stars.forEach((s) => view.append(h("div", { class: "card item", onclick: () => openService(s.service_id) },
          h("div", { class: "row" }, h("strong", {}, s.label), h("span", {}, s.rate_display + " / 1k")), h("div", { class: "mut" }, s.name))));
      }
      view.append(h("h2", {}, "Categories"));
      if (!cat.categories.length) view.append(h("div", { class: "card mut" }, "No services are available yet."));
      cat.categories.forEach((c) => view.append(h("div", { class: "card item", onclick: () => openCategory(c) },
        h("div", { class: "row" }, h("strong", {}, (c.emoji ? c.emoji + " " : "") + c.name), h("span", { class: "mut" }, c.service_count + " ›")))));
    } catch (e) { view.append(h("div", { class: "card bad" }, e.message)); }
  }

  async function openCategory(cat, offset = 0, holder) {
    if (!holder) {
      clear(view);
      view.append(h("button", { class: "back", onclick: showShop }, "‹ Back"), h("h2", {}, cat.name));
      holder = h("div", {}); view.append(holder);
    }
    try {
      const r = await api("/api/categories/" + encodeURIComponent(cat.id) + "/services?limit=40&offset=" + offset);
      r.services.forEach((s) => holder.append(h("div", { class: "card item", onclick: () => openService(s.id) },
        h("strong", {}, s.name),
        h("div", { class: "row mut" }, h("span", {}, s.sell_per_1000_display + " / 1k"), h("span", {}, s.min_qty + " – " + s.max_qty)))));
      const more = holder.querySelector(".more"); if (more) more.remove();
      if (r.has_more) holder.append(h("button", { class: "ghost more", onclick: () => openCategory(cat, offset + 40, holder) }, "Load more"));
    } catch (e) { holder.append(h("div", { class: "card bad" }, e.message)); }
  }

  async function openService(id) {
    clear(view);
    view.append(h("button", { class: "back", onclick: showShop }, "‹ Back"));
    let svc;
    try { svc = await api("/api/services/" + encodeURIComponent(id)); }
    catch (e) { view.append(h("div", { class: "card bad" }, e.message)); return; }

    let key = uid(), timer;
    const link = h("input", { type: "url", inputmode: "url", placeholder: "https://…", maxlength: "512", autocomplete: "off" });
    const qty = h("input", { type: "number", inputmode: "numeric", min: svc.min_qty, max: svc.max_qty, placeholder: svc.min_qty + " – " + svc.max_qty });
    const extra = svc.type === "comments" ? h("textarea", { placeholder: "One comment per line", maxlength: "20000" })
                : svc.type === "mentions" ? h("textarea", { placeholder: "One username per line", maxlength: "20000" }) : null;
    const coupon = h("input", { type: "text", placeholder: "Optional", maxlength: "32", autocapitalize: "characters", autocomplete: "off" });
    const quoteBox = h("div", { class: "card mut" }, "Enter a quantity to see the price.");
    const btn = h("button", { class: "primary", disabled: true }, "Place order");

    const touch = () => { key = uid(); clearTimeout(timer); timer = setTimeout(quote, 350); };
    [link, qty, coupon, extra].forEach((el) => el && el.addEventListener("input", touch));

    async function quote() {
      const q = parseInt(qty.value, 10);
      btn.disabled = true;
      if (!q || q < svc.min_qty || q > svc.max_qty) { quoteBox.textContent = "Quantity must be between " + svc.min_qty + " and " + svc.max_qty + "."; return; }
      try {
        const r = await api("/api/quote", { method: "POST", body: { service_id: svc.id, quantity: q, coupon_code: coupon.value.trim() || null } });
        clear(quoteBox);
        quoteBox.append(h("div", { class: "row" }, h("span", {}, "Total"), h("strong", {}, r.charge_display)));
        if (r.discount_paise > 0) quoteBox.append(h("div", { class: "ok" }, "Coupon applied: −" + money(r.discount_paise)));
        btn.disabled = false;
      } catch (e) { quoteBox.textContent = e.message; }
    }

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const body = { service_id: svc.id, link: link.value.trim(), quantity: parseInt(qty.value, 10), coupon_code: coupon.value.trim() || null, idempotency_key: key };
      if (extra) body[svc.type === "comments" ? "comments" : "mentions"] = extra.value;
      try {
        const o = await api("/api/orders", { method: "POST", body });
        if (o.status === "failed") toast(o.fail_reason || "The order could not be placed. You were not charged.");
        else if (o.status === "review") toast("Order received — confirming with the provider.");
        else toast("Order placed ✓");
        await refreshMe(); setTab("orders");
      } catch (e) { toast(e.message); btn.disabled = false; }
    });

    view.append(h("div", { class: "card" },
      h("strong", {}, svc.name), svc.description ? h("div", { class: "mut" }, svc.description) : null,
      h("div", { class: "mut" }, svc.sell_per_1000_display + " per 1,000" + (svc.average_time ? " · " + svc.average_time : "")),
      svc.refillable ? h("div", { class: "mut" }, "Refill supported") : null),
      h("label", {}, "Link"), link, h("label", {}, "Quantity"), qty,
      extra ? [h("label", {}, svc.type === "comments" ? "Comments" : "Usernames"), extra] : null,
      h("label", {}, "Coupon"), coupon, quoteBox, btn);
  }

  /* ---------------- orders ---------------- */
  const CANCELABLE = new Set(["pending", "awaiting_provider", "processing", "in_progress"]);
  const REFILLABLE = new Set(["completed", "partial"]);

  async function showOrders() {
    clear(view); view.append(h("h2", {}, "Your orders"));
    try {
      const r = await api("/api/orders?limit=30");
      if (!r.orders.length) view.append(h("div", { class: "card mut" }, "No orders yet."));
      r.orders.forEach((o) => view.append(orderCard(o)));
    } catch (e) { view.append(h("div", { class: "card bad" }, e.message)); }
  }

  function orderCard(o) {
    const card = h("div", { class: "card" });
    const render = (o) => {
      clear(card);
      card.append(h("div", { class: "row" }, h("strong", {}, o.public_id), h("span", { class: "status" }, o.status_label)),
        h("div", { class: "mut", style: "word-break:break-all" }, o.link),
        h("div", { class: "row mut" }, h("span", {}, "Qty " + o.quantity), h("span", {}, o.charge_display)),
        o.remains != null ? h("div", { class: "mut" }, "Remaining " + o.remains) : null,
        o.fail_reason ? h("div", { class: "bad" }, o.fail_reason) : null);
      const act = (label, path, okMsg) => h("button", { class: "ghost", onclick: async () => {
        try { const n = await api("/api/orders/" + o.public_id + path, { method: "POST" }); toast(okMsg); await refreshMe(); render(n); }
        catch (e) { toast(e.message); } } }, label);
      const row = h("div", { class: "chips" });
      row.append(act("Refresh", "/refresh", "Updated"));
      if (CANCELABLE.has(o.status)) row.append(act("Cancel", "/cancel", "Cancel requested"));
      if (REFILLABLE.has(o.status)) row.append(act("Refill", "/refill", "Refill requested"));
      card.append(row);
    };
    render(o); return card;
  }

  /* ---------------- wallet ---------------- */
  async function showWallet() {
    clear(view);
    try {
      const w = await api("/api/wallet");
      view.append(h("div", { class: "card" }, h("div", { class: "mut" }, "Available"), h("h1", {}, w.wallet_display.available),
        h("div", { class: "mut" }, "Reserved " + w.wallet_display.reserved)));

      view.append(h("h2", {}, "Add funds"));
      const amount = h("input", { type: "number", inputmode: "numeric", placeholder: "Amount in " + state.cfg.currency, min: Math.ceil(state.cfg.min_deposit_paise / 100) });
      const chips = h("div", { class: "chips" });
      [100, 500, 1000, 2000, 5000].forEach((v) => chips.append(h("button", { class: "chip", onclick: () => { amount.value = v; } }, state.cfg.symbol + v)));
      const out = h("div", {});
      view.append(h("div", { class: "card" }, chips, h("label", {}, "Amount"), amount,
        h("button", { class: "primary", onclick: (ev) => deposit(ev.currentTarget, amount, out) }, "Continue"), out));

      view.append(h("h2", {}, "Recent activity"));
      if (!w.ledger.length) view.append(h("div", { class: "card mut" }, "No activity yet."));
      w.ledger.forEach((e) => view.append(h("div", { class: "card" },
        h("div", { class: "row" }, h("span", {}, e.reason), h("strong", { class: e.amount_paise >= 0 && e.type !== "debit" ? "ok" : "" }, e.amount_display)),
        h("div", { class: "mut" }, (e.created_at || "").replace("T", " ").slice(0, 16)))));
    } catch (e) { view.append(h("div", { class: "card bad" }, e.message)); }
  }

  async function deposit(button, amountInput, out) {
    const rupees = parseInt(amountInput.value, 10);
    if (!rupees) { toast("Enter an amount"); return; }
    button.disabled = true; clear(out);
    try {
      const p = await api("/api/deposit", { method: "POST", body: { amount_paise: rupees * 100, idempotency_key: uid() } });
      if (p.method === "razorpay" && p.checkout_url) {
        out.append(h("p", { class: "mut" }, "Complete the payment in the secure window. Your wallet updates automatically."));
        if (tg && tg.openLink) tg.openLink(p.checkout_url); else window.open(p.checkout_url, "_blank", "noopener");
        watchPayment(p.public_id, out);
      } else if (p.method === "manual") {
        out.append(h("div", { class: "card" }, h("strong", {}, "Invoice " + p.public_id), h("p", { style: "white-space:pre-wrap" }, p.instructions || "Contact support to complete this payment."),
          h("p", { class: "mut" }, "An admin credits your wallet after verifying the payment.")));
      } else if (state.cfg.demo_payments) {
        out.append(h("button", { class: "ghost", onclick: async () => { await api("/api/deposit/" + p.public_id + "/mock-pay", { method: "POST" }); toast("Demo payment credited"); await refreshMe(); showWallet(); } }, "Demo: pay now"));
      } else { out.append(h("div", { class: "bad" }, "Deposits are unavailable right now.")); }
    } catch (e) { toast(e.message); }
    button.disabled = false;
  }

  function watchPayment(id, out) {
    stopPolling(); let n = 0;
    state.poll = setInterval(async () => {
      n += 1;
      try {
        const s = await api("/api/deposit/" + id);
        if (s.status === "paid") { stopPolling(); toast("Payment received ✓"); await refreshMe(); showWallet(); }
        else if (s.status === "review") { stopPolling(); clear(out); out.append(h("div", { class: "card" }, "Your payment needs a quick manual check. It will be resolved shortly.")); }
        else if (s.status === "expired" || n > 60) { stopPolling(); }
      } catch (_) { /* transient; keep polling */ }
    }, 4000);
  }

  /* ---------------- boot ---------------- */
  (async () => {
    if (!initData) { gate("Please open this page from inside Telegram."); return; }
    try {
      [state.me, state.cfg] = await Promise.all([api("/api/me"), api("/api/config")]);
      $("#brand").textContent = state.cfg.brand;
      document.title = state.cfg.brand;
      $("#balance").textContent = money(state.me.wallet.available_paise);
      setTab("shop");
    } catch (e) { gate(e.status === 401 ? "Your session expired. Close and reopen the app from the bot." : e.message); }
  })();
})();
