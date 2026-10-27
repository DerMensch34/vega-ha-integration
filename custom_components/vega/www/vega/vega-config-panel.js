/* Device configuration page: /vega-config/<config_entry_id>.
 * Resolve the editor through HA's entity registry, never by a guessed entity ID
 * or by picking the first available tracker. Reads/writes remain user actions.
 */
const editorUrl = new URL("./vega-can-editor.js", import.meta.url);
editorUrl.search = new URL(import.meta.url).search;
await import(editorUrl.href);

class VegaConfigPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._request = 0;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; height:100%; overflow:auto; color:var(--primary-text-color); background:var(--primary-background-color); }
        header { padding:16px 24px; border-bottom:1px solid var(--divider-color); background:var(--app-header-background-color, var(--card-background-color)); color:var(--app-header-text-color, var(--primary-text-color)); }
        a { color:inherit; text-decoration:none; display:inline-block; padding:6px 0; }
        h1 { font-size:22px; font-weight:500; margin:8px 0 0; }
        main { max-width:1000px; margin:0 auto; padding:20px; }
        #status { white-space:pre-wrap; color:var(--secondary-text-color); }
        #status.error { color:var(--error-color); }
        button { margin:8px 0 16px; padding:10px 16px; border:0; border-radius:8px; cursor:pointer; color:var(--text-primary-color, white); background:var(--primary-color); }
        [hidden] { display:none !important; }
        @media(max-width:600px) { header { padding:12px 16px; } main { padding:12px 8px; } h1 { font-size:20px; } }
      </style>
      <header><a id="back" href="/config/devices/dashboard">← Устройства</a><h1>Команды и сценарии Vega</h1></header>
      <main><p id="status" role="status">Выберите устройство Vega на странице устройств.</p>
        <button id="retry" hidden>Обновить</button><div id="editor"></div></main>`;
    this.shadowRoot.querySelector("#retry").addEventListener("click", () => {
      this._attempted = false;
      this._resolve();
    });
    // HA can create this element and assign its properties before the module
    // finishes loading. Replay those own properties through the new setters.
    for (const key of ["route", "hass"]) {
      if (Object.prototype.hasOwnProperty.call(this, key)) {
        const value = this[key];
        delete this[key];
        this[key] = value;
      }
    }
  }

  set route(route) {
    let entry = null;
    try {
      const path = (route?.path || "").replace(/\/$/, "");
      if (/^\/[A-Za-z0-9_-]+$/.test(path)) entry = path.slice(1);
    } catch (_) { /* An invalid route must never select another tracker. */ }
    if (entry === this._entryId) return;
    this._entryId = entry;
    this._request++;
    this._attempted = false;
    this._row = null;
    this._editor = null;
    this.shadowRoot.querySelector("#editor").replaceChildren();
    const back = this.shadowRoot.querySelector("#back");
    back.href = "/config/devices/dashboard";
    back.textContent = "← Устройства";
    this._resolve();
  }

  set hass(hass) {
    this._hass = hass;
    this._resolve();
    this._updateEditor();
  }

  _message(text, retry = false, error = false) {
    const status = this.shadowRoot.querySelector("#status");
    status.textContent = text;
    status.hidden = !text;
    status.className = error ? "error" : "";
    this.shadowRoot.querySelector("#retry").hidden = !retry;
  }

  async _resolve() {
    if (!this._hass) return;
    if (!this._entryId) {
      this._message("Откройте редактор по ссылке конфигурации на устройстве Vega.");
      return;
    }
    if (this._attempted) return;
    this._attempted = true;
    this._row = null;
    const request = ++this._request;
    const entry = this._entryId;
    this._message("Загружаю настройки устройства…");
    try {
      const entities = await this._hass.callWS({type: "config/entity_registry/list"});
      if (request !== this._request) return;
      const matches = entities.filter(e => e.platform === "vega" && e.config_entry_id === entry &&
        e.entity_id.startsWith("sensor.") && e.unique_id?.endsWith("_can_scripts"));
      if (matches.length !== 1) throw new Error(matches.length
        ? "Для этой интеграции найдено несколько редакторов. Проверьте устройства Vega."
        : "Сенсор команд этого устройства не найден. Проверьте, что интеграция Vega загружена.");
      const row = matches[0];
      if (row.disabled_by) throw new Error("Сенсор CAN Scripts отключён. Включите его на странице устройства.");
      this._row = row;
      if (row.device_id) {
        const back = this.shadowRoot.querySelector("#back");
        back.href = `/config/devices/device/${encodeURIComponent(row.device_id)}`;
        back.textContent = "← К устройству";
      }
      this._updateEditor();
    } catch (error) {
      if (request !== this._request) return;
      this.shadowRoot.querySelector("#editor").hidden = true;
      this._message(error.message || String(error), true, true);
    }
  }

  _updateEditor() {
    if (!this._row || !this._hass) return;
    const container = this.shadowRoot.querySelector("#editor");
    const state = this._hass.states[this._row.entity_id];
    if (!state || state.attributes.entry_id !== this._entryId || state.state === "unavailable") {
      container.hidden = true;
      this._message("Данные устройства пока недоступны. Проверьте интеграцию Vega или обновите список сущностей.", true);
      return;
    }
    if (!this._editor || this._editorEntity !== this._row.entity_id) {
      this._editor = document.createElement("vega-can-editor");
      this._editor.setConfig({entity: this._row.entity_id});
      this._editorEntity = this._row.entity_id;
      container.replaceChildren(this._editor);
    }
    container.hidden = false;
    this._message("");
    this._editor.hass = this._hass;
  }
}

if (!customElements.get("vega-config-panel")) customElements.define("vega-config-panel", VegaConfigPanel);
