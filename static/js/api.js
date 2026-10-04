/* 轻量 API 封装 + 全局加载态 */
const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
  async post(path) {
    const r = await fetch(path, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
  overview: () => API.get("/api/overview"),
  map: () => API.get("/api/map"),
  rainEvents: () => API.get("/api/rain-events"),
  rainEvent: (id) => API.get(`/api/rain-events/${id}`),
  reservoirs: () => API.get("/api/reservoirs"),
  warnings: (scope = "active") => API.get(`/api/warnings?scope=${scope}`),
  evacuations: (activeOnly = true) => API.get(`/api/evacuations?active_only=${activeOnly}`),
  forecast: (eid, mode, { force = false, idemKey = "" } = {}) => {
    const qs = force ? "?force=true" : "";
    const headers = {};
    if (idemKey) headers["Idempotency-Key"] = idemKey;
    return fetch(`/api/forecast/${eid}/${mode}${qs}`, { method: "POST", headers })
      .then(async (r) => {
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          const err = new Error(body.detail || `HTTP ${r.status}`);
          err.status = r.status;
          throw err;
        }
        return r.json();
      });
  },
  forecastRuns: () => API.get("/api/forecast/runs"),
  forecastSeries: (runId) => API.get(`/api/forecast/series/${runId}`),
};

/* 全局运行状态：跨视图共享最近一次预报结果 / 运行记录 */
const store = {
  lastForecast: null,        // {run_id, ...完整预报结果}
  runs: [],                  // [{id, event_id, mode, created_at}]
  mapData: null,
  overview: null,
};

const fmt = {
  num(v, d = 1) { return (v == null ? "—" : Number(v).toFixed(d)); },
  lvName(lv) { return { red: "红色预警", orange: "橙色预警", yellow: "黄色预警", blue: "蓝色预警", "": "" }[lv] || lv; },
  lvClass(lv) { return { red: "lv-4", orange: "lv-3", yellow: "lv-2", blue: "lv-1" }[lv] || ""; },
  lvBadge(lv) { return { red: "red", orange: "orange", yellow: "yellow", blue: "blue" }[lv] || "gray"; },
  hour(h) { return `${String(h).padStart(2, "0")}:00`; },
  time(t) { if (!t) return "—"; const d = new Date(t); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; },
};

// 普通 script 的顶层 const 不会挂到 window，显式导出供视图/全局配置使用
window.store = store;
window.fmt = fmt;
window.API = API;
