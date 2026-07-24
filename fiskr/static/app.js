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

// Champs cherchables de la vue « Listés — Base de Données » (groupe → [valeur, libellé])
// Les valeurs correspondent au paramètre search_field de GET /api/watchlist/db
const WL_SEARCH_FIELD_GROUPS = [
    ["Recherche", [
        ["default", "Champs indexés (nom, ID, LEI, IMO)"],
        ["any", "🔎 Tout champ"],
    ]],
    ["Identité", [
        ["primary_name", "Nom principal / Raison sociale"],
        ["individual_name_parsed", "Prénom / Nom / Nom de jeune fille"],
        ["aliases", "Alias"],
        ["entity_type", "Type d'entité (I/E/V/O)"],
        ["gender", "Genre"],
        ["dates_of_birth", "Dates de naissance"],
        ["date_of_death", "Date de décès"],
    ]],
    ["Localisation", [
        ["countries", "Pays rattachés"],
        ["place_of_birth", "Lieu de naissance"],
        ["address", "Adresse"],
        ["city", "Ville"],
        ["state", "État / Région"],
        ["country", "Pays"],
        ["alternative_addresses", "Adresses alternatives"],
    ]],
    ["Références", [
        ["official_reference", "Référence officielle"],
        ["designation", "Fonction / Désignation"],
        ["designation_reasons", "Motifs de la désignation"],
        ["additional_informations", "Informations additionnelles"],
        ["origin", "Origine / Source"],
        ["sanction_programs", "Programmes de sanctions"],
        ["listed_on", "Date d'inscription"],
        ["pep_role", "Fonction PEP"],
        ["designating_state", "État désignant"],
        ["title", "Titre"],
        ["name_original_script", "Nom (écriture d'origine)"],
        ["secondary_sanctions_risk", "Sanctions secondaires"],
    ]],
    ["Identifiants", [
        ["entity_id", "ID de fiche"],
        ["lei_number", "LEI"],
        ["bic_swift", "BIC / SWIFT"],
        ["tax_id", "Numéro fiscal"],
        ["duns_number", "D-U-N-S"],
        ["crypto_wallets", "Adresses crypto"],
        ["imo_number", "IMO (navire)"],
        ["vessel_mmsi", "MMSI (navire)"],
        ["vessel_call_sign", "Indicatif radio (navire)"],
        ["aircraft_tail_number", "Tail Number (aéronef)"],
        ["passport_documents", "Passeports"],
        ["national_id_documents", "Cartes d'identité"],
        ["national_registry_ids", "Registres nationaux"],
        ["other_registration_ids", "Autres enregistrements"],
        ["other_id_documents", "Autres documents"],
    ]],
    ["Contact", [
        ["phone_numbers", "Téléphones"],
        ["email_addresses", "Emails"],
        ["websites", "Sites web"],
    ]],
];

function initWatchlistFieldFilter() {
    const select = document.getElementById("wl-field-filter");
    if (!select) return;
    select.innerHTML = WL_SEARCH_FIELD_GROUPS.map(([group, fields]) => `
        <optgroup label="${group}">
            ${fields.map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
        </optgroup>`).join("");
}

// Changement du champ de recherche : placeholder adapté + relance de la recherche
function onWatchlistFieldChange() {
    const select = document.getElementById("wl-field-filter");
    const input = document.getElementById("wl-search-input");
    if (select && input) {
        const label = select.options[select.selectedIndex]?.textContent || "";
        input.placeholder = select.value === "default"
            ? "🔍 Rechercher (nom, ID, LEI, IMO)..."
            : `🔍 Rechercher dans : ${label.replace("🔎 ", "")}...`;
        if (input.value.trim()) filterWatchlist();
        input.focus();
    }
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
function _openAppDialog({ title, message, input, textarea, placeholder, required, danger, confirmLabel, cancelLabel, password }) {
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
            if (!textarea && password) inputEl.type = "password";
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
                            required: options.required !== false, password: options.password,
                            confirmLabel: options.confirmLabel || "Valider" });
}

// ------------------ THÈME (clair / sombre) & NAVIGATION RESPONSIVE ------------------

function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function applyTheme(theme) {
    if (theme === "light") {
        document.documentElement.setAttribute("data-theme", "light");
    } else {
        document.documentElement.removeAttribute("data-theme");
    }
    try { localStorage.setItem("fiskr_theme", theme); } catch (e) { /* stockage indisponible */ }
    const btn = document.getElementById("theme-toggle-btn");
    if (btn) btn.textContent = theme === "light" ? "☀️" : "🌙";
}

function toggleTheme() {
    applyTheme(currentTheme() === "light" ? "dark" : "light");
}

// Sidebar rétractable (mobile / tablette) : classe sur <body>, overlay cliquable
function toggleSidebar(force) {
    const open = force !== undefined ? force : !document.body.classList.contains("sidebar-open");
    document.body.classList.toggle("sidebar-open", open);
}

// ------------------ APPELS API CENTRALISÉS ------------------

// Wrapper unique : erreurs réseau signalées, session expirée redirigée vers /login.
// options.silent = pas de toast (polling de badges, rafraîchissements de fond).
async function apiFetch(url, options = {}) {
    const { silent, ...fetchOptions } = options;
    // Langue active envoyée au backend : les messages detail/message des
    // réponses JSON arrivent traduits (fiskr/apimessages.py)
    if (window.fiskrI18n && fiskrI18n.currentLang() !== "fr") {
        fetchOptions.headers = { ...(fetchOptions.headers || {}),
                                 "Accept-Language": fiskrI18n.currentLang() };
    }
    let response;
    try {
        response = await fetch(url, fetchOptions);
    } catch (e) {
        if (!silent) showToast("Serveur injoignable. Vérifiez votre connexion.", "error");
        throw e;
    }
    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Session expirée");
    }
    return response;
}

// ------------------ FORMATAGE DES DATES (fr-FR) ------------------

// Locale d'affichage suivant la langue active (i18n), repli francais
function uiLocale() {
    return (window.fiskrI18n && window.fiskrI18n.locale) ? window.fiskrI18n.locale() : uiLocale();
}

function formatDateTime(value) {
    if (!value) return "—";
    const d = new Date(value);
    return isNaN(d.getTime()) ? String(value) : d.toLocaleString(uiLocale(), { dateStyle: "short", timeStyle: "short" });
}

function formatDate(value) {
    if (!value) return "—";
    const d = new Date(value);
    return isNaN(d.getTime()) ? String(value) : d.toLocaleDateString(uiLocale());
}

// ------------------ ÉTATS DE TABLES (chargement / vide) ------------------

function _tbodyOf(target) {
    return typeof target === "string" ? document.getElementById(target) : target;
}

// Lignes squelettes pendant un fetch
function tableLoading(target, cols, rows = 3) {
    const tbody = _tbodyOf(target);
    if (!tbody) return;
    const cells = Array.from({ length: cols }, () => '<td><span class="skeleton-cell"></span></td>').join("");
    tbody.innerHTML = Array.from({ length: rows }, () => `<tr>${cells}</tr>`).join("");
}

// État vide homogène
function tableEmpty(target, cols, message, icon = "📭") {
    const tbody = _tbodyOf(target);
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="${cols}" class="empty-state"><span class="empty-icon">${icon}</span>${escapeHtml(message)}</td></tr>`;
}

// ------------------ TRI DES COLONNES (côté client) ------------------

// Tri générique de toutes les tables rendues en mémoire : clic sur un <th>.
// Les tables paginées côté serveur portent data-no-client-sort et gèrent
// leur tri via l'API (ex. Listés — Base de Données).
function _sortValue(cellText) {
    const cleaned = cellText.replace(/[%\s ]/g, "").replace(",", ".");
    const n = parseFloat(cleaned);
    return isNaN(n) || !/^[-+]?[\d.,]+$/.test(cleaned) ? null : n;
}

function sortTableByHeader(th) {
    const table = th.closest("table");
    const tbody = table ? table.querySelector("tbody") : null;
    if (!tbody) return;
    const idx = Array.from(th.parentNode.children).indexOf(th);
    const asc = !th.classList.contains("sort-asc");
    table.querySelectorAll("th").forEach((h) => h.classList.remove("sort-asc", "sort-desc"));
    th.classList.add(asc ? "sort-asc" : "sort-desc");
    const rows = Array.from(tbody.querySelectorAll(":scope > tr")).filter((r) => !r.querySelector(".empty-state"));
    const text = (row) => (row.children[idx] ? row.children[idx].textContent.trim() : "");
    rows.sort((a, b) => {
        const ta = text(a), tb = text(b);
        const na = _sortValue(ta), nb = _sortValue(tb);
        const cmp = (na !== null && nb !== null)
            ? na - nb
            : ta.localeCompare(tb, "fr", { numeric: true, sensitivity: "base" });
        return asc ? cmp : -cmp;
    });
    rows.forEach((r) => tbody.appendChild(r));
}

// Rend cliquables les en-têtes des tables client (affordance visuelle .sortable)
function initSortableTables() {
    document.querySelectorAll("table").forEach((table) => {
        if (table.hasAttribute("data-no-client-sort")) return;
        table.querySelectorAll("thead th").forEach((th) => {
            if (!th.textContent.trim() || th.classList.contains("no-sort")) return;
            th.classList.add("sortable");
        });
    });
    document.addEventListener("click", (e) => {
        const th = e.target.closest("th.sortable");
        if (th && !th.closest("table")?.hasAttribute("data-no-client-sort")) sortTableByHeader(th);
    });
}

// ------------------ LIBELLÉS FRANÇAIS DES STATUTS ------------------

const STATUS_LABELS = {
    // Alertes
    OPEN: "Ouverte", IN_PROGRESS: "En cours", ESCALATED: "Escaladée",
    PENDING_VALIDATION: "À valider (4 yeux)", CLOSED_CONFIRMED: "Vrai positif",
    CLOSED_FALSE_POSITIVE: "Faux positif", CLOSED_BY_RULE: "Close par règle",
    // Snapshots
    READY: "En production", PENDING_REVIEW: "En homologation", SUPERSEDED: "Remplacé",
    REJECTED: "Rejeté", PROCESSING: "En traitement", ERROR: "Erreur",
    // Règles
    DRAFT: "Brouillon", ACTIVE: "Active",
    // Divers
    ADDED: "Ajouté", REMOVED: "Supprimé", MODIFIED: "Modifié",
    NO_MATCH: "Aucun match", ALERT: "Alerte", WHITELISTED: "Liste blanche",
    SUCCESS: "Succès", NO_CHANGE: "Sans changement",
};

function statusLabel(status) {
    return STATUS_LABELS[status] || status || "—";
}

// ------------------ ACCESSIBILITÉ (modales, onglets) ------------------

function initA11y() {
    // Échap ferme la modale visible la plus haute (la modale générique gère déjà le sien)
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        const open = Array.from(document.querySelectorAll(".modal:not(.hidden)"))
            .filter((m) => m.id !== "app-dialog").pop();
        if (open) open.classList.add("hidden");
    });
    document.querySelectorAll(".modal").forEach((m) => {
        m.setAttribute("role", "dialog");
        m.setAttribute("aria-modal", "true");
        // Clic sur le fond = fermeture (hors modale générique à Promise)
        if (m.id !== "app-dialog") {
            m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); });
        }
    });
    document.querySelectorAll(".sub-tabs").forEach((bar) => bar.setAttribute("role", "tablist"));
    document.querySelectorAll(".sub-tab-btn").forEach((b) => {
        b.setAttribute("role", "tab");
        b.setAttribute("aria-selected", b.classList.contains("active") ? "true" : "false");
    });
}

// ------------------ COMPTEURS DE LA BARRE LATÉRALE (badges) ------------------

async function refreshSidebarCounters() {
    try {
        const response = await apiFetch("/api/counters", { silent: true });
        if (!response.ok) return;
        const c = await response.json();
        const alertBadge = document.getElementById("alerts-open-badge");
        if (alertBadge) {
            alertBadge.textContent = c.open_alerts;
            alertBadge.classList.toggle("hidden", !c.open_alerts);
        }
        // Badges par canal sur les sous-onglets Criblage / Filtrage
        const scrBadge = document.getElementById("alerts-screening-badge");
        if (scrBadge) {
            scrBadge.textContent = c.open_alerts_screening ?? 0;
            scrBadge.classList.toggle("hidden", !c.open_alerts_screening);
        }
        const filBadge = document.getElementById("alerts-filtering-badge");
        if (filBadge) {
            filBadge.textContent = c.open_alerts_filtering ?? 0;
            filBadge.classList.toggle("hidden", !c.open_alerts_filtering);
        }
        const reviewBadge = document.getElementById("review-pending-badge");
        if (reviewBadge) {
            reviewBadge.textContent = c.pending_reviews;
            reviewBadge.classList.toggle("hidden", !c.pending_reviews);
        }
        // Centre de notifications 🔔 (badge + panneau si ouvert)
        _lastCounters = c;
        renderNotifCenter();
    } catch (e) { /* silencieux : simple polling de badges */ }
}

// Peuple les selects de filtre « Liste » et les cases du périmètre de criblage
function initListTypeControls() {
    const selects = [
        ["wl-list-filter", false],
        ["snapshots-list-filter", false],
        ["screening-list-filter", true],
        ["filtering-list-filter", true],
        ["audit-list-filter", true],
        ["whitelist-list-filter", true],
    ];
    for (const [id, withUnknown] of selects) {
        const el = document.getElementById(id);
        if (el) el.innerHTML = listTypeFilterOptions(withUnknown);
    }
    initWatchlistFieldFilter();
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
    // Thème (icône du bouton), accessibilité et tri des tables
    applyTheme(currentTheme());
    initA11y();
    initSortableTables();
    initCommandPalette();
    initHashRouting();
    initDropZones();
    // Check authentication and load user info
    checkAuthUser();
    initListTypeControls();
    // Initial data loading — l'accueil d'abord (onglet par défaut)
    fetchHomeDashboard();
    fetchWatchlist();
    fetchWatchlistHash();
    fetchAuditHistory();
    fetchSnapshots();
    fetchConfig();
    fetchIngestionSettings();
    fetchPendingReviews();
    fetchAlerts("SCREENING");
    fetchWhitelist();
    refreshSidebarCounters();
    // Badges vivants : compteurs légers rafraîchis toutes les 60 s
    setInterval(refreshSidebarCounters, 60_000);
});

// Check current logged-in user profile
async function checkAuthUser() {
    try {
        const response = await apiFetch("/api/auth/me");
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
                const labels = { admin: "Administrateur (ACPR/AMF)", reviewer: "Réviseur Homologation", user: "Analyste Conformité", blocking: "Paramétrage Blocking", rules: "Règles Faux Positifs" };
                roleEl.textContent = roles.map(r => labels[r] || r).join(" / ") || "Analyste Conformité";
            }
            if (navUsersItem) {
                navUsersItem.classList.toggle("hidden", !isAdmin);
            }
            // Onglet Paramètres (réglages transverses) réservé aux admins
            const navSettingsItem = document.getElementById("nav-item-settings");
            if (navSettingsItem) navSettingsItem.classList.toggle("hidden", !isAdmin);
            // Journal des actions d'administration (sous-onglet Audit, admin)
            const adminLogBtn = document.getElementById("sub-btn-audit-admin");
            if (adminLogBtn) adminLogBtn.classList.toggle("hidden", !isAdmin);
            // Carte des réglages (dans l'onglet Paramètres) et actions de revue (reviewer ou admin)
            const settingsCard = document.getElementById("review-settings-card");
            if (settingsCard) settingsCard.classList.toggle("hidden", !isAdmin);
            const apiKeysCard = document.getElementById("apikeys-card");
            if (apiKeysCard) apiKeysCard.classList.toggle("hidden", !isAdmin);
            const retentionCard = document.getElementById("retention-card");
            if (retentionCard) retentionCard.classList.toggle("hidden", !isAdmin);
            const portabilityCard = document.getElementById("config-portability-card");
            if (portabilityCard) portabilityCard.classList.toggle("hidden", !isAdmin);
            const scoringCard = document.getElementById("scoring-card");
            if (scoringCard) scoringCard.classList.toggle("hidden", !isAdmin);
            const checklistCard = document.getElementById("checklist-card");
            if (checklistCard) checklistCard.classList.toggle("hidden", !isAdmin);
            const reviewActions = document.getElementById("review-actions");
            if (reviewActions) reviewActions.classList.toggle("hidden", !isReviewer);
            const exclusionToolbar = document.getElementById("review-exclusion-toolbar");
            if (exclusionToolbar) exclusionToolbar.classList.toggle("hidden", !isReviewer);
            // Sous-onglets de paramétrage (rôles dédiés, admin passe toujours)
            const canBlocking = isAdmin || roles.includes("blocking");
            const canRules = isAdmin || roles.includes("rules");
            const blockingBtn = document.getElementById("sub-btn-alerts-blocking");
            if (blockingBtn) blockingBtn.classList.toggle("hidden", !canBlocking);
            const rulesBtn = document.getElementById("sub-btn-alerts-rules");
            if (rulesBtn) rulesBtn.classList.toggle("hidden", !canRules);
        }
    } catch (e) {
        console.error("Auth check failed:", e);
    }
}

// Handle User Logout
async function handleLogout() {
    if (!await confirmDialog("Voulez-vous vraiment vous déconnecter de Fiskr ?", { title: "Déconnexion" })) return;
    try {
        await apiFetch("/api/auth/logout", { method: "POST" });
    } catch (e) {
        console.error("Logout request error:", e);
    } finally {
        window.location.href = "/login";
    }
}

// Fetch Active System Configuration
async function fetchConfig() {
    try {
        const response = await apiFetch("/api/config");
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

    // Mobile : replier la sidebar après la navigation
    toggleSidebar(false);
    // Deep link : l'URL suit la navigation (boutons navigateur, partage)
    updateLocationHash(tabId);

    // Refresh tab-specific data
    if (tabId === "home") {
        fetchHomeDashboard();
    }
    if (tabId === "alerts") {
        fetchAlerts("SCREENING");
        fetchAlerts("FILTERING");
        fetchWhitelist();
        fetchSavedViews("SCREENING");
        fetchSavedViews("FILTERING");
    }
    if (tabId === "kpi") {
        fetchKpis();
        initActivityReportDates();
        fetchWorkload();
    }
    if (tabId === "settings") {
        fetchIngestionSettings();
        fetchApiKeys();
        refreshMfaCard();
        fetchRetentionSettings();
        fetchAbsenceCard();
        fetchScoringSettings();
        fetchChecklistSettings();
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
        btn.setAttribute("aria-selected", "false");
    });

    // Activate clicked sub-tab button
    const activeBtn = document.getElementById(`sub-btn-${subTabId}`);
    if (activeBtn) {
        activeBtn.classList.add("active");
        activeBtn.setAttribute("aria-selected", "true");
    }
    
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
    updateLocationHash(sectionId, subTabId);

    // Refresh sub-tab specific data if needed
    if (subTabId === "watchlist-active") {
        fetchWatchlist();
    } else if (subTabId === "watchlist-snapshots") {
        fetchSnapshots();
    } else if (subTabId === "watchlist-review") {
        // Rupture de flux corrigée : les snapshots en attente d'homologation
        // sont rechargés à chaque ouverture du sous-onglet, plus seulement au load
        fetchPendingReviews();
    } else if (subTabId === "alerts-screening") {
        fetchAlerts("SCREENING");
    } else if (subTabId === "alerts-filtering") {
        fetchAlerts("FILTERING");
    } else if (subTabId === "alerts-whitelist") {
        fetchWhitelist();
    } else if (subTabId === "alerts-blocking") {
        fetchBlockingSettings();
    } else if (subTabId === "alerts-rules") {
        fetchFpRules();
    } else if (subTabId === "screening-batch") {
        fetchBatchCampaigns();
    } else if (subTabId === "audit-screening") {
        fetchAuditHistory(1);
    } else if (subTabId === "audit-admin") {
        fetchAdminLog();
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
        const response = await apiFetch("/api/watchlist/entity", {
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
        const response = await apiFetch("/api/snapshots");
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
        tableEmpty(tbody, 5, "Aucun snapshot importé");
        return;
    }

    snaps.forEach(snap => {
        const dateStr = new Date(snap.uploaded_at).toLocaleString(uiLocale());
        const tr = document.createElement("tr");

        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(snap.file_name)}</strong><br><small style="color:var(--text-muted)">Hash: ${snap.file_hash.substring(0,8)}...</small></td>
            <td>${listTypeBadge(snap.file_type)}</td>
            <td>${snap.status === "PROCESSING" && snap.processed_count
                ? `${snap.processed_count.toLocaleString(uiLocale())}…` : snap.record_count}</td>
            <td>${snapshotStatusBadge(snap.status, snap)}</td>
        `;
        tbody.appendChild(tr);
    });
}

// Badge de statut d'un snapshot (incl. cycle de vie homologation)
function snapshotStatusBadge(status, snap) {
    if (status === "PENDING_REVIEW") {
        return '<span class="status-dot orange"></span> <span style="color: var(--color-warning); font-weight: 600;">EN ATTENTE D\'HOMOLOGATION</span>';
    }
    if (status === "REJECTED") {
        return '<span class="status-dot" style="background: var(--color-alert);"></span> <span style="color: var(--color-alert); font-weight: 600;">REJETÉ</span>';
    }
    if (status === "PROCESSING" && snap && snap.phase) {
        const phaseLabel = PROGRESS_PHASE_LABELS[snap.phase] || snap.phase;
        return `<span class="status-dot orange"></span> PROCESSING <small style="color:var(--text-muted)">— ${escapeHtml(phaseLabel)}</small>`;
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
        const dateStr = new Date(snap.uploaded_at).toLocaleString(uiLocale());
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

// ------------------ PROGRESSION DES OPERATIONS LONGUES ------------------
// Libellés français des phases renvoyées par GET /api/progress
const PROGRESS_PHASE_LABELS = {
    UPLOAD: "Téléversement du fichier…",
    DOWNLOAD: "Téléchargement depuis la source…",
    HASH: "Calcul de l'empreinte SHA-256…",
    PARSE: "Analyse du fichier…",
    PERSIST: "Enregistrement des fiches…",
    DELTA: "Calcul du delta…",
    RELOAD: "Rechargement du cache de production…",
    DONE: "Terminé",
};

// Démarre l'interrogation périodique de GET /api/progress?id=<token> et
// alimente la barre #<barPrefix>-progress. Retourne une fonction stop().
function startProgressPolling(token, barPrefix, intervalMs = 1500) {
    const wrap = document.getElementById(`${barPrefix}-progress`);
    const phaseEl = document.getElementById(`${barPrefix}-progress-phase`);
    const countEl = document.getElementById(`${barPrefix}-progress-count`);
    const fillEl = document.getElementById(`${barPrefix}-progress-fill`);
    if (!wrap) return () => {};
    wrap.classList.remove("hidden");
    if (phaseEl) phaseEl.textContent = "Préparation…";
    if (countEl) countEl.textContent = "";
    if (fillEl) { fillEl.style.width = "0%"; fillEl.classList.add("indeterminate"); }

    let stopped = false;
    const tick = async () => {
        if (stopped) return;
        try {
            const resp = await apiFetch(`/api/progress?id=${encodeURIComponent(token)}`);
            if (resp.ok) {
                const p = await resp.json();
                if (phaseEl) phaseEl.textContent = PROGRESS_PHASE_LABELS[p.phase] || p.phase || "En cours…";
                if (countEl) {
                    let txt = "";
                    if (p.processed) {
                        txt = p.total
                            ? `${p.processed.toLocaleString(uiLocale())} / ${p.total.toLocaleString(uiLocale())}`
                            : p.processed.toLocaleString(uiLocale());
                    }
                    countEl.textContent = txt;
                }
                if (fillEl) {
                    if (p.pct !== null && p.pct !== undefined) {
                        fillEl.classList.remove("indeterminate");
                        fillEl.style.width = `${Math.min(100, p.pct)}%`;
                    } else {
                        fillEl.classList.add("indeterminate");
                        fillEl.style.width = "100%";
                    }
                }
            }
        } catch (e) { /* la progression ne doit jamais casser l'opération */ }
    };
    const timer = setInterval(tick, intervalMs);
    tick();
    return function stop() {
        stopped = true;
        clearInterval(timer);
        wrap.classList.add("hidden");
        if (fillEl) fillEl.classList.remove("indeterminate");
    };
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
    
    // Jeton de progression : le serveur alimente GET /api/progress pendant
    // que la requête d'import est encore en vol (gros fichiers)
    const progressId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
        : `ing-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    formData.append("progress_id", progressId);

    btn.disabled = true;
    btn.textContent = "Importation en cours...";
    const stopProgress = startProgressPolling(progressId, "ingest");

    try {
        const response = await apiFetch("/api/ingest", {
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
        stopProgress();
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

    // Progression en direct dans le bouton (phase + compteur), jeton sync:<source>
    const syncToken = `sync:${source.toLowerCase()}`;
    const syncTimer = setInterval(async () => {
        try {
            const resp = await apiFetch(`/api/progress?id=${encodeURIComponent(syncToken)}`);
            if (!resp.ok) return;
            const p = await resp.json();
            if (p.status !== "RUNNING") return;
            const label = PROGRESS_PHASE_LABELS[p.phase] || p.phase || "";
            const count = p.processed ? ` ${p.processed.toLocaleString(uiLocale())}${p.total ? " / " + p.total.toLocaleString(uiLocale()) : ""}` : "";
            btn.textContent = `${label}${count}`;
        } catch (e) { /* jamais bloquant */ }
    }, 1500);

    try {
        const response = await apiFetch("/api/sync/run", {
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
        clearInterval(syncTimer);
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// Load and render the synchronization reports history
async function fetchSyncReports() {
    try {
        const response = await apiFetch("/api/sync/reports");
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
        const dateStr = new Date(report.executed_at).toLocaleString(uiLocale());
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";

        let statusBadge;
        if (report.status === "SUCCESS") statusBadge = '<span class="status-badge no_match">SUCCESS</span>';
        else if (report.status === "ERROR") statusBadge = '<span class="status-badge alert">ERROR</span>';
        else statusBadge = `<span class="status-badge warning">${escapeHtml(report.status)}</span>`;

        // Echecs partiels (actes/PDF inaccessibles) : la synchronisation a
        // abouti mais une partie de la source n'a pas pu être récupérée
        const delta = report.delta_report || {};
        const partialFailures = (delta.fetch_failures || []).length + (delta.pdf_failures || []).length;
        if (partialFailures > 0 && report.status !== "ERROR") {
            statusBadge += ` <span class="status-badge warning" title="${partialFailures} élément(s) inaccessibles — repris au prochain run">⚠ ${partialFailures}</span>`;
        }

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
// Sources planifiables (clef API -> libellé de SYNC_SOURCE_LABELS)
const CRON_SOURCE_KEYS = {
    ofac: "OFAC", eurlex: "EURLEX", dgt: "DGT", eu_fsf: "EUFSF",
    un: "UN", pep: "PEP", ofsi: "OFSI",
};

async function fetchSyncConfig() {
    try {
        const response = await apiFetch("/api/sync/config");
        if (!response.ok) return;
        const cfg = await response.json();
        const info = document.getElementById("sync-schedule-info");
        const autoTxt = cfg.auto_enabled
            ? "⏰ Planificateur cron actif : chaque source suit sa propre expression ci-dessous."
            : "⏸️ Synchronisation automatique désactivée (sync.auto_enabled dans config.yaml).";
        const mailTxt = cfg.email_configured
            ? "Rapports envoyés par email (SMTP configuré)."
            : "Rapports disponibles dans l'application uniquement (SMTP non configuré).";
        if (info) info.textContent = `${autoTxt} ${mailTxt}`;
        renderCronSchedules(cfg);
    } catch (e) {
        console.error("Error fetching sync config:", e);
    }
}

// ------------------ PLANIFICATION CRON PAR SOURCE ------------------

function renderCronSchedules(cfg) {
    const tbody = document.querySelector("#cron-schedules-table tbody");
    if (!tbody) return;
    const isAdmin = currentUser && userRoles(currentUser).includes("admin");
    const schedules = cfg.schedules || {};
    const nextRuns = cfg.next_runs || {};
    tbody.innerHTML = Object.entries(CRON_SOURCE_KEYS).map(([source, labelKey]) => {
        const enabled = (cfg[source] || {}).enabled;
        return `<tr>
            <td>${escapeHtml(SYNC_SOURCE_LABELS[labelKey] || labelKey)}
                ${enabled ? "" : '<br><small style="color: var(--text-muted);">source désactivée</small>'}</td>
            <td><input type="text" id="cron-${source}" value="${escapeHtml(schedules[source] || "")}"
                       style="font-family: monospace; max-width: 200px;" ${isAdmin ? "" : "disabled"}
                       title="Expression cron 5 champs : minute heure jour mois jour-de-semaine"></td>
            <td>${nextRuns[source] ? formatDateTime(nextRuns[source]) : "—"}</td>
        </tr>`;
    }).join("");
    const saveBtn = document.getElementById("cron-save-btn");
    if (saveBtn) saveBtn.classList.toggle("hidden", !isAdmin);
}

async function saveCronSchedules() {
    const schedules = {};
    for (const source of Object.keys(CRON_SOURCE_KEYS)) {
        const el = document.getElementById(`cron-${source}`);
        if (el) schedules[source] = el.value.trim();
    }
    try {
        const response = await apiFetch("/api/settings/sync", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ schedules }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "planification refusée."), "error");
            return;
        }
        showToast(data.message, "success");
        fetchSyncConfig();
    } catch (e) { console.error("Cron schedules save error:", e); }
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
        const response = await apiFetch("/api/snapshots/compare", {
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
        const response = await apiFetch("/api/watchlist");
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
// Tri serveur de la table « Listés — Base de Données » (paginée côté API)
let wlSortBy = null;
let wlSortDir = "asc";

function sortWatchlistBy(column) {
    if (wlSortBy === column) {
        wlSortDir = wlSortDir === "asc" ? "desc" : "asc";
    } else {
        wlSortBy = column;
        wlSortDir = "asc";
    }
    // Indicateurs visuels sur les en-têtes de la table serveur
    document.querySelectorAll("#watchlist-table thead th[data-sort-col]").forEach((th) => {
        th.classList.remove("sort-asc", "sort-desc");
        if (th.getAttribute("data-sort-col") === wlSortBy) {
            th.classList.add(wlSortDir === "asc" ? "sort-asc" : "sort-desc");
        }
    });
    fetchWatchlist(1);
}

async function fetchWatchlist(page = 1) {
    wlCurrentPage = page;
    const searchEl = document.getElementById("wl-search-input");
    const listFilterEl = document.getElementById("wl-list-filter");
    const scopeFilterEl = document.getElementById("wl-scope-filter");

    const params = new URLSearchParams({ page: String(page), page_size: String(wlItemsPerPage) });
    const search = searchEl ? searchEl.value.trim() : "";
    if (search) params.set("search", search);
    const fieldFilterEl = document.getElementById("wl-field-filter");
    if (search && fieldFilterEl && fieldFilterEl.value && fieldFilterEl.value !== "default") {
        params.set("search_field", fieldFilterEl.value);
    }
    if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
    params.set("scope", scopeFilterEl && scopeFilterEl.value ? scopeFilterEl.value : "production");
    if (wlSortBy) {
        params.set("sort_by", wlSortBy);
        params.set("sort_dir", wlSortDir);
    }

    tableLoading(document.querySelector("#watchlist-table tbody"), 7);
    try {
        const response = await apiFetch(`/api/watchlist/db?${params.toString()}`);
        const data = await response.json();
        if (!response.ok) {
            showToast(`Erreur de lecture de la base : ${data.detail || JSON.stringify(data)}`, "error");
            return;
        }
        // Bandeau fuzzy : la recherche exacte n'a rien donné, résultats approchés
        const hint = document.getElementById("wl-match-hint");
        if (hint) {
            if (data.match_mode === "fuzzy" && (data.total || 0) > 0) {
                hint.innerHTML = `≈ Aucun résultat exact pour « <strong>${escapeHtml(search)}</strong> » — ` +
                    `<strong>${data.total}</strong> résultat(s) approché(s) (tolérance aux fautes de frappe), classés par similarité.`;
                hint.classList.remove("hidden");
            } else if (data.match_mode === "fuzzy") {
                hint.innerHTML = `Aucun résultat, même en recherche approchée, pour « <strong>${escapeHtml(search)}</strong> ».`;
                hint.classList.remove("hidden");
            } else {
                hint.classList.add("hidden");
            }
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
        tableEmpty(tbody, 7, "Aucune entité en base pour ce périmètre");
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
                ${item._fuzzy_score ? ` <span class="badge-secondary" title="Score de similarité avec la recherche (résultat approché)">≈ ${item._fuzzy_score} %</span>` : ''}
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
        const response = await apiFetch("/api/screen", {
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
        if (data.alert_id) { fetchAlerts("SCREENING"); refreshSidebarCounters(); }
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
            const response = await apiFetch("/api/screen", {
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
        if (alertsCount) { fetchAlerts("SCREENING"); refreshSidebarCounters(); }
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

        const response = await apiFetch(`/api/history?${params}`);
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
        tableEmpty(tbody, 7, "Aucune décision pour ce filtre");
        return;
    }

    logs.forEach(log => {
        const dateStr = new Date(log.timestamp + "Z").toLocaleString(uiLocale());
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
    const dateStr = new Date(log.timestamp + "Z").toLocaleString(uiLocale());
    
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
        const response = await apiFetch("/api/snapshots/purge", {
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
        ? `@${item.modified_by} — ${item.modified_at ? new Date(item.modified_at + (item.modified_at.endsWith("Z") ? "" : "Z")).toLocaleString(uiLocale()) : ""}`
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
        ${extendedFieldsRows(item)}
        <div id="entity-relations-section"></div>
        <div id="entity-changes-section"></div>
    `;

    modal.classList.remove("hidden");
    if (item.id) loadEntityChanges(item.id);
    if (item.entity_id) loadEntityRelations(item.entity_id);
}

// ------------------ RELATIONS & LIENS CAPITALISTIQUES (règle des 50 %) ------------------

async function loadEntityRelations(entityId) {
    const container = document.getElementById("entity-relations-section");
    if (!container) return;
    container.innerHTML = '<p class="section-desc" style="margin: 1rem 0 0;">Chargement des relations…</p>';
    try {
        const response = await apiFetch(`/api/relationships/${encodeURIComponent(entityId)}`, { silent: true });
        if (!response.ok) { container.innerHTML = ""; return; }
        const data = await response.json();
        const canEdit = currentUser && (userRoles(currentUser).includes("admin") || userRoles(currentUser).includes("reviewer"));

        const inherited = (data.inherited_risk || []).map(chain => `
            <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 8px; padding: 0.6rem 0.9rem; margin-bottom: 0.5rem; font-size: 0.85rem;">
                ⚠️ <strong>Règle des 50 %</strong> — détention majoritaire par
                <strong>${escapeHtml(chain.owner_name || chain.owner_entity_id)}</strong>
                ${chain.ownership_pct !== null && chain.ownership_pct !== undefined ? `(${chain.ownership_pct} %)` : "(contrôle présumé, source OFAC)"}
                ${chain.via && chain.via.length ? `<small style="color: var(--text-muted);"> via ${chain.via.map(escapeHtml).join(" → ")}</small>` : ""}
            </div>`).join("");

        const rows = (data.relations || []).map(rel => {
            const isOutgoing = rel.from_entity_id === entityId;
            const otherName = isOutgoing ? (rel.to_name || rel.to_entity_id) : (rel.from_name || rel.from_entity_id);
            const direction = isOutgoing ? "→" : "←";
            return `<tr>
                <td>${direction} ${escapeHtml(rel.relation_type_label)}${rel.relation_label ? ` <small style="color: var(--text-muted);">(${escapeHtml(rel.relation_label)})</small>` : ""}</td>
                <td><strong>${escapeHtml(otherName)}</strong><br><small style="color: var(--text-muted);">${escapeHtml(isOutgoing ? rel.to_entity_id : rel.from_entity_id)}</small></td>
                <td>${rel.ownership_pct !== null && rel.ownership_pct !== undefined ? rel.ownership_pct + " %" : "—"}</td>
                <td><span class="badge-secondary">${escapeHtml(rel.source)}</span>${rel.comment ? `<br><small style="color: var(--text-muted);">${escapeHtml(rel.comment)}</small>` : ""}</td>
                <td>${canEdit && rel.source === "MANUAL" ? `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text); padding: 0.1rem 0.5rem;" onclick="deleteEntityRelation(${rel.id}, '${escapeHtml(entityId)}')">✕</button>` : ""}</td>
            </tr>`;
        }).join("");

        const addForm = canEdit ? `
            <div class="filter-bar" style="margin-top: 0.5rem;">
                <input type="text" id="rel-other-id" placeholder="ID de l'entité liée (ex. 9101, UN-QDi.430)" style="flex: 1 1 200px;">
                <select id="rel-type" style="min-width: 190px;">
                    ${(data.relation_types || []).map(t => `<option value="${t.code}">${escapeHtml(t.label)}</option>`).join("")}
                </select>
                <input type="text" id="rel-pct" placeholder="% détention" style="flex: 0 1 110px; min-width: 100px;" title="Pourcentage de détention (règle des 50 %)">
                <button class="btn btn-sm btn-secondary" onclick="addEntityRelation('${escapeHtml(entityId)}')">➕ Lier</button>
            </div>` : "";

        const graphBtn = (data.relations || []).length
            ? `<button class="btn btn-sm btn-secondary" style="float: right; margin-top: -0.35rem;" onclick="openRelationGraph('${escapeHtml(entityId)}')">🕸 Graphe</button>`
            : "";
        container.innerHTML = `
            <div class="modal-section" style="margin-top: 1.25rem;">
                <h4>🔗 Relations & liens capitalistiques ${graphBtn}</h4>
                ${inherited}
                ${rows ? `<div class="table-container" style="max-height: 220px;">
                    <table><thead><tr><th>Relation</th><th>Entité liée</th><th>Détention</th><th>Source</th><th></th></tr></thead>
                    <tbody>${rows}</tbody></table></div>`
                  : '<p style="font-size: 0.85rem; color: var(--text-muted);">Aucune relation connue pour cette entité.</p>'}
                ${addForm}
            </div>`;
    } catch (e) {
        container.innerHTML = "";
    }
}

async function addEntityRelation(entityId) {
    const otherId = (document.getElementById("rel-other-id")?.value || "").trim();
    const relType = document.getElementById("rel-type")?.value || "OWNED_BY";
    const pctRaw = (document.getElementById("rel-pct")?.value || "").trim().replace(",", ".");
    if (!otherId) { showToast("Renseignez l'identifiant de l'entité liée.", "error"); return; }
    const payload = { from_entity_id: entityId, to_entity_id: otherId, relation_type: relType };
    if (pctRaw) {
        const pct = parseFloat(pctRaw);
        if (isNaN(pct)) { showToast("Pourcentage de détention invalide.", "error"); return; }
        payload.ownership_pct = pct;
    }
    try {
        const response = await apiFetch("/api/relationships", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "création refusée."), "error"); return; }
        showToast(data.message, "success");
        loadEntityRelations(entityId);
    } catch (e) { console.error("Relation create error:", e); }
}

async function deleteEntityRelation(relId, entityId) {
    if (!await confirmDialog("Supprimer cette relation manuelle ?", { danger: true })) return;
    try {
        const response = await apiFetch(`/api/relationships/${relId}`, { method: "DELETE" });
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "suppression refusée."), "error"); return; }
        showToast(data.message, "success");
        loadEntityRelations(entityId);
    } catch (e) { console.error("Relation delete error:", e); }
}

// Champs étendus : [champ, libellé] — affichés dans la modale seulement si non vides,
// éditables dans le formulaire, journalisés comme les autres
const WL_EXTENDED_SCALAR_FIELDS = [
    ["bic_swift", "BIC / SWIFT"],
    ["tax_id", "Numéro fiscal (Tax ID / INN)"],
    ["duns_number", "D-U-N-S"],
    ["title", "Titre"],
    ["name_original_script", "Nom (écriture d'origine)"],
    ["listed_on", "Inscrit le"],
    ["delisted_on", "Radié le"],
    ["pep_role", "Fonction PEP"],
    ["designating_state", "État désignant"],
    ["secondary_sanctions_risk", "Risque de sanctions secondaires"],
    ["vessel_flag", "Pavillon (navire)"],
    ["vessel_type", "Type de navire"],
    ["vessel_call_sign", "Indicatif radio (navire)"],
    ["vessel_mmsi", "MMSI (navire)"],
    ["vessel_tonnage", "Tonnage"],
    ["vessel_owner", "Propriétaire du navire"],
    ["aircraft_model", "Modèle d'aéronef"],
    ["aircraft_operator", "Opérateur d'aéronef"],
    ["aircraft_construction_number", "N° de construction (aéronef)"],
    ["organization_established_date", "Date de création (PM)"],
    ["organization_type", "Type d'organisation"],
];
const WL_EXTENDED_LIST_FIELDS = [
    ["sanction_programs", "Programmes de sanctions"],
    ["phone_numbers", "Téléphones"],
    ["email_addresses", "Emails"],
    ["websites", "Sites web"],
];

function cryptoWalletsText(wallets) {
    if (!Array.isArray(wallets) || !wallets.length) return "";
    return wallets.map(w => (w.currency ? `${w.currency}: ` : "") + (w.address || "")).join("; ");
}

// Lignes de la modale pour les champs étendus non vides
function extendedFieldsRows(item) {
    const rows = [];
    for (const [field, label] of WL_EXTENDED_SCALAR_FIELDS) {
        if (item[field]) rows.push(`<div class="details-item"><strong>${label}</strong><span>${escapeHtml(String(item[field]))}</span></div>`);
    }
    for (const [field, label] of WL_EXTENDED_LIST_FIELDS) {
        const value = Array.isArray(item[field]) ? item[field].join("; ") : "";
        if (value) rows.push(`<div class="details-item" style="grid-column: span 2;"><strong>${label}</strong><span>${escapeHtml(value)}</span></div>`);
    }
    const crypto = cryptoWalletsText(item.crypto_wallets);
    if (crypto) rows.push(`<div class="details-item" style="grid-column: span 2;"><strong>Adresses crypto</strong><span>${escapeHtml(crypto)}</span></div>`);
    return rows.length
        ? `<h3 style="margin: 1rem 0 0.5rem;">Champs étendus</h3><div class="details-grid">${rows.join("")}</div>`
        : "";
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
        const response = await apiFetch(`/api/watchlist/entity/${entityPk}/changes`);
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
                                <td><small>${c.changed_at ? new Date(c.changed_at + "Z").toLocaleString(uiLocale()) : "-"}</small></td>
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
        <h4 style="margin: 1rem 0 0.5rem;">Champs étendus</h4>
        <div class="details-grid">
            ${WL_EXTENDED_SCALAR_FIELDS.map(([field, label]) => _editInput(field, label, item[field])).join("")}
            ${WL_EXTENDED_LIST_FIELDS.map(([field, label]) => _editInput(field, `${label} (point-virgules)`, (item[field] || []).join("; "), true)).join("")}
            ${_editInput("crypto_wallets", "Adresses crypto (DEVISE: adresse ; …)", cryptoWalletsText(item.crypto_wallets), true)}
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
        ...WL_EXTENDED_SCALAR_FIELDS.map(([field]) => field),
    ];
    for (const field of scalarFields) {
        const newValue = _editValue(field) || null;
        if (newValue !== (item[field] || null)) patch[field] = newValue;
    }

    // Champs étendus liste (point-virgules) + adresses crypto (DEVISE: adresse)
    for (const [field] of WL_EXTENDED_LIST_FIELDS) {
        const newList = _splitList(_editValue(field), ";");
        if (JSON.stringify(newList) !== JSON.stringify(item[field] || [])) patch[field] = newList;
    }
    const newWallets = _splitList(_editValue("crypto_wallets"), ";").map(entry => {
        const sep = entry.indexOf(":");
        return sep > 0
            ? { currency: entry.slice(0, sep).trim(), address: entry.slice(sep + 1).trim() }
            : { currency: "", address: entry.trim() };
    });
    if (JSON.stringify(newWallets) !== JSON.stringify(item.crypto_wallets || [])) patch.crypto_wallets = newWallets;

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
        const response = await apiFetch(`/api/watchlist/entity/${item.id}`, {
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
        const response = await apiFetch("/api/users");
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
        tableEmpty(tbody, 7, "Aucun utilisateur trouvé.", "👥");
        return;
    }

    tbody.innerHTML = users.map(u => {
        // Roles empilables : un badge par role ("reviewer,user" -> 2 badges)
        const badgeStyles = {
            admin: 'background: rgba(99, 102, 241, 0.2); border: 1px solid rgba(99, 102, 241, 0.4); color: #a5b4fc; font-weight: 700;',
            reviewer: 'background: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.3); color: var(--color-warning); font-weight: 600;',
            user: 'background: rgba(14, 165, 233, 0.15); border: 1px solid rgba(14, 165, 233, 0.3); color: #38bdf8; font-weight: 600;'
        };
        const badgeLabels = { admin: "ADMINISTRATEUR", reviewer: "RÉVISEUR", user: "ANALYSTE USER", auditor: "AUDITEUR (LECTURE SEULE)" };
        const roleBadge = userRoles(u).map(r =>
            `<span style="${badgeStyles[r] || badgeStyles.user} padding: 0.25rem 0.6rem; border-radius: 12px; font-size: 0.75rem; margin-right: 4px; display: inline-block;">${badgeLabels[r] || escapeHtml(r.toUpperCase())}</span>`
        ).join("") || `<span style="${badgeStyles.user} padding: 0.25rem 0.6rem; border-radius: 12px; font-size: 0.75rem;">ANALYSTE USER</span>`;

        const dateFormatted = u.created_at ? new Date(u.created_at).toLocaleDateString(uiLocale(), { hour: "2-digit", minute: "2-digit" }) : "N/A";
        const isSelf = currentUser && currentUser.id === u.id;

        return `
            <tr>
                <td style="font-weight: bold; color: var(--text-secondary);">#${u.id}</td>
                <td><strong style="color: var(--text-primary);">@${escapeHtml(u.username)}</strong> ${isSelf ? '<span style="font-size: 0.7rem; background: rgba(34, 197, 94, 0.2); color: var(--success-soft-text); padding: 2px 6px; border-radius: 4px; margin-left: 4px;">VOUS</span>' : ''}${u.absent_until ? `<span title="Absent jusqu'au ${formatDateTime(u.absent_until)} — délégué : @${escapeHtml(u.delegate_to || "?")}" style="font-size: 0.7rem; background: rgba(245, 158, 11, 0.2); color: var(--color-warning); padding: 2px 6px; border-radius: 4px; margin-left: 4px;">🌴 ABSENT → @${escapeHtml(u.delegate_to || "?")}</span>` : ''}</td>
                <td>${escapeHtml(u.full_name || "—")}</td>
                <td>${roleBadge}</td>
                <td>${u.totp_enabled
                    ? `<span title="Double authentification active" style="color: var(--success-soft-text); font-weight: 600; font-size: 0.8rem;">🛡 Active</span>
                       <button class="btn btn-sm" onclick="resetUserTotp(${u.id}, '${escapeHtml(u.username)}')" title="Réinitialiser la MFA (téléphone perdu)" style="background: var(--surface-3); margin-left: 6px;">↺</button>`
                    : `<span style="color: var(--text-muted); font-size: 0.8rem;">—</span>`}</td>
                <td style="font-size: 0.85rem; color: var(--text-muted);">${dateFormatted}</td>
                <td style="text-align: right;">
                    <button class="btn btn-sm" onclick="openEditUserModal(${u.id})" style="background: var(--surface-3); margin-right: 6px;">✏️ Éditer</button>
                    ${!isSelf ? `<button class="btn btn-sm" onclick="deleteUserAccount(${u.id}, '${escapeHtml(u.username)}')" style="background: rgba(239, 68, 68, 0.2); color: var(--danger-soft-text); border: 1px solid rgba(239, 68, 68, 0.3);">🗑️ Supprimer</button>` : ''}
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
            response = await apiFetch(`/api/users/${editId}`, {
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
            response = await apiFetch("/api/users", {
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
        const response = await apiFetch(`/api/users/${userId}`, {
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
        const profileResp = await apiFetch("/api/users/me/profile", {
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
            const passResp = await apiFetch("/api/users/me/password", {
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
        const response = await apiFetch("/api/settings/ingestion");
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
        // SLA par priorité + notifications métier
        const sla = ingestionSettings.alert_sla_hours || {};
        for (const [prio, id] of [["CRITICAL", "setting-sla-critical"], ["HIGH", "setting-sla-high"],
                                  ["MEDIUM", "setting-sla-medium"], ["LOW", "setting-sla-low"]]) {
            const el = document.getElementById(id);
            if (el) el.value = sla[prio] ?? 0;
        }
        const notif = ingestionSettings.notification_events || {};
        for (const [event, id] of [["alert_created", "setting-notify-alert-created"],
                                   ["alert_pending_validation", "setting-notify-pending-validation"],
                                   ["snapshot_pending_review", "setting-notify-pending-review"],
                                   ["sync_error", "setting-notify-sync-error"]]) {
            const el = document.getElementById(id);
            if (el) el.checked = !!notif[event];
        }
        // Digest KPI périodique
        const digest = ingestionSettings.digest || {};
        const digestEnabledEl = document.getElementById("setting-digest-enabled");
        const digestCronEl = document.getElementById("setting-digest-cron");
        if (digestEnabledEl) digestEnabledEl.checked = !!digest.enabled;
        if (digestCronEl) digestCronEl.value = digest.cron || "0 8 * * 1-5";
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
        backtest_max_gap_pct: parseFloat(document.getElementById("setting-backtest-gap").value) || 20,
        alert_sla_hours: {
            CRITICAL: parseInt(document.getElementById("setting-sla-critical")?.value, 10) || 0,
            HIGH: parseInt(document.getElementById("setting-sla-high")?.value, 10) || 0,
            MEDIUM: parseInt(document.getElementById("setting-sla-medium")?.value, 10) || 0,
            LOW: parseInt(document.getElementById("setting-sla-low")?.value, 10) || 0,
        },
        notification_events: {
            alert_created: !!document.getElementById("setting-notify-alert-created")?.checked,
            alert_pending_validation: !!document.getElementById("setting-notify-pending-validation")?.checked,
            snapshot_pending_review: !!document.getElementById("setting-notify-pending-review")?.checked,
            sync_error: !!document.getElementById("setting-notify-sync-error")?.checked,
        },
        digest: {
            enabled: !!document.getElementById("setting-digest-enabled")?.checked,
            cron: (document.getElementById("setting-digest-cron")?.value || "").trim(),
        },
    };
    try {
        const response = await apiFetch("/api/settings/ingestion", {
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
        const response = await apiFetch("/api/review/pending");
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
        tableEmpty(tbody, 6, "Aucun snapshot en attente d'homologation.", "✅");
        return;
    }
    tbody.innerHTML = pending.map(snap => {
        const dateStr = snap.uploaded_at ? new Date(snap.uploaded_at).toLocaleString(uiLocale()) : "-";
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
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(snapshotId)}`);
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Impossible de charger le snapshot."), "error");
            return;
        }
        document.getElementById("review-detail-card").classList.remove("hidden");
        document.getElementById("review-detail-title").textContent = `Examen du Snapshot — ${data.file_name}`;
        const uploadedStr = data.uploaded_at ? new Date(data.uploaded_at).toLocaleString(uiLocale()) : "-";
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
        fetchBacktestCandidateRules();
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
        const response = await apiFetch("/api/testpanels");
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

// Règles anti-FP candidates proposées au cahier de tests (canal criblage) :
// brouillons en tête, puis en validation, puis actives. Silencieux si le
// compte n'a pas le rôle `rules` (403) — le select reste sur « Aucune ».
async function fetchBacktestCandidateRules() {
    const select = document.getElementById("backtest-candidate-rule");
    if (!select) return;
    try {
        const response = await apiFetch("/api/fprules?channel=SCREENING", { silent: true });
        if (!response.ok) return;
        const data = await response.json();
        const weight = { DRAFT: 0, PENDING_VALIDATION: 1, ACTIVE: 2 };
        const rules = (data.items || [])
            .filter(r => r.status in weight)
            .sort((a, b) => (weight[a.status] - weight[b.status]) || (a.id - b.id));
        const labels = { DRAFT: "brouillon", PENDING_VALIDATION: "en validation", ACTIVE: "active" };
        select.innerHTML = '<option value="">Aucune — règles actives uniquement</option>' +
            rules.map(r => `<option value="${r.id}">#${r.id} ${escapeHtml(r.name)} (${labels[r.status]}, v${r.version})</option>`).join("");
    } catch (e) { /* rôle rules absent ou réseau : select inchangé */ }
}

async function generateTestPanel() {
    const btn = document.getElementById("generate-panel-btn");
    const size = parseInt(document.getElementById("backtest-panel-size").value, 10) || 500;
    btn.disabled = true;
    btn.textContent = "Génération...";
    try {
        const response = await apiFetch("/api/testpanels/generate", {
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
        const candidateRuleSel = document.getElementById("backtest-candidate-rule");
        const payload = { panel_snapshot_id: panelId };
        if (candidateRuleSel && candidateRuleSel.value) {
            payload.candidate_rule_id = parseInt(candidateRuleSel.value, 10);
        }
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/backtest`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
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
        <div class="metric" style="flex: 1; background: var(--surface-hover); padding: 1rem; border-radius: 8px; border: 1px solid var(--border-color);">
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
    const executedStr = report.executed_at ? new Date(report.executed_at).toLocaleString(uiLocale()) : "";

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
        ${backtestRulesBlockHtml(report)}
    `;

    if (reminder) {
        reminder.innerHTML = report.verdict === "OK"
            ? `<p class="section-desc" style="color: var(--color-safe);">✅ Cahier de tests exécuté le ${escapeHtml(executedStr)} — écart ${report.gap_pct} % dans le seuil toléré (${report.threshold_pct} %).</p>`
            : `<p class="section-desc" style="color: var(--color-warning);">⚠️ Le dernier cahier de tests signale un écart de ${report.gap_pct} % (seuil : ${report.threshold_pct} %). Posez des Good Guys ou des exclusions puis relancez-le avant d'approuver.</p>`;
    }
}

// Bloc « Règles anti-FP » du rapport de backtest. Les anciens rapports
// (antérieurs à la clé additive `rules`) restent valides : bloc omis.
function backtestRulesBlockHtml(report) {
    const rules = report.rules;
    if (!rules) return "";
    const cand = rules.candidate_rule;
    const delta = rules.suppressed_delta || 0;
    const deltaTxt = delta > 0 ? `−${delta} alerte(s) en moins grâce à la règle candidate`
        : (delta < 0 ? `+${-delta} alerte(s) de plus (la règle candidate en supprime moins)` : "aucun écart imputable aux règles");
    const suppressedPairs = rules.candidate_suppressed_pairs || [];
    const pairRows = suppressedPairs.slice(0, 50).map(p => `
        <tr>
            <td><code>${escapeHtml(p.client_id || "")}</code><br><small>${escapeHtml(p.client_name || "")}</small></td>
            <td><small><strong>${escapeHtml(p.entity_name || "")}</strong></small></td>
            <td>${listTypeBadge(p.list_type)}</td>
            <td>${p.score}</td>
            <td><small>${escapeHtml(p.rule_name || "")}</small></td>
        </tr>`).join("");
    return `
        <div style="margin-top: 1rem; border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem;">
            <h4 style="margin: 0 0 0.4rem;">🧩 Règles anti-faux positifs dans ce cahier de tests</h4>
            <p class="section-desc" style="margin: 0 0 0.5rem;">
                ${rules.active_count} règle(s) active(s) appliquée(s) des deux côtés (comme en production).
                ${cand ? `Règle candidate évaluée côté candidat : <strong>#${cand.id} ${escapeHtml(cand.name)}</strong> (${escapeHtml(cand.status)}, v${cand.version}).` : "Aucune règle candidate évaluée sur ce run."}
            </p>
            <div style="display: flex; gap: 1.5rem; flex-wrap: wrap; font-size: 0.9rem;">
                <span>Supprimées par règles — production : <strong>${rules.current_suppressed}</strong></span>
                <span>candidat : <strong>${rules.candidate_suppressed}</strong></span>
                <span>${cand ? `Effet : <strong>${deltaTxt}</strong>` : ""}</span>
                <span>Écart avant règles : <strong>${rules.gap_pct_before_rules} %</strong> (vs ${report.gap_pct} % après)</span>
            </div>
            ${suppressedPairs.length ? `
            <details style="margin-top: 0.5rem;">
                <summary style="cursor: pointer; font-weight: 600;">Échantillon des alertes supprimées par règle côté candidat (${suppressedPairs.length})</summary>
                <div class="table-container" style="max-height: 220px; overflow-y: auto;">
                    <table>
                        <thead><tr><th>Pseudo-client</th><th>Listé</th><th>Liste</th><th>Score</th><th>Règle</th></tr></thead>
                        <tbody>${pairRows}</tbody>
                    </table>
                </div>
            </details>` : ""}
        </div>`;
}

async function bulkGoodGuys() {
    const checked = Array.from(document.querySelectorAll(".goodguy-cb:checked"));
    if (!checked.length) {
        showToast("Sélectionnez au moins une paire à mettre en liste blanche.", "warning");
        return;
    }
    const justification = await promptDialog(
        `Justification commune pour ${checked.length} paire(s) « Good Guy »`,
        { placeholder: "Ex. : homonymes avérés lors du cahier de tests d'homologation du " + new Date().toLocaleDateString(uiLocale()), textarea: true }
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
        const response = await apiFetch("/api/whitelist/bulk", {
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
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/entities?${params}`);
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
            <button class="btn btn-sm" ${data.page <= 1 ? "disabled" : ""} onclick="loadReviewEntitiesPage(${data.page - 1})" style="background: var(--surface-3);">← Précédent</button>
            <span style="margin: 0 1rem; color: var(--text-muted); font-size: 0.85rem;">Page ${data.page} / ${totalPages} — ${data.total} entité(s)</span>
            <button class="btn btn-sm" ${data.page >= totalPages ? "disabled" : ""} onclick="loadReviewEntitiesPage(${data.page + 1})" style="background: var(--surface-3);">Suivant →</button>
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
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/exclusions`, {
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
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/exclusions/remove`, {
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
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/approve`, {
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
        const response = await apiFetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/reject`, {
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

// Filtre de statut courant par canal (deux files distinctes)
const DEFAULT_ALERT_FILTER = "OPEN,IN_PROGRESS,ESCALATED,PENDING_VALIDATION";
let alertsFilterByChannel = { SCREENING: DEFAULT_ALERT_FILTER, FILTERING: DEFAULT_ALERT_FILTER };
let currentAlertId = null;

const ALERT_CHANNEL_CONF = {
    SCREENING: { table: "screening-alerts-table", listFilter: "screening-list-filter",
                 priorityFilter: "screening-priority-filter", section: "alerts-screening" },
    FILTERING: { table: "filtering-alerts-table", listFilter: "filtering-list-filter",
                 priorityFilter: "filtering-priority-filter", section: "alerts-filtering" },
};

// Badge de priorité (case management) + indicateur de retard SLA
const ALERT_PRIORITY_CONF = {
    CRITICAL: ["var(--color-alert)", "CRITIQUE"],
    HIGH: ["var(--color-warning)", "HAUTE"],
    MEDIUM: ["var(--color-primary)", "MOYENNE"],
    LOW: ["var(--text-muted)", "BASSE"],
};

function alertPriorityBadge(a) {
    const [color, label] = ALERT_PRIORITY_CONF[a.priority] || ["var(--text-muted)", a.priority || "—"];
    const overdue = a.overdue
        ? `<br><span title="Échéance SLA dépassée (${formatDateTime(a.due_at)})" style="color: var(--color-alert); font-size: 0.7rem; font-weight: 700;">⏰ EN RETARD</span>`
        : "";
    return `<span style="color: ${color}; font-weight: 700; font-size: 0.78rem;">${label}</span>${overdue}`;
}

// Export CSV de la file d'alertes avec les filtres actifs de l'écran
function exportAlertsCsv(channel) {
    const conf = ALERT_CHANNEL_CONF[channel];
    const params = new URLSearchParams({ channel });
    const filter = alertsFilterByChannel[channel];
    if (filter) params.set("status", filter);
    const listFilterEl = document.getElementById(conf.listFilter);
    if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
    const prioEl = document.getElementById(conf.priorityFilter);
    if (prioEl && prioEl.value) params.set("priority", prioEl.value);
    window.open(`/api/export/alerts.csv?${params.toString()}`, "_blank");
}

// Décompose le client_id d'une alerte de filtrage (TXN:msgid:idx) en message + n° de partie
function describeFilteringSubject(a) {
    const parts = (a.client_id || "").split(":");
    if (parts[0] === "TXN" && parts.length >= 3) {
        return `<strong>${escapeHtml(a.client_name)}</strong><br><small style="color:var(--text-muted)">Message ${escapeHtml(parts.slice(1, -1).join(":"))} · partie #${escapeHtml(parts[parts.length - 1])}</small>`;
    }
    return `<strong>${escapeHtml(a.client_name)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(a.client_id || "")}</small>`;
}

// Pagination des files d'alertes (etat par canal)
const ALERTS_PAGE_SIZE = 100;
let alertsPageByChannel = { SCREENING: 1, FILTERING: 1 };

function renderQueuePagination(containerId, page, total, pageSize, onPageFn) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (total <= pageSize) { container.classList.add("hidden"); return; }
    container.classList.remove("hidden");
    container.innerHTML = `
        <span class="pagination-info">${total} élément(s) — page ${page} / ${totalPages}</span>
        <div class="pagination-buttons">
            <button class="pagination-btn" ${page <= 1 ? "disabled" : ""} onclick="${onPageFn}(${page - 1})">← Précédent</button>
            <button class="pagination-btn" ${page >= totalPages ? "disabled" : ""} onclick="${onPageFn}(${page + 1})">Suivant →</button>
        </div>`;
}

function goToAlertsPageScreening(page) { fetchAlerts("SCREENING", page); }
function goToAlertsPageFiltering(page) { fetchAlerts("FILTERING", page); }

async function fetchAlerts(channel = "SCREENING", page = null) {
    const conf = ALERT_CHANNEL_CONF[channel];
    if (!conf) return;
    if (page !== null) alertsPageByChannel[channel] = Math.max(1, page);
    const currentPage = alertsPageByChannel[channel] || 1;
    try {
        const params = new URLSearchParams({ page: String(currentPage), page_size: String(ALERTS_PAGE_SIZE), channel });
        const filter = alertsFilterByChannel[channel];
        if (filter) params.set("status", filter);
        const listFilterEl = document.getElementById(conf.listFilter);
        if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
        const prioFilterEl = document.getElementById(conf.priorityFilter);
        if (prioFilterEl && prioFilterEl.value) params.set("priority", prioFilterEl.value);
        tableLoading(document.querySelector(`#${conf.table} tbody`), 10);
        const response = await apiFetch(`/api/alerts?${params}`);
        if (!response.ok) return;
        const data = await response.json();
        renderAlertsTable(channel, data.items || []);
        renderQueuePagination(
            channel === "FILTERING" ? "filtering-alerts-pagination" : "screening-alerts-pagination",
            data.page, data.total, data.page_size,
            channel === "FILTERING" ? "goToAlertsPageFiltering" : "goToAlertsPageScreening",
        );
    } catch (e) {
        console.error("Error fetching alerts:", e);
    }
}

function setAlertFilter(channel, filter) {
    alertsFilterByChannel[channel] = filter;
    const section = document.getElementById(`sub-sec-${ALERT_CHANNEL_CONF[channel].section}`);
    if (section) {
        section.querySelectorAll(".alerts-status-filters button").forEach(btn => {
            const active = btn.dataset.filter === filter;
            btn.classList.toggle("btn-secondary", active);
            btn.style.background = active ? "" : "var(--surface-3)";
        });
    }
    fetchAlerts(channel, 1);
}

function alertStatusBadge(status) {
    const styles = {
        OPEN: ["var(--color-warning)", "OUVERTE"],
        IN_PROGRESS: ["#38bdf8", "EN COURS"],
        ESCALATED: ["var(--color-alert)", "ESCALADÉE"],
        PENDING_VALIDATION: ["#c084fc", "À VALIDER (4-YEUX)"],
        CLOSED_CONFIRMED: ["var(--color-alert)", "VRAI POSITIF"],
        CLOSED_FALSE_POSITIVE: ["var(--success-soft-text)", "FAUX POSITIF"],
        CLOSED_BY_RULE: ["#94a3b8", "CLÔTURÉE PAR RÈGLE"],
    };
    const [color, label] = styles[status] || ["#9ca3af", status];
    return `<span style="color: ${color}; font-weight: 600; font-size: 0.8rem;">${label}</span>`;
}

function renderAlertsTable(channel, items) {
    const tbody = document.querySelector(`#${ALERT_CHANNEL_CONF[channel].table} tbody`);
    if (!tbody) return;
    // Nouvelle page = nouvelle sélection (les cases ne survivent pas au rendu)
    clearAlertSelection(channel, false);
    if (!items.length) {
        tableEmpty(tbody, 10, "Aucune alerte pour ce filtre.", "✅");
        return;
    }
    tbody.innerHTML = items.map(a => {
        const subject = channel === "FILTERING"
            ? describeFilteringSubject(a)
            : `<strong>${escapeHtml(a.client_name)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(a.client_id || "")}</small>`;
        const selectable = !a.status.startsWith("CLOSED");
        return `
        <tr>
            <td>${selectable ? `<input type="checkbox" class="alert-select" data-alert-id="${a.id}" onchange="toggleAlertSelection('${channel}', ${a.id}, this.checked)" aria-label="Sélectionner l'alerte ${a.id}">` : ""}</td>
            <td>${alertPriorityBadge(a)}</td>
            <td>${formatDateTime(a.created_at)}</td>
            <td>${subject}</td>
            <td>${escapeHtml(a.watchlist_name)}<br><small style="color:var(--text-muted)">${escapeHtml(a.watchlist_entity_id)}</small></td>
            <td>${listTypeBadge(a.list_type)}</td>
            <td><strong style="color: ${a.final_score >= 90 ? 'var(--color-alert)' : 'var(--color-warning)'};">${a.final_score.toFixed(1)}%</strong></td>
            <td>${alertStatusBadge(a.status)}</td>
            <td>${escapeHtml(a.assigned_to || "—")}</td>
            <td><button class="btn btn-sm btn-secondary" onclick="openAlertModal(${a.id})">🔎 Instruire</button></td>
        </tr>`;
    }).join("");
}

async function openAlertModal(alertId) {
    currentAlertId = alertId;
    try {
        const response = await apiFetch(`/api/alerts/${alertId}`);
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
                <small style="color: var(--text-muted);">${e.timestamp ? new Date(e.timestamp).toLocaleString(uiLocale()) : ""} — <strong>@${escapeHtml(e.username)}</strong> · ${escapeHtml(e.action)}</small>
                ${e.detail ? `<div style="font-size: 0.85rem;">${escapeHtml(e.detail)}</div>` : ""}
            </div>
        `).join("");

        let actionsHtml = "";
        if (!isClosed) {
            actionsHtml += `<div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem;">`;
            if (a.status !== "PENDING_VALIDATION") {
                actionsHtml += `<button class="btn btn-sm btn-secondary" onclick="alertAction('assign')">📌 M'assigner</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: var(--surface-3);" onclick="alertActionWithComment('comment', 'Commentaire')">💬 Commenter</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text);" onclick="alertActionWithComment('escalate', 'Motif de l\\'escalade')">⚠️ Escalader</button>`;
                actionsHtml += `<button class="btn btn-sm btn-primary" onclick="proposeAlertDecision('FALSE_POSITIVE')">✅ Proposer : Faux positif</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(239,68,68,0.85);" onclick="proposeAlertDecision('CONFIRMED')">🚨 Proposer : Vrai positif</button>`;
            } else if (isReviewer && a.proposed_by !== me) {
                actionsHtml += `<span style="align-self: center; font-size: 0.85rem; color: var(--text-muted);">Proposé par @${escapeHtml(a.proposed_by)} : <strong>${a.proposed_decision === "CONFIRMED" ? "vrai positif" : "faux positif"}</strong></span>`;
                actionsHtml += `<button class="btn btn-sm btn-primary" onclick="validateAlertDecision(true)">✔️ Valider (4-yeux)</button>`;
                actionsHtml += `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text);" onclick="validateAlertDecision(false)">↩️ Refuser & renvoyer</button>`;
            } else {
                actionsHtml += `<span style="align-self: center; font-size: 0.85rem; color: var(--text-muted);">Décision proposée par @${escapeHtml(a.proposed_by)} — en attente d'un validateur différent (rôle réviseur).</span>`;
            }
            actionsHtml += `</div>`;
        } else {
            actionsHtml = `<p class="section-desc" style="margin-top: 1rem;">Clôturée par <strong>@${escapeHtml(a.decided_by)}</strong> le ${a.decided_at ? new Date(a.decided_at).toLocaleString(uiLocale()) : ""} — ${escapeHtml(a.decision_comment || "")}</p>`;
            // Faux positif avere : proposer la mise en liste blanche (reviseurs)
            if (a.status === "CLOSED_FALSE_POSITIVE" && isReviewer) {
                actionsHtml += `<button class="btn btn-sm btn-secondary" onclick="openWhitelistModal('${escapeHtml(a.client_id || "")}', '${escapeHtml(a.watchlist_entity_id)}', '${escapeHtml(a.client_name)}', '${escapeHtml(a.watchlist_name)}')">🛡️ Mettre en liste blanche</button>`;
            }
        }

        // Pieces jointes + selection de priorite (case management)
        const attachmentsHtml = (a.attachments || []).map(att => `
            <li style="font-size: 0.82rem; margin-bottom: 0.25rem;">
                <a href="/api/alerts/attachments/${att.id}" target="_blank" style="color: var(--color-accent);">📎 ${escapeHtml(att.file_name)}</a>
                <small style="color: var(--text-muted);"> — @${escapeHtml(att.uploaded_by)}, ${formatDateTime(att.uploaded_at)}${att.comment ? " · " + escapeHtml(att.comment) : ""}</small>
            </li>`).join("");
        const prioritySelector = !isClosed ? `
            <select id="alert-priority-select" onchange="changeAlertPriority(this.value)" title="Modifier la priorité (l'échéance SLA est recalculée)" style="width: auto; padding: 0.3rem 0.5rem; font-size: 0.8rem;">
                ${["CRITICAL", "HIGH", "MEDIUM", "LOW"].map(p =>
                    `<option value="${p}" ${a.priority === p ? "selected" : ""}>${ALERT_PRIORITY_CONF[p][1]}</option>`).join("")}
            </select>` : alertPriorityBadge(a);

        document.getElementById("alert-modal-body").innerHTML = `
            <p class="section-desc" style="display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;">
                Score final <strong>${a.final_score.toFixed(1)}%</strong> · assignée à <strong>${escapeHtml(a.assigned_to || "personne")}</strong>
                · journal d'audit #${a.audit_id} (${escapeHtml(a.watchlist_version || "")})
                · Priorité : ${prioritySelector}
                ${a.due_at ? `· Échéance SLA : <strong style="${a.overdue ? "color: var(--color-alert);" : ""}">${formatDateTime(a.due_at)}${a.overdue ? " ⏰" : ""}</strong>` : ""}
                <span style="margin-left: auto; display: inline-flex; gap: 0.4rem;">
                    <button class="btn btn-sm btn-primary" onclick="openCasefileModal(${a.id})" title="Dossier d'investigation complet (checklist, contexte, relations)">📁 Dossier</button>
                    ${a.client_id && !String(a.client_id).startsWith("TXN:") ? `<button class="btn btn-sm btn-secondary" onclick="openClient360('${escapeHtml(a.client_id)}')" title="Tout l'historique de ce client">👤 Client 360°</button>` : ""}
                    <a class="btn btn-sm btn-secondary" href="/api/alerts/${a.id}/report" target="_blank" title="Rapport imprimable (ACPR/FED)">🖨 Rapport</a>
                </span>
            </p>
            <h3 style="font-size: 0.95rem; margin: 0.75rem 0 0.5rem;">Explication du score (decision tree)</h3>
            <div class="table-container" style="max-height: 160px;">
                <table><thead><tr><th>Ajustement</th><th>Impact</th><th>Détail</th></tr></thead><tbody>${adjRows || '<tr><td colspan="3" style="color: var(--text-muted);">Hard match ou aucun ajustement.</td></tr>'}</tbody></table>
            </div>
            <h3 style="font-size: 0.95rem; margin: 1rem 0 0.5rem;">Historique</h3>
            <div style="max-height: 220px; overflow-y: auto;">${eventsHtml || '<small style="color: var(--text-muted);">Aucun événement.</small>'}</div>
            <h3 style="font-size: 0.95rem; margin: 1rem 0 0.5rem;">Pièces jointes</h3>
            <ul style="list-style: none; margin-bottom: 0.5rem;">${attachmentsHtml || '<li style="font-size: 0.82rem; color: var(--text-muted);">Aucune pièce jointe.</li>'}</ul>
            <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                <input type="file" id="alert-attachment-file" style="flex: 1 1 220px; padding: 0.35rem; font-size: 0.8rem;">
                <button class="btn btn-sm btn-secondary" onclick="uploadAlertAttachment()">📎 Joindre</button>
            </div>
            ${actionsHtml}
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--border-color);">
                <button class="btn btn-sm btn-secondary" onclick="generateAlertNarrative()">📝 Générer un narratif</button>
                <button class="btn btn-sm" style="background: var(--surface-3);" onclick="fetchAlertAdverseMedia('client')">📰 Presse : client</button>
                <button class="btn btn-sm" style="background: var(--surface-3);" onclick="fetchAlertAdverseMedia('watchlist')">📰 Presse : listé</button>
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
    const response = await apiFetch(`/api/alerts/${currentAlertId}/${path}`, {
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
    if (data) { openAlertModal(currentAlertId); refreshAlertQueues(); }
}

async function changeAlertPriority(priority) {
    const data = await _postAlertAction("priority", { priority });
    if (data) { showToast(data.message, "success"); openAlertModal(currentAlertId); refreshAlertQueues(); }
}

async function uploadAlertAttachment() {
    const input = document.getElementById("alert-attachment-file");
    if (!input || !input.files || !input.files.length) {
        showToast("Sélectionnez d'abord un fichier à joindre.", "error");
        return;
    }
    const comment = await promptDialog("Commentaire de la pièce jointe", {
        message: "Description de la pièce (optionnel).", textarea: true, required: false,
        placeholder: "Ex. justificatif KYC, échange client..."
    });
    if (comment === null) return;
    const formData = new FormData();
    formData.append("file", input.files[0]);
    if (comment) formData.append("comment", comment);
    try {
        const response = await apiFetch(`/api/alerts/${currentAlertId}/attachments`, { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "dépôt refusé."), "error");
            return;
        }
        showToast(data.message, "success");
        openAlertModal(currentAlertId);
    } catch (e) {
        console.error("Attachment upload error:", e);
    }
}

async function alertActionWithComment(action, promptLabel) {
    const comment = await promptDialog(promptLabel, { textarea: true, placeholder: "Votre commentaire..." });
    if (comment === null) return;
    const data = await _postAlertAction(action, { comment });
    if (data) { openAlertModal(currentAlertId); refreshAlertQueues(); }
}

async function proposeAlertDecision(decision) {
    const label = decision === "CONFIRMED" ? "vrai positif" : "faux positif";
    const comment = await promptDialog(`Proposer « ${label} »`, {
        message: "Commentaire obligatoire motivant la décision proposée (validation 4-yeux ensuite).",
        textarea: true, placeholder: "Motivation réglementaire de la décision..."
    });
    if (comment === null) return;
    const data = await _postAlertAction("propose", { decision, comment });
    if (data) { showToast(data.message, "success"); openAlertModal(currentAlertId); refreshAlertQueues(); }
}

async function validateAlertDecision(approve) {
    const comment = await promptDialog(approve ? "Valider la décision (4-yeux)" : "Refuser et renvoyer en analyse", {
        message: approve ? "Commentaire (optionnel)." : "Motif du refus (obligatoire) — l'alerte repartira en analyse.",
        textarea: true, required: !approve,
        placeholder: approve ? "Commentaire éventuel..." : "Motif du refus..."
    });
    if (comment === null) return;
    const data = await _postAlertAction("validate", { approve, comment });
    if (data) { showToast(data.message, "success"); openAlertModal(currentAlertId); refreshAlertQueues(); }
}

// ------------------ LISTE BLANCHE CLIENT x LISTÉ (GOOD GUYS) ------------------

let whitelistPage = 1;

async function fetchWhitelist(page = null) {
    if (page !== null) whitelistPage = Math.max(1, page);
    try {
        const params = new URLSearchParams({ page: String(whitelistPage), page_size: "100" });
        const listFilterEl = document.getElementById("whitelist-list-filter");
        if (listFilterEl && listFilterEl.value) params.set("list_type", listFilterEl.value);
        const response = await apiFetch(`/api/whitelist?${params}`);
        if (!response.ok) return;
        const data = await response.json();
        renderWhitelistTable(data.items || []);
        renderQueuePagination("whitelist-pagination", data.page, data.total, data.page_size, "fetchWhitelist");
    } catch (e) {
        console.error("Error fetching whitelist:", e);
    }
}

function renderWhitelistTable(items) {
    const tbody = document.querySelector("#whitelist-table tbody");
    if (!tbody) return;
    if (!items.length) {
        tableEmpty(tbody, 8, "Aucune paire en liste blanche.");
        return;
    }
    const stateBadge = (state) => {
        const map = { ACTIVE: ["var(--success-soft-text)", "ACTIVE"], EXPIRED: ["var(--color-warning)", "EXPIRÉE"], REVOKED: ["#9ca3af", "RÉVOQUÉE"] };
        const [color, label] = map[state] || ["#9ca3af", state];
        return `<span style="color: ${color}; font-weight: 600; font-size: 0.8rem;">${label}</span>`;
    };
    tbody.innerHTML = items.map(p => `
        <tr ${p.state !== "ACTIVE" ? 'style="opacity: 0.55;"' : ""}>
            <td><strong>${escapeHtml(p.client_name || p.client_id)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(p.client_id)}</small></td>
            <td>${escapeHtml(p.watchlist_name || p.watchlist_entity_id)}<br><small style="color:var(--text-muted)">${escapeHtml(p.watchlist_entity_id)}</small></td>
            <td>${listTypeBadge(p.list_type)}</td>
            <td style="max-width: 260px;"><small>${escapeHtml(p.justification || "—")}</small>${p.evidence_file_name ? `<br><a href="/api/whitelist/evidence/${p.id}" target="_blank" style="color: var(--color-accent); font-size: 0.75rem;">📎 ${escapeHtml(p.evidence_file_name)}</a>` : ""}</td>
            <td>@${escapeHtml(p.created_by)}<br><small style="color:var(--text-muted)">${p.created_at ? new Date(p.created_at).toLocaleDateString(uiLocale()) : ""}</small></td>
            <td>${p.expires_at ? new Date(p.expires_at).toLocaleDateString(uiLocale()) : "—"}</td>
            <td>${stateBadge(p.state)}</td>
            <td>${p.state === "ACTIVE" ? `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text);" onclick="revokeWhitelistPair(${p.id})">Révoquer</button>` : ""}</td>
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
        const response = await apiFetch(`/api/whitelist/${pairId}/revoke`, {
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
        const response = await apiFetch("/api/whitelist", { method: "POST", body: formData });
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
        const response = await apiFetch("/api/kpi");
        if (!response.ok) return;
        const k = await response.json();

        const tile = (label, value, color) => `
            <div class="metric" style="flex: 1; min-width: 170px; background: var(--surface-2); padding: 1rem; border-radius: 8px; border: 1px solid var(--border-color);">
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
                <td>${formatDateTime(s.executed_at)}</td>
                <td>${escapeHtml(SYNC_SOURCE_LABELS[s.source] || s.source)} <small style="color:var(--text-muted)">${escapeHtml(s.trigger)}</small></td>
                <td>${escapeHtml(statusLabel(s.status))}</td>
                <td><small>+${s.added} / ~${s.modified} / -${s.removed}</small></td>
              </tr>`).join("")
            : '<tr><td colspan="4" style="color: var(--text-muted); text-align: center;">Aucune synchronisation.</td></tr>';

        // Ventilation par analyste (volumes + délai moyen de décision)
        const analystsBody = document.querySelector("#kpi-analysts-table tbody");
        if (analystsBody) {
            const analysts = a.by_analyst || [];
            analystsBody.innerHTML = analysts.length
                ? analysts.map(r => `<tr>
                    <td>@${escapeHtml(r.analyst)}</td>
                    <td><strong>${r.decided}</strong></td>
                    <td>${r.avg_decision_hours !== null && r.avg_decision_hours !== undefined ? r.avg_decision_hours + " h" : "—"}</td>
                  </tr>`).join("")
                : '<tr><td colspan="3" style="color: var(--text-muted); text-align: center;">Aucune alerte décidée.</td></tr>';
        }

        // Efficacité des règles anti-faux positifs (hit_count)
        const rulesBody = document.querySelector("#kpi-fprules-table tbody");
        if (rulesBody) {
            const rules = k.fp_rules || [];
            rulesBody.innerHTML = rules.length
                ? rules.map(r => `<tr>
                    <td>${escapeHtml(r.name)} <small style="color:var(--text-muted)">v${r.version}${r.enabled ? "" : " (désactivée)"}</small></td>
                    <td>${escapeHtml(r.channel === "FILTERING" ? "Filtrage" : "Criblage")}</td>
                    <td><strong>${r.hit_count}</strong></td>
                  </tr>`).join("")
                : '<tr><td colspan="3" style="color: var(--text-muted); text-align: center;">Aucune règle active.</td></tr>';
        }
    } catch (e) {
        console.error("Error fetching KPIs:", e);
    }
}

// ------------------ ACCUEIL « VUE D'ENSEMBLE » (graphiques SVG natifs) ------------------

// Courbe 30 jours : alertes créées (par canal) et clôturées — SVG sans dépendance
function renderAlertsTimeseriesChart(containerId, ts) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const series = [
        { key: "created_screening", label: "Criblage (créées)", color: "var(--color-primary)" },
        { key: "created_filtering", label: "Filtrage (créées)", color: "var(--color-accent)" },
        { key: "closed", label: "Clôturées", color: "var(--color-safe)" },
    ];
    if (!ts || !ts.length) {
        container.innerHTML = '<p class="empty-state"><span class="empty-icon">📈</span>Aucune alerte sur les 30 derniers jours.</p>';
        return;
    }
    const W = 640, H = 220, P = { l: 36, r: 12, t: 12, b: 26 };
    const maxY = Math.max(1, ...ts.flatMap(d => series.map(s => d[s.key] || 0)));
    const x = (i) => P.l + i * (W - P.l - P.r) / Math.max(1, ts.length - 1);
    const y = (v) => H - P.b - (v / maxY) * (H - P.t - P.b);

    let grid = "";
    const gridSteps = 4;
    for (let g = 0; g <= gridSteps; g++) {
        const value = Math.round(maxY * g / gridSteps);
        const gy = y(value).toFixed(1);
        grid += `<line x1="${P.l}" y1="${gy}" x2="${W - P.r}" y2="${gy}" stroke="var(--border-color)" stroke-width="1"/>`
              + `<text x="${P.l - 6}" y="${gy}" font-size="9" text-anchor="end" dominant-baseline="middle">${value}</text>`;
    }
    let xLabels = "";
    const labelEvery = Math.max(1, Math.ceil(ts.length / 6));
    ts.forEach((d, i) => {
        if (i % labelEvery !== 0 && i !== ts.length - 1) return;
        xLabels += `<text x="${x(i).toFixed(1)}" y="${H - 8}" font-size="9" text-anchor="middle">${escapeHtml(d.date.slice(5))}</text>`;
    });
    const lines = series.map(s =>
        `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" points="${
            ts.map((d, i) => `${x(i).toFixed(1)},${y(d[s.key] || 0).toFixed(1)}`).join(" ")}"/>`
    ).join("");
    const legend = `<div class="chart-legend">${series.map(s =>
        `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${s.label}</span>`).join("")}</div>`;

    container.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Alertes sur 30 jours">${grid}${xLabels}${lines}</svg>${legend}`;
}

// Barres horizontales : fiches en production par liste
function renderListsBarChart(containerId, byType) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const entries = Object.entries(byType || {}).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
        container.innerHTML = '<p class="empty-state"><span class="empty-icon">📊</span>Aucune liste en production.</p>';
        return;
    }
    const W = 640, rowH = 30, P = { l: 70, r: 54 };
    const H = entries.length * rowH + 8;
    const maxV = Math.max(1, ...entries.map(([, v]) => v));
    const bars = entries.map(([type, value], i) => {
        const bw = Math.max(2, (value / maxV) * (W - P.l - P.r));
        const by = i * rowH + 6;
        return `<text x="${P.l - 8}" y="${by + 12}" font-size="10" text-anchor="end" dominant-baseline="middle">${escapeHtml(listTypeLabel(type))}</text>`
             + `<rect x="${P.l}" y="${by}" width="${bw.toFixed(1)}" height="18" rx="4" fill="var(--color-primary)" opacity="0.85"/>`
             + `<text x="${P.l + bw + 6}" y="${by + 12}" font-size="10" dominant-baseline="middle">${value.toLocaleString(uiLocale())}</text>`;
    }).join("");
    container.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Fiches par liste">${bars}</svg>`;
}

// Donut : répartition des alertes par statut
const _DONUT_STATUS_COLORS = {
    OPEN: "var(--color-warning)", IN_PROGRESS: "var(--color-primary)",
    ESCALATED: "var(--color-alert)", PENDING_VALIDATION: "var(--color-secondary)",
    CLOSED_CONFIRMED: "#b91c1c", CLOSED_FALSE_POSITIVE: "var(--color-safe)",
    CLOSED_BY_RULE: "var(--text-muted)",
};

function renderStatusDonut(containerId, legendId, byStatus) {
    const container = document.getElementById(containerId);
    const legendEl = document.getElementById(legendId);
    if (!container) return;
    const entries = Object.entries(byStatus || {}).filter(([, v]) => v > 0);
    const total = entries.reduce((sum, [, v]) => sum + v, 0);
    if (!total) {
        container.innerHTML = '<p class="empty-state"><span class="empty-icon">🗂</span>Aucune alerte enregistrée.</p>';
        if (legendEl) legendEl.innerHTML = "";
        return;
    }
    const R = 54, C = 2 * Math.PI * R;
    let offset = 0;
    const segments = entries.map(([status, value]) => {
        const frac = value / total;
        const color = _DONUT_STATUS_COLORS[status] || "var(--text-muted)";
        const seg = `<circle r="${R}" cx="80" cy="80" fill="none" stroke="${color}" stroke-width="22"
            stroke-dasharray="${(frac * C).toFixed(2)} ${(C - frac * C).toFixed(2)}"
            stroke-dashoffset="${(-offset * C).toFixed(2)}" transform="rotate(-90 80 80)"/>`;
        offset += frac;
        return seg;
    }).join("");
    container.innerHTML = `<svg class="chart-svg" viewBox="0 0 160 160" style="max-width: 200px;" role="img" aria-label="Répartition des alertes">
        ${segments}
        <text x="80" y="76" font-size="22" font-weight="700" text-anchor="middle" style="fill: var(--text-primary);">${total}</text>
        <text x="80" y="94" font-size="9" text-anchor="middle">alertes</text>
    </svg>`;
    if (legendEl) {
        legendEl.innerHTML = `<div class="chart-legend" style="justify-content: center;">${entries.map(([status, value]) =>
            `<span class="legend-item"><span class="legend-swatch" style="background:${_DONUT_STATUS_COLORS[status] || "var(--text-muted)"}"></span>${escapeHtml(statusLabel(status))} (${value})</span>`).join("")}</div>`;
    }
}

async function fetchHomeDashboard() {
    const tiles = document.getElementById("home-tiles");
    if (!tiles) return;
    try {
        const [kpiResp, countersResp] = await Promise.all([
            apiFetch("/api/kpi", { silent: true }),
            apiFetch("/api/counters", { silent: true }),
        ]);
        if (!kpiResp.ok) return;
        const k = await kpiResp.json();
        const c = countersResp.ok ? await countersResp.json() : {};
        const a = k.alerts || {};
        const byStatus = a.by_status || {};

        const tile = (icon, label, value, sub, onclick) => `
            <div class="home-tile" ${onclick ? `onclick="${onclick}"` : ""} role="button" tabindex="0">
                <div class="tile-label">${icon} ${label}</div>
                <div class="tile-value">${value}</div>
                ${sub ? `<div class="tile-sub">${sub}</div>` : ""}
            </div>`;
        const fpRate = (a.false_positive_rate_pct !== null && a.false_positive_rate_pct !== undefined)
            ? a.false_positive_rate_pct + " %" : "—";
        const avgH = (a.avg_decision_hours !== null && a.avg_decision_hours !== undefined)
            ? a.avg_decision_hours + " h" : "—";
        tiles.innerHTML =
            tile("🚨", "Criblage", c.open_alerts_screening ?? 0, "alertes ouvertes",
                 "switchTab('alerts'); switchSubTab('alerts', 'alerts-screening')") +
            tile("💸", "Filtrage", c.open_alerts_filtering ?? 0, "alertes ouvertes",
                 "switchTab('alerts'); switchSubTab('alerts', 'alerts-filtering')") +
            tile("👁", "4 yeux", byStatus.PENDING_VALIDATION || 0, "décisions à valider",
                 "switchTab('alerts'); switchSubTab('alerts', 'alerts-screening')") +
            tile("📥", "Homologation", c.pending_reviews ?? 0, "snapshots en attente",
                 "switchTab('watchlist-mgmt'); switchSubTab('watchlist-mgmt', 'watchlist-review')") +
            tile("📉", "Faux positifs", fpRate, "taux sur alertes closes", "switchTab('kpi')") +
            tile("⏱", "Délai moyen", avgH, "création → décision", "switchTab('kpi')");

        renderAlertsTimeseriesChart("home-chart-alerts", a.timeseries_30d || []);
        renderListsBarChart("home-chart-lists", (k.lists || {}).production_entities_by_type || {});
        renderStatusDonut("home-chart-status", "home-chart-status-legend", byStatus);

        // Liste « à traiter » : alertes ouvertes les plus anciennes
        const todo = document.getElementById("home-todo-list");
        if (todo) {
            const oldest = a.oldest_open || [];
            todo.innerHTML = oldest.length
                ? oldest.map(al => `
                    <li onclick="switchTab('alerts'); switchSubTab('alerts', '${al.channel === "FILTERING" ? "alerts-filtering" : "alerts-screening"}'); openAlertModal(${al.id})">
                        <span class="item-main">#${al.id} — ${escapeHtml(al.client_name || "?")} × ${escapeHtml(al.watchlist_name || "?")}</span>
                        <span class="item-meta">${escapeHtml(statusLabel(al.status))} · ${formatDate(al.created_at)}</span>
                    </li>`).join("")
                : '<li style="cursor: default;"><span class="item-main" style="color: var(--text-muted);">✅ Aucune alerte en attente.</span></li>';
        }

        // Dernière synchronisation
        const lastSyncEl = document.getElementById("home-last-sync");
        const lastSync = (k.recent_syncs || [])[0];
        if (lastSyncEl) {
            lastSyncEl.innerHTML = lastSync
                ? `<strong>${escapeHtml(SYNC_SOURCE_LABELS[lastSync.source] || lastSync.source)}</strong> — ${escapeHtml(statusLabel(lastSync.status))}
                   <br><small style="color: var(--text-muted);">${formatDateTime(lastSync.executed_at)} · +${lastSync.added} / ~${lastSync.modified} / -${lastSync.removed}</small>`
                : "Aucune synchronisation exécutée.";
        }
    } catch (e) {
        console.error("Erreur de chargement de la vue d'ensemble :", e);
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
        const response = await apiFetch("/api/transactions/screen", { method: "POST", body: formData });
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
        const response = await apiFetch(`/api/alerts/${currentAlertId}/narrative`, { method: "POST" });
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
        const response = await apiFetch(`/api/adverse-media?name=${encodeURIComponent(name)}`);
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

// ============================================================================
// PARAMÉTRAGE : BLOCKING KEYS PAR CANAL + RÈGLES ANTI-FAUX POSITIFS (mode DEV)
// ============================================================================

function refreshAlertQueues() {
    fetchAlerts("SCREENING");
    fetchAlerts("FILTERING");
    refreshSidebarCounters();
}

// ------------------ BLOCKING KEYS ------------------

let blockingComponents = [];
let blockingLabels = {};
let blockingDraft = { SCREENING: [], FILTERING: [] };

async function fetchBlockingSettings() {
    try {
        const response = await apiFetch("/api/settings/blocking");
        if (!response.ok) return;
        const data = await response.json();
        blockingComponents = data.components || [];
        blockingLabels = data.component_labels || {};
        blockingDraft.SCREENING = [...(data.screening.layout || [])];
        blockingDraft.FILTERING = [...(data.filtering.layout || [])];
        renderBlockingEditor("SCREENING", data.screening.source);
        renderBlockingEditor("FILTERING", data.filtering.source);
    } catch (e) {
        console.error("Error fetching blocking settings:", e);
    }
}

function renderBlockingEditor(channel, source) {
    const el = document.getElementById(channel === "SCREENING" ? "blocking-screening-editor" : "blocking-filtering-editor");
    if (!el) return;
    const layout = blockingDraft[channel];
    const selected = layout.map((comp, i) => `
        <div style="display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.6rem; background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3); border-radius: 6px; margin-bottom: 0.4rem;">
            <span style="color: var(--text-muted); font-size: 0.8rem;">#${i + 1}</span>
            <strong style="flex: 1;">${escapeHtml(blockingLabels[comp] || comp)}</strong>
            <button class="btn btn-sm" style="background: var(--surface-3); padding: 0.1rem 0.5rem;" ${i === 0 ? "disabled" : ""} onclick="moveBlockingComponent('${channel}', ${i}, -1)">↑</button>
            <button class="btn btn-sm" style="background: var(--surface-3); padding: 0.1rem 0.5rem;" ${i === layout.length - 1 ? "disabled" : ""} onclick="moveBlockingComponent('${channel}', ${i}, 1)">↓</button>
            <button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text); padding: 0.1rem 0.5rem;" onclick="removeBlockingComponent('${channel}', ${i})">✕</button>
        </div>`).join("") || '<p class="section-desc" style="color: var(--color-warning);">Au moins une composante est requise.</p>';
    const available = blockingComponents.filter(c => !layout.includes(c));
    const addOptions = available.map(c => `<option value="${c}">${escapeHtml(blockingLabels[c] || c)}</option>`).join("");
    el.innerHTML = `
        <div style="margin-bottom: 0.75rem;">
            <small style="color: var(--text-muted);">Source : ${source === "database" ? "base (personnalisé)" : "config.yaml (défaut)"}</small>
        </div>
        ${selected}
        ${available.length ? `
            <div style="display: flex; gap: 0.5rem; margin-top: 0.5rem;">
                <select id="blocking-add-${channel}" style="flex: 1;">${addOptions}</select>
                <button class="btn btn-sm btn-secondary" onclick="addBlockingComponent('${channel}')">+ Ajouter</button>
            </div>` : ""}
    `;
}

function moveBlockingComponent(channel, index, dir) {
    const layout = blockingDraft[channel];
    const target = index + dir;
    if (target < 0 || target >= layout.length) return;
    [layout[index], layout[target]] = [layout[target], layout[index]];
    renderBlockingEditor(channel, "database");
}

function removeBlockingComponent(channel, index) {
    if (blockingDraft[channel].length <= 1) { showToast("Au moins une composante est requise.", "warning"); return; }
    blockingDraft[channel].splice(index, 1);
    renderBlockingEditor(channel, "database");
}

function addBlockingComponent(channel) {
    const sel = document.getElementById(`blocking-add-${channel}`);
    if (sel && sel.value) {
        blockingDraft[channel].push(sel.value);
        renderBlockingEditor(channel, "database");
    }
}

async function saveBlocking(channel) {
    const payload = channel === "SCREENING"
        ? { screening_layout: blockingDraft.SCREENING }
        : { filtering_layout: blockingDraft.FILTERING };
    if (channel === "SCREENING") {
        const ok = await confirmDialog("Modifier la blocking key du criblage recharge immédiatement le cache de production et change la sélection des candidats. Continuer ?", { title: "Blocking Key — Criblage" });
        if (!ok) return;
    }
    try {
        const response = await apiFetch("/api/settings/blocking", {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "échec."), "error"); return; }
        showToast(data.message, "success");
        fetchBlockingSettings();
    } catch (e) {
        console.error("Error saving blocking:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

// ------------------ RÈGLES ANTI-FAUX POSITIFS ------------------

let fpRulesCache = [];
let currentFpRuleId = null;

function fpRuleChannel() {
    const sel = document.getElementById("rules-channel-select");
    return sel ? sel.value : "SCREENING";
}

function fpRuleStatusBadge(status) {
    const map = {
        DRAFT: ["#94a3b8", "BROUILLON"],
        PENDING_VALIDATION: ["#c084fc", "EN VALIDATION"],
        ACTIVE: ["var(--success-soft-text)", "ACTIVE"],
        SUPERSEDED: ["#6b7280", "REMPLACÉE"],
    };
    const [color, label] = map[status] || ["#9ca3af", status];
    return `<span style="color: ${color}; font-weight: 600; font-size: 0.78rem;">${label}</span>`;
}

async function fetchFpRules() {
    try {
        const response = await apiFetch(`/api/fprules?channel=${fpRuleChannel()}`);
        if (!response.ok) return;
        const data = await response.json();
        fpRulesCache = data.items || [];
        renderFpRulesTable();
    } catch (e) {
        console.error("Error fetching FP rules:", e);
    }
}

function renderFpRulesTable() {
    const tbody = document.querySelector("#fprules-table tbody");
    if (!tbody) return;
    if (!fpRulesCache.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Aucune règle pour ce canal. Créez-en une (mode DEV).</td></tr>';
        return;
    }
    tbody.innerHTML = fpRulesCache.map(r => `
        <tr>
            <td>${r.run_order}</td>
            <td><strong>${escapeHtml(r.name)}</strong>${r.enabled === false && r.status === "ACTIVE" ? ' <small style="color:var(--color-warning)">(désactivée)</small>' : ""}</td>
            <td>${fpRuleStatusBadge(r.status)}</td>
            <td>v${r.version}</td>
            <td>${r.hit_count}</td>
            <td><button class="btn btn-sm btn-secondary" onclick="openFpRule(${r.id})">Ouvrir</button></td>
        </tr>`).join("");
}

function newFpRule() {
    currentFpRuleId = null;
    const card = document.getElementById("fprule-editor-card");
    card.style.display = "block";
    document.getElementById("fprule-editor").innerHTML = fpRuleEditorHtml(null);
    card.scrollIntoView({ behavior: "smooth" });
}

async function openFpRule(ruleId) {
    const rule = fpRulesCache.find(r => r.id === ruleId);
    if (!rule) return;
    currentFpRuleId = ruleId;
    const card = document.getElementById("fprule-editor-card");
    card.style.display = "block";
    document.getElementById("fprule-editor").innerHTML = fpRuleEditorHtml(rule);
    card.scrollIntoView({ behavior: "smooth" });
    if (rule.status === "DRAFT") loadFpRuleTests(ruleId);
}

function fpRuleEditorHtml(rule) {
    const isNew = !rule;
    const status = rule ? rule.status : "DRAFT";
    const editable = isNew || status === "DRAFT";
    const me = currentUser ? currentUser.username : "";
    const branchBanner = rule && rule.replaces_rule_id
        ? `<div style="background: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3); border-radius: 6px; padding: 0.5rem 0.75rem; margin-bottom: 0.75rem; font-size: 0.85rem;">🌿 Nouvelle version (v${rule.version}) — branche de la règle #${rule.replaces_rule_id} en production.</div>` : "";
    let actions = "";
    if (editable) {
        actions = `
            <button class="btn btn-primary" onclick="saveFpRule()">💾 Enregistrer le brouillon</button>
            ${!isNew ? `<button class="btn btn-secondary" onclick="submitFpRule()">📤 Soumettre en validation</button>` : ""}
            ${!isNew ? `<button class="btn" style="background: rgba(239,68,68,0.2); color:var(--danger-soft-text);" onclick="deleteFpRule()">🗑️ Supprimer</button>` : ""}`;
    } else if (status === "PENDING_VALIDATION") {
        const isSubmitter = rule.submitted_by === me;
        actions = `
            <span style="align-self:center; font-size: 0.85rem; color: var(--text-muted);">Soumise par @${escapeHtml(rule.submitted_by || "")}${isSubmitter ? " (vous — un autre habilité doit valider)" : ""}</span>
            ${!isSubmitter ? `<button class="btn btn-primary" onclick="validateFpRule()">✔️ Valider & mettre en production (4-yeux)</button>` : ""}
            <button class="btn" style="background: rgba(239,68,68,0.2); color:var(--danger-soft-text);" onclick="rejectFpRule()">↩️ Renvoyer en brouillon</button>`;
    } else if (status === "ACTIVE") {
        actions = `
            <button class="btn btn-secondary" onclick="editFpRuleVersion()">✏️ Modifier (nouvelle version)</button>
            <button class="btn" style="background: var(--surface-3);" onclick="toggleFpRule()">${rule.enabled ? "⏸️ Désactiver" : "▶️ Activer"}</button>`;
    }
    return `
        ${branchBanner}
        <div style="display:flex; justify-content: space-between; align-items:center;">
            <h3 style="margin:0;">${isNew ? "Nouvelle règle" : escapeHtml(rule.name)} ${rule ? fpRuleStatusBadge(rule.status) : ""}</h3>
            <button class="btn btn-sm" style="background: var(--surface-3);" onclick="closeFpRuleEditor()">Fermer</button>
        </div>
        <div class="form-group"><label>Nom</label><input type="text" id="fprule-name" value="${escapeHtml(rule ? rule.name : "")}" ${editable ? "" : "disabled"}></div>
        <div class="form-group"><label>Description</label><input type="text" id="fprule-desc" value="${escapeHtml(rule ? (rule.description || "") : "")}" ${editable ? "" : "disabled"}></div>
        <div class="form-group" style="max-width: 160px;"><label>Ordre d'exécution</label><input type="number" id="fprule-order" value="${rule ? rule.run_order : 100}" ${editable ? "" : "disabled"}></div>
        ${editable ? fpRuleNlSectionHtml() : ""}
        ${editable ? fpRuleToolbarHtml() : ""}
        <div class="form-group">
            <label>Code Python — <code>def rule(ctx) -&gt; bool</code> (True = supprimer l'alerte)</label>
            <div style="position: relative;">
                <textarea id="fprule-code" rows="14" style="font-family: monospace; font-size: 0.82rem;" ${editable ? `oninput="onFpRuleCodeInput(event)" onkeydown="onFpRuleCodeKeydown(event)" onclick="hideFpAutocomplete()" onblur="setTimeout(hideFpAutocomplete, 200)"` : "disabled"}>${escapeHtml(rule ? rule.code : FP_RULE_TEMPLATE)}</textarea>
                <div id="fprule-autocomplete" class="fp-autocomplete hidden" role="listbox"></div>
            </div>
            ${editable ? '<div id="fprule-validate-msg" class="fp-validate-msg"></div>' : ""}
        </div>
        <div style="display:flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem;">${actions}</div>
        ${(rule && rule.status === "DRAFT") ? fpRuleDevBenchHtml() : ""}
        ${rule ? `<div id="fprule-changes" style="margin-top: 1rem;"></div>` : ""}
    `;
}

const FP_RULE_TEMPLATE = `def rule(ctx):
    """True = SUPPRIMER l'alerte (auto-clôture CLOSED_BY_RULE, tracée à l'audit).
    ctx : channel, client_name, entity_name, list_type, final_score, base_score,
    hard_match, adjustments, client, entity, party (filtrage), message (filtrage).
    Modules : re, math, datetime, date, timedelta, unicodedata."""
    # Exemple : supprimer les scores faibles sans correspondance exacte
    # return ctx["final_score"] < 80 and not ctx["hard_match"]
    return False
`;

// ---- Atelier d'édition : palette ctx, snippets, validation, autocomplétion ----

// Clés du contexte rule(ctx), typées pour la palette, l'autocomplétion et le
// formulaire structuré (miroir du contrat de fiskr/fprules.py)
const FP_CTX_KEYS = [
    { key: "channel", type: "str", desc: "SCREENING ou FILTERING" },
    { key: "client_id", type: "str", desc: "identifiant client" },
    { key: "client_name", type: "str", desc: "nom complet du client" },
    { key: "entity_id", type: "str", desc: "identifiant de l'entité listée" },
    { key: "entity_name", type: "str", desc: "nom principal de l'entité listée" },
    { key: "list_type", type: "str", desc: "liste d'origine (WATCHLIST_OFAC…)" },
    { key: "final_score", type: "num", desc: "score final 0-100" },
    { key: "base_score", type: "num", desc: "score avant ajustements" },
    { key: "hard_match", type: "bool", desc: "correspondance exacte (identifiant)" },
    { key: "adjustments", type: "dict", desc: "détail des ajustements de score" },
    { key: "client", type: "dict", desc: "profil client complet (criblage)" },
    { key: "entity", type: "dict", desc: "fiche listée complète" },
    { key: "party", type: "dict", desc: "partie du message (filtrage)" },
    { key: "message", type: "dict", desc: "message ISO 20022 (filtrage)" },
];
// Sous-clés les plus utiles pour l'autocomplétion imbriquée et le formulaire
const FP_CTX_SUBKEYS = {
    party: ["name", "roles", "country", "bic", "is_agent", "address", "birth_date"],
    message: ["type", "msg_id"],
    entity: ["entity_type", "primary_name", "countries", "dates_of_birth", "programs", "designation_date"],
    client: ["client_type", "client_first_name", "client_last_name", "client_company_name", "client_dob", "client_countries", "client_segment"],
};

// Modèles de code insérables (snippets)
const FP_RULE_SNIPPETS = [
    ["Seuil de score (hors hard match)", `    # Supprimer sous un seuil de score, jamais sur correspondance exacte
    return ctx["final_score"] < 82 and not ctx["hard_match"]`],
    ["Pays hors périmètre", `    # Supprimer si aucun pays de l'entité n'est dans la zone surveillée
    surveilles = {"RU", "IR", "KP", "SY"}
    pays = set((ctx.get("entity") or {}).get("countries", {}).get("citizenship") or [])
    return not (pays & surveilles) and not ctx["hard_match"]`],
    ["Motif regex sur le nom", `    # Supprimer les collisions sur un nom générique (ex : sociétés homonymes)
    return bool(re.search(r"\\b(TRADING|HOLDING)\\b", ctx["entity_name"] or "", re.I)) and ctx["final_score"] < 90`],
    ["Type d'entité différent", `    # Supprimer si le client est une personne physique et l'entité une société
    client_type = (ctx.get("client") or {}).get("client_type")
    entity_type = (ctx.get("entity") or {}).get("entity_type")
    return client_type == "PP" and entity_type == "E" and not ctx["hard_match"]`],
    ["Date de naissance absente côté liste", `    # Score moyen sans DOB côté liste : purement homonymique
    dobs = (ctx.get("entity") or {}).get("dates_of_birth") or []
    return not dobs and ctx["final_score"] < 88 and not ctx["hard_match"]`],
    ["Rôle de partie (filtrage)", `    # Filtrage : ignorer les agents techniques (banques intermédiaires)
    party = ctx.get("party") or {}
    return bool(party.get("is_agent")) and ctx["final_score"] < 92 and not ctx["hard_match"]`],
    ["Écart d'ajustement pays", `    # Supprimer quand le malus pays a déjà fortement réduit le score
    adj = ctx.get("adjustments") or {}
    return adj.get("country_penalty", 0) <= -10 and ctx["final_score"] < 85`],
    ["Combinaison ET/OU", `    # Combinaison de critères : score bas ET (pas de pays commun OU type différent)
    faible = ctx["final_score"] < 84 and not ctx["hard_match"]
    entity = ctx.get("entity") or {}
    sans_pays = not (entity.get("countries") or {}).get("citizenship")
    return faible and sans_pays`],
];

function fpRuleToolbarHtml() {
    const chips = FP_CTX_KEYS.map(k =>
        `<button type="button" class="fp-ctx-chip" title="${escapeHtml(k.desc)}" onclick="insertFpCtxKey('${k.key}')">${k.key}</button>`
    ).join("");
    const snippetOpts = FP_RULE_SNIPPETS.map((s, i) => `<option value="${i}">${escapeHtml(s[0])}</option>`).join("");
    return `
        <div class="fp-toolbar">
            <select id="fprule-snippet-select" onchange="insertFpSnippet(this)">
                <option value="">📋 Insérer un modèle…</option>${snippetOpts}
            </select>
            <button type="button" class="btn btn-sm btn-secondary" onclick="checkFpRuleSyntax(false)">✓ Vérifier la syntaxe</button>
        </div>
        <div class="fp-ctx-palette" title="Cliquer pour insérer au curseur">${chips}</div>`;
}

// Insertion au curseur dans la textarea de code
function insertAtFpCursor(text) {
    const ta = document.getElementById("fprule-code");
    if (!ta) return;
    ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
    ta.focus();
    scheduleFpValidation();
}

function insertFpCtxKey(key) {
    insertAtFpCursor(`ctx["${key}"]`);
}

function insertFpSnippet(sel) {
    const idx = sel.value;
    sel.value = "";
    if (idx === "") return;
    const snippet = FP_RULE_SNIPPETS[parseInt(idx, 10)];
    if (snippet) insertAtFpCursor(`\n${snippet[1]}\n`);
}

// ---- Validation serveur (débouncée 800 ms + bouton) ----
let fpValidateTimer = null;

function scheduleFpValidation() {
    clearTimeout(fpValidateTimer);
    fpValidateTimer = setTimeout(() => checkFpRuleSyntax(true), 800);
}

async function checkFpRuleSyntax(silent) {
    const ta = document.getElementById("fprule-code");
    const msgEl = document.getElementById("fprule-validate-msg");
    if (!ta || !msgEl) return;
    try {
        const response = await apiFetch("/api/fprules/validate", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code: ta.value }), silent: true,
        });
        if (!response.ok) return;
        const v = await response.json();
        if (v.valid) {
            msgEl.innerHTML = '<span style="color: var(--success-soft-text);">✓ Syntaxe valide — <code>rule(ctx)</code> prête à être testée.</span>';
        } else if (v.line) {
            // Message cliquable : positionne le curseur sur la ligne fautive
            msgEl.innerHTML = `<a href="javascript:void(0)" onclick="gotoFpRuleLine(${v.line})" style="color: var(--color-alert);">⚠ Ligne ${v.line}${v.offset ? `, col. ${v.offset}` : ""} : ${escapeHtml(v.error || "syntaxe invalide")}</a>`;
        } else {
            msgEl.innerHTML = `<span style="color: var(--color-alert);">⚠ ${escapeHtml(v.error || "code invalide")}</span>`;
        }
    } catch (e) { if (!silent) showToast("Validation impossible (réseau).", "error"); }
}

function gotoFpRuleLine(line) {
    const ta = document.getElementById("fprule-code");
    if (!ta) return;
    const lines = ta.value.split("\n");
    let pos = 0;
    for (let i = 0; i < Math.min(line - 1, lines.length); i++) pos += lines[i].length + 1;
    ta.focus();
    ta.setSelectionRange(pos, pos + (lines[line - 1] || "").length);
}

// ---- Autocomplétion maison sur ctx[" (zéro lib, CSP OK) ----
let fpAcState = { open: false, items: [], selected: 0, anchor: 0 };

function onFpRuleCodeInput() {
    scheduleFpValidation();
    updateFpAutocomplete();
}

// Position pixel du caret dans une textarea : technique du div miroir
function fpCaretCoords(ta) {
    const div = document.createElement("div");
    const style = getComputedStyle(ta);
    ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
     "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
     "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
     "boxSizing", "whiteSpace", "wordWrap", "width"].forEach(p => { div.style[p] = style[p]; });
    div.style.position = "absolute";
    div.style.visibility = "hidden";
    div.style.whiteSpace = "pre-wrap";
    div.style.wordWrap = "break-word";
    div.textContent = ta.value.substring(0, ta.selectionStart);
    const marker = document.createElement("span");
    marker.textContent = "​";
    div.appendChild(marker);
    document.body.appendChild(div);
    const coords = { left: marker.offsetLeft, top: marker.offsetTop + marker.offsetHeight };
    document.body.removeChild(div);
    return { left: coords.left - ta.scrollLeft, top: coords.top - ta.scrollTop };
}

function updateFpAutocomplete() {
    const ta = document.getElementById("fprule-code");
    const box = document.getElementById("fprule-autocomplete");
    if (!ta || !box) return;
    const before = ta.value.substring(0, ta.selectionStart);
    // Déclencheur 1 : ctx[" ou ctx[' en cours de frappe
    let m = before.match(/ctx\[\s*["']([A-Za-z_]*)$/);
    let items = [];
    let anchor = 0;
    if (m) {
        const prefix = m[1].toLowerCase();
        anchor = ta.selectionStart - m[1].length;
        items = FP_CTX_KEYS.filter(k => k.key.startsWith(prefix))
            .map(k => ({ label: k.key, hint: k.type, insert: k.key }));
    } else {
        // Déclencheur 2 : sous-clés — party.get(" / (ctx.get("party") or {}).get("
        m = before.match(/(party|message|entity|client)"?\)?(?:\s*or\s*\{\})?\)?\.get\(\s*["']([A-Za-z_]*)$/);
        if (m && FP_CTX_SUBKEYS[m[1]]) {
            const prefix = m[2].toLowerCase();
            anchor = ta.selectionStart - m[2].length;
            items = FP_CTX_SUBKEYS[m[1]].filter(k => k.startsWith(prefix))
                .map(k => ({ label: `${m[1]}.${k}`, hint: "", insert: k }));
        }
    }
    if (!items.length) { hideFpAutocomplete(); return; }
    fpAcState = { open: true, items, selected: 0, anchor };
    const pos = fpCaretCoords(ta);
    box.style.left = `${Math.min(pos.left, ta.clientWidth - 230)}px`;
    box.style.top = `${pos.top}px`;
    renderFpAutocomplete();
    box.classList.remove("hidden");
}

function renderFpAutocomplete() {
    const box = document.getElementById("fprule-autocomplete");
    if (!box) return;
    box.innerHTML = fpAcState.items.map((it, i) =>
        `<div class="fp-ac-item${i === fpAcState.selected ? " selected" : ""}" role="option" onmousedown="event.preventDefault(); pickFpAutocomplete(${i})">
            <span>${escapeHtml(it.label)}</span>${it.hint ? `<small>${escapeHtml(it.hint)}</small>` : ""}
        </div>`).join("");
}

function pickFpAutocomplete(index) {
    const ta = document.getElementById("fprule-code");
    const item = fpAcState.items[index];
    if (!ta || !item) return;
    ta.setRangeText(item.insert, fpAcState.anchor, ta.selectionStart, "end");
    hideFpAutocomplete();
    ta.focus();
    scheduleFpValidation();
}

function hideFpAutocomplete() {
    const box = document.getElementById("fprule-autocomplete");
    if (box) box.classList.add("hidden");
    fpAcState.open = false;
}

function onFpRuleCodeKeydown(e) {
    if (!fpAcState.open) return;
    if (e.key === "ArrowDown") {
        e.preventDefault();
        fpAcState.selected = (fpAcState.selected + 1) % fpAcState.items.length;
        renderFpAutocomplete();
    } else if (e.key === "ArrowUp") {
        e.preventDefault();
        fpAcState.selected = (fpAcState.selected - 1 + fpAcState.items.length) % fpAcState.items.length;
        renderFpAutocomplete();
    } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pickFpAutocomplete(fpAcState.selected);
    } else if (e.key === "Escape") {
        hideFpAutocomplete();
    }
}

// ---- Test unitaire pré-rempli depuis une alerte réelle ----
async function addFpRuleTestFromAlert() {
    if (!currentFpRuleId) { showToast("Enregistrez d'abord le brouillon.", "warning"); return; }
    const alertId = await promptDialog("N° de l'alerte à rejouer (le contexte ctx sera pré-rempli)");
    if (!alertId) return;
    try {
        const response = await apiFetch(`/api/fprules/context-from-alert/${encodeURIComponent(alertId.trim())}`);
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "alerte introuvable."), "error"); return; }
        const ctxRaw = await promptDialog(
            `Contexte reconstruit de l'alerte #${data.alert_id} — ajustez si besoin puis validez`,
            { textarea: true, message: JSON.stringify(data.ctx, null, 2).substring(0, 4000) }
        );
        // Le contexte proposé est affiché dans le message ; champ vide = le reprendre tel quel
        let ctx;
        try { ctx = ctxRaw && ctxRaw.trim() ? JSON.parse(ctxRaw) : data.ctx; }
        catch (e) { showToast("JSON invalide.", "error"); return; }
        if (ctxRaw === null) return;
        const expected = await confirmDialog(
            "La règle doit-elle SUPPRIMER l'alerte pour ce cas ? (Annuler = conserver)",
            { confirmLabel: "Supprimer", cancelLabel: "Conserver" });
        const resp2 = await apiFetch(`/api/fprules/${currentFpRuleId}/tests`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: `Alerte #${data.alert_id}`, ctx, expected }),
        });
        if (!resp2.ok) { const d = await resp2.json(); showToast("Erreur : " + (d.detail || ""), "error"); return; }
        showToast("Cas de test créé depuis l'alerte.", "success");
        loadFpRuleTests(currentFpRuleId);
    } catch (e) { showToast("Erreur réseau.", "error"); }
}

// ---- Création en langage naturel : IA + formulaire structuré ----

function fpRuleNlSectionHtml() {
    return `
        <div class="fp-nl-section">
            <label style="font-weight: 600;">🗣️ Décrire la règle en langage naturel</label>
            <p class="section-desc" style="margin: 0.2rem 0 0.5rem;">Deux assistants génèrent un brouillon de code dans l'éditeur ci-dessous — le circuit de gouvernance (tests, soumission, validation 4-yeux) reste inchangé.</p>
            <textarea id="fprule-nl-input" rows="2" placeholder="Ex : supprimer les alertes sous 85 % quand l'entité listée n'a pas de date de naissance, sauf correspondance exacte"></textarea>
            <div style="display:flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.4rem;">
                <button type="button" class="btn btn-sm btn-primary" onclick="generateFpRuleFromNL()">✨ Générer par IA</button>
                <button type="button" class="btn btn-sm btn-secondary" onclick="toggleFpFormBuilder()">🧩 Formulaire structuré (sans IA)</button>
            </div>
            <div id="fprule-nl-status" style="margin-top: 0.4rem;"></div>
            <div id="fprule-form-builder" class="hidden" style="margin-top: 0.75rem;"></div>
        </div>`;
}

async function generateFpRuleFromNL() {
    const input = document.getElementById("fprule-nl-input");
    const statusEl = document.getElementById("fprule-nl-status");
    const instruction = input ? input.value.trim() : "";
    if (!instruction) { showToast("Décrivez d'abord la règle souhaitée.", "warning"); return; }
    statusEl.innerHTML = '<small style="color: var(--text-muted);">✨ Génération en cours…</small>';
    try {
        const response = await apiFetch("/api/fprules/generate", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ instruction, channel: fpRuleChannel() }),
        });
        const data = await response.json();
        if (response.status === 503) {
            statusEl.innerHTML = `<small style="color: var(--color-warning);">⚠ ${escapeHtml(data.detail || "IA non configurée.")} </small>`;
            toggleFpFormBuilder(true);
            return;
        }
        if (response.status === 422 && data.detail && data.detail.raw_code) {
            document.getElementById("fprule-code").value = data.detail.raw_code;
            statusEl.innerHTML = `<small style="color: var(--color-alert);">⚠ ${escapeHtml(data.detail.message || "Code généré invalide")} — le code brut a été déposé dans l'éditeur pour correction manuelle.</small>`;
            checkFpRuleSyntax(true);
            return;
        }
        if (!response.ok) {
            statusEl.innerHTML = `<small style="color: var(--color-alert);">Erreur : ${escapeHtml(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))}</small>`;
            return;
        }
        document.getElementById("fprule-code").value = data.code;
        statusEl.innerHTML = `<small style="color: var(--success-soft-text);">✓ Brouillon généré (${escapeHtml(data.model || "IA")})${data.explanation ? " — " + escapeHtml(data.explanation) : ""}. Relisez, testez, puis soumettez.</small>`;
        checkFpRuleSyntax(true);
    } catch (e) {
        statusEl.innerHTML = '<small style="color: var(--color-alert);">Erreur réseau pendant la génération.</small>';
    }
}

// Champs proposés par le formulaire structuré (générateur de code sans IA)
const FP_FORM_FIELDS = [
    { id: "final_score", label: "Score final (%)", type: "num", access: 'ctx["final_score"]' },
    { id: "base_score", label: "Score de base (%)", type: "num", access: 'ctx["base_score"]' },
    { id: "hard_match", label: "Correspondance exacte", type: "bool", access: 'ctx["hard_match"]' },
    { id: "list_type", label: "Liste d'origine", type: "str", access: 'ctx["list_type"]' },
    { id: "entity_name", label: "Nom de l'entité listée", type: "str", access: 'ctx["entity_name"]' },
    { id: "client_name", label: "Nom du client", type: "str", access: 'ctx["client_name"]' },
    { id: "entity_type", label: "Type d'entité (I/E)", type: "str", access: '(ctx.get("entity") or {}).get("entity_type")' },
    { id: "client_type", label: "Type de client (PP/PM)", type: "str", access: '(ctx.get("client") or {}).get("client_type")' },
    { id: "party_country", label: "Pays de la partie (filtrage)", type: "str", access: '(ctx.get("party") or {}).get("country")' },
    { id: "party_is_agent", label: "Partie = agent technique (filtrage)", type: "bool", access: '(ctx.get("party") or {}).get("is_agent")' },
];
const FP_FORM_OPERATORS = {
    num: [["<", "inférieur à"], ["<=", "inférieur ou égal à"], [">", "supérieur à"], [">=", "supérieur ou égal à"], ["==", "égal à"]],
    str: [["==", "égal à"], ["!=", "différent de"], ["contains", "contient"], ["regex", "correspond à la regex"]],
    bool: [["is_true", "est vrai"], ["is_false", "est faux"]],
};

function toggleFpFormBuilder(forceOpen) {
    const panel = document.getElementById("fprule-form-builder");
    if (!panel) return;
    const open = forceOpen === true || panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !open);
    if (open && !panel.innerHTML) {
        panel.innerHTML = `
            <div style="border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem; background: var(--surface-hover);">
                <strong>Conditions (la règle SUPPRIME l'alerte quand elles sont réunies)</strong>
                <div id="fprule-form-rows" style="margin-top: 0.5rem;"></div>
                <div style="display:flex; gap: 0.5rem; align-items:center; flex-wrap: wrap; margin-top: 0.5rem;">
                    <button type="button" class="btn btn-sm" style="background: var(--surface-3);" onclick="addFpFormRow()">+ Condition</button>
                    <label style="display:flex; align-items:center; gap:0.3rem; font-size:0.85rem;">Combinaison
                        <select id="fprule-form-combinator"><option value="and">ET (toutes)</option><option value="or">OU (au moins une)</option></select>
                    </label>
                    <label style="display:flex; align-items:center; gap:0.3rem; font-size:0.85rem;">
                        <input type="checkbox" id="fprule-form-guard" checked> Ne jamais supprimer un hard match
                    </label>
                    <button type="button" class="btn btn-sm btn-primary" onclick="insertFpFormCode()">⤵ Générer le code dans l'éditeur</button>
                </div>
            </div>`;
        addFpFormRow();
    }
}

function addFpFormRow() {
    const rows = document.getElementById("fprule-form-rows");
    if (!rows) return;
    const idx = rows.children.length;
    const fieldOpts = FP_FORM_FIELDS.map(f => `<option value="${f.id}">${escapeHtml(f.label)}</option>`).join("");
    const row = document.createElement("div");
    row.className = "fp-form-row";
    row.innerHTML = `
        <select class="fp-form-field" onchange="onFpFormFieldChange(this)">${fieldOpts}</select>
        <select class="fp-form-op">${FP_FORM_OPERATORS.num.map(o => `<option value="${o[0]}">${o[1]}</option>`).join("")}</select>
        <input type="text" class="fp-form-value" placeholder="valeur">
        <button type="button" class="btn btn-sm" style="background: rgba(239,68,68,0.15); color: var(--danger-soft-text);" onclick="this.parentElement.remove()">✕</button>`;
    rows.appendChild(row);
    if (idx === 0) onFpFormFieldChange(row.querySelector(".fp-form-field"));
}

function onFpFormFieldChange(sel) {
    const field = FP_FORM_FIELDS.find(f => f.id === sel.value);
    const row = sel.parentElement;
    const opSel = row.querySelector(".fp-form-op");
    const valInput = row.querySelector(".fp-form-value");
    const ops = FP_FORM_OPERATORS[field ? field.type : "num"];
    opSel.innerHTML = ops.map(o => `<option value="${o[0]}">${o[1]}</option>`).join("");
    valInput.style.display = (field && field.type === "bool") ? "none" : "";
}

// Génère du Python déterministe depuis les lignes du formulaire (accès sûrs,
// valeurs échappées via JSON.stringify) — le serveur reste le garde-fou final
function buildRuleCode(conditions, combinator, guardHardMatch) {
    const parts = conditions.map(c => {
        const field = FP_FORM_FIELDS.find(f => f.id === c.field);
        if (!field) return null;
        const acc = field.access;
        if (field.type === "bool") return c.op === "is_true" ? `bool(${acc})` : `not ${acc}`;
        if (field.type === "num") {
            const num = parseFloat(String(c.value).replace(",", "."));
            if (isNaN(num)) return null;
            return `(${acc} or 0) ${c.op} ${num}`;
        }
        const value = JSON.stringify(String(c.value));
        if (c.op === "contains") return `${value}.lower() in str(${acc} or "").lower()`;
        if (c.op === "regex") return `bool(re.search(${value}, str(${acc} or ""), re.I))`;
        return `str(${acc} or "") ${c.op} ${value}`;
    }).filter(Boolean);
    if (!parts.length) return null;
    const joiner = combinator === "or" ? " or " : " and ";
    const conditionExpr = parts.length > 1 ? parts.map(p => `(${p})`).join(joiner) : parts[0];
    const guard = guardHardMatch ? `    if ctx["hard_match"]:\n        return False  # garde-fou : jamais de suppression sur correspondance exacte\n` : "";
    return `def rule(ctx):\n    """Générée par le formulaire structuré — ${conditions.length} condition(s), combinaison ${combinator === "or" ? "OU" : "ET"}."""\n${guard}    return ${conditionExpr}\n`;
}

function insertFpFormCode() {
    const rows = Array.from(document.querySelectorAll("#fprule-form-rows .fp-form-row"));
    const conditions = rows.map(r => ({
        field: r.querySelector(".fp-form-field").value,
        op: r.querySelector(".fp-form-op").value,
        value: r.querySelector(".fp-form-value").value,
    }));
    const combinator = document.getElementById("fprule-form-combinator").value;
    const guard = document.getElementById("fprule-form-guard").checked;
    const code = buildRuleCode(conditions, combinator, guard);
    if (!code) { showToast("Complétez au moins une condition valide.", "warning"); return; }
    document.getElementById("fprule-code").value = code;
    showToast("Code généré dans l'éditeur — relisez puis testez.", "success");
    checkFpRuleSyntax(true);
}

function fpRuleDevBenchHtml() {
    return `
        <hr style="border-color: var(--border-color); margin: 1.25rem 0;">
        <h4 style="margin: 0 0 0.5rem;">🧪 Banc d'essai (mode DEV) — la production n'est pas touchée</h4>
        <div id="fprule-tests-section"></div>
        <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem;">
            <button class="btn btn-sm btn-secondary" onclick="runFpRuleTests()">▶ Lancer les tests unitaires</button>
            <button class="btn btn-sm" style="background: var(--surface-3);" onclick="benchFpRule('history')">📜 Rejouer l'historique réel</button>
            <button class="btn btn-sm" style="background: var(--surface-3);" onclick="addFpRuleTestFromAlert()">🎯 + Test depuis une alerte</button>
        </div>
        <div id="fprule-bench-result" style="margin-top: 0.75rem;"></div>`;
}

function closeFpRuleEditor() {
    document.getElementById("fprule-editor-card").style.display = "none";
    currentFpRuleId = null;
}

function _fpRulePayload() {
    return {
        name: document.getElementById("fprule-name").value.trim(),
        description: document.getElementById("fprule-desc").value.trim(),
        run_order: parseInt(document.getElementById("fprule-order").value, 10) || 100,
        code: document.getElementById("fprule-code").value,
    };
}

async function saveFpRule() {
    const payload = _fpRulePayload();
    if (!payload.name) { showToast("Le nom est requis.", "warning"); return; }
    try {
        let response;
        if (currentFpRuleId) {
            response = await apiFetch(`/api/fprules/${currentFpRuleId}`, {
                method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
            });
        } else {
            response = await apiFetch("/api/fprules", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ...payload, channel: fpRuleChannel() }),
            });
        }
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "échec."), "error"); return; }
        showToast("Règle enregistrée (brouillon).", "success");
        await fetchFpRules();
        openFpRule(data.id);
    } catch (e) {
        console.error("Error saving FP rule:", e);
        showToast("Erreur réseau de communication.", "error");
    }
}

async function _fpRuleAction(path, body, confirmMsg) {
    if (!currentFpRuleId) return;
    if (confirmMsg && !await confirmDialog(confirmMsg)) return;
    try {
        const response = await apiFetch(`/api/fprules/${currentFpRuleId}/${path}`, {
            method: path === "delete" ? "DELETE" : "POST",
            headers: { "Content-Type": "application/json" },
            body: path === "delete" ? undefined : JSON.stringify(body || {}),
        });
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "échec."), "error"); return null; }
        showToast(data.message || "OK", "success");
        return data;
    } catch (e) {
        console.error("FP rule action error:", e);
        showToast("Erreur réseau de communication.", "error");
        return null;
    }
}

async function submitFpRule() {
    // Enregistre d'abord le code courant, puis soumet
    await saveFpRule();
    const data = await _fpRuleAction("submit", {});
    if (data) { await fetchFpRules(); openFpRule(currentFpRuleId); }
}

async function validateFpRule() {
    const comment = await promptDialog("Commentaire de validation (facultatif)", { textarea: true, required: false });
    if (comment === null) return;
    const data = await _fpRuleAction("validate", { comment });
    if (data) { await fetchFpRules(); closeFpRuleEditor(); refreshAlertQueues(); }
}

async function rejectFpRule() {
    const comment = await promptDialog("Motif du renvoi en brouillon (obligatoire)", { textarea: true });
    if (!comment) return;
    const data = await _fpRuleAction("reject", { comment });
    if (data) { await fetchFpRules(); openFpRule(currentFpRuleId); }
}

async function toggleFpRule() {
    const data = await _fpRuleAction("toggle", {});
    if (data) { await fetchFpRules(); openFpRule(currentFpRuleId); refreshAlertQueues(); }
}

async function deleteFpRule() {
    if (!await confirmDialog("Supprimer ce brouillon de règle ?", { danger: true })) return;
    const response = await apiFetch(`/api/fprules/${currentFpRuleId}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) { showToast("Erreur : " + (data.detail || "échec."), "error"); return; }
    showToast("Règle supprimée.", "success");
    closeFpRuleEditor();
    fetchFpRules();
}

async function editFpRuleVersion() {
    // Modifier une règle ACTIVE crée une nouvelle version brouillon (branche)
    const payload = _fpRulePayload();
    const response = await apiFetch(`/api/fprules/${currentFpRuleId}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) { showToast("Erreur : " + (data.detail || "échec."), "error"); return; }
    showToast("Nouvelle version brouillon créée (branche de la production).", "success");
    await fetchFpRules();
    openFpRule(data.id);
}

// ---- Banc d'essai : tests unitaires ----

async function loadFpRuleTests(ruleId) {
    try {
        const response = await apiFetch(`/api/fprules/${ruleId}/tests`);
        if (!response.ok) return;
        const data = await response.json();
        renderFpRuleTests(data.items || []);
    } catch (e) { console.error("Error loading rule tests:", e); }
}

function renderFpRuleTests(tests) {
    const el = document.getElementById("fprule-tests-section");
    if (!el) return;
    const rows = tests.map(t => {
        const state = t.last_run_at
            ? (t.last_error ? `<span style="color:var(--color-alert);">erreur</span>`
                : (t.last_result === t.expected ? `<span style="color:var(--success-soft-text);">✔ vert</span>` : `<span style="color:var(--color-alert);">✘ échec</span>`))
            : '<span style="color:var(--text-muted);">non exécuté</span>';
        return `<tr>
            <td>${escapeHtml(t.name)}</td>
            <td>${t.expected ? "supprimer" : "conserver"}</td>
            <td>${state}</td>
            <td><button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color:var(--danger-soft-text); padding: 0.1rem 0.5rem;" onclick="deleteFpRuleTest(${t.id})">✕</button></td>
        </tr>`;
    }).join("");
    el.innerHTML = `
        <p class="section-desc" style="margin: 0.25rem 0;">Tests unitaires (soumission bloquée tant qu'ils ne sont pas tous verts) :</p>
        <div class="table-container" style="max-height: 180px; overflow-y: auto;">
            <table><thead><tr><th>Nom</th><th>Attendu</th><th>Résultat</th><th></th></tr></thead>
            <tbody>${rows || '<tr><td colspan="4" style="color:var(--text-muted); text-align:center;">Aucun test. Ajoutez-en au moins un.</td></tr>'}</tbody></table>
        </div>
        <button class="btn btn-sm btn-secondary" style="margin-top: 0.5rem;" onclick="addFpRuleTest()">+ Ajouter un cas de test</button>`;
}

async function addFpRuleTest() {
    const name = await promptDialog("Nom du cas de test");
    if (!name) return;
    const ctxRaw = await promptDialog("Contexte ctx (JSON) — ex : {\"final_score\": 72, \"hard_match\": false}", { textarea: true });
    if (ctxRaw === null) return;
    let ctx;
    try { ctx = JSON.parse(ctxRaw); } catch (e) { showToast("JSON invalide.", "error"); return; }
    const expected = await confirmDialog("La règle doit-elle SUPPRIMER l'alerte pour ce cas ? (Annuler = conserver)", { confirmLabel: "Supprimer", cancelLabel: "Conserver" });
    try {
        const response = await apiFetch(`/api/fprules/${currentFpRuleId}/tests`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, ctx, expected }),
        });
        if (!response.ok) { const d = await response.json(); showToast("Erreur : " + (d.detail || ""), "error"); return; }
        loadFpRuleTests(currentFpRuleId);
    } catch (e) { showToast("Erreur réseau.", "error"); }
}

async function deleteFpRuleTest(testId) {
    await apiFetch(`/api/fprules/${currentFpRuleId}/tests/${testId}`, { method: "DELETE" });
    loadFpRuleTests(currentFpRuleId);
}

async function runFpRuleTests() {
    // Enregistre le code courant avant de tester
    await saveFpRule();
    const response = await apiFetch(`/api/fprules/${currentFpRuleId}/tests/run`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) { showToast("Erreur : " + (data.detail || ""), "error"); return; }
    loadFpRuleTests(currentFpRuleId);
    showToast(`Tests : ${data.passed}/${data.total} vert(s)${data.all_green ? " — prêt à soumettre" : ""}.`, data.all_green ? "success" : "warning");
}

async function benchFpRule(source) {
    await saveFpRule();
    const bench = document.getElementById("fprule-bench-result");
    if (bench) bench.innerHTML = '<small style="color: var(--text-muted);">Rejeu en cours…</small>';
    try {
        const response = await apiFetch(`/api/fprules/${currentFpRuleId}/bench`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source, sample_size: 200 }),
        });
        const data = await response.json();
        if (!response.ok) { if (bench) bench.innerHTML = `<small style="color:var(--color-alert);">${escapeHtml(data.detail || "échec")}</small>`; return; }
        const tp = data.true_positive_hits || [];
        if (bench) bench.innerHTML = `
            <div style="background: var(--surface-hover); border: 1px solid var(--border-color); border-radius: 6px; padding: 0.75rem;">
                <strong>Rejeu (${escapeHtml(source === "history" ? "historique réel" : "panel")})</strong> —
                supprimées : <strong>${data.suppressed}</strong>, conservées : ${data.kept}, erreurs : ${data.errors}
                ${tp.length ? `<div style="margin-top: 0.5rem; color: var(--color-alert);">⚠️ ${tp.length} VRAI(S) POSITIF(S) confirmé(s) seraient supprimé(s) : ${tp.slice(0, 5).map(t => escapeHtml(t.client_name + " × " + t.entity_name)).join(", ")}${tp.length > 5 ? "…" : ""}. Corrigez la règle avant de soumettre.</div>` : (data.suppressed ? '<div style="margin-top: 0.5rem; color:var(--success-soft-text);">Aucun vrai positif confirmé impacté.</div>' : "")}
            </div>`;
    } catch (e) { if (bench) bench.innerHTML = '<small style="color:var(--color-alert);">Erreur réseau.</small>'; }
}

// ------------------ EXPORTS CSV (vue base des listes & journal d'audit) ------------------

function exportWatchlistCsv() {
    const params = new URLSearchParams();
    const scopeEl = document.getElementById("wl-scope-filter");
    params.set("scope", scopeEl && scopeEl.value ? scopeEl.value : "production");
    const listEl = document.getElementById("wl-list-filter");
    if (listEl && listEl.value) params.set("list_type", listEl.value);
    const searchEl = document.getElementById("wl-search-input");
    if (searchEl && searchEl.value.trim()) {
        params.set("search", searchEl.value.trim());
        const fieldEl = document.getElementById("wl-field-filter");
        if (fieldEl && fieldEl.value && fieldEl.value !== "default") params.set("search_field", fieldEl.value);
    }
    window.open(`/api/export/watchlist.csv?${params.toString()}`, "_blank");
}

function exportHistoryCsv() {
    const params = new URLSearchParams();
    const listEl = document.getElementById("audit-list-filter");
    if (listEl && listEl.value) params.set("list_type", listEl.value);
    const statusEl = document.getElementById("audit-status-filter");
    if (statusEl && statusEl.value) params.set("status", statusEl.value);
    window.open(`/api/export/history.csv?${params.toString()}`, "_blank");
}

// ------------------ JOURNAL DES ACTIONS D'ADMINISTRATION ------------------

const ADMIN_ACTION_LABELS = {
    USER_CREATED: "Compte créé", USER_UPDATED: "Compte modifié", USER_DELETED: "Compte supprimé",
    SETTINGS_UPDATED: "Réglages modifiés", BLOCKING_UPDATED: "Blocking keys modifiées",
    SNAPSHOTS_PURGED: "Snapshots purgés", WHITELIST_REVOKED: "Liste blanche révoquée",
};

function _adminLogDelta(row) {
    const parts = [];
    if (row.before && Object.keys(row.before).length) {
        parts.push(`<div><small style="color: var(--text-muted);">Avant :</small> <code style="font-size: 0.72rem;">${escapeHtml(JSON.stringify(row.before))}</code></div>`);
    }
    if (row.after && Object.keys(row.after).length) {
        parts.push(`<div><small style="color: var(--text-muted);">Après :</small> <code style="font-size: 0.72rem;">${escapeHtml(JSON.stringify(row.after))}</code></div>`);
    }
    if (row.detail) parts.push(`<div style="font-size: 0.78rem;">${escapeHtml(row.detail)}</div>`);
    return parts.join("") || '<small style="color: var(--text-muted);">—</small>';
}

async function fetchAdminLog() {
    const tbody = document.querySelector("#admin-log-table tbody");
    if (!tbody) return;
    tableLoading(tbody, 5);
    try {
        const response = await apiFetch("/api/admin-log?page_size=100");
        if (!response.ok) {
            tableEmpty(tbody, 5, "Accès réservé aux administrateurs.", "🔒");
            return;
        }
        const data = await response.json();
        const items = data.items || [];
        if (!items.length) {
            tableEmpty(tbody, 5, "Aucune action d'administration enregistrée.");
            return;
        }
        tbody.innerHTML = items.map(r => `
            <tr>
                <td>${formatDateTime(r.at)}</td>
                <td>@${escapeHtml(r.username)}</td>
                <td><span class="badge-secondary">${escapeHtml(ADMIN_ACTION_LABELS[r.action] || r.action)}</span></td>
                <td>${escapeHtml(r.target || "—")}</td>
                <td>${_adminLogDelta(r)}</td>
            </tr>`).join("");
    } catch (e) {
        console.error("Erreur de chargement du journal d'administration :", e);
    }
}

// ------------------ RECHERCHE GLOBALE (Ctrl+K) ------------------

let _paletteDebounce = null;
let _paletteSelection = 0;

const PALETTE_NAV_ITEMS = [
    { label: "🏠 Vue d'ensemble", action: () => switchTab("home") },
    { label: "📋 Gestion des Watchlists", action: () => switchTab("watchlist-mgmt") },
    { label: "🔍 Criblage temps réel", action: () => switchTab("screening") },
    { label: "💸 Filtrage transactionnel (ISO 20022)", action: () => { switchTab("screening"); switchSubTab("screening", "screening-transactions"); } },
    { label: "🚨 Alertes de criblage", action: () => { switchTab("alerts"); switchSubTab("alerts", "alerts-screening"); } },
    { label: "🚨 Alertes de filtrage", action: () => { switchTab("alerts"); switchSubTab("alerts", "alerts-filtering"); } },
    { label: "🛡️ Liste blanche (Good Guys)", action: () => { switchTab("alerts"); switchSubTab("alerts", "alerts-whitelist"); } },
    { label: "📥 Homologation des listes", action: () => { switchTab("watchlist-mgmt"); switchSubTab("watchlist-mgmt", "watchlist-review"); } },
    { label: "📈 Pilotage (KPI)", action: () => switchTab("kpi") },
    { label: "📜 Audit réglementaire", action: () => switchTab("audit") },
];

function openCommandPalette() {
    const overlay = document.getElementById("command-palette");
    if (!overlay) return;
    overlay.classList.remove("hidden");
    const input = document.getElementById("palette-input");
    input.value = "";
    renderPaletteResults([]);
    input.focus();
}

function closeCommandPalette() {
    const overlay = document.getElementById("command-palette");
    if (overlay) overlay.classList.add("hidden");
}

function renderPaletteResults(groups) {
    const container = document.getElementById("palette-results");
    if (!container) return;
    _paletteSelection = 0;
    if (!groups.length) {
        container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem 0.75rem;">Tapez pour chercher un listé, une alerte, ou naviguer…</p>';
        return;
    }
    let idx = 0;
    container.innerHTML = groups.map(group => `
        <div class="palette-group">
            <div class="palette-group-title">${escapeHtml(group.title)}</div>
            ${group.items.map(item => {
                const html = `<div class="palette-item ${idx === 0 ? "selected" : ""}" data-idx="${idx}"
                    onmouseenter="paletteHover(${idx})" onclick="paletteActivate(${idx})">${item.html}</div>`;
                idx += 1;
                return html;
            }).join("")}
        </div>`).join("");
    container._flatActions = groups.flatMap(g => g.items.map(i => i.action));
}

function paletteHover(idx) {
    _paletteSelection = idx;
    document.querySelectorAll("#palette-results .palette-item").forEach(el => {
        el.classList.toggle("selected", parseInt(el.dataset.idx, 10) === idx);
    });
}

function paletteActivate(idx) {
    const container = document.getElementById("palette-results");
    const actions = container && container._flatActions;
    if (actions && actions[idx]) {
        closeCommandPalette();
        actions[idx]();
    }
}

async function runPaletteSearch(term) {
    const groups = [];
    const needle = term.trim().toLowerCase();
    // Navigation (filtrée localement)
    const navMatches = PALETTE_NAV_ITEMS.filter(n => !needle || n.label.toLowerCase().includes(needle)).slice(0, 5);
    if (navMatches.length) {
        groups.push({ title: "Navigation", items: navMatches.map(n => ({
            html: escapeHtml(n.label), action: n.action })) });
    }
    if (needle.length >= 2) {
        try {
            const [wlResp, alResp] = await Promise.all([
                apiFetch(`/api/watchlist/db?search=${encodeURIComponent(term)}&page_size=5`, { silent: true }),
                apiFetch(`/api/alerts?search=${encodeURIComponent(term)}&page_size=5`, { silent: true }),
            ]);
            if (wlResp.ok) {
                const wl = await wlResp.json();
                if ((wl.items || []).length) {
                    groups.push({ title: `Listés (${wl.total})`, items: wl.items.map(item => ({
                        html: `<strong>${escapeHtml(item.primary_name)}</strong> <small style="color: var(--text-muted);">${escapeHtml(item.entity_id)} · ${escapeHtml(listTypeLabel(item.list_type))}${wl.match_mode === "fuzzy" ? " · ≈" : ""}</small>`,
                        action: () => { switchTab("watchlist-mgmt"); switchSubTab("watchlist-mgmt", "watchlist-active"); showWatchlistDetails(item); },
                    })) });
                }
            }
            if (alResp.ok) {
                const al = await alResp.json();
                if ((al.items || []).length) {
                    groups.push({ title: `Alertes (${al.total})`, items: al.items.map(a => ({
                        html: `<strong>#${a.id} ${escapeHtml(a.client_name)}</strong> × ${escapeHtml(a.watchlist_name)} <small style="color: var(--text-muted);">${escapeHtml(statusLabel(a.status))}</small>`,
                        action: () => { switchTab("alerts"); switchSubTab("alerts", a.channel === "FILTERING" ? "alerts-filtering" : "alerts-screening"); openAlertModal(a.id); },
                    })) });
                }
            }
        } catch (e) { /* recherche silencieuse */ }
    }
    renderPaletteResults(groups);
}

function initCommandPalette() {
    const input = document.getElementById("palette-input");
    const overlay = document.getElementById("command-palette");
    if (!input || !overlay) return;
    document.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
            e.preventDefault();
            openCommandPalette();
        }
    });
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeCommandPalette(); });
    input.addEventListener("input", () => {
        clearTimeout(_paletteDebounce);
        _paletteDebounce = setTimeout(() => runPaletteSearch(input.value), 250);
    });
    input.addEventListener("keydown", (e) => {
        const items = document.querySelectorAll("#palette-results .palette-item");
        if (e.key === "Escape") { closeCommandPalette(); return; }
        if (e.key === "ArrowDown") { e.preventDefault(); paletteHover(Math.min(_paletteSelection + 1, items.length - 1)); }
        else if (e.key === "ArrowUp") { e.preventDefault(); paletteHover(Math.max(_paletteSelection - 1, 0)); }
        else if (e.key === "Enter") { e.preventDefault(); paletteActivate(_paletteSelection); }
    });
}

// ------------------ CAMPAGNES DE CRIBLAGE BATCH (serveur + inbox CFT) ------------------

let _batchPollTimer = null;

function campaignStatusBadge(status) {
    const map = {
        RUNNING: ["var(--color-warning)", "EN COURS"],
        DONE: ["var(--success-soft-text)", "TERMINÉE"],
        ERROR: ["var(--color-alert)", "ERREUR"],
    };
    const [color, label] = map[status] || ["var(--text-muted)", status];
    return `<span style="color: ${color}; font-weight: 700; font-size: 0.78rem;">${label}</span>`;
}

async function fetchBatchCampaigns() {
    const tbody = document.querySelector("#batch-campaigns-table tbody");
    if (!tbody) return;
    try {
        const response = await apiFetch("/api/batch/campaigns", { silent: true });
        if (!response.ok) return;
        const data = await response.json();
        const items = data.items || [];
        if (!items.length) {
            tableEmpty(tbody, 9, "Aucune campagne : lancez-en une avec un fichier CSV, ou déposez un fichier dans l'inbox CFT.", "🗂");
        } else {
            tbody.innerHTML = items.map(c => `
                <tr>
                    <td>#${c.id}</td>
                    <td><strong>${escapeHtml(c.name)}</strong>${c.file_name ? `<br><small style="color: var(--text-muted);">${escapeHtml(c.file_name)}</small>` : ""}</td>
                    <td>${c.trigger === "inbox" ? '<span class="badge-secondary">📥 CFT</span>' : '<span class="badge-secondary">Manuel</span>'}</td>
                    <td>${campaignStatusBadge(c.status)}${c.error_message ? `<br><small style="color: var(--color-alert);">${escapeHtml(c.error_message)}</small>` : ""}</td>
                    <td>${c.processed_clients} / ${c.total_clients}</td>
                    <td><strong style="color: ${c.alert_count ? "var(--color-alert)" : "var(--text-muted)"};">${c.alert_count}</strong></td>
                    <td>${c.rejected_count || 0}</td>
                    <td>${formatDateTime(c.created_at)}</td>
                    <td>
                        <button class="btn btn-sm btn-secondary" onclick="openBatchCampaign(${c.id})">🔎 Détails</button>
                        <button class="btn btn-sm" style="background: var(--surface-3);" onclick="window.open('/api/export/batch/${c.id}.csv', '_blank')">⬇ CSV</button>
                    </td>
                </tr>`).join("");
        }
        // Rafraîchissement automatique tant qu'une campagne tourne et que l'onglet est visible
        const anyRunning = items.some(c => c.status === "RUNNING");
        clearTimeout(_batchPollTimer);
        const batchVisible = document.getElementById("sub-sec-screening-batch")?.classList.contains("active");
        if (anyRunning && batchVisible) {
            _batchPollTimer = setTimeout(fetchBatchCampaigns, 4000);
        }
    } catch (e) { /* silencieux */ }
}

async function launchBatchCampaign() {
    const input = document.getElementById("batch-file-input");
    if (!input || !input.files || !input.files.length) {
        showToast("Sélectionnez un fichier CSV de clients.", "error");
        return;
    }
    const formData = new FormData();
    formData.append("file", input.files[0]);
    const nameEl = document.getElementById("batch-campaign-name");
    if (nameEl && nameEl.value.trim()) formData.append("name", nameEl.value.trim());
    try {
        const response = await apiFetch("/api/batch/campaigns", { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (typeof data.detail === "string" ? data.detail : "fichier refusé."), "error");
            return;
        }
        showToast(data.message, "success");
        input.value = "";
        if (nameEl) nameEl.value = "";
        fetchBatchCampaigns();
    } catch (e) { console.error("Batch campaign launch error:", e); }
}

async function openBatchCampaign(campaignId, statusFilter = "") {
    const container = document.getElementById("batch-campaign-detail");
    if (!container) return;
    container.classList.remove("hidden");
    try {
        const params = new URLSearchParams({ page_size: "200" });
        if (statusFilter) params.set("status", statusFilter);
        const response = await apiFetch(`/api/batch/campaigns/${campaignId}?${params}`);
        if (!response.ok) return;
        const c = await response.json();
        const rows = (c.results || []).map(r => `
            <tr>
                <td>${escapeHtml(r.client_id || "—")}</td>
                <td><strong>${escapeHtml(r.client_name || "—")}</strong></td>
                <td>${escapeHtml(statusLabel(r.status))}</td>
                <td>${r.final_score !== null && r.final_score !== undefined ? r.final_score.toFixed(1) + " %" : "—"}</td>
                <td>${r.watchlist_name ? `${escapeHtml(r.watchlist_name)}<br><small style="color: var(--text-muted);">${escapeHtml(r.watchlist_entity_id || "")}</small>` : (r.error ? `<small style="color: var(--color-alert);">${escapeHtml(r.error)}</small>` : "—")}</td>
                <td>${r.list_type ? listTypeBadge(r.list_type) : "—"}</td>
                <td>${r.alert_id ? `<button class="btn btn-sm btn-secondary" onclick="switchTab('alerts'); switchSubTab('alerts', 'alerts-screening'); openAlertModal(${r.alert_id})">🚨 Alerte #${r.alert_id}</button>` : "—"}</td>
            </tr>`).join("");
        container.innerHTML = `
            <h3 style="font-size: 1rem; margin-bottom: 0.5rem;">Campagne #${c.id} — ${escapeHtml(c.name)} ${campaignStatusBadge(c.status)}</h3>
            <p class="section-desc" style="margin-bottom: 0.75rem;">
                ${c.processed_clients}/${c.total_clients} client(s) criblé(s) ·
                <strong style="color: var(--color-alert);">${c.alert_count} alerte(s)</strong> ·
                ${c.no_match_count} sans match · ${c.rejected_count} rejet(s) quality gate
            </p>
            <div class="filter-bar" style="margin-bottom: 0.5rem;">
                <button class="btn btn-sm ${statusFilter === "" ? "btn-secondary" : ""}" style="${statusFilter === "" ? "" : "background: var(--surface-3);"}" onclick="openBatchCampaign(${c.id}, '')">Tous (${c.results_total})</button>
                <button class="btn btn-sm ${statusFilter === "ALERT" ? "btn-secondary" : ""}" style="${statusFilter === "ALERT" ? "" : "background: var(--surface-3);"}" onclick="openBatchCampaign(${c.id}, 'ALERT')">Alertes</button>
                <button class="btn btn-sm ${statusFilter === "REJECTED" ? "btn-secondary" : ""}" style="${statusFilter === "REJECTED" ? "" : "background: var(--surface-3);"}" onclick="openBatchCampaign(${c.id}, 'REJECTED')">Rejets</button>
                <button class="btn btn-sm" style="background: var(--surface-3); margin-left: auto;" onclick="window.open('/api/export/batch/${c.id}.csv', '_blank')">⬇ Export CSV</button>
            </div>
            <div class="table-container max-height-table">
                <table>
                    <thead><tr><th>ID Client</th><th>Client</th><th>Statut</th><th>Score</th><th>Fiche listée / motif</th><th>Liste</th><th>Alerte</th></tr></thead>
                    <tbody>${rows || '<tr><td colspan="7" class="empty-state">Aucun résultat pour ce filtre.</td></tr>'}</tbody>
                </table>
            </div>`;
    } catch (e) { console.error("Batch campaign detail error:", e); }
}

// ------------------ GRAPHE DES RELATIONS (rendu SVG radial natif) ------------------

let currentGraphCenter = null;

function _graphNodeColor(entityType) {
    return entityType === "I" ? "var(--color-secondary)" : "var(--color-accent)";
}

async function openRelationGraph(entityId) {
    if (!entityId) return;
    currentGraphCenter = entityId;
    const depth = document.getElementById("graph-depth")?.value || "2";
    try {
        const response = await apiFetch(`/api/relationships/graph/${encodeURIComponent(entityId)}?depth=${depth}`);
        if (!response.ok) return;
        const data = await response.json();
        renderRelationGraph(data);
        document.getElementById("graph-modal").classList.remove("hidden");
    } catch (e) { console.error("Graph load error:", e); }
}

function renderRelationGraph(data) {
    const container = document.getElementById("graph-svg-container");
    const title = document.getElementById("graph-modal-title");
    const truncatedHint = document.getElementById("graph-truncated-hint");
    if (!container) return;
    const centerNode = (data.nodes || []).find(n => n.id === data.center);
    if (title) title.textContent = `🕸 Graphe des relations — ${centerNode ? centerNode.name : data.center}`;
    if (truncatedHint) truncatedHint.classList.toggle("hidden", !data.truncated);

    const W = 880, H = 560, CX = W / 2, CY = H / 2;
    const rings = [0, 150, 250, 330];

    // Positions : centre + anneaux par profondeur (répartition angulaire)
    const byDepth = {};
    (data.nodes || []).forEach(n => { (byDepth[n.depth] = byDepth[n.depth] || []).push(n); });
    const positions = {};
    Object.entries(byDepth).forEach(([nodeDepth, nodes]) => {
        const d = parseInt(nodeDepth, 10);
        if (d === 0) { positions[nodes[0].id] = { x: CX, y: CY }; return; }
        const radius = rings[Math.min(d, rings.length - 1)];
        nodes.forEach((node, i) => {
            const angle = (2 * Math.PI * i) / nodes.length - Math.PI / 2 + (d % 2 ? 0 : Math.PI / nodes.length);
            positions[node.id] = { x: CX + radius * Math.cos(angle), y: CY + radius * Math.sin(angle) };
        });
    });

    // Arêtes (dessinées sous les nœuds) : rouge = détention majoritaire (50 %)
    const edgesSvg = (data.edges || []).map(edge => {
        const from = positions[edge.from], to = positions[edge.to];
        if (!from || !to) return "";
        const color = edge.majority ? "var(--color-alert)" : "var(--color-primary)";
        const midX = (from.x + to.x) / 2, midY = (from.y + to.y) / 2;
        const label = edge.ownership_pct !== null && edge.ownership_pct !== undefined
            ? `${edge.ownership_pct} %` : (edge.majority ? "≥50 % (présumé)" : edge.label);
        return `
            <line x1="${from.x.toFixed(1)}" y1="${from.y.toFixed(1)}" x2="${to.x.toFixed(1)}" y2="${to.y.toFixed(1)}"
                  stroke="${color}" stroke-width="${edge.majority ? 2.6 : 1.4}" opacity="0.75"
                  marker-end="url(#graph-arrow${edge.majority ? "-red" : ""})"/>
            <text x="${midX.toFixed(1)}" y="${(midY - 5).toFixed(1)}" font-size="8.5" text-anchor="middle"
                  style="fill: ${edge.majority ? "var(--color-alert)" : "var(--text-muted)"};">
                <title>${escapeHtml(edge.label)}${edge.source ? " · " + escapeHtml(edge.source) : ""}</title>${escapeHtml(label)}
            </text>`;
    }).join("");

    // Nœuds : centre plus gros, clic = recentrage
    const nodesSvg = (data.nodes || []).map(node => {
        const pos = positions[node.id];
        if (!pos) return "";
        const isCenter = node.id === data.center;
        const radius = isCenter ? 24 : 15;
        const name = node.name || node.id;
        const shortName = name.length > 22 ? name.slice(0, 21) + "…" : name;
        return `
            <g style="cursor: ${isCenter ? "default" : "pointer"};"
               ${isCenter ? "" : `onclick="openRelationGraph('${escapeHtml(node.id)}')"`}>
                <circle cx="${pos.x.toFixed(1)}" cy="${pos.y.toFixed(1)}" r="${radius}"
                        fill="${_graphNodeColor(node.entity_type)}" opacity="${isCenter ? 1 : 0.85}"
                        stroke="${isCenter ? "var(--text-primary)" : "transparent"}" stroke-width="2.5"/>
                <text x="${pos.x.toFixed(1)}" y="${(pos.y + radius + 12).toFixed(1)}" font-size="9.5"
                      text-anchor="middle" font-weight="${isCenter ? 700 : 500}"
                      style="fill: var(--text-primary);">
                    <title>${escapeHtml(name)} (${escapeHtml(node.id)})</title>${escapeHtml(shortName)}
                </text>
            </g>`;
    }).join("");

    container.innerHTML = `
        <svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Graphe des relations">
            <defs>
                <marker id="graph-arrow" viewBox="0 0 8 8" refX="16" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                    <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--color-primary)" opacity="0.75"/>
                </marker>
                <marker id="graph-arrow-red" viewBox="0 0 8 8" refX="16" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                    <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--color-alert)"/>
                </marker>
            </defs>
            ${edgesSvg}
            ${nodesSvg}
        </svg>`;
}

// ------------------ CLÉS D'API TECHNIQUES (comptes de service) ------------------

async function fetchApiKeys() {
    const tbody = document.querySelector("#apikeys-table tbody");
    if (!tbody) return;
    try {
        const response = await apiFetch("/api/apikeys", { silent: true });
        if (!response.ok) return;
        const items = (await response.json()).items || [];
        if (!items.length) {
            tableEmpty(tbody, 7, "Aucune clé d'API. Créez-en une pour vos intégrations systèmes.", "🔑");
            return;
        }
        tbody.innerHTML = items.map(k => `
            <tr>
                <td><strong>${escapeHtml(k.name)}</strong></td>
                <td><code>${escapeHtml(k.prefix)}…</code></td>
                <td>${escapeHtml(k.roles)}</td>
                <td>${formatDate(k.created_at)}<br><small style="color: var(--text-muted);">par @${escapeHtml(k.created_by || "?")}</small></td>
                <td>${k.last_used_at ? formatDateTime(k.last_used_at) : "jamais"}</td>
                <td>${k.active
                    ? '<span style="color: var(--color-safe); font-weight: 700; font-size: 0.78rem;">ACTIVE</span>'
                    : `<span style="color: var(--text-muted); font-weight: 700; font-size: 0.78rem;">RÉVOQUÉE</span><br><small style="color: var(--text-muted);">${formatDate(k.revoked_at)}</small>`}</td>
                <td>${k.active ? `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text);" onclick="revokeApiKey(${k.id}, '${escapeHtml(k.name)}')">Révoquer</button>` : ""}</td>
            </tr>`).join("");
    } catch (e) { /* silencieux */ }
}

async function createApiKey() {
    const nameEl = document.getElementById("apikey-name");
    const roleEl = document.getElementById("apikey-role");
    const name = (nameEl?.value || "").trim();
    if (!name) { showToast("Donnez un nom à la clé (ex. « CFT production »).", "error"); return; }
    try {
        const response = await apiFetch("/api/apikeys", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, role: roleEl?.value || "user" }),
        });
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "création refusée."), "error"); return; }
        if (nameEl) nameEl.value = "";
        fetchApiKeys();
        // La clé complète n'est montrée qu'ICI, une seule fois
        await _openAppDialog({
            title: "🔑 Clé créée — copiez-la maintenant",
            message: `Cette clé ne sera PLUS JAMAIS affichée. Transmettez-la au système appelant (en-tête X-API-Key) :\n\n${data.api_key}`,
            confirmLabel: "J'ai copié la clé", cancelLabel: "Fermer",
        });
    } catch (e) { console.error("API key create error:", e); }
}

async function revokeApiKey(keyId, name) {
    if (!await confirmDialog(`Révoquer la clé « ${name} » ? Les appels du système porteur échoueront immédiatement.`, { danger: true })) return;
    try {
        const response = await apiFetch(`/api/apikeys/${keyId}/revoke`, { method: "POST" });
        const data = await response.json();
        if (!response.ok) { showToast("Erreur : " + (data.detail || "révocation refusée."), "error"); return; }
        showToast(data.message, "success");
        fetchApiKeys();
    } catch (e) { console.error("API key revoke error:", e); }
}

// ------------------ NAVIGATION PAR URL (deep links #onglet/sous-onglet) ------------------

let _applyingHashRoute = false;

function updateLocationHash(tabId, subTabId) {
    if (_applyingHashRoute) return;
    const target = subTabId ? `#${tabId}/${subTabId}` : `#${tabId}`;
    if (location.hash !== target) {
        history.pushState(null, "", target);
    }
}

function applyHashRoute() {
    const raw = (location.hash || "").replace(/^#/, "");
    if (!raw) return false;
    const [tabId, subTabId] = raw.split("/");
    const section = document.getElementById(`sec-${tabId}`);
    if (!section) return false;
    _applyingHashRoute = true;
    try {
        switchTab(tabId);
        if (subTabId && document.getElementById(`sub-sec-${subTabId}`)) {
            switchSubTab(tabId, subTabId);
        }
    } finally {
        _applyingHashRoute = false;
    }
    return true;
}

function initHashRouting() {
    window.addEventListener("hashchange", applyHashRoute);
    window.addEventListener("popstate", applyHashRoute);
    applyHashRoute(); // route initiale si l'URL porte déjà un ancrage
}

// ------------------ CENTRE DE NOTIFICATIONS (🔔) ------------------

let _lastCounters = {};

function renderNotifCenter() {
    const badge = document.getElementById("bell-badge");
    const list = document.getElementById("notif-list");
    const c = _lastCounters;
    const totalTodo = (c.open_alerts || 0) + (c.pending_reviews || 0);
    if (badge) {
        badge.textContent = totalTodo > 99 ? "99+" : String(totalTodo);
        badge.classList.toggle("hidden", !totalTodo);
    }
    if (!list) return;
    const entries = [];
    if (c.open_alerts_screening) entries.push({ icon: "🚨", label: `${c.open_alerts_screening} alerte(s) de criblage ouverte(s)`, hash: "#alerts/alerts-screening" });
    if (c.open_alerts_filtering) entries.push({ icon: "💸", label: `${c.open_alerts_filtering} alerte(s) de filtrage ouverte(s)`, hash: "#alerts/alerts-filtering" });
    if (c.pending_validation) entries.push({ icon: "👁", label: `${c.pending_validation} décision(s) en attente de validation 4-yeux`, hash: "#alerts/alerts-screening" });
    if (c.overdue_alerts) entries.push({ icon: "⏰", label: `${c.overdue_alerts} alerte(s) en retard SLA`, hash: "#alerts/alerts-screening" });
    if (c.pending_reviews) entries.push({ icon: "📥", label: `${c.pending_reviews} snapshot(s) en attente d'homologation`, hash: "#watchlist-mgmt/watchlist-review" });
    list.innerHTML = entries.length
        ? entries.map(e => `<li onclick="location.hash='${e.hash}'; toggleNotifCenter(false);">
                <span class="item-main">${e.icon} ${escapeHtml(e.label)}</span><span class="item-meta">→</span>
            </li>`).join("")
        : '<li style="cursor: default;"><span class="item-main" style="color: var(--text-muted);">✅ Rien à traiter.</span></li>';
}

function toggleNotifCenter(force) {
    const panel = document.getElementById("notif-panel");
    if (!panel) return;
    const open = force !== undefined ? force : panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !open);
    if (open) renderNotifCenter();
}

document.addEventListener("click", (e) => {
    const panel = document.getElementById("notif-panel");
    if (panel && !panel.classList.contains("hidden")
        && !e.target.closest("#notif-panel") && !e.target.closest("#bell-btn")) {
        panel.classList.add("hidden");
    }
});

// ------------------ ZONES DE DÉPÔT (drag & drop des fichiers) ------------------

function makeDropZone(inputId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const zone = input.closest(".card") || input.parentElement;
    if (!zone || zone._dropWired) return;
    zone._dropWired = true;
    ["dragenter", "dragover"].forEach(eventName => zone.addEventListener(eventName, (e) => {
        e.preventDefault();
        zone.classList.add("drop-hover");
    }));
    ["dragleave", "drop"].forEach(eventName => zone.addEventListener(eventName, (e) => {
        e.preventDefault();
        zone.classList.remove("drop-hover");
    }));
    zone.addEventListener("drop", (e) => {
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
            input.files = e.dataTransfer.files;
            showToast(`Fichier « ${e.dataTransfer.files[0].name} » prêt : validez pour lancer.`, "info");
        }
    });
}

function initDropZones() {
    ["ingest-file", "batch-file-input", "txn-file-input"].forEach(makeDropZone);
}

// ------------------ VUE CLIENT 360° ------------------

async function openClient360(clientId) {
    if (!clientId) return;
    const modal = document.getElementById("client360-modal");
    const body = document.getElementById("client360-body");
    const title = document.getElementById("client360-title");
    if (!modal || !body) return;
    body.innerHTML = '<p class="section-desc">Chargement…</p>';
    modal.classList.remove("hidden");
    try {
        const response = await apiFetch(`/api/clients/${encodeURIComponent(clientId)}/overview`);
        if (!response.ok) { body.innerHTML = '<p class="section-desc">Client introuvable.</p>'; return; }
        const d = await response.json();
        const k = d.kyc;
        if (title) title.textContent = `👤 Vue client 360° — ${k ? (k.company_name || `${k.first_name || ""} ${k.last_name || ""}`.trim()) : clientId}`;

        const kycHtml = k ? `
            <div class="details-grid" style="margin-bottom: 1rem;">
                <div class="details-item"><strong>Identifiant</strong><span>${escapeHtml(clientId)}</span></div>
                <div class="details-item"><strong>Type</strong><span>${k.client_type === "PP" ? "Personne physique" : "Personne morale"}</span></div>
                <div class="details-item"><strong>Naissance</strong><span>${escapeHtml(k.dob || "—")}</span></div>
                <div class="details-item"><strong>Pays</strong><span>${escapeHtml(((k.countries || {}).nationality || []).join(", ") || k.country || "—")}</span></div>
                <div class="details-item"><strong>IBAN / BIC</strong><span>${escapeHtml(k.iban || "—")} ${k.bic ? "· " + escapeHtml(k.bic) : ""}</span></div>
                <div class="details-item"><strong>Notation de risque</strong><span>${escapeHtml(k.risk_rating || "—")}${k.pep_flag ? ' · <strong style="color: var(--color-warning);">PEP</strong>' : ""}</span></div>
                <div class="details-item"><strong>Segment / Secteur</strong><span>${escapeHtml(k.segment || "—")} ${k.activity_sector ? "· " + escapeHtml(k.activity_sector) : ""}</span></div>
                <div class="details-item"><strong>Entrée en relation</strong><span>${escapeHtml(k.relationship_start || "—")}${k.status ? " · " + escapeHtml(k.status) : ""}</span></div>
            </div>`
            : `<p class="section-desc">Aucune fiche KYC en production pour <strong>${escapeHtml(clientId)}</strong> (client ad hoc ou hors référentiel).</p>`;

        const screeningsHtml = (d.screenings || []).slice(0, 15).map(s => `
            <tr>
                <td>${formatDateTime(s.timestamp)}</td>
                <td>${escapeHtml(s.watchlist_name || "—")}</td>
                <td>${s.list_type ? listTypeBadge(s.list_type) : "—"}</td>
                <td>${s.final_score !== null && s.final_score !== undefined ? s.final_score.toFixed(1) + " %" : "—"}</td>
                <td>${escapeHtml(statusLabel(s.status))}</td>
            </tr>`).join("");

        const alertsHtml = (d.alerts || []).slice(0, 15).map(a => `
            <tr>
                <td>#${a.id}</td><td>${formatDateTime(a.created_at)}</td>
                <td>${escapeHtml(a.watchlist_name)}</td>
                <td>${alertPriorityBadge(a)}</td>
                <td>${escapeHtml(statusLabel(a.status))}</td>
                <td><button class="btn btn-sm btn-secondary" onclick="document.getElementById('client360-modal').classList.add('hidden'); switchTab('alerts'); openAlertModal(${a.id})">🔎</button></td>
            </tr>`).join("");

        const pairsHtml = (d.whitelist_pairs || []).map(p => `
            <li style="font-size: 0.83rem;">🛡️ ${escapeHtml(p.watchlist_name || p.watchlist_entity_id)} — ${escapeHtml(p.state)}${p.expires_at ? " · expire " + formatDate(p.expires_at) : ""}</li>`).join("");

        body.innerHTML = `
            ${kycHtml}
            <div class="modal-section">
                <h4>Criblages (${d.counts.screenings})</h4>
                ${screeningsHtml ? `<div class="table-container" style="max-height: 190px;"><table>
                    <thead><tr><th>Date</th><th>Fiche matchée</th><th>Liste</th><th>Score</th><th>Décision</th></tr></thead>
                    <tbody>${screeningsHtml}</tbody></table></div>`
                    : '<p style="font-size: 0.85rem; color: var(--text-muted);">Jamais criblé.</p>'}
            </div>
            <div class="modal-section">
                <h4>Alertes (${d.counts.alerts})</h4>
                ${alertsHtml ? `<div class="table-container" style="max-height: 190px;"><table>
                    <thead><tr><th>#</th><th>Date</th><th>Listé</th><th>Priorité</th><th>Statut</th><th></th></tr></thead>
                    <tbody>${alertsHtml}</tbody></table></div>`
                    : '<p style="font-size: 0.85rem; color: var(--text-muted);">Aucune alerte.</p>'}
            </div>
            <div class="modal-section">
                <h4>Liste blanche (${d.counts.whitelist_pairs})</h4>
                ${pairsHtml ? `<ul style="list-style: none;">${pairsHtml}</ul>`
                    : '<p style="font-size: 0.85rem; color: var(--text-muted);">Aucune paire.</p>'}
            </div>`;
    } catch (e) { console.error("Client 360 error:", e); }
}

// =========================================================================
// LOT OPERATIONS : ACTIONS EN MASSE + MFA TOTP
// =========================================================================

// --- Sélection multiple dans les files d'alertes ---
const selectedAlertsByChannel = { SCREENING: new Set(), FILTERING: new Set() };

function updateBulkBar(channel) {
    const prefix = channel === "FILTERING" ? "filtering" : "screening";
    const bar = document.getElementById(`${prefix}-bulk-bar`);
    const count = document.getElementById(`${prefix}-bulk-count`);
    const selected = selectedAlertsByChannel[channel];
    if (!bar) return;
    bar.classList.toggle("hidden", selected.size === 0);
    if (count) count.textContent = `${selected.size} sélectionnée(s)`;
}

function toggleAlertSelection(channel, alertId, checked) {
    const selected = selectedAlertsByChannel[channel];
    if (checked) selected.add(alertId); else selected.delete(alertId);
    updateBulkBar(channel);
}

function toggleSelectAllAlerts(channel, checked) {
    const conf = ALERT_CHANNEL_CONF[channel];
    const selected = selectedAlertsByChannel[channel];
    document.querySelectorAll(`#${conf.table} tbody .alert-select`).forEach(box => {
        box.checked = checked;
        const id = parseInt(box.dataset.alertId, 10);
        if (checked) selected.add(id); else selected.delete(id);
    });
    updateBulkBar(channel);
}

function clearAlertSelection(channel, uncheckBoxes = true) {
    selectedAlertsByChannel[channel].clear();
    const prefix = channel === "FILTERING" ? "filtering" : "screening";
    const selectAll = document.getElementById(`${prefix}-select-all`);
    if (selectAll) selectAll.checked = false;
    if (uncheckBoxes) {
        const conf = ALERT_CHANNEL_CONF[channel];
        document.querySelectorAll(`#${conf.table} tbody .alert-select`).forEach(b => { b.checked = false; });
    }
    updateBulkBar(channel);
}

async function runBulkAlertAction(channel, payload, confirmMsg) {
    const ids = Array.from(selectedAlertsByChannel[channel]);
    if (!ids.length) { showToast("Aucune alerte sélectionnée.", "error"); return; }
    const ok = await confirmDialog(confirmMsg.replace("{n}", ids.length));
    if (!ok) return;
    try {
        const response = await apiFetch("/api/alerts/bulk", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids, ...payload }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Action en masse refusée."), "error");
            return;
        }
        showToast(data.message || "Action en masse effectuée.", "success");
        clearAlertSelection(channel, false);
        fetchAlerts(channel);
        refreshSidebarCounters();
    } catch (e) { console.error("Bulk action error:", e); }
}

function bulkAssignSelected(channel) {
    runBulkAlertAction(channel, { action: "assign" },
        "S'assigner les {n} alerte(s) sélectionnée(s) ?");
}

function bulkPrioritySelected(channel) {
    const prefix = channel === "FILTERING" ? "filtering" : "screening";
    const priority = document.getElementById(`${prefix}-bulk-priority`)?.value;
    if (!priority) { showToast("Choisissez d'abord une priorité.", "error"); return; }
    runBulkAlertAction(channel, { action: "priority", priority },
        `Passer les {n} alerte(s) sélectionnée(s) en priorité ${priority} (échéance SLA recalculée) ?`);
}

// --- MFA TOTP (carte Paramètres + réinitialisation admin) ---
async function refreshMfaCard() {
    const statusEl = document.getElementById("mfa-status");
    const actionsEl = document.getElementById("mfa-actions");
    if (!statusEl || !actionsEl) return;
    try {
        const response = await apiFetch("/api/auth/me", { silent: true });
        if (!response.ok) return;
        const me = (await response.json()).user || {};
        const enabled = !!me.totp_enabled;
        statusEl.innerHTML = enabled
            ? `<span style="color: var(--success-soft-text); font-weight: 700;">🛡 MFA active</span> — un code est demandé à chaque connexion.`
            : `<span style="color: var(--color-warning); font-weight: 700;">MFA inactive</span> — la connexion repose sur le seul mot de passe.`;
        actionsEl.innerHTML = enabled
            ? `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: var(--danger-soft-text);" onclick="disableTotp()">Désactiver la MFA…</button>`
            : `<button class="btn btn-sm btn-primary" onclick="startTotpSetup()">Activer la MFA…</button>`;
        const setupZone = document.getElementById("mfa-setup-zone");
        if (setupZone && enabled) setupZone.classList.add("hidden");
    } catch (e) { console.error("MFA card error:", e); }
}

async function startTotpSetup() {
    try {
        const response = await apiFetch("/api/auth/totp/setup", { method: "POST" });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Impossible de démarrer l'enrôlement."), "error");
            return;
        }
        document.getElementById("mfa-secret").textContent = data.secret;
        document.getElementById("mfa-uri").textContent = data.otpauth_uri;
        document.getElementById("mfa-confirm-code").value = "";
        document.getElementById("mfa-setup-zone").classList.remove("hidden");
        document.getElementById("mfa-confirm-code").focus();
    } catch (e) { console.error("TOTP setup error:", e); }
}

async function confirmTotp() {
    const code = (document.getElementById("mfa-confirm-code")?.value || "").trim();
    if (!code) { showToast("Saisissez le code affiché par l'application.", "error"); return; }
    try {
        const response = await apiFetch("/api/auth/totp/confirm", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Code refusé."), "error");
            return;
        }
        showToast(data.message || "MFA activée.", "success");
        document.getElementById("mfa-setup-zone").classList.add("hidden");
        refreshMfaCard();
    } catch (e) { console.error("TOTP confirm error:", e); }
}

async function disableTotp() {
    const password = await promptDialog("Mot de passe requis pour désactiver la MFA :",
                                        { password: true, placeholder: "Mot de passe" });
    if (!password) return;
    try {
        const response = await apiFetch("/api/auth/totp/disable", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Désactivation refusée."), "error");
            return;
        }
        showToast(data.message || "MFA désactivée.", "success");
        refreshMfaCard();
    } catch (e) { console.error("TOTP disable error:", e); }
}

async function resetUserTotp(userId, username) {
    const ok = await confirmDialog(
        `Réinitialiser la MFA de @${username} ? Le compte se reconnectera au mot de passe seul et pourra ré-enrôler un téléphone.`);
    if (!ok) return;
    try {
        const response = await apiFetch(`/api/users/${userId}/totp/reset`, { method: "POST" });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Réinitialisation refusée."), "error");
            return;
        }
        showToast(data.message || "MFA réinitialisée.", "success");
        fetchUsersList();
    } catch (e) { console.error("TOTP reset error:", e); }
}

// =========================================================================
// LOT GOUVERNANCE : RETENTION + VUES SAUVEGARDEES + RAPPORT D'ACTIVITE
// =========================================================================

// --- Rétention des données (admin) ---
async function fetchRetentionSettings() {
    const card = document.getElementById("retention-card");
    if (!card || card.classList.contains("hidden")) return;
    try {
        const response = await apiFetch("/api/admin/retention", { silent: true });
        if (!response.ok) return;
        const data = await response.json();
        const policy = data.policy || {};
        const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        setVal("retention-audit", policy.audit_trail ?? 0);
        setVal("retention-alerts", policy.closed_alerts ?? 0);
        setVal("retention-syncs", policy.sync_reports ?? 0);
        setVal("retention-batch", policy.batch_campaigns ?? 0);
        setVal("retention-cron", policy.cron || "30 2 * * *");
        const archiveEl = document.getElementById("retention-archive");
        if (archiveEl) archiveEl.checked = policy.archive !== false;
        renderRetentionPreview(data.preview || {});
    } catch (e) { console.error("Retention fetch error:", e); }
}

function renderRetentionPreview(preview) {
    const el = document.getElementById("retention-preview");
    if (!el) return;
    const labels = { audit_trail: "décisions de criblage", closed_alerts: "alertes clôturées",
                     sync_reports: "rapports de sync", batch_campaigns: "campagnes batch" };
    const parts = Object.entries(preview).filter(([, n]) => n > 0)
        .map(([family, n]) => `${n} ${labels[family] || family}`);
    el.textContent = parts.length
        ? `Purge aujourd'hui avec cette politique : ${parts.join(", ")}.`
        : "Rien à purger avec la politique actuelle.";
}

async function saveRetentionSettings() {
    const val = (id) => parseInt(document.getElementById(id)?.value, 10) || 0;
    const payload = {
        audit_trail: val("retention-audit"),
        closed_alerts: val("retention-alerts"),
        sync_reports: val("retention-syncs"),
        batch_campaigns: val("retention-batch"),
        cron: (document.getElementById("retention-cron")?.value || "").trim(),
        archive: !!document.getElementById("retention-archive")?.checked,
    };
    try {
        const response = await apiFetch("/api/settings/retention", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Réglage refusé."), "error");
            return;
        }
        showToast(data.message || "Politique de rétention mise à jour.", "success");
        renderRetentionPreview(data.preview || {});
    } catch (e) { console.error("Retention save error:", e); }
}

async function runRetentionNow() {
    const ok = await confirmDialog(
        "Exécuter la purge de rétention maintenant ? Les enregistrements au-delà des durées configurées seront définitivement supprimés (action tracée au journal).");
    if (!ok) return;
    try {
        const response = await apiFetch("/api/admin/retention/run", { method: "POST" });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Purge refusée."), "error");
            return;
        }
        showToast(data.message || "Purge effectuée.", "success");
        fetchRetentionSettings();
    } catch (e) { console.error("Retention run error:", e); }
}

// --- Vues sauvegardées des files d'alertes ---
let savedViewsByChannel = { SCREENING: [], FILTERING: [] };

function _viewsSelectId(channel) { return channel === "FILTERING" ? "filtering-views" : "screening-views"; }

async function fetchSavedViews(channel) {
    const select = document.getElementById(_viewsSelectId(channel));
    if (!select) return;
    try {
        const response = await apiFetch(`/api/views?channel=${channel}`, { silent: true });
        if (!response.ok) return;
        const items = (await response.json()).items || [];
        savedViewsByChannel[channel] = items;
        select.innerHTML = `<option value="">Vues…</option>` +
            items.map(v => `<option value="${v.id}">${escapeHtml(v.name)}</option>`).join("") +
            (items.length ? `<option value="__delete__">🗑 Supprimer une vue…</option>` : "");
    } catch (e) { console.error("Saved views fetch error:", e); }
}

async function applySavedView(channel, value) {
    const select = document.getElementById(_viewsSelectId(channel));
    if (!value) return;
    if (value === "__delete__") {
        select.value = "";
        const views = savedViewsByChannel[channel];
        const name = await promptDialog("Nom exact de la vue à supprimer :", {
            placeholder: views.map(v => v.name).join(", ") });
        if (!name) return;
        const view = views.find(v => v.name === name.trim());
        if (!view) { showToast("Vue introuvable.", "error"); return; }
        const response = await apiFetch(`/api/views/${view.id}`, { method: "DELETE" });
        const data = await response.json();
        showToast(data.message || data.detail || "Vue supprimée.", response.ok ? "success" : "error");
        fetchSavedViews(channel);
        return;
    }
    const view = savedViewsByChannel[channel].find(v => String(v.id) === String(value));
    if (!view) return;
    const filters = view.filters || {};
    const conf = ALERT_CHANNEL_CONF[channel];
    alertsFilterByChannel[channel] = filters.status || "";
    const prioEl = document.getElementById(conf.priorityFilter);
    if (prioEl) prioEl.value = filters.priority || "";
    const listEl = document.getElementById(conf.listFilter);
    if (listEl) listEl.value = filters.list_type || "";
    // Synchronise l'état visuel des boutons de statut avec le filtre restauré
    const section = document.getElementById(`sub-sec-${conf.section}`);
    if (section) {
        section.querySelectorAll(".alerts-status-filters button").forEach(btn => {
            const active = btn.dataset.filter === (filters.status || "");
            btn.classList.toggle("btn-secondary", active);
            btn.style.background = active ? "" : "var(--surface-3)";
        });
    }
    fetchAlerts(channel, 1);
    showToast(`Vue « ${view.name} » appliquée.`, "success");
}

async function saveCurrentView(channel) {
    const name = await promptDialog("Nom de la vue (les filtres courants seront mémorisés) :",
                                    { placeholder: "Ex. Critiques criblage à traiter" });
    if (!name) return;
    const conf = ALERT_CHANNEL_CONF[channel];
    const filters = {
        status: alertsFilterByChannel[channel] || "",
        priority: document.getElementById(conf.priorityFilter)?.value || "",
        list_type: document.getElementById(conf.listFilter)?.value || "",
    };
    try {
        const response = await apiFetch("/api/views", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim(), channel, filters }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Sauvegarde refusée."), "error");
            return;
        }
        showToast(data.message || "Vue sauvegardée.", "success");
        fetchSavedViews(channel);
    } catch (e) { console.error("Saved view create error:", e); }
}

// --- Rapport d'activité réglementaire (période) ---
function initActivityReportDates() {
    const fromEl = document.getElementById("activity-from");
    const toEl = document.getElementById("activity-to");
    if (!fromEl || !toEl || fromEl.value) return;
    const today = new Date();
    const monthAgo = new Date(today.getTime() - 30 * 86400000);
    toEl.value = today.toISOString().slice(0, 10);
    fromEl.value = monthAgo.toISOString().slice(0, 10);
}

function _activityParams() {
    const params = new URLSearchParams();
    const from = document.getElementById("activity-from")?.value;
    const to = document.getElementById("activity-to")?.value;
    if (from) params.set("date_from", from);
    if (to) params.set("date_to", to);
    return params;
}

async function fetchActivityReport() {
    const tbody = document.querySelector("#activity-report-table tbody");
    if (!tbody) return;
    tableLoading(tbody, 3);
    try {
        const response = await apiFetch(`/api/reports/activity?${_activityParams()}`);
        const report = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (report.detail || "Rapport indisponible."), "error");
            tableEmpty(tbody, 3, "Rapport indisponible.", "⚠️");
            return;
        }
        const rows = [];
        const emit = (section, label, value) =>
            rows.push(`<tr><td>${escapeHtml(section)}</td><td>${escapeHtml(label)}</td><td style="text-align: right; font-weight: 600;">${value ?? "—"}</td></tr>`);
        emit("Criblage", "Décisions totales", report.screenings.total);
        Object.entries(report.screenings.by_status).forEach(([status, count]) =>
            emit("Criblage", `Décisions ${STATUS_LABELS[status] || status}`, count));
        emit("Alertes", "Créées", report.alerts.created);
        Object.entries(report.alerts.created_by_channel).forEach(([channel, count]) =>
            emit("Alertes", `Créées — ${channel === "FILTERING" ? "filtrage" : "criblage"}`, count));
        Object.entries(report.alerts.created_by_priority).forEach(([prio, count]) =>
            emit("Alertes", `Créées priorité ${prio}`, count));
        emit("Alertes", "Décidées", report.alerts.decided);
        Object.entries(report.alerts.decided_by_status).forEach(([status, count]) =>
            emit("Alertes", `Décidées — ${STATUS_LABELS[status] || status}`, count));
        emit("Alertes", "Délai moyen de décision (h)", report.alerts.avg_decision_hours);
        emit("Alertes", "Escalades", report.alerts.escalations);
        emit("Alertes", "Encore ouvertes", report.alerts.still_open);
        emit("Liste blanche", "Paires créées", report.whitelist.created);
        emit("Liste blanche", "Paires révoquées", report.whitelist.revoked);
        emit("Synchronisations", "Total", report.syncs.total);
        Object.entries(report.syncs.by_status).forEach(([status, count]) =>
            emit("Synchronisations", `Statut ${status}`, count));
        emit("Batch", "Campagnes", report.batch.campaigns);
        emit("Batch", "Clients criblés", report.batch.clients_screened);
        tbody.innerHTML = rows.join("");
    } catch (e) {
        console.error("Activity report error:", e);
        tableEmpty(tbody, 3, "Erreur réseau.", "⚠️");
    }
}

function exportActivityCsv() {
    window.open(`/api/reports/activity.csv?${_activityParams()}`, "_blank");
}

function printActivityReport() {
    window.open(`/api/reports/activity/print?${_activityParams()}`, "_blank");
}

// =========================================================================
// LOT PILOTAGE & PORTABILITE : ARCHIVE, CHARGE DE TRAVAIL, CONFIG
// =========================================================================

// --- Charge de travail des analystes (échéances SLA) ---
async function fetchWorkload() {
    const tbody = document.querySelector("#workload-table tbody");
    if (!tbody) return;
    tableLoading(tbody, 9);
    try {
        const channel = document.getElementById("workload-channel")?.value || "";
        const params = channel ? `?channel=${channel}` : "";
        const response = await apiFetch(`/api/alerts/workload${params}`);
        if (!response.ok) { tableEmpty(tbody, 9, "Charge indisponible.", "⚠️"); return; }
        const data = await response.json();
        const totalsEl = document.getElementById("workload-totals");
        if (totalsEl) {
            totalsEl.textContent =
                `${data.totals.open} ouverte(s) · ${data.totals.overdue} en retard · ${data.totals.pending_validation} à valider (4-yeux)`;
        }
        const row = (label, b, muted = false) => `
            <tr${muted ? ' style="opacity: 0.85;"' : ""}>
                <td>${label}</td>
                <td><strong>${b.open_total}</strong></td>
                <td>${b.by_priority.CRITICAL ? `<span style="color: var(--color-alert); font-weight: 700;">${b.by_priority.CRITICAL}</span>` : "—"}</td>
                <td>${b.by_priority.HIGH ? `<span style="color: var(--color-warning); font-weight: 600;">${b.by_priority.HIGH}</span>` : "—"}</td>
                <td>${b.by_priority.MEDIUM || "—"}</td>
                <td>${b.by_priority.LOW || "—"}</td>
                <td>${b.overdue ? `<span style="color: var(--color-alert); font-weight: 700;">⏰ ${b.overdue}</span>` : "—"}</td>
                <td>${b.next_due_at ? formatDateTime(b.next_due_at) : "—"}</td>
                <td>${b.pending_validation || "—"}</td>
            </tr>`;
        const rows = [];
        if (data.unassigned.open_total > 0) {
            rows.push(row(`<em style="color: var(--color-warning);">Non assignées</em>`, data.unassigned, true));
        }
        data.analysts.forEach(a => rows.push(row(`<strong>@${escapeHtml(a.username)}</strong>`, a)));
        if (!rows.length) {
            tableEmpty(tbody, 9, "Aucune alerte ouverte : file à jour.", "✅");
            return;
        }
        tbody.innerHTML = rows.join("");
    } catch (e) {
        console.error("Workload error:", e);
        tableEmpty(tbody, 9, "Erreur réseau.", "⚠️");
    }
}

// --- Portabilité de la configuration (admin) ---
function exportAppConfig() {
    window.open("/api/admin/config/export", "_blank");
}

async function importAppConfig(input) {
    const file = input.files && input.files[0];
    input.value = "";
    if (!file) return;
    const resultEl = document.getElementById("config-import-result");
    let parsed;
    try {
        parsed = JSON.parse(await file.text());
    } catch (e) {
        showToast("Fichier illisible : JSON invalide.", "error");
        return;
    }
    const settings = parsed.settings || parsed;
    if (typeof settings !== "object" || Array.isArray(settings)) {
        showToast("Format inattendu : objet « settings » requis.", "error");
        return;
    }
    const ok = await confirmDialog(
        `Importer ${Object.keys(settings).length} réglage(s) depuis « ${file.name} » ? Les réglages actuels seront remplacés (delta journalisé).`);
    if (!ok) return;
    try {
        const response = await apiFetch("/api/admin/config/import", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ settings }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Import refusé."), "error");
            return;
        }
        showToast(data.message || "Configuration importée.", "success");
        if (resultEl) {
            resultEl.textContent = `Appliqués : ${data.applied.join(", ")}`
                + (data.skipped.length ? ` — ignorés : ${data.skipped.join(", ")}` : "");
        }
        fetchIngestionSettings();
        fetchRetentionSettings();
    } catch (e) { console.error("Config import error:", e); }
}

// =========================================================================
// LOT INTL : ABSENCE/DELEGATION + SEUILS DE SCORE
// =========================================================================

// --- Absence & délégation ---
async function fetchAbsenceCard() {
    const statusEl = document.getElementById("absence-status");
    const delegateSel = document.getElementById("absence-delegate");
    if (!statusEl || !delegateSel) return;
    try {
        const response = await apiFetch("/api/users/directory", { silent: true });
        if (!response.ok) return;
        const items = (await response.json()).items || [];
        const meName = currentUser ? currentUser.username : "";
        delegateSel.innerHTML = `<option value="">Choisir…</option>` + items
            .filter(u => u.username !== meName && !(u.roles || []).includes("auditor"))
            .map(u => `<option value="${escapeHtml(u.username)}">@${escapeHtml(u.username)}${u.full_name ? " — " + escapeHtml(u.full_name) : ""}${u.absent ? " (absent)" : ""}</option>`)
            .join("");
        const me = items.find(u => u.username === meName);
        if (me && me.absent) {
            statusEl.innerHTML = `<span style="color: var(--color-warning); font-weight: 700;">🌴 Absence active</span> — vos alertes vont à <strong>@${escapeHtml(me.delegate_to || "?")}</strong>.`;
            if (me.delegate_to) delegateSel.value = me.delegate_to;
        } else {
            statusEl.innerHTML = `<span style="color: var(--success-soft-text); font-weight: 600;">Présent</span> — aucune délégation active.`;
        }
    } catch (e) { console.error("Absence card error:", e); }
}

async function saveAbsence() {
    const until = document.getElementById("absence-until")?.value;
    const delegate = document.getElementById("absence-delegate")?.value;
    if (!until) { showToast("Indiquez la date de fin d'absence.", "error"); return; }
    if (!delegate) { showToast("Choisissez un délégué.", "error"); return; }
    try {
        const response = await apiFetch("/api/users/me/absence", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                absent_until: until, delegate_to: delegate,
                reassign_open: !!document.getElementById("absence-reassign")?.checked,
            }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Déclaration refusée."), "error");
            return;
        }
        showToast(data.message || "Absence enregistrée.", "success");
        fetchAbsenceCard();
    } catch (e) { console.error("Absence save error:", e); }
}

async function clearAbsence() {
    try {
        const response = await apiFetch("/api/users/me/absence", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ absent_until: null }),
        });
        const data = await response.json();
        showToast(data.message || data.detail || "Absence terminée.", response.ok ? "success" : "error");
        fetchAbsenceCard();
    } catch (e) { console.error("Absence clear error:", e); }
}

// --- Seuils de score à chaud (admin) ---
const SCORING_OVERRIDE_TYPES = ["WATCHLIST_OFAC", "WATCHLIST_EU", "WATCHLIST_UN",
                                "WATCHLIST_DGT", "WATCHLIST_PEP", "WATCHLIST_OFSI", "WATCHLIST_SSIE"];

async function fetchScoringSettings() {
    const card = document.getElementById("scoring-card");
    if (!card || card.classList.contains("hidden")) return;
    try {
        const response = await apiFetch("/api/settings/scoring", { silent: true });
        if (!response.ok) return;
        const data = await response.json();
        const globalEl = document.getElementById("scoring-global");
        if (globalEl) globalEl.value = data.cut_off_threshold;
        const row = document.getElementById("scoring-overrides-row");
        if (row) {
            row.innerHTML = SCORING_OVERRIDE_TYPES.map(t => `
                <div class="form-group">
                    <label for="scoring-ov-${t}">${listTypeLabel(t)}</label>
                    <input type="number" id="scoring-ov-${t}" min="0" max="100" step="0.5"
                           placeholder="global" value="${data.cut_off_overrides[t] ?? ""}">
                </div>`).join("");
        }
    } catch (e) { console.error("Scoring settings error:", e); }
}

async function saveScoringSettings() {
    const overrides = {};
    for (const t of SCORING_OVERRIDE_TYPES) {
        const raw = document.getElementById(`scoring-ov-${t}`)?.value;
        overrides[t] = raw === "" || raw === undefined ? null : parseFloat(raw);
    }
    const payload = {
        cut_off_threshold: parseFloat(document.getElementById("scoring-global")?.value) || 75,
        cut_off_overrides: overrides,
    };
    try {
        const response = await apiFetch("/api/settings/scoring", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Réglage refusé."), "error");
            return;
        }
        showToast(data.message || "Seuils mis à jour.", "success");
        fetchScoringSettings();
    } catch (e) { console.error("Scoring save error:", e); }
}

// =========================================================================
// LOT GO : DOSSIER D'INVESTIGATION + SIMULATION DE SEUILS + CHECKLIST
// =========================================================================

let currentCasefileAlertId = null;

async function openCasefileModal(alertId) {
    currentCasefileAlertId = alertId;
    const modal = document.getElementById("casefile-modal");
    const body = document.getElementById("casefile-body");
    if (!modal || !body) return;
    modal.classList.remove("hidden");
    body.innerHTML = `<p style="color: var(--text-muted);">Chargement…</p>`;
    try {
        const response = await apiFetch(`/api/alerts/${alertId}/casefile`);
        const cf = await response.json();
        if (!response.ok) {
            body.innerHTML = `<p style="color: var(--danger-soft-text);">${escapeHtml(cf.detail || "Dossier indisponible.")}</p>`;
            return;
        }
        document.getElementById("casefile-title").innerHTML =
            `📁 Dossier — Alerte #${cf.id} · ${escapeHtml(cf.client_name)} × ${escapeHtml(cf.watchlist_name)}`;
        renderCasefile(cf);
    } catch (e) { console.error("Casefile error:", e); }
}

function renderCasefile(cf) {
    const body = document.getElementById("casefile-body");
    const doneCount = cf.checklist.filter(i => i.done).length;
    const closed = String(cf.status || "").startsWith("CLOSED");
    const checklistHtml = cf.checklist.map(item => `
        <label style="display: flex; align-items: flex-start; gap: 0.6rem; padding: 0.35rem 0; cursor: ${closed ? "default" : "pointer"};">
            <input type="checkbox" ${item.done ? "checked" : ""} ${closed ? "disabled" : ""}
                   onchange="toggleCasefileCheck(${item.index}, this.checked)" style="margin-top: 3px;">
            <span>${escapeHtml(item.label)}
                ${item.done && item.by ? `<small style="color: var(--text-muted);"> — @${escapeHtml(item.by)} ${item.at ? formatDateTime(item.at) : ""}</small>` : ""}
            </span>
        </label>`).join("");

    const ctx = cf.client_context;
    const contextHtml = ctx
        ? `<p>Criblages antérieurs : <strong>${ctx.screenings}</strong> · autres alertes : <strong>${ctx.other_alerts}</strong> · liste blanche : <strong>${ctx.whitelist_pairs}</strong>
           <button class="btn btn-sm btn-secondary" style="margin-left: 0.5rem;" onclick="openClient360('${escapeHtml(ctx.client_id)}')">👤 Client 360°</button></p>`
        : `<p style="color: var(--text-muted);">Partie de transaction (pas de dossier client).</p>`;

    const inherited = cf.entity_relations.inherited_risk || [];
    const inheritedHtml = inherited.length
        ? `<p style="color: var(--danger-soft-text); font-weight: 600;">⚠ Règle des 50 % : ${inherited.map(r => escapeHtml(r.owner_name || r.owner_id)).join(" ; ")}</p>`
        : "";
    const relationsHtml = `
        <p>${cf.entity_relations.count} relation(s) connue(s) pour cette fiche.
           ${cf.entity_relations.count ? `<button class="btn btn-sm btn-secondary" style="margin-left: 0.5rem;" onclick="openRelationGraph('${escapeHtml(cf.watchlist_entity_id)}')">🕸 Graphe</button>` : ""}
        </p>${inheritedHtml}`;

    const attachmentsHtml = (cf.attachments || []).map(att =>
        `<li>${escapeHtml(att.file_name)} <small style="color: var(--text-muted);">(@${escapeHtml(att.uploaded_by)})</small></li>`
    ).join("") || `<li style="color: var(--text-muted);">Aucune pièce jointe.</li>`;

    const eventsHtml = (cf.events || []).slice(-15).map(e => `
        <div style="border-left: 2px solid var(--border-color); padding: 0.3rem 0 0.3rem 0.7rem;">
            <small style="color: var(--text-muted);">${e.timestamp ? formatDateTime(e.timestamp) : ""} — <strong>@${escapeHtml(e.username)}</strong> · ${escapeHtml(e.action)}</small>
            ${e.detail ? `<div style="font-size: 0.83rem;">${escapeHtml(e.detail)}</div>` : ""}
        </div>`).join("");

    body.innerHTML = `
        <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;">
            ${alertPriorityBadge(cf)} ${alertStatusBadge(cf.status)}
            <span style="margin-left: auto;"></span>
            <a class="btn btn-sm btn-secondary" href="/api/alerts/${cf.id}/casefile/print" target="_blank">🖨 Imprimer le dossier</a>
            <button class="btn btn-sm btn-secondary" onclick="document.getElementById('casefile-modal').classList.add('hidden'); openAlertModal(${cf.id});">🔎 Instruire</button>
        </div>
        <div class="c360-section">
            <h4>Checklist d'instruction (${doneCount}/${cf.checklist.length})</h4>
            ${checklistHtml}
        </div>
        <div class="c360-section"><h4>Contexte client</h4>${contextHtml}</div>
        <div class="c360-section"><h4>Relations de la fiche listée</h4>${relationsHtml}</div>
        <div class="c360-section"><h4>Pièces jointes</h4><ul style="list-style: none;">${attachmentsHtml}</ul></div>
        <div class="c360-section"><h4>Dernières actions</h4>${eventsHtml || '<p style="color: var(--text-muted);">Aucune action.</p>'}</div>`;
}

async function toggleCasefileCheck(index, done) {
    if (!currentCasefileAlertId) return;
    try {
        const response = await apiFetch(`/api/alerts/${currentCasefileAlertId}/checklist`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ index, done }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Mise à jour refusée."), "error");
            openCasefileModal(currentCasefileAlertId);
            return;
        }
        showToast(`Checklist : ${data.done}/${data.total}`, "success");
        openCasefileModal(currentCasefileAlertId);
    } catch (e) { console.error("Checklist toggle error:", e); }
}

// --- Checklist d'instruction (réglage admin) ---
async function fetchChecklistSettings() {
    const card = document.getElementById("checklist-card");
    if (!card || card.classList.contains("hidden")) return;
    try {
        const response = await apiFetch("/api/settings/checklist", { silent: true });
        if (!response.ok) return;
        const data = await response.json();
        const box = document.getElementById("checklist-items");
        if (box) box.value = (data.items || []).join("\n");
    } catch (e) { console.error("Checklist settings error:", e); }
}

async function saveChecklistSettings() {
    const raw = document.getElementById("checklist-items")?.value || "";
    const items = raw.split("\n").map(l => l.trim()).filter(Boolean);
    try {
        const response = await apiFetch("/api/settings/checklist", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items }),
        });
        const data = await response.json();
        if (!response.ok) {
            showToast("Erreur : " + (data.detail || "Réglage refusé."), "error");
            return;
        }
        showToast(data.message || "Checklist mise à jour.", "success");
        fetchChecklistSettings();
    } catch (e) { console.error("Checklist save error:", e); }
}

// --- Simulation d'impact des seuils ---
async function simulateScoringImpact() {
    const resultEl = document.getElementById("scoring-simulate-result");
    if (!resultEl) return;
    const overrides = {};
    for (const t of SCORING_OVERRIDE_TYPES) {
        const raw = document.getElementById(`scoring-ov-${t}`)?.value;
        overrides[t] = raw === "" || raw === undefined ? null : parseFloat(raw);
    }
    const payload = {
        cut_off_threshold: parseFloat(document.getElementById("scoring-global")?.value) || 75,
        cut_off_overrides: overrides,
        days: 30,
    };
    resultEl.innerHTML = `<p style="color: var(--text-muted);">Simulation en cours…</p>`;
    try {
        const response = await apiFetch("/api/settings/scoring/simulate", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) {
            resultEl.innerHTML = "";
            showToast("Erreur : " + (data.detail || "Simulation refusée."), "error");
            return;
        }
        const deltaBadge = (d) => d === 0
            ? `<span style="color: var(--text-muted);">=</span>`
            : d > 0
                ? `<span style="color: var(--color-alert); font-weight: 700;">+${d}</span>`
                : `<span style="color: var(--success-soft-text); font-weight: 700;">${d}</span>`;
        const rows = Object.entries(data.by_list).map(([lt, b]) => `
            <tr><td>${listTypeBadge(lt)}</td><td>${b.replayed}</td>
                <td>${b.alerts_now}</td><td>${b.alerts_candidate}</td>
                <td>${deltaBadge(b.delta)}</td></tr>`).join("");
        resultEl.innerHTML = `
            <p style="font-size: 0.9rem; margin-bottom: 0.5rem;">
                Rejeu de <strong>${data.totals.replayed}</strong> décision(s) sur ${data.period_days} jours
                avec un seuil global à <strong>${data.candidate.cut_off_threshold}</strong> :
                <strong>${data.totals.alerts_candidate}</strong> alertes au lieu de
                <strong>${data.totals.alerts_now}</strong> (${deltaBadge(data.totals.delta)})
                ${data.truncated ? " — échantillon tronqué à 50 000 lignes" : ""}.
                <em style="color: var(--text-muted);">Aucune modification appliquée.</em>
            </p>
            <div class="table-container">
                <table>
                    <thead><tr><th>Liste</th><th>Rejouées</th><th>Alertes actuelles</th><th>Alertes candidates</th><th>Δ</th></tr></thead>
                    <tbody>${rows || '<tr><td colspan="5" style="color: var(--text-muted);">Aucune décision rejouable sur la période.</td></tr>'}</tbody>
                </table>
            </div>`;
    } catch (e) { console.error("Simulate error:", e); }
}
