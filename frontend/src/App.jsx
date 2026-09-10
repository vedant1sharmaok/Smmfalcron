import { useState, useEffect, useCallback, useRef } from "react";

// ── Telegram WebApp SDK bridge ──────────────────────────────────────────────
const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const tgUser = tg?.initDataUnsafe?.user || { first_name: "User", id: 0 };
if (tg) { tg.ready(); tg.expand(); tg.setHeaderColor("#0F0F0F"); tg.setBackgroundColor("#0F0F0F"); }

// ── API client ───────────────────────────────────────────────────────────────
const API = "https://your-domain.com/miniapp/api";
let _sessionToken = null;

async function apiCall(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (_sessionToken) headers["Authorization"] = `Bearer ${_sessionToken}`;
  if (!_sessionToken && initData) headers["X-Telegram-Init-Data"] = initData;

  const resp = await fetch(`${API}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) { _sessionToken = null; throw new Error("AUTH"); }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.user_message || err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

const api = {
  auth:       (initData)         => apiCall("POST", "/auth", { init_data: initData }),
  categories: ()                 => apiCall("GET",  "/categories"),
  services:   (catId, page)      => apiCall("GET",  `/services?category_id=${catId}&page=${page}`),
  service:    (pubId)            => apiCall("GET",  `/services/${pubId}`),
  preview:    (body)             => apiCall("POST", "/orders/preview", body),
  order:      (body)             => apiCall("POST", "/orders", body),
  orders:     (page)             => apiCall("GET",  `/orders?page=${page}`),
  orderDetail:(id)               => apiCall("GET",  `/orders/${id}`),
  wallet:     ()                 => apiCall("GET",  "/wallet"),
  deposit:    (body)             => apiCall("POST", "/wallet/deposit", body),
  profile:    ()                 => apiCall("GET",  "/profile"),
};

// ── Design tokens (inline — no Tailwind compiler needed) ──────────────────
const T = {
  bg0: "#0F0F0F", bg1: "#1A1A1A", bg2: "#252525",
  accent: "#6C63FF", accentDim: "#3D3880",
  success: "#22C55E", danger: "#EF4444", warning: "#F59E0B",
  text0: "#F5F5F5", text1: "#A1A1AA", text2: "#6B7280",
  border: "#2A2A2A",
};

const css = (obj) => Object.entries(obj).map(([k, v]) =>
  `${k.replace(/([A-Z])/g, m => `-${m.toLowerCase()}`)}:${v}`).join(";");

// ── Shared components ─────────────────────────────────────────────────────
function Spinner() {
  return (
    <div style={{ display:"flex", justifyContent:"center", padding:"48px 0" }}>
      <div style={{
        width:28, height:28, borderRadius:"50%",
        border:`3px solid ${T.bg2}`,
        borderTopColor: T.accent,
        animation: "spin 0.7s linear infinite",
      }}/>
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
    </div>
  );
}

function Err({ msg, onRetry }) {
  return (
    <div style={{ padding:"24px 16px", textAlign:"center" }}>
      <div style={{ fontSize:13, color:T.danger, marginBottom:12 }}>{msg}</div>
      {onRetry && <Btn onClick={onRetry} variant="ghost">Try again</Btn>}
    </div>
  );
}

function Btn({ children, onClick, variant="primary", disabled, style={} }) {
  const base = {
    display:"block", width:"100%", padding:"14px 0",
    borderRadius:8, border:"none", cursor: disabled ? "not-allowed" : "pointer",
    fontSize:15, fontWeight:600, transition:"opacity 0.15s",
    opacity: disabled ? 0.45 : 1, ...style,
  };
  const variants = {
    primary:  { background: T.accent, color: "#fff" },
    ghost:    { background: "transparent", color: T.text1, border:`1px solid ${T.border}` },
    danger:   { background: T.danger, color: "#fff" },
    success:  { background: T.success, color: "#fff" },
  };
  return <button style={{ ...base, ...variants[variant] }} onClick={disabled ? undefined : onClick}>{children}</button>;
}

function AmountTag({ value, currency="₹", large }) {
  return (
    <span style={{
      fontVariantNumeric:"tabular-nums", fontWeight:600,
      fontSize: large ? 28 : 16, color: T.text0,
    }}>
      {currency}{typeof value === "number" ? value.toFixed(2) : value}
    </span>
  );
}

function Badge({ label, color=T.accentDim, textColor=T.accent }) {
  return (
    <span style={{
      display:"inline-block", padding:"2px 8px", borderRadius:4,
      fontSize:11, fontWeight:600, background:color, color:textColor,
    }}>{label}</span>
  );
}

function StatusBadge({ status }) {
  const map = {
    pending:    [T.warning,    "#78350F", "Pending"],
    processing: [T.accentDim, T.accent,  "Processing"],
    in_progress:[T.accentDim, T.accent,  "In progress"],
    completed:  ["#14532D",   T.success, "Completed"],
    partial:    ["#713F12",   T.warning, "Partial"],
    failed:     ["#450A0A",   T.danger,  "Failed"],
    cancelled:  [T.bg2,       T.text1,   "Cancelled"],
    refunded:   [T.bg2,       T.text1,   "Refunded"],
  };
  const [bg, fg, label] = map[status] || [T.bg2, T.text1, status];
  return <Badge label={label} color={bg} textColor={fg}/>;
}

function Card({ children, style={}, accent }) {
  return (
    <div style={{
      background: T.bg1, borderRadius:10,
      borderLeft: accent ? `3px solid ${T.accent}` : `1px solid ${T.border}`,
      padding:"14px 16px", marginBottom:10, ...style,
    }}>{children}</div>
  );
}

function Row({ label, value, muted }) {
  return (
    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:8 }}>
      <span style={{ fontSize:13, color:T.text1 }}>{label}</span>
      <span style={{ fontSize:13, color: muted ? T.text2 : T.text0, fontWeight:500 }}>{value}</span>
    </div>
  );
}

function Input({ value, onChange, placeholder, type="text", style={} }) {
  return (
    <input
      type={type} value={value} onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width:"100%", boxSizing:"border-box",
        background:T.bg2, border:`1px solid ${T.border}`,
        borderRadius:8, padding:"13px 14px",
        fontSize:15, color:T.text0, outline:"none",
        fontFamily:"inherit", ...style,
      }}
    />
  );
}

// ── Screens ───────────────────────────────────────────────────────────────

// ── Catalog ──────────────────────────────────────────────────────────────────
function CatalogScreen({ onSelectService }) {
  const [cats, setCats] = useState([]);
  const [activeCat, setActiveCat] = useState(null);
  const [services, setServices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [svcLoading, setSvcLoading] = useState(false);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);

  useEffect(() => {
    api.categories().then(data => {
      setCats(data.items || []);
      if (data.items?.length) setActiveCat(data.items[0].id);
      setLoading(false);
    }).catch(e => { setError(e.message); setLoading(false); });
  }, []);

  useEffect(() => {
    if (!activeCat) return;
    setSvcLoading(true); setPage(1);
    api.services(activeCat, 1).then(data => {
      setServices(data.items || []);
      setHasMore((data.items?.length || 0) < (data.total || 0));
      setSvcLoading(false);
    }).catch(e => { setError(e.message); setSvcLoading(false); });
  }, [activeCat]);

  const loadMore = () => {
    const next = page + 1;
    api.services(activeCat, next).then(data => {
      setServices(s => [...s, ...(data.items || [])]);
      setHasMore(services.length + (data.items?.length||0) < (data.total||0));
      setPage(next);
    });
  };

  const filtered = services.filter(s =>
    !search || s.display_name.toLowerCase().includes(search.toLowerCase())
  );

  if (loading) return <Spinner/>;
  if (error) return <Err msg={error} onRetry={() => window.location.reload()}/>;

  return (
    <div>
      {/* Category tabs */}
      <div style={{
        display:"flex", gap:8, overflowX:"auto", padding:"12px 16px 0",
        scrollbarWidth:"none", borderBottom:`1px solid ${T.border}`,
      }}>
        {cats.map(c => (
          <button key={c.id} onClick={() => { setActiveCat(c.id); setSearch(""); }}
            style={{
              flexShrink:0, padding:"7px 14px", borderRadius:20, border:"none",
              background: activeCat===c.id ? T.accent : T.bg2,
              color: activeCat===c.id ? "#fff" : T.text1,
              fontSize:13, fontWeight:600, cursor:"pointer",
              transition:"background 0.15s",
            }}>
            {c.name}
          </button>
        ))}
      </div>

      {/* Search */}
      <div style={{ padding:"12px 16px 8px" }}>
        <Input value={search} onChange={setSearch} placeholder="Search services…"/>
      </div>

      {/* Services */}
      <div style={{ padding:"0 16px" }}>
        {svcLoading ? <Spinner/> : filtered.length === 0 ? (
          <div style={{ textAlign:"center", padding:"40px 0", color:T.text2, fontSize:14 }}>
            No services found
          </div>
        ) : filtered.map(svc => (
          <ServiceCard key={svc.public_id} svc={svc} onTap={() => onSelectService(svc.public_id)}/>
        ))}
        {hasMore && !svcLoading && (
          <Btn variant="ghost" onClick={loadMore} style={{ marginBottom:12 }}>Load more</Btn>
        )}
      </div>
    </div>
  );
}

function ServiceCard({ svc, onTap }) {
  return (
    <Card accent onClick={onTap} style={{ cursor:"pointer" }}>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start" }}>
        <div style={{ flex:1, paddingRight:12 }}>
          <div style={{ fontSize:14, fontWeight:600, color:T.text0, marginBottom:4, lineHeight:1.35 }}>
            {svc.display_name}
          </div>
          <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
            {svc.refill_eligible && <Badge label="Refill"/>}
            {svc.cancel_eligible && <Badge label="Cancel" color="#1a1a00" textColor={T.warning}/>}
            <Badge label={`${svc.min_qty}–${Number(svc.max_qty).toLocaleString()}`}
                   color={T.bg2} textColor={T.text1}/>
          </div>
        </div>
        <div style={{ textAlign:"right", flexShrink:0 }}>
          <div style={{ fontSize:11, color:T.text2, marginBottom:2 }}>per 1000</div>
          <AmountTag value={parseFloat(svc.price_per_1000)}/>
        </div>
      </div>
    </Card>
  );
}

// ── Service Detail + Order ────────────────────────────────────────────────────
function ServiceDetailScreen({ publicId, wallet, onBack, onOrderPlaced }) {
  const [svc, setSvc] = useState(null);
  const [qty, setQty] = useState("");
  const [link, setLink] = useState("");
  const [coupon, setCoupon] = useState("");
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [step, setStep] = useState("detail"); // detail | confirm | success
  const [idem] = useState(() => crypto.randomUUID());

  useEffect(() => {
    api.service(publicId).then(data => { setSvc(data); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [publicId]);

  const suggestions = svc ? (() => {
    const mn = svc.min_qty, mx = svc.max_qty;
    if (mn === mx) return [mn];
    const pts = [mn, mn*5, mn*10, mx].filter((v,i,a) => v<=mx && a.indexOf(v)===i);
    return [...new Set(pts)].slice(0,4);
  })() : [];

  const getPreview = async () => {
    setError(null);
    const q = parseInt(qty);
    if (!q || q < svc.min_qty || q > svc.max_qty) {
      setError(`Quantity must be ${svc.min_qty}–${Number(svc.max_qty).toLocaleString()}`);
      return;
    }
    if (!link.trim()) { setError("Enter the link for this service"); return; }
    try {
      const data = await api.preview({
        service_public_id: publicId, quantity: q,
        link: link.trim(), coupon_code: coupon || undefined,
      });
      setPreview(data);
      setStep("confirm");
    } catch(e) { setError(e.message); }
  };

  const placeOrder = async () => {
    setSubmitting(true); setError(null);
    try {
      const data = await api.order({
        service_public_id: publicId, quantity: parseInt(qty),
        link: link.trim(), coupon_code: coupon || undefined,
        idempotency_key: idem,
      });
      setStep("success");
      setTimeout(() => onOrderPlaced(data.public_ref), 1800);
    } catch(e) { setError(e.message); setSubmitting(false); }
  };

  if (loading) return <Spinner/>;
  if (!svc) return <Err msg={error || "Service not found"} onRetry={onBack}/>;

  if (step === "success") return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center",
                  justifyContent:"center", padding:"80px 32px", textAlign:"center" }}>
      <div style={{
        width:64, height:64, borderRadius:"50%", background:"#14532D",
        display:"flex", alignItems:"center", justifyContent:"center",
        fontSize:28, marginBottom:20,
        animation:"popIn 0.4s cubic-bezier(.34,1.56,.64,1) both",
      }}>✓</div>
      <style>{`@keyframes popIn{from{transform:scale(0.4);opacity:0}to{transform:scale(1);opacity:1}}`}</style>
      <div style={{ fontSize:18, fontWeight:700, color:T.text0, marginBottom:8 }}>Order placed</div>
      <div style={{ fontSize:13, color:T.text1 }}>Processing usually takes a few minutes</div>
    </div>
  );

  return (
    <div style={{ paddingBottom:80 }}>
      {/* Back */}
      <div style={{ padding:"12px 16px 0" }}>
        <button onClick={onBack} style={{ background:"none", border:"none",
          color:T.text1, fontSize:14, cursor:"pointer", padding:0 }}>
          ← Back
        </button>
      </div>

      {/* Header */}
      <div style={{ padding:"12px 16px 16px", borderBottom:`1px solid ${T.border}` }}>
        <div style={{ fontSize:17, fontWeight:700, color:T.text0, marginBottom:8, lineHeight:1.3 }}>
          {svc.display_name}
        </div>
        <div style={{ display:"flex", gap:6, flexWrap:"wrap", marginBottom:12 }}>
          {svc.refill_eligible && <Badge label="Refill"/>}
          {svc.cancel_eligible && <Badge label="Cancel" color="#1a1a00" textColor={T.warning}/>}
        </div>
        <Row label="Min / Max" value={`${svc.min_qty.toLocaleString()} – ${Number(svc.max_qty).toLocaleString()}`}/>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
          <span style={{ fontSize:13, color:T.text1 }}>Price per 1,000</span>
          <AmountTag value={parseFloat(svc.price_per_1000)} large/>
        </div>
      </div>

      {step === "detail" && (
        <div style={{ padding:"16px" }}>
          {/* Quantity suggestions */}
          <div style={{ marginBottom:12 }}>
            <div style={{ fontSize:12, color:T.text2, marginBottom:8 }}>Quick select</div>
            <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:6 }}>
              {suggestions.map(s => (
                <button key={s} onClick={() => setQty(String(s))}
                  style={{
                    padding:"10px 4px", borderRadius:8, border:"none",
                    background: qty===String(s) ? T.accentDim : T.bg2,
                    color: qty===String(s) ? T.accent : T.text1,
                    fontSize:13, fontWeight:600, cursor:"pointer",
                  }}>
                  {Number(s).toLocaleString()}
                </button>
              ))}
            </div>
          </div>

          <div style={{ marginBottom:12 }}>
            <div style={{ fontSize:12, color:T.text2, marginBottom:6 }}>Quantity</div>
            <Input value={qty} onChange={setQty} placeholder={`${svc.min_qty}–${Number(svc.max_qty).toLocaleString()}`} type="number"/>
          </div>

          <div style={{ marginBottom:12 }}>
            <div style={{ fontSize:12, color:T.text2, marginBottom:6 }}>Link</div>
            <Input value={link} onChange={setLink} placeholder="https://instagram.com/yourprofile"/>
          </div>

          <div style={{ marginBottom:16 }}>
            <div style={{ fontSize:12, color:T.text2, marginBottom:6 }}>Coupon (optional)</div>
            <Input value={coupon} onChange={setCoupon} placeholder="DISCOUNT10"/>
          </div>

          {error && <div style={{ fontSize:13, color:T.danger, marginBottom:12 }}>{error}</div>}

          {/* Wallet balance reminder */}
          {wallet && (
            <div style={{ fontSize:12, color:T.text2, marginBottom:14, textAlign:"right" }}>
              Wallet: <span style={{ color:T.text1, fontWeight:600 }}>₹{parseFloat(wallet.balance).toFixed(2)}</span>
            </div>
          )}

          <Btn onClick={getPreview}>Preview order</Btn>
        </div>
      )}

      {step === "confirm" && preview && (
        <div style={{ padding:"16px" }}>
          <div style={{ fontSize:14, fontWeight:600, color:T.text0, marginBottom:12 }}>Order summary</div>
          <Card>
            <Row label="Service" value={svc.display_name}/>
            <Row label="Quantity" value={parseInt(qty).toLocaleString()}/>
            <Row label="Link" value={link.length > 28 ? link.slice(0,25)+"…" : link} muted/>
            {preview.coupon_applied && <Row label="Coupon" value={`-₹${parseFloat(preview.coupon_discount||0).toFixed(2)}`}/>}
            {preview.reseller_price && <Row label="Reseller price" value={`₹${parseFloat(preview.reseller_price).toFixed(2)}`}/>}
          </Card>
          <Card style={{ background:T.accentDim, borderColor:T.accentDim }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
              <span style={{ fontSize:15, fontWeight:600, color:T.text0 }}>Total</span>
              <AmountTag value={parseFloat(preview.price)} large/>
            </div>
          </Card>

          {parseFloat(wallet?.balance||0) < parseFloat(preview.price) && (
            <div style={{ fontSize:13, color:T.danger, margin:"8px 0 12px",
                          padding:"10px 12px", background:"#450A0A", borderRadius:8 }}>
              Insufficient balance. Please deposit first.
            </div>
          )}

          {error && <div style={{ fontSize:13, color:T.danger, marginBottom:12 }}>{error}</div>}

          <Btn onClick={placeOrder} disabled={submitting || parseFloat(wallet?.balance||0) < parseFloat(preview.price)}>
            {submitting ? "Placing order…" : "Confirm & pay"}
          </Btn>
          <div style={{ marginTop:10 }}>
            <Btn variant="ghost" onClick={() => setStep("detail")}>Edit</Btn>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Orders ────────────────────────────────────────────────────────────────────
function OrdersScreen() {
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [page, setPage] = useState(1);

  const load = useCallback((p=1) => {
    setLoading(true);
    api.orders(p).then(data => {
      setOrders(p===1 ? (data.items||[]) : o => [...o,...(data.items||[])]);
      setLoading(false); setPage(p);
    }).catch(e => { setError(e.message); setLoading(false); });
  }, []);

  useEffect(() => { load(1); }, [load]);

  if (selected) return <OrderDetailScreen orderId={selected} onBack={() => { setSelected(null); load(1); }}/>;

  return (
    <div style={{ padding:"0 16px" }}>
      <div style={{ padding:"16px 0 12px", fontSize:16, fontWeight:700, color:T.text0 }}>Orders</div>
      {loading && orders.length===0 ? <Spinner/> : error ? <Err msg={error} onRetry={() => load(1)}/> :
        orders.length===0 ? (
          <div style={{ textAlign:"center", padding:"60px 0", color:T.text2 }}>
            <div style={{ fontSize:32, marginBottom:12 }}>📋</div>
            <div style={{ fontSize:14 }}>No orders yet</div>
          </div>
        ) : (
          <>
            {orders.map(o => (
              <Card key={o.id} onClick={() => setSelected(o.id)} style={{ cursor:"pointer" }}>
                <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:6 }}>
                  <div style={{ fontSize:12, color:T.text2, fontFamily:"monospace" }}>{o.public_ref}</div>
                  <StatusBadge status={o.status}/>
                </div>
                <div style={{ fontSize:14, color:T.text0, marginBottom:4 }}>{o.service_name}</div>
                <div style={{ display:"flex", justifyContent:"space-between" }}>
                  <span style={{ fontSize:12, color:T.text1 }}>{Number(o.quantity).toLocaleString()} units</span>
                  <AmountTag value={parseFloat(o.price_charged)}/>
                </div>
              </Card>
            ))}
            {loading && <Spinner/>}
          </>
        )
      }
    </div>
  );
}

function OrderDetailScreen({ orderId, onBack }) {
  const [order, setOrder] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.orderDetail(orderId).then(data => { setOrder(data); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [orderId]);

  return (
    <div style={{ padding:"12px 16px", paddingBottom:80 }}>
      <button onClick={onBack} style={{ background:"none", border:"none",
        color:T.text1, fontSize:14, cursor:"pointer", padding:"0 0 12px" }}>← Back</button>
      {loading ? <Spinner/> : error ? <Err msg={error} onRetry={onBack}/> : !order ? null : (
        <>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:16 }}>
            <div style={{ fontSize:16, fontWeight:700, color:T.text0 }}>{order.service_name}</div>
            <StatusBadge status={order.status}/>
          </div>
          <Card>
            <Row label="Ref" value={order.public_ref} muted/>
            <Row label="Quantity" value={Number(order.quantity).toLocaleString()}/>
            <Row label="Price" value={`₹${parseFloat(order.price_charged).toFixed(2)}`}/>
            <Row label="Source" value={order.source}/>
            {order.remains != null && <Row label="Remains" value={Number(order.remains).toLocaleString()}/>}
            {order.start_count != null && <Row label="Start count" value={Number(order.start_count).toLocaleString()}/>}
          </Card>
          {order.link && (
            <Card>
              <div style={{ fontSize:12, color:T.text2, marginBottom:4 }}>Link</div>
              <div style={{ fontSize:13, color:T.text1, wordBreak:"break-all" }}>{order.link}</div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

// ── Wallet / Deposit ──────────────────────────────────────────────────────────
function WalletScreen({ wallet, onRefresh }) {
  const [amount, setAmount] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);
  const presets = [100, 200, 500, 1000, 2000, 5000];

  const deposit = async () => {
    const amt = parseFloat(amount);
    if (!amt || amt < 10) { setError("Minimum deposit is ₹10"); return; }
    if (amt > 100000) { setError("Maximum deposit is ₹1,00,000"); return; }
    setSubmitting(true); setError(null);
    try {
      const data = await api.deposit({ amount: amt, provider: "razorpay" });
      // Open Razorpay checkout
      if (data.payment_link) window.open(data.payment_link, "_blank");
      setSuccess(true);
      onRefresh();
    } catch(e) { setError(e.message); }
    finally { setSubmitting(false); }
  };

  return (
    <div style={{ padding:"0 16px" }}>
      <div style={{ padding:"16px 0 4px", fontSize:16, fontWeight:700, color:T.text0 }}>Wallet</div>

      {/* Balance */}
      <Card accent style={{ marginBottom:16 }}>
        <div style={{ fontSize:12, color:T.text2, marginBottom:4 }}>Available balance</div>
        <AmountTag value={parseFloat(wallet?.balance||0).toFixed(2)} large/>
        <div style={{ fontSize:11, color:T.text2, marginTop:4 }}>{wallet?.currency || "INR"}</div>
      </Card>

      {/* Deposit */}
      <div style={{ fontSize:14, fontWeight:600, color:T.text0, marginBottom:12 }}>Add funds</div>

      {/* Preset amounts */}
      <div style={{ display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:8, marginBottom:12 }}>
        {presets.map(p => (
          <button key={p} onClick={() => setAmount(String(p))}
            style={{
              padding:"10px 0", borderRadius:8, border:"none",
              background: amount===String(p) ? T.accentDim : T.bg2,
              color: amount===String(p) ? T.accent : T.text1,
              fontSize:14, fontWeight:600, cursor:"pointer",
            }}>
            ₹{p.toLocaleString()}
          </button>
        ))}
      </div>

      <div style={{ marginBottom:12 }}>
        <Input value={amount} onChange={setAmount} placeholder="Custom amount" type="number"/>
      </div>

      {success && (
        <div style={{ fontSize:13, color:T.success, padding:"10px 12px",
          background:"#14532D20", borderRadius:8, marginBottom:12 }}>
          Payment initiated. Your balance will update once confirmed.
        </div>
      )}
      {error && <div style={{ fontSize:13, color:T.danger, marginBottom:12 }}>{error}</div>}

      <Btn onClick={deposit} disabled={submitting}>
        {submitting ? "Opening checkout…" : "Deposit via Razorpay"}
      </Btn>
    </div>
  );
}

// ── Profile ───────────────────────────────────────────────────────────────────
function ProfileScreen({ wallet }) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.profile().then(data => { setProfile(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  const stats = [
    { label:"Total orders",    value: profile?.total_orders ?? "—" },
    { label:"Balance",         value: `₹${parseFloat(wallet?.balance||0).toFixed(2)}` },
    { label:"Member since",    value: profile?.member_since ? new Date(profile.member_since).toLocaleDateString("en-IN",{month:"short",year:"numeric"}) : "—" },
  ];

  return (
    <div style={{ padding:"0 16px", paddingBottom:80 }}>
      <div style={{ padding:"24px 0 16px", textAlign:"center" }}>
        <div style={{
          width:64, height:64, borderRadius:"50%",
          background: T.accentDim, display:"flex", alignItems:"center",
          justifyContent:"center", fontSize:24, margin:"0 auto 12px",
        }}>
          {tgUser.first_name?.[0]?.toUpperCase() || "U"}
        </div>
        <div style={{ fontSize:18, fontWeight:700, color:T.text0 }}>{tgUser.first_name}</div>
        {tgUser.username && <div style={{ fontSize:13, color:T.text2 }}>@{tgUser.username}</div>}
        {profile?.is_premium && <div style={{ marginTop:6 }}><Badge label="⭐ Premium"/></div>}
      </div>

      {loading ? <Spinner/> : (
        <Card>
          {stats.map(s => <Row key={s.label} label={s.label} value={s.value}/>)}
        </Card>
      )}

      <div style={{ padding:"8px 0", fontSize:11, color:T.text2, textAlign:"center" }}>
        User ID: {tgUser.id}
      </div>
    </div>
  );
}

// ── Tab nav ───────────────────────────────────────────────────────────────────
const TABS = [
  { id:"catalog", label:"Services",  icon:"🛒" },
  { id:"orders",  label:"Orders",    icon:"📋" },
  { id:"wallet",  label:"Wallet",    icon:"💳" },
  { id:"profile", label:"Profile",   icon:"👤" },
];

// ── Root app ──────────────────────────────────────────────────────────────────
export default function App() {
  const [authed, setAuthed] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [tab, setTab] = useState("catalog");
  const [wallet, setWallet] = useState(null);
  const [selectedService, setSelectedService] = useState(null);

  // Auth on mount
  useEffect(() => {
    if (!initData) { setAuthed(true); return; }   // dev mode
    api.auth(initData).then(data => {
      _sessionToken = data.session_token;
      setAuthed(true);
      return api.wallet();
    }).then(w => setWallet(w))
      .catch(e => setAuthError(e.message));
  }, []);

  const refreshWallet = useCallback(() => {
    api.wallet().then(setWallet).catch(() => {});
  }, []);

  if (authError) return (
    <div style={{ background:T.bg0, minHeight:"100vh", display:"flex",
                  alignItems:"center", justifyContent:"center", padding:24 }}>
      <Err msg={authError}/>
    </div>
  );

  if (!authed) return (
    <div style={{ background:T.bg0, minHeight:"100vh", display:"flex",
                  alignItems:"center", justifyContent:"center" }}>
      <Spinner/>
    </div>
  );

  return (
    <div style={{ background:T.bg0, minHeight:"100vh", fontFamily:"Inter, system-ui, sans-serif",
                  color:T.text0, maxWidth:480, margin:"0 auto", position:"relative" }}>
      <style>{`
        * { -webkit-tap-highlight-color: transparent; }
        html,body { margin:0; padding:0; background:${T.bg0}; }
        input::placeholder { color:${T.text2}; }
        ::-webkit-scrollbar { display:none; }
      `}</style>

      {/* Screen content */}
      <div style={{ paddingBottom:72 }}>
        {selectedService ? (
          <ServiceDetailScreen
            publicId={selectedService}
            wallet={wallet}
            onBack={() => setSelectedService(null)}
            onOrderPlaced={(ref) => { setSelectedService(null); setTab("orders"); refreshWallet(); }}
          />
        ) : tab === "catalog" ? (
          <CatalogScreen onSelectService={(id) => setSelectedService(id)}/>
        ) : tab === "orders" ? (
          <OrdersScreen/>
        ) : tab === "wallet" ? (
          <WalletScreen wallet={wallet} onRefresh={refreshWallet}/>
        ) : (
          <ProfileScreen wallet={wallet}/>
        )}
      </div>

      {/* Bottom tab bar */}
      {!selectedService && (
        <div style={{
          position:"fixed", bottom:0, left:"50%", transform:"translateX(-50%)",
          width:"100%", maxWidth:480,
          background:T.bg1, borderTop:`1px solid ${T.border}`,
          display:"grid", gridTemplateColumns:"repeat(4,1fr)",
          paddingBottom: "env(safe-area-inset-bottom, 0px)",
          zIndex:100,
        }}>
          {TABS.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              style={{
                background:"none", border:"none", cursor:"pointer",
                padding:"10px 0 8px", display:"flex", flexDirection:"column",
                alignItems:"center", gap:3,
              }}>
              <span style={{ fontSize:20 }}>{t.icon}</span>
              <span style={{ fontSize:10, fontWeight:600,
                color: tab===t.id ? T.accent : T.text2 }}>
                {t.label}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
