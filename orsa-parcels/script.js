// Reads what the browser already knows. No network calls, nothing to configure.
(() => {
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };

  set("host", location.hostname || "localhost");
  set("tls", location.protocol === "https:" ? "on" : "off (plain HTTP)");
})();
