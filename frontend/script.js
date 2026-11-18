/* =========================================================
   CLAIMCHECK AI - FRONTEND
   Backend: FastAPI (ClaimCheck AI, Google News RSS + GDELT)
   ========================================================= */

const API_BASE = "http://127.0.0.1:8000";

const STORAGE_KEY = "verificationResult";
const CLAIM_KEY = "lastClaim";
const HISTORY_KEY = "verificationHistoryList";
const MAX_HISTORY = 25;

let currentMode = "text";
let selectedImage = null;
let bothSelectedImage = null;


/* =========================================================
   DOM READY
   ========================================================= */

document.addEventListener("DOMContentLoaded", () => {

    initializeTheme();
    initializeTabs();
    initializeImageUpload();
    initializeButtons();

    if (document.getElementById("verdict")) {
        loadVerificationResult();
    }

    if (document.getElementById("historyContainer")) {
        initializeHistoryPage();
    }

});


/* =========================================================
   THEME
   ========================================================= */

function initializeTheme() {

    const themeBtn = document.getElementById("themeBtn");
    if (!themeBtn) return;

    const savedTheme = localStorage.getItem("claimcheckTheme");

    if (savedTheme === "dark") {
        document.body.classList.add("dark");
        themeBtn.textContent = "☀";
    } else {
        themeBtn.textContent = "☼";
    }

    themeBtn.addEventListener("click", () => {

        document.body.classList.toggle("dark");
        const isDark = document.body.classList.contains("dark");

        localStorage.setItem("claimcheckTheme", isDark ? "dark" : "light");
        themeBtn.textContent = isDark ? "☀" : "☼";

    });

}


/* =========================================================
   INPUT TABS
   ========================================================= */

function initializeTabs() {

    const tabs = document.querySelectorAll(".input-tab");
    if (!tabs.length) return;

    tabs.forEach(tab => {

        tab.addEventListener("click", () => {

            tabs.forEach(t => t.classList.remove("active"));
            tab.classList.add("active");

            currentMode = tab.dataset.mode || "text";
            updateInputMode();

        });

    });

}


function updateInputMode() {

    const textInput = document.getElementById("textInput");
    const imageInput = document.getElementById("imageInput");
    const bothInput = document.getElementById("bothInput");

    [textInput, imageInput, bothInput].forEach(el => {
        if (el) el.classList.add("hidden");
    });

    if (currentMode === "text" && textInput) {
        textInput.classList.remove("hidden");
    } else if (currentMode === "image" && imageInput) {
        imageInput.classList.remove("hidden");
    } else if (currentMode === "both" && bothInput) {
        bothInput.classList.remove("hidden");
    }

}


/* =========================================================
   IMAGE UPLOAD (handles both the "Image" tab and the
   "Text + Image" tab, which each have their own inputs)
   ========================================================= */

function initializeImageUpload() {

    wireDropZone({
        browseBtnId: "browseBtn",
        fileInputId: "imageFile",
        dropAreaId: "dropArea",
        previewContainerId: "imagePreview",
        previewImgId: "previewImage",
        nameId: "imageName",
        removeBtnId: "removeImage",
        onSelect: file => { selectedImage = file; },
        onRemove: () => { selectedImage = null; }
    });

    wireDropZone({
        browseBtnId: "bothBrowseBtn",
        fileInputId: "bothImageFile",
        dropAreaId: "bothDropArea",
        previewContainerId: "bothImagePreview",
        previewImgId: "bothPreviewImage",
        nameId: "bothImageName",
        removeBtnId: "removeBothImage",
        onSelect: file => { bothSelectedImage = file; },
        onRemove: () => { bothSelectedImage = null; }
    });

}


function wireDropZone(config) {

    const browseBtn = document.getElementById(config.browseBtnId);
    const fileInput = document.getElementById(config.fileInputId);
    const dropArea = document.getElementById(config.dropAreaId);
    const removeBtn = document.getElementById(config.removeBtnId);

    if (browseBtn && fileInput) {
        browseBtn.addEventListener("click", () => fileInput.click());
    }

    if (fileInput) {
        fileInput.addEventListener("change", event => {
            const file = event.target.files[0];
            if (file) handleImage(file, config);
        });
    }

    if (dropArea) {

        dropArea.addEventListener("dragover", event => {
            event.preventDefault();
            dropArea.classList.add("drag-over");
        });

        dropArea.addEventListener("dragleave", () => {
            dropArea.classList.remove("drag-over");
        });

        dropArea.addEventListener("drop", event => {
            event.preventDefault();
            dropArea.classList.remove("drag-over");
            const file = event.dataTransfer.files[0];
            if (file) handleImage(file, config);
        });

    }

    if (removeBtn) {
        removeBtn.addEventListener("click", () => removeSelectedImage(config));
    }

}


function handleImage(file, config) {

    if (!file.type.startsWith("image/")) {
        showToast("Please select a valid image.", "error");
        return;
    }

    if (file.size > 10 * 1024 * 1024) {
        showToast("Image must be smaller than 10 MB.", "error");
        return;
    }

    config.onSelect(file);

    const preview = document.getElementById(config.previewImgId);
    const previewContainer = document.getElementById(config.previewContainerId);
    const nameEl = document.getElementById(config.nameId);
    const dropArea = document.getElementById(config.dropAreaId);

    if (preview) preview.src = URL.createObjectURL(file);
    if (nameEl) nameEl.textContent = file.name;
    if (previewContainer) previewContainer.classList.remove("hidden");
    if (dropArea) dropArea.classList.add("hidden");

    showToast("Image ready.", "success");

}


function removeSelectedImage(config) {

    config.onRemove();

    const fileInput = document.getElementById(config.fileInputId);
    const preview = document.getElementById(config.previewImgId);
    const previewContainer = document.getElementById(config.previewContainerId);
    const dropArea = document.getElementById(config.dropAreaId);

    if (fileInput) fileInput.value = "";
    if (preview) preview.src = "";
    if (previewContainer) previewContainer.classList.add("hidden");
    if (dropArea) dropArea.classList.remove("hidden");

}


/* =========================================================
   BUTTONS
   ========================================================= */

function initializeButtons() {

    const verifyBtn = document.getElementById("verifyBtn");
    const clearBtn = document.getElementById("clearBtn");

    if (verifyBtn) {
        verifyBtn.addEventListener("click", event => {
            event.preventDefault();
            verifyClaim();
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener("click", event => {
            event.preventDefault();
            clearInputs();
        });
    }

}


/* =========================================================
   VERIFY CLAIM
   ========================================================= */

async function verifyClaim() {

    const claimInput = document.getElementById("claimText");
    const claimInputBoth = document.getElementById("claimTextBoth");

    const claim = currentMode === "both"
        ? cleanClaimText(claimInputBoth ? claimInputBoth.value : "")
        : cleanClaimText(claimInput ? claimInput.value : "");

    const image = currentMode === "both" ? bothSelectedImage : selectedImage;

    if ((currentMode === "text" || currentMode === "both") && !claim) {
        showToast("Please enter a claim first.", "error");
        (currentMode === "both" ? claimInputBoth : claimInput)?.focus();
        return;
    }

    if ((currentMode === "image" || currentMode === "both") && !image) {
        showToast("Please upload an image.", "error");
        return;
    }

    const verifyBtn = document.getElementById("verifyBtn");

    setButtonLoading(verifyBtn, true);
    showVerificationOverlay();

    try {

        let response;

        if (currentMode === "text") {

            response = await fetch(`${API_BASE}/verify`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ claim: claim, max_sources: 15 })
            });

        } else if (currentMode === "image") {

            const formData = new FormData();
            formData.append("file", image);

            response = await fetch(`${API_BASE}/verify/image`, {
                method: "POST",
                body: formData
            });

        } else if (currentMode === "both") {

            // The backend's /verify/multimodal endpoint reads
            // "claim" as a query parameter (it isn't declared as
            // a Form field), so it has to be sent on the URL while
            // the image travels in the multipart body.
            const formData = new FormData();
            if (image) formData.append("file", image);

            const url = `${API_BASE}/verify/multimodal?claim=${encodeURIComponent(claim)}`;

            response = await fetch(url, {
                method: "POST",
                body: formData
            });

        }

        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`Server error ${response.status}: ${errorText}`);
        }

        const data = await response.json();

        if (data && data.success === false) {
            throw new Error(data.error || "Verification failed.");
        }

        /*
         * The backend now actually runs OCR on uploaded images. If
         * it couldn't extract enough readable text, it returns a
         * real (UNVERIFIED) result rather than a special pending
         * status, so just let it flow through to the result page
         * like any other verdict.
         */

        const serializedResult = JSON.stringify(data);
        sessionStorage.setItem(STORAGE_KEY, serializedResult);
        localStorage.setItem(STORAGE_KEY, serializedResult);

        if (claim) {
            sessionStorage.setItem(CLAIM_KEY, claim);
            localStorage.setItem(CLAIM_KEY, claim);
        }

        saveToHistory(data);

        window.location.href = "result.html";

    } catch (error) {

        console.error("Verification failed:", error);
        hideVerificationOverlay();
        showToast(getFriendlyError(error), "error");

    } finally {

        setButtonLoading(verifyBtn, false);

    }

}


/* =========================================================
   CLEAN CLAIM
   ========================================================= */

function cleanClaimText(value) {

    if (!value) return "";
    let text = String(value).trim();

    if (text.startsWith('"') && text.endsWith('"')) {
        text = text.slice(1, -1);
    }

    return text.trim();

}


/* =========================================================
   CLEAR INPUTS
   ========================================================= */

function clearInputs() {

    const claimText = document.getElementById("claimText");
    const claimTextBoth = document.getElementById("claimTextBoth");

    if (claimText) claimText.value = "";
    if (claimTextBoth) claimTextBoth.value = "";

    removeSelectedImage({
        fileInputId: "imageFile",
        previewImgId: "previewImage",
        previewContainerId: "imagePreview",
        dropAreaId: "dropArea",
        onRemove: () => { selectedImage = null; }
    });

    removeSelectedImage({
        fileInputId: "bothImageFile",
        previewImgId: "bothPreviewImage",
        previewContainerId: "bothImagePreview",
        dropAreaId: "bothDropArea",
        onRemove: () => { bothSelectedImage = null; }
    });

    const tabs = document.querySelectorAll(".input-tab");

    tabs.forEach(tab => {
        tab.classList.remove("active");
        if (tab.dataset.mode === "text") tab.classList.add("active");
    });

    currentMode = "text";
    updateInputMode();

    showToast("Input cleared.", "success");

}


/* =========================================================
   LOADING BUTTON
   ========================================================= */

function setButtonLoading(button, loading) {

    if (!button) return;

    if (loading) {

        if (!button.dataset.originalText) {
            button.dataset.originalText = button.innerHTML;
        }

        button.disabled = true;
        button.innerHTML = `<span class="loading-spinner"></span> Verifying...`;

    } else {

        button.disabled = false;
        if (button.dataset.originalText) {
            button.innerHTML = button.dataset.originalText;
        }

    }

}


/* =========================================================
   FULL SCREEN VERIFICATION OVERLAY
   ========================================================= */

function showVerificationOverlay() {

    hideVerificationOverlay();

    const overlay = document.createElement("div");
    overlay.id = "verificationOverlay";
    overlay.className = "verification-overlay";

    overlay.innerHTML = `
        <div class="verification-modal">
            <div class="scan-animation">
                <div class="scan-ring"></div>
                <div class="scan-icon">🔍</div>
            </div>
            <div class="scan-badge">LIVE ANALYSIS</div>
            <h2>Verifying Claim</h2>
            <p>Searching sources and comparing evidence...</p>
            <div class="analysis-steps">
                <div class="analysis-step active"><span>✓</span> Claim extraction</div>
                <div class="analysis-step"><span>○</span> Source discovery</div>
                <div class="analysis-step"><span>○</span> Evidence comparison</div>
                <div class="analysis-step"><span>○</span> Verdict calculation</div>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    const steps = overlay.querySelectorAll(".analysis-step");

    steps.forEach((step, index) => {

        setTimeout(() => {

            if (!document.body.contains(overlay)) return;

            steps.forEach(s => s.classList.remove("active"));
            step.classList.add("active");

            const icon = step.querySelector("span");
            if (icon) icon.textContent = "✓";

        }, index * 900);

    });

}


function hideVerificationOverlay() {

    const overlay = document.getElementById("verificationOverlay");
    if (overlay) overlay.remove();

}


/* =========================================================
   RESULT PAGE
   ========================================================= */

function loadVerificationResult() {

    let stored = sessionStorage.getItem(STORAGE_KEY) || localStorage.getItem(STORAGE_KEY);

    if (!stored) {
        showEmptyResult();
        return;
    }

    let result;

    try {
        result = JSON.parse(stored);
    } catch (error) {
        console.error("Invalid stored result:", error);
        showEmptyResult();
        return;
    }

    if (!result || result.success === false) {
        showEmptyResult();
        return;
    }

    const claim = result.claim || localStorage.getItem(CLAIM_KEY) || "No claim provided";
    const verdict = normalizeVerdict(result.verdict);
    const confidence = normalizeConfidence(result);
    const summary = result.summary || "No explanation was returned by the verification engine.";

    const sources = Array.isArray(result.sources) ? result.sources : [];

    const supportingSources = sources.filter(s => s.evidence_type === "supporting");
    const contradictingSources = sources.filter(s => s.evidence_type === "contradicting");
    const contextualSources = sources.filter(s => s.evidence_type === "contextual");

    updateVerdict(verdict, confidence, summary);

    setText(["#claimFound"], claim);

    renderEvidenceAnalysis(supportingSources, contradictingSources, contextualSources, sources.length);
    renderKeyPoints(result, supportingSources, contradictingSources);
    renderSources(sources);
    renderVerificationSteps(result.verification_tasks);
    renderSourceAnalysis(result.source_analysis);

    hideVerificationOverlay();

}


/* =========================================================
   VERDICT
   ========================================================= */

function updateVerdict(verdict, confidence, explanation) {

    const verdictElement = document.getElementById("verdict");
    const description = document.getElementById("verdictDescription");
    const confidenceElement = document.getElementById("confidence");
    const icon = document.getElementById("verdictIcon");
    const circle = document.getElementById("confidenceCircle");

    if (verdictElement) {
        verdictElement.textContent = formatVerdict(verdict);
        verdictElement.dataset.verdict = verdict;
    }

    if (description) description.textContent = explanation;

    if (confidenceElement) {
        confidenceElement.textContent = confidence === null ? "—" : `${confidence}%`;
    }

    if (icon) icon.textContent = getVerdictIcon(verdict);

    if (circle && confidence !== null) {
        circle.style.setProperty("--confidence", `${confidence}%`);
        circle.setAttribute("data-confidence", confidence);
    }

    document.body.classList.remove(
        "verdict-true", "verdict-false", "verdict-mixed",
        "verdict-related_information", "verdict-unverified"
    );

    document.body.classList.add(`verdict-${verdict.toLowerCase()}`);

}


function normalizeVerdict(value) {

    if (!value) return "UNVERIFIED";

    const v = String(value).trim().toUpperCase();

    if (v === "TRUE") return "TRUE";
    if (v === "FALSE") return "FALSE";
    if (v === "MIXED") return "MIXED";
    if (v.includes("RELATED")) return "RELATED_INFORMATION";

    return "UNVERIFIED";

}


function formatVerdict(verdict) {

    switch (verdict) {
        case "TRUE": return "TRUE";
        case "FALSE": return "FALSE";
        case "MIXED": return "MIXED";
        case "RELATED_INFORMATION": return "RELATED INFORMATION";
        default: return "UNVERIFIED";
    }

}


function getVerdictIcon(verdict) {

    switch (verdict) {
        case "TRUE": return "✓";
        case "FALSE": return "✕";
        case "MIXED": return "⚖";
        case "RELATED_INFORMATION": return "ℹ";
        default: return "?";
    }

}


/*
 * The backend returns BOTH "confidence" (a 0-1 fraction) and
 * "confidence_percentage" (already scaled to 0-100). Always
 * prefer the percentage field so we never show "1%" for a
 * confidence of 0.62.
 */
function normalizeConfidence(result) {

    let value = result.confidence_percentage;

    if (value === undefined || value === null) {
        value = typeof result.confidence === "number" ? result.confidence * 100 : null;
    }

    if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return null;
    }

    return Math.max(0, Math.min(100, Math.round(Number(value))));

}


/* =========================================================
   EVIDENCE ANALYSIS (summary paragraph)
   ========================================================= */

function renderEvidenceAnalysis(supporting, contradicting, contextual, totalSources) {

    const container = document.getElementById("evidenceAnalysis");
    if (!container) return;

    if (!totalSources) {
        container.textContent = "No sources were retrieved for this claim.";
        return;
    }

    const parts = [];

    parts.push(
        supporting.length === 1
            ? "1 independent source supports the claim"
            : `${supporting.length} independent sources support the claim`
    );

    parts.push(
        contradicting.length === 1
            ? "1 source contradicts it"
            : `${contradicting.length} sources contradict it`
    );

    parts.push(`${contextual.length} additional source(s) provide related context`);

    container.textContent = `Out of ${totalSources} source(s) reviewed: ${parts.join(", ")}.`;

}


/* =========================================================
   KEY POINTS
   ========================================================= */

function renderKeyPoints(result, supporting, contradicting) {

    const container = document.getElementById("keyPoints");
    if (!container) return;

    container.innerHTML = "";

    const points = [];

    if (result.summary) points.push(result.summary);

    if (supporting.length > 0) {
        points.push(`${supporting.length} independent source(s) provided supporting evidence.`);
    }

    if (contradicting.length > 0) {
        points.push(`${contradicting.length} independent source(s) contained contradicting evidence.`);
    }

    const analysis = result.source_analysis;

    if (analysis) {

        if (analysis.independent_publishers !== undefined) {
            points.push(`Evidence was gathered from ${analysis.independent_publishers} independent publisher(s).`);
        }

        if (analysis.independent_story_clusters !== undefined) {
            points.push(`Sources formed ${analysis.independent_story_clusters} independent story cluster(s).`);
        }

        if (analysis.syndicated_duplicates) {
            points.push(`${analysis.syndicated_duplicates} syndicated duplicate article(s) were excluded from corroboration.`);
        }

    }

    if (!points.length) {
        points.push("No additional verification insights were returned.");
    }

    points.forEach(point => {
        const li = document.createElement("li");
        li.textContent = point;
        container.appendChild(li);
    });

}


/* =========================================================
   SOURCES
   ========================================================= */

const EVIDENCE_TYPE_LABEL = {
    supporting: "SUPPORTS",
    contradicting: "CONTRADICTS",
    contextual: "CONTEXT",
    unavailable: "UNAVAILABLE"
};

function renderSources(sources) {

    const container = document.getElementById("sourcesList");
    if (!container) return;

    container.innerHTML = "";

    if (!sources.length) {
        container.innerHTML = `
            <div class="source">
                <p>No sources were returned by the verification engine.</p>
            </div>
        `;
        return;
    }

    sources.forEach((source, index) => {
        container.appendChild(createSourceCard(source, index));
    });

}


function createSourceCard(source, index) {

    const card = document.createElement("div");
    card.className = "source";

    const title = source.title || "Untitled source";
    const publisher = source.publisher || source.publisher_domain || source.resolved_domain || "Unknown publisher";
    const domain = source.resolved_domain || source.publisher_domain || "";
    const quality = (source.source_quality || "unknown").toLowerCase();
    const evidenceType = source.evidence_type || (source.article_available ? "contextual" : "unavailable");
    const classificationLabel = EVIDENCE_TYPE_LABEL[evidenceType] || "OTHER";

    // The backend returns supporting/contradicting/context_evidence
    // as plain sentence strings per source (not objects), so read
    // them directly rather than looking for a nested ".excerpt".
    const excerpt =
        (source.supporting_evidence && source.supporting_evidence[0]) ||
        (source.contradicting_evidence && source.contradicting_evidence[0]) ||
        (source.context_evidence && source.context_evidence[0]) ||
        (source.article_available ? "" : "Article content could not be retrieved from this source.");

    const published = source.published || "";
    const url = source.resolved_url || source.url || "";

    const duplicateNote = source.is_duplicate
        ? `<p class="duplicate-note">Syndicated copy of "${escapeHTML(source.duplicate_of || "another source")}"</p>`
        : "";

    card.innerHTML = `
        <div class="source-head">
            <h3>${index + 1}. ${escapeHTML(title)}</h3>
            <span class="badge badge-${evidenceType}">${classificationLabel}</span>
        </div>
        <p class="source-meta">
            ${escapeHTML(publisher)}${domain ? " · " + escapeHTML(domain) : ""}
            <span class="badge badge-quality-${quality}">${escapeHTML(quality)} quality</span>
            ${published ? " · " + escapeHTML(formatDate(published)) : ""}
        </p>
        ${excerpt ? `<p>${escapeHTML(excerpt)}</p>` : ""}
        ${duplicateNote}
        ${url ? `<a href="${safeURL(url)}" target="_blank" rel="noopener noreferrer">Open Source ↗</a>` : ""}
    `;

    return card;

}


/* =========================================================
   VERIFICATION STEPS
   (backend returns an object of booleans, not an array)
   ========================================================= */

const TASK_LABELS = {
    claim_extraction: "Claim extraction",
    news_search: "News search",
    article_resolution: "Article resolution",
    article_extraction: "Article extraction",
    evidence_detection: "Evidence detection",
    syndication_detection: "Syndication detection",
    independent_source_analysis: "Independent source analysis",
    corroboration: "Corroboration",
    claim_decomposition: "AI claim decomposition",
    llm_reasoning: "AI-generated reasoning"
};

function renderVerificationSteps(tasks) {

    const container = document.getElementById("verificationSteps");
    if (!container) return;

    container.innerHTML = "";

    if (!tasks || typeof tasks !== "object" || !Object.keys(tasks).length) {
        container.innerHTML = `
            <div class="verification-step">
                <span>✓</span>
                <p>Verification completed.</p>
            </div>
        `;
        return;
    }

    Object.entries(tasks).forEach(([key, done]) => {

        const step = document.createElement("div");
        step.className = "verification-step";

        step.innerHTML = `
            <span>${done ? "✓" : "○"}</span>
            <p>${escapeHTML(TASK_LABELS[key] || formatTaskName(key))}</p>
        `;

        container.appendChild(step);

    });

}


/* =========================================================
   SOURCE ANALYSIS
   ========================================================= */

function renderSourceAnalysis(analysis) {

    const container = document.getElementById("sourceAnalysis");
    if (!container) return;

    if (!analysis || !Object.keys(analysis).length) {
        container.innerHTML = "<p>Source analysis information was not returned.</p>";
        return;
    }

    const publishers = Array.isArray(analysis.publishers) ? analysis.publishers : [];

    const qualityCounts = publishers.reduce((acc, p) => {
        const q = (p.quality || "unknown").toLowerCase();
        acc[q] = (acc[q] || 0) + 1;
        return acc;
    }, {});

    const items = [
        ["Total sources", analysis.total_sources],
        ["Independent sources", analysis.independent_sources],
        ["Independent publishers", analysis.independent_publishers],
        ["Independent story clusters", analysis.independent_story_clusters],
        ["Syndicated duplicates", analysis.syndicated_duplicates],
        ["High-quality publishers", qualityCounts.high || 0],
        ["Medium-quality publishers", qualityCounts.medium || 0],
        ["Lower-quality publishers", qualityCounts.lower || 0]
    ].filter(([, value]) => value !== undefined);

    container.innerHTML = `
        <ul class="key-points">
            ${items.map(([label, value]) => `<li><strong>${value}</strong> — ${escapeHTML(label)}</li>`).join("")}
        </ul>
    `;

}


/* =========================================================
   EMPTY RESULT
   ========================================================= */

function showEmptyResult() {

    setText(["#verdict"], "UNVERIFIED");
    setText(["#verdictDescription"], "No verification result was found. Please return to the home page and verify a claim.");
    setText(["#confidence"], "—");
    setText(["#claimFound"], "No claim found.");
    setText(["#evidenceAnalysis"], "No evidence is available.");

    const keyPoints = document.getElementById("keyPoints");
    if (keyPoints) {
        keyPoints.innerHTML = "<li>No verification result was returned.</li>";
    }

    const sources = document.getElementById("sourcesList");
    if (sources) {
        sources.innerHTML = `
            <div class="source">
                <p>No sources were returned by the verification engine.</p>
            </div>
        `;
    }

    const steps = document.getElementById("verificationSteps");
    if (steps) {
        steps.innerHTML = `
            <div class="verification-step">
                <span>!</span>
                <p>Start a new verification from the home page.</p>
            </div>
        `;
    }

    const analysis = document.getElementById("sourceAnalysis");
    if (analysis) {
        analysis.innerHTML = "<p>Source analysis will appear here.</p>";
    }

}


/* =========================================================
   VERIFICATION HISTORY
   ========================================================= */

function saveToHistory(result) {

    if (!result || result.success === false) return;

    let list = [];

    try {
        list = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
        if (!Array.isArray(list)) list = [];
    } catch {
        list = [];
    }

    list.unshift({
        id: `${Date.now()}`,
        claim: result.claim || "Untitled claim",
        verdict: normalizeVerdict(result.verdict),
        confidence: normalizeConfidence(result),
        timestamp: new Date().toISOString(),
        result: result
    });

    list = list.slice(0, MAX_HISTORY);

    try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
    } catch {
        // Storage full or unavailable — history is a nice-to-have,
        // don't block the verification flow on it.
    }

}


function getHistory() {

    try {
        const list = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
        return Array.isArray(list) ? list : [];
    } catch {
        return [];
    }

}


function initializeHistoryPage() {

    const searchInput = document.getElementById("historySearch");
    const clearBtn = document.getElementById("clearHistory");

    renderHistory(getHistory());

    if (searchInput) {
        searchInput.addEventListener("input", () => {
            const query = searchInput.value.trim().toLowerCase();
            const all = getHistory();
            const filtered = query
                ? all.filter(item => item.claim.toLowerCase().includes(query))
                : all;
            renderHistory(filtered);
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener("click", () => {
            localStorage.removeItem(HISTORY_KEY);
            renderHistory([]);
            showToast("History cleared.", "success");
        });
    }

}


function renderHistory(list) {

    const container = document.getElementById("historyContainer");
    if (!container) return;

    if (!list.length) {
        container.innerHTML = `
            <div class="empty-history">
                <div class="empty-icon">📋</div>
                <h2>No Verification History</h2>
                <p>Once you verify a claim, your results will appear here.</p>
                <a href="index.html" class="primary-btn">Verify Your First Claim</a>
            </div>
        `;
        return;
    }

    container.innerHTML = "";

    list.forEach(item => {

        const el = document.createElement("div");
        el.className = "history-item";

        el.innerHTML = `
            <div>
                <h3>${escapeHTML(item.claim)}</h3>
                <p>
                    <span class="badge badge-${(item.verdict || "").toLowerCase()}">${escapeHTML(formatVerdict(item.verdict))}</span>
                    ${item.confidence !== null ? ` · ${item.confidence}% confidence` : ""}
                    · ${escapeHTML(formatDate(item.timestamp))}
                </p>
            </div>
            <button type="button" class="secondary-btn">View</button>
        `;

        el.querySelector("button").addEventListener("click", () => {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify(item.result));
            localStorage.setItem(STORAGE_KEY, JSON.stringify(item.result));
            localStorage.setItem(CLAIM_KEY, item.claim);
            window.location.href = "result.html";
        });

        container.appendChild(el);

    });

}


/* =========================================================
   GENERIC TEXT SETTER
   ========================================================= */

function setText(selectors, value) {

    selectors.forEach(selector => {
        const element = document.querySelector(selector);
        if (element) element.textContent = value;
    });

}


/* =========================================================
   DATE FORMAT
   ========================================================= */

function formatDate(dateString) {

    if (!dateString) return "";

    const date = new Date(dateString);

    if (Number.isNaN(date.getTime())) return dateString;

    return date.toLocaleDateString(undefined, {
        day: "2-digit",
        month: "short",
        year: "numeric"
    });

}


/* =========================================================
   TASK NAME
   ========================================================= */

function formatTaskName(task) {

    if (!task) return "Verification step";

    return String(task)
        .replaceAll("_", " ")
        .replace(/\b\w/g, char => char.toUpperCase());

}


/* =========================================================
   ESCAPE HTML
   ========================================================= */

function escapeHTML(value) {

    if (value === null || value === undefined) return "";

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

}


/* =========================================================
   SAFE URL
   ========================================================= */

function safeURL(url) {

    try {

        const parsed = new URL(url);

        if (parsed.protocol === "http:" || parsed.protocol === "https:") {
            return parsed.href;
        }

    } catch {
        // Invalid URL
    }

    return "#";

}


/* =========================================================
   TOAST
   ========================================================= */

function showToast(message, type = "info") {

    let container = document.getElementById("toastContainer");

    if (!container) {
        container = document.createElement("div");
        container.id = "toastContainer";
        container.className = "toast-container";
        document.body.appendChild(container);
    }

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;

    const icon = type === "success" ? "✓" : type === "error" ? "!" : "i";

    toast.innerHTML = `
        <span class="toast-icon">${icon}</span>
        <span class="toast-message">${escapeHTML(message)}</span>
    `;

    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add("toast-hide");
        setTimeout(() => toast.remove(), 300);
    }, 4000);

}


/* =========================================================
   FRIENDLY ERROR
   ========================================================= */

function getFriendlyError(error) {

    const message = error?.message || "";

    if (message.includes("Failed to fetch")) {
        return "Cannot connect to the verification server. Make sure the FastAPI backend is running on http://127.0.0.1:8000";
    }

    if (message.includes("500")) {
        return "The verification server encountered an error. Check the FastAPI terminal for details.";
    }

    if (message.includes("404")) {
        return "Verification API endpoint was not found.";
    }

    if (message.includes("422")) {
        return "The server rejected the request format. Please try again.";
    }

    return message || "Verification failed. Please try again.";

}


/* =========================================================
   DEBUG HELPER
   ========================================================= */

window.ClaimCheckDebug = {

    getStoredResult() {
        const value = sessionStorage.getItem(STORAGE_KEY);
        if (!value) return null;
        try {
            return JSON.parse(value);
        } catch {
            return null;
        }
    },

    clearResult() {
        sessionStorage.removeItem(STORAGE_KEY);
        localStorage.removeItem(STORAGE_KEY);
    },

    testAPI() {
        fetch(`${API_BASE}/health`)
            .then(r => r.json())
            .then(data => console.log("API is working:", data))
            .catch(error => console.error("API connection failed:", error));
    }

};
