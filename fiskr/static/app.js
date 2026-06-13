// Fiskr - Dashboard Controller

let activeWatchlist = [];
let auditHistory = [];

document.addEventListener("DOMContentLoaded", () => {
    // Initial data loading
    fetchWatchlist();
    fetchAuditHistory();
    fetchConfig();
});

// Tab navigation
function switchTab(tabId) {
    // Update nav items
    document.querySelectorAll(".nav-item").forEach(item => {
        item.classList.remove("active");
    });
    const activeBtn = document.getElementById(`nav-btn-${tabId}`);
    if (activeBtn) activeBtn.classList.add("active");
    
    // Update view sections
    document.querySelectorAll(".tab-content").forEach(sec => {
        sec.classList.remove("active");
    });
    document.getElementById(`sec-${tabId}`).classList.add("active");
    
    // Refresh lists on tab click
    if (tabId === "watchlist") {
        fetchWatchlist();
    } else if (tabId === "history") {
        fetchAuditHistory();
    }
}

// Toggle fields based on entity type PP/PM
function toggleFormFields() {
    const entityType = document.getElementById("client-type").value;
    const dobGroup = document.getElementById("dob-group");
    
    if (entityType === "PM") {
        dobGroup.style.display = "none";
    } else {
        dobGroup.style.display = "block";
    }
}

// Fetch Watchlist data
async function fetchWatchlist() {
    try {
        const response = await fetch("/api/watchlist");
        const data = await response.json();
        
        activeWatchlist = data.items || [];
        
        // Update hash in sidebar
        const hashEl = document.getElementById("sidebar-wl-hash");
        if (hashEl) {
            hashEl.textContent = data.hash ? data.hash.substring(0, 12) + "..." : "N/A";
            hashEl.title = data.hash;
        }
        
        renderWatchlistTable(activeWatchlist);
    } catch (error) {
        console.error("Error fetching watchlist:", error);
    }
}

// Render Watchlist Table
function renderWatchlistTable(items) {
    const tbody = document.querySelector("#watchlist-table tbody");
    tbody.innerHTML = "";
    
    if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Aucune fiche chargée</td></tr>';
        return;
    }
    
    items.forEach(item => {
        const tr = document.createElement("tr");
        
        // Format countries
        const countriesDict = item.countries || {};
        const citizenship = countriesDict.citizenship || [];
        const residence = countriesDict.residence || [];
        const birth = countriesDict.birth_country || [];
        const allCountries = [...new Set([...citizenship, ...residence, ...birth])].join(", ") || "-";
        
        // Format DOB
        const dobs = item.dates_of_birth || [];
        const dobStr = dobs.join(", ") || "-";
        
        tr.innerHTML = `
            <td><code>${escapeHtml(item.entity_id)}</code></td>
            <td><span class="status-badge ${item.entity_type === "PP" ? "no_match" : "alert"}">${item.entity_type}</span></td>
            <td><strong>${escapeHtml(item.primary_name)}</strong>${item.aliases && item.aliases.length > 0 ? ` <small style="color: var(--text-muted)">(${item.aliases.join(', ')})</small>` : ''}</td>
            <td>${escapeHtml(allCountries)}</td>
            <td>${escapeHtml(dobStr)}</td>
        `;
        tbody.appendChild(tr);
    });
}

// Filter Watchlist items
function filterWatchlist() {
    const query = document.getElementById("wl-search-input").value.toLowerCase().trim();
    if (!query) {
        renderWatchlistTable(activeWatchlist);
        return;
    }
    
    const filtered = activeWatchlist.filter(item => {
        const id = (item.entity_id || "").toLowerCase();
        const name = (item.primary_name || "").toLowerCase();
        const aliases = (item.aliases || []).join(" ").toLowerCase();
        return id.includes(query) || name.includes(query) || aliases.includes(query);
    });
    
    renderWatchlistTable(filtered);
}

// Add Item to Watchlist
async function handleAddWatchlist(event) {
    event.preventDefault();
    
    const wlId = document.getElementById("wl-id").value.trim();
    const wlName = document.getElementById("wl-name").value.trim();
    const wlType = document.getElementById("wl-type").value;
    const wlGender = document.getElementById("wl-gender").value;
    const wlDob = document.getElementById("wl-dob").value.trim();
    const wlCountriesStr = document.getElementById("wl-countries").value.trim();
    const wlAliasesStr = document.getElementById("wl-aliases").value.trim();
    
    const countriesList = wlCountriesStr ? wlCountriesStr.split(",").map(c => c.trim().toUpperCase()) : [];
    const aliasesList = wlAliasesStr ? wlAliasesStr.split(",").map(a => a.trim()) : [];
    
    const payload = {
        entity_id: wlId,
        entity_type: wlType,
        primary_name: wlName,
        aliases: aliasesList,
        dates_of_birth: wlDob ? [wlDob] : [],
        genders: wlGender !== "U" ? [wlGender] : [],
        countries: {
            citizenship: wlType === "PP" ? countriesList : [],
            residence: countriesList,
            birth_country: []
        }
    };
    
    try {
        const response = await fetch("/api/watchlist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const errData = await response.json();
            alert(`Erreur : ${errData.detail || JSON.stringify(errData)}`);
            return;
        }
        
        alert("Fiche ajoutée et indexée dans le moteur avec succès !");
        document.getElementById("add-watchlist-form").reset();
        fetchWatchlist();
    } catch (error) {
        console.error("Error adding to watchlist:", error);
        alert("Erreur réseau de communication avec l'API.");
    }
}

// Handle Real-Time Screening
async function handleScreening(event) {
    event.preventDefault();
    
    const clientName = document.getElementById("client-name").value.trim();
    const clientType = document.getElementById("client-type").value;
    const clientGender = document.getElementById("client-gender").value;
    const clientDob = document.getElementById("client-dob").value;
    const clientCountriesStr = document.getElementById("client-countries").value.trim();
    const clientAliasesStr = document.getElementById("client-aliases").value.trim();
    
    const countriesList = clientCountriesStr ? clientCountriesStr.split(",").map(c => c.trim().toUpperCase()) : [];
    const aliasesList = clientAliasesStr ? clientAliasesStr.split(",").map(a => a.trim()) : [];
    
    const payload = {
        entity_type: clientType,
        primary_name: clientName,
        aliases: aliasesList,
        dates_of_birth: clientDob ? [clientDob] : [],
        genders: [clientGender],
        countries: {
            citizenship: countriesList,
            residence: countriesList,
            birth_country: []
        }
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
            alert(`Criblage bloqué (Data Quality Gate) : ${errors}`);
            return;
        }
        
        const data = await response.json();
        
        // Hide placeholder and show results
        placeholder.classList.add("hidden");
        resultsCard.classList.remove("hidden");
        
        renderScreeningResult(data);
    } catch (error) {
        console.error("Error running screening:", error);
        alert("Erreur lors de l'exécution du criblage.");
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Launch Screening Engine";
    }
}

// Render Screening Result
function renderScreeningResult(data) {
    const qualityReport = data.client_quality_report;
    const bestMatch = data.best_match;
    const keys = data.blocking_keys_generated || [];
    
    // 1. Data Quality Gate Render
    const qStatusDot = document.querySelector("#quality-gate-status .status-dot");
    const qStatusText = document.querySelector("#quality-gate-status");
    const qAnomalies = document.getElementById("quality-gate-anomalies");
    
    qAnomalies.innerHTML = "";
    
    qStatusDot.className = "status-dot";
    if (qualityReport.status === "OK") {
        qStatusDot.classList.add("green");
        qStatusText.innerHTML = '<span class="status-dot green"></span> Conforme (OK)';
    } else {
        qStatusDot.classList.add("orange");
        qStatusText.innerHTML = '<span class="status-dot orange"></span> Qualité Dégradée';
    }
    
    if (qualityReport.warnings && qualityReport.warnings.length > 0) {
        qualityReport.warnings.forEach(w => {
            const div = document.createElement("div");
            div.className = "anomaly-item warning";
            div.textContent = w;
            qAnomalies.appendChild(div);
        });
    } else {
        const div = document.createElement("div");
        div.className = "anomaly-item";
        div.style.color = "var(--text-muted)";
        div.textContent = "Aucune anomalie détectée.";
        qAnomalies.appendChild(div);
    }
    
    // 2. Metrics Render
    document.getElementById("metric-blocking-keys").textContent = keys.join(", ");
    document.getElementById("metric-candidates").textContent = data.candidates_count;
    
    // 3. Score Gauge & Alert Status
    const statusBadge = document.getElementById("badge-compliance-status");
    const finalScore = bestMatch ? bestMatch.final_score : 0.0;
    const baseScore = bestMatch ? bestMatch.base_score : 0.0;
    
    document.getElementById("metric-base-score").textContent = `${baseScore.toFixed(1)}%`;
    document.getElementById("gauge-score-value").textContent = `${finalScore.toFixed(1)}%`;
    
    // Animate Gauge
    const progressCircle = document.getElementById("gauge-progress");
    const radius = progressCircle.r.baseVal.value;
    const circumference = 2 * Math.PI * radius;
    const offset = circumference - (finalScore / 100) * circumference;
    progressCircle.style.strokeDasharray = `${circumference}`;
    progressCircle.style.strokeDashoffset = `${offset}`;
    
    // Match colors
    if (bestMatch && bestMatch.status === "ALERT") {
        statusBadge.textContent = "ALERT";
        statusBadge.className = "status-badge alert";
        progressCircle.style.stroke = "var(--color-alert)";
    } else {
        statusBadge.textContent = "NO_MATCH";
        statusBadge.className = "status-badge no_match";
        progressCircle.style.stroke = "var(--color-safe)";
    }
    
    // 4. Decision Tree
    const treeContainer = document.getElementById("match-details-container");
    
    if (!bestMatch) {
        treeContainer.innerHTML = `
            <div style="text-align: center; padding: 1.5rem; color: var(--text-muted)">
                Aucune fiche watchlist ne correspond aux clés de blocking générées (Pas de calcul de score).
            </div>
        `;
        return;
    }
    
    // Restore base html structure
    treeContainer.innerHTML = `
        <div class="detail-row">
            <span>Nom Client Apparié :</span>
            <strong>${escapeHtml(bestMatch.best_client_name)}</strong>
        </div>
        <div class="detail-row">
            <span>Nom Watchlist Apparié :</span>
            <strong>${escapeHtml(bestMatch.best_watchlist_name)}</strong>
        </div>
        <div class="detail-row">
            <span>Fiche Watchlist Source :</span>
            <strong><code>${escapeHtml(bestMatch.watchlist_entity.entity_id)}</code></strong>
        </div>
        
        <div class="adjustments-tree">
            <h4>Ajustements Contextuels</h4>
            <ul>
                <li>
                    <span class="adj-name">Date de Naissance (DOB)</span>
                    <span id="adj-dob-val" class="adj-val">0</span>
                    <div id="adj-dob-desc" class="adj-desc">-</div>
                </li>
                <li>
                    <span class="adj-name">Genre</span>
                    <span id="adj-gender-val" class="adj-val">0</span>
                    <div id="adj-gender-desc" class="adj-desc">-</div>
                </li>
                <li>
                    <span class="adj-name">Géographie (Pays)</span>
                    <span id="adj-geo-val" class="adj-val">0</span>
                    <div id="adj-geo-desc" class="adj-desc">-</div>
                </li>
            </ul>
        </div>
    `;
    
    // Populate adjustments
    const dobAdj = bestMatch.adjustments.dob;
    const genderAdj = bestMatch.adjustments.gender;
    const geoAdj = bestMatch.adjustments.geography;
    
    formatAdjustment("adj-dob-val", "adj-dob-desc", dobAdj.score, dobAdj.description);
    formatAdjustment("adj-gender-val", "adj-gender-desc", genderAdj.score, genderAdj.description);
    formatAdjustment("adj-geo-val", "adj-geo-desc", geoAdj.score, geoAdj.description);
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

// Load sample JSON into Batch Area
function loadSampleBatchJSON() {
    const samples = [
        {
            entity_id: "BATCH-CLI-01",
            entity_type: "PP",
            primary_name: "Vladimir Putin",
            dates_of_birth: ["1952-10-07"],
            genders: ["M"],
            countries: { citizenship: ["RU"] }
        },
        {
            entity_id: "BATCH-CLI-02",
            entity_type: "PM",
            primary_name: "Rosneft SA",
            dates_of_birth: [],
            genders: [],
            countries: { residence: ["RU"] }
        },
        {
            entity_id: "BATCH-CLI-03",
            entity_type: "PP",
            primary_name: "John Smith",
            dates_of_birth: ["1980-05-12"],
            genders: ["M"],
            countries: { residence: ["US"] }
        },
        {
            entity_id: "BATCH-CLI-04",
            entity_type: "PP",
            primary_name: "Kadafi Mouammar",
            dates_of_birth: ["1942-06-07"],
            genders: ["M"],
            countries: { citizenship: ["LY"] }
        }
    ];
    document.getElementById("batch-json-input").value = JSON.stringify(samples, null, 2);
}

// Run Batch Screening Simulation
async function runBatchScreening() {
    const inputText = document.getElementById("batch-json-input").value.trim();
    if (!inputText) {
        alert("Veuillez saisir un tableau de profils clients.");
        return;
    }
    
    let clients = [];
    try {
        clients = JSON.parse(inputText);
        if (!Array.isArray(clients)) {
            alert("Le format de données doit être un tableau JSON.");
            return;
        }
    } catch (e) {
        alert(`Erreur de syntaxe JSON : ${e.message}`);
        return;
    }
    
    const btn = document.getElementById("run-batch-btn");
    btn.disabled = true;
    btn.textContent = "Exécution du Batch...";
    
    const resultsContainer = document.getElementById("batch-results-container");
    const tbody = document.querySelector("#batch-results-table tbody");
    tbody.innerHTML = "";
    
    let alertsCount = 0;
    
    try {
        for (const client of clients) {
            // We run each client through real screening endpoint.
            // This allows demonstrating the screening algorithm.
            const response = await fetch("/api/screen", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(client)
            });
            
            const tr = document.createElement("tr");
            
            if (!response.ok) {
                // If Quality Gate rejected, show as REJECT
                const errData = await response.json();
                const errors = errData.detail && errData.detail.errors ? errData.detail.errors.join(", ") : "Qualité Invalide";
                
                tr.innerHTML = `
                    <td><code>${escapeHtml(client.entity_id || "-")}</code></td>
                    <td><strong>${escapeHtml(client.primary_name || "-")}</strong></td>
                    <td colspan="3" style="color: var(--color-alert)"><strong>REJETE : Data Quality Gate</strong> (${errors})</td>
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
                    <td><code>${escapeHtml(client.entity_id || "-")}</code></td>
                    <td><strong>${escapeHtml(client.primary_name || "-")}</strong></td>
                    <td><code>${escapeHtml(best.watchlist_entity.entity_id)}</code></td>
                    <td><strong>${escapeHtml(best.best_watchlist_name)}</strong></td>
                    <td style="color: var(--color-alert); font-weight:700">${best.final_score.toFixed(1)}%</td>
                    <td><span class="status-badge alert">ALERT</span></td>
                `;
            } else {
                tr.innerHTML = `
                    <td><code>${escapeHtml(client.entity_id || "-")}</code></td>
                    <td><strong>${escapeHtml(client.primary_name || "-")}</strong></td>
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
        
    } catch (error) {
        console.error("Error running batch screening:", error);
        alert("Erreur lors de l'exécution du batch.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Lancer le Batch Screening";
        // Refresh audit log history
        fetchAuditHistory();
    }
}

// Fetch Audit History logs
async function fetchAuditHistory() {
    try {
        const response = await fetch("/api/history");
        auditHistory = await response.json();
        
        renderAuditHistoryTable(auditHistory);
    } catch (error) {
        console.error("Error fetching audit logs:", error);
    }
}

// Render Audit History table
function renderAuditHistoryTable(logs) {
    const tbody = document.querySelector("#audit-table tbody");
    tbody.innerHTML = "";
    
    if (logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Aucune décision auditée</td></tr>';
        return;
    }
    
    logs.forEach(log => {
        const tr = document.createElement("tr");
        
        // Parse timestamp
        const dateStr = new Date(log.timestamp + "Z").toLocaleString("fr-FR");
        
        tr.innerHTML = `
            <td>${escapeHtml(dateStr)}</td>
            <td><strong>${escapeHtml(log.client_name)}</strong> <span style="font-size:0.75rem" class="status-badge">${log.client_type}</span></td>
            <td><code>${escapeHtml(log.watchlist_id)}</code> - <strong>${escapeHtml(log.watchlist_name)}</strong></td>
            <td style="font-weight: 700; color: ${log.status === "ALERT" ? "var(--color-alert)" : "var(--color-safe)"}">${log.final_score.toFixed(1)}%</td>
            <td><span class="status-badge ${log.status === "ALERT" ? "alert" : "no_match"}">${log.status}</span></td>
            <td><button class="btn btn-secondary" style="font-size:0.75rem; padding: 0.25rem 0.5rem;" onclick="viewAuditLogDetail(${log.id})">Inspecter</button></td>
        `;
        tbody.appendChild(tr);
    });
}

// View Audit Log Detail Modal
function viewAuditLogDetail(logId) {
    const log = auditHistory.find(item => item.id === logId);
    if (!log) return;
    
    const modal = document.getElementById("audit-modal");
    const content = document.getElementById("modal-audit-details");
    
    const dateStr = new Date(log.timestamp + "Z").toLocaleString("fr-FR");
    
    // Parse decision tree
    let tree = typeof log.decision_tree === "string" ? JSON.parse(log.decision_tree) : log.decision_tree;
    let configState = typeof log.config_state === "string" ? JSON.parse(log.config_state) : log.config_state;
    
    let adjHtml = "";
    if (tree && tree.adjustments) {
        adjHtml = `
            <ul>
                <li>Date de Naissance : <strong>${tree.adjustments.dob.score} points</strong> (${tree.adjustments.dob.description})</li>
                <li>Genre : <strong>${tree.adjustments.gender.score} points</strong> (${tree.adjustments.gender.description})</li>
                <li>Géographie : <strong>${tree.adjustments.geography.score} points</strong> (${tree.adjustments.geography.description})</li>
            </ul>
        `;
    } else {
        adjHtml = "<p>Aucun ajustement appliqué.</p>";
    }
    
    content.innerHTML = `
        <div class="modal-section">
            <h4>Informations Générales</h4>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 1rem; font-size: 0.85rem">
                <div>Horodatage : <strong>${escapeHtml(dateStr)}</strong></div>
                <div>Statut Décision : <strong style="color:${log.status === 'ALERT' ? 'var(--color-alert)' : 'var(--color-safe)'}">${log.status}</strong></div>
                <div>Fiche Watchlist : <strong><code>${escapeHtml(log.watchlist_id)}</code> - ${escapeHtml(log.watchlist_name)}</strong></div>
                <div>Client Criblé : <strong>${escapeHtml(log.client_name)} (${log.client_type})</strong></div>
            </div>
        </div>
        
        <div class="modal-section">
            <h4>Détail Algorithmique</h4>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 1rem; font-size: 0.85rem">
                <div>Score de Base Textuel : <strong>${log.base_score.toFixed(1)}%</strong></div>
                <div>Score Final Calculé : <strong>${log.final_score.toFixed(1)}%</strong></div>
            </div>
            <div style="margin-top:0.75rem; font-size:0.85rem">
                <h5>Ajustements Linéaires appliqués :</h5>
                ${adjHtml}
            </div>
        </div>
        
        <div class="modal-section">
            <h4>Version Watchlist & Intégrité</h4>
            <div style="font-size:0.85rem">
                Version de la Liste : <strong>${escapeHtml(log.watchlist_version)}</strong><br>
                SHA-256 Hash Liste : <code class="hash-badge" style="display:block; margin-top:0.25rem">${escapeHtml(log.watchlist_hash)}</code>
            </div>
        </div>
        
        <div class="modal-section">
            <h4>Paramètres de Configuration Utilisés</h4>
            <pre class="pre-block">${escapeHtml(JSON.stringify(configState, null, 2))}</pre>
        </div>
    `;
    
    modal.style.display = "block";
}

function closeAuditModal() {
    document.getElementById("audit-modal").style.display = "none";
}

// Fetch global config
async function fetchConfig() {
    try {
        const response = await fetch("/api/config");
        const configData = await response.json();
        console.log("Active configuration:", configData);
    } catch (e) {
        console.error("Error fetching config:", e);
    }
}

// Escape HTML utility
def_escape = {
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

// Window click to close modal
window.onclick = function(event) {
    const modal = document.getElementById("audit-modal");
    if (event.target == modal) {
        modal.style.display = "none";
    }
}
