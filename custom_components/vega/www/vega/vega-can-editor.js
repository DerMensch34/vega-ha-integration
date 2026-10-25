/*
 * vega-can-editor
 *
 * Views and edits the tracker's CAN scripts (Setting 5502 slots) in the UI.
 * Reads the decoded slots from the `can_scripts` sensor's `slots` attribute.
 * Shows an overview list of slots with CAN decoding; each script opens in a
 * dedicated modal dialog for detailed step-by-step inspection and editing.
 * Includes built-in CAN decoder with real-time feedback.
 *
 * Config:
 *   type: custom:vega-can-editor
 *   entity: sensor.vega_<imei>_can_scripts
 */

function explainCanFrame(bus, canId, dataHex) {
  const d = (dataHex || "").toLowerCase().replace(/\s+/g, "");
  const cid = typeof canId === "string" ? (canId.startsWith("0x") ? parseInt(canId, 16) : parseInt(canId, 10)) : (canId || 0);
  const pfx = "0x" + cid.toString(16).toUpperCase();
  const busLabel = bus === 1 ? "Body" : (bus === 2 ? "EV/Diag" : "Bus " + bus);

  if (cid === 0x701) {
    if (d.startsWith("03220203aa21")) return `UDS (${pfx}, ${busLabel}): Постановка на охрану (закрыть ЦЗ)`;
    if (d.startsWith("032202030000")) return `UDS (${pfx}, ${busLabel}): Снятие с охраны (открыть ЦЗ)`;
    if (d.startsWith("03220203")) return `UDS (${pfx}, ${busLabel}): Запрос состояния/управления ЦЗ (DID 0x0203)`;
    if (d.startsWith("062f010303080055")) return `UDS (${pfx}, ${busLabel}): Включение звукового сигнала (Horn ON)`;
    if (d.startsWith("062f010303000055")) return `UDS (${pfx}, ${busLabel}): Выключение звукового сигнала (Horn OFF)`;
    if (d.startsWith("062f0103")) return `UDS (${pfx}, ${busLabel}): Управление клаксоном (IOCBI DID 0x0103)`;
    return `UDS Diag (${pfx}, ${busLabel}): Диагностический запрос`;
  }
  if (cid === 0x011) {
    const parts = [];
    if (d.startsWith("04")) parts.push("сигнал клаксона");
    if (d.length >= 6) {
      const b12 = d.slice(2, 6);
      if (b12 === "440a") parts.push("включение охлаждения (A/C ON)");
      else if (b12 === "580a") parts.push("климат Auto 22°C");
      else if (b12 === "800a") {
        const t = d.length >= 8 ? parseInt(d.slice(6, 8), 16) : 0;
        parts.push(`максимальный прогрев ${t ? t + "°C " : ""}(Heating ON)`);
      } else if (b12 === "0009") parts.push("сброс триггера команды (снятие импульса / idle)");
    }
    if (d.length >= 8 && d.slice(6, 8) === "5b") parts.push("скорость вентилятора климата");
    if (d.length >= 12) {
      const b45 = d.slice(8, 12);
      if (b45 === "aa21") parts.push("закрытие центрального замка (Lock)");
      else if (b45 === "0002") parts.push("открытие центрального замка (Unlock)");
    }
    if (d.length >= 12 && d.slice(10, 12) === "05") parts.push("открытие замка багажника");
    if (parts.length) return `BCM (${pfx}, ${busLabel}): ${parts.join(", ")}`;
    return `BCM (${pfx}, ${busLabel}): Команда кузовной электроники`;
  }
  if (cid === 0x092) {
    if (d.length >= 6 && d.slice(4, 6) === "60") return `VCU (${pfx}, ${busLabel}): Импульс пробуждения зажигания (Remote ON wake-up)`;
    return `VCU (${pfx}, ${busLabel}): Управление питанием / пробуждение`;
  }
  if (cid === 0x093) {
    if (d.startsWith("24c3ccc0")) return `HVAC (${pfx}, ${busLabel}): Уставка климата 22°C Auto`;
    return `HVAC (${pfx}, ${busLabel}): Конфигурация климат-контроля`;
  }
  if (cid === 0x09a) {
    if (d.startsWith("10")) return `Tailgate (${pfx}, ${busLabel}): Открытие электропривода багажника`;
    if (d.startsWith("00")) return `Tailgate (${pfx}, ${busLabel}): Закрытие электропривода багажника`;
    return `Tailgate (${pfx}, ${busLabel}): Модуль двери багажника`;
  }
  if (cid === 0x394) {
    if (d.startsWith("fcff0fff")) return `HVAC (${pfx}, ${busLabel}): Запрос максимального прогрева (HI / 32°C)`;
    if (d.startsWith("7cff0fff")) return `HVAC (${pfx}, ${busLabel}): Запрос поддержания 22°C (Auto)`;
    if (d.startsWith("4100")) return `HVAC (${pfx}, ${busLabel}): Запрос кондиционирования (Cooling LO)`;
    return `HVAC (${pfx}, ${busLabel}): Управление целевым режимом климата`;
  }
  return `CAN (${pfx}, ${busLabel}): ${d}`;
}

function explainCanScript(script) {
  const frames = script.frames || [];
  if (!frames.length) return "Пустой скрипт";
  const allHex = frames.map(f => (f.data || "").toLowerCase().replace(/\s+/g, ""));
  const canIds = new Set(frames.map(f => typeof f.can_id === "string" ? (f.can_id.startsWith("0x") ? parseInt(f.can_id, 16) : parseInt(f.can_id, 10)) : (f.can_id || 0)));

  const hasRemoteOn = allHex.some(d => d.length >= 6 && d.slice(4, 6) === "60");
  const hasPreheat = allHex.some(d => d.startsWith("fcff0fff") || (d.length >= 6 && d.slice(2, 6) === "800a"));
  const hasCooling = allHex.some(d => d.startsWith("4100") || (d.length >= 6 && d.slice(2, 6) === "440a"));
  const hasAuto22 = allHex.some(d => d.startsWith("7cff0fff") || d.startsWith("24c3ccc0") || (d.length >= 6 && d.slice(2, 6) === "580a"));
  const hasLock = allHex.some(d => d.startsWith("03220203aa21") || (d.length >= 12 && d.slice(8, 12) === "aa21"));
  const hasUnlock = allHex.some(d => d.startsWith("032202030000") || (d.length >= 12 && d.slice(8, 12) === "0002"));
  const hasHorn = allHex.some(d => d.startsWith("062f0103") || d.startsWith("04"));
  const hasTrunkOpen = allHex.some(d => d.startsWith("1000ffff") || (d.length >= 12 && d.slice(10, 12) === "05"));
  const hasTrunkClose = allHex.some(d => d.startsWith("0000ffff"));
  const hasRemoteOff = allHex.some(d => d.length >= 6 && d.slice(2, 6) === "0009");

  if (hasRemoteOn && hasPreheat) return "Дистанционный запуск и максимальный прогрев (VCU + HVAC + BCM)";
  if (hasRemoteOn) return "Дистанционный запуск зажигания (VCU + BCM)";
  if (hasCooling) return "Включение охлаждения салона (HVAC + BCM)";
  if (hasAuto22) return "Включение климат-контроля 22°C Auto (HVAC + BCM)";
  if (hasPreheat) return "Включение максимального прогрева салона (HVAC + BCM)";
  if (hasLock) return "Закрытие центрального замка / постановка на охрану (UDS + BCM)";
  if (hasUnlock) return "Открытие центрального замка / снятие с охраны (UDS + BCM)";
  if (hasHorn) return "Звуковой сигнал клаксона (UDS + BCM)";
  if (hasTrunkOpen) return "Открытие багажника (BCM + Tailgate)";
  if (hasTrunkClose) return "Закрытие багажника (Tailgate)";
  if (hasRemoteOff) return "Выключение дистанционного запуска / климата (BCM)";

  const names = [];
  if (canIds.has(0x701)) names.push("UDS Diag");
  if (canIds.has(0x011)) names.push("BCM");
  if (canIds.has(0x092)) names.push("VCU");
  if (canIds.has(0x093) || canIds.has(0x394)) names.push("HVAC");
  if (canIds.has(0x09a)) names.push("Tailgate");
  if (names.length) return "Команды в блоки: " + names.join(", ");
  return `Пользовательский скрипт (${frames.length} кадр.)`;
}

class VegaCanEditor extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("vega-can-editor: 'entity' is required");
    }
    this._config = config;
    this._rendered = false;
    this._activeSlot = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) this._build();
    this._update();
  }

  getCardSize() {
    return 6;
  }

  _slots() {
    const st = this._hass && this._hass.states[this._config.entity];
    return (st && st.attributes && st.attributes.slots) || [];
  }

  _build() {
    this.innerHTML = `
      <ha-card header="CAN-скрипты (Setting 5502)">
        <style>
          .wrap { padding: 8px 16px 16px; }
          .bar { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 16px; flex-wrap:wrap; }
          .slot-table { width:100%; border-collapse:collapse; margin-top:6px; }
          .slot-row { border-top:1px solid var(--divider-color, rgba(255,255,255,0.1)); cursor:pointer; transition:background 0.15s; }
          .slot-row:hover { background:var(--table-row-alternative-background-color, rgba(255,255,255,0.04)); }
          .slot-cell { padding:10px 8px; vertical-align:middle; }
          .slot-badge { display:inline-block; font-size:11px; font-weight:600; padding:2px 8px; border-radius:12px;
                        background:var(--secondary-background-color, rgba(255,255,255,0.1));
                        color:var(--primary-text-color, #fff); }
          .slot-title { font-weight:600; font-size:14px; margin-bottom:2px; }
          .slot-summary { font-size:12px; color:var(--secondary-text-color, #888); }
          .btn { cursor:pointer; border:none; border-radius:8px; padding:7px 12px; font-weight:600;
                 background:var(--primary-color, #03a9f4); color:#fff; font-size:13px; }
          .btn.sec { background:var(--secondary-background-color, rgba(255,255,255,0.1));
                     color:var(--primary-text-color, #fff); }
          .btn.sec:hover { background:var(--divider-color, rgba(255,255,255,0.2)); }
          .hint { font-size:12px; color:var(--secondary-text-color, #888); }

          /* Modal Window Styles */
          .modal-backdrop {
            position: fixed; inset: 0; background: rgba(0, 0, 0, 0.65);
            backdrop-filter: blur(3px); z-index: 99999; display: flex;
            align-items: center; justify-content: center; padding: 16px;
            box-sizing: border-box;
          }
          .modal-box {
            background: var(--ha-card-background, var(--card-background-color, #1c1c1e));
            color: var(--primary-text-color, #fff); border-radius: 14px;
            width: 100%; max-width: 660px; max-height: 88vh; display: flex;
            flex-direction: column; box-shadow: 0 16px 36px rgba(0,0,0,0.5);
            border: 1px solid var(--divider-color, rgba(255,255,255,0.12));
            overflow: hidden; animation: popIn 0.2s ease-out;
          }
          @keyframes popIn {
            from { transform: scale(0.95); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
          }
          .modal-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 18px; border-bottom: 1px solid var(--divider-color, rgba(255,255,255,0.1));
          }
          .modal-title { font-size: 17px; font-weight: 600; }
          .modal-close {
            background: transparent; border: none; font-size: 22px; cursor: pointer;
            color: var(--secondary-text-color, #aaa); line-height: 1; padding: 0 6px;
          }
          .modal-close:hover { color: var(--primary-text-color, #fff); }
          .modal-body {
            padding: 16px 18px; overflow-y: auto; flex: 1; display: flex;
            flex-direction: column; gap: 14px;
          }
          .section-title { font-size: 13px; font-weight: 600; text-transform: uppercase;
                           letter-spacing: 0.5px; color: var(--secondary-text-color, #888); }
          .summary-box {
            background: var(--secondary-background-color, rgba(255,255,255,0.05));
            border-left: 4px solid var(--primary-color, #03a9f4);
            padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13px; font-weight: 500;
          }
          .frames-list { display: flex; flex-direction: column; gap: 8px; }
          .frame-card {
            background: var(--secondary-background-color, rgba(255,255,255,0.04));
            border: 1px solid var(--divider-color, rgba(255,255,255,0.08));
            border-radius: 8px; padding: 10px 12px;
          }
          .frame-header {
            display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px;
          }
          .tag {
            font-size: 11px; padding: 2px 6px; border-radius: 4px; font-family: monospace;
            background: var(--divider-color, rgba(255,255,255,0.12));
          }
          .frame-action {
            font-size: 13px; font-weight: 600; color: var(--primary-color, #03a9f4);
            margin-bottom: 3px;
          }
          .frame-hex { font-family: monospace; font-size: 11px; color: var(--secondary-text-color, #888); }
          .json-area {
            width: 100%; box-sizing: border-box; min-height: 130px; font-family: monospace;
            font-size: 12px; background: var(--card-background-color, #111);
            color: var(--primary-text-color, #eee); border: 1px solid var(--divider-color, rgba(255,255,255,0.15));
            border-radius: 8px; padding: 8px; resize: vertical;
          }
          .modal-footer {
            display: flex; justify-content: flex-end; gap: 10px; padding: 12px 18px;
            border-top: 1px solid var(--divider-color, rgba(255,255,255,0.1));
            background: var(--secondary-background-color, rgba(255,255,255,0.02));
          }
        </style>
        <div class="bar">
          <button class="btn sec" id="read">Прочитать из трекера</button>
          <span class="hint">Запись требует включённого Direct Control.</span>
        </div>
        <div class="wrap" id="wrap"></div>
        <div id="modal-container"></div>
      </ha-card>
    `;
    this.querySelector("#read").addEventListener("click", () => {
      this._hass.callService("vega", "read_can_scripts", {});
      this._toast("Чтение слотов из трекера…");
    });
    this._wrap = this.querySelector("#wrap");
    this._modalContainer = this.querySelector("#modal-container");
    this._rendered = true;
  }

  _update() {
    if (!this._rendered) return;
    const slots = this._slots();
    const sig = slots.map((s) => `${s.slot}:${s.name}:${(s.frames||[]).length}`).join(",");
    if (sig === this._sig && !this._activeSlot) return;
    this._sig = sig;

    this._wrap.innerHTML = "";
    if (!slots.length) {
      this._wrap.innerHTML = `<div class="hint" style="padding:12px 0;">Нет данных. Нажмите «Прочитать из трекера».</div>`;
      return;
    }

    const table = document.createElement("table");
    table.className = "slot-table";
    for (const s of slots) {
      const tr = document.createElement("tr");
      tr.className = "slot-row";
      const frameCount = (s.frames || []).length;
      const summaryText = explainCanScript(s);
      tr.innerHTML = `
        <td class="slot-cell" style="width:70px;">
          <span class="slot-badge">Слот ${s.slot}</span>
        </td>
        <td class="slot-cell">
          <div class="slot-title">${s.name || "(без имени)"}</div>
          <div class="slot-summary">${summaryText}</div>
        </td>
        <td class="slot-cell" style="text-align:right; width:130px;">
          <button class="btn sec open-btn">Просмотр</button>
        </td>
      `;
      tr.querySelector(".open-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        this._openModal(s);
      });
      tr.addEventListener("click", () => this._openModal(s));
      table.appendChild(tr);
    }
    this._wrap.appendChild(table);

    // If modal is open for a slot, re-render it with fresh data
    if (this._activeSlot !== null) {
      const fresh = slots.find((s) => s.slot === this._activeSlot);
      if (fresh) this._renderModalContent(fresh);
    }
  }

  _openModal(slotData) {
    this._activeSlot = slotData.slot;
    this._modalContainer.innerHTML = `
      <div class="modal-backdrop" id="backdrop">
        <div class="modal-box" id="box">
          <div class="modal-header">
            <span class="modal-title">Слот ${slotData.slot}: ${slotData.name || "(без имени)"}</span>
            <button class="modal-close" id="close-x">&times;</button>
          </div>
          <div class="modal-body" id="modal-body"></div>
          <div class="modal-footer">
            <button class="btn sec" id="close-btn">Закрыть</button>
            <button class="btn" id="save-btn">Записать в трекер</button>
          </div>
        </div>
      </div>
    `;

    const close = () => {
      this._activeSlot = null;
      this._modalContainer.innerHTML = "";
    };

    this.querySelector("#close-x").addEventListener("click", close);
    this.querySelector("#close-btn").addEventListener("click", close);
    this.querySelector("#backdrop").addEventListener("click", (e) => {
      if (e.target.id === "backdrop") close();
    });

    this._renderModalContent(slotData);

    const ta = this.querySelector("#slot-json");
    if (ta) {
      ta.addEventListener("input", () => {
        try {
          const parsed = JSON.parse(ta.value);
          this._renderDecodedSections(parsed);
        } catch (_) {}
      });
    }

    this.querySelector("#save-btn").addEventListener("click", () => {
      const taCurrent = this.querySelector("#slot-json");
      if (taCurrent) this._save(slotData.slot, taCurrent.value);
    });
  }

  _renderDecodedSections(s) {
    const summaryEl = this.querySelector("#summary-target");
    const framesEl = this.querySelector("#frames-target");
    if (!summaryEl || !framesEl) return;

    summaryEl.textContent = `🎯 ${s.summary || explainCanScript(s)}`;

    const frames = s.frames || [];
    if (!frames.length) {
      framesEl.innerHTML = `<div class="hint">В этом слоте нет CAN-кадров.</div>`;
      return;
    }
    framesEl.innerHTML = frames
      .map((f, i) => {
        const busNum = f.bus || 0;
        const busName = busNum === 1 ? "Body (1)" : busNum === 2 ? "EV/Diag (2)" : `Bus ${busNum}`;
        const canIdNum = typeof f.can_id === "string" ? (f.can_id.startsWith("0x") ? parseInt(f.can_id, 16) : parseInt(f.can_id, 10)) : (f.can_id || 0);
        const canIdHex = `0x${canIdNum.toString(16).toUpperCase()}`;
        const actionDesc = explainCanFrame(busNum, canIdNum, f.data);
        return `
          <div class="frame-card">
            <div class="frame-header">
              <span class="tag">Шаг ${i + 1}</span>
              <span class="tag">${canIdHex}</span>
              <span class="tag">${busName}</span>
              <span class="tag">${f.delay_ms || 0} мс</span>
            </div>
            <div class="frame-action">📌 ${actionDesc}</div>
            <div class="frame-hex">DATA: ${f.data || "0000000000000000"}</div>
          </div>
        `;
      })
      .join("");
  }

  _renderModalContent(s) {
    const body = this.querySelector("#modal-body");
    if (!body) return;

    const jsonSpec = {
      name: s.name,
      repeat: s.repeat || 1,
      frames: (s.frames || []).map((f) => ({
        delay_ms: f.delay_ms,
        can_id: f.can_id,
        bus: f.bus,
        data: f.data,
      })),
    };

    body.innerHTML = `
      <div class="section-title">Расшифровка действия</div>
      <div class="summary-box" id="summary-target">🎯 ${explainCanScript(s)}</div>

      <div class="section-title" style="margin-top:6px;">Последовательность кадров</div>
      <div class="frames-list" id="frames-target"></div>

      <div class="section-title" style="margin-top:8px;">Редактор (JSON)</div>
      <div class="hint">Формат: name, repeat, frames: [{delay_ms, can_id, bus, data}]. При правке расшифровка обновляется на лету.</div>
      <textarea class="json-area" id="slot-json">${JSON.stringify(jsonSpec, null, 2)}</textarea>
    `;

    this._renderDecodedSections(s);
  }

  async _save(slot, text) {
    let script;
    try {
      script = JSON.parse(text);
    } catch (e) {
      this._toast(`Слот ${slot}: неверный JSON — ${e.message}`);
      return;
    }
    try {
      await this._hass.callService("vega", "write_can_script", { slot, script });
      this._toast(`Слот ${slot}: записано в трекер`);
      this._activeSlot = null;
      this._modalContainer.innerHTML = "";
    } catch (e) {
      this._toast(`Слот ${slot}: ошибка записи — ${(e && e.message) || e}`);
    }
  }

  _toast(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", { bubbles: true, composed: true, detail: { message } })
    );
  }
}

customElements.define("vega-can-editor", VegaCanEditor);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "vega-can-editor",
  name: "Vega CAN Editor",
  description: "Просмотр и редактирование CAN-скриптов трекера (Setting 5502) в модальном окне с встроенным декодером",
});
console.info("%c VEGA-CAN-EDITOR v0.2.2 %c loaded ", "background:#00c0fc;color:#fff", "");
