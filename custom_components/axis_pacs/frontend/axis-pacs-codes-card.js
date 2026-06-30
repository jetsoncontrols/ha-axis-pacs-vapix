/*
 * Axis PACS Access Codes — a custom Lovelace card for managing the cluster-wide
 * cardholder / PIN / card database exposed by the `axis_pacs` integration.
 *
 * The integration exposes code management only as domain services that return
 * data in the service response (there are no entities holding the credential
 * list), so this bespoke card calls those services directly over the websocket
 * connection and renders an editable table + add form.
 *
 * Ships inside the integration and auto-registers (no HACS frontend entry / no
 * Lovelace resource). Vanilla custom element — no Lit, no build step.
 *
 * Card options (all optional):
 *   type: custom:axis-pacs-codes-card
 *   title: Access Codes          # card header
 *   entry_id: <config entry id>  # pin to a specific controller; default: auto
 *   allow_reveal: true           # set false to forbid revealing codes (kiosks)
 */

const DOMAIN = "axis_pacs";
const WS_MANAGERS = "axis_pacs/managers";

const KIND_LABEL = { card: "Card", pin: "PIN", both: "PIN + Card", none: "—" };

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

class AxisPacsCodesCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { title: "Access Codes", entry_id: null, allow_reveal: true };
    this._hass = null;
    this._loaded = false;
    this._s = {
      loading: true,
      busy: false,
      error: null,
      managers: null,
      manager: null,
      entryId: null,
      credentials: [],
      profiles: [],
      doors: [],
      schedules: [],
      revealed: false,
      addOpen: false,
      editToken: null,
      confirmDelete: null,
    };
    this._bound = false;
  }

  setConfig(config) {
    config = config || {};
    this._config = {
      // Explicit empty string hides the card's own header title (e.g. when the
      // card is wrapped in an expander that already shows "Access Codes"); the
      // tools row (Show codes / refresh) still renders.
      title: config.title === undefined ? "Access Codes" : config.title,
      entry_id: config.entry_id || null,
      allow_reveal: config.allow_reveal !== false,
    };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && !this._loaded) {
      this._loaded = true;
      this._init();
    }
  }

  getCardSize() {
    return 6;
  }

  get _isAdmin() {
    return !!(this._hass && this._hass.user && this._hass.user.is_admin);
  }

  // Whether the current user may manage codes: admins always; non-admins only
  // when the controller opts in (manage_allow_non_admin) — matched server-side.
  get _canManage() {
    if (this._isAdmin) return true;
    return !!(this._s.manager && this._s.manager.allow_non_admin);
  }

  // ---- data ------------------------------------------------------------- //
  async _init() {
    try {
      await this._resolveManager();
      if (this._s.entryId) {
        await this._loadProfiles();
        await this._loadDoors();
        await this._loadSchedules();
        await this._loadCredentials();
      }
    } catch (err) {
      this._s.error = this._errText(err);
    } finally {
      this._s.loading = false;
      this._render();
    }
  }

  async _resolveManager() {
    if (this._config.entry_id) {
      this._s.entryId = this._config.entry_id;
      return;
    }
    const res = await this._hass.connection.sendMessagePromise({ type: WS_MANAGERS });
    const managers = (res && res.managers) || [];
    this._s.managers = managers;
    if (managers.length === 0) {
      throw new Error(
        "No Axis PACS controller has code management enabled. Turn on " +
          "“Manage access codes” in the integration options on one controller."
      );
    }
    this._s.manager = managers[0];
    this._s.entryId = managers[0].entry_id;
  }

  async _loadProfiles() {
    const resp = await this._callResponse("list_access_profiles", {});
    this._s.profiles = (resp && resp.access_profiles) || [];
  }

  async _loadDoors() {
    const resp = await this._callResponse("list_doors", {});
    this._s.doors = (resp && resp.doors) || [];
  }

  async _loadSchedules() {
    const resp = await this._callResponse("list_schedules", {});
    this._s.schedules = (resp && resp.schedules) || [];
  }

  async _loadCredentials() {
    const resp = await this._callResponse("list_credentials", {
      include_pins: this._s.revealed,
    });
    const creds = (resp && resp.credentials) || [];
    creds.sort((a, b) =>
      (a.user_name || "￿").localeCompare(b.user_name || "￿")
    );
    this._s.credentials = creds;
  }

  _callResponse(service, data) {
    return this._hass.connection
      .sendMessagePromise({
        type: "call_service",
        domain: DOMAIN,
        service,
        service_data: { config_entry_id: this._s.entryId, ...data },
        return_response: true,
      })
      .then((r) => r.response);
  }

  _callService(service, data) {
    return this._hass.callService(DOMAIN, service, {
      config_entry_id: this._s.entryId,
      ...data,
    });
  }

  async _mutate(fn) {
    this._s.busy = true;
    this._s.error = null;
    this._render();
    try {
      await fn();
      await this._loadCredentials();
    } catch (err) {
      this._s.error = this._errText(err);
    } finally {
      this._s.busy = false;
      this._render();
    }
  }

  _errText(err) {
    if (!err) return "Unknown error";
    return err.message || err.error || String(err);
  }

  // ---- profile / door helpers ------------------------------------------ //
  _profilesByToken() {
    const map = {};
    for (const p of this._s.profiles) map[p.token] = p;
    return map;
  }

  _profileLabel(p) {
    if (p.doors && p.doors.length) return p.doors.map((d) => d.name).join(", ");
    return p.name || p.token;
  }

  _doorsForCredential(cred) {
    const byTok = this._profilesByToken();
    const names = [];
    for (const tok of cred.access_profile_tokens || []) {
      const p = byTok[tok];
      if (!p) {
        if (!names.includes("(unknown)")) names.push("(unknown)");
        continue;
      }
      for (const n of (p.doors && p.doors.length
        ? p.doors.map((d) => d.name)
        : [p.name || "(profile)"])) {
        if (!names.includes(n)) names.push(n);
      }
    }
    return names;
  }

  // ---- groups / individual-door access model ---------------------------- //
  // A "group" is any access profile that is NOT a one-door grant and not the
  // internal rex-enabler. One-door profiles are treated as individual-door
  // grants (find-or-created by `ensure_door_profile`).
  _groupProfiles() {
    return this._s.profiles.filter(
      (p) => !p.system && (!p.doors || p.doors.length !== 1)
    );
  }

  _alwaysSchedule() {
    const s =
      this._s.schedules.find((x) => x.token === "standard_always") ||
      this._s.schedules.find((x) => /always/i.test(x.name || ""));
    return s ? s.token : (this._s.schedules[0] || {}).token || "standard_always";
  }

  // For a credential's profile tokens, find a one-door profile granting `doorTok`.
  _doorGrantFor(credTokens, doorTok) {
    const byTok = this._profilesByToken();
    for (const t of credTokens || []) {
      const p = byTok[t];
      if (p && !p.system && p.doors && p.doors.length === 1 &&
          p.doors[0].token === doorTok) {
        const sched = (p.policies && p.policies[0] && p.policies[0].schedule_token) ||
          this._alwaysSchedule();
        return { profile: p.token, schedule: sched };
      }
    }
    return null;
  }

  // Groups + Individual-doors picker, shared by the add and edit forms.
  _renderAccessPicker(prefix, credTokens) {
    const sel = new Set(credTokens || []);
    const groups = this._groupProfiles()
      .map((p) => {
        const doors = (p.doors || []).map((d) => esc(d.name)).join(", ");
        const scheds = (p.schedules || []).map(esc).join(", ");
        const sub = doors
          ? `${doors}${scheds ? " · " + scheds : ""}`
          : "no doors yet";
        return `
        <label class="chk">
          <input type="checkbox" data-grp="${prefix}-group" value="${esc(p.token)}"
            ${sel.has(p.token) ? "checked" : ""}>
          <span>${esc(p.name)} <em class="sub">(${sub})</em></span>
        </label>`;
      })
      .join("");

    const doors = this._s.doors
      .map((d) => {
        const grant = this._doorGrantFor(credTokens, d.token);
        const cur = grant ? grant.schedule : this._alwaysSchedule();
        const opts = this._s.schedules
          .map(
            (s) =>
              `<option value="${esc(s.token)}" ${s.token === cur ? "selected" : ""}>${esc(s.name)}</option>`
          )
          .join("");
        return `
        <div class="doorrow">
          <label class="chk">
            <input type="checkbox" data-grp="${prefix}-door" value="${esc(d.token)}"
              ${grant ? "checked" : ""}>
            <span>${esc(d.name)}</span>
          </label>
          <select>${opts}</select>
        </div>`;
      })
      .join("");

    return `
      <div class="frow">
        <span class="lbl">Groups</span>
        <div class="checks">${groups || "<em>No groups</em>"}</div>
      </div>
      <div class="frow">
        <span class="lbl">Individual doors <em class="sub">(in addition to groups)</em></span>
        <div class="doors">${doors || "<em>No doors</em>"}</div>
      </div>`;
  }

  // Read the picker synchronously (before a re-render wipes the form).
  _readAccess(prefix) {
    const root = this.shadowRoot;
    const groupTokens = Array.from(
      root.querySelectorAll(`input[data-grp="${prefix}-group"]:checked`)
    ).map((i) => i.value);
    const doorGrants = Array.from(
      root.querySelectorAll(`input[data-grp="${prefix}-door"]:checked`)
    ).map((cb) => {
      const row = cb.closest(".doorrow");
      const sel = row ? row.querySelector("select") : null;
      return {
        door_token: cb.value,
        schedule_token: sel ? sel.value : this._alwaysSchedule(),
      };
    });
    return { groupTokens, doorGrants };
  }

  // Turn individual-door grants into profile tokens (find-or-create), async.
  async _resolveDoorProfiles(doorGrants) {
    const tokens = [];
    for (const g of doorGrants) {
      const resp = await this._callResponse("ensure_door_profile", {
        door_token: g.door_token,
        schedule_token: g.schedule_token,
      });
      if (resp && resp.profile_token) tokens.push(resp.profile_token);
    }
    return tokens;
  }

  // ---- validity window (start/end dates) + expiry action ---------------- //
  // The controller enforces the window itself (date only); the "when expired"
  // action is applied by the integration's daily reaper once the end passes.
  _renderValidity(prefix, cred) {
    const vf = (cred && cred.valid_from) || "";
    const vt = (cred && cred.valid_to) || "";
    const act = (cred && cred.expire_action) || "disable";
    return `
      <div class="frow three valid">
        <label class="fld">
          <span>Start date <em class="sub">(optional)</em></span>
          <input id="${prefix}-valid-from" type="date" value="${esc(vf)}">
        </label>
        <label class="fld">
          <span>End date <em class="sub">(optional)</em></span>
          <input id="${prefix}-valid-to" type="date" value="${esc(vt)}">
        </label>
        <label class="fld type">
          <span>When expired</span>
          <select id="${prefix}-expire">
            <option value="disable" ${act === "delete" ? "" : "selected"}>Disable</option>
            <option value="delete" ${act === "delete" ? "selected" : ""}>Delete</option>
          </select>
        </label>
      </div>`;
  }

  // Read the date inputs synchronously (before a re-render wipes the form).
  _readValidity(prefix) {
    const root = this.shadowRoot;
    const val = (id) => {
      const el = root.getElementById(id);
      return el ? (el.value || "").trim() : "";
    };
    return {
      valid_from: val(`${prefix}-valid-from`),
      valid_to: val(`${prefix}-valid-to`),
      expire_action: val(`${prefix}-expire`) || "disable",
    };
  }

  // Small status pill for the Name cell: Expired / Starts <date> / Expires <date>.
  _validityBadge(c) {
    if (!c.valid_from && !c.valid_to) return "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const parse = (s) => {
      const t = new Date(s + "T00:00:00");
      return isNaN(t.getTime()) ? null : t;
    };
    const to = c.valid_to ? parse(c.valid_to) : null;
    const from = c.valid_from ? parse(c.valid_from) : null;
    if (to && to < today)
      return `<span class="vbadge expired" title="Expired ${esc(c.valid_to)}">Expired</span>`;
    if (from && from > today)
      return `<span class="vbadge pending" title="Valid from ${esc(c.valid_from)}">Starts ${esc(c.valid_from)}</span>`;
    if (to)
      return `<span class="vbadge" title="Valid through ${esc(c.valid_to)}">Expires ${esc(c.valid_to)}</span>`;
    return "";
  }

  _codeText(cred) {
    if (this._s.revealed) {
      const parts = [];
      if (cred.pin) parts.push(cred.pin);
      if (cred.card) parts.push(cred.card);
      return parts.length ? parts.join(" / ") : "—";
    }
    return cred.kind && cred.kind !== "none" ? "••••" : "—";
  }

  // ---- actions (wired via delegation) ----------------------------------- //
  _onClick(ev) {
    const el = ev.target.closest("[data-action]");
    if (!el || this._s.busy) return;
    const action = el.dataset.action;
    const token = el.dataset.token;

    if (action === "refresh") return this._mutate(async () => {});
    if (action === "reveal") {
      this._s.revealed = !this._s.revealed;
      return this._mutate(async () => {});
    }
    if (action === "add-open") {
      this._s.addOpen = !this._s.addOpen;
      this._s.editToken = null;
      return this._render();
    }
    if (action === "add-cancel") {
      this._s.addOpen = false;
      return this._render();
    }
    if (action === "add-generate") return this._generateCode("add");
    if (action === "add-save") return this._submitAdd();
    if (action === "edit-open") {
      this._s.editToken = this._s.editToken === token ? null : token;
      this._s.addOpen = false;
      this._s.confirmDelete = null;
      return this._render();
    }
    if (action === "edit-cancel") {
      this._s.editToken = null;
      return this._render();
    }
    if (action === "edit-generate") return this._generateCode("edit");
    if (action === "edit-save") return this._submitEdit(token);
    if (action === "delete") {
      this._s.confirmDelete = token;
      return this._render();
    }
    if (action === "delete-cancel") {
      this._s.confirmDelete = null;
      return this._render();
    }
    if (action === "delete-confirm") {
      this._s.confirmDelete = null;
      const cred = this._s.credentials.find((c) => c.token === token);
      const userToken = cred && cred.user_token;
      const lastForUser =
        userToken &&
        this._s.credentials.filter((c) => c.user_token === userToken).length <= 1;
      return this._mutate(async () => {
        await this._callService("remove_credential", { credential_token: token });
        // A cardholder with no remaining credentials is dead weight — drop it so
        // "delete" reads as removing the person, not just one of their codes.
        if (lastForUser) {
          await this._callService("remove_user", { user_token: userToken });
        }
      });
    }
  }

  _onChange(ev) {
    // Credential-type switch (add or edit form): relabel the code field (no
    // re-render so the other fields keep their values).
    if (ev.target.id === "add-type" || ev.target.id === "edit-type") {
      const prefix = ev.target.id.split("-")[0];
      const isPin = ev.target.value === "pin";
      const label = this.shadowRoot.getElementById(`${prefix}-code-label`);
      const code = this.shadowRoot.getElementById(`${prefix}-code`);
      if (label) label.textContent = isPin ? "PIN" : "Card number";
      if (code) code.placeholder = isPin
        ? "1234"
        : prefix === "edit"
        ? "leave blank to keep"
        : "card number";
      return;
    }
    const el = ev.target.closest('[data-toggle="enabled"]');
    if (!el || this._s.busy) return;
    const token = el.dataset.token;
    const enabled = el.checked;
    this._mutate(() =>
      this._callService("set_credential_enabled", {
        credential_token: token,
        enabled,
      })
    );
  }

  _selectedProfiles(group) {
    return Array.from(
      this.shadowRoot.querySelectorAll(`input[data-grp="${group}"]:checked`)
    ).map((i) => i.value);
  }

  async _generateCode(prefix) {
    const root = this.shadowRoot;
    const typeEl = root.getElementById(`${prefix}-type`);
    const codeEl = root.getElementById(`${prefix}-code`);
    if (!typeEl || !codeEl) return;
    const prev = codeEl.value;
    codeEl.disabled = true;
    codeEl.value = "…";
    try {
      const resp = await this._callResponse("generate_code", { kind: typeEl.value });
      codeEl.value = (resp && resp.code) || prev;
    } catch (err) {
      codeEl.value = prev;
      this._s.error = this._errText(err);
      this._render();
      return;
    }
    codeEl.disabled = false;
  }

  _submitAdd() {
    const root = this.shadowRoot;
    const name = (root.getElementById("add-name").value || "").trim();
    const kind = root.getElementById("add-type").value;
    const code = (root.getElementById("add-code").value || "").trim();
    const enabled = root.getElementById("add-enabled").checked;
    const { groupTokens, doorGrants } = this._readAccess("add");
    const v = this._readValidity("add");
    if (!name || !code) {
      this._s.error = `Name and ${kind === "card" ? "card number" : "PIN"} are required.`;
      return this._render();
    }
    this._s.addOpen = false;
    this._mutate(async () => {
      const doorTokens = await this._resolveDoorProfiles(doorGrants);
      await this._callService("add_credential", {
        name,
        kind,
        code,
        access_profile_tokens: [...groupTokens, ...doorTokens],
        enabled,
        valid_from: v.valid_from,
        valid_to: v.valid_to,
        expire_action: v.expire_action,
      });
    });
  }

  _submitEdit(credToken) {
    const cred = this._s.credentials.find((c) => c.token === credToken);
    if (!cred) return;
    const root = this.shadowRoot;
    const name = (root.getElementById("edit-name").value || "").trim();
    const kind = root.getElementById("edit-type").value;
    const newCode = (root.getElementById("edit-code").value || "").trim();
    const { groupTokens, doorGrants } = this._readAccess("edit");
    const v = this._readValidity("edit");
    const curVf = cred.valid_from || "";
    const curVt = cred.valid_to || "";
    const curAct = cred.expire_action || "disable";
    const validityChanged =
      v.valid_from !== curVf ||
      v.valid_to !== curVt ||
      (!!v.valid_to && v.expire_action !== curAct);
    const nameChanged = name !== (cred.user_name || "");
    const curCode = this._s.revealed ? cred.pin || cred.card || "" : null;
    const curKind = cred.has_pin && !cred.has_card ? "pin" : "card";
    // Change the code only when one was entered and it actually differs (when
    // revealed we can compare; otherwise any entry is treated as a change).
    const codeChanged =
      !!newCode &&
      !(this._s.revealed && newCode === curCode && kind === curKind);

    this._s.editToken = null;
    this._mutate(async () => {
      if (nameChanged && cred.user_token) {
        await this._callService("set_user", {
          user_token: cred.user_token,
          name,
        });
      }
      if (codeChanged) {
        await this._callService("set_credential_code", {
          credential_token: credToken,
          kind,
          code: newCode,
        });
      }
      const doorTokens = await this._resolveDoorProfiles(doorGrants);
      const next = [...groupTokens, ...doorTokens];
      const orig = (cred.access_profile_tokens || []).slice().sort();
      if (JSON.stringify(orig) !== JSON.stringify(next.slice().sort())) {
        await this._callService("set_credential_access_profiles", {
          credential_token: credToken,
          access_profile_tokens: next,
        });
      }
      if (validityChanged) {
        await this._callService("set_credential_validity", {
          credential_token: credToken,
          valid_from: v.valid_from,
          valid_to: v.valid_to,
          expire_action: v.expire_action,
        });
      }
    });
  }

  // ---- render ----------------------------------------------------------- //
  _render() {
    if (!this.shadowRoot) return;
    const s = this._s;
    const admin = this._canManage;
    const canReveal = admin && this._config.allow_reveal;

    let body;
    if (s.loading) {
      body = `<div class="msg">Loading access codes…</div>`;
    } else if (s.error && !s.credentials.length) {
      body = `<div class="msg err">${esc(s.error)}</div>`;
    } else {
      body = this._renderBody(admin);
    }

    this.shadowRoot.innerHTML = `
      <style>${AxisPacsCodesCard.styles}</style>
      <ha-card>
        <div class="head">
          <div class="title">${esc(this._config.title)}</div>
          <div class="tools">
            ${
              canReveal
                ? `<button class="link" data-action="reveal">${
                    s.revealed ? "Hide codes" : "Show codes"
                  }</button>`
                : ""
            }
            <button class="icon" data-action="refresh" title="Refresh">↻</button>
          </div>
        </div>
        ${s.busy ? `<div class="bar"></div>` : ""}
        ${
          s.error && s.credentials.length
            ? `<div class="msg err inline">${esc(s.error)}</div>`
            : ""
        }
        ${body}
      </ha-card>
    `;

    if (!this._bound) {
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
      this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
      this._bound = true;
    }
  }

  _renderBody(admin) {
    const s = this._s;
    const rows = s.credentials.map((c) => this._renderRow(c, admin)).join("");
    const empty = s.credentials.length
      ? ""
      : `<tr><td class="empty" colspan="${admin ? 7 : 6}">No credentials.</td></tr>`;

    return `
      <div class="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Name</th><th>Type</th><th>Code</th><th>Doors</th><th>Last Used</th>
              <th class="c">Enabled</th>${admin ? "<th></th>" : ""}
            </tr>
          </thead>
          <tbody>${rows}${empty}</tbody>
        </table>
      </div>
      ${admin ? this._renderAdd() : ""}
    `;
  }

  _relTime(iso) {
    if (!iso) return "—";
    const t = Date.parse(iso);
    if (isNaN(t)) return "—";
    let s = Math.floor((Date.now() - t) / 1000);
    if (s < 0) s = 0;
    if (s < 45) return "just now";
    const m = Math.round(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.round(m / 60);
    if (h < 24) return h + "h ago";
    const d = Math.round(h / 24);
    if (d < 30) return d + "d ago";
    return new Date(t).toLocaleDateString();
  }

  _renderRow(c, admin) {
    const s = this._s;
    const doors = this._doorsForCredential(c);
    const doorText = doors.length ? doors.map(esc).join(", ") : "—";
    const editing = admin && s.editToken === c.token;
    const confirming = s.confirmDelete === c.token;
    const luText = this._relTime(c.last_used);
    const luTitle = c.last_used
      ? new Date(Date.parse(c.last_used)).toLocaleString() +
        (c.last_used_door ? " · " + c.last_used_door : "")
      : "No use recorded yet";

    const main = `
      <tr class="${c.enabled ? "" : "off"}">
        <td>${esc(c.user_name || "(unnamed)")}${this._validityBadge(c)}</td>
        <td>${esc(KIND_LABEL[c.kind] || "—")}</td>
        <td class="code">${esc(this._codeText(c))}</td>
        <td>${doorText}</td>
        <td class="lastused" title="${esc(luTitle)}">${esc(luText)}</td>
        <td class="c">
          <label class="sw">
            <input type="checkbox" data-toggle="enabled" data-token="${esc(c.token)}"
              ${c.enabled ? "checked" : ""} ${admin ? "" : "disabled"}>
            <span class="slider"></span>
          </label>
        </td>
        ${
          admin
            ? `<td class="c nowrap">
                 <button class="icon" data-action="edit-open" data-token="${esc(c.token)}" title="Edit">✎</button>
                 ${
                   confirming
                     ? `<button class="link danger" data-action="delete-confirm" data-token="${esc(c.token)}">Confirm</button>
                        <button class="link" data-action="delete-cancel">✕</button>`
                     : `<button class="icon" data-action="delete" data-token="${esc(c.token)}" title="Delete">🗑</button>`
                 }
               </td>`
            : ""
        }
      </tr>`;

    if (!editing) return main;
    return main + this._renderEdit(c);
  }

  _renderEdit(c) {
    const curKind = c.has_pin && !c.has_card ? "pin" : "card";
    const curCode = this._s.revealed ? c.pin || c.card || "" : "";
    return `
      <tr class="form"><td colspan="7">
        <div class="formwrap">
          <div class="frow three">
            <label class="fld">
              <span>Cardholder name</span>
              <input id="edit-name" type="text" value="${esc(c.user_name || "")}"
                placeholder="Doe, Jane">
            </label>
            <label class="fld type">
              <span>Type</span>
              <select id="edit-type">
                <option value="card" ${curKind === "card" ? "selected" : ""}>Card</option>
                <option value="pin" ${curKind === "pin" ? "selected" : ""}>PIN</option>
              </select>
            </label>
            <label class="fld">
              <span id="edit-code-label">${curKind === "pin" ? "PIN" : "Card number"}</span>
              <div class="codewrap">
                <input id="edit-code" type="text" inputmode="numeric"
                  value="${esc(curCode)}" placeholder="${this._s.revealed ? "" : "leave blank to keep"}">
                <button class="icon gen" type="button" data-action="edit-generate"
                  title="Generate a unique code"><ha-icon icon="mdi:dice-multiple"></ha-icon></button>
              </div>
            </label>
          </div>
          ${this._renderAccessPicker("edit", c.access_profile_tokens)}
          ${this._renderValidity("edit", c)}
          <div class="frow actions">
            <button class="btn" data-action="edit-save" data-token="${esc(c.token)}">Save</button>
            <button class="link" data-action="edit-cancel">Cancel</button>
          </div>
        </div>
      </td></tr>`;
  }

  _renderAdd() {
    const s = this._s;
    if (!s.addOpen) {
      return `<div class="addbar"><button class="btn" data-action="add-open">+ Add user</button></div>`;
    }
    return `
      <div class="formwrap add">
        <div class="frow three">
          <label class="fld">
            <span>Cardholder name</span>
            <input id="add-name" type="text" placeholder="Doe, Jane">
          </label>
          <label class="fld type">
            <span>Type</span>
            <select id="add-type">
              <option value="card" selected>Card</option>
              <option value="pin">PIN</option>
            </select>
          </label>
          <label class="fld">
            <span id="add-code-label">Card number</span>
            <div class="codewrap">
              <input id="add-code" type="text" inputmode="numeric" placeholder="card number">
              <button class="icon gen" type="button" data-action="add-generate"
                title="Generate a unique code"><ha-icon icon="mdi:dice-multiple"></ha-icon></button>
            </div>
          </label>
        </div>
        ${this._renderAccessPicker("add", [])}
        ${this._renderValidity("add", null)}
        <div class="frow actions">
          <label class="chk inline"><input id="add-enabled" type="checkbox" checked><span>Enabled</span></label>
          <span class="spacer"></span>
          <button class="btn" data-action="add-save">Add</button>
          <button class="link" data-action="add-cancel">Cancel</button>
        </div>
      </div>`;
  }
}

AxisPacsCodesCard.styles = `
  :host { display: block; }
  ha-card { padding: 12px 16px 16px; box-sizing: border-box; width: 100%; }
  .tablewrap { overflow-x: auto; }
  .head { display: flex; align-items: center; justify-content: space-between; }
  .title { font-size: 1.15rem; font-weight: 500; }
  .tools { display: flex; align-items: center; gap: 8px; }
  .bar { height: 2px; margin: 6px 0; background: linear-gradient(90deg,
    var(--primary-color), transparent); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:.4} 50%{opacity:1} }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: .95rem; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid
    var(--divider-color, #e0e0e0); vertical-align: middle; }
  th { font-weight: 500; color: var(--secondary-text-color); font-size: .82rem;
    text-transform: uppercase; letter-spacing: .03em; }
  td.c, th.c { text-align: center; }
  td.code { font-family: var(--code-font-family, monospace); letter-spacing: .08em; }
  td.lastused { white-space: nowrap; color: var(--secondary-text-color); }
  td.nowrap { white-space: nowrap; }
  tr.off td { color: var(--secondary-text-color); }
  td.empty, .msg { color: var(--secondary-text-color); padding: 16px 4px; }
  .msg.err { color: var(--error-color, #db4437); }
  .msg.inline { padding: 4px 4px 0; font-size: .9rem; }
  .empty { text-align: center; }
  button.icon { background: none; border: none; cursor: pointer; font-size: 1rem;
    color: var(--secondary-text-color); padding: 2px 6px; border-radius: 4px; }
  button.icon:hover { background: var(--secondary-background-color); color: var(--primary-text-color); }
  button.link { background: none; border: none; cursor: pointer; color: var(--primary-color);
    font-size: .9rem; padding: 2px 6px; }
  button.link.danger { color: var(--error-color, #db4437); font-weight: 500; }
  button.btn { background: var(--primary-color); color: var(--text-primary-color, #fff);
    border: none; border-radius: 6px; padding: 7px 14px; cursor: pointer; font-size: .92rem; }
  button.btn:hover { opacity: .9; }
  .addbar { margin-top: 12px; }
  .formwrap { padding: 10px 0 4px; }
  .formwrap.add { margin-top: 12px; padding: 12px; background: var(--secondary-background-color);
    border-radius: 8px; }
  tr.form td { background: var(--secondary-background-color); }
  .frow { margin-bottom: 10px; }
  .frow.two, .frow.three { display: flex; gap: 12px; flex-wrap: wrap; }
  .fld.type { flex: 0 0 auto; min-width: 110px; }
  .frow.actions { display: flex; align-items: center; gap: 6px; }
  .spacer { flex: 1; }
  .fld { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 160px; }
  .fld span, .lbl { font-size: .82rem; color: var(--secondary-text-color); }
  .lbl { display: block; margin-bottom: 6px; }
  input[type="text"], input[type="date"], select { padding: 7px 9px;
    border: 1px solid var(--divider-color, #ccc); border-radius: 6px;
    background: var(--card-background-color); color: var(--primary-text-color);
    font-size: .95rem; font-family: inherit; }
  input[type="date"]::-webkit-calendar-picker-indicator { filter: var(--date-picker-filter, none); }
  select { cursor: pointer; }
  .vbadge { display: inline-block; margin-left: 8px; padding: 1px 7px; border-radius: 10px;
    font-size: .72rem; font-weight: 500; vertical-align: middle; white-space: nowrap;
    background: var(--secondary-background-color); color: var(--secondary-text-color);
    border: 1px solid var(--divider-color, #ddd); }
  .vbadge.expired { background: var(--error-color, #db4437); color: #fff; border-color: transparent; }
  .vbadge.pending { background: var(--warning-color, #ffa600); color: #222; border-color: transparent; }
  .codewrap { display: flex; gap: 6px; align-items: center; }
  .codewrap input { flex: 1; min-width: 90px; }
  button.gen { padding: 4px 6px; color: var(--primary-color); }
  button.gen ha-icon { --mdc-icon-size: 20px; }
  .checks { display: flex; flex-wrap: wrap; gap: 8px 16px; }
  .chk { display: inline-flex; align-items: center; gap: 6px; font-size: .92rem; cursor: pointer; }
  .doors { display: flex; flex-direction: column; gap: 6px; }
  .doorrow { display: flex; align-items: center; gap: 10px; }
  .doorrow .chk { min-width: 130px; }
  .doorrow select { padding: 5px 8px; }
  .sub { font-style: normal; color: var(--secondary-text-color); font-size: .85em; }
  .chk.inline span { color: var(--primary-text-color); }
  /* toggle */
  .sw { position: relative; display: inline-block; width: 38px; height: 20px; }
  .sw input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; inset: 0; background: var(--switch-unchecked-track-color, #bbb);
    border-radius: 20px; transition: .2s; cursor: pointer; }
  .slider:before { content: ""; position: absolute; height: 14px; width: 14px; left: 3px; top: 3px;
    background: var(--switch-unchecked-button-color, #fff); border-radius: 50%; transition: .2s; }
  .sw input:checked + .slider { background: var(--switch-checked-track-color, var(--primary-color)); }
  .sw input:checked + .slider:before { transform: translateX(18px);
    background: var(--switch-checked-button-color, #fff); }
  .sw input:disabled + .slider { opacity: .5; cursor: default; }
`;

customElements.define("axis-pacs-codes-card", AxisPacsCodesCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "axis-pacs-codes-card",
  name: "Axis PACS Access Codes",
  description: "Manage Axis PACS cardholders, PINs and cards.",
});

// eslint-disable-next-line no-console
console.info("%c axis-pacs-codes-card %c loaded ", "background:#222;color:#7cf", "");
