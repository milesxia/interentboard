(() => {
  "use strict";

  const getJSON = async (url) => {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  };

  window.addEventListener("DOMContentLoaded", async () => {
    let frontend = { build_sha: "unknown", build_time: "unknown" };
    let backend = { build_sha: "unknown", build_time: "unknown" };
    try { frontend = await getJSON("/build.json"); } catch (e) { console.warn(e); }
    try { backend = await getJSON("/api/build"); } catch (e) { console.warn(e); }

    const f = String(frontend.build_sha || "unknown");
    const b = String(backend.build_sha || "unknown");
    const known = f !== "unknown" && b !== "unknown";
    const mismatch = known && f !== b;

    const badge = document.createElement("div");
    badge.id = "internetboard-build-badge";
    badge.style.cssText = [
      "position:fixed", "right:8px", "bottom:6px", "z-index:2147483647",
      "font:10px/1.2 monospace", "padding:3px 6px", "border-radius:4px",
      "background:rgba(0,0,0,.65)", "color:#fff", "opacity:.75",
      "pointer-events:none"
    ].join(";");
    badge.textContent = mismatch
      ? `BUILD MISMATCH FE:${f.slice(0,7)} BE:${b.slice(0,7)}`
      : `BUILD ${f.slice(0,7)} / ${b.slice(0,7)}`;
    if (mismatch) {
      badge.style.background = "rgba(160,0,0,.9)";
      badge.style.opacity = "1";
      console.error("InternetBoard frontend/backend build mismatch", { frontend, backend });
    } else {
      console.info("InternetBoard build", { frontend, backend });
    }
    document.body.appendChild(badge);
    window.INTERNETBOARD_BUILD = { frontend, backend, mismatch };
  });
})();
