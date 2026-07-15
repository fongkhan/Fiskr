// Fiskr - Dashboard Controller v3.1

let currentUser = null;
let activeWatchlist = [];
let auditHistory = [];
let activeSnapshots = [];
let wlCurrentPage = 1;
const wlItemsPerPage = 100;
let wlFilteredItems = [];

document.addEventListener("DOMContentLoaded", () => {
    // Check authentication and load user info
    checkAuthUser();
    // Initial data loading
    fetchWatchlist();
    fetchAuditHistory();
    fetchSnapshots();
    fetchConfig();
    fetchIngestionSettings();
    fetchPendingReviews();
    fetchAlerts();
    fetchWhitelist();
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
            // Reglages homologation (admin) et actions de revue (reviewer ou admin)
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
    if (!confirm("Voulez-vous vraiment vous déconnecter de Fiskr ?")) return;
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

// Fetch Audit Trail History
async function fetchAuditHistory() {
    try {
        const response = await fetch("/api/history");
        if (response.status === 401) return;
        auditHistory = await response.json();
        renderAuditTable(auditHistory);
    } catch (e) {
        console.error("Failed to fetch audit history:", e);
    }
}

function renderAuditTable(history) {
    const tbody = document.querySelector("#audit-table tbody");
    if (!tbody) return;
    
    if (!history || history.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted)">Aucune décision enregistrée dans la piste d\'audit.</td></tr>';
        return;
    }

    tbody.innerHTML = history.map(item => {
        const statusClass = item.status === "ALERT" ? "alert" : "no_match";
        const scoreFormatted = item.final_score !== null ? `${item.final_score.toFixed(1)}%` : "N/A";
        const timestampFormatted = item.timestamp ? new Date(item.timestamp).toLocaleString("fr-FR") : "-";

        return `
            <tr>
                <td>${timestampFormatted}</td>
                <td><strong>${escapeHtml(item.client_name || item.client_id || "N/A")}</strong></td>
                <td>${escapeHtml(item.matched_entity_name || item.matched_entity_id || "Aucun")}</td>
                <td><strong style="color: ${item.status === 'ALERT' ? '#f87171' : '#4ade80'};">${scoreFormatted}</strong></td>
                <td><span class="status-badge ${statusClass}">${item.status}</span></td>
                <td>
                    <button class="btn btn-sm" onclick="showAuditModal('${item.id}')" style="background: rgba(255, 255, 255, 0.08);">👁️ Détails</button>
                </td>
            </tr>
        `;
    }).join("");
}

function showAuditModal(auditId) {
    const item = auditHistory.find(a => a.id === auditId || String(a.id) === String(auditId));
    if (!item) return;

    const modal = document.getElementById("audit-modal");
    const container = document.getElementById("modal-audit-details");
    if (!modal || !container) return;

    container.innerHTML = `
        <div class="details-grid">
            <div class="details-item"><strong>ID Piste d'Audit</strong><span>${item.id}</span></div>
            <div class="details-item"><strong>Horodatage</strong><span>${new Date(item.timestamp).toLocaleString("fr-FR")}</span></div>
            <div class="details-item"><strong>ID Client</strong><span>${escapeHtml(item.client_id || "-")}</span></div>
            <div class="details-item"><strong>Client / Raison Sociale</strong><span>${escapeHtml(item.client_name || "-")}</span></div>
            <div class="details-item"><strong>ID Watchlist Matchée</strong><span>${escapeHtml(item.matched_entity_id || "-")}</span></div>
            <div class="details-item"><strong>Fiche Matchée</strong><span>${escapeHtml(item.matched_entity_name || "-")}</span></div>
            <div class="details-item"><strong>Score Final</strong><span>${item.final_score}%</span></div>
            <div class="details-item"><strong>Décision / Statut</strong><span>${item.status}</span></div>
            <div class="details-item"><strong>Version Watchlist</strong><span>${escapeHtml(item.watchlist_version || "-")}</span></div>
            <div class="details-item"><strong>Hash Watchlist</strong><span><code class="hash-badge">${escapeHtml(item.watchlist_hash || "-")}</code></span></div>
        </div>
        <div class="modal-section" style="margin-top: 1.5rem;">
            <h4>Arbre de Décision / Explication du Score (JSON)</h4>
            <pre class="pre-block">${JSON.stringify(item.decision_tree, null, 2)}</pre>
        </div>
        <div class="modal-section">
            <h4>État de Configuration au Moment du Screening</h4>
            <pre class="pre-block">${JSON.stringify(item.config_state, null, 2)}</pre>
        </div>
    `;

    modal.classList.remove("hidden");
    modal.style.display = "flex";
}

function closeAuditModal() {
    const modal = document.getElementById("audit-modal");
    if (modal) {
        modal.classList.add("hidden");
        modal.style.display = "none";
    }
}


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
            alert(`Erreur de validation Quality Gate : ${errors}`);
            return;
        }
        
        const data = await response.json();
        alert(`Entité ajoutée avec succès ! ID : ${data.entity_id}`);
        
        // Reset form
        document.getElementById("manual-entity-form").reset();
        toggleManualFormFields();
        
        // Switch back to Active Watchlist and refresh
        fetchWatchlist();
        switchSubTab('watchlist-mgmt', 'watchlist-active');
        
    } catch (e) {
        console.error("Error manual insert:", e);
        alert("Erreur réseau de communication.");
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
        
        renderSnapshotsTable(activeSnapshots);
        populateCompareSelects(activeSnapshots);
    } catch (e) {
        console.error("Error fetching snapshots:", e);
    }
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
        
        let typeBadge = "";
        if (snap.file_type === "WATCHLIST_OFAC") typeBadge = '<span class="status-badge alert">OFAC XML</span>';
        else if (snap.file_type === "WATCHLIST_EU") typeBadge = '<span class="status-badge warning">EU CSV/PDF</span>';
        else if (snap.file_type === "WATCHLIST_SSIE") typeBadge = '<span class="status-badge alert">SSIE XML</span>';
        else if (snap.file_type === "WATCHLIST_DGT") typeBadge = '<span class="status-badge warning">DGT JSON</span>';
        else if (snap.file_type === "WATCHLIST_UN") typeBadge = '<span class="status-badge warning">ONU XML</span>';
        else if (snap.file_type === "WATCHLIST_PEP") typeBadge = '<span class="status-badge warning">PEP CSV</span>';
        else if (snap.file_type === "WATCHLIST_OFSI") typeBadge = '<span class="status-badge warning">OFSI CSV</span>';
        else typeBadge = '<span class="status-badge no_match">CLIENT BASE</span>';
        
        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(snap.file_name)}</strong><br><small style="color:var(--text-muted)">Hash: ${snap.file_hash.substring(0,8)}...</small></td>
            <td>${typeBadge}</td>
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
        const optionText = `${snap.file_name} (${snap.file_type}) - ${dateStr}`;
        
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
        alert("Veuillez sélectionner un fichier.");
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
                alert("Les sélecteurs SSIE ne sont pas un JSON valide.");
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
            alert(`Erreur d'importation : ${data.detail || JSON.stringify(data)}`);
            return;
        }
        
        const data = await response.json();
        alert(`Instantané importé avec succès ! ${data.message}`);
        fileInput.value = "";
        fetchSnapshots();
        fetchWatchlist();
        fetchPendingReviews();
    } catch (e) {
        console.error("Error ingesting snapshot:", e);
        alert("Erreur réseau de communication.");
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
            alert(`Erreur de synchronisation : ${data.detail || JSON.stringify(data)}`);
            return;
        }
        alert(`Synchronisation ${data.source} terminée — Statut : ${data.status}\n${data.message || ""}\nDelta : +${data.added_count} / ~${data.modified_count} / -${data.removed_count}`);
        fetchSyncReports();
        fetchSnapshots();
        fetchWatchlist();
        fetchPendingReviews();
    } catch (e) {
        console.error("Error running source sync:", e);
        alert("Erreur réseau pendant la synchronisation.");
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

        const sourceLabel = report.source === "OFAC" ? "OFAC XML" : "EUR-Lex JO";

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
        alert("Sélectionnez deux snapshots différents pour comparer.");
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
            alert(`Erreur de comparaison : ${data.detail || JSON.stringify(data)}`);
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
        alert("Erreur lors de la comparaison des versions.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Comparer les versions";
    }
}

// Fetch Watchlist
async function fetchWatchlist() {
    try {
        const response = await fetch("/api/watchlist");
        const data = await response.json();
        
        activeWatchlist = data.items || [];
        wlFilteredItems = activeWatchlist;
        wlCurrentPage = 1;
        
        const hashEl = document.getElementById("sidebar-wl-hash");
        if (hashEl) {
            hashEl.textContent = data.hash ? data.hash.substring(0, 12) + "..." : "NONE";
            hashEl.title = data.hash;
        }
        
        renderWatchlistTable(wlFilteredItems, wlCurrentPage);
    } catch (e) {
        console.error("Error loading watchlist:", e);
    }
}

// Render Watchlist Table
function renderWatchlistTable(items, page = 1) {
    const tbody = document.querySelector("#watchlist-table tbody");
    tbody.innerHTML = "";
    
    if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted)">Aucune entité de sanctions active chargée</td></tr>';
        updatePaginationControls(0, 0);
        return;
    }
    
    const startIndex = (page - 1) * wlItemsPerPage;
    const endIndex = Math.min(startIndex + wlItemsPerPage, items.length);
    const paginatedItems = items.slice(startIndex, endIndex);
    
    const fragment = document.createDocumentFragment();
    
    paginatedItems.forEach(item => {
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
        if (item.entity_type === "I") typeBadge = '<span class="status-badge no_match">I (Indiv)</span>';
        else if (item.entity_type === "E") typeBadge = '<span class="status-badge alert">E (Entity)</span>';
        else if (item.entity_type === "V") typeBadge = '<span class="status-badge warning">V (Vessel)</span>';
        else typeBadge = '<span class="status-badge">O (Other)</span>';
        
        tr.innerHTML = `
            <td><code>${escapeHtml(item.entity_id)}</code></td>
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
    updatePaginationControls(items.length, page);
}

// Filter Watchlist active items
function filterWatchlist() {
    const query = document.getElementById("wl-search-input").value.toLowerCase().trim();
    if (!query) {
        wlFilteredItems = activeWatchlist;
        wlCurrentPage = 1;
        renderWatchlistTable(wlFilteredItems, wlCurrentPage);
        return;
    }
    
    wlFilteredItems = activeWatchlist.filter(item => {
        const id = (item.entity_id || "").toLowerCase();
        const name = (item.primary_name || "").toLowerCase();
        const lei = (item.lei_number || "").toLowerCase();
        const imo = (item.imo_number || "").toLowerCase();
        return id.includes(query) || name.includes(query) || lei.includes(query) || imo.includes(query);
    });
    
    wlCurrentPage = 1;
    renderWatchlistTable(wlFilteredItems, wlCurrentPage);
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
    wlCurrentPage = newPage;
    renderWatchlistTable(wlFilteredItems, wlCurrentPage);
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
            alert(`Criblage rejeté par le Data Quality Gate : ${errors}`);
            return;
        }
        
        const data = await response.json();
        
        placeholder.classList.add("hidden");
        resultsCard.classList.remove("hidden");

        renderScreeningResult(data);
        // Une decision ALERT ouvre une alerte de travail : rafraichir le badge
        if (data.alert_id) fetchAlerts();
    } catch (e) {
        console.error("Error screening:", e);
        alert("Erreur réseau lors de l'appel au moteur.");
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Launch Screening Engine";
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
        alert("Saisissez des clients.");
        return;
    }
    
    let clients = [];
    try {
        clients = JSON.parse(text);
        if (!Array.isArray(clients)) {
            alert("Format attendu : Tableau JSON");
            return;
        }
    } catch (e) {
        alert(`Erreur JSON : ${e.message}`);
        return;
    }
    
    const btn = document.getElementById("run-batch-btn");
    btn.disabled = true;
    btn.textContent = "Exécution...";
    
    const resultsContainer = document.getElementById("batch-results-container");
    const tbody = document.querySelector("#batch-results-table tbody");
    tbody.innerHTML = "";
    
    let alertsCount = 0;
    
    try {
        for (const client of clients) {
            const response = await fetch("/api/screen", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(client)
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
                    <td><span class="status-badge alert">ALERT</span></td>
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
    } catch (e) {
        console.error("Batch failure:", e);
        alert("Erreur lors de l'exécution du batch.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Lancer le Batch Screening";
        fetchAuditHistory();
    }
}

// Fetch Audit history
async function fetchAuditHistory() {
    try {
        const response = await fetch("/api/history");
        auditHistory = await response.json();
        renderAuditHistoryTable(auditHistory);
    } catch (e) {
        console.error("Error loading history:", e);
    }
}

function renderAuditHistoryTable(logs) {
    const tbody = document.querySelector("#audit-table tbody");
    tbody.innerHTML = "";
    
    if (logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted)">Aucun audit log disponible</td></tr>';
        return;
    }
    
    logs.forEach(log => {
        const dateStr = new Date(log.timestamp + "Z").toLocaleString("fr-FR");
        const tr = document.createElement("tr");
        
        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(log.client_name)}</strong> <span class="status-badge">${log.client_type}</span></td>
            <td><code>${escapeHtml(log.watchlist_id)}</code> - <strong>${escapeHtml(log.watchlist_name)}</strong></td>
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
    
    modal.style.display = "block";
}

function closeAuditModal() {
    document.getElementById("audit-modal").style.display = "none";
}

async function fetchConfig() {
    try {
        const response = await fetch("/api/config");
        const configData = await response.json();
        console.log("Active configuration loaded:", configData);
    } catch (e) {
        console.error("Error loading config:", e);
    }
}

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

window.onclick = function(event) {
    const modal = document.getElementById("audit-modal");
    if (event.target == modal) {
        modal.style.display = "none";
    }
}

// Purge Failed/Processing Snapshots
async function purgeFailedSnapshots() {
    if (!confirm("Voulez-vous vraiment purger tous les snapshots et entités en erreur ou en cours de traitement ? Cette action est irréversible.")) {
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
            alert(`Erreur lors de la purge : ${data.detail || JSON.stringify(data)}`);
            return;
        }
        
        const data = await response.json();
        alert(`Purge terminée : ${data.message}`);
        fetchSnapshots();
        fetchWatchlist();
    } catch (e) {
        console.error("Purge failed:", e);
        alert("Erreur réseau lors de l'appel à la purge.");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = "🗑️ Purger les imports erronés";
        }
    }
}

// Watchlist details Modal trigger
function showWatchlistDetails(item) {
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
    
    body.innerHTML = `
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
            <div class="details-item" style="grid-column: span 2;"><strong>Motifs de la Désignation</strong><span>${escapeHtml(item.designation_reasons || "-")}</span></div>
            <div class="details-item" style="grid-column: span 2;"><strong>Adresses Alternatives</strong><span>${escapeHtml(altAddrs)}</span></div>
            <div class="details-item" style="grid-column: span 2;"><strong>Alias</strong><span>${escapeHtml(aliasesStr)}</span></div>
            <div class="details-item"><strong>LEI (Legal Entity Identifier)</strong><span>${escapeHtml(item.lei_number || "-")}</span></div>
            <div class="details-item"><strong>IMO Code (Navire)</strong><span>${escapeHtml(item.imo_number || "-")}</span></div>
            <div class="details-item"><strong>Tail Number (Immatriculation Aéronef)</strong><span>${escapeHtml(item.aircraft_tail_number || "-")}</span></div>
        </div>
    `;
    
    modal.classList.remove("hidden");
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
            alert("Accès refusé. Droits d'administrateur requis.");
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
            alert("Erreur: " + (data.detail || "Échec de l'enregistrement de l'utilisateur."));
            return;
        }

        closeUserModal();
        fetchUsersList();
        if (currentUser && currentUser.id === parseInt(editId, 10)) {
            checkAuthUser();
        }
    } catch (err) {
        console.error("Save user error:", err);
        alert("Erreur de communication avec le serveur.");
    }
}

async function deleteUserAccount(userId, username) {
    if (!confirm(`Voulez-vous vraiment supprimer définitivement le compte de @${username} ?`)) return;

    try {
        const response = await fetch(`/api/users/${userId}`, {
            method: "DELETE"
        });
        const data = await response.json();
        if (!response.ok) {
            alert("Erreur: " + (data.detail || "Échec de la suppression du compte."));
            return;
        }

        fetchUsersList();
    } catch (err) {
        console.error("Delete user error:", err);
        alert("Erreur de connexion lors de la suppression.");
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
            alert("Erreur Profil: " + (profileData.detail || "Échec de la mise à jour du profil."));
            return;
        }

        // 2. Update Password if requested
        if (oldPassword || newPassword) {
            if (!oldPassword || !newPassword) {
                alert("Pour modifier votre mot de passe, veuillez saisir l'ancien ET le nouveau mot de passe.");
                return;
            }
            const passResp = await fetch("/api/users/me/password", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
            });
            const passData = await passResp.json();
            if (!passResp.ok) {
                alert("Erreur Mot de Passe: " + (passData.detail || "Échec du changement de mot de passe."));
                return;
            }
        }

        alert("Votre profil et vos paramètres de sécurité ont été mis à jour.");
        closeProfileModal();
        checkAuthUser();
    } catch (err) {
        console.error("Update profile error:", err);
        alert("Erreur de communication avec le serveur.");
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
        if (approvalEl) approvalEl.checked = ingestionSettings.require_approval;
        if (justifEl) justifEl.checked = ingestionSettings.exclusion_justification_required;
        if (fileEl) fileEl.checked = ingestionSettings.exclusion_file_required;
        if (fourEyesEl) fourEyesEl.checked = ingestionSettings.alert_four_eyes_required;
        if (wlJustifEl) wlJustifEl.checked = ingestionSettings.whitelist_justification_required;
        if (wlFileEl) wlFileEl.checked = ingestionSettings.whitelist_file_required;
        if (rescreenEl) rescreenEl.checked = ingestionSettings.auto_rescreen;
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
        auto_rescreen: document.getElementById("setting-auto-rescreen").checked
    };
    try {
        const response = await fetch("/api/settings/ingestion", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) {
            alert("Erreur : " + (data.detail || "Échec de la mise à jour des réglages."));
            return;
        }
        alert(data.message || "Réglages mis à jour.");
        fetchIngestionSettings();
    } catch (e) {
        console.error("Error saving ingestion settings:", e);
        alert("Erreur réseau de communication.");
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
                <td><span class="status-badge warning">${escapeHtml(snap.file_type)}</span></td>
                <td>${snap.record_count}</td>
                <td>${snap.excluded_count || 0}</td>
                <td><button class="btn btn-sm btn-secondary" onclick="openReviewDetail('${escapeHtml(snap.snapshot_id)}')">🔍 Examiner</button></td>
            </tr>
        `;
    }).join("");
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
            alert("Erreur : " + (data.detail || "Impossible de charger le snapshot."));
            return;
        }
        document.getElementById("review-detail-card").classList.remove("hidden");
        document.getElementById("review-detail-title").textContent = `Examen du Snapshot — ${data.file_name}`;
        const uploadedStr = data.uploaded_at ? new Date(data.uploaded_at).toLocaleString("fr-FR") : "-";
        document.getElementById("review-detail-meta").textContent =
            `${data.file_type} · ${data.record_count} fiches · importé le ${uploadedStr} · delta calculé par rapport à la production actuelle` +
            (data.production_snapshot_id ? "" : " (aucune liste du même type en production : tout est en ajout)");
        const summary = data.delta_summary || {};
        document.getElementById("review-delta-added").textContent = summary.added_count ?? 0;
        document.getElementById("review-delta-removed").textContent = summary.removed_count ?? 0;
        document.getElementById("review-delta-modified").textContent = summary.modified_count ?? 0;
        await loadReviewEntitiesPage(1);
        document.getElementById("review-detail-card").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
        console.error("Error opening review detail:", e);
        alert("Erreur réseau de communication.");
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
            alert("Erreur : " + (data.detail || "Impossible de charger les entités."));
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
        alert("Sélectionnez au moins une entité à exclure (cases à cocher).");
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
        alert("Une justification est obligatoire pour exclure une entité (réglage actif).");
        return;
    }
    if (ingestionSettings && ingestionSettings.exclusion_file_required && fileInput.files.length === 0) {
        alert("Une pièce jointe justificative est obligatoire pour exclure une entité (réglage actif).");
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
            alert("Erreur : " + (data.detail || "Échec de l'exclusion."));
            return;
        }
        closeExclusionModal();
        reviewExcludedSelection = new Set();
        alert(data.message);
        loadReviewEntitiesPage(reviewCurrentPage);
        fetchPendingReviews();
    } catch (e) {
        console.error("Error submitting exclusions:", e);
        alert("Erreur réseau de communication.");
    }
}

async function removeExclusions() {
    if (!reviewCurrentSnapshotId || reviewExcludedSelection.size === 0) {
        alert("Sélectionnez au moins une entité à réintégrer (cases à cocher).");
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
            alert("Erreur : " + (data.detail || "Échec de la réintégration."));
            return;
        }
        reviewExcludedSelection = new Set();
        alert(data.message);
        loadReviewEntitiesPage(reviewCurrentPage);
        fetchPendingReviews();
    } catch (e) {
        console.error("Error removing exclusions:", e);
        alert("Erreur réseau de communication.");
    }
}

async function approvePendingSnapshot() {
    if (!reviewCurrentSnapshotId) return;
    if (!confirm("Approuver ce snapshot ? Il sera mis en production et remplacera les listes antérieures du même type (hors entités exclues).")) return;
    const comment = document.getElementById("review-comment").value.trim();
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment })
        });
        const data = await response.json();
        if (!response.ok) {
            alert("Erreur : " + (data.detail || "Échec de l'approbation."));
            return;
        }
        alert(`${data.message} (${data.excluded_count} entité(s) exclue(s))`);
        document.getElementById("review-detail-card").classList.add("hidden");
        reviewCurrentSnapshotId = null;
        fetchPendingReviews();
        fetchSnapshots();
        fetchWatchlist();
    } catch (e) {
        console.error("Error approving snapshot:", e);
        alert("Erreur réseau de communication.");
    }
}

async function rejectPendingSnapshot() {
    if (!reviewCurrentSnapshotId) return;
    const comment = document.getElementById("review-comment").value.trim();
    if (!comment) {
        alert("Un commentaire est requis pour rejeter un snapshot.");
        return;
    }
    if (!confirm("Rejeter ce snapshot ? Il n'entrera jamais en production (conservé en base pour l'audit).")) return;
    try {
        const response = await fetch(`/api/review/snapshots/${encodeURIComponent(reviewCurrentSnapshotId)}/reject`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment })
        });
        const data = await response.json();
        if (!response.ok) {
            alert("Erreur : " + (data.detail || "Échec du rejet."));
            return;
        }
        alert(data.message);
        document.getElementById("review-detail-card").classList.add("hidden");
        reviewCurrentSnapshotId = null;
        fetchPendingReviews();
        fetchSnapshots();
    } catch (e) {
        console.error("Error rejecting snapshot:", e);
        alert("Erreur réseau de communication.");
    }
}

// ------------------ ALERTES (CYCLE DE VIE + 4-YEUX) ------------------

let alertsFilter = "OPEN,IN_PROGRESS,ESCALATED,PENDING_VALIDATION";
let currentAlertId = null;

async function fetchAlerts() {
    try {
        const params = new URLSearchParams({ page: "1", page_size: "100" });
        if (alertsFilter) params.set("status", alertsFilter);
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
        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Aucune alerte pour ce filtre.</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(a => `
        <tr>
            <td>${a.created_at ? new Date(a.created_at).toLocaleString("fr-FR") : "-"}</td>
            <td><strong>${escapeHtml(a.client_name)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(a.client_id || "")}</small></td>
            <td>${escapeHtml(a.watchlist_name)}<br><small style="color:var(--text-muted)">${escapeHtml(a.watchlist_entity_id)}</small></td>
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
            alert("Erreur : " + (a.detail || "Impossible de charger l'alerte."));
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
        `;
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
        alert("Erreur : " + (data.detail || "Action refusée."));
        return null;
    }
    return data;
}

async function alertAction(action) {
    const data = await _postAlertAction(action, {});
    if (data) { openAlertModal(currentAlertId); fetchAlerts(); }
}

async function alertActionWithComment(action, promptLabel) {
    const comment = prompt(promptLabel + " :");
    if (comment === null) return;
    const data = await _postAlertAction(action, { comment });
    if (data) { openAlertModal(currentAlertId); fetchAlerts(); }
}

async function proposeAlertDecision(decision) {
    const label = decision === "CONFIRMED" ? "vrai positif" : "faux positif";
    const comment = prompt(`Commentaire obligatoire pour proposer « ${label} » :`);
    if (comment === null) return;
    const data = await _postAlertAction("propose", { decision, comment });
    if (data) { alert(data.message); openAlertModal(currentAlertId); fetchAlerts(); }
}

async function validateAlertDecision(approve) {
    const comment = prompt(approve ? "Commentaire (optionnel) :" : "Motif du refus (obligatoire) :");
    if (comment === null) return;
    const data = await _postAlertAction("validate", { approve, comment });
    if (data) { alert(data.message); openAlertModal(currentAlertId); fetchAlerts(); }
}

// ------------------ LISTE BLANCHE CLIENT x LISTÉ (GOOD GUYS) ------------------

async function fetchWhitelist() {
    try {
        const response = await fetch("/api/whitelist?page_size=100");
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
        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Aucune paire en liste blanche.</td></tr>';
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
            <td style="max-width: 260px;"><small>${escapeHtml(p.justification || "—")}</small>${p.evidence_file_name ? `<br><a href="/api/whitelist/evidence/${p.id}" target="_blank" style="color: var(--color-accent); font-size: 0.75rem;">📎 ${escapeHtml(p.evidence_file_name)}</a>` : ""}</td>
            <td>@${escapeHtml(p.created_by)}<br><small style="color:var(--text-muted)">${p.created_at ? new Date(p.created_at).toLocaleDateString("fr-FR") : ""}</small></td>
            <td>${p.expires_at ? new Date(p.expires_at).toLocaleDateString("fr-FR") : "—"}</td>
            <td>${stateBadge(p.state)}</td>
            <td>${p.state === "ACTIVE" ? `<button class="btn btn-sm" style="background: rgba(239,68,68,0.2); color: #fca5a5;" onclick="revokeWhitelistPair(${p.id})">Révoquer</button>` : ""}</td>
        </tr>
    `).join("");
}

async function revokeWhitelistPair(pairId) {
    const comment = prompt("Motif de la révocation (obligatoire) — les alertes de ce couple reprendront :");
    if (comment === null) return;
    try {
        const response = await fetch(`/api/whitelist/${pairId}/revoke`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ comment })
        });
        const data = await response.json();
        if (!response.ok) {
            alert("Erreur : " + (data.detail || "Révocation refusée."));
            return;
        }
        alert(data.message);
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
            alert("Erreur : " + (data.detail || "Mise en liste blanche refusée."));
            return;
        }
        closeWhitelistModal();
        alert(data.message);
        fetchWhitelist();
    } catch (e) {
        console.error("Error creating whitelist pair:", e);
        alert("Erreur réseau de communication.");
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
            ? Object.entries(byType).map(([t, n]) => `<tr><td>${escapeHtml(t)}</td><td><strong>${n}</strong></td></tr>`).join("")
            : '<tr><td colspan="2" style="color: var(--text-muted); text-align: center;">Aucune liste en production.</td></tr>';

        const syncsBody = document.querySelector("#kpi-syncs-table tbody");
        const syncs = k.recent_syncs || [];
        syncsBody.innerHTML = syncs.length
            ? syncs.map(s => `<tr>
                <td>${s.executed_at ? new Date(s.executed_at).toLocaleString("fr-FR") : "-"}</td>
                <td>${escapeHtml(s.source)} <small style="color:var(--text-muted)">${escapeHtml(s.trigger)}</small></td>
                <td>${escapeHtml(s.status)}</td>
                <td><small>+${s.added} / ~${s.modified} / -${s.removed}</small></td>
              </tr>`).join("")
            : '<tr><td colspan="4" style="color: var(--text-muted); text-align: center;">Aucune synchronisation.</td></tr>';
    } catch (e) {
        console.error("Error fetching KPIs:", e);
    }
}
