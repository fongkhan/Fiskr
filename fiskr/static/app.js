// Fiskr - Dashboard Controller v3.1

let currentUser = null;
let auditHistory = [];
let activeSnapshots = [];
let wlCurrentPage = 1;
const wlItemsPerPage = 100;
let wlSearchDebounce = null;

// ------------------ LIBELLÉS PARTAGÉS (types de listes & sources) ------------------

// Une seule source de vérité pour les libellés de types de listes, partout dans l'UI
const LIST_TYPE_LABELS = {
    WATCHLIST_OFAC: "OFAC",
    WATCHLIST_EU: "UE",
    WATCHLIST_UN: "ONU",
    WATCHLIST_DGT: "DGT",
    WATCHLIST_PEP: "PEP",
    WATCHLIST_OFSI: "OFSI",
    WATCHLIST_SSIE: "SSIE",
    CLIENT_BASE: "Base clients",
};

const SYNC_SOURCE_LABELS = {
    OFAC: "OFAC SDN",
    EURLEX: "EUR-Lex JO",
    EUFSF: "UE FSF",
    DGT: "DGT Gels",
    UN: "ONU",
    PEP: "PEP OpenSanctions",
    OFSI: "UK OFSI",
};

function listTypeLabel(t) {
    if (!t) return "Inconnue";
    return LIST_TYPE_LABELS[t] || t;
}

function listTypeBadge(t) {
    const label = listTypeLabel(t);
    const muted = !t ? ' style="opacity:0.55;"' : "";
    return `<span class="badge-secondary"${muted} title="${escapeHtml(t || "Type de liste inconnu (enregistrement antérieur)")}">${escapeHtml(label)}</span>`;
}

// Options des selects de filtre « Liste » (valeur UNKNOWN = enregistrements sans type)
function listTypeFilterOptions(withUnknown) {
    let html = '<option value="">Toutes les listes</option>';
    for (const [value, label] of Object.entries(LIST_TYPE_LABELS)) {
        if (value === "CLIENT_BASE") continue;
        html += `<option value="${value}">${label}</option>`;
    }
    if (withUnknown) html += '<option value="UNKNOWN">Inconnue (antérieur)</option>';
    return html;
}

// ------------------ NOTIFICATIONS INTÉGRÉES (toasts & dialogs) ------------------

function showToast(message, type = "info", durationMs = 4500) {
    const container = document.getElementById("toast-container");
    if (!container) { console.log(`[${type}] ${message}`); return; }
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    const icons = { success: "✅", error: "⚠️", info: "ℹ️" };
    toast.innerHTML = `<span class="toast-icon">${icons[type] || icons.info}</span><span class="toast-msg">${escapeHtml(message)}</span>`;
    toast.onclick = () => toast.remove();
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add("toast-out");
        setTimeout(() => toast.remove(), 350);
    }, type === "error" ? Math.max(durationMs, 7000) : durationMs);
}

// Modale générique : résout une Promise (bouton, Escape = annulation)
function _openAppDialog({ title, message, input, textarea, placeholder, required, danger, confirmLabel, cancelLabel }) {
    return new Promise((resolve) => {
        const overlay = document.getElementById("app-dialog");
        const titleEl = document.getElementById("app-dialog-title");
        const msgEl = document.getElementById("app-dialog-message");
        const fieldWrap = document.getElementById("app-dialog-field");
        const errEl = document.getElementById("app-dialog-error");
        const okBtn = document.getElementById("app-dialog-confirm");
        const cancelBtn = document.getElementById("app-dialog-cancel");

        titleEl.textContent = title || "Confirmation";
        msgEl.textContent = message || "";
        errEl.classList.add("hidden");
        okBtn.textContent = confirmLabel || "Confirmer";
        cancelBtn.textContent = cancelLabel || "Annuler";
        okBtn.className = danger ? "btn btn-danger" : "btn btn-primary";

        let inputEl = null;
        fieldWrap.innerHTML = "";
        if (input || textarea) {
            inputEl = document.createElement(textarea ? "textarea" : "input");
            if (textarea) inputEl.rows = 4;
            inputEl.placeholder = placeholder || "";
            inputEl.id = "app-dialog-input";
            fieldWrap.appendChild(inputEl);
        }

        const cleanup = (value) => {
            overlay.classList.add("hidden");
            document.removeEventListener("keydown", onKey);
            okBtn.onclick = cancelBtn.onclick = null;
            resolve(value);
        };
        const onKey = (e) => { if (e.key === "Escape") cleanup(null); };

        okBtn.onclick = () => {
            if (inputEl) {
                const value = inputEl.value.trim();
                if (required && !value) {
                    errEl.textContent = "Ce champ est obligatoire.";
                    errEl.classList.remove("hidden");
                    inputEl.focus();
                    return;
                }
                cleanup(value);
            } else {
                cleanup(true);
            }
        };
        cancelBtn.onclick = () => cleanup(inputEl ? null : false);
        document.addEventListener("keydown", onKey);
        overlay.classList.remove("hidden");
        (inputEl || okBtn).focus();
    });
}

function confirmDialog(message, options = {}) {
    return _openAppDialog({ title: options.title || "Confirmation", message, danger: options.danger,
                            confirmLabel: options.confirmLabel, cancelLabel: options.cancelLabel });
}

// Saisie intégrée (remplace prompt() ; textarea pour les commentaires réglementaires)
function promptDialog(title, options = {}) {
    return _openAppDialog({ title, message: options.message || "", input: !options.textarea,
                            textarea: options.textarea, placeholder: options.placeholder,
                            required: options.required !== false,
                            confirmLabel: options.confirmLabel || "Valider" });
}

// ------------------ COMPTEURS DE LA BARRE LATÉRALE (badges) ------------------

async function refreshSidebarCounters() {
    try {
        const response = await fetch("/api/counters");
        if (!response.ok) return;
        const c = await response.json();
        const alertBadge = document.getElementById("alerts-open-badge");
        if (alertBadge) {
            alertBadge.textContent = c.open_alerts;
            alertBadge.classList.toggle("hidden", !c.open_alerts);
        }
        const reviewBadge = document.getElementById("review-pending-badge");
        if (reviewBadge) {
            reviewBadge.textContent = c.pending_reviews;
            reviewBadge.classList.toggle("hidden", !c.pending_reviews);
        }
    } catch (e) { /* silencieux : simple polling de badges */ }
}

// Peuple les selects de filtre « Liste » et les cases du périmètre de criblage
function initListTypeControls() {
    const selects = [
        ["wl-list-filter", false],
        ["snapshots-list-filter", false],
        ["alerts-list-filter", true],
        ["audit-list-filter", true],
        ["whitelist-list-filter", true],
    ];
    for (const [id, withUnknown] of selects) {
        const el = document.getElementById(id);
        if (el) el.innerHTML = listTypeFilterOptions(withUnknown);
    }
    for (const containerId of ["screening-lists-checkboxes", "batch-lists-checkboxes"]) {
        const container = document.getElementById(containerId);
        if (!container) continue;
        container.innerHTML = Object.entries(LIST_TYPE_LABELS)
            .filter(([value]) => value !== "CLIENT_BASE")
            .map(([value, label]) => `
                <label style="display: flex; align-items: center; gap: 0.4rem; cursor: pointer; font-size: 0.85rem;">
                    <input type="checkbox" class="${containerId}-cb" value="${value}" checked> ${label}
                </label>`)
            .join("");
    }
}

// Lit les cases cochées d'un périmètre de criblage ; null = toutes (pas de restriction)
function selectedScreeningLists(containerId) {
    const boxes = Array.from(document.querySelectorAll(`.${containerId}-cb`));
    if (!boxes.length) return null;
    const checked = boxes.filter(b => b.checked).map(b => b.value);
    if (!checked.length || checked.length === boxes.length) return null;
    return checked;
}

document.addEventListener("DOMContentLoaded", () => {
    // Check authentication and load user info
    checkAuthUser();
    initListTypeControls();
    // Initial data loading
    fetchWatchlist();
    fetchWatchlistHash();
    fetchAuditHistory();
    fetchSnapshots();
    fetchConfig();
    fetchIngestionSettings();
    fetchPendingReviews();
    fetchAlerts();
    fetchWhitelist();
    // Badges vivants : compteurs légers rafraîchis toutes les 60 s
    setInterval(refreshSidebarCounters, 60_000);
});

// Check current logged-in user profile
async function checkAuthUser() {
    try {
        const response = await fetch("/api/auth/me");
        if (response.status === 401) {
            window.location.href = "/login";
            return;
        }
        const data = await response.json();
        if (data.user) {
            currentUser = data.user;
            const roles = userRoles(currentUser);
            const isAdmin = roles.includes("admin");
            const isReviewer = isAdmin || roles.includes("reviewer");
            const userEl = document.getElementById("sidebar-username");
            const roleEl = document.getElementById("sidebar-role");
            const navUsersItem = document.getElementById("nav-item-users");

            if (userEl) {
                userEl.textContent = data.user.full_name || data.user.username;
                userEl.title = `Connecté en tant que @${data.user.username}`;
            }
            if (roleEl) {
                const labels = { admin: "Administrateur (ACPR/AMF)", reviewer: "Réviseur Homologation", user: "Analyste Conformité" };
                roleEl.textContent = roles.map(r => labels[r] || r).join(" / ") || "Analyste Conformité";
            }
            if (navUsersItem) {
                navUsersItem.classList.toggle("hidden", !isAdmin);
            }
            // Onglet Paramètres (réglages transverses) réservé aux admins
            const navSettingsItem = document.getElementById("nav-item-settings");
            if (navSettingsItem) navSettingsItem.classList.toggle("hidden", !isAdmin);
            // Carte des réglages (dans l'onglet Paramètres) et actions de revue (reviewer ou admin)
            const settingsCard = document.getElementById("review-settings-card");
            if (settingsCard) settingsCard.classList.toggle("hidden", !isAdmin);
            const reviewActions = document.getElementById("review-actions");
            if (reviewActions) reviewActions.classList.toggle("hidden", !isReviewer);
            const exclusionToolbar = document.getElementById("review-exclusion-toolbar");
            if (exclusionToolbar) exclusionToolbar.classList.toggle("hidden", !isReviewer);
        }
    } catch (e) {
        console.error("Auth check failed:", e);
    }
}

// Handle User Logout
async function handleLogout() {
    if (!await confirmDialog("Voulez-vous vraiment vous déconnecter de Fiskr ?", { title: "Déconnexion" })) return;
    try {
        await fetch("/api/auth/logout", { method: "POST" });
    } catch (e) {
        console.error("Logout request error:", e);
    } finally {
        window.location.href = "/login";
    }
}

// Fetch Active System Configuration
async function fetchConfig() {
    try {
        const response = await fetch("/api/config");
        if (response.status === 401) return;
        const configData = await response.json();
        console.log("System config active:", configData);
    } catch (e) {
        console.error("Failed to fetch config:", e);
    }
}

// (Le journal d'audit est géré plus bas : fetchAuditHistory / renderAuditHistoryTable /
//  viewAuditLogDetail — les anciennes versions dupliquées et boguées ont été supprimées.)

// Tab navigation
function switchTab(tabId) {
    document.querySelectorAll(".nav-item").forEach(item => {
        item.classList.remove("active");
    });
    const activeBtn = document.getElementById(`nav-btn-${tabId}`);
    if (activeBtn) activeBtn.classList.add("active");
    
    document.querySelectorAll(".tab-content").forEach(sec => {
        sec.classList.remove("active");
    });
    const activeSec = document.getElementById(`sec-${tabId}`);
    if (activeSec) activeSec.classList.add("active");
    
    // Refresh tab-specific data
    if (tabId === "alerts") {
        fetchAlerts();
        fetchWhitelist();
    }
    if (tabId === "kpi") {
        fetchKpis();
    }
    if (tabId === "settings") {
        fetchIngestionSettings();
    }
    if (tabId === "watchlist-mgmt") {
        const activeSubBtn = activeSec.querySelector(".sub-tab-btn.active");
        if (activeSubBtn) {
            const subTabId = activeSubBtn.id.replace("sub-btn-", "");
            if (subTabId === "watchlist-active") {
                fetchWatchlist();
            } else if (subTabId === "watchlist-snapshots") {
                fetchSnapshots();
            }
        } else {
            fetchWatchlist();
            fetchSnapshots();
        }
    } else if (tabId === "audit") {
        fetchAuditHistory();
    } else if (tabId === "users") {
        fetchUsersList();
    }
}


// Sub-tab navigation
function switchSubTab(sectionId, subTabId) {
    const section = document.getElementById(`sec-${sectionId}`);
    if (!section) return;
    
    // Deactivate all sub-tab buttons inside this section
    section.querySelectorAll(".sub-tab-btn").forEach(btn => {
        btn.classList.remove("active");
    });
    
    // Activate clicked sub-tab button
    const activeBtn = document.getElementById(`sub-btn-${subTabId}`);
    if (activeBtn) activeBtn.classList.add("active");
    
    // Hide all sub-tab content panels inside this section
    section.querySelectorAll(".sub-tab-content").forEach(content => {
        content.classList.remove("active");
        content.classList.add("hidden");
    });
    
    // Show active sub-tab content panel
    const activeContent = document.getElementById(`sub-sec-${subTabId}`);
    if (activeContent) {
        activeContent.classList.add("active");
        activeContent.classList.remove("hidden");
    }

    // Refresh sub-tab specific data if needed
    if (subTabId === "watchlist-active") {
        fetchWatchlist();
    } else if (subTabId === "watchlist-snapshots") {
        fetchSnapshots();
    } else if (subTabId === "watchlist-review") {
        // Rupture de flux corrigée : les snapshots en attente d'homologation
        // sont rechargés à chaque ouverture du sous-onglet, plus seulement au load
        fetchPendingReviews();
    } else if (subTabId === "alerts-queue") {
        fetchAlerts();
    } else if (subTabId === "alerts-whitelist") {
        fetchWhitelist();
    } else if (subTabId === "watchlist-sync") {
        fetchSyncReports();
        fetchSyncConfig();
        const dateInput = document.getElementById("sync-eurlex-date");
        if (dateInput && !dateInput.value) {
            const now = new Date();
            dateInput.value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
        }
    }
}

// Toggle fields based on entity type in Screening Form
function toggleFormFields() {
    const type = document.getElementById("client-type").value;
    
    const ppFields = document.getElementById("pp-fields");
    const pmFields = document.getElementById("pm-fields");
    
    const leiGroup = document.getElementById("screen-lei-group");
    const vesselGroup = document.getElementById("screen-vessel-group");
    const aircraftGroup = document.getElementById("screen-aircraft-group");
    const passportGroup = document.getElementById("screen-passport-group");
    const passportCountryGroup = document.getElementById("screen-passport-country-group");
    const nationalIdGroup = document.getElementById("screen-national-id-group");
    
    // Default: Hide all specific groups
    if (ppFields) ppFields.classList.add("hidden");
    if (pmFields) pmFields.classList.add("hidden");
    
    if (leiGroup) leiGroup.classList.add("hidden");
    if (vesselGroup) vesselGroup.classList.add("hidden");
    if (aircraftGroup) aircraftGroup.classList.add("hidden");
    if (passportGroup) passportGroup.classList.add("hidden");
    if (passportCountryGroup) passportCountryGroup.classList.add("hidden");
    if (nationalIdGroup) nationalIdGroup.classList.add("hidden");
    
    if (type === "I") {
        // Individual
        if (ppFields) ppFields.classList.remove("hidden");
        if (passportGroup) passportGroup.classList.remove("hidden");
        if (passportCountryGroup) passportCountryGroup.classList.remove("hidden");
        if (nationalIdGroup) nationalIdGroup.classList.remove("hidden");
    } else if (type === "E") {
        // Corporate Entity
        if (pmFields) {
            pmFields.classList.remove("hidden");
            const label = pmFields.querySelector("label");
            if (label) label.textContent = "Raison Sociale / Nom PM *";
        }
        if (leiGroup) leiGroup.classList.remove("hidden");
    } else if (type === "V") {
        // Vessel
        if (pmFields) {
            pmFields.classList.remove("hidden");
            const label = pmFields.querySelector("label");
            if (label) label.textContent = "Nom du Navire *";
        }
        if (vesselGroup) vesselGroup.classList.remove("hidden");
    } else if (type === "O") {
        // Other / Aircraft
        if (pmFields) {
            pmFields.classList.remove("hidden");
            const label = pmFields.querySelector("label");
            if (label) label.textContent = "Nom Principal *";
        }
        if (aircraftGroup) aircraftGroup.classList.remove("hidden");
    }
}

// Toggle manual form fields based on entity type
function toggleManualFormFields() {
    const type = document.getElementById("manual-entity-type").value;
    
    const individualFields = document.getElementById("manual-individual-fields");
    
    const leiGroup = document.getElementById("manual-lei-group");
    const vesselGroup = document.getElementById("manual-vessel-group");
    const aircraftGroup = document.getElementById("manual-aircraft-group");
    const passportGroup = document.getElementById("manual-passport-group");
    const passportCountryGroup = document.getElementById("manual-passport-country-group");
    const nationalIdGroup = document.getElementById("manual-national-id-group");
    
    // Default: Hide all specific groups
    if (individualFields) individualFields.classList.add("hidden");
    
    if (leiGroup) leiGroup.classList.add("hidden");
    if (vesselGroup) vesselGroup.classList.add("hidden");
    if (aircraftGroup) aircraftGroup.classList.add("hidden");
    if (passportGroup) passportGroup.classList.add("hidden");
    if (passportCountryGroup) passportCountryGroup.classList.add("hidden");
    if (nationalIdGroup) nationalIdGroup.classList.add("hidden");
    
    if (type === "I") {
        if (individualFields) individualFields.classList.remove("hidden");
        if (passportGroup) passportGroup.classList.remove("hidden");
        if (passportCountryGroup) passportCountryGroup.classList.remove("hidden");
        if (nationalIdGroup) nationalIdGroup.classList.remove("hidden");
    } else if (type === "E") {
        if (leiGroup) leiGroup.classList.remove("hidden");
    } else if (type === "V") {
        if (vesselGroup) vesselGroup.classList.remove("hidden");
    } else if (type === "O") {
        if (aircraftGroup) aircraftGroup.classList.remove("hidden");
    }
}

// Handle manual watchlist entity submission
async function handleManualEntity(event) {
    event.preventDefault();
    
    const type = document.getElementById("manual-entity-type").value;
    const primaryName = document.getElementById("manual-primary-name").value.trim();
    const firstName = document.getElementById("manual-first-name").value.trim();
    const lastName = document.getElementById("manual-last-name").value.trim();
    const maidenName = document.getElementById("manual-maiden-name").value.trim();
    const dob = document.getElementById("manual-dob").value.trim();
    
    // Hard match
    const lei = document.getElementById("manual-lei").value.trim();
    const imo = document.getElementById("manual-imo").value.trim();
    const aircraft = document.getElementById("manual-aircraft").value.trim();
    const passportNum = document.getElementById("manual-passport-num").value.trim();
    const passportCountry = document.getElementById("manual-passport-country").value.trim();
    const nationalId = document.getElementById("manual-national-id").value.trim();
    
    // Extra
    const gender = document.getElementById("manual-gender").value;
    const dateOfDeath = document.getElementById("manual-date-of-death").value.trim();
    const nationality = document.getElementById("manual-nationality").value.trim();
    const residence = document.getElementById("manual-residence").value.trim();
    const placeOfBirth = document.getElementById("manual-place-of-birth").value.trim();
    const aliases = document.getElementById("manual-aliases").value.trim();
    const address = document.getElementById("manual-address").value.trim();
    const city = document.getElementById("manual-city").value.trim();
    const state = document.getElementById("manual-state").value.trim();
    const country = document.getElementById("manual-country").value.trim();
    const origin = document.getElementById("manual-origin").value.trim();
    const designation = document.getElementById("manual-designation").value.trim();
    const designationReasons = document.getElementById("manual-designation-reasons").value.trim();
    const altAddresses = document.getElementById("manual-alternative-addresses").value.trim();
    const additionalInfo = document.getElementById("manual-additional-informations").value.trim();
    
    const payload = {
        entity_type: type,
        primary_name: primaryName,
        first_name: type === "I" ? firstName : null,
        last_name: type === "I" ? lastName : null,
        maiden_name: type === "I" ? maidenName : null,
        aliases: aliases || null,
        dates_of_birth: dob || null,
        nationality: nationality || null,
        residence: residence || null,
        lei_number: lei || null,
        imo_number: imo || null,
        gender: gender || "U",
        aircraft_tail_number: aircraft || null,
        passport_documents: passportNum ? `${passportNum}` : null,
        national_id_documents: nationalId ? `${nationalId}` : null,
        place_of_birth: placeOfBirth || null,
        address: address || null,
        city: city || null,
        state: state || null,
        country: country || null,
        origin: origin || null,
        designation: designation || null,
        designation_reasons: designationReasons || null,
        alternative_addresses: altAddresses || null,
        additional_informations: additionalInfo || null,
        date_of_death: dateOfDeath || null
    };
    
    const btn = document.getElementById("submit-manual-btn");
    btn.disabled = true;
    btn.textContent = "Ajout en cours...";
    
    try {
        const response = await fetch("/api/watchlist/entity", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const errData = await response.json();
            const errors = errData.detail && errData.detail.errors ? errData.detail.errors.join(", ") : JSON.stringify(errData);
            showToast(`Erreur de validation Quality Gate : ${errors}`, "error");
            return;
        }
        
        const data = await response.json();
        showToast(`Entité ajoutée avec succès ! ID : ${data.entity_id}`, "success");
        
        // Reset form
        document.getElementById("manual-entity-form").reset();
        toggleManualFormFields();
        
        // Switch back to Active Watchlist and refresh
        fetchWatchlist();
        fetchWatchlistHash();
        switchSubTab('watchlist-mgmt', 'watchlist-active');
        
    } catch (e) {
        console.error("Error manual insert:", e);
        showToast("Erreur réseau de communication.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Ajouter à la Watchlist Active";
    }
}

// Collapsible Accordion Utility
function toggleAccordion(id) {
    const content = document.getElementById(id);
    const header = content.previousElementSibling;
    const section = content.parentElement;
    
    if (content.classList.contains("hidden")) {
        content.classList.remove("hidden");
        section.classList.add("active");
    } else {
        content.classList.add("hidden");
        section.classList.remove("active");
    }
}

// Fetch Snapshots List
async function fetchSnapshots() {
    try {
        const response = await fetch("/api/snapshots");
        activeSnapshots = await response.json();

        renderSnapshotsFiltered();
        populateCompareSelects(activeSnapshots);
    } catch (e) {
        console.error("Error fetching snapshots:", e);
    }
}

// Filtre client-side de l'historique des snapshots par type de liste
function renderSnapshotsFiltered() {
    const filterEl = document.getElementById("snapshots-list-filter");
    const selected = filterEl ? filterEl.value : "";
    const snaps = selected
        ? activeSnapshots.filter(s => s.file_type === selected)
        : activeSnapshots;
    renderSnapshotsTable(snaps);
}

// Render Snapshots Table
function renderSnapshotsTable(snaps) {
    const tbody = document.querySelector("#snapshots-table tbody");
    tbody.innerHTML = "";

    if (snaps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Aucun snapshot importé</td></tr>';
        return;
    }

    snaps.forEach(snap => {
        const dateStr = new Date(snap.uploaded_at).toLocaleString("fr-FR");
        const tr = document.createElement("tr");

        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(snap.file_name)}</strong><br><small style="color:var(--text-muted)">Hash: ${snap.file_hash.substring(0,8)}...</small></td>
            <td>${listTypeBadge(snap.file_type)}</td>
            <td>${snap.record_count}</td>
            <td>${snapshotStatusBadge(snap.status)}</td>
        `;
        tbody.appendChild(tr);
    });
}

// Badge de statut d'un snapshot (incl. cycle de vie homologation)
function snapshotStatusBadge(status) {
    if (status === "PENDING_REVIEW") {
        return '<span class="status-dot orange"></span> <span style="color: var(--color-warning); font-weight: 600;">EN ATTENTE D\'HOMOLOGATION</span>';
    }
    if (status === "REJECTED") {
        return '<span class="status-dot" style="background: #f87171;"></span> <span style="color: #f87171; font-weight: 600;">REJETÉ</span>';
    }
    return `<span class="status-dot ${status === 'READY' ? 'green' : 'orange'}"></span> ${status}`;
}

// Populate compare dropdown selectors
function populateCompareSelects(snaps) {
    const oldSelect = document.getElementById("compare-old-snap");
    const newSelect = document.getElementById("compare-new-snap");
    
    const oldVal = oldSelect.value;
    const newVal = newSelect.value;
    
    oldSelect.innerHTML = '<option value="">Sélectionnez un snapshot...</option>';
    newSelect.innerHTML = '<option value="">Sélectionnez un snapshot...</option>';
    
    snaps.forEach(snap => {
        const dateStr = new Date(snap.uploaded_at).toLocaleString("fr-FR");
        const optionText = `${snap.file_name} (${listTypeLabel(snap.file_type)}) - ${dateStr}`;
        
        const opt1 = document.createElement("option");
        opt1.value = snap.snapshot_id;
        opt1.textContent = optionText;
        oldSelect.appendChild(opt1);
        
        const opt2 = document.createElement("option");
        opt2.value = snap.snapshot_id;
        opt2.textContent = optionText;
        newSelect.appendChild(opt2);
    });
    
    oldSelect.value = oldVal;
    newSelect.value = newVal;
}

// Show/hide the SSIE selectors panel depending on the chosen file type
function toggleSsieOptions() {
    const fileType = document.getElementById("ingest-file-type").value;
    const panel = document.getElementById("ssie-options");
    panel.classList.toggle("hidden", fileType !== "WATCHLIST_SSIE");
}

// Handle Snapshot Ingestion (Upload file)
async function handleIngestion(event) {
    event.preventDefault();

    const fileType = document.getElementById("ingest-file-type").value;
    const fileInput = document.getElementById("ingest-file");
    const delimiter = document.getElementById("ingest-delimiter").value.trim();
    const btn = document.getElementById("submit-ingest-btn");

    if (fileInput.files.length === 0) {
        showToast("Veuillez sélectionner un fichier.", "error");
        return;
    }

    const formData = new FormData();
    formData.append("file_type", fileType);
    formData.append("file", fileInput.files[0]);
    formData.append("delimiter", delimiter || ",");

    if (fileType === "WATCHLIST_SSIE") {
        const selectorsRaw = document.getElementById("ssie-selectors").value.trim();
        if (selectorsRaw) {
            try {
                JSON.parse(selectorsRaw);
            } catch (e) {
                showToast("Les sélecteurs SSIE ne sont pas un JSON valide.", "error");
                return;
            }
            formData.append("ssie_selectors", selectorsRaw);
        }
        const sourceFormat = document.getElementById("ssie-source-format").value.trim();
        if (sourceFormat) {
            formData.append("ssie_source_format", sourceFormat);
        }
    }
    
    btn.disabled = true;
    btn.textContent = "Importation en cours...";
    
    try {
        const response = await fetch("/api/ingest", {
            method: "POST",
            body: formData
        });
        
        if (!response.ok) {
            const data = await response.json();
            showToast(`Erreur d'importation : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        
        const data = await response.json();
        showToast(`Instantané importé avec succès ! ${data.message}`, "success");
        fileInput.value = "";
        fetchSnapshots();
        fetchWatchlist();
        fetchWatchlistHash();
        fetchPendingReviews();
        // Fluidité du parcours : proposer d'enchaîner directement sur l'homologation
        if (data.status === "PENDING_REVIEW") {
            const go = await confirmDialog(
                "Le snapshot est en attente d'homologation. Ouvrir le parcours de production de liste (delta, exclusions, cahier de tests, décision) maintenant ?",
                { confirmLabel: "Ouvrir l'homologation", cancelLabel: "Plus tard" }
            );
            if (go) openPendingReview(data.snapshot_id);
        }
    } catch (e) {
        console.error("Error ingesting snapshot:", e);
        showToast("Erreur réseau de communication.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Charger & Archiver";
    }
}

// ------------------ SOURCES AUTOMATIQUES (Sync OFAC / EUR-Lex) ------------------

// Trigger a manual source synchronization (OFAC download or EUR-Lex scraping)
async function handleSourceSync(source) {
    const btnIds = { OFAC: "sync-ofac-btn", EURLEX: "sync-eurlex-btn", DGT: "sync-dgt-btn", UN: "sync-un-btn", EUFSF: "sync-eufsf-btn", PEP: "sync-pep-btn", OFSI: "sync-ofsi-btn" };
    const btn = document.getElementById(btnIds[source] || "sync-ofac-btn");
    const payload = { source };
    if (source === "EURLEX") {
        const dateVal = document.getElementById("sync-eurlex-date").value;
        if (dateVal) payload.date = dateVal;
    }

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Synchronisation en cours...";

    try {
        const response = await fetch("/api/sync/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) {
            showToast(`Erreur de synchronisation : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        const srcLabel = SYNC_SOURCE_LABELS[data.source] || data.source;
        let msg = `Synchronisation ${srcLabel} : ${data.status} — delta +${data.added_count} / ~${data.modified_count} / −${data.removed_count}`;
        if (data.rescreen && data.rescreen.new_alerts) {
            msg += ` · re-criblage : ${data.rescreen.new_alerts} nouvelle(s) alerte(s)`;
        }
        showToast(msg, data.status === "ERROR" ? "error" : "success", 8000);
        fetchSyncReports();
        fetchSnapshots();
        fetchWatchlist();
        fetchWatchlistHash();
        fetchPendingReviews();
        refreshSidebarCounters();
        // Fluidité du parcours : proposer d'enchaîner directement sur l'homologation
        if (data.status === "PENDING_REVIEW" && data.snapshot_id) {
            const go = await confirmDialog(
                `La synchronisation ${srcLabel} attend une homologation. Ouvrir le parcours de production de liste maintenant ?`,
                { confirmLabel: "Ouvrir l'homologation", cancelLabel: "Plus tard" }
            );
            if (go) openPendingReview(data.snapshot_id);
        }
    } catch (e) {
        console.error("Error running source sync:", e);
        showToast("Erreur réseau pendant la synchronisation.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// Load and render the synchronization reports history
async function fetchSyncReports() {
    try {
        const response = await fetch("/api/sync/reports");
        if (!response.ok) return;
        const reports = await response.json();
        renderSyncReportsTable(reports);
    } catch (e) {
        console.error("Error fetching sync reports:", e);
    }
}

function renderSyncReportsTable(reports) {
    const tbody = document.querySelector("#sync-reports-table tbody");
    tbody.innerHTML = "";

    if (!reports || reports.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Aucune synchronisation exécutée</td></tr>';
        return;
    }

    reports.forEach(report => {
        const dateStr = new Date(report.executed_at).toLocaleString("fr-FR");
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";

        let statusBadge;
        if (report.status === "SUCCESS") statusBadge = '<span class="status-badge no_match">SUCCESS</span>';
        else if (report.status === "ERROR") statusBadge = '<span class="status-badge alert">ERROR</span>';
        else statusBadge = `<span class="status-badge warning">${escapeHtml(report.status)}</span>`;

        const sourceLabel = SYNC_SOURCE_LABELS[report.source] || report.source;

        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}<br><small style="color:var(--text-muted)">${escapeHtml(report.trigger || "MANUAL")}</small></td>
            <td><strong>${escapeHtml(sourceLabel)}</strong></td>
            <td>${statusBadge}</td>
            <td>+${report.added_count} / ~${report.modified_count} / −${report.removed_count}</td>
            <td>${report.email_sent ? "📧 Envoyé" : "—"}</td>
        `;
        tr.addEventListener("click", () => showSyncReportDetail(report));
        tbody.appendChild(tr);
    });
}

// Display the detail (message + truncated delta) of a sync report
function showSyncReportDetail(report) {
    const panel = document.getElementById("sync-report-detail");
    const content = document.getElementById("sync-report-detail-content");
    const detail = {
        source: report.source,
        executed_at: report.executed_at,
        statut: report.status,
        message: report.message,
        snapshot: report.snapshot_id,
        snapshot_precedent: report.previous_snapshot_id,
        delta: report.delta_report
    };
    content.textContent = JSON.stringify(detail, null, 2);

    // Pieces probantes : liens vers les PDF officiels EUR-Lex archives
    const evidenceDiv = document.getElementById("sync-report-evidence");
    const acts = (report.delta_report && report.delta_report.acts) || [];
    const withPdf = acts.filter(a => a.pdf_file);
    if (withPdf.length > 0) {
        evidenceDiv.innerHTML = "<h3>Pièces probantes (PDF officiels)</h3>" + withPdf.map(a =>
            `<div style="margin-bottom: 0.4rem;">📄 <a href="/api/sync/evidence/${encodeURIComponent(a.pdf_file)}" target="_blank">${escapeHtml(a.pdf_file)}</a>` +
            `<br><small style="color:var(--text-muted)">${escapeHtml((a.title || "").substring(0, 110))} — SHA-256: ${escapeHtml((a.pdf_sha256 || "").substring(0, 16))}…</small></div>`
        ).join("");
        evidenceDiv.classList.remove("hidden");
    } else {
        evidenceDiv.innerHTML = "";
        evidenceDiv.classList.add("hidden");
    }

    panel.classList.remove("hidden");
}

// Display the active scheduler configuration under the source cards
async function fetchSyncConfig() {
    try {
        const response = await fetch("/api/sync/config");
        if (!response.ok) return;
        const cfg = await response.json();
        const info = document.getElementById("sync-schedule-info");
        const autoTxt = cfg.auto_enabled
            ? `⏰ Synchronisation automatique activée chaque jour à ${cfg.schedule_time}.`
            : "⏸️ Synchronisation automatique désactivée (sync.auto_enabled dans config.yaml).";
        const mailTxt = cfg.email_configured
            ? "Rapports envoyés par email (SMTP configuré)."
            : "Rapports disponibles dans l'application uniquement (SMTP non configuré).";
        info.textContent = `${autoTxt} ${mailTxt}`;
    } catch (e) {
        console.error("Error fetching sync config:", e);
    }
}

// Handle Delta Snapshot Comparison
async function handleCompareSnapshots(event) {
    event.preventDefault();
    
    const oldId = document.getElementById("compare-old-snap").value;
    const newId = document.getElementById("compare-new-snap").value;
    const btn = document.getElementById("submit-compare-btn");
    
    if (!oldId || !newId) {
        showToast("Sélectionnez deux snapshots différents pour comparer.", "error");
        return;
    }
    
    btn.disabled = true;
    btn.textContent = "Calcul des écarts...";
    
    try {
        const response = await fetch("/api/snapshots/compare", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                snapshot_old_id: oldId,
                snapshot_new_id: newId
            })
        });
        
        if (!response.ok) {
            const data = await response.json();
            showToast(`Erreur de comparaison : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        
        const report = await response.json();
        
        // Show delta result block
        document.getElementById("delta-results-card").classList.remove("hidden");
        
        // Populate Counts
        document.getElementById("delta-added-count").textContent = report.summary.added_count;
        document.getElementById("delta-removed-count").textContent = report.summary.removed_count;
        document.getElementById("delta-modified-count").textContent = report.summary.modified_count;
        
        // Populate ADDED details
        const addedItems = document.getElementById("delta-added-items");
        addedItems.innerHTML = "";
        if (report.details.added.length === 0) {
            addedItems.innerHTML = '<li>Aucun élément ajouté.</li>';
        } else {
            report.details.added.forEach(item => {
                const li = document.createElement("li");
                li.innerHTML = `🟢 ID: <code>${escapeHtml(item.id)}</code> | Nom: <strong>${escapeHtml(item.primary_name)}</strong> | Type: <span class="status-badge no_match">${item.type}</span>`;
                addedItems.appendChild(li);
            });
        }
        
        // Populate REMOVED details
        const removedItems = document.getElementById("delta-removed-items");
        removedItems.innerHTML = "";
        if (report.details.removed.length === 0) {
            removedItems.innerHTML = '<li>Aucun élément supprimé.</li>';
        } else {
            report.details.removed.forEach(item => {
                const li = document.createElement("li");
                li.innerHTML = `🔴 ID: <code>${escapeHtml(item.id)}</code> | Nom: <strong>${escapeHtml(item.primary_name)}</strong> | Type: <span class="status-badge alert">${item.type}</span>`;
                removedItems.appendChild(li);
            });
        }
        
        // Populate MODIFIED details
        const modifiedTbody = document.querySelector("#delta-modified-table tbody");
        modifiedTbody.innerHTML = "";
        if (report.details.modified.length === 0) {
            modifiedTbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted)">Aucune modification détectée</td></tr>';
        } else {
            report.details.modified.forEach(item => {
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td><code>${escapeHtml(item.id)}</code></td>
                    <td><strong>${escapeHtml(item.primary_name)}</strong></td>
                    <td><span class="status-badge warning">${escapeHtml(item.changes_detected.join(", "))}</span></td>
                    <td><pre class="pre-block" style="font-size:0.7rem;">${escapeHtml(JSON.stringify(item.before, null, 1))}</pre></td>
                    <td><pre class="pre-block" style="font-size:0.7rem;">${escapeHtml(JSON.stringify(item.after, null, 1))}</pre></td>
                `;
                modifiedTbody.appendChild(tr);
            });
        }
        
        // Expand Accordions automatically to show data
        document.getElementById("delta-added-list").classList.remove("hidden");
        document.getElementById("delta-removed-list").classList.remove("hidden");
        document.getElementById("delta-modified-list").classList.remove("hidden");
        
    } catch (e) {
        console.error("Comparison failed:", e);
        showToast("Erreur lors de la comparaison des versions.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Comparer les versions";
    }
}

// Met à jour le hash du cache moteur en sidebar (lit le cache, pas la base)
async function fetchWatchlistHash() {
    try {
        const response = await fetch("/api/watchlist");
        const data = await response.json();
        const hashEl = document.getElementById("sidebar-wl-hash");
        if (hashEl) {
            hashEl.textContent = data.hash ? data.hash.substring(0, 12) + "..." : "NONE";
            hashEl.title = data.hash;
        }
    } catch (e) {
        console.error("Error loading watchlist hash:", e);
    }
}

// Vue « Listés — Base de Données » : lecture en direct, paginée côté serveur
async function fetchWatchlist(page = 1) {
    wlCurrentPage = page;
    const searchEl = document.getElementById("wl-search-input");
    const listFilterEl = document.getElementById("wl-list-filter");
    const scopeFilterEl = document.getElementById("wl-scope-filter");

    const params = new URLSearchParams({ page: String(page), page_size: String(wlItemsPerPage) });
    const search = searchEl ? searchEl.value.trim() : "";
    if (search) params.set("search", search);
    if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
    params.set("scope", scopeFilterEl && scopeFilterEl.value ? scopeFilterEl.value : "production");

    try {
        const response = await fetch(`/api/watchlist/db?${params.toString()}`);
        const data = await response.json();
        if (!response.ok) {
            showToast(`Erreur de lecture de la base : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        renderWatchlistTable(data.items || [], data.page, data.total);
    } catch (e) {
        console.error("Error loading watchlist from database:", e);
    }
}

// Render Watchlist Table
function renderWatchlistTable(items, page = 1, total = 0) {
    const tbody = document.querySelector("#watchlist-table tbody");
    tbody.innerHTML = "";

    if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted)">Aucune entité en base pour ce périmètre</td></tr>';
        updatePaginationControls(0, 0);
        return;
    }

    const fragment = document.createDocumentFragment();

    items.forEach(item => {
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";
        tr.title = "Cliquez pour voir les détails de cette fiche";
        tr.onclick = () => showWatchlistDetails(item);
        
        // Format countries
        const countriesDict = item.countries || {};
        const citizenship = countriesDict.citizenship || [];
        const residence = countriesDict.residence || [];
        const birth = countriesDict.birth_country || [];
        const juris = countriesDict.jurisdiction_country || [];
        const allCountries = [...new Set([...citizenship, ...residence, ...birth, ...juris])].join(", ") || "-";
        
        const dobStr = (item.dates_of_birth || []).join(", ") || "-";
        const decStr = item.is_deceased ? "🪦 Mort" : "Vivant";
        
        let typeBadge = "";
        if (item.entity_type === "I") typeBadge = '<span class="status-badge no_match">I (Individu)</span>';
        else if (item.entity_type === "E") typeBadge = '<span class="status-badge alert">E (Entité)</span>';
        else if (item.entity_type === "V") typeBadge = '<span class="status-badge warning">V (Navire)</span>';
        else typeBadge = '<span class="status-badge">O (Autre)</span>';

        const excludedBadge = item.excluded ? ' <span class="status-badge alert" title="Entité exclue de la production lors de l\'homologation">EXCLUE</span>' : "";

        tr.innerHTML = `
            <td><code>${escapeHtml(item.entity_id)}</code></td>
            <td>${listTypeBadge(item._list_type)}</td>
            <td>${snapshotStatusBadge(item.snapshot_status)}${excludedBadge}</td>
            <td>${typeBadge}</td>
            <td>
                <strong>${escapeHtml(item.primary_name)}</strong>
                ${item.lei_number ? `<br><small style="color:var(--color-accent)">LEI: ${item.lei_number}</small>` : ''}
                ${item.imo_number ? `<br><small style="color:var(--color-accent)">IMO: ${item.imo_number}</small>` : ''}
            </td>
            <td>${escapeHtml(allCountries)}</td>
            <td>
                <small style="color:var(--text-muted)">DOB: ${escapeHtml(dobStr)}</small><br>
                <small>${decStr} | ${item.gender}</small>
            </td>
        `;
        fragment.appendChild(tr);
    });
    
    tbody.appendChild(fragment);
    updatePaginationControls(total, page);
}

// Filtres de la vue base de données : relance une requête serveur (debounce 300 ms)
function filterWatchlist() {
    clearTimeout(wlSearchDebounce);
    wlSearchDebounce = setTimeout(() => fetchWatchlist(1), 300);
}

// Update Watchlist Pagination UI Controls
function updatePaginationControls(totalItems, page) {
    const container = document.getElementById("watchlist-pagination");
    if (!container) return;
    
    if (totalItems === 0) {
        container.innerHTML = "";
        container.classList.add("hidden");
        return;
    }
    
    container.classList.remove("hidden");
    
    const totalPages = Math.ceil(totalItems / wlItemsPerPage);
    const startIndex = (page - 1) * wlItemsPerPage + 1;
    const endIndex = Math.min(page * wlItemsPerPage, totalItems);
    
    container.innerHTML = `
        <span class="pagination-info">
            Affichage de <strong>${startIndex}</strong> à <strong>${endIndex}</strong> sur <strong>${totalItems}</strong> entités
        </span>
        <div class="pagination-buttons">
            <button class="pagination-btn" id="wl-prev-btn" ${page === 1 ? "disabled" : ""} onclick="changeWatchlistPage(${page - 1})">Précédent</button>
            <span class="pagination-info" style="align-self: center; margin: 0 0.5rem;">Page ${page} / ${totalPages}</span>
            <button class="pagination-btn" id="wl-next-btn" ${page === totalPages ? "disabled" : ""} onclick="changeWatchlistPage(${page + 1})">Suivant</button>
        </div>
    `;
}

// Switch Watchlist Page
function changeWatchlistPage(newPage) {
    fetchWatchlist(newPage);
}

// Handle Real-Time Sandbox Screening
// Handle Real-Time Sandbox Screening
async function handleScreening(event) {
    event.preventDefault();
    
    const clientTypeSelect = document.getElementById("client-type").value;
    // Map select type to PP/PM for API compatibility
    const clientType = clientTypeSelect === "I" ? "PP" : "PM";
    const clientGender = document.getElementById("client-gender").value;
    
    // Get names
    const firstName = document.getElementById("client-firstname").value.trim();
    const lastName = document.getElementById("client-lastname").value.trim();
    const maidenName = document.getElementById("client-maidenname").value.trim();
    const companyName = document.getElementById("client-companyname").value.trim();
    const dob = document.getElementById("client-dob").value;
    
    const countriesStr = document.getElementById("client-countries").value.trim();
    const aliasesStr = document.getElementById("client-aliases").value.trim();
    
    const countriesList = countriesStr ? countriesStr.split(",").map(c => c.trim().toUpperCase()) : [];
    const aliasesList = aliasesStr ? aliasesStr.split(",").map(a => a.trim()) : [];
    
    // Hard Match fields
    const lei = document.getElementById("client-lei").value.trim();
    const imo = document.getElementById("client-imo").value.trim();
    const aircraft = document.getElementById("client-aircraft").value.trim();
    const passportNum = document.getElementById("client-passport-num").value.trim();
    const passportCountry = document.getElementById("client-passport-country").value.trim();
    const nationalId = document.getElementById("client-national-id").value.trim();
    
    // New fields
    const placeOfBirth = document.getElementById("client-place-of-birth").value.trim();
    const dateOfDeath = document.getElementById("client-date-of-death").value.trim();
    const address = document.getElementById("client-address").value.trim();
    const city = document.getElementById("client-city").value.trim();
    const state = document.getElementById("client-state").value.trim();
    const country = document.getElementById("client-country").value.trim();
    const origin = document.getElementById("client-origin").value.trim();
    const designation = document.getElementById("client-designation").value.trim();
    const altAddressesStr = document.getElementById("client-alternative-addresses").value.trim();
    const additionalInfo = document.getElementById("client-additional-informations").value.trim();
    
    const altAddressesList = altAddressesStr ? altAddressesStr.split(";").map(a => a.trim()) : [];
    
    const payload = {
        client_id: `SCREEN-${Math.floor(Math.random() * 10000)}`,
        client_type: clientType,
        client_first_name: clientType === "PP" ? firstName : "",
        client_last_name: clientType === "PP" ? lastName : "",
        client_maiden_name: clientType === "PP" ? maidenName : "",
        client_company_name: clientType === "PM" ? companyName : "",
        client_dob: dob || null,
        client_gender: clientGender,
        client_is_deceased: !!dateOfDeath,
        client_countries: {
            nationality: countriesList,
            residence: countriesList,
            birth_country: [],
            registration_country: clientType === "PM" ? countriesList : []
        },
        client_lei_number: lei || null,
        transaction_vessel_imo: imo || null,
        transaction_aircraft_registration: aircraft || null,
        client_passport_documents: passportNum ? [{ "number": passportNum, "issuing_country": passportCountry || "XX" }] : [],
        client_national_id_documents: nationalId ? [{ "number": nationalId, "issuing_country": "XX" }] : [],
        client_other_id_documents: [],
        
        // Add new fields to payload
        client_place_of_birth: placeOfBirth || null,
        client_address: address || null,
        client_city: city || null,
        client_state: state || null,
        client_country: country || null,
        client_origin: origin || null,
        client_designation: designation || null,
        client_additional_informations: additionalInfo || null,
        client_alternative_addresses: altAddressesList,
        client_date_of_death: dateOfDeath || null
    };

    // Périmètre des listes criblées (défaut : toutes ; restriction tracée dans l'audit)
    const restrictedLists = selectedScreeningLists("screening-lists-checkboxes");
    if (restrictedLists) payload.screening_lists = restrictedLists;
    
    const placeholder = document.getElementById("screening-results-placeholder");
    const resultsCard = document.getElementById("screening-results-card");
    const submitBtn = document.getElementById("submit-screen-btn");
    
    submitBtn.disabled = true;
    submitBtn.textContent = "Criblage en cours...";
    
    try {
        const response = await fetch("/api/screen", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const errData = await response.json();
            const errors = errData.detail && errData.detail.errors ? errData.detail.errors.join(", ") : JSON.stringify(errData);
            showToast(`Criblage rejeté par le Data Quality Gate : ${errors}`, "error");
            return;
        }

        const data = await response.json();

        placeholder.classList.add("hidden");
        resultsCard.classList.remove("hidden");

        renderScreeningResult(data);

        // Continuité criblage -> alerte : lien direct vers l'alerte ouverte
        const alertLink = document.getElementById("screening-alert-link");
        if (alertLink) {
            if (data.alert_id) {
                alertLink.innerHTML = `<button class="btn btn-sm btn-primary" onclick="switchTab('alerts'); openAlertModal(${data.alert_id});">🔎 Instruire l'alerte #${data.alert_id}</button>`;
                alertLink.classList.remove("hidden");
            } else {
                alertLink.classList.add("hidden");
                alertLink.innerHTML = "";
            }
        }
        // Rappel visuel d'un criblage à périmètre restreint (tracé dans l'audit)
        const restrictionNote = document.getElementById("screening-restriction-note");
        if (restrictionNote) {
            if (data.screening_lists && data.screening_lists !== "ALL") {
                restrictionNote.innerHTML = `<small style="color: var(--color-warning);">⚠️ Criblage restreint aux listes : <strong>${escapeHtml(data.screening_lists.map(listTypeLabel).join(", "))}</strong> (tracé dans le journal d'audit)</small>`;
                restrictionNote.classList.remove("hidden");
            } else {
                restrictionNote.classList.add("hidden");
                restrictionNote.innerHTML = "";
            }
        }
        // Une decision ALERT ouvre une alerte de travail : rafraichir le badge
        if (data.alert_id) fetchAlerts();
    } catch (e) {
        console.error("Error screening:", e);
        showToast("Erreur réseau lors de l'appel au moteur.", "error");
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Lancer le criblage";
    }
}

// Render Screening Result
function renderScreeningResult(data) {
    const report = data.client_quality_report;
    const best = data.best_match;
    const keys = data.blocking_keys_generated || [];
    
    // 1. Data Quality Gate report
    const qStatusText = document.querySelector("#quality-gate-status");
    const qAnomalies = document.getElementById("quality-gate-anomalies");
    qAnomalies.innerHTML = "";
    
    if (report.status === "OK") {
        qStatusText.innerHTML = '<span class="status-dot green"></span> Conforme (OK)';
    } else {
        qStatusText.innerHTML = '<span class="status-dot orange"></span> Qualité Dégradée';
    }
    
    if (report.warnings && report.warnings.length > 0) {
        report.warnings.forEach(w => {
            const div = document.createElement("div");
            div.className = "anomaly-item warning";
            div.textContent = w;
            qAnomalies.appendChild(div);
        });
    } else {
        const div = document.createElement("div");
        div.className = "anomaly-item";
        div.style.color = "var(--text-muted)";
        div.textContent = "Aucune anomalie de conformité de structure détectée.";
        qAnomalies.appendChild(div);
    }
    
    // 2. Metrics
    document.getElementById("metric-blocking-keys").textContent = keys.join(", ");
    document.getElementById("metric-blocking-keys").title = keys.join(", ");
    document.getElementById("metric-candidates").textContent = data.candidates_count;
    
    // 3. Gauge score & compliance status
    const statusBadge = document.getElementById("badge-compliance-status");
    const finalScore = best ? best.final_score : 0.0;
    const baseScore = best ? best.base_score : 0.0;
    
    document.getElementById("metric-base-score").textContent = `${baseScore.toFixed(1)}%`;
    document.getElementById("gauge-score-value").textContent = `${finalScore.toFixed(1)}%`;
    
    const progress = document.getElementById("gauge-progress");
    const circumference = 2 * Math.PI * 45;
    const offset = circumference - (finalScore / 100) * circumference;
    progress.style.strokeDasharray = `${circumference}`;
    progress.style.strokeDashoffset = `${offset}`;
    
    if (best && best.status === "ALERT") {
        statusBadge.textContent = "ALERT";
        statusBadge.className = "status-badge alert";
        progress.style.stroke = "var(--color-alert)";
    } else if (best && best.status === "WHITELISTED") {
        statusBadge.textContent = "SUPPRIMÉE PAR LISTE BLANCHE";
        statusBadge.className = "status-badge warning";
        progress.style.stroke = "var(--color-warning)";
    } else {
        statusBadge.textContent = "NO_MATCH";
        statusBadge.className = "status-badge no_match";
        progress.style.stroke = "var(--color-safe)";
    }
    // Transparence du seuil applique (variable par type de liste)
    if (best && best.cut_off_applied !== undefined) {
        const listType = (best.watchlist_entity && best.watchlist_entity._list_type) || "";
        statusBadge.title = `Seuil appliqué : ${best.cut_off_applied}%${listType ? " (" + listType + ")" : ""}`;
    }
    
    // 4. Decision Tree
    const hardMatchNotice = document.getElementById("hard-match-notice");
    const adjustmentsSection = document.getElementById("adjustments-section");
    
    if (!best) {
        hardMatchNotice.classList.add("hidden");
        adjustmentsSection.classList.remove("hidden");
        document.getElementById("best-match-cname").textContent = report.cleansed_name || "-";
        document.getElementById("best-match-wname").textContent = "Aucune fiche correspondante";
        document.getElementById("best-match-wid").textContent = "NONE";
        return;
    }
    
    document.getElementById("best-match-cname").textContent = best.best_client_name;
    document.getElementById("best-match-wname").textContent = best.best_watchlist_name;
    document.getElementById("best-match-wid").textContent = best.watchlist_entity.entity_id;
    
    if (best.hard_match_triggered) {
        hardMatchNotice.classList.remove("hidden");
        hardMatchNotice.textContent = `⚡ HARD MATCH : ${best.hard_match_details}`;
        adjustmentsSection.classList.add("hidden");
    } else {
        hardMatchNotice.classList.add("hidden");
        adjustmentsSection.classList.remove("hidden");
        
        const dob = best.adjustments.dob;
        const gender = best.adjustments.gender;
        const geo = best.adjustments.geography;
        
        formatAdjustment("adj-dob-val", "adj-dob-desc", dob.score, dob.description);
        formatAdjustment("adj-gender-val", "adj-gender-desc", gender.score, gender.description);
        formatAdjustment("adj-geo-val", "adj-geo-desc", geo.score, geo.description);
    }
}

function formatAdjustment(valId, descId, score, desc) {
    const valEl = document.getElementById(valId);
    const descEl = document.getElementById(descId);
    descEl.textContent = desc;
    if (score > 0) {
        valEl.textContent = `+${score}`;
        valEl.className = "adj-val plus";
    } else if (score < 0) {
        valEl.textContent = `${score}`;
        valEl.className = "adj-val minus";
    } else {
        valEl.textContent = `0`;
        valEl.className = "adj-val";
    }
}

// Load sample client CSV records into Batch Area
function loadSampleBatchJSON() {
    const samples = [
        {
            client_id: "BATCH-CLI-01",
            client_type: "PP",
            client_first_name: "VLADIMIR",
            client_last_name: "PUTIN",
            client_dob: "1952-10-07",
            client_gender: "M",
            client_countries: { nationality: ["RU"], residence: ["RU"] }
        },
        {
            client_id: "BATCH-CLI-02",
            client_type: "PP",
            client_first_name: "HANZ",
            client_last_name: "MUTLER",
            client_dob: "1975-12-15",
            client_gender: "M",
            client_countries: { nationality: ["DE"], residence: ["FR"] }
        },
        {
            client_id: "BATCH-CLI-03",
            client_type: "PM",
            client_company_name: "SOCIETE GENERALE",
            client_countries: { residence: ["FR"], registration_country: ["FR"] },
            client_lei_number: "96950058N5D982K5G550" // Trigger Hard Match corporate
        },
        {
            client_id: "BATCH-CLI-04",
            client_type: "PP",
            client_first_name: "ALEXANDRA",
            client_last_name: "SMITH",
            client_maiden_name: "MULLER",
            client_dob: "1988-04-23",
            client_gender: "F",
            client_countries: { nationality: ["US"] }
        }
    ];
    document.getElementById("batch-json-input").value = JSON.stringify(samples, null, 2);
}

// Run batch simulation in loop
async function runBatchScreening() {
    const text = document.getElementById("batch-json-input").value.trim();
    if (!text) {
        showToast("Saisissez des clients.", "error");
        return;
    }
    
    let clients = [];
    try {
        clients = JSON.parse(text);
        if (!Array.isArray(clients)) {
            showToast("Format attendu : tableau JSON.", "error");
            return;
        }
    } catch (e) {
        showToast(`Erreur JSON : ${e.message}`, "error");
        return;
    }

    // Périmètre des listes criblées, appliqué à chaque client du lot
    const batchRestriction = selectedScreeningLists("batch-lists-checkboxes");
    
    const btn = document.getElementById("run-batch-btn");
    btn.disabled = true;
    btn.textContent = "Exécution...";
    
    const resultsContainer = document.getElementById("batch-results-container");
    const tbody = document.querySelector("#batch-results-table tbody");
    tbody.innerHTML = "";
    
    let alertsCount = 0;
    
    try {
        for (const client of clients) {
            const payload = batchRestriction ? { ...client, screening_lists: batchRestriction } : client;
            const response = await fetch("/api/screen", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            
            const tr = document.createElement("tr");
            
            if (!response.ok) {
                const errData = await response.json();
                const errors = errData.detail && errData.detail.errors ? errData.detail.errors.join(", ") : "Rejeté";
                
                tr.innerHTML = `
                    <td><code>${escapeHtml(client.client_id || "-")}</code></td>
                    <td><strong>${escapeHtml(client.client_company_name || client.client_last_name || "-")}</strong></td>
                    <td colspan="3" style="color:var(--color-alert)"><strong>REJETE (Quality Gate)</strong> : ${escapeHtml(errors)}</td>
                    <td><span class="status-badge alert">REJECT</span></td>
                `;
                tbody.appendChild(tr);
                continue;
            }
            
            const data = await response.json();
            const best = data.best_match;
            
            if (best && best.status === "ALERT") {
                alertsCount++;
                tr.innerHTML = `
                    <td><code>${escapeHtml(client.client_id || "-")}</code></td>
                    <td><strong>${escapeHtml(best.best_client_name)}</strong></td>
                    <td><code>${escapeHtml(best.watchlist_entity.entity_id)}</code></td>
                    <td><strong>${escapeHtml(best.best_watchlist_name)}</strong></td>
                    <td style="color:var(--color-alert); font-weight:700">${best.final_score.toFixed(1)}%</td>
                    <td><span class="status-badge alert">ALERT</span>${data.alert_id ? ` <a href="#" onclick="switchTab('alerts'); openAlertModal(${data.alert_id}); return false;" style="font-size: 0.75rem;">🔎 #${data.alert_id}</a>` : ""}</td>
                `;
            } else {
                tr.innerHTML = `
                    <td><code>${escapeHtml(client.client_id || "-")}</code></td>
                    <td><strong>${escapeHtml(client.client_company_name || client.client_last_name || "-")}</strong></td>
                    <td>${best ? `<code>${escapeHtml(best.watchlist_entity.entity_id)}</code>` : "-"}</td>
                    <td>${best ? escapeHtml(best.best_watchlist_name) : "Aucun match (Bloqué)"}</td>
                    <td>${best ? `${best.final_score.toFixed(1)}%` : "0.0%"}</td>
                    <td><span class="status-badge no_match">NO_MATCH</span></td>
                `;
            }
            tbody.appendChild(tr);
        }
        
        document.getElementById("batch-alerts-count").textContent = alertsCount;
        resultsContainer.classList.remove("hidden");
        if (batchRestriction) {
            showToast(`Criblage de masse restreint aux listes : ${batchRestriction.map(listTypeLabel).join(", ")} (tracé dans l'audit).`, "info");
        }
        if (alertsCount) fetchAlerts();
    } catch (e) {
        console.error("Batch failure:", e);
        showToast("Erreur lors de l'exécution du criblage de masse.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Lancer le Batch Screening";
        fetchAuditHistory();
    }
}

// Fetch Audit history
let auditCurrentPage = 1;

async function fetchAuditHistory(page = null) {
    try {
        if (page) auditCurrentPage = page;
        const params = new URLSearchParams({ page: String(auditCurrentPage), page_size: "50" });
        const listFilterEl = document.getElementById("audit-list-filter");
        if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
        const statusFilterEl = document.getElementById("audit-status-filter");
        if (statusFilterEl && statusFilterEl.value) params.set("status", statusFilterEl.value);

        const response = await fetch(`/api/history?${params}`);
        const data = await response.json();
        auditHistory = data.items || [];
        renderAuditHistoryTable(auditHistory);
        renderAuditPagination(data.total || 0, data.page || 1, data.page_size || 50);
    } catch (e) {
        console.error("Error loading history:", e);
    }
}

function renderAuditPagination(total, page, pageSize) {
    const container = document.getElementById("audit-pagination");
    if (!container) return;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    container.innerHTML = `
        <span class="pagination-info">${total} décision(s) — page ${page} / ${totalPages}</span>
        <button class="pagination-btn" ${page <= 1 ? "disabled" : ""} onclick="fetchAuditHistory(${page - 1})">Précédent</button>
        <button class="pagination-btn" ${page >= totalPages ? "disabled" : ""} onclick="fetchAuditHistory(${page + 1})">Suivant</button>
    `;
}

function renderAuditHistoryTable(logs) {
    const tbody = document.querySelector("#audit-table tbody");
    tbody.innerHTML = "";

    if (logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted)">Aucune décision pour ce filtre</td></tr>';
        return;
    }

    logs.forEach(log => {
        const dateStr = new Date(log.timestamp + "Z").toLocaleString("fr-FR");
        const tr = document.createElement("tr");

        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(log.client_name)}</strong> <span class="status-badge">${log.client_type}</span></td>
            <td><code>${escapeHtml(log.watchlist_id)}</code> - <strong>${escapeHtml(log.watchlist_name)}</strong></td>
            <td>${listTypeBadge(log.list_type)}</td>
            <td style="font-weight: 700; color: ${log.status === "ALERT" ? "var(--color-alert)" : "var(--color-safe)"}">${log.final_score.toFixed(1)}%</td>
            <td><span class="status-badge ${log.status === "ALERT" ? "alert" : "no_match"}">${log.status}</span></td>
            <td><button class="btn btn-secondary" style="font-size:0.75rem; padding: 0.25rem 0.5rem;" onclick="viewAuditLogDetail(${log.id})">Inspecter</button></td>
        `;
        tbody.appendChild(tr);
    });
}

function viewAuditLogDetail(logId) {
    const log = auditHistory.find(item => item.id === logId);
    if (!log) return;
    
    const modal = document.getElementById("audit-modal");
    const content = document.getElementById("modal-audit-details");
    const dateStr = new Date(log.timestamp + "Z").toLocaleString("fr-FR");
    
    let tree = typeof log.decision_tree === "string" ? JSON.parse(log.decision_tree) : log.decision_tree;
    let configState = typeof log.config_state === "string" ? JSON.parse(log.config_state) : log.config_state;
    
    let adjHtml = "";
    if (tree.hard_match_triggered) {
        adjHtml = `<p style="color:var(--color-warning); font-weight:700">⚡ HARD MATCH déclenché : ${tree.hard_match_details}</p>`;
    } else if (tree && tree.adjustments) {
        adjHtml = `
            <ul>
                <li>Date de Naissance : <strong>${tree.adjustments.dob.score} points</strong> (${tree.adjustments.dob.description})</li>
                <li>Genre : <strong>${tree.adjustments.gender.score} points</strong> (${tree.adjustments.gender.description})</li>
                <li>Géographie : <strong>${tree.adjustments.geography.score} points</strong> (${tree.adjustments.geography.description})</li>
            </ul>
        `;
    }
    
    content.innerHTML = `
        <div class="modal-section">
            <h4>Informations Générales</h4>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 1rem; font-size: 0.85rem">
                <div>Horodatage : <strong>${escapeHtml(dateStr)}</strong></div>
                <div>Statut : <strong style="color:${log.status === 'ALERT' ? 'var(--color-alert)' : 'var(--color-safe)'}">${log.status}</strong></div>
                <div>Watchlist : <strong><code>${escapeHtml(log.watchlist_id)}</code> - ${escapeHtml(log.watchlist_name)}</strong></div>
                <div>Client : <strong>${escapeHtml(log.client_name)} (${log.client_type})</strong></div>
                <div>Liste d'origine : ${listTypeBadge(log.list_type)}</div>
                ${tree && tree.screening_lists_restriction && tree.screening_lists_restriction !== "ALL"
                    ? `<div style="color: var(--color-warning);">⚠️ Criblage restreint : <strong>${escapeHtml(tree.screening_lists_restriction.map(listTypeLabel).join(", "))}</strong></div>`
                    : ""}
            </div>
        </div>
        
        <div class="modal-section">
            <h4>Analyse Algorithmique</h4>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 1rem; font-size: 0.85rem">
                <div>Score Textuel : <strong>${log.base_score.toFixed(1)}%</strong></div>
                <div>Score Final : <strong>${log.final_score.toFixed(1)}%</strong></div>
            </div>
            <div style="margin-top:0.75rem; font-size:0.85rem">
                <h5>Raison / Détail linéaire :</h5>
                ${adjHtml}
            </div>
        </div>
        
        <div class="modal-section">
            <h4>Source & Intégrité</h4>
            <div style="font-size:0.85rem">
                Version Listes : <strong>${escapeHtml(log.watchlist_version)}</strong><br>
                SHA-256 Hash : <code class="hash-badge" style="display:block; margin-top:0.25rem">${escapeHtml(log.watchlist_hash)}</code>
            </div>
        </div>
        
        <div class="modal-section">
            <h4>Snapshot Configuration</h4>
            <pre class="pre-block">${escapeHtml(JSON.stringify(configState, null, 2))}</pre>
        </div>
    `;
    
    modal.classList.remove("hidden");
    modal.style.display = "flex";
}

function closeAuditModal() {
    const modal = document.getElementById("audit-modal");
    modal.classList.add("hidden");
    modal.style.display = "none";
}

// (fetchConfig est définie en tête de fichier — le doublon sans gestion du 401 a été supprimé.)

// Escape HTML utility
const def_escape = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
};
function escapeHtml(text) {
    if (text === null || text === undefined) return "";
    return String(text).replace(/[&<>"']/g, function(m) { return def_escape[m]; });
}

// (Le gestionnaire global de clic sur l'arrière-plan des modales est défini plus bas —
//  l'ancien doublon, qui ne gérait que la modale d'audit, a été supprimé.)

// Purge Failed/Processing Snapshots
async function purgeFailedSnapshots() {
    if (!await confirmDialog("Voulez-vous vraiment purger tous les snapshots et entités en erreur ou en cours de traitement ?\nCette action est irréversible.", { title: "Purger les imports erronés", danger: true, confirmLabel: "Purger" })) {
        return;
    }
    
    const btn = document.getElementById("btn-purge-snapshots");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = "⌛ Purge en cours...";
    }
    
    try {
        const response = await fetch("/api/snapshots/purge", {
            method: "POST"
        });
        
        if (!response.ok) {
            const data = await response.json();
            showToast(`Erreur lors de la purge : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        
        const data = await response.json();
        showToast(`Purge terminée : ${data.message}`, "success");
        fetchSnapshots();
        fetchWatchlist();
        fetchWatchlistHash();
    } catch (e) {
        console.error("Purge failed:", e);
        showToast("Erreur réseau lors de l'appel à la purge.", "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = "🗑️ Purger les imports erronés";
        }
    }
}

// Watchlist details Modal trigger
// Fiche affichée dans la modale de détails (support du mode édition)
let wlDetailsItem = null;

// Même détection de date que le back : ISO (YYYY-MM-DD) ou JJ/MM/AAAA
const OFFICIAL_REF_DATE_RE = /(\d{4}-\d{2}-\d{2}|\d{2}\/\d{2}\/\d{4})/;

// L'édition est réservée aux reviewers/admins, sur les fiches en production
// (la vue Base de Données fournit id + snapshot_status ; le cache moteur non)
function canEditWatchlistEntity(item) {
    const roles = userRoles(currentUser);
    if (!roles.includes("admin") && !roles.includes("reviewer")) return false;
    return Boolean(item && item.id && item.snapshot_status === "READY" && !item.excluded);
}

function showWatchlistDetails(item) {
    wlDetailsItem = item;
    const modal = document.getElementById("details-modal");
    const title = document.getElementById("modal-title");
    const body = document.getElementById("modal-body");

    title.textContent = `Détails de l'Entité : ${item.entity_id}`;

    const altAddrs = Array.isArray(item.alternative_addresses) ? item.alternative_addresses.join("; ") : (item.alternative_addresses || "-");
    const citizenship = item.countries?.citizenship?.join(", ") || "-";
    const residence = item.countries?.residence?.join(", ") || "-";

    let highAliases = [];
    let lowAliases = [];
    if (item.aliases) {
        if (typeof item.aliases === "object" && !Array.isArray(item.aliases)) {
            highAliases = item.aliases.high_priority || [];
            lowAliases = item.aliases.low_priority || [];
        } else if (Array.isArray(item.aliases)) {
            highAliases = item.aliases;
        }
    }
    const aliasesStr = [...highAliases, ...lowAliases].join(", ") || "-";

    const modifiedStr = item.modified_by
        ? `@${item.modified_by} — ${item.modified_at ? new Date(item.modified_at + (item.modified_at.endsWith("Z") ? "" : "Z")).toLocaleString("fr-FR") : ""}`
        : "-";
    const editBar = canEditWatchlistEntity(item)
        ? `<div style="display:flex; justify-content:flex-end; margin-bottom: 0.75rem;">
               <button class="btn-secondary" onclick="showWatchlistEntityEditForm()">✏️ Modifier la fiche</button>
           </div>`
        : "";

    body.innerHTML = `
        ${editBar}
        <div class="details-grid">
            <div class="details-item"><strong>Nom Principal / Label</strong><span>${escapeHtml(item.primary_name || "-")}</span></div>
            <div class="details-item"><strong>Type d'Entité</strong><span>${escapeHtml(item.entity_type || "-")}</span></div>
            <div class="details-item"><strong>Genre</strong><span>${escapeHtml(item.gender || "-")}</span></div>
            <div class="details-item"><strong>Prénom</strong><span>${escapeHtml(item.individual_name_parsed?.first_name || "-")}</span></div>
            <div class="details-item"><strong>Nom</strong><span>${escapeHtml(item.individual_name_parsed?.last_name || "-")}</span></div>
            <div class="details-item"><strong>Nom de Jeune Fille</strong><span>${escapeHtml(item.individual_name_parsed?.maiden_name || "-")}</span></div>
            <div class="details-item"><strong>Nationalité (Pays)</strong><span>${escapeHtml(citizenship)}</span></div>
            <div class="details-item"><strong>Résidence</strong><span>${escapeHtml(residence)}</span></div>
            <div class="details-item"><strong>Lieu de Naissance</strong><span>${escapeHtml(item.place_of_birth || "-")}</span></div>
            <div class="details-item"><strong>Date de Naissance</strong><span>${escapeHtml((item.dates_of_birth || []).join(", ") || "-")}</span></div>
            <div class="details-item"><strong>Adresse</strong><span>${escapeHtml(item.address || "-")}</span></div>
            <div class="details-item"><strong>Ville</strong><span>${escapeHtml(item.city || "-")}</span></div>
            <div class="details-item"><strong>État / Région</strong><span>${escapeHtml(item.state || "-")}</span></div>
            <div class="details-item"><strong>Pays</strong><span>${escapeHtml(item.country || "-")}</span></div>
            <div class="details-item"><strong>Date de Décès</strong><span>${escapeHtml(item.date_of_death || "-")}</span></div>
            <div class="details-item"><strong>Origine / Source</strong><span>${escapeHtml(item.origin || "-")}</span></div>
            <div class="details-item"><strong>Fonction / Désignation</strong><span>${escapeHtml(item.designation || "-")}</span></div>
            <div class="details-item"><strong>Informations Additionnelles</strong><span>${escapeHtml(item.additional_informations || "-")}</span></div>
            <div class="details-item" style="grid-column: span 2;"><strong>Référence Officielle</strong><span>${escapeHtml(item.official_reference || "-")}</span></div>
            <div class="details-item" style="grid-column: span 2;"><strong>Motifs de la Désignation</strong><span>${escapeHtml(item.designation_reasons || "-")}</span></div>
            <div class="details-item" style="grid-column: span 2;"><strong>Adresses Alternatives</strong><span>${escapeHtml(altAddrs)}</span></div>
            <div class="details-item" style="grid-column: span 2;"><strong>Alias</strong><span>${escapeHtml(aliasesStr)}</span></div>
            <div class="details-item"><strong>LEI (Legal Entity Identifier)</strong><span>${escapeHtml(item.lei_number || "-")}</span></div>
            <div class="details-item"><strong>IMO Code (Navire)</strong><span>${escapeHtml(item.imo_number || "-")}</span></div>
            <div class="details-item"><strong>Tail Number (Immatriculation Aéronef)</strong><span>${escapeHtml(item.aircraft_tail_number || "-")}</span></div>
            <div class="details-item"><strong>Dernière Modification Manuelle</strong><span>${escapeHtml(modifiedStr)}</span></div>
        </div>
        <div id="entity-changes-section"></div>
    `;

    modal.classList.remove("hidden");
    if (item.id) loadEntityChanges(item.id);
}

// Libellés français des champs pour le journal des modifications
const ENTITY_FIELD_LABELS = {
    primary_name: "Nom principal", entity_type: "Type d'entité", gender: "Genre",
    individual_name_parsed: "Prénom / Nom / Nom de jeune fille", dates_of_birth: "Dates de naissance",
    countries: "Pays rattachés", aliases: "Alias", place_of_birth: "Lieu de naissance",
    address: "Adresse", city: "Ville", state: "État / Région", country: "Pays",
    date_of_death: "Date de décès", is_deceased: "Décédé", origin: "Origine / Source",
    designation: "Fonction / Désignation", designation_reasons: "Motifs de la désignation",
    additional_informations: "Informations additionnelles", official_reference: "Référence officielle",
    alternative_addresses: "Adresses alternatives", lei_number: "LEI", imo_number: "IMO",
    aircraft_tail_number: "Tail Number",
};

// Historique des modifications manuelles de la fiche (journal immuable)
async function loadEntityChanges(entityPk) {
    const container = document.getElementById("entity-changes-section");
    if (!container) return;
    try {
        const response = await fetch(`/api/watchlist/entity/${entityPk}/changes`);
        if (!response.ok) return;
        const data = await response.json();
        const items = data.items || [];
        if (!items.length) return;
        container.innerHTML = `
            <h3 style="margin-top: 1.25rem;">Historique des modifications (${items.length})</h3>
            <div class="table-container" style="max-height: 220px; overflow-y: auto;">
                <table>
                    <thead><tr><th>Quand</th><th>Par</th><th>Champ</th><th>Avant</th><th>Après</th></tr></thead>
                    <tbody>
                        ${items.map(c => `
                            <tr>
                                <td><small>${c.changed_at ? new Date(c.changed_at + "Z").toLocaleString("fr-FR") : "-"}</small></td>
                                <td><small>@${escapeHtml(c.changed_by || "")}</small></td>
                                <td><small>${escapeHtml(ENTITY_FIELD_LABELS[c.field] || c.field)}</small></td>
                                <td><small style="color:var(--text-muted)">${escapeHtml(c.old_value ?? "∅")}</small></td>
                                <td><small>${escapeHtml(c.new_value ?? "∅")}</small></td>
                            </tr>`).join("")}
                    </tbody>
                </table>
            </div>
        `;
    } catch (e) {
        console.error("Error loading entity changes:", e);
    }
}

// ------------------ ÉDITION D'UNE FICHE LISTÉE (PATCH) ------------------

function _editInput(id, label, value, span2 = false) {
    return `
        <div class="details-item"${span2 ? ' style="grid-column: span 2;"' : ""}>
            <strong>${label}</strong>
            <input type="text" id="edit-ent-${id}" value="${escapeHtml(value ?? "")}">
        </div>`;
}

function showWatchlistEntityEditForm() {
    const item = wlDetailsItem;
    if (!item || !canEditWatchlistEntity(item)) return;
    const body = document.getElementById("modal-body");
    const parsed = item.individual_name_parsed || {};
    const countries = item.countries || {};
    const aliases = (item.aliases && !Array.isArray(item.aliases)) ? item.aliases : { high_priority: [], low_priority: [] };

    const syncWarning = item.snapshot_id !== "manual-watchlist"
        ? `<p class="section-desc" style="color: var(--color-warning, #b8860b);">⚠️ Fiche issue d'une source synchronisée : la prochaine synchronisation de la liste remplacera ce snapshot et écrasera ces modifications. Le journal des modifications, lui, est conservé.</p>`
        : "";

    body.innerHTML = `
        <p class="section-desc">Modification de la fiche <code>${escapeHtml(item.entity_id)}</code> — chaque champ modifié est tracé (qui, quand, avant → après).</p>
        ${syncWarning}
        <div class="details-grid">
            ${_editInput("primary_name", "Nom Principal / Label", item.primary_name, true)}
            <div class="details-item"><strong>Type d'Entité</strong>
                <select id="edit-ent-entity_type">
                    ${["I", "E", "V", "O"].map(t => `<option value="${t}" ${item.entity_type === t ? "selected" : ""}>${t}</option>`).join("")}
                </select>
            </div>
            <div class="details-item"><strong>Genre</strong>
                <select id="edit-ent-gender">
                    ${["M", "F", "U"].map(g => `<option value="${g}" ${(item.gender || "U") === g ? "selected" : ""}>${g}</option>`).join("")}
                </select>
            </div>
            ${_editInput("first_name", "Prénom", parsed.first_name)}
            ${_editInput("last_name", "Nom", parsed.last_name)}
            ${_editInput("maiden_name", "Nom de Jeune Fille", parsed.maiden_name)}
            ${_editInput("citizenship", "Nationalités (codes, virgules)", (countries.citizenship || []).join(", "))}
            ${_editInput("residence", "Résidences (codes, virgules)", (countries.residence || []).join(", "))}
            ${_editInput("place_of_birth", "Lieu de Naissance", item.place_of_birth)}
            ${_editInput("dates_of_birth", "Dates de Naissance (AAAA-MM-JJ, virgules)", (item.dates_of_birth || []).join(", "))}
            ${_editInput("address", "Adresse", item.address, true)}
            ${_editInput("city", "Ville", item.city)}
            ${_editInput("state", "État / Région", item.state)}
            ${_editInput("country", "Pays", item.country)}
            ${_editInput("date_of_death", "Date de Décès", item.date_of_death)}
            ${_editInput("origin", "Origine / Source", item.origin)}
            ${_editInput("designation", "Fonction / Désignation", item.designation)}
            ${_editInput("additional_informations", "Informations Additionnelles", item.additional_informations, true)}
            ${_editInput("official_reference", "Référence Officielle", item.official_reference, true)}
            <div class="details-item" style="grid-column: span 2;" id="edit-touch-ref-wrapper" ${OFFICIAL_REF_DATE_RE.test(item.official_reference || "") ? "" : 'hidden'}>
                <label style="display:flex; align-items:center; gap:0.4rem; cursor:pointer;">
                    <input type="checkbox" id="edit-touch-ref-date" checked>
                    Mettre à jour la date contenue dans la référence officielle à la date du jour
                </label>
            </div>
            ${_editInput("designation_reasons", "Motifs de la Désignation", item.designation_reasons, true)}
            ${_editInput("alternative_addresses", "Adresses Alternatives (point-virgules)", (item.alternative_addresses || []).join("; "), true)}
            ${_editInput("aliases_high", "Alias forts (virgules)", (aliases.high_priority || []).join(", "), true)}
            ${_editInput("aliases_low", "Alias faibles (virgules)", (aliases.low_priority || []).join(", "), true)}
            ${_editInput("lei_number", "LEI", item.lei_number)}
            ${_editInput("imo_number", "IMO", item.imo_number)}
            ${_editInput("aircraft_tail_number", "Tail Number", item.aircraft_tail_number)}
        </div>
        <div style="display:flex; justify-content:flex-end; gap:0.5rem; margin-top:1rem;">
            <button class="btn-secondary" onclick="showWatchlistDetails(wlDetailsItem)">Annuler</button>
            <button class="btn-primary" id="edit-ent-save-btn" onclick="saveWatchlistEntityEdits()">💾 Enregistrer les modifications</button>
        </div>
    `;

    // La case « date du jour » n'a de sens que si la référence contient une date
    const refInput = document.getElementById("edit-ent-official_reference");
    refInput.addEventListener("input", () => {
        document.getElementById("edit-touch-ref-wrapper").hidden = !OFFICIAL_REF_DATE_RE.test(refInput.value);
    });
}

function _editValue(id) {
    const el = document.getElementById(`edit-ent-${id}`);
    return el ? el.value.trim() : "";
}

function _splitList(raw, separator) {
    return raw.split(separator).map(v => v.trim()).filter(Boolean);
}

async function saveWatchlistEntityEdits() {
    const item = wlDetailsItem;
    if (!item) return;
    const patch = {};

    const scalarFields = [
        "primary_name", "entity_type", "gender", "place_of_birth", "address", "city",
        "state", "country", "date_of_death", "origin", "designation",
        "designation_reasons", "additional_informations", "official_reference",
        "lei_number", "imo_number", "aircraft_tail_number",
    ];
    for (const field of scalarFields) {
        const newValue = _editValue(field) || null;
        if (newValue !== (item[field] || null)) patch[field] = newValue;
    }

    const parsed = item.individual_name_parsed || {};
    const newParsed = {
        first_name: _editValue("first_name"),
        last_name: _editValue("last_name"),
        maiden_name: _editValue("maiden_name"),
    };
    if (newParsed.first_name !== (parsed.first_name || "") || newParsed.last_name !== (parsed.last_name || "") || newParsed.maiden_name !== (parsed.maiden_name || "")) {
        patch.individual_name_parsed = newParsed;
    }

    const newDobs = _splitList(_editValue("dates_of_birth"), ",");
    if (JSON.stringify(newDobs) !== JSON.stringify(item.dates_of_birth || [])) patch.dates_of_birth = newDobs;

    const countries = item.countries || {};
    const newCountries = {
        ...countries,
        citizenship: _splitList(_editValue("citizenship"), ",").map(c => c.toUpperCase()),
        residence: _splitList(_editValue("residence"), ",").map(c => c.toUpperCase()),
    };
    if (JSON.stringify(newCountries) !== JSON.stringify(countries)) patch.countries = newCountries;

    const aliases = (item.aliases && !Array.isArray(item.aliases)) ? item.aliases : { high_priority: [], low_priority: [] };
    const newAliases = {
        high_priority: _splitList(_editValue("aliases_high"), ","),
        low_priority: _splitList(_editValue("aliases_low"), ","),
    };
    if (JSON.stringify(newAliases) !== JSON.stringify({ high_priority: aliases.high_priority || [], low_priority: aliases.low_priority || [] })) {
        patch.aliases = newAliases;
    }

    const newAltAddrs = _splitList(_editValue("alternative_addresses"), ";");
    if (JSON.stringify(newAltAddrs) !== JSON.stringify(item.alternative_addresses || [])) patch.alternative_addresses = newAltAddrs;

    const touchWrapper = document.getElementById("edit-touch-ref-wrapper");
    const touchBox = document.getElementById("edit-touch-ref-date");
    const touchDate = Boolean(touchWrapper && !touchWrapper.hidden && touchBox && touchBox.checked);

    if (!Object.keys(patch).length && !touchDate) {
        showToast("Aucune modification à enregistrer.", "info");
        return;
    }
    patch.touch_official_reference_date = touchDate;

    const btn = document.getElementById("edit-ent-save-btn");
    btn.disabled = true;
    try {
        const response = await fetch(`/api/watchlist/entity/${item.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(patch),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast(`Erreur : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        showToast(data.message + (data.official_reference_date_touched ? " Référence officielle datée du jour." : ""), "success");
        // Ré-affiche la fiche à jour et rafraîchit la vue + le hash du cache recriblé
        showWatchlistDetails(data.entity);
        fetchWatchlist(wlCurrentPage);
        fetchWatchlistHash();
    } catch (e) {
        console.error("Error patching entity:", e);
        showToast("Erreur réseau de communication.", "error");
    } finally {
        btn.disabled = false;
    }
}

function closeDetailsModal() {
    document.getElementById("details-modal").classList.add("hidden");
}

// =========================================================================
// GESTION DES UTILISATEURS & PROFIL (ADMIN / ME)
// =========================================================================

let registeredUsers = [];

async function fetchUsersList() {
    try {
        const response = await fetch("/api/users");
        if (response.status === 401 || response.status === 403) {
            showToast("Accès refusé. Droits d'administrateur requis.", "error");
            return;
        }
        registeredUsers = await response.json();
        renderUsersTable(registeredUsers);
    } catch (err) {
        console.error("Failed to fetch users list:", err);
    }
}

function renderUsersTable(users) {
    const tbody = document.getElementById("users-table-body");
    if (!tbody) return;
    
    if (!users || users.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Aucun utilisateur trouvé.</td></tr>`;
        return;
    }

    tbody.innerHTML = users.map(u => {
        // Roles empilables : un badge par role ("reviewer,user" -> 2 badges)
        const badgeStyles = {
            admin: 'background: rgba(99, 102, 241, 0.2); border: 1px solid rgba(99, 102, 241, 0.4); color: #a5b4fc; font-weight: 700;',
            reviewer: 'background: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.3); color: #fbbf24; font-weight: 600;',
            user: 'background: rgba(14, 165, 233, 0.15); border: 1px solid rgba(14, 165, 233, 0.3); color: #38bdf8; font-weight: 600;'
        };
        const badgeLabels = { admin: "ADMINISTRATEUR", reviewer: "RÉVISEUR", user: "ANALYSTE USER" };
        const roleBadge = userRoles(u).map(r =>
            `<span style="${badgeStyles[r] || badgeStyles.user} padding: 0.25rem 0.6rem; border-radius: 12px; font-size: 0.75rem; margin-right: 4px; display: inline-block;">${badgeLabels[r] || escapeHtml(r.toUpperCase())}</span>`
        ).join("") || `<span style="${badgeStyles.user} padding: 0.25rem 0.6rem; border-radius: 12px; font-size: 0.75rem;">ANALYSTE USER</span>`;

        const dateFormatted = u.created_at ? new Date(u.created_at).toLocaleDateString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : "N/A";
        const isSelf = currentUser && currentUser.id === u.id;

        return `
            <tr>
                <td style="font-weight: bold; color: var(--text-secondary);">#${u.id}</td>
                <td><strong style="color: var(--text-primary);">@${escapeHtml(u.username)}</strong> ${isSelf ? '<span style="font-size: 0.7rem; background: rgba(34, 197, 94, 0.2); color: #4ade80; padding: 2px 6px; border-radius: 4px; margin-left: 4px;">VOUS</span>' : ''}</td>
                <td>${escapeHtml(u.full_name || "—")}</td>
                <td>${roleBadge}</td>
                <td style="font-size: 0.85rem; color: var(--text-muted);">${dateFormatted}</td>
                <td style="text-align: right;">
                    <button class="btn btn-sm" onclick="openEditUserModal(${u.id})" style="background: rgba(255, 255, 255, 0.08); margin-right: 6px;">✏️ Éditer</button>
                    ${!isSelf ? `<button class="btn btn-sm" onclick="deleteUserAccount(${u.id}, '${escapeHtml(u.username)}')" style="background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3);">🗑️ Supprimer</button>` : ''}
                </td>
            </tr>
        `;
    }).join("");
}

function openCreateUserModal() {
    document.getElementById("user-modal-title").textContent = "Créer un Utilisateur";
    document.getElementById("user-edit-id").value = "";
    document.getElementById("user-input-username").value = "";
    document.getElementById("user-input-username").disabled = false;
    document.getElementById("user-input-fullname").value = "";
    document.getElementById("user-input-role").value = "user";
    document.getElementById("user-input-password").value = "";
    document.getElementById("user-input-password").required = true;
    document.getElementById("user-password-hint").style.display = "none";
    
    document.getElementById("user-modal").classList.remove("hidden");
}

function openEditUserModal(userId) {
    const user = registeredUsers.find(u => u.id === userId);
    if (!user) return;

    document.getElementById("user-modal-title").textContent = `Éditer le Compte @${user.username}`;
    document.getElementById("user-edit-id").value = user.id;
    document.getElementById("user-input-username").value = user.username;
    document.getElementById("user-input-username").disabled = false;
    document.getElementById("user-input-fullname").value = user.full_name || "";
    const roleSelect = document.getElementById("user-input-role");
    roleSelect.value = user.role;
    if (roleSelect.value !== user.role) {
        // Combinaison de roles sans option predefinie : repli sur le role dominant
        const roles = userRoles(user);
        roleSelect.value = roles.includes("admin") ? "admin" : (roles.includes("reviewer") ? (roles.includes("user") ? "reviewer,user" : "reviewer") : "user");
    }
    document.getElementById("user-input-password").value = "";
    document.getElementById("user-input-password").required = false;
    document.getElementById("user-password-hint").style.display = "block";
    
    document.getElementById("user-modal").classList.remove("hidden");
}

function closeUserModal() {
    document.getElementById("user-modal").classList.add("hidden");
}

async function handleSaveUser(event) {
    event.preventDefault();
    const editId = document.getElementById("user-edit-id").value;
    const username = document.getElementById("user-input-username").value.trim();
    const fullName = document.getElementById("user-input-fullname").value.trim();
    const role = document.getElementById("user-input-role").value;
    const password = document.getElementById("user-input-password").value;

    try {
        let response;
        if (editId) {
            // Update existing user
            response = await fetch(`/api/users/${editId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username,
                    full_name: fullName,
                    role,
                    password: password || undefined
                })
            });
        } else {
            // Create new user
            response = await fetch("/api/users", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username,
                    full_name: fullName,
                    role,
                    password
                })
            });
        }

        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur: " + (data.detail || "Échec de l'enregistrement de l'utilisateur."), "error");
            return;
        }

        closeUserModal();
        fetchUsersList();
        if (currentUser && currentUser.id === parseInt(editId, 10)) {
            checkAuthUser();
        }
    } catch (err) {
        console.error("Save user error:", err);
        showToast("Erreur de communication avec le serveur.", "error");
    }
}

async function deleteUserAccount(userId, username) {
    if (!await confirmDialog(`Voulez-vous vraiment supprimer définitivement le compte de @${username} ?`, { title: "Suppression de compte", danger: true, confirmLabel: "Supprimer" })) return;

    try {
        const response = await fetch(`/api/users/${userId}`, {
            method: "DELETE"
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur: " + (data.detail || "Échec de la suppression du compte."), "error");
            return;
        }

        fetchUsersList();
    } catch (err) {
        console.error("Delete user error:", err);
        showToast("Erreur de connexion lors de la suppression.", "error");
    }
}

// -------------------------------------------------------------------------
// MON PROFIL & SÉCURITÉ (SELF SERVICE)
// -------------------------------------------------------------------------

function openProfileModal() {
    if (!currentUser) return;

    document.getElementById("profile-input-username").value = currentUser.username;
    document.getElementById("profile-input-fullname").value = currentUser.full_name || "";
    document.getElementById("profile-old-password").value = "";
    document.getElementById("profile-new-password").value = "";

    document.getElementById("profile-modal").classList.remove("hidden");
}

function closeProfileModal() {
    document.getElementById("profile-modal").classList.add("hidden");
}

async function handleUpdateProfile(event) {
    event.preventDefault();
    const username = document.getElementById("profile-input-username").value.trim();
    const fullName = document.getElementById("profile-input-fullname").value.trim();
    const oldPassword = document.getElementById("profile-old-password").value;
    const newPassword = document.getElementById("profile-new-password").value;

    try {
        // 1. Update Profile Info
        const profileResp = await fetch("/api/users/me/profile", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, full_name: fullName })
        });
        const profileData = await profileResp.json();
        if (!profileResp.ok) {
            showToast("Erreur Profil: " + (profileData.detail || "Échec de la mise à jour du profil."), "error");
            return;
        }

        // 2. Update Password if requested
        if (oldPassword || newPassword) {
            if (!oldPassword || !newPassword) {
                showToast("Pour modifier votre mot de passe, veuillez saisir l'ancien ET le nouveau mot de passe.", "error");
                return;
            }
            const passResp = await fetch("/api/users/me/password", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
            });
            const passData = await passResp.json();
            if (!passResp.ok) {
                showToast("Erreur Mot de Passe: " + (passData.detail || "Échec du changement de mot de passe."), "error");
                return;
            }
        }

        showToast("Votre profil et vos paramètres de sécurité ont été mis à jour.", "success");
        closeProfileModal();
        checkAuthUser();
    } catch (err) {
        console.error("Update profile error:", err);
        showToast("Erreur de communication avec le serveur.", "error");
    }
}

// Global Modal Backdrop Click Listener
window.onclick = function(event) {
    const auditModal = document.getElementById("audit-modal");
    const detailsModal = document.getElementById("details-modal");
    const userModal = document.getElementById("user-modal");
    const profileModal = document.getElementById("profile-modal");

    if (event.target === auditModal) {
        auditModal.style.display = "none";
    }
    if (event.target === detailsModal) {
        detailsModal.classList.add("hidden");
    }
    if (event.target === userModal) {
        userModal.classList.add("hidden");
    }
    if (event.target === profileModal) {
        profileModal.classList.add("hidden");
    }
};



// ------------------ HOMOLOGATION (REVUE AVANT PRODUCTION) ------------------

let ingestionSettings = null;
let pendingReviews = [];
let reviewCurrentSnapshotId = null;
let reviewCurrentPage = 1;
let reviewExcludedSelection = new Set();

// Roles empilables : "user,reviewer" -> ["user", "reviewer"]
function userRoles(user) {
    if (!user) return [];
    if (Array.isArray(user.roles)) return user.roles;
    return (user.role || "").split(",").map(r => r.trim().toLowerCase()).filter(Boolean);
}

async function fetchIngestionSettings() {
    try {
        const response = await fetch("/api/settings/ingestion");
        if (!response.ok) return;
        ingestionSettings = await response.json();
        const approvalEl = document.getElementById("setting-require-approval");
        const justifEl = document.getElementById("setting-exclusion-justification");
        const fileEl = document.getElementById("setting-exclusion-file");
        const fourEyesEl = document.getElementById("setting-alert-four-eyes");
        const wlJustifEl = document.getElementById("setting-whitelist-justification");
        const wlFileEl = document.getElementById("setting-whitelist-file");
        const rescreenEl = document.getElementById("setting-auto-rescreen");
        const btRequiredEl = document.getElementById("setting-backtest-required");
        const btGapEl = document.getElementById("setting-backtest-gap");
        if (approvalEl) approvalEl.checked = ingestionSettings.require_approval;
        if (justifEl) justifEl.checked = ingestionSettings.exclusion_justification_required;
        if (fileEl) fileEl.checked = ingestionSettings.exclusion_file_required;
        if (fourEyesEl) fourEyesEl.checked = ingestionSettings.alert_four_eyes_required;
        if (wlJustifEl) wlJustifEl.checked = ingestionSettings.whitelist_justification_required;
        if (wlFileEl) wlFileEl.checked = ingestionSettings.whitelist_file_required;
        if (rescreenEl) rescreenEl.checked = ingestionSettings.auto_rescreen;
        if (btRequiredEl) btRequiredEl.checked = ingestionSettings.backtest_required;
        if (btGapEl) btGapEl.value = ingestionSettings.backtest_max_gap_pct ?? 20;
        // Encart de l'onglet Homologation : parcours actif seulement si le mode l'est
        const modeHint = document.getElementById("review-mode-hint");
        if (modeHint) modeHint.classList.toggle("hidden", !!ingestionSettings.require_approval);
        // Asterisques "obligatoire" de la modale d'exclusion
        const justifMark = document.getElementById("exclusion-justification-required-mark");
        const fileMark = document.getElementById("exclusion-file-required-mark");
        if (justifMark) justifMark.classList.toggle("hidden", !ingestionSettings.exclusion_justification_required);
        if (fileMark) fileMark.classList.toggle("hidden", !ingestionSettings.exclusion_file_required);
    } catch (e) {
        console.error("Error fetching ingestion settings:", e);
    }
}

async function saveIngestionSettings() {
    const payload = {
        require_approval: document.getElementById("setting-require-approval").checked,
        exclusion_justification_required: document.getElementById("setting-exclusion-justification").checked,
        exclusion_file_required: document.getElementById("setting-exclusion-file").checked,
        alert_four_eyes_required: document.getElementById("setting-alert-four-eyes").checked,
        whitelist_justification_required: document.getElementById("setting-whitelist-justification").checked,
        whitelist_file_required: document.getElementById("setting-whitelist-file").checked,
        auto_rescreen: document.getElementById("setting-auto-rescreen").checked,
        backtest_required: document.getElementById("setting-backtest-required").checked,
        backtest_max_gap_pct: parseFloat(document.getElementById("setting-backtest-gap").value) || 20
    };
    try {
        const response = await fetch("/api/settings/ingestion", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec de la mise à jour des réglages."), "error");
            return;
        }
        showToast(data.message || "Réglages mis à jour.", "success");
        fetchIngestionSettings();
    } catch (e) {
        console.error("Error saving ingestion settings:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

async function fetchPendingReviews() {
    try {
        const response = await fetch("/api/review/pending");
        if (!response.ok) return;
        const data = await response.json();
        pendingReviews = data.pending || [];
        renderPendingTable(pendingReviews);
        const badge = document.getElementById("review-pending-badge");
        if (badge) {
            badge.textContent = pendingReviews.length;
            badge.classList.toggle("hidden", pendingReviews.length === 0);
        }
    } catch (e) {
        console.error("Error fetching pending reviews:", e);
    }
}

function renderPendingTable(pending) {
    const tbody = document.querySelector("#review-pending-table tbody");
    if (!tbody) return;
    if (!pending || pending.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Aucun snapshot en attente d\'homologation.</td></tr>';
        return;
    }
    tbody.innerHTML = pending.map(snap => {
        const dateStr = snap.uploaded_at ? new Date(snap.uploaded_at).toLocaleString("fr-FR") : "-";
        return `
            <tr>
                <td>${escapeHtml(dateStr)}</td>
                <td><strong>${escapeHtml(snap.file_name)}</strong><br><small style="color:var(--text-muted)">Hash: ${(snap.file_hash || "").substring(0, 8)}...</small></td>
                <td>${listTypeBadge(snap.file_type)}</td>
                <td>${snap.record_count}</td>
                <td>${snap.excluded_count || 0}</td>
                <td><button class="btn btn-sm btn-secondary" onclick="openReviewDetail('${escapeHtml(snap.snapshot_id)}')">🔍 Examiner</button></td>
            </tr>
        `;
    }).join("");
}

// ------------------ PARCOURS GUIDÉ DE PRODUCTION DE LISTE ------------------

// Ouvre directement le parcours d'homologation d'un snapshot (depuis un import ou une synchro)
function openPendingReview(snapshotId) {
    switchTab("watchlist-mgmt");
    switchSubTab("watchlist-mgmt", "watchlist-review");
    if (snapshotId) openReviewDetail(snapshotId);
}

// Étape affichée du parcours (1 Delta, 2 Exclusions, 3 Cahier de tests, 4 Décision)
function showReviewStep(step) {
    for (let i = 1; i <= 4; i++) {
        const panel = document.getElementById(`review-step-${i}`);
        const btn = document.getElementById(`step-btn-${i}`);
        if (panel) panel.classList.toggle("hidden", i !== step);
        if (btn) btn.classList.toggle("active", i === step);
    }
}

async function openReviewDetail(snapshotId) {
    reviewCurrentSnapshotId = snapshotId;
    reviewExcludedSelection = new Set();
    reviewCurrentPage = 1;
    const searchInput = document.getElementById("review-entity-search");
    if (searchInput) searchInput.value = "";
    const commentEl = document.getElementById("review-comment");
    if (commentEl) commentEl.value = "";
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(snapshotId)}`);
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Impossible de charger le snapshot."), "error");
            return;
        }
        document.getElementById("review-detail-card").classList.remove("hidden");
        document.getElementById("review-detail-title").textContent = `Examen du Snapshot — ${data.file_name}`;
        const uploadedStr = data.uploaded_at ? new Date(data.uploaded_at).toLocaleString("fr-FR") : "-";
        document.getElementById("review-detail-meta").textContent =
            `Liste ${listTypeLabel(data.file_type)} · ${data.record_count} fiches · importé le ${uploadedStr} · delta calculé par rapport à la production actuelle` +
            (data.production_snapshot_id ? "" : " (aucune liste du même type en production : tout est en ajout)");
        const summary = data.delta_summary || {};
        document.getElementById("review-delta-added").textContent = summary.added_count ?? 0;
        document.getElementById("review-delta-removed").textContent = summary.removed_count ?? 0;
        document.getElementById("review-delta-modified").textContent = summary.modified_count ?? 0;
        renderReviewDeltaDetails(data.delta_details);
        // Cahier de tests : panels disponibles + dernier rapport archivé
        showReviewStep(1);
        fetchTestPanels();
        renderBacktestReport(data.backtest_report);
        await loadReviewEntitiesPage(1);
        document.getElementById("review-detail-card").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
        console.error("Error opening review detail:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

// Delta détaillé (étape 1) : listes des ajouts / modifications / suppressions
function renderReviewDeltaDetails(deltaDetails) {
    const container = document.getElementById("review-delta-details");
    if (!container) return;
    const details = (deltaDetails && deltaDetails.details) || deltaDetails || {};
    const added = details.added || [];
    const removed = details.removed || [];
    const modified = details.modified || [];
    if (!added.length && !removed.length && !modified.length) {
        container.innerHTML = '<p class="section-desc">Aucune différence détaillée à afficher (liste identique ou premier import).</p>';
        return;
    }

    const rows3 = (items, cls) => items.map(e => `
        <tr><td><code>${escapeHtml(e.id || "")}</code></td><td>${escapeHtml(e.type || "")}</td>
        <td><span class="status-badge ${cls}">${escapeHtml(e.primary_name || "")}</span></td></tr>`).join("");

    const modifiedRows = modified.map(e => {
        const changes = (e.changes_detected || []).map(field => {
            const before = e.before ? e.before[field] : undefined;
            const after = e.after ? e.after[field] : undefined;
            const fmt = v => (v === null || v === undefined) ? "∅" : (typeof v === "object" ? JSON.stringify(v) : String(v));
            return `<small><strong>${escapeHtml(field)}</strong> : <span style="color:var(--text-muted)">${escapeHtml(fmt(before))}</span> → ${escapeHtml(fmt(after))}</small>`;
        }).join("<br>");
        return `<tr><td><code>${escapeHtml(e.id || "")}</code></td><td><strong>${escapeHtml(e.primary_name || "")}</strong></td><td>${changes || "-"}</td></tr>`;
    }).join("");

    const section = (title, count, inner) => count ? `
        <details style="margin-bottom: 0.6rem;">
            <summary style="cursor: pointer; font-weight: 600; padding: 0.4rem 0;">${title} (${count})</summary>
            <div class="table-container" style="max-height: 260px; overflow-y: auto;"><table>${inner}</table></div>
        </details>` : "";

    container.innerHTML =
        section("🟢 Ajouts", added.length, `<thead><tr><th>ID</th><th>Type</th><th>Nom</th></tr></thead><tbody>${rows3(added, "no_match")}</tbody>`) +
        section("🟠 Modifications (avant → après)", modified.length, `<thead><tr><th>ID</th><th>Nom</th><th>Champs modifiés</th></tr></thead><tbody>${modifiedRows}</tbody>`) +
        section("🔴 Suppressions", removed.length, `<thead><tr><th>ID</th><th>Type</th><th>Nom</th></tr></thead><tbody>${rows3(removed, "alert")}</tbody>`);
}

// ------------------ ÉTAPE 3 : CAHIER DE TESTS (BACKTEST) ------------------

async function fetchTestPanels(selectSnapshotId = null) {
    const select = document.getElementById("backtest-panel-select");
    if (!select) return;
    try {
        const response = await fetch("/api/testpanels");
        if (!response.ok) return;
        const data = await response.json();
        const panels = data.panels || [];
        if (!panels.length) {
            select.innerHTML = '<option value="">Aucun panel — générez-en un ou importez une base clients</option>';
            return;
        }
        select.innerHTML = panels.map(p => {
            const label = `${p.generated ? "🧪 " : "👥 "}${p.file_name} (${p.record_count} clients)`;
            return `<option value="${escapeHtml(p.snapshot_id)}">${escapeHtml(label)}</option>`;
        }).join("");
        if (selectSnapshotId) select.value = selectSnapshotId;
    } catch (e) {
        console.error("Error fetching test panels:", e);
    }
}

async function generateTestPanel() {
    const btn = document.getElementById("generate-panel-btn");
    const size = parseInt(document.getElementById("backtest-panel-size").value, 10) || 500;
    btn.disabled = true;
    btn.textContent = "Génération...";
    try {
        const response = await fetch("/api/testpanels/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ snapshot_id: reviewCurrentSnapshotId, size }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec de la génération."), "error");
            return;
        }
        showToast(data.message, "success");
        await fetchTestPanels(data.snapshot_id);
    } catch (e) {
        console.error("Error generating test panel:", e);
        showToast("Erreur réseau de communication.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "⚙️ Générer un panel";
    }
}

async function runReviewBacktest() {
    if (!reviewCurrentSnapshotId) return;
    const panelId = document.getElementById("backtest-panel-select").value;
    if (!panelId) {
        showToast("Choisissez ou générez d'abord un panel de pseudo-clients.", "warning");
        return;
    }
    const btn = document.getElementById("run-backtest-btn");
    btn.disabled = true;
    btn.textContent = "Criblage à blanc en cours...";
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/backtest`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ panel_snapshot_id: panelId }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec du cahier de tests."), "error");
            return;
        }
        renderBacktestReport(data);
        showToast(data.verdict === "OK"
            ? "Cahier de tests terminé : écart dans le seuil toléré."
            : "Cahier de tests terminé : écart élevé — examinez les nouvelles alertes.", data.verdict === "OK" ? "success" : "warning", 7000);
    } catch (e) {
        console.error("Error running backtest:", e);
        showToast("Erreur réseau de communication.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "▶ Lancer le cahier de tests";
    }
}

function backtestVerdictBadge(report) {
    if (!report) return "";
    return report.verdict === "OK"
        ? '<span class="status-badge no_match">ÉCART OK</span>'
        : '<span class="status-badge warning">ÉCART ÉLEVÉ</span>';
}

function renderBacktestReport(report) {
    const container = document.getElementById("backtest-results");
    const reminder = document.getElementById("review-backtest-reminder");
    if (!container) return;
    if (!report) {
        container.classList.add("hidden");
        container.innerHTML = "";
        if (reminder) reminder.innerHTML = `
            <p class="section-desc" style="color: var(--color-warning);">⚠️ Aucun cahier de tests n'a été exécuté sur ce snapshot. Recommandé avant toute mise en production (étape 3).</p>`;
        return;
    }

    const rateCard = (title, side, accent) => `
        <div class="metric" style="flex: 1; background: rgba(255,255,255,0.03); padding: 1rem; border-radius: 8px; border: 1px solid var(--border-color);">
            <span class="metric-label" style="font-weight: 600; color: ${accent};">${title}</span>
            <span class="metric-value" style="font-size: 1.4rem;">${side.alerts} alerte(s)</span>
            <small style="color: var(--text-muted);">taux d'interception : ${side.interception_rate_pct} % · ${side.whitelisted_suppressed} supprimée(s) par liste blanche</small>
        </div>`;

    const pairRow = (p, withCheckbox) => `
        <tr>
            ${withCheckbox ? `<td><input type="checkbox" class="goodguy-cb" data-client-id="${escapeHtml(p.client_id)}" data-entity-id="${escapeHtml(p.entity_id)}" data-client-name="${escapeHtml(p.client_name || "")}" data-entity-name="${escapeHtml(p.entity_name || "")}" data-list-type="${escapeHtml(p.list_type || "")}"></td>` : ""}
            <td><code>${escapeHtml(p.client_id)}</code><br><small>${escapeHtml(p.client_name || "")}</small></td>
            <td><code>${escapeHtml(p.entity_id)}</code><br><small><strong>${escapeHtml(p.entity_name || "")}</strong></small></td>
            <td>${listTypeBadge(p.list_type)}</td>
            <td><span class="status-badge alert">${p.score}</span></td>
        </tr>`;

    const newPairs = report.new_pairs || [];
    const resolvedPairs = report.resolved_pairs || [];
    const executedStr = report.executed_at ? new Date(report.executed_at).toLocaleString("fr-FR") : "";

    container.classList.remove("hidden");
    container.innerHTML = `
        <div style="display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem;">
            <h3 style="margin: 0;">Résultats du cahier de tests</h3>
            ${backtestVerdictBadge(report)}
            <small style="color: var(--text-muted);">panel de ${report.panel_size} pseudo-clients · exécuté par @${escapeHtml(report.executed_by || "")} le ${escapeHtml(executedStr)}</small>
        </div>
        <div class="score-metrics" style="flex-direction: row; gap: 1.5rem; margin-bottom: 1rem;">
            ${rateCard("Liste actuelle (production)", report.current, "var(--text-secondary)")}
            ${rateCard("Liste candidate", report.candidate, "var(--color-accent)")}
            <div class="metric" style="flex: 1; background: rgba(245, 158, 11, 0.08); padding: 1rem; border-radius: 8px; border: 1px solid rgba(245, 158, 11, 0.2);">
                <span class="metric-label" style="font-weight: 600; color: var(--color-warning);">Écart</span>
                <span class="metric-value" style="font-size: 1.4rem;">${report.gap_pct} %</span>
                <small style="color: var(--text-muted);">seuil toléré : ${report.threshold_pct} %</small>
            </div>
        </div>
        ${newPairs.length ? `
            <h4 style="margin: 0.75rem 0 0.4rem;">Nouvelles alertes avec la liste candidate (${report.new_pairs_count})</h4>
            <p class="section-desc">Vérifiez chaque paire : s'il s'agit d'un homonyme avéré (« Good Guy »), mettez-la en liste blanche puis relancez le cahier de tests.</p>
            <div style="display: flex; gap: 0.75rem; margin-bottom: 0.5rem; align-items: center;">
                <label style="font-size: 0.85rem; cursor: pointer;"><input type="checkbox" onchange="document.querySelectorAll('.goodguy-cb').forEach(cb => cb.checked = this.checked)"> Tout sélectionner</label>
                <button class="btn btn-sm btn-secondary" onclick="bulkGoodGuys()">🕊️ Good Guy (liste blanche) sur la sélection</button>
            </div>
            <div class="table-container" style="max-height: 300px; overflow-y: auto;">
                <table>
                    <thead><tr><th style="width:32px;"></th><th>Pseudo-client</th><th>Listé</th><th>Liste</th><th>Score</th></tr></thead>
                    <tbody>${newPairs.map(p => pairRow(p, true)).join("")}</tbody>
                </table>
            </div>` : '<p class="section-desc">✅ Aucune nouvelle alerte par rapport à la liste actuelle.</p>'}
        ${resolvedPairs.length ? `
            <details style="margin-top: 0.75rem;">
                <summary style="cursor: pointer; font-weight: 600;">Alertes résolues par la liste candidate (${report.resolved_pairs_count})</summary>
                <div class="table-container" style="max-height: 240px; overflow-y: auto;">
                    <table>
                        <thead><tr><th>Pseudo-client</th><th>Listé</th><th>Liste</th><th>Score</th></tr></thead>
                        <tbody>${resolvedPairs.map(p => pairRow(p, false)).join("")}</tbody>
                    </table>
                </div>
            </details>` : ""}
    `;

    if (reminder) {
        reminder.innerHTML = report.verdict === "OK"
            ? `<p class="section-desc" style="color: var(--color-safe);">✅ Cahier de tests exécuté le ${escapeHtml(executedStr)} — écart ${report.gap_pct} % dans le seuil toléré (${report.threshold_pct} %).</p>`
            : `<p class="section-desc" style="color: var(--color-warning);">⚠️ Le dernier cahier de tests signale un écart de ${report.gap_pct} % (seuil : ${report.threshold_pct} %). Posez des Good Guys ou des exclusions puis relancez-le avant d'approuver.</p>`;
    }
}

async function bulkGoodGuys() {
    const checked = Array.from(document.querySelectorAll(".goodguy-cb:checked"));
    if (!checked.length) {
        showToast("Sélectionnez au moins une paire à mettre en liste blanche.", "warning");
        return;
    }
    const justification = await promptDialog(
        `Justification commune pour ${checked.length} paire(s) « Good Guy »`,
        { placeholder: "Ex. : homonymes avérés lors du cahier de tests d'homologation du " + new Date().toLocaleDateString("fr-FR"), textarea: true }
    );
    if (justification === null) return;
    const pairs = checked.map(cb => ({
        client_id: cb.dataset.clientId,
        watchlist_entity_id: cb.dataset.entityId,
        client_name: cb.dataset.clientName || null,
        watchlist_name: cb.dataset.entityName || null,
        list_type: cb.dataset.listType || null,
    }));
    try {
        const response = await fetch("/api/whitelist/bulk", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pairs, justification }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec de la mise en liste blanche."), "error");
            return;
        }
        showToast(`${data.message} Relancez le cahier de tests pour mesurer l'amélioration.`, "success", 8000);
        fetchWhitelist();
    } catch (e) {
        console.error("Error bulk whitelisting:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

async function loadReviewEntitiesPage(page) {
    if (!reviewCurrentSnapshotId) return;
    reviewCurrentPage = page;
    const search = (document.getElementById("review-entity-search").value || "").trim();
    const params = new URLSearchParams({ page: String(page), page_size: "100" });
    if (search) params.set("search", search);
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/entities?${params}`);
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Impossible de charger les entités."), "error");
            return;
        }
        renderReviewEntitiesTable(data);
    } catch (e) {
        console.error("Error loading review entities:", e);
    }
}

function renderReviewEntitiesTable(data) {
    const tbody = document.querySelector("#review-entities-table tbody");
    if (!tbody) return;
    if (!data.items || data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Aucune entité trouvée.</td></tr>';
    } else {
        tbody.innerHTML = data.items.map(item => {
            const checked = reviewExcludedSelection.has(item.id) ? "checked" : "";
            let exclusionCell = '<small style="color: var(--text-muted);">—</small>';
            if (item.excluded) {
                const evidenceLink = item.exclusion_file_name
                    ? ` · <a href="/api/review/exclusion-evidence/${item.id}" target="_blank" style="color: var(--color-accent);">📎 ${escapeHtml(item.exclusion_file_name)}</a>`
                    : "";
                exclusionCell = `<span class="status-badge alert">EXCLU</span><br>` +
                    `<small>${escapeHtml(item.exclusion_justification || "(sans justification)")} — ${escapeHtml(item.excluded_by || "")}${evidenceLink}</small>`;
            }
            return `
                <tr ${item.excluded ? 'style="opacity: 0.65;"' : ""}>
                    <td><input type="checkbox" ${checked} onchange="toggleReviewSelection(${item.id}, this.checked)"></td>
                    <td><code>${escapeHtml(item.entity_id)}</code></td>
                    <td>${escapeHtml(item.entity_type)}</td>
                    <td><strong>${escapeHtml(item.primary_name)}</strong></td>
                    <td>${exclusionCell}</td>
                </tr>
            `;
        }).join("");
    }
    const pagination = document.getElementById("review-entities-pagination");
    if (pagination) {
        const totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 100)));
        pagination.classList.toggle("hidden", data.total === 0);
        pagination.innerHTML = `
            <button class="btn btn-sm" ${data.page <= 1 ? "disabled" : ""} onclick="loadReviewEntitiesPage(${data.page - 1})" style="background: rgba(255,255,255,0.08);">← Précédent</button>
            <span style="margin: 0 1rem; color: var(--text-muted); font-size: 0.85rem;">Page ${data.page} / ${totalPages} — ${data.total} entité(s)</span>
            <button class="btn btn-sm" ${data.page >= totalPages ? "disabled" : ""} onclick="loadReviewEntitiesPage(${data.page + 1})" style="background: rgba(255,255,255,0.08);">Suivant →</button>
        `;
    }
    updateReviewSelectionInfo();
}

function toggleReviewSelection(entityPk, isChecked) {
    if (isChecked) reviewExcludedSelection.add(entityPk);
    else reviewExcludedSelection.delete(entityPk);
    updateReviewSelectionInfo();
}

function updateReviewSelectionInfo() {
    const info = document.getElementById("review-selection-info");
    if (info) info.textContent = reviewExcludedSelection.size > 0 ? `${reviewExcludedSelection.size} entité(s) sélectionnée(s)` : "";
}

function openExclusionModal() {
    if (reviewExcludedSelection.size === 0) {
        showToast("Sélectionnez au moins une entité à exclure (cases à cocher).", "error");
        return;
    }
    document.getElementById("exclusion-modal-count").textContent =
        `${reviewExcludedSelection.size} entité(s) sélectionnée(s) seront exclues de la mise en production (conservées en base pour l'audit).`;
    document.getElementById("exclusion-justification").value = "";
    document.getElementById("exclusion-file").value = "";
    document.getElementById("exclusion-modal").classList.remove("hidden");
}

function closeExclusionModal() {
    document.getElementById("exclusion-modal").classList.add("hidden");
}

async function submitExclusions(event) {
    event.preventDefault();
    if (!reviewCurrentSnapshotId || reviewExcludedSelection.size === 0) return;
    const justification = document.getElementById("exclusion-justification").value.trim();
    const fileInput = document.getElementById("exclusion-file");
    // Pre-validation cote client selon les reglages modulaires (le serveur revalide)
    if (ingestionSettings && ingestionSettings.exclusion_justification_required && !justification) {
        showToast("Une justification est obligatoire pour exclure une entité (réglage actif).", "error");
        return;
    }
    if (ingestionSettings && ingestionSettings.exclusion_file_required && fileInput.files.length === 0) {
        showToast("Une pièce jointe justificative est obligatoire pour exclure une entité (réglage actif).", "error");
        return;
    }
    const formData = new FormData();
    formData.append("entity_ids", JSON.stringify([...reviewExcludedSelection]));
    formData.append("justification", justification);
    if (fileInput.files.length > 0) formData.append("file", fileInput.files[0]);
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/exclusions`, {
            method: "POST",
            body: formData
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec de l'exclusion."), "error");
            return;
        }
        closeExclusionModal();
        reviewExcludedSelection = new Set();
        showToast(data.message, "success");
        loadReviewEntitiesPage(reviewCurrentPage);
        fetchPendingReviews();
    } catch (e) {
        console.error("Error submitting exclusions:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

async function removeExclusions() {
    if (!reviewCurrentSnapshotId || reviewExcludedSelection.size === 0) {
        showToast("Sélectionnez au moins une entité à réintégrer (cases à cocher).", "error");
        return;
    }
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/exclusions/remove`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entity_ids: [...reviewExcludedSelection] })
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec de la réintégration."), "error");
            return;
        }
        reviewExcludedSelection = new Set();
        showToast(data.message, "success");
        loadReviewEntitiesPage(reviewCurrentPage);
        fetchPendingReviews();
    } catch (e) {
        console.error("Error removing exclusions:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

async function approvePendingSnapshot() {
    if (!reviewCurrentSnapshotId) return;
    if (!await confirmDialog("Approuver ce snapshot ?\nIl sera mis en production et remplacera les listes antérieures du même type (hors entités exclues).", { title: "Approbation d'homologation", confirmLabel: "Approuver" })) return;
    const comment = document.getElementById("review-comment").value.trim();
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment })
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec de l'approbation."), "error");
            return;
        }
        showToast(`${data.message} (${data.excluded_count} entité(s) exclue(s))`, "success");
        document.getElementById("review-detail-card").classList.add("hidden");
        reviewCurrentSnapshotId = null;
        fetchPendingReviews();
        fetchSnapshots();
        fetchWatchlist();
        fetchWatchlistHash();
    } catch (e) {
        console.error("Error approving snapshot:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

async function rejectPendingSnapshot() {
    if (!reviewCurrentSnapshotId) return;
    const comment = document.getElementById("review-comment").value.trim();
    if (!comment) {
        showToast("Un commentaire est requis pour rejeter un snapshot.", "error");
        return;
    }
    if (!await confirmDialog("Rejeter ce snapshot ?\nIl n'entrera jamais en production (conservé en base pour l'audit).", { title: "Rejet d'homologation", danger: true, confirmLabel: "Rejeter" })) return;
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/reject`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment })
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Échec du rejet."), "error");
            return;
        }
        showToast(data.message, "success");
        document.getElementById("review-detail-card").classList.add("hidden");
        reviewCurrentSnapshotId = null;
        fetchPendingReviews();
        fetchSnapshots();
    } catch (e) {
        console.error("Error rejecting snapshot:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

// ------------------ ALERTES (CYCLE DE VIE + 4-YEUX) ------------------

let alertsFilter = "OPEN,IN_PROGRESS,ESCALATED,PENDING_VALIDATION";
let currentAlertId = null;

async function fetchAlerts() {
    try {
        const params = new URLSearchParams({ page: "1", page_size: "100" });
        if (alertsFilter) params.set("status", alertsFilter);
        const listFilterEl = document.getElementById("alerts-list-filter");
        if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
        const response = await fetch(`/api/alerts?${params}`);
        if (!response.ok) return;
        const data = await response.json();
        renderAlertsTable(data.items || []);
        const badge = document.getElementById("alerts-open-badge");
        if (badge) {
            badge.textContent = data.open_count;
            badge.classList.toggle("hidden", !data.open_count);
        }
    } catch (e) {
        console.error("Error fetching alerts:", e);
    }
}

function setAlertFilter(filter) {
    alertsFilter = filter;
    document.querySelectorAll("#alerts-status-filters button").forEach(btn => {
        const active = btn.dataset.filter === filter;
        btn.classList.toggle("btn-secondary", active);
        btn.style.background = active ? "" : "rgba(255,255,255,0.08)";
    });
    fetchAlerts();
}

function alertStatusBadge(status) {
    const styles = {
        OPEN: ["#fbbf24", "OUVERTE"],
        IN_PROGRESS: ["#38bdf8", "EN COURS"],
        ESCALATED: ["#f87171", "ESCALADÉE"],
        PENDING_VALIDATION: ["#c084fc", "À VALIDER (4-YEUX)"],
        CLOSED_CONFIRMED: ["#f87171", "VRAI POSITIF"],
        CLOSED_FALSE_POSITIVE: ["#4ade80", "FAUX POSITIF"],
    };
    const [color, label] = styles[status] || ["#9ca3af", status];
    return `<span style="color: ${color}; font-weight: 600; font-size: 0.8rem;">${label}</span>`;
}

function renderAlertsTable(items) {
    const tbody = document.querySelector("#alerts-table tbody");
    if (!tbody) return;
    if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Aucune alerte pour ce filtre.</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(a => `
        <tr>
            <td>${a.created_at ? new Date(a.created_at).toLocaleString("fr-FR") : "-"}</td>
            <td><strong>${escapeHtml(a.client_name)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(a.client_id || "")}</small></td>
            <td>${escapeHtml(a.watchlist_name)}<br><small style="color:var(--text-muted)">${escapeHtml(a.watchlist_entity_id)}</small></td>
            <td>${listTypeBadge(a.list_type)}</td>
            <td><strong style="color: ${a.final_score >= 90 ? '#f87171' : 'var(--color-warning)'};">${a.final_score.toFixed(1)}%</strong></td>
            <td>${alertStatusBadge(a.status)}</td>
            <td>${escapeHtml(a.assigned_to || "—")}</td>
            <td><button class="btn btn-sm btn-secondary" onclick="openAlertModal(${a.id})">🔎 Instruire</button></td>
        </tr>
    `).join("");
}

async function openAlertModal(alertId) {
    currentAlertId = alertId;
    try {
        const response = await fetch(`/api/alerts/${alertId}`);
        const a = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (a.detail || "Impossible de charger l'alerte."), "error");
            return;
        }
        document.getElementById("alert-modal-title").innerHTML =
            `Alerte #${a.id} — ${escapeHtml(a.client_name)} × ${escapeHtml(a.watchlist_name)} ${alertStatusBadge(a.status)}`;

        const roles = userRoles(currentUser);
        const isReviewer = roles.includes("admin") || roles.includes("reviewer");
        const isClosed = a.status.startsWith("CLOSED");
        const me = currentUser ? currentUser.username : "";

        const adjustments = ((a.decision_tree || {}).adjustments) || {};
        const adjRows = Object.entries(adjustments).map(([k, v]) =>
            `<tr><td>${escapeHtml(k)}</td><td>${v.score > 0 ? "+" : ""}${v.score}</td><td>${escapeHtml(v.description || "")}</td></tr>`
        ).join("");

        const eventsHtml = (a.events || []).map(e => `
            <div style="border-left: 2px solid var(--border-color); padding: 0.35rem 0 0.35rem 0.75rem; margin-left: 0.25rem;">
                <small style="color: var(--text-muted);">${e.timestamp ? new Date(e.timestamp).toLocaleString("fr-FR") : ""} — <strong>@${escapeHtml(e.username)}</strong> · ${escapeHtml(e.action)}</small>
                ${e.detail ? `<div style="font-size: 0.85rem;">${escapeHtml(e.detail)}</div>` : ""}
            </div>
        `).join("");

        let actionsHtml = "";
        if (!isClosed) {
            actionsHtml += `<div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem;">`;
            if (a.status !== "PENDING_VALIDATION") {
                actionsHtml += `<button class="btn btn-sm btn-secondary" onclick="alertAction('assign')">📌 M'assigner</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(255,255,255,0.08);" onclick="alertActionWithComment('comment', 'Commentaire')">💬 Commenter</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: #fca5a5;" onclick="alertActionWithComment('escalate', 'Motif de l\\'escalade')">⚠️ Escalader</button>`;
                actionsHtml += `<button class="btn btn-sm btn-primary" onclick="proposeAlertDecision('FALSE_POSITIVE')">✅ Proposer : Faux positif</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(239,68,68,0.85);" onclick="proposeAlertDecision('CONFIRMED')">🚨 Proposer : Vrai positif</button>`;
            } else if (isReviewer && a.proposed_by !== me) {
                actionsHtml += `<span style="align-self: center; font-size: 0.85rem; color: var(--text-muted);">Proposé par @${escapeHtml(a.proposed_by)} : <strong>${a.proposed_decision === "CONFIRMED" ? "vrai positif" : "faux positif"}</strong></span>`;
                actionsHtml += `<button class="btn btn-sm btn-primary" onclick="validateAlertDecision(true)">✔️ Valider (4-yeux)</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: #fca5a5;" onclick="validateAlertDecision(false)">↩️ Refuser & renvoyer</button>`;
            } else {
                actionsHtml += `<span style="align-self: center; font-size: 0.85rem; color: var(--text-muted);">Décision proposée par @${escapeHtml(a.proposed_by)} — en attente d'un validateur différent (rôle réviseur).</span>`;
            }
            actionsHtml += `</div>`;
        } else {
            actionsHtml = `<p class="section-desc" style="margin-top: 1rem;">Clôturée par <strong>@${escapeHtml(a.decided_by)}</strong> le ${a.decided_at ? new Date(a.decided_at).toLocaleString("fr-FR") : ""} — ${escapeHtml(a.decision_comment || "")}</p>`;
            // Faux positif avere : proposer la mise en liste blanche (reviseurs)
            if (a.status === "CLOSED_FALSE_POSITIVE" && isReviewer) {
                actionsHtml += `<button class="btn btn-sm btn-secondary" onclick="openWhitelistModal('${escapeHtml(a.client_id || "")}', '${escapeHtml(a.watchlist_entity_id)}', '${escapeHtml(a.client_name)}', '${escapeHtml(a.watchlist_name)}')">🛡️ Mettre en liste blanche</button>`;
            }
        }

        document.getElementById("alert-modal-body").innerHTML = `
            <p class="section-desc">Score final <strong>${a.final_score.toFixed(1)}%</strong> · assignée à <strong>${escapeHtml(a.assigned_to || "personne")}</strong> · journal d'audit #${a.audit_id} (${escapeHtml(a.watchlist_version || "")})</p>
            <h3 style="font-size: 0.95rem; margin: 0.75rem 0 0.5rem;">Explication du score (decision tree)</h3>
            <div class="table-container" style="max-height: 160px;">
                <table><thead><tr><th>Ajustement</th><th>Impact</th><th>Détail</th></tr></thead><tbody>${adjRows || '<tr><td colspan="3" style="color: var(--text-muted);">Hard match ou aucun ajustement.</td></tr>'}</tbody></table>
            </div>
            <h3 style="font-size: 0.95rem; margin: 1rem 0 0.5rem;">Historique</h3>
            <div style="max-height: 220px; overflow-y: auto;">${eventsHtml || '<small style="color: var(--text-muted);">Aucun événement.</small>'}</div>
            ${actionsHtml}
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--border-color);">
                <button class="btn btn-sm btn-secondary" onclick="generateAlertNarrative()">📝 Générer un narratif</button>
                <button class="btn btn-sm" style="background: rgba(255,255,255,0.08);" onclick="fetchAlertAdverseMedia('client')">📰 Presse : client</button>
                <button class="btn btn-sm" style="background: rgba(255,255,255,0.08);" onclick="fetchAlertAdverseMedia('watchlist')">📰 Presse : listé</button>
            </div>
            <div id="alert-narrative-container" class="hidden" style="margin-top: 0.75rem;"></div>
            <div id="alert-adverse-container" class="hidden" style="margin-top: 0.75rem;"></div>
        `;
        currentAlertNames = { client: a.client_name || "", watchlist: a.watchlist_name || "" };
        document.getElementById("alert-modal").classList.remove("hidden");
    } catch (e) {
        console.error("Error opening alert:", e);
    }
}

function closeAlertModal() {
    document.getElementById("alert-modal").classList.add("hidden");
    currentAlertId = null;
}

async function _postAlertAction(path, body) {
    const response = await fetch(`/api/alerts/${currentAlertId}/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) {
        showToast("Erreur : " + (data.detail || "Action refusée."), "error");
        return null;
    }
    return data;
}

async function alertAction(action) {
    const data = await _postAlertAction(action, {});
    if (data) { openAlertModal(currentAlertId); fetchAlerts(); }
}

async function alertActionWithComment(action, promptLabel) {
    const comment = await promptDialog(promptLabel, { textarea: true, placeholder: "Votre commentaire..." });
    if (comment === null) return;
    const data = await _postAlertAction(action, { comment });
    if (data) { openAlertModal(currentAlertId); fetchAlerts(); }
}

async function proposeAlertDecision(decision) {
    const label = decision === "CONFIRMED" ? "vrai positif" : "faux positif";
    const comment = await promptDialog(`Proposer « ${label} »`, {
        message: "Commentaire obligatoire motivant la décision proposée (validation 4-yeux ensuite).",
        textarea: true, placeholder: "Motivation réglementaire de la décision..."
    });
    if (comment === null) return;
    const data = await _postAlertAction("propose", { decision, comment });
    if (data) { showToast(data.message, "success"); openAlertModal(currentAlertId); fetchAlerts(); }
}

async function validateAlertDecision(approve) {
    const comment = await promptDialog(approve ? "Valider la décision (4-yeux)" : "Refuser et renvoyer en analyse", {
        message: approve ? "Commentaire (optionnel)." : "Motif du refus (obligatoire) — l'alerte repartira en analyse.",
        textarea: true, required: !approve,
        placeholder: approve ? "Commentaire éventuel..." : "Motif du refus..."
    });
    if (comment === null) return;
    const data = await _postAlertAction("validate", { approve, comment });
    if (data) { showToast(data.message, "success"); openAlertModal(currentAlertId); fetchAlerts(); }
}

// ------------------ LISTE BLANCHE CLIENT x LISTÉ (GOOD GUYS) ------------------

async function fetchWhitelist() {
    try {
        const params = new URLSearchParams({ page_size: "100" });
        const listFilterEl = document.getElementById("whitelist-list-filter");
        if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
        const response = await fetch(`/api/whitelist?${params}`);
        if (!response.ok) return;
        const data = await response.json();
        renderWhitelistTable(data.items || []);
    } catch (e) {
        console.error("Error fetching whitelist:", e);
    }
}

function renderWhitelistTable(items) {
    const tbody = document.querySelector("#whitelist-table tbody");
    if (!tbody) return;
    if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Aucune paire en liste blanche.</td></tr>';
        return;
    }
    const stateBadge = (state) => {
        const map = { ACTIVE: ["#4ade80", "ACTIVE"], EXPIRED: ["#fbbf24", "EXPIRÉE"], REVOKED: ["#9ca3af", "RÉVOQUÉE"] };
        const [color, label] = map[state] || ["#9ca3af", state];
        return `<span style="color: ${color}; font-weight: 600; font-size: 0.8rem;">${label}</span>`;
    };
    tbody.innerHTML = items.map(p => `
        <tr ${p.state !== "ACTIVE" ? 'style="opacity: 0.55;"' : ""}>
            <td><strong>${escapeHtml(p.client_name || p.client_id)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(p.client_id)}</small></td>
            <td>${escapeHtml(p.watchlist_name || p.watchlist_entity_id)}<br><small style="color:var(--text-muted)">${escapeHtml(p.watchlist_entity_id)}</small></td>
            <td>${listTypeBadge(p.list_type)}</td>
            <td style="max-width: 260px;"><small>${escapeHtml(p.justification || "—")}</small>${p.evidence_file_name ? `<br><a href="/api/whitelist/evidence/${p.id}" target="_blank" style="color: var(--color-accent); font-size: 0.75rem;">📎 ${escapeHtml(p.evidence_file_name)}</a>` : ""}</td>
            <td>@${escapeHtml(p.created_by)}<br><small style="color:var(--text-muted)">${p.created_at ? new Date(p.created_at).toLocaleDateString("fr-FR") : ""}</small></td>
            <td>${p.expires_at ? new Date(p.expires_at).toLocaleDateString("fr-FR") : "—"}</td>
            <td>${stateBadge(p.state)}</td>
            <td>${p.state === "ACTIVE" ? `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: #fca5a5;" onclick="revokeWhitelistPair(${p.id})">Révoquer</button>` : ""}</td>
        </tr>
    `).join("");
}

async function revokeWhitelistPair(pairId) {
    const comment = await promptDialog("Révoquer la paire de liste blanche", {
        message: "Motif de la révocation (obligatoire) — les alertes de ce couple reprendront.",
        textarea: true, placeholder: "Motif réglementaire de la révocation..."
    });
    if (comment === null) return;
    try {
        const response = await fetch(`/api/whitelist/${pairId}/revoke`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment })
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Révocation refusée."), "error");
            return;
        }
        showToast(data.message, "success");
        fetchWhitelist();
    } catch (e) {
        console.error("Error revoking whitelist pair:", e);
    }
}

function openWhitelistModal(clientId, entityId, clientName, entityName) {
    document.getElementById("whitelist-client-id").value = clientId;
    document.getElementById("whitelist-entity-id").value = entityId;
    document.getElementById("whitelist-client-name").value = clientName || "";
    document.getElementById("whitelist-entity-name").value = entityName || "";
    document.getElementById("whitelist-modal-pair").innerHTML =
        `Couple <strong>${escapeHtml(clientName || clientId)}</strong> × <strong>${escapeHtml(entityName || entityId)}</strong> : les alertes futures seront supprimées (suppression tracée dans l'audit).`;
    document.getElementById("whitelist-justification").value = "";
    document.getElementById("whitelist-file").value = "";
    document.getElementById("whitelist-expires").value = "";
    // Asterisques selon les reglages modulaires
    const jMark = document.getElementById("whitelist-justification-required-mark");
    const fMark = document.getElementById("whitelist-file-required-mark");
    if (jMark && ingestionSettings) jMark.classList.toggle("hidden", !ingestionSettings.whitelist_justification_required);
    if (fMark && ingestionSettings) fMark.classList.toggle("hidden", !ingestionSettings.whitelist_file_required);
    document.getElementById("whitelist-modal").classList.remove("hidden");
}

function closeWhitelistModal() {
    document.getElementById("whitelist-modal").classList.add("hidden");
}

async function submitWhitelist(event) {
    event.preventDefault();
    const formData = new FormData();
    formData.append("client_id", document.getElementById("whitelist-client-id").value);
    formData.append("watchlist_entity_id", document.getElementById("whitelist-entity-id").value);
    formData.append("client_name", document.getElementById("whitelist-client-name").value);
    formData.append("watchlist_name", document.getElementById("whitelist-entity-name").value);
    formData.append("justification", document.getElementById("whitelist-justification").value.trim());
    const expires = document.getElementById("whitelist-expires").value;
    if (expires) formData.append("expires_at", expires);
    const fileInput = document.getElementById("whitelist-file");
    if (fileInput.files.length > 0) formData.append("file", fileInput.files[0]);
    try {
        const response = await fetch("/api/whitelist", { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Mise en liste blanche refusée."), "error");
            return;
        }
        closeWhitelistModal();
        showToast(data.message, "success");
        fetchWhitelist();
    } catch (e) {
        console.error("Error creating whitelist pair:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

// ------------------ PILOTAGE (KPI CONFORMITE) ------------------

async function fetchKpis() {
    try {
        const response = await fetch("/api/kpi");
        if (!response.ok) return;
        const k = await response.json();

        const tile = (label, value, color) => `
            <div class="metric" style="flex: 1; min-width: 170px; background: rgba(255,255,255,0.04); padding: 1rem; border-radius: 8px; border: 1px solid var(--border-color);">
                <span class="metric-label" style="font-weight: 600;">${label}</span>
                <span class="metric-value" style="font-size: 1.5rem; ${color ? "color: " + color + ";" : ""}">${value}</span>
            </div>`;

        const a = k.alerts || {};
        document.getElementById("kpi-tiles").innerHTML =
            tile("Alertes ouvertes", a.open ?? 0, "var(--color-warning)") +
            tile("Faux positifs clos", a.closed_false_positive ?? 0, "var(--color-safe)") +
            tile("Vrais positifs confirmés", a.closed_confirmed ?? 0, "var(--color-alert)") +
            tile("Taux de faux positifs", a.false_positive_rate_pct !== null && a.false_positive_rate_pct !== undefined ? a.false_positive_rate_pct + " %" : "—") +
            tile("Délai moyen de décision", a.avg_decision_hours !== null && a.avg_decision_hours !== undefined ? a.avg_decision_hours + " h" : "—") +
            tile("Paires en liste blanche", k.whitelist_active_pairs ?? 0);

        const listsBody = document.querySelector("#kpi-lists-table tbody");
        const byType = (k.lists || {}).production_entities_by_type || {};
        listsBody.innerHTML = Object.keys(byType).length
            ? Object.entries(byType).map(([t, n]) => `<tr><td>${listTypeBadge(t)}</td><td><strong>${n}</strong></td></tr>`).join("")
            : '<tr><td colspan="2" style="color: var(--text-muted); text-align: center;">Aucune liste en production.</td></tr>';

        const syncsBody = document.querySelector("#kpi-syncs-table tbody");
        const syncs = k.recent_syncs || [];
        syncsBody.innerHTML = syncs.length
            ? syncs.map(s => `<tr>
                <td>${s.executed_at ? new Date(s.executed_at).toLocaleString("fr-FR") : "-"}</td>
                <td>${escapeHtml(SYNC_SOURCE_LABELS[s.source] || s.source)} <small style="color:var(--text-muted)">${escapeHtml(s.trigger)}</small></td>
                <td>${escapeHtml(s.status)}</td>
                <td><small>+${s.added} / ~${s.modified} / -${s.removed}</small></td>
              </tr>`).join("")
            : '<tr><td colspan="4" style="color: var(--text-muted); text-align: center;">Aucune synchronisation.</td></tr>';
    } catch (e) {
        console.error("Error fetching KPIs:", e);
    }
}

// ------------------ P3 : FILTRAGE TRANSACTIONNEL ISO 20022 ------------------

async function runTransactionScreening() {
    const input = document.getElementById("txn-file-input");
    if (!input.files || !input.files.length) {
        showToast("Sélectionnez un message de paiement XML (pain.001 ou pacs.008).", "error");
        return;
    }
    const btn = document.getElementById("txn-screen-btn");
    btn.disabled = true;
    btn.textContent = "Filtrage en cours...";
    try {
        const formData = new FormData();
        formData.append("file", input.files[0]);
        const response = await fetch("/api/transactions/screen", { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (typeof data.detail === "string" ? data.detail : "message invalide."), "error");
            return;
        }
        renderTransactionResult(data);
    } catch (e) {
        console.error("Transaction screening error:", e);
        showToast("Erreur lors du filtrage transactionnel.", "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Filtrer le paiement";
    }
}

function renderTransactionResult(data) {
    document.getElementById("txn-results-container").classList.remove("hidden");
    const verdictLine = document.getElementById("txn-verdict-line");
    if (data.verdict === "HIT") {
        verdictLine.innerHTML = `Verdict : <span class="status-badge alert">HIT — ${data.hits_count} partie(s) en alerte</span>`;
    } else {
        verdictLine.innerHTML = `Verdict : <span class="status-badge safe">PASS — aucune correspondance</span>`;
    }
    const m = data.message || {};
    document.getElementById("txn-message-info").textContent =
        `Message ${m.message_type || "?"} · MsgId ${m.msg_id || "—"} · ${data.transactions_count} transaction(s) · ` +
        `${(data.parties || []).length} partie(s) distincte(s) criblée(s).`;

    const tbody = document.querySelector("#txn-results-table tbody");
    tbody.innerHTML = (data.parties || []).map(p => {
        let badge;
        if (p.status === "ALERT") badge = '<span class="status-badge alert">ALERT</span>';
        else if (p.status === "WHITELISTED") badge = '<span class="status-badge warning">WHITELISTED</span>';
        else badge = '<span class="status-badge safe">NO_MATCH</span>';
        return `<tr>
            <td>${escapeHtml(p.name)}${p.is_agent ? ' <small style="color:var(--text-muted)">(banque)</small>' : ""}</td>
            <td><small>${escapeHtml((p.roles || []).join(", "))}</small></td>
            <td>${escapeHtml(p.country || "—")}</td>
            <td><small>${escapeHtml(p.bic || "—")}</small></td>
            <td>${p.best_watchlist_name ? p.final_score.toFixed(1) + " %" : "—"}</td>
            <td>${p.best_watchlist_name ? escapeHtml(p.best_watchlist_name) + (p.list_type ? ` <small style="color:var(--text-muted)">${escapeHtml(p.list_type)}</small>` : "") : "—"}</td>
            <td>${badge}${p.hard_match ? ' <small style="color:var(--color-alert)">hard match</small>' : ""}</td>
            <td>${p.alert_id ? `<a href="#" onclick="switchTab('alerts'); openAlertModal(${p.alert_id}); return false;">#${p.alert_id}</a>` : "—"}</td>
        </tr>`;
    }).join("");
}

// ------------------ P3 : NARRATIF D'ALERTE & ADVERSE MEDIA ------------------

let currentAlertNames = { client: "", watchlist: "" };

async function generateAlertNarrative() {
    if (!currentAlertId) return;
    const container = document.getElementById("alert-narrative-container");
    container.classList.remove("hidden");
    container.innerHTML = '<small style="color: var(--text-muted);">Génération du narratif...</small>';
    try {
        const response = await fetch(`/api/alerts/${currentAlertId}/narrative`, { method: "POST" });
        const data = await response.json();
        if (!response.ok) {
            container.innerHTML = `<small style="color: var(--color-alert);">Erreur : ${escapeHtml(data.detail || "génération impossible.")}</small>`;
            return;
        }
        container.innerHTML = `
            <h3 style="font-size: 0.95rem; margin: 0 0 0.5rem;">Projet de narratif ${data.llm_used ? '<small style="color: var(--text-muted);">(reformulé par IA — à relire)</small>' : '<small style="color: var(--text-muted);">(déterministe, fondé sur l\'audit)</small>'}</h3>
            <textarea id="alert-narrative-text" rows="12" style="width: 100%; font-size: 0.85rem;">${escapeHtml(data.narrative)}</textarea>
            <div style="display: flex; gap: 0.5rem; margin-top: 0.5rem;">
                <button class="btn btn-sm btn-secondary" onclick="copyNarrative()">📋 Copier</button>
            </div>
            <small style="color: var(--text-muted);">La décision (vrai/faux positif) reste humaine et soumise à la validation 4-yeux — ce texte est un brouillon éditable.</small>
        `;
    } catch (e) {
        console.error("Narrative error:", e);
        container.innerHTML = '<small style="color: var(--color-alert);">Erreur lors de la génération.</small>';
    }
}

function copyNarrative() {
    const ta = document.getElementById("alert-narrative-text");
    if (!ta) return;
    ta.select();
    navigator.clipboard.writeText(ta.value).then(() => {}, () => document.execCommand("copy"));
}

async function fetchAlertAdverseMedia(which) {
    const name = which === "watchlist" ? currentAlertNames.watchlist : currentAlertNames.client;
    if (!name) return;
    const container = document.getElementById("alert-adverse-container");
    container.classList.remove("hidden");
    container.innerHTML = `<small style="color: var(--text-muted);">Recherche presse sur « ${escapeHtml(name)} »...</small>`;
    try {
        const response = await fetch(`/api/adverse-media?name=${encodeURIComponent(name)}`);
        const data = await response.json();
        if (!response.ok) {
            container.innerHTML = `<small style="color: var(--color-alert);">Erreur : ${escapeHtml(data.detail || "recherche impossible.")}</small>`;
            return;
        }
        const articles = data.articles || [];
        container.innerHTML = `
            <h3 style="font-size: 0.95rem; margin: 0 0 0.5rem;">Adverse media — « ${escapeHtml(data.name)} » <small style="color: var(--text-muted);">(${articles.length} article(s), informatif uniquement)</small></h3>
            ${articles.length ? articles.map(art => `
                <div style="border-left: 2px solid var(--border-color); padding: 0.35rem 0 0.35rem 0.75rem; margin-left: 0.25rem;">
                    <a href="${escapeHtml(art.link)}" target="_blank" rel="noopener noreferrer" style="font-size: 0.85rem;">${escapeHtml(art.title)}</a>
                    <div><small style="color: var(--text-muted);">${escapeHtml(art.source || "")} ${art.published ? "· " + escapeHtml(art.published) : ""}</small></div>
                </div>
            `).join("") : '<small style="color: var(--text-muted);">Aucun article trouvé avec les mots-clés LCB-FT configurés.</small>'}
        `;
    } catch (e) {
        console.error("Adverse media error:", e);
        container.innerHTML = '<small style="color: var(--color-alert);">Le fournisseur presse est injoignable.</small>';
    }
}
