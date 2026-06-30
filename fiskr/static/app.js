// Fiskr - Dashboard Controller v2.0

let activeWatchlist = [];
let auditHistory = [];
let activeSnapshots = [];
let wlCurrentPage = 1;
const wlItemsPerPage = 100;
let wlFilteredItems = [];

document.addEventListener("DOMContentLoaded", () => {
    // Initial data loading
    fetchWatchlist();
    fetchAuditHistory();
    fetchSnapshots();
    fetchConfig();
});

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
    }
}

// Toggle fields based on entity type PP/PM
function toggleFormFields() {
    const entityType = document.getElementById("client-type").value;
    const ppFields = document.getElementById("pp-fields");
    const pmFields = document.getElementById("pm-fields");
    
    if (entityType === "PM") {
        ppFields.classList.add("hidden");
        pmFields.classList.remove("hidden");
    } else {
        ppFields.classList.remove("hidden");
        pmFields.classList.add("hidden");
    }
}

// Toggle manual form fields based on entity type
function toggleManualFormFields() {
    const entityType = document.getElementById("manual-entity-type").value;
    const individualFields = document.getElementById("manual-individual-fields");
    const vesselFields = document.getElementById("manual-vessel-fields");
    
    if (entityType === "I") {
        individualFields.classList.remove("hidden");
        vesselFields.classList.add("hidden");
    } else if (entityType === "V") {
        individualFields.classList.add("hidden");
        vesselFields.classList.remove("hidden");
    } else {
        individualFields.classList.add("hidden");
        vesselFields.classList.add("hidden");
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
    const nationality = document.getElementById("manual-nationality").value.trim();
    const residence = document.getElementById("manual-residence").value.trim();
    const aliases = document.getElementById("manual-aliases").value.trim();
    const lei = document.getElementById("manual-lei").value.trim();
    const imo = document.getElementById("manual-imo").value.trim();
    
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
        imo_number: type === "V" ? imo : null
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
        else typeBadge = '<span class="status-badge no_match">CLIENT BASE</span>';
        
        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(snap.file_name)}</strong><br><small style="color:var(--text-muted)">Hash: ${snap.file_hash.substring(0,8)}...</small></td>
            <td>${typeBadge}</td>
            <td>${snap.record_count}</td>
            <td><span class="status-dot ${snap.status === 'READY' ? 'green' : 'orange'}"></span> ${snap.status}</td>
        `;
        tbody.appendChild(tr);
    });
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
    } catch (e) {
        console.error("Error ingesting snapshot:", e);
        alert("Erreur réseau de communication.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Charger & Archiver";
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
async function handleScreening(event) {
    event.preventDefault();
    
    const clientType = document.getElementById("client-type").value;
    const clientGender = document.getElementById("client-gender").value;
    
    // Get correct names based on type
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
    
    const payload = {
        client_type: clientType,
        client_first_name: clientType === "PP" ? firstName : "",
        client_last_name: clientType === "PP" ? lastName : "",
        client_maiden_name: clientType === "PP" ? maidenName : "",
        client_company_name: clientType === "PM" ? companyName : "",
        client_dob: dob || null,
        client_gender: clientGender,
        client_is_deceased: false,
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
        client_other_id_documents: []
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
    } else {
        statusBadge.textContent = "NO_MATCH";
        statusBadge.className = "status-badge no_match";
        progress.style.stroke = "var(--color-safe)";
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
