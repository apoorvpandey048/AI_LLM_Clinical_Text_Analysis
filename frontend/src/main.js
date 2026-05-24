/**
 * SNAP-AI Frontend Application v2
 *
 * Features:
 * - Real-time LLM streaming with typewriter effect
 * - Custom prompt template editor with versioning
 * - Model selection & management (Clinical Camel, Me-LLaMA, BioMistral-7B, etc.)
 * - File / text upload and processing
 * - Results display in table and card views
 */

// ============================================
// Configuration
// ============================================

const API_BASE = '/api/v1';
const POLL_INTERVAL_MS = 2000;

// ============================================
// Auth State
// ============================================

const authState = {
    token: localStorage.getItem('snapai_token'),
    user: JSON.parse(localStorage.getItem('snapai_user') || 'null'),
};

/**
 * Get auth headers for API calls.
 */
function getAuthHeaders() {
    const headers = {};
    if (authState.token) {
        headers['Authorization'] = `Bearer ${authState.token}`;
    }
    return headers;
}

/**
 * Unwrap API response envelope.
 * Backend wraps data in { success, data, error, meta }.
 * Extracts the `data` field or returns the raw response for backward compatibility.
 */
function unwrapApiResponse(json) {
    return json?.data || json;
}

/**
 * Parse JSON, check HTTP status, and unwrap API response in one step.
 * Throws on non-OK responses with the server's error message.
 */
async function safeJson(res) {
    const json = await res.json();
    if (!res.ok) throw new Error(extractErrorMessage(json) || `API Error (${res.status})`);
    return unwrapApiResponse(json);
}

/**
 * Extract a human-readable error string from any API error shape.
 * Handles: string, {error: "..."}, {detail: "..."}, {message: "..."},
 * {error: {message: "..."}}, and prevents [object Object].
 */
// `extractErrorMessage` implementation appears later in the file with a
// more comprehensive handler (validation arrays, nested error objects, etc.).
// Keep the single comprehensive implementation to avoid duplicate declarations.

/**
 * Safely attempt to parse JSON, returning null on failure.
 * Use for extracting error bodies from known-failed responses.
 */
async function tryJson(res) {
    try {
        return await res.json();
    } catch {
        return null;
    }
}

/**
 * Authenticated fetch wrapper.
 * Deduplicates 401 handling so multiple parallel calls don't spam the user.
 */
let _logoutInProgress = false;
async function authFetch(url, options = {}) {
    const headers = { ...getAuthHeaders(), ...(options.headers || {}) };
    const res = await fetch(url, { ...options, headers });
    if (res.status === 401) {
        // Token expired or invalid — only show one toast
        if (!_logoutInProgress) {
            _logoutInProgress = true;
            handleLogout();
            // Reset flag after a short delay so future real 401s still work
            setTimeout(() => { _logoutInProgress = false; }, 2000);
        }
        throw new Error('Session expired. Please log in again.');
    }
    return res;
}

// Recommended clinical models for the dropdown
const RECOMMENDED_MODELS = [
    { name: 'gpt-oss-120b', desc: 'GPT OSS 120B — Large open-source model' },
    { name: 'clinical-camel:70b', desc: 'Clinical Camel 70B — LLaMA-2 fine-tuned for clinical research' },
    { name: 'clinical-camel:13b', desc: 'Clinical Camel 13B — Efficient clinical LLM' },
    { name: 'me-llama:70b-chat', desc: 'Me-LLaMA 70B Chat — Medical LLM from LLaMA-2' },
    { name: 'me-llama:13b-chat', desc: 'Me-LLaMA 13B Chat — Medical LLM (13B)' },
    { name: 'biomistral:7b', desc: 'BioMistral 7B — Biomedical Mistral variant' },
    { name: 'llama3:8b', desc: 'LLaMA 3 8B — General purpose' },
    { name: 'mistral:7b', desc: 'Mistral 7B — Fast general model' },
    { name: 'qwen2.5:14b', desc: 'Qwen 2.5 14B — Multilingual model' },
];

// Layer metadata — dynamically updated from /api/v1/prompts/layers
let LAYERS = {
    layer1_ctp: { label: 'Layer 1 — Clinical Text Pre-Processor (CTP)', shortLabel: 'Layer 1: CTP', number: 1, is_builtin: true },
    layer2_cie: { label: 'Layer 2 — Complication Info Extraction (CIE)', shortLabel: 'Layer 2: CIE', number: 2, is_builtin: true },
    layer3_ccc: { label: 'Layer 3 — CCI Calculation & Cross-Check (CCC)', shortLabel: 'Layer 3: CCC', number: 3, is_builtin: true },
};
const BUILTIN_LAYERS = ['layer1_ctp', 'layer2_cie', 'layer3_ccc'];

// ============================================
// State
// ============================================

const state = {
    selectedFiles: [],
    textContent: '',
    currentJobId: null,
    pollInterval: null,
    isProcessing: false,
    eventSource: null,
    currentView: 'table',

    // Streaming state (dynamically keyed by available layers)
    liveOutputBuffers: {},
    // Preserved streaming history per case
    streamingHistory: {}, // { caseNumber: { layer_name: '' } }
    activeLiveTab: 'layer1_ctp',
    currentStreamingLayer: null,
    currentCaseNumber: 1,

    // Connection state
    connectionStatus: 'unknown', // 'connected', 'reconnecting', 'disconnected'
    reconnectAttempts: 0,
    maxReconnectAttempts: 5,

    // Completed case tracking — prevents SSE tokens from overwriting finished outputs
    frozenCases: new Set(),

    // Execution timeline per case — keyed by layer_id, never by index
    // { layer_id: { label, status, startTime, endTime, durationMs, errorMessage } }
    layerTimeline: {},
    // Pipeline snapshot fetched from server after job completes
    pipelineSnapshot: null,

    // Last completed job ID (retained so replay/baseline buttons work after processing resets currentJobId)
    lastCompletedJobId: null,

    // Prompt state
    activePromptLayer: 'layer1_ctp',
    promptData: {},       // { layer: { content, source, version } }
    promptDirty: false,

    // Model state
    activeModel: null,
    availableModels: [],

    // OpenRouter state
    openRouterEnabled: false,
    openRouterKey: null,

    // System info
    systemInfo: null,
};

// ============================================
// DOM Ready
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    // Check auth state first
    if (authState.token && authState.user) {
        hideAuthOverlay();
        initApp();
    } else {
        showAuthOverlay();
    }
});

function initApp() {
    updateUserDisplay();
    initTabs();
    initDropzone();
    initTextInput();
    initProcessButton();
    initExportActions();
    initPromptEditor();
    initLiveOutputTabs();
    initLockedConfigActions();
    loadModels();
    loadSystemInfo();
    checkVllmHealth();
    // Poll vLLM health every 60 seconds
    setInterval(checkVllmHealth, 60000);
    loadLayers().then(() => loadAllPrompts());
    initLogViewer();
    if (authState.user && authState.user.role === 'admin') {
        document.getElementById('nav-admin')?.classList.remove('hidden');
        document.getElementById('nav-ops')?.classList.remove('hidden');
        loadAdminUsers();
    }
    
    initOpenRouterToggle();

    // Role-based visibility: hide Prompts tab for non-admin (supplementary — backend enforces)
    if (authState.user?.role !== 'admin') {
        document.getElementById('nav-prompts')?.classList.add('hidden');
    }

    // Initialize Lucide icons for static elements
    if (window.lucide) lucide.createIcons();

    // Auto-reload last job results if the user refreshes the page
    const savedJobId = localStorage.getItem('snapai_last_job_id');
    if (savedJobId) {
        // Silently re-fetch — show results section if successful
        authFetch(`${API_BASE}/jobs/${savedJobId}/results`).then(async res => {
            if (!res.ok) return;
            const data = unwrapApiResponse(await res.json());
            if (data && (data.results || data.cases)) {
                window.currentResults = data;
                state.currentJobId = savedJobId;
                renderResults(data);

                // Restore full UI state (snapshot, locked config, timeline, metrics, compare)
                state.pipelineSnapshot = data.pipeline_snapshot || null;
                renderSnapshotViewer(data.pipeline_snapshot, data);
                renderLockedConfig(data);

                if (data.pipeline_snapshot && !Object.keys(state.layerTimeline).length) {
                    data.pipeline_snapshot.forEach(layer => {
                        state.layerTimeline[layer.layer_name] = {
                            label: layer.label || layer.layer_name,
                            status: 'COMPLETED',
                            startTime: null,
                            endTime: null,
                            durationMs: null,
                            errorMessage: null,
                        };
                    });
                }
                renderExecutionTimeline();
                loadJobMetrics(savedJobId);
                populateCompareSelect(savedJobId);

                document.getElementById('upload-section').classList.add('hidden');
                document.getElementById('results-section').classList.remove('hidden');
            }
        }).catch(() => { /* ignore — silent reload only */ });
    }

    console.log('SNAP-AI Frontend v2 initialized');
}

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const icons = {
        info: '<i data-lucide="info"></i>',
        success: '<i data-lucide="check-circle"></i>',
        error: '<i data-lucide="alert-circle"></i>',
        warning: '<i data-lucide="alert-triangle"></i>'
    };

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <span class="toast-message">${escapeHtml(message)}</span>
    <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
  `;
    container.appendChild(toast);
    if (window.lucide) lucide.createIcons({ props: { size: 18 } });
    setTimeout(() => toast.remove(), 5000);
}

// ============================================
// Job Actions: Replay & Baseline
// ============================================

/**
 * Resolve the best available job ID for replay/baseline actions.
 * Priority: state.lastCompletedJobId > state.currentJobId > localStorage fallback.
 */
function _resolveJobId() {
    return state.lastCompletedJobId
        || state.currentJobId
        || localStorage.getItem('snapai_last_job_id')
        || null;
}

/**
 * Bind a click handler to a button with duplicate-listener prevention.
 * Uses a data-bound attribute as a guard so calling initLockedConfigActions
 * multiple times (e.g. after re-login) never stacks listeners.
 */
function _bindOnce(el, handler) {
    if (!el || el.dataset.bound) return;
    el.dataset.bound = '1';
    el.addEventListener('click', handler);
}

function initLockedConfigActions() {
    // Locked-config panel buttons
    _bindOnce(document.getElementById('replay-this-run-btn'), () => {
        const jobId = _resolveJobId();
        if (jobId) replayJob(jobId);
        else showToast('No job available to replay', 'warning');
    });

    _bindOnce(document.getElementById('mark-baseline-btn'), () => {
        const jobId = _resolveJobId();
        if (jobId) markBaseline(jobId);
        else showToast('No job available to mark as baseline', 'warning');
    });

    // Bottom "Run Actions" buttons (same logic, different DOM elements)
    _bindOnce(document.getElementById('replay-btn-bottom'), () => {
        const jobId = _resolveJobId();
        if (jobId) replayJob(jobId);
        else showToast('No job available to replay', 'warning');
    });

    _bindOnce(document.getElementById('baseline-btn-bottom'), () => {
        const jobId = _resolveJobId();
        if (jobId) markBaseline(jobId);
        else showToast('No job available to mark as baseline', 'warning');
    });
}

/**
 * Set disabled state on all baseline buttons (top + bottom).
 */
function _setBaselineButtonsDisabled(disabled) {
    ['mark-baseline-btn', 'baseline-btn-bottom'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = disabled;
    });
}

/**
 * Update the visual state of all baseline buttons after a toggle.
 */
function _updateBaselineButtonsUI(isBaseline) {
    ['mark-baseline-btn', 'baseline-btn-bottom'].forEach(id => {
        const btn = document.getElementById(id);
        if (!btn) return;
        if (isBaseline) {
            btn.classList.add('baseline-active');
            btn.innerHTML = `<i data-lucide="flag"></i> Baseline <span class="status-dot"></span>`;
        } else {
            btn.classList.remove('baseline-active');
            btn.innerHTML = `<i data-lucide="flag" class="btn-icon"></i> Mark as Baseline`;
        }
    });
    if (window.lucide) lucide.createIcons();
}

async function markBaseline(jobId) {
    if (!jobId) {
        showToast('No job selected', 'warning');
        return;
    }

    _setBaselineButtonsDisabled(true);

    try {
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/mark-baseline`, {
            method: 'POST',
        });

        const data = await safeJson(res);

        if (!data) {
            showToast('Failed to update baseline', 'error');
            return;
        }

        _updateBaselineButtonsUI(data.is_regression_baseline);
        showToast(
            data.is_regression_baseline ? 'Run marked as baseline' : 'Baseline removed',
            data.is_regression_baseline ? 'success' : 'info',
        );

        // Refresh state so Ops page reflects the baseline
        await loadSystemInfo();
    } catch (err) {
        const msg = err instanceof Error ? err.message : extractErrorMessage(err);
        showToast(`Baseline update failed: ${msg || 'Unknown error'}`, 'error');
    } finally {
        _setBaselineButtonsDisabled(false);
    }
}

// ============================================
// Page Navigation
// ============================================

function switchPage(page) {
    // Role guard: block non-admin from admin/ops/prompts pages
    if ((page === 'admin' || page === 'operations') && authState.user?.role !== 'admin') {
        showToast('Admin access required', 'warning');
        return;
    }
    if (page === 'prompts' && authState.user?.role !== 'admin') {
        showToast('Admin access required', 'warning');
        return;
    }
    // Load page-specific data
    if (page === 'operations') loadOperationsDashboard();
    if (page === 'saved') loadSavedCases();

    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));

    const pageEl = document.getElementById(`page-${page}`);
    const btnEl = document.querySelector(`.nav-btn[data-page="${page}"]`);
    if (pageEl) pageEl.classList.add('active');
    if (btnEl) btnEl.classList.add('active');
}

// ============================================
// Tab Switching (File / Text)
// ============================================

function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            document.getElementById('file-tab').classList.toggle('active', tab === 'file');
            document.getElementById('text-tab').classList.toggle('active', tab === 'text');
            updateProcessButton();
        });
    });
}

// ============================================
// Dropzone
// ============================================

function initDropzone() {
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('file-input');

    dropzone.addEventListener('click', () => {
        if (!state.isProcessing) fileInput.click();
    });

    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (!state.isProcessing) dropzone.classList.add('dragover');
    });

    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));

    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        if (state.isProcessing) return;
        const droppedFiles = Array.from(e.dataTransfer.files);
        const allowedExts = ['.docx', '.pdf', '.txt', '.xlsx'];
        const validFiles = droppedFiles.filter(f => allowedExts.some(ext => f.name.toLowerCase().endsWith(ext)));
        const invalidFiles = droppedFiles.filter(f => !allowedExts.some(ext => f.name.toLowerCase().endsWith(ext)));
        if (invalidFiles.length > 0) {
            showToast(`Skipped ${invalidFiles.length} unsupported file(s). Allowed: .docx, .pdf, .txt, .xlsx`, 'warning');
        }
        if (validFiles.length > 0) {
            handleFilesSelect(validFiles);
        } else if (invalidFiles.length > 0) {
            showToast('No supported files dropped. Allowed: .docx, .pdf, .txt, .xlsx', 'error');
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) handleFilesSelect(Array.from(e.target.files));
    });
}

function handleFilesSelect(newFiles) {
    // Merge with existing selection (avoid duplicates by name)
    const existingNames = new Set(state.selectedFiles.map(f => f.name));
    for (const f of newFiles) {
        if (!existingNames.has(f.name)) {
            state.selectedFiles.push(f);
            existingNames.add(f.name);
        }
    }
    renderFileInfo();
    updateProcessButton();
}

function renderFileInfo() {
    const info = document.getElementById('file-info');
    if (state.selectedFiles.length === 0) {
        info.classList.add('hidden');
        return;
    }
    info.classList.remove('hidden');
    const fileChips = state.selectedFiles.map((f, idx) => `
        <span class="file-chip">
            <i data-lucide="file"></i> ${escapeHtml(f.name)} (${formatFileSize(f.size)})
            <button class="file-chip-remove" onclick="removeFile(${idx})">&times;</button>
        </span>
    `).join('');
    info.innerHTML = `
        <div class="file-chips">${fileChips}</div>
        <button class="btn btn-sm btn-outline" onclick="clearFile()">Remove All</button>
    `;
    if (window.lucide) lucide.createIcons();
}

function removeFile(idx) {
    state.selectedFiles.splice(idx, 1);
    renderFileInfo();
    updateProcessButton();
}

function clearFile() {
    state.selectedFiles = [];
    document.getElementById('file-info').classList.add('hidden');
    document.getElementById('file-input').value = '';
    updateProcessButton();
}

// ============================================
// Text Input
// ============================================

function initTextInput() {
    document.getElementById('text-input').addEventListener('input', (e) => {
        state.textContent = e.target.value;
        updateProcessButton();
    });
}

// ============================================
// Process Button
// ============================================

function initProcessButton() {
    document.getElementById('process-btn').addEventListener('click', async () => {
        if (state.isProcessing) return;
        const activeTab = document.querySelector('.tab-btn.active').dataset.tab;
        if (activeTab === 'file' && state.selectedFiles.length > 0) {
            await processFiles(state.selectedFiles);
        } else if (activeTab === 'text' && state.textContent.trim()) {
            await processText(state.textContent);
        }
    });
}

function updateProcessButton() {
    const btn = document.getElementById('process-btn');
    const activeTab = document.querySelector('.tab-btn.active').dataset.tab;
    const hasFile = activeTab === 'file' && state.selectedFiles.length > 0;
    const hasText = activeTab === 'text' && state.textContent.trim();
    btn.disabled = state.isProcessing || (!hasFile && !hasText);
}

/**
 * Enable or disable the Cancel Job button.
 * Cancel is only valid while a job is actively RUNNING.
 */
function setCancelButtonEnabled(enabled) {
    const btn = document.getElementById('cancel-job-btn');
    if (btn) btn.disabled = !enabled;
}

function setResumeButtonEnabled(enabled) {
    const btn = document.getElementById('resume-job-btn');
    if (btn) btn.disabled = !enabled;
}

// ============================================
// API: Process File / Text
// ============================================

async function processFiles(files) {
    // GPU safety check
    if (state.systemInfo?.gpu_busy) {
        showToast('GPU resources currently busy. Please retry shortly.', 'warning');
        return;
    }

    const formData = new FormData();
    files.forEach(file => formData.append('files', file));
    if (state.openRouterEnabled && state.openRouterKey) {
        formData.append('backend', 'openrouter');
        formData.append('openrouter_key', state.openRouterKey);
    }

    try {
        state.isProcessing = true;
        updateProcessButton();
        showProgress();

        const res = await authFetch(`${API_BASE}/upload`, { method: 'POST', body: formData });
        if (!res.ok) throw new Error(`Upload failed: ${res.status}`);

        const data = unwrapApiResponse(await res.json());
        if (data.warnings && data.warnings.length > 0) {
            data.warnings.forEach(w => showToast(w, 'warning'));
        }
        state.currentJobId = data.job_id;
        initializeProgressUI(data.case_count || 1);
        startStreaming(data.job_id);
        showToast(data.message || `Processing ${data.case_count || 1} case(s)...`, 'info');
    } catch (err) {
        showError(err.message);
        state.isProcessing = false;
        updateProcessButton();
    }
}

async function processText(text) {
    // GPU safety check
    if (state.systemInfo?.gpu_busy) {
        showToast('GPU resources currently busy. Please retry shortly.', 'warning');
        return;
    }

    try {
        state.isProcessing = true;
        updateProcessButton();
        showProgress();

        const reqBody = { text };
        if (state.openRouterEnabled && state.openRouterKey) {
            reqBody.backend = 'openrouter';
            reqBody.openrouter_key = state.openRouterKey;
        }

        const res = await authFetch(`${API_BASE}/upload/text`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reqBody),
        });
        if (!res.ok) throw new Error(`Processing failed: ${res.status}`);

        const data = unwrapApiResponse(await res.json());
        state.currentJobId = data.job_id;
        initializeProgressUI(data.case_count || 1);
        startStreaming(data.job_id);
        showToast(`Processing ${data.case_count || 1} case(s)...`, 'info');
    } catch (err) {
        showError(err.message);
        state.isProcessing = false;
        updateProcessButton();
    }
}

// ============================================
// SSE Streaming (Typewriter Effect) with Auto-Reconnect
// ============================================

function startStreaming(jobId) {
    closeStream();
    resetLiveOutput();
    state.reconnectAttempts = 0;
    state.streamingHistory = {};
    state.currentCaseNumber = 1;
    state.frozenCases = new Set();
    state.layerTimeline = {};
    state.pipelineSnapshot = null;
    state.lastResumableJobId = jobId;  // remember for the Resume control
    state.lastProgress = null;
    setCancelButtonEnabled(true);
    setResumeButtonEnabled(false);

    // Pre-populate timeline with PENDING entries for all known layers
    Object.keys(LAYERS).forEach(key => {
        state.layerTimeline[key] = {
            label: LAYERS[key].label || key,
            status: 'PENDING',
            startTime: null,
            endTime: null,
            durationMs: null,
            errorMessage: null,
        };
    });
    renderExecutionTimeline();

    connectSSE(jobId);
}

function connectSSE(jobId) {
    const url = `${API_BASE}/stream/${jobId}`;
    state.eventSource = new EventSource(url);

    const statusEl = document.getElementById('live-output-status');
    statusEl.classList.add('streaming');
    statusEl.innerHTML = '<span class="live-dot"></span> Streaming...';
    updateConnectionStatus('connected');

    state.eventSource.addEventListener('token', (e) => {
        try {
            const data = JSON.parse(e.data);
            // Update LAYERS metadata from SSE label if available
            const layerKey = normalizeLayerKey(data.layer);
            if (data.label && layerKey && LAYERS[layerKey]) {
                LAYERS[layerKey].label = data.label;
                LAYERS[layerKey].shortLabel = data.label.split('—')[0]?.trim() || data.label.substring(0, 20);
            }
            appendToken(data.layer, data.token, data.case_number);
        } catch { /* ignore parse errors */ }
    });

    state.eventSource.addEventListener('layer_start', (e) => {
        try {
            const data = JSON.parse(e.data);
            const layerKey = normalizeLayerKey(data.layer);
            state.currentCaseNumber = data.case_number || state.currentCaseNumber;
            // Update LAYERS metadata from SSE label
            if (data.label && layerKey) {
                if (!LAYERS[layerKey]) {
                    LAYERS[layerKey] = { label: data.label, shortLabel: data.label.split('—')[0]?.trim() || data.label.substring(0, 20), number: Object.keys(LAYERS).length + 1, is_builtin: false };
                    renderDynamicLiveOutputTabs();
                    renderDynamicLayerSteps();
                } else {
                    LAYERS[layerKey].label = data.label;
                    LAYERS[layerKey].shortLabel = data.label.split('—')[0]?.trim() || data.label.substring(0, 20);
                }
            }
            // Timeline: mark layer as RUNNING
            if (layerKey) {
                if (!state.layerTimeline[layerKey]) {
                    state.layerTimeline[layerKey] = { label: data.label || layerKey, status: 'PENDING', startTime: null, endTime: null, durationMs: null, errorMessage: null };
                }
                state.layerTimeline[layerKey].status = 'RUNNING';
                state.layerTimeline[layerKey].startTime = data.timestamp ? new Date(data.timestamp) : new Date();
                state.layerTimeline[layerKey].label = data.label || state.layerTimeline[layerKey].label;
                updateTimelineRow(layerKey);
            }
            setActiveLayer(layerKey);
            updateCurrentCaseInfo(data.case_number, null, layerKey);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('layer_complete', (e) => {
        try {
            const data = JSON.parse(e.data);
            // Update LAYERS metadata from SSE label
            const layerKey = normalizeLayerKey(data.layer);
            if (data.label && layerKey && LAYERS[layerKey]) {
                LAYERS[layerKey].label = data.label;
            }
            // Timeline: mark layer as COMPLETED or FAILED
            if (layerKey && state.layerTimeline[layerKey]) {
                const entry = state.layerTimeline[layerKey];
                entry.status = data.success === false ? 'FAILED' : 'COMPLETED';
                entry.endTime = data.timestamp ? new Date(data.timestamp) : new Date();
                entry.durationMs = data.duration_ms || (entry.startTime ? (entry.endTime - entry.startTime) : null);
                if (data.error_message) entry.errorMessage = data.error_message;
                updateTimelineRow(layerKey);
            }
            markLayerComplete(data.layer);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('case_complete', (e) => {
        try {
            const data = JSON.parse(e.data);
            const finishedCase = data.case_number || state.currentCaseNumber;
            // Freeze this case: SSE tokens must not overwrite completed outputs
            state.frozenCases.add(finishedCase);
            // Preserve streaming history for this case before resetting
            preserveStreamingHistory(finishedCase);
            resetLiveOutput();
            updateCaseProgress(data);
            state.currentCaseNumber = finishedCase + 1;

            // — Live streaming: append completed case card —
            renderLiveCaseCard(data, jobId);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('progress', (e) => {
        try {
            const data = JSON.parse(e.data);
            // A healthy event proves the connection works — refresh the reconnect budget so a
            // multi-hour batch with intermittent drops never exhausts it (streaming hardening).
            state.reconnectAttempts = 0;
            state.lastProgress = data;
            updateProgressUI(data);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('complete', (e) => {
        // Preserve final case streaming history
        preserveStreamingHistory(state.currentCaseNumber);
        closeStream();
        setCancelButtonEnabled(false);
        statusEl.classList.remove('streaming');
        statusEl.innerHTML = '<span class="live-dot"></span> Complete';
        updateConnectionStatus('connected');

        // Reset processing state BEFORE delegating to loadResults
        // (defense-in-depth: loadResults is async and may fail)
        console.debug('[SSE_RECV complete] resetting processing state');
        state.lastCompletedJobId = state.currentJobId || jobId;
        state.isProcessing = false;
        state.currentJobId = null;
        updateProcessButton();

        let resolvedJobId = jobId;
        try {
            const data = JSON.parse(e.data);
            resolvedJobId = data.job_id || jobId;
        } catch { /* use outer jobId */ }
        loadResults(resolvedJobId);
        // Partial completion (some cases FAILED) — offer Resume to re-run just those.
        if (state.lastProgress && (state.lastProgress.failed || 0) > 0) {
            setResumeButtonEnabled(true);
            showToast(`Completed with ${state.lastProgress.failed} failed case(s) — Resume to retry`, 'warning');
        } else {
            showToast('Processing complete!', 'success');
        }
        scheduleStallCheck();
    });

    state.eventSource.addEventListener('job_failed', (e) => {
        // Backend explicitly signals job failure — exit processing immediately
        // Guard: ignore duplicate events
        if (!state.isProcessing && !state.currentJobId) return;
        closeStream();
        setCancelButtonEnabled(false);
        statusEl.classList.remove('streaming');
        statusEl.innerHTML = '<span class="live-dot error"></span> Failed';
        state.isProcessing = false;
        state.currentJobId = null;
        updateProcessButton();
        setResumeButtonEnabled(true);  // crash/failure -> Resume re-runs unfinished cases
        try {
            const data = JSON.parse(e.data);
            showProcessingError(data.error || 'Pipeline failed', data);
        } catch {
            showProcessingError('Pipeline failed — check server logs');
        }
        scheduleStallCheck();
    });

    state.eventSource.addEventListener('timeout', () => {
        // Server closed the stream after its max lifetime (SSE_STREAM_TIMEOUT) — NOT an error.
        // Re-subscribe transparently so a multi-hour batch keeps streaming live tokens.
        if (state.isProcessing) {
            state.reconnectAttempts = 0;
            closeStream();
            connectSSE(jobId);
        }
    });

    state.eventSource.addEventListener('error', () => {
        // SSE can auto-reconnect; only treat as fatal if readyState is CLOSED
        if (state.eventSource && state.eventSource.readyState === EventSource.CLOSED) {
            handleSSEDisconnect(jobId);
        }
    });

    // Also poll as fallback (less frequently)
    state.pollInterval = setInterval(() => pollJobStatus(jobId), POLL_INTERVAL_MS * 2);
}

function handleSSEDisconnect(jobId) {
    const statusEl = document.getElementById('live-output-status');

    if (state.reconnectAttempts < state.maxReconnectAttempts) {
        state.reconnectAttempts++;
        const backoffMs = Math.min(1000 * Math.pow(2, state.reconnectAttempts - 1), 30000);

        updateConnectionStatus('reconnecting');
        statusEl.classList.remove('streaming');
        statusEl.innerHTML = `<span class="live-dot reconnecting"></span> Reconnecting (${state.reconnectAttempts}/${state.maxReconnectAttempts})...`;

        setTimeout(() => {
            if (state.isProcessing) {
                closeStream();
                connectSSE(jobId);
            }
        }, backoffMs);
    } else {
        closeStream();
        updateConnectionStatus('disconnected');
        statusEl.classList.remove('streaming');
        statusEl.innerHTML = '<span class="live-dot error"></span> Disconnected - Using fallback polling';
        setResumeButtonEnabled(true);  // live stream lost; user can Resume if the job stalled
        // Fall back to polling
        startLegacyPolling(jobId);
    }
}

function updateConnectionStatus(status) {
    state.connectionStatus = status;
    const indicator = document.getElementById('connection-status-indicator');
    if (indicator) {
        const labels = {
            connected: 'Backend Connected',
            reconnecting: 'Reconnecting...',
            disconnected: 'Backend Offline',
        };
        const tooltips = {
            connected: 'API, Redis, and Model backend all healthy',
            reconnecting: 'Attempting to reconnect to server...',
            disconnected: 'Cannot reach backend — using polling',
        };
        indicator.innerHTML = `
            <span class="connection-indicator ${status}" title="${tooltips[status] || ''}">
                <span class="status-dot"></span>
                ${labels[status] || status}
            </span>
        `;
    }
}

function preserveStreamingHistory(caseNumber) {
    if (!caseNumber) return;
    const history = {};
    Object.keys(state.liveOutputBuffers).forEach(k => {
        history[k] = state.liveOutputBuffers[k] || '';
    });
    state.streamingHistory[caseNumber] = history;
}

function closeStream() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
    if (state.pollInterval) {
        clearInterval(state.pollInterval);
        state.pollInterval = null;
    }
}

function startLegacyPolling(jobId) {
    if (state.pollInterval) clearInterval(state.pollInterval);
    state.pollInterval = setInterval(() => pollJobStatus(jobId), POLL_INTERVAL_MS);
}

/**
 * Stall safety net.
 * If state.isProcessing is true but no SSE/poll is active, something went wrong.
 * Called after job transitions to a terminal state (complete/failed/cancelled).
 * Waits briefly then forces a UI reset if still stuck.
 */
let _stallCheckTimer = null;
function scheduleStallCheck() {
    if (_stallCheckTimer) clearTimeout(_stallCheckTimer);
    _stallCheckTimer = setTimeout(() => {
        _stallCheckTimer = null;
        if (state.isProcessing) {
            console.warn('[STALL_SAFETY] processing still true after terminal event — forcing reset');
            state.isProcessing = false;
            state.currentJobId = null;
            updateProcessButton();
            setCancelButtonEnabled(false);
            closeStream();
        }
    }, 10000);
}

// ============================================
// Live Output Panel (Typewriter)
// ============================================

function initLiveOutputTabs() {
    renderDynamicLiveOutputTabs();
}

function renderDynamicLiveOutputTabs() {
    const container = document.getElementById('live-output-tabs-container');
    if (!container) return;

    const layerKeys = Object.keys(LAYERS);
    container.innerHTML = layerKeys.map(key => {
        const layer = LAYERS[key];
        const isActive = key === state.activeLiveTab;
        return `<button class="live-tab ${isActive ? 'active' : ''}" data-layer="${key}">${layer.shortLabel || key}</button>`;
    }).join('');

    // Rebind click handlers
    container.querySelectorAll('.live-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            state.activeLiveTab = tab.dataset.layer;
            container.querySelectorAll('.live-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            renderLiveOutputBuffer();
        });
    });
}

function appendToken(layer, token, caseNumber) {
    if (!layer || !token) return;

    // Skip if this case is already marked COMPLETE — outputs are immutable
    if (caseNumber && state.frozenCases.has(caseNumber)) return;

    // Map stream layer names to our keys
    const layerKey = normalizeLayerKey(layer);
    if (!layerKey) return;

    // Auto-create buffer for dynamic/custom layers
    if (!(layerKey in state.liveOutputBuffers)) {
        state.liveOutputBuffers[layerKey] = '';
        // If this is a new layer, ensure it has a tab
        if (!LAYERS[layerKey]) {
            LAYERS[layerKey] = { label: layerKey, shortLabel: layerKey, number: Object.keys(LAYERS).length + 1, is_builtin: false };
        }
        renderDynamicLiveOutputTabs();
    }

    // Update current case number if provided
    if (caseNumber) {
        state.currentCaseNumber = caseNumber;
    }

    state.liveOutputBuffers[layerKey] += token;

    // If this is the currently visible tab, render it
    if (layerKey === state.activeLiveTab) {
        renderLiveOutputBuffer();
    }

    // Auto-switch to active layer
    if (state.currentStreamingLayer !== layerKey) {
        state.currentStreamingLayer = layerKey;
        switchLiveTab(layerKey);
    }
}

function switchLiveTab(layerKey) {
    state.activeLiveTab = layerKey;
    document.querySelectorAll('.live-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.layer === layerKey);
    });
    renderLiveOutputBuffer();
}

function renderLiveOutputBuffer() {
    const textEl = document.getElementById('live-output-text');
    const buffer = state.liveOutputBuffers[state.activeLiveTab] || '';
    textEl.innerHTML = escapeHtml(buffer) + '<span class="cursor-blink">|</span>';

    // Auto-scroll
    const body = textEl.closest('.live-output-body');
    if (body) body.scrollTop = body.scrollHeight;
}

function resetLiveOutput() {
    state.liveOutputBuffers = {};
    Object.keys(LAYERS).forEach(k => { state.liveOutputBuffers[k] = ''; });
    state.currentStreamingLayer = null;
    renderLiveOutputBuffer();
}

function normalizeLayerKey(layer) {
    if (!layer) return null;
    const l = layer.toLowerCase();
    // Built-in aliases
    if (l.includes('layer1') || l.includes('ctp') || l.includes('layer_1')) return 'layer1_ctp';
    if (l.includes('layer2') || l.includes('cie') || l.includes('layer_2')) return 'layer2_cie';
    if (l.includes('layer3') || l.includes('ccc') || l.includes('layer_3')) return 'layer3_ccc';
    // Custom layers — pass through as-is
    return l;
}

// ============================================
// Progress UI
// ============================================

function showProgress() {
    document.getElementById('upload-section').classList.add('hidden');
    document.getElementById('progress-section').classList.remove('hidden');
    document.getElementById('results-section').classList.add('hidden');
}

function initializeProgressUI(caseCount) {
    document.getElementById('progress-fill').style.width = '0%';
    document.getElementById('progress-text').textContent = `0 of ${caseCount} cases completed`;
    document.getElementById('case-status-list').innerHTML = '';
    renderDynamicLayerSteps();
    resetLayerSteps();
}

function renderDynamicLayerSteps() {
    const container = document.getElementById('layer-progress');
    if (!container) return;
    const layerKeys = Object.keys(LAYERS);
    container.innerHTML = layerKeys.map((key, i) => {
        const layer = LAYERS[key];
        const labels = {
            layer1_ctp: { main: 'Pre-Processing', sub: 'Text extraction' },
            layer2_cie: { main: 'Extraction', sub: 'Complications' },
            layer3_ccc: { main: 'Verification', sub: 'CCI calculation' },
        };
        const defaultLabel = labels[key] || { main: layer.shortLabel || key, sub: 'Custom layer' };
        return `
            <div class="layer-step" data-layer="${i + 1}" data-layer-key="${key}" id="layer-step-${i + 1}">
                <div class="layer-step-icon">${i + 1}</div>
                <div class="layer-step-label">${defaultLabel.main}</div>
                <div class="layer-step-sublabel">${defaultLabel.sub}</div>
            </div>
        `;
    }).join('');
}

function resetLayerSteps() {
    const steps = document.querySelectorAll('#layer-progress .layer-step');
    steps.forEach(el => { el.className = 'layer-step'; });
}

function setActiveLayer(layerKey) {
    const steps = document.querySelectorAll('#layer-progress .layer-step');
    let found = false;
    steps.forEach(el => {
        if (found) {
            el.className = 'layer-step';
        } else if (el.dataset.layerKey === layerKey) {
            el.className = 'layer-step active';
            found = true;
        } else {
            el.className = 'layer-step completed';
        }
    });
}

function markLayerComplete(layer) {
    const layerKey = normalizeLayerKey(layer);
    const el = document.querySelector(`#layer-progress .layer-step[data-layer-key="${layerKey}"]`);
    if (el) el.className = 'layer-step completed';
}

// ============================================
// Execution Timeline
// ============================================

const TIMELINE_STATUS_ICONS = {
    PENDING: '○',
    RUNNING: '●',
    COMPLETED: '✓',
    FAILED: '✗',
    SKIPPED: '–',
};

/**
 * Format milliseconds into a human-readable duration.
 */
function formatDuration(ms) {
    if (ms == null) return '—';
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
}

/**
 * Full re-render of the execution timeline panel.
 * Called once at job start and when new layers appear.
 * Uses requestAnimationFrame to batch heavy DOM updates.
 */
let _timelineRafPending = false;
function renderExecutionTimeline() {
    const container = document.getElementById('execution-timeline');
    if (!container) return;

    // Debounce rapid successive calls via rAF
    if (_timelineRafPending) return;
    _timelineRafPending = true;
    requestAnimationFrame(() => {
        _timelineRafPending = false;
        _renderTimelineImpl(container);
    });
}

function _renderTimelineImpl(container) {

    const entries = Object.entries(state.layerTimeline);
    if (!entries.length) {
        container.innerHTML = '<div class="timeline-empty">No layers yet</div>';
        return;
    }

    const html = entries.map(([layerId, t]) => {
        const statusClass = (t.status || 'PENDING').toLowerCase();
        const icon = TIMELINE_STATUS_ICONS[t.status] || '○';
        const dur = formatDuration(t.durationMs);
        const errorHtml = t.errorMessage
            ? `<details class="timeline-error-details"><summary class="timeline-error-summary">⚠ Error details</summary><pre class="timeline-error-msg">${escapeHtml(t.errorMessage)}</pre></details>`
            : '';
        return `<div class="timeline-row timeline-${statusClass}" id="tl-${layerId}" data-layer="${layerId}">
            <span class="timeline-icon">${icon}</span>
            <span class="timeline-label">${escapeHtml(t.label || layerId)}</span>
            <span class="timeline-duration">${dur}</span>
            <span class="timeline-status-badge">${t.status}</span>
            ${errorHtml}
        </div>`;
    }).join('');

    container.innerHTML = html;

    // Also mirror into the results-section timeline (if visible)
    const resultsTimeline = document.getElementById('results-execution-timeline');
    if (resultsTimeline) resultsTimeline.innerHTML = html;
}

// ============================================
// Lightweight In-Memory API Cache
// ============================================

const _apiCache = new Map();
const CACHE_TTL_MS = 30000; // 30 seconds

function getCached(key) {
    const entry = _apiCache.get(key);
    if (!entry) return null;
    if (Date.now() - entry.ts > CACHE_TTL_MS) {
        _apiCache.delete(key);
        return null;
    }
    return entry.data;
}

function setCache(key, data) {
    _apiCache.set(key, { data, ts: Date.now() });
}

/**
 * Surgically update a single timeline row without full re-render.
 * Falls back to full re-render if the row doesn't exist yet.
 */
function updateTimelineRow(layerId) {
    const row = document.getElementById(`tl-${layerId}`);
    if (!row) {
        renderExecutionTimeline();
        return;
    }
    const t = state.layerTimeline[layerId];
    if (!t) return;

    const statusClass = (t.status || 'PENDING').toLowerCase();
    const icon = TIMELINE_STATUS_ICONS[t.status] || '○';
    const dur = formatDuration(t.durationMs);

    row.className = `timeline-row timeline-${statusClass}`;
    row.querySelector('.timeline-icon').textContent = icon;
    row.querySelector('.timeline-label').textContent = t.label || layerId;
    row.querySelector('.timeline-duration').textContent = dur;
    row.querySelector('.timeline-status-badge').textContent = t.status;

    // Error details
    const existingErr = row.querySelector('.timeline-error-details');
    if (t.errorMessage && !existingErr) {
        row.insertAdjacentHTML('beforeend',
            `<details class="timeline-error-details"><summary class="timeline-error-summary">⚠ Error details</summary><pre class="timeline-error-msg">${escapeHtml(t.errorMessage)}</pre></details>`
        );
    } else if (!t.errorMessage && existingErr) {
        existingErr.remove();
    }
}

/**
 * Build a summary line for the timeline header badge.
 */
function getTimelineSummaryBadge() {
    const entries = Object.values(state.layerTimeline);
    const failed = entries.filter(t => t.status === 'FAILED').length;
    const completed = entries.filter(t => t.status === 'COMPLETED').length;
    const running = entries.filter(t => t.status === 'RUNNING').length;
    if (running > 0) return { text: 'Running', cls: 'running' };
    if (failed > 0) return { text: `${failed} failed`, cls: 'failed' };
    if (completed === entries.length && completed > 0) return { text: 'All passed', cls: 'passed' };
    return { text: 'Pending', cls: 'pending' };
}

// ============================================
// Pipeline Snapshot Viewer
// ============================================

/**
 * Render the read-only snapshot viewer panel.
 * Data comes from the job results after completion or page reload.
 */
function renderSnapshotViewer(snapshot, jobData) {
    const container = document.getElementById('snapshot-viewer');
    if (!container) return;

    if (!snapshot || !snapshot.length) {
        container.innerHTML = '<div class="snapshot-empty">No snapshot available for this job.</div>';
        return;
    }

    // Check if current layer config differs from the snapshot
    const mismatch = detectSnapshotMismatch(snapshot);

    const modelName = jobData?.model_name || '—';
    const jobTime = jobData?.created_at ? new Date(jobData.created_at).toLocaleString() : '—';

    const replaySourceId = jobData?.replay_source_id;
    const jobId = jobData?.id || jobData?.job_id;
    const replayBadge = replaySourceId
        ? `<span class="snapshot-replay-badge" title="Replayed from ${replaySourceId}"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline-block;vertical-align:middle;"></i> Replay</span>`
        : '';

    let html = `<div class="run-status-ribbon">
        <i data-lucide="shield-check"></i>
        Reproducible Snapshot
    </div>
    <div class="snapshot-header">
        <span class="snapshot-title"><i data-lucide="lock" style="width:16px;height:16px;display:inline-block;vertical-align:middle;"></i> Pipeline Snapshot</span>
        <span class="snapshot-meta">Run: ${escapeHtml(jobTime)} · Model: ${escapeHtml(modelName)}</span>
        ${replayBadge}
        ${mismatch ? '<span class="snapshot-mismatch-badge"><i data-lucide="alert-triangle" style="width:14px;height:14px;display:inline-block;vertical-align:middle;"></i> Configuration changed after this run</span>' : ''}
        ${jobId ? `<button class="btn btn-sm btn-primary btn-replay" onclick="replayJob('${jobId}')" title="Re-run with the same frozen snapshot"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline-block;vertical-align:middle;"></i> Replay This Run</button>` : ''}
    </div>`;

    html += '<div class="snapshot-table-wrap"><table class="snapshot-table"><thead><tr>'
        + '<th>#</th><th>Layer</th><th>ID</th><th>Version</th><th>Builtin</th>'
        + '</tr></thead><tbody>';

    snapshot.forEach((layer, i) => {
        html += `<tr>
            <td>${i + 1}</td>
            <td>${escapeHtml(layer.label || layer.layer_name)}</td>
            <td class="snapshot-mono">${escapeHtml(layer.layer_name)}</td>
            <td>${escapeHtml(layer.prompt_version || layer.version || '—')}</td>
            <td>${layer.is_builtin ? '✓' : '—'}</td>
        </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
    if (window.lucide) lucide.createIcons();
}

/**
 * Compare snapshot layers with current LAYERS config.
 * Returns true if there is a mismatch.
 */
function detectSnapshotMismatch(snapshot) {
    if (!snapshot || !snapshot.length) return false;
    const snapshotNames = snapshot.map(l => l.layer_name);
    const currentNames = Object.keys(LAYERS);
    if (snapshotNames.length !== currentNames.length) return true;
    for (let i = 0; i < snapshotNames.length; i++) {
        if (snapshotNames[i] !== currentNames[i]) return true;
    }
    return false;
}

// ============================================
// Replay, Metrics & Comparison
// ============================================

/**
 * Set disabled state on all replay buttons (top + bottom).
 */
function _setReplayButtonsDisabled(disabled) {
    ['replay-this-run-btn', 'replay-btn-bottom'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = disabled;
    });
}

/**
 * Replay a completed job using its frozen pipeline snapshot.
 * Creates a new job on the backend and starts streaming it.
 */
async function replayJob(jobId) {
    if (!jobId) {
        showToast('No job available to replay', 'warning');
        return;
    }

    // Show replay mode selection modal
    _showReplayModal(jobId);
}

function _showReplayModal(jobId) {
    // Remove existing modal if present
    const existing = document.getElementById('replay-modal-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'replay-modal-overlay';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog replay-modal">
            <div class="modal-header">
                <h3><i data-lucide="refresh-cw" style="width:20px;height:20px;display:inline-block;vertical-align:middle;"></i> Replay Run</h3>
                <button class="modal-close" onclick="document.getElementById('replay-modal-overlay').remove()">&times;</button>
            </div>
            <div class="modal-body">
                <p class="modal-subtitle">Choose which prompts to use for the replay:</p>
                <div class="replay-mode-options">
                    <label class="replay-mode-option selected" data-mode="original">
                        <input type="radio" name="replay-mode" value="original" checked>
                        <div class="replay-mode-content">
                            <span class="replay-mode-title">Original Prompts</span>
                            <span class="replay-mode-desc">Use the exact prompts from this run. Ensures reproducibility.</span>
                        </div>
                    </label>
                    <label class="replay-mode-option" data-mode="latest">
                        <input type="radio" name="replay-mode" value="latest">
                        <div class="replay-mode-content">
                            <span class="replay-mode-title">Latest Prompts</span>
                            <span class="replay-mode-desc">Use currently active prompts. Test your latest prompt changes.</span>
                        </div>
                    </label>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-outline" onclick="document.getElementById('replay-modal-overlay').remove()">Cancel</button>
                <button class="btn btn-primary" id="replay-confirm-btn" onclick="_executeReplay('${jobId}')">Start Replay</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    // Radio button interaction
    overlay.querySelectorAll('.replay-mode-option').forEach(opt => {
        opt.addEventListener('click', () => {
            overlay.querySelectorAll('.replay-mode-option').forEach(o => o.classList.remove('selected'));
            opt.classList.add('selected');
            opt.querySelector('input[type=radio]').checked = true;
        });
    });

    // Close on overlay click
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
    });

    if (window.lucide) lucide.createIcons();
}

async function _executeReplay(jobId) {
    const overlay = document.getElementById('replay-modal-overlay');
    const modeInput = overlay?.querySelector('input[name="replay-mode"]:checked');
    const mode = modeInput?.value || 'original';

    // Remove modal
    if (overlay) overlay.remove();

    _setReplayButtonsDisabled(true);

    try {
        showToast(`Starting replay (${mode} prompts)…`, 'info');
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/replay?mode=${mode}`, { method: 'POST' });
        if (!res.ok) {
            const body = await tryJson(res);
            throw new Error(body?.detail || `Replay failed (${res.status})`);
        }
        const data = unwrapApiResponse(await res.json());
        const newJobId = data.job_id;
        if (!newJobId) throw new Error('No job ID returned from replay');
        showToast(`Replay started (${mode}) → Job ${newJobId.substring(0, 8)}…`, 'success');

        // Navigate to progress view and start streaming the new job
        state.currentJobId = newJobId;
        state.isProcessing = true;
        document.getElementById('results-section').classList.add('hidden');
        document.getElementById('progress-section').classList.remove('hidden');
        startStreaming(newJobId);
    } catch (err) {
        const msg = err instanceof Error ? err.message : extractErrorMessage(err);
        showToast(`Replay failed: ${msg || 'Unknown error'}`, 'error');
    } finally {
        _setReplayButtonsDisabled(false);
    }
}

/**
 * Load per-layer execution metrics for a specific job and render them.
 */
async function loadJobMetrics(jobId) {
    const container = document.getElementById('metrics-viewer');
    if (!container) return;

    // Check cache first
    const cached = getCached(`metrics_${jobId}`);
    if (cached) {
        renderJobMetrics(cached, container);
        return;
    }

    try {
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/metrics`);
        if (!res.ok) {
            container.innerHTML = '<div class="metrics-empty">No metrics available.</div>';
            return;
        }
        const data = unwrapApiResponse(await res.json());
        const metrics = data.metrics || [];
        setCache(`metrics_${jobId}`, metrics);
        renderJobMetrics(metrics, container);
    } catch {
        container.innerHTML = '<div class="metrics-empty">Failed to load metrics.</div>';
    }
}

/**
 * Render per-layer metrics table for a single job.
 */
function renderJobMetrics(metrics, container) {
    if (!metrics || !metrics.length) {
        container.innerHTML = `<div class="metrics-empty">
            <i data-lucide="trending-up" style="width:24px;height:24px;opacity:0.4;"></i>
            <p>No metrics recorded yet.</p>
            <p class="text-muted">Metrics appear after successful pipeline completion.</p>
        </div>`;
        if (window.lucide) lucide.createIcons();
        return;
    }

    // Aggregate by layer_id
    const byLayer = {};
    metrics.forEach(m => {
        if (!byLayer[m.layer_id]) {
            byLayer[m.layer_id] = { total: 0, success: 0, fail: 0, totalMs: 0, errors: [] };
        }
        const b = byLayer[m.layer_id];
        b.total++;
        if (m.success) b.success++; else b.fail++;
        b.totalMs += m.duration_ms || 0;
        if (m.error_type) b.errors.push(m.error_type);
    });

    let html = '<table class="metrics-table"><thead><tr>'
        + '<th>Layer</th><th>Runs</th><th>Avg Time</th><th>Successes</th><th>Failures</th><th>Success %</th>'
        + '</tr></thead><tbody>';

    Object.entries(byLayer).forEach(([layerId, b]) => {
        const avgMs = b.total ? Math.round(b.totalMs / b.total) : 0;
        const pct = b.total ? ((b.success / b.total) * 100).toFixed(1) : '—';
        const pctCls = b.fail > 0 ? 'metrics-warn' : 'metrics-ok';
        html += `<tr>
            <td class="snapshot-mono">${escapeHtml(layerId)}</td>
            <td>${b.total}</td>
            <td>${formatDuration(avgMs)}</td>
            <td>${b.success}</td>
            <td class="${b.fail > 0 ? 'metrics-fail-cell' : ''}">${b.fail}</td>
            <td class="${pctCls}">${pct}%</td>
        </tr>`;
    });

    html += '</tbody></table>';
    container.innerHTML = html;
}

/**
 * Load aggregate metrics across all of the user's jobs.
 */
async function loadAggregateMetrics() {
    const container = document.getElementById('metrics-viewer');
    if (!container) return;

    try {
        const res = await authFetch(`${API_BASE}/jobs/metrics/aggregate`);
        if (!res.ok) return;
        const data = unwrapApiResponse(await res.json());
        renderAggregateMetrics(data.layers || [], container);
    } catch { /* non-fatal */ }
}

function renderAggregateMetrics(layers, container) {
    if (!layers || !layers.length) return;

    let html = '<h4 class="metrics-section-title">Aggregate Metrics (All Jobs)</h4>';
    html += '<table class="metrics-table"><thead><tr>'
        + '<th>Layer</th><th>Avg Time</th><th>Total Runs</th><th>Failures</th><th>Success %</th>'
        + '</tr></thead><tbody>';

    layers.forEach(l => {
        const pctCls = l.success_rate < 100 ? 'metrics-warn' : 'metrics-ok';
        html += `<tr>
            <td class="snapshot-mono">${escapeHtml(l.layer_id)}</td>
            <td>${formatDuration(Math.round(l.avg_duration_ms))}</td>
            <td>${l.total_runs}</td>
            <td class="${l.failures > 0 ? 'metrics-fail-cell' : ''}">${l.failures}</td>
            <td class="${pctCls}">${l.success_rate.toFixed(1)}%</td>
        </tr>`;
    });

    html += '</tbody></table>';
    container.innerHTML += html;
}

/**
 * Populate the comparison job selector with completed jobs.
 */
async function populateCompareSelect(currentJobId) {
    const select = document.getElementById('compare-job-select');
    if (!select) return;

    try {
        // Use cached job list if available
        let allJobs = getCached('jobList');
        if (!allJobs) {
            const res = await authFetch(`${API_BASE}/jobs`);
            if (!res.ok) return;
            const data = unwrapApiResponse(await res.json());
            allJobs = data.jobs || [];
            setCache('jobList', allJobs);
        }
        const jobs = allJobs.filter(
            j => j.status === 'completed' && j.job_id !== currentJobId
        );
        select.innerHTML = '<option value="">— Select job —</option>';
        jobs.forEach(j => {
            const d = j.created_at ? new Date(j.created_at) : null;
            const datePart = d ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '—';
            const timePart = d ? d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }) : '';
            const shortModel = (j.model_name || '—').replace(/:.*$/, '').replace(/-instruct.*$/i, '');
            const src = j.source_type || 'direct';
            const shortId = j.job_id.substring(0, 8);
            const label = `Run: ${datePart} — ${timePart}  |  Model: ${shortModel}  |  Source: ${src}  |  ID: ${shortId}`;
            const opt = document.createElement('option');
            opt.value = j.job_id;
            opt.textContent = label;
            select.appendChild(opt);
        });
    } catch { /* non-fatal */ }
}

/**
 * Run a side-by-side comparison between the current job and the selected job.
 */
async function runComparison() {
    const select = document.getElementById('compare-job-select');
    const resultsDiv = document.getElementById('compare-results');
    if (!select || !resultsDiv) return;

    const jobB = select.value;
    const jobA = state.currentJobId || localStorage.getItem('snapai_last_job_id');
    if (!jobA || !jobB) {
        showToast('Select a job to compare against', 'info');
        return;
    }

    try {
        resultsDiv.innerHTML = '<div class="compare-loading">Loading comparison…</div>';
        const res = await authFetch(`${API_BASE}/jobs/compare?job_a=${jobA}&job_b=${jobB}`);
        if (!res.ok) {
            const body = await tryJson(res);
            throw new Error(body?.detail || `Compare failed (${res.status})`);
        }
        const data = unwrapApiResponse(await res.json());
        renderCompareResults(data, resultsDiv);
    } catch (err) {
        resultsDiv.innerHTML = `<div class="compare-error">${escapeHtml(err.message)}</div>`;
    }
}

/**
 * Render comparison results as a side-by-side table.
 */
function renderCompareResults(data, container) {
    const a = data.job_a || {};
    const b = data.job_b || {};
    const aMetrics = a.layer_metrics || {};
    const bMetrics = b.layer_metrics || {};

    // Also support the flat layers array from backend
    const layersArr = data.layers || [];
    if (!Object.keys(aMetrics).length && layersArr.length) {
        layersArr.forEach(l => {
            if (l.layer_id && l.job_a) aMetrics[l.layer_id] = l.job_a;
            if (l.layer_id && l.job_b) bMetrics[l.layer_id] = l.job_b;
        });
    }

    // Collect all layer IDs from both jobs
    const allLayers = [...new Set([...Object.keys(aMetrics), ...Object.keys(bMetrics)])];

    let html = '<table class="compare-table"><thead><tr>'
        + '<th>Layer</th>'
        + `<th colspan="3">Job A <span class="compare-id">${(a.job_id || '').substring(0, 8)}…</span></th>`
        + `<th colspan="3">Job B <span class="compare-id">${(b.job_id || '').substring(0, 8)}…</span></th>`
        + '<th>Δ Avg</th>'
        + '</tr><tr class="compare-subheader">'
        + '<th></th><th>Avg</th><th>Runs</th><th>Fail</th><th>Avg</th><th>Runs</th><th>Fail</th><th></th>'
        + '</tr></thead><tbody>';

    allLayers.forEach(layerId => {
        const la = aMetrics[layerId] || {};
        const lb = bMetrics[layerId] || {};
        const avgA = la.avg_duration_ms != null ? Math.round(la.avg_duration_ms) : null;
        const avgB = lb.avg_duration_ms != null ? Math.round(lb.avg_duration_ms) : null;
        let delta = '';
        if (avgA != null && avgB != null) {
            const diff = avgB - avgA;
            const cls = diff > 0 ? 'compare-slower' : diff < 0 ? 'compare-faster' : '';
            delta = `<span class="${cls}">${diff > 0 ? '+' : ''}${formatDuration(Math.abs(diff))}</span>`;
        }
        html += `<tr>
            <td class="snapshot-mono">${escapeHtml(layerId)}</td>
            <td>${avgA != null ? formatDuration(avgA) : '—'}</td>
            <td>${la.total_runs ?? '—'}</td>
            <td>${la.failures ?? '—'}</td>
            <td>${avgB != null ? formatDuration(avgB) : '—'}</td>
            <td>${lb.total_runs ?? '—'}</td>
            <td>${lb.failures ?? '—'}</td>
            <td>${delta || '—'}</td>
        </tr>`;
    });

    html += '</tbody></table>';

    // Snapshot diff
    const snapshotDiff = data.snapshot_diff || {};
    const snapALayers = data.snapshot_a_layers || [];
    const snapBLayers = data.snapshot_b_layers || [];
    // Build diff from backend arrays if snapshot_diff is empty
    const onlyInA = snapshotDiff.only_in_a || snapALayers.filter(n => !snapBLayers.includes(n));
    const onlyInB = snapshotDiff.only_in_b || snapBLayers.filter(n => !snapALayers.includes(n));
    if (onlyInA.length || onlyInB.length) {
        html += '<div class="compare-snapshot-diff">';
        html += '<h4>Snapshot Differences</h4>';
        if (onlyInA.length) {
            html += `<div class="compare-diff-section"><strong>Only in Job A:</strong> ${onlyInA.map(escapeHtml).join(', ')}</div>`;
        }
        if (onlyInB.length) {
            html += `<div class="compare-diff-section"><strong>Only in Job B:</strong> ${onlyInB.map(escapeHtml).join(', ')}</div>`;
        }
        html += '</div>';
    }

    container.innerHTML = html;
}

function updateCurrentCaseInfo(caseNum, total, layerKey) {
    const caseLabel = document.getElementById('current-case-label');
    const layerLabel = document.getElementById('current-layer-label');
    if (caseNum) caseLabel.textContent = `Processing Case #${caseNum}${total ? ` of ${total}` : ''}`;
    if (layerKey) layerLabel.textContent = LAYERS[layerKey]?.label || layerKey;
}

function updateCaseProgress(data) {
    if (data.completed != null && data.total != null) {
        const pct = Math.round((data.completed / data.total) * 100);
        document.getElementById('progress-fill').style.width = `${pct}%`;
        document.getElementById('progress-text').textContent = `${data.completed} of ${data.total} cases completed`;
    }
}

function updateProgressUI(data) {
    if (data.cases) renderCaseStatusList(data.cases);
    if (data.completed != null && data.total != null) {
        updateCaseProgress(data);
    }
    if (data.current_case) {
        updateCurrentCaseInfo(data.current_case, data.total, data.current_layer);
    }
    if (data.current_layer) setActiveLayer(normalizeLayerKey(data.current_layer));
}

async function pollJobStatus(jobId) {
    try {
        // Use the lightweight /status endpoint when available (SSE fallback polling)
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/status`);
        if (!res.ok) return;
        const data = unwrapApiResponse(await res.json());

        if (data.status === 'completed') {
            console.debug('[POLL_STATUS completed] resetting processing state');
            closeStream();
            setCancelButtonEnabled(false);
            state.isProcessing = false;
            state.currentJobId = null;
            updateProcessButton();
            loadResults(jobId);
            showToast('Processing complete!', 'success');
            scheduleStallCheck();
        } else if (data.status === 'failed') {
            closeStream();
            setCancelButtonEnabled(false);
            state.isProcessing = false;
            state.currentJobId = null;
            updateProcessButton();
            showProcessingError(data.error || data.message || 'Processing failed', data);
        } else if (data.status === 'cancelled') {
            closeStream();
            setCancelButtonEnabled(false);
            state.isProcessing = false;
            updateProcessButton();
            showToast('Job cancelled — showing partial results', 'info');
            // Load partial results so completed cases are visible
            if (state.currentJobId || state.lastCompletedJobId) {
                loadResults(state.currentJobId || state.lastCompletedJobId);
            }
            state.currentJobId = null;
        } else if (data.status === 'stopping') {
            // Job is stopping gracefully — update UI but keep streaming
            setCancelButtonEnabled(false);
            showToast('Stopping job... finishing current case', 'info');
        } else {
            updateProgressUI(data);
        }
    } catch { /* polling failure is non-fatal */ }
}

function renderCaseStatusList(cases) {
    const list = document.getElementById('case-status-list');
    if (!cases || !Array.isArray(cases)) return;

    list.innerHTML = cases.map((c, i) => `
    <div class="case-status-item">
      <span>Case #${c.case_number || i + 1}</span>
      <span class="status-badge status-${c.status || 'queued'}">${c.status || 'queued'}</span>
    </div>
  `).join('');
}

// ============================================
// Results
// ============================================

async function loadResults(jobId) {
    try {
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/results`);
        if (!res.ok) throw new Error('Failed to load results');
        const data = unwrapApiResponse(await res.json());
        window.currentResults = data;
        // Retain the job ID so replay/baseline buttons work after processing resets currentJobId
        state.lastCompletedJobId = jobId;

        // Guard: cancelled jobs — show partial results + cancelled banner
        if (data.status === 'cancelled') {
            renderCancelledJobState();
            // Still render whatever results exist (partial)
            renderResults(data);
            document.getElementById('progress-section').classList.add('hidden');
            document.getElementById('results-section').classList.remove('hidden');
            state.isProcessing = false;
            updateProcessButton();
            try { localStorage.setItem('snapai_last_job_id', jobId); } catch { }
            return;
        }

        // Hide cancelled state if visible from previous job
        const cancelledEl = document.getElementById('cancelled-job-state');
        if (cancelledEl) cancelledEl.classList.add('hidden');

        renderResults(data);

        // Render snapshot viewer with frozen pipeline config
        state.pipelineSnapshot = data.pipeline_snapshot || null;
        renderSnapshotViewer(data.pipeline_snapshot, data);

        // Render locked config banner (Stage 6)
        renderLockedConfig(data);

        // If timeline is empty (page reload), reconstruct from snapshot
        if (data.pipeline_snapshot && !Object.keys(state.layerTimeline).length) {
            data.pipeline_snapshot.forEach(layer => {
                state.layerTimeline[layer.layer_name] = {
                    label: layer.label || layer.layer_name,
                    status: 'COMPLETED',
                    startTime: null,
                    endTime: null,
                    durationMs: null,
                    errorMessage: null,
                };
            });
        }
        renderExecutionTimeline();

        // Load per-layer execution metrics for this job
        loadJobMetrics(jobId);
        // Defer compare dropdown population (lazy-load)
        setTimeout(() => populateCompareSelect(jobId), 300);
        // Load saved cases panel
        setTimeout(() => renderSavedCasesPanel(), 500);

        // Persist job ID so results survive a page reload
        try { localStorage.setItem('snapai_last_job_id', jobId); } catch { /* ignore quota errors */ }

        document.getElementById('progress-section').classList.add('hidden');
        document.getElementById('results-section').classList.remove('hidden');
        state.isProcessing = false;
        updateProcessButton();
    } catch (err) {
        // CRITICAL: always reset processing state even if results fail to load
        console.warn('[loadResults] failed, forcing state reset:', err.message);
        state.isProcessing = false;
        state.currentJobId = null;
        updateProcessButton();
        showError(err.message);
    }
}

function renderResults(data) {
    const results = data.results || data.cases || [];

    // Summary stats
    const total = results.length;
    const completed = results.filter(r => r.status === 'completed' || r.layer3_output).length;
    const avgCci = results.reduce((acc, r) => {
        const cci = r.layer3_output?.cci_score ?? r.final_cci ?? 0;
        return acc + cci;
    }, 0) / (completed || 1);

    document.getElementById('results-summary').innerHTML = `
    <div class="summary-card">
      <div class="summary-value">${total}</div>
      <div class="summary-label">Total Cases</div>
    </div>
    <div class="summary-card">
      <div class="summary-value">${completed}</div>
      <div class="summary-label">Completed</div>
    </div>
    <div class="summary-card">
      <div class="summary-value">${avgCci.toFixed(1)}</div>
      <div class="summary-label">Avg CCI Score</div>
    </div>
  `;

    renderResultsTable(results);
    renderResultsCards(results);

    // Initialize Lucide icons after dynamic render
    if (window.lucide) lucide.createIcons();
}

function renderResultsTable(results) {
    const container = document.getElementById('results-table-container');
    if (!results.length) {
        container.innerHTML = '<p>No results available.</p>';
        return;
    }

    const rows = results.map((r, i) => {
        const cci = r.layer3_output?.cci_score ?? r.final_cci ?? '—';
        const verdict = r.layer3_output?.verdict ?? r.final_verdict ?? '—';
        const complications = r.layer2_output?.complications ?? [];
        const grades = complications.map(c => c.cd_grade || '?').filter(Boolean);

        return `<tr class="${r.error_message ? 'row-error' : ''}">
      <td>${r.case_number ?? i + 1}</td>
      <td><div class="cd-grades">${grades.length ? grades.map(g => `<span class="cd-grade-badge">${g}</span>`).join('') : '<span class="no-grades">None</span>'}</div></td>
      <td class="cci-value">${cci}</td>
      <td class="verdict-cell"><span class="verdict-badge verdict-${(verdict + '').toLowerCase().replace(/\s/g, '')}">${verdict}</span></td>
    </tr>`;
    }).join('');

    container.innerHTML = `
    <table class="results-table">
      <thead><tr>
        <th>Case #</th>
        <th>CD Grades</th>
        <th>CCI Score</th>
        <th>Verdict</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderResultsCards(results) {
    const container = document.getElementById('case-results');

    // Store result data BEFORE template rendering so copy buttons can access it
    window._resultData = results;

    const html = results.map((r, i) => {
        // ── Comparison mode: render side-by-side view ──
        // Check for comparison data in extra_layer_outputs (persisted via DB)
        const compData = r.extra_layer_outputs?.__comparison_data__;
        if (compData && compData.independent && compData.chained) {
            // Normalize: merge comparison data into r for the renderer
            const compResult = {
                ...r,
                execution_mode: 'comparison',
                independent: compData.independent,
                chained: compData.chained,
                comparison: compData.comparison || {},
            };
            return renderComparisonResultCard(compResult, i);
        }
        // Also check direct keys (from SSE/streaming before DB round-trip)
        if (r.execution_mode === 'comparison' && r.independent && r.chained) {
            return renderComparisonResultCard(r, i);
        }

        // ── Standard rendering (independent or chained) ──
        const caseNum = r.case_number ?? i + 1;
        const verdict = r.layer3_output?.verdict ?? r.final_verdict ?? '—';
        const cci = r.layer3_output?.cci_score ?? r.final_cci ?? '—';
        const executionMode = r.execution_mode ?? 'independent';

        // Always use server-persisted outputs — never depend on streaming buffers
        const extraOutputs = r.extra_layer_outputs || {};
        const extraRawOutputs = r.extra_layer_raw_outputs || {};
        const allExtraKeys = [...new Set([...Object.keys(extraOutputs), ...Object.keys(extraRawOutputs)])]
            .filter(k => k !== '__comparison_data__');  // Exclude comparison data from regular layer rendering

        // Extra layer JSON sections — IDs only, content set in post-render pass
        const extraJsonSections = allExtraKeys.map(key => {
            const label = LAYERS[key]?.label || key;
            return `
              <details class="json-details" open>
                <summary>
                  ${escapeHtml(label)}
                  <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${i}].extra_layer_outputs?.['${key}'] ?? {}, this)">Copy JSON</button>
                </summary>
                <div class="output-meta-row"><span class="output-char-count" id="json-charcount-extra-${key}-${i}"></span></div>
                <pre class="json-tree" id="json-extra-${key}-${i}"></pre>
              </details>`;
        }).join('');

        // Extra layer raw sections — IDs only, content set in post-render pass
        const extraRawSections = allExtraKeys.map(key => {
            const label = LAYERS[key]?.label || key;
            return `
              <div class="raw-output-section">
                <div class="raw-output-header">
                  <h4>${escapeHtml(label)}</h4>
                  <span class="output-char-count" id="raw-charcount-extra-${key}-${i}"></span>
                  <button class="btn btn-sm btn-outline" onclick="copyRawOutput(${i}, 'extra-${key}', this)">Copy</button>
                </div>
                <pre class="raw-output-text" id="raw-extra-${key}-${i}"></pre>
              </div>`;
        }).join('');

        const caseId = r.case_id || '';
        const rJobId = state.lastCompletedJobId || state.currentJobId || '';
        return `
      <div class="case-result-card ${r.error_message ? 'has-error' : ''}">
        <div class="case-result-header" onclick="toggleResult(${i})">
          <span><strong>Case #${caseNum}</strong>
            <span class="insight-badge">
              <i data-lucide="activity"></i>
              CCI Stable
            </span>
            ${executionMode !== 'independent' ? `<span class="execution-mode-label ${executionMode}-label">${executionMode.charAt(0).toUpperCase() + executionMode.slice(1)}</span>` : ''}
          </span>
          <div class="case-result-badges">
            <span class="cci-display">${cci}</span>
            <span class="verdict-badge verdict-${(verdict + '').toLowerCase().replace(/\s/g, '')}">${verdict}</span>
            ${caseId ? `<button class="btn btn-xs btn-secondary save-case-btn" onclick="event.stopPropagation(); toggleSaveCase('${caseId}', '${rJobId}', this)" title="Save this case for later review"><i data-lucide="bookmark" style="width:12px;height:12px;display:inline-block;vertical-align:middle;"></i> Save Case</button>` : ''}
          </div>
        </div>
        <div class="case-result-body" id="case-result-body-${i}">
          ${r.error_message ? `<div class="error-message">${escapeHtml(r.error_message)}</div>` : ''}

          <div class="result-tabs">
            <button class="result-tab-btn active" onclick="switchResultTab(${i}, 'json')"><i data-lucide="clipboard-list" class="btn-icon"></i> Parsed JSON</button>
            <button class="result-tab-btn" onclick="switchResultTab(${i}, 'raw')"><i data-lucide="file-text" class="btn-icon"></i> Raw LLM Output</button>
          </div>

          <!-- ── JSON Tab (default: active) ── -->
          <div class="result-tab-content active output-fullscreen-panel" id="result-tab-json-${i}">
            <div class="output-panel-toolbar">
              <span class="output-audit-label"><i data-lucide="lock" class="btn-icon"></i> Audit Output (Read-only)</span>
              <button class="btn btn-xs btn-ghost output-fullscreen-btn" onclick="toggleOutputFullscreen(this)"><i data-lucide="maximize" class="btn-icon"></i> Fullscreen</button>
            </div>

            <details class="json-details" open>
              <summary>
                Layer 1: CTP — Clinical Text Pre-Processor
                <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${i}].layer1_output ?? {}, this)">Copy JSON</button>
                <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); downloadJSON(window._resultData[${i}].layer1_output ?? {}, 'case${caseNum}_layer1_ctp.json')">Download</button>
                <button class="btn btn-xs btn-stream-dl" onclick="event.stopPropagation(); downloadLayerRaw('${caseId}', 'layer1_ctp', ${i})" title="Download raw streamed text">📄 Raw Stream</button>
              </summary>
              <div class="output-meta-row"><span class="output-char-count" id="json-charcount-l1-${i}"></span></div>
              <pre class="json-tree" id="json-l1-${i}"></pre>
            </details>

            <details class="json-details" open>
              <summary>
                Layer 2: CIE — Complication Info Extraction
                <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${i}].layer2_output ?? {}, this)">Copy JSON</button>
                <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); downloadJSON(window._resultData[${i}].layer2_output ?? {}, 'case${caseNum}_layer2_cie.json')">Download</button>
                <button class="btn btn-xs btn-stream-dl" onclick="event.stopPropagation(); downloadLayerRaw('${caseId}', 'layer2_cie', ${i})" title="Download raw streamed text">📄 Raw Stream</button>
              </summary>
              <div class="output-meta-row"><span class="output-char-count" id="json-charcount-l2-${i}"></span></div>
              <pre class="json-tree" id="json-l2-${i}"></pre>
            </details>

            <details class="json-details" open>
              <summary>
                Layer 3: CCC — CCI Calculation &amp; Cross-Check
                <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${i}].layer3_output ?? {}, this)">Copy JSON</button>
                <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); downloadJSON(window._resultData[${i}].layer3_output ?? {}, 'case${caseNum}_layer3_ccc.json')">Download</button>
                <button class="btn btn-xs btn-stream-dl" onclick="event.stopPropagation(); downloadLayerRaw('${caseId}', 'layer3_ccc', ${i})" title="Download raw streamed text">📄 Raw Stream</button>
              </summary>
              <div class="output-meta-row"><span class="output-char-count" id="json-charcount-l3-${i}"></span></div>
              <pre class="json-tree" id="json-l3-${i}"></pre>
            </details>

            ${extraJsonSections}
          </div>

          <!-- ── Raw LLM Output Tab ── -->
          <div class="result-tab-content output-fullscreen-panel" id="result-tab-raw-${i}">
            <div class="output-panel-toolbar">
              <span class="output-audit-label"><i data-lucide="lock" class="btn-icon"></i> Audit Output (Read-only)</span>
              <button class="btn btn-xs btn-ghost output-fullscreen-btn" onclick="toggleOutputFullscreen(this)"><i data-lucide="maximize" class="btn-icon"></i> Fullscreen</button>
            </div>
            <div class="raw-output-container">
              <div class="raw-output-section">
                <div class="raw-output-header">
                  <h4>Layer 1: CTP</h4>
                  <span class="output-char-count" id="raw-charcount-l1-${i}"></span>
                  <button class="btn btn-sm btn-outline" onclick="copyRawOutput(${i}, 'l1', this)">Copy</button>
                </div>
                <pre class="raw-output-text" id="raw-l1-${i}"></pre>
              </div>
              <div class="raw-output-section">
                <div class="raw-output-header">
                  <h4>Layer 2: CIE</h4>
                  <span class="output-char-count" id="raw-charcount-l2-${i}"></span>
                  <button class="btn btn-sm btn-outline" onclick="copyRawOutput(${i}, 'l2', this)">Copy</button>
                </div>
                <pre class="raw-output-text" id="raw-l2-${i}"></pre>
              </div>
              <div class="raw-output-section">
                <div class="raw-output-header">
                  <h4>Layer 3: CCC</h4>
                  <span class="output-char-count" id="raw-charcount-l3-${i}"></span>
                  <button class="btn btn-sm btn-outline" onclick="copyRawOutput(${i}, 'l3', this)">Copy</button>
                </div>
                <pre class="raw-output-text" id="raw-l3-${i}"></pre>
              </div>
              ${extraRawSections}
            </div>
          </div>

        </div>
      </div>
    `;
    }).join('');

    container.innerHTML = html;

    // ── Post-render pass: populate <pre> elements with modular functions ──
    // This is done after innerHTML is set so we can use DOM APIs (textContent / highlighting).
    results.forEach((r, i) => {
        // Parsed JSON layers
        renderParsedOutput(r.layer1_output, document.getElementById(`json-l1-${i}`));
        renderParsedOutput(r.layer2_output, document.getElementById(`json-l2-${i}`));
        renderParsedOutput(r.layer3_output, document.getElementById(`json-l3-${i}`));

        // Char counts for parsed JSON
        updateOutputCharCount(r.layer1_output, `json-charcount-l1-${i}`);
        updateOutputCharCount(r.layer2_output, `json-charcount-l2-${i}`);
        updateOutputCharCount(r.layer3_output, `json-charcount-l3-${i}`);

        // Raw LLM outputs — uses textContent for safety + large-blob performance
        renderRawOutput(r.layer1_raw_output, document.getElementById(`raw-l1-${i}`));
        renderRawOutput(r.layer2_raw_output, document.getElementById(`raw-l2-${i}`));
        renderRawOutput(r.layer3_raw_output, document.getElementById(`raw-l3-${i}`));

        // Char counts for raw sections
        updateOutputCharCount(r.layer1_raw_output, `raw-charcount-l1-${i}`);
        updateOutputCharCount(r.layer2_raw_output, `raw-charcount-l2-${i}`);
        updateOutputCharCount(r.layer3_raw_output, `raw-charcount-l3-${i}`);

        // Extra layers
        const extraOutputs = r.extra_layer_outputs || {};
        const extraRawOutputs = r.extra_layer_raw_outputs || {};
        const allExtraKeys = [...new Set([...Object.keys(extraOutputs), ...Object.keys(extraRawOutputs)])];
        allExtraKeys.forEach(key => {
            renderParsedOutput(extraOutputs[key], document.getElementById(`json-extra-${key}-${i}`));
            renderRawOutput(extraRawOutputs[key], document.getElementById(`raw-extra-${key}-${i}`));
            updateOutputCharCount(extraOutputs[key], `json-charcount-extra-${key}-${i}`);
            updateOutputCharCount(extraRawOutputs[key], `raw-charcount-extra-${key}-${i}`);
        });
    });
}

function switchResultTab(index, tab) {
    const jsonTab = document.getElementById(`result-tab-json-${index}`);
    const rawTab = document.getElementById(`result-tab-raw-${index}`);
    const buttons = document.querySelectorAll(`#case-result-body-${index} .result-tab-btn`);

    buttons.forEach((btn, i) => {
        btn.classList.toggle('active', (tab === 'json' && i === 0) || (tab === 'raw' && i === 1));
    });

    // Use the 'active' class that is controlled by CSS (.result-tab-content.active)
    if (jsonTab) jsonTab.classList.toggle('active', tab === 'json');
    if (rawTab) rawTab.classList.toggle('active', tab === 'raw');
}

function copyRawOutput(index, layer, btnEl) {
    const el = document.getElementById(`raw-${layer}-${index}`);
    if (!el) return;
    // Use textContent (not innerText) — avoids reflow, works on hidden elements
    const text = el.textContent;
    navigator.clipboard.writeText(text).then(() => {
        if (btnEl) {
            const orig = btnEl.textContent;
            btnEl.textContent = 'Copied!';
            btnEl.classList.add('btn-copied');
            setTimeout(() => {
                btnEl.textContent = orig;
                btnEl.classList.remove('btn-copied');
            }, 1500);
        } else {
            showToast('Copied to clipboard', 'success');
        }
    }).catch(() => showToast('Failed to copy', 'error'));
}

/**
 * Copy any object or string to clipboard.
 * If btnEl is passed, shows inline “Copied!” feedback for 1.5 s.
 * Without btnEl, falls back to a toast notification.
 */
function copyToClipboard(obj, btnEl) {
    const text = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
    navigator.clipboard.writeText(text).then(() => {
        if (btnEl) {
            const orig = btnEl.textContent;
            btnEl.textContent = 'Copied!';
            btnEl.classList.add('btn-copied');
            setTimeout(() => {
                btnEl.textContent = orig;
                btnEl.classList.remove('btn-copied');
            }, 1500);
        } else {
            showToast('Copied to clipboard', 'success');
        }
    }).catch(() => {
        showToast('Failed to copy', 'error');
    });
}

function downloadJSON(obj, filename) {
    const text = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'output.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(`Downloaded ${filename}`, 'success');
}

// ============================================
// Per-Layer Stream Downloads
// ============================================

/**
 * Download the raw stream text for a specific layer from the API.
 * Falls back to streaming history or stored raw output if API fails.
 */
async function downloadLayerRaw(caseId, layerName, resultIndex) {
    if (!caseId) {
        // Fallback: try to use frontend streaming history
        const result = window._resultData?.[resultIndex];
        const streamData = result?.layer_stream_data?.[layerName];
        if (streamData?.raw_stream) {
            _downloadText(streamData.raw_stream, `snap-ai_${layerName}_raw.txt`);
            return;
        }
        // Try legacy raw output
        const layerMap = { layer1_ctp: 'layer1_raw_output', layer2_cie: 'layer2_raw_output', layer3_ccc: 'layer3_raw_output' };
        const rawKey = layerMap[layerName];
        if (rawKey && result?.[rawKey]) {
            _downloadText(result[rawKey], `snap-ai_${layerName}_raw.txt`);
            return;
        }
        showToast('No stream data available for this layer', 'warning');
        return;
    }

    try {
        const res = await authFetch(`${API_BASE}/downloads/cases/${caseId}/layers/${layerName}/raw`);
        if (!res.ok) {
            // API not available — try frontend fallback
            const result = window._resultData?.[resultIndex];
            const streamData = result?.layer_stream_data?.[layerName];
            if (streamData?.raw_stream) {
                _downloadText(streamData.raw_stream, `snap-ai_${layerName}_raw.txt`);
                return;
            }
            throw new Error(`Download failed (${res.status})`);
        }
        const text = await res.text();
        _downloadText(text, `snap-ai_${layerName}_raw_${caseId.substring(0, 8)}.txt`);
    } catch (err) {
        showToast(`Stream download failed: ${err.message}`, 'error');
    }
}

/**
 * Download the parsed JSON from the API for a specific layer.
 */
async function downloadLayerJson(caseId, layerName) {
    try {
        const res = await authFetch(`${API_BASE}/downloads/cases/${caseId}/layers/${layerName}/json`);
        if (!res.ok) throw new Error(`Download failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai_${layerName}_parsed_${caseId.substring(0, 8)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`Downloaded ${layerName} JSON`, 'success');
    } catch (err) {
        showToast(`JSON download failed: ${err.message}`, 'error');
    }
}

/**
 * Download the full execution bundle for a layer.
 */
async function downloadLayerFull(caseId, layerName) {
    try {
        const res = await authFetch(`${API_BASE}/downloads/cases/${caseId}/layers/${layerName}/full`);
        if (!res.ok) throw new Error(`Download failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai_${layerName}_full_${caseId.substring(0, 8)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`Downloaded ${layerName} full bundle`, 'success');
    } catch (err) {
        showToast(`Full bundle download failed: ${err.message}`, 'error');
    }
}

/**
 * Export all streams for the current session/job.
 */
async function downloadStreamExport() {
    const jobId = state.lastCompletedJobId || state.currentJobId || localStorage.getItem('snapai_last_job_id');
    if (!jobId) {
        showToast('No job available to export', 'warning');
        return;
    }

    const btn = document.getElementById('stream-export-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Exporting…'; }

    try {
        const res = await authFetch(`${API_BASE}/downloads/jobs/${jobId}/export`);
        if (!res.ok) throw new Error(`Export failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai_session_${jobId.substring(0, 8)}_streams.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast('Stream export downloaded', 'success');
    } catch (err) {
        showToast(`Stream export failed: ${err.message}`, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="radio" class="btn-icon"></i> Export Streams'; }
        if (window.lucide) lucide.createIcons();
    }
}

/**
 * Helper: download text content as a file.
 */
function _downloadText(text, filename) {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(`Downloaded ${filename}`, 'success');
}

function switchView(view) {
    state.currentView = view;
    document.querySelectorAll('.view-toggle-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === view);
    });
    const tableContainer = document.getElementById('results-table-container');
    const cardsContainer = document.getElementById('case-results');
    if (view === 'table') {
        tableContainer.classList.remove('hidden');
        cardsContainer.classList.add('hidden');
    } else {
        tableContainer.classList.add('hidden');
        cardsContainer.classList.remove('hidden');
    }
}

function toggleResult(index) {
    const body = document.getElementById(`case-result-body-${index}`);
    if (body) body.classList.toggle('expanded');
}

function syntaxHighlightJSON(obj) {
    const json = JSON.stringify(obj, null, 2);
    return json.replace(
        /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
        (match) => {
            let cls = 'json-number';
            if (/^"/.test(match)) {
                cls = /:$/.test(match) ? 'json-key' : 'json-string';
            } else if (/true|false/.test(match)) {
                cls = 'json-boolean';
            } else if (/null/.test(match)) {
                cls = 'json-null';
            }
            return `<span class="${cls}">${match}</span>`;
        }
    );
}

// ============================================
// Output Panel Rendering (Modular)
// ============================================

/**
 * Render parsed JSON output into a <pre> element.
 * Applies syntax highlighting; falls back gracefully on invalid JSON.
 * Never corrupts the DOM on parse errors.
 */
function renderParsedOutput(data, preEl) {
    if (!preEl) return;
    if (data === null || data === undefined) {
        preEl.textContent = 'Output not available.';
        return;
    }
    try {
        const obj = typeof data === 'string' ? JSON.parse(data) : data;
        // innerHTML is safe here: syntaxHighlightJSON only adds <span> tags to
        // already-JSON-stringified text (no user content injected as HTML).
        preEl.innerHTML = syntaxHighlightJSON(obj);
    } catch (_e) {
        // Insert warning label once before the pre, then show raw content via textContent
        const prev = preEl.previousElementSibling;
        if (!prev || !prev.classList.contains('json-parse-warning')) {
            const warn = document.createElement('div');
            warn.className = 'json-parse-warning';
            warn.textContent = '(!) Failed to parse JSON \u2014 showing raw content.';
            preEl.parentNode && preEl.parentNode.insertBefore(warn, preEl);
        }
        preEl.textContent = typeof data === 'string' ? data : JSON.stringify(data);
    }
}

/**
 * Render raw LLM output text into a <pre> element.
 * Uses textContent (NOT innerHTML) — safe for any content, fast for large blobs.
 */
function renderRawOutput(text, preEl) {
    if (!preEl) return;
    preEl.textContent = text || 'Output not available.';
}

/**
 * Set a char-count label element.
 */
function updateOutputCharCount(text, elId) {
    const el = document.getElementById(elId);
    if (!el) return;
    const chars = typeof text === 'string' ? text.length
        : text !== null && text !== undefined ? JSON.stringify(text).length : 0;
    el.textContent = chars > 0 ? `${chars.toLocaleString()} chars` : '';
}

/**
 * Toggle fullscreen for the nearest .output-fullscreen-panel ancestor.
 */
function toggleOutputFullscreen(btnEl) {
    const panel = btnEl.closest('.output-fullscreen-panel');
    if (!panel) return;
    const isFs = panel.classList.toggle('output-panel-fullscreen');
    btnEl.textContent = isFs ? '[X] Exit Fullscreen' : '[+] Fullscreen';
    document.body.style.overflow = isFs ? 'hidden' : '';
}

/**
 * Fetch a single case’s full output from the database.
 * Falls back to window._resultData cache if the data is already loaded.
 */
async function fetchCaseOutput(jobId, caseId) {
    if (window._resultData && Array.isArray(window._resultData)) {
        const cached = window._resultData.find(r => r.case_id === caseId);
        if (cached && (cached.layer1_output || cached.layer2_output || cached.layer3_output)) {
            return cached;
        }
    }
    try {
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/cases/${caseId}`);
        if (!res.ok) return null;
        const json = await res.json();
        return json.data || null;
    } catch {
        return null;
    }
}

// ============================================
// Export & New Analysis
// ============================================

function initExportActions() {
    document.getElementById('export-btn')?.addEventListener('click', () => {
        if (!window.currentResults) return;
        const blob = new Blob([JSON.stringify(window.currentResults, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai-results-${state.currentJobId || 'export'}.json`;
        a.click();
        URL.revokeObjectURL(url);
        showToast('Results exported', 'success');
    });

    document.getElementById('new-analysis-btn')?.addEventListener('click', () => {
        clearFile();
        document.getElementById('text-input').value = '';
        state.textContent = '';
        window.currentResults = null;
        resetToUpload();
    });
}

function resetToUpload() {
    // Reset section visibility
    document.getElementById('upload-section').classList.remove('hidden');
    document.getElementById('progress-section').classList.add('hidden');
    document.getElementById('results-section').classList.add('hidden');

    // Reset global state
    state.isProcessing = false;
    state.currentJobId = null;
    closeStream();

    // Reset progress section header back to original (remove error state)
    const progressSection = document.getElementById('progress-section');
    const h2 = progressSection.querySelector('h2');
    h2.innerHTML = `
        <svg class="section-icon spinning" xmlns="http://www.w3.org/2000/svg" width="24" height="24"
            viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 1 1-6.219-8.56" />
        </svg>
        Processing...
    `;

    // Reset current case info
    document.getElementById('current-case-label').textContent = 'Initializing...';
    document.getElementById('current-layer-label').textContent = 'Preparing pipeline';

    // Reset layer steps
    resetLayerSteps();

    // Reset progress bar
    document.getElementById('progress-fill').style.width = '0%';
    document.getElementById('progress-text').textContent = '0 of 0 cases completed';

    // Reset case status list (clear any error cards)
    document.getElementById('case-status-list').innerHTML = '';

    // Clear the persisted last-job so reload doesn't re-show old results
    try { localStorage.removeItem('snapai_last_job_id'); } catch { /* ignore */ }

    // Reset live output
    resetLiveOutput();

    // Reset live output tabs to layer 1
    document.querySelectorAll('.live-tab').forEach(tab => tab.classList.remove('active'));
    const l1Tab = document.getElementById('live-tab-l1');
    if (l1Tab) l1Tab.classList.add('active');
    state.activeLiveOutputLayer = 'layer1_ctp';

    // Update process button state
    updateProcessButton();
}

function showError(message) {
    showToast(message, 'error');
    state.isProcessing = false;
    updateProcessButton();
}

function showProcessingError(message, data) {
    state.isProcessing = false;
    updateProcessButton();
    closeStream();

    // Build helpful error UI
    const errorSection = document.getElementById('progress-section');
    const h2 = errorSection.querySelector('h2');
    h2.innerHTML = `
        <svg class="section-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24"
            viewBox="0 0 24 24" fill="none" stroke="#e74c3c" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <line x1="15" x2="9" y1="9" y2="15"/>
            <line x1="9" x2="15" y1="9" y2="15"/>
        </svg>
        Processing Failed
    `;

    // Show error details
    const caseList = document.getElementById('case-status-list');
    caseList.innerHTML = `
        <div class="error-detail-card">
            <div class="error-detail-title">Error Details</div>
            <div class="error-detail-message">${escapeHtml(message)}</div>
            <div class="error-detail-actions">
                <p><strong>What to do:</strong></p>
                <ul>
                    <li>Check that the LLM model is loaded (open Model Management)</li>
                    <li>Verify the backend and Ollama services are running</li>
                    <li>Try with a smaller text input first</li>
                    <li>Check the browser console for more details</li>
                </ul>
                <button class="btn btn-primary btn-sm" onclick="resetToUpload()">Try Again</button>
                <button class="btn btn-outline btn-sm" onclick="openModelModal()">Check Models</button>
            </div>
        </div>
    `;
    showToast(message, 'error');
}

// ============================================
// Model Management
// ============================================

async function loadModels() {
    const dropdown = document.getElementById('model-dropdown');
    try {
        const res = await authFetch(`${API_BASE}/models`);
        if (!res.ok) throw new Error('Failed to load models');
        const data = unwrapApiResponse(await res.json());

        state.availableModels = data.models || [];
        state.activeModel = data.active_model || null;

        renderModelDropdown();
        renderModelList();
    } catch {
        // Populate with recommended models as fallback
        dropdown.innerHTML = RECOMMENDED_MODELS.map(m =>
            `<option value="${m.name}">${m.name}</option>`
        ).join('');
    }
}

function renderModelDropdown() {
    const dropdown = document.getElementById('model-dropdown');
    const pulledNames = new Set(state.availableModels.map(m => m.name || m));

    // Build options: pulled models first, then recommended unpulled ones
    let options = '';

    // Add pulled models
    state.availableModels.forEach(m => {
        const name = m.name || m;
        const size = m.size_gb ? ` (${m.size_gb} GB)` : '';
        const sel = name === state.activeModel ? 'selected' : '';
        options += `<option value="${name}" ${sel}>✓ ${name}${size}</option>`;
    });

    // Add recommended models that aren't pulled yet
    const unpulled = RECOMMENDED_MODELS.filter(m => !pulledNames.has(m.name));
    if (unpulled.length > 0) {
        options += `<option disabled>── Not Installed ──</option>`;
        unpulled.forEach(m => {
            options += `<option value="${m.name}" data-needs-pull="true">⬇ ${m.name}</option>`;
        });
    }

    if (!options) {
        options = RECOMMENDED_MODELS.map(m =>
            `<option value="${m.name}">⬇ ${m.name}</option>`
        ).join('');
    }

    dropdown.innerHTML = options;

    // Remove old listeners by cloning
    const newDropdown = dropdown.cloneNode(true);
    dropdown.parentNode.replaceChild(newDropdown, dropdown);

    newDropdown.addEventListener('change', async (e) => {
        const model = e.target.value;
        const option = e.target.selectedOptions[0];

        // If the model needs pulling, offer to pull it
        if (option?.dataset.needsPull === 'true') {
            const doPull = confirm(`Model "${model}" is not installed yet.\n\nWould you like to download and install it now?\n\nNote: Large models may take several minutes to download.`);
            if (doPull) {
                showToast(`Pulling ${model}... Check Model Management for progress.`, 'info');
                openModelModal();
                // Put the model name in the custom input and trigger pull
                const customInput = document.getElementById('model-pull-custom');
                if (customInput) customInput.value = model;
                await pullModelWithProgress(model);
                await loadModels();
            } else {
                await loadModels(); // Reset dropdown
            }
            return;
        }

        // Set active model
        try {
            const res = await authFetch(`${API_BASE}/models/active`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model }),
            });
            if (res.ok) {
                state.activeModel = model;
                showToast(`Active model set to ${model}`, 'success');
                updateSystemInfoModel(model);
            } else {
                const err = await res.json().catch(() => ({}));
                showToast(`Failed: ${extractErrorMessage(err)}`, 'error');
            }
        } catch {
            showToast('Failed to set active model', 'error');
        }
    });
}

function openModelModal() {
    document.getElementById('model-modal-overlay').classList.remove('hidden');
    renderModelList();
    renderModelPullDropdown();
}

function closeModelModal() {
    document.getElementById('model-modal-overlay').classList.add('hidden');
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('manage-models-btn')?.addEventListener('click', openModelModal);

    // Close modal on overlay click
    document.getElementById('model-modal-overlay')?.addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeModelModal();
    });
});

async function pullModel() {
    const selectEl = document.getElementById('model-pull-select');
    const modelName = selectEl?.value?.trim();
    if (!modelName) {
        showToast('Select a model to pull', 'warning');
        return;
    }
    await pullModelWithProgress(modelName);
}

async function pullModelWithProgress(modelName) {
    const statusEl = document.getElementById('model-pull-status');
    statusEl.className = 'model-pull-status loading';
    statusEl.innerHTML = `<span class="spinner-small"></span> Pulling ${modelName}...`;
    statusEl.classList.remove('hidden');

    try {
        const res = await authFetch(`${API_BASE}/models/pull`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: modelName }),
        });

        if (!res.ok) {
            throw new Error(`Server returned ${res.status}`);
        }

        // Read SSE stream for progress
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // keep incomplete line

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    if (data.done) {
                        if (data.success) {
                            statusEl.className = 'model-pull-status success';
                            statusEl.textContent = `\u2705 ${modelName} pulled successfully! You can now select it as active.`;
                            await loadModels();
                            renderModelPullDropdown();
                        } else {
                            throw new Error(data.error || 'Pull failed');
                        }
                    } else {
                        const pct = data.percent != null ? ` (${data.percent}%)` : '';
                        statusEl.innerHTML = `<span class="spinner-small"></span> ${escapeHtml(data.status || 'Downloading')}${pct}`;
                    }
                } catch (parseErr) {
                    if (parseErr.message === 'Pull failed' || parseErr.message?.includes('Pull failed')) throw parseErr;
                    // Ignore JSON parse errors on partial chunks
                }
            }
        }
    } catch (err) {
        statusEl.className = 'model-pull-status error';
        statusEl.innerHTML = `
            <strong>\u274c Failed to pull ${escapeHtml(modelName)}</strong><br>
            <span>${escapeHtml(err.message)}</span><br>
            <small>Make sure the model name is correct and Ollama is running.</small>
        `;
    }
}

function renderModelPullDropdown() {
    const container = document.getElementById('model-pull-section-inner');
    if (!container) return;

    const pulledNames = new Set(state.availableModels.map(m => m.name || m));
    const unpulledModels = RECOMMENDED_MODELS.filter(m => !pulledNames.has(m.name));

    if (unpulledModels.length === 0) {
        container.innerHTML = `
            <p class="all-models-installed">✅ All recommended models are installed!</p>
            <div class="custom-pull-section">
                <label>Or pull a custom model:</label>
                <div class="model-pull-input">
                    <input type="text" id="model-pull-custom" class="input" placeholder="e.g. codellama:7b">
                    <button class="btn btn-primary btn-sm" onclick="pullCustomModel()">Pull</button>
                </div>
            </div>
        `;
        return;
    }

    const options = unpulledModels.map(m =>
        `<option value="${m.name}">${m.name} — ${m.desc.split('—')[1]?.trim() || ''}</option>`
    ).join('');

    container.innerHTML = `
        <label>Select a model to download:</label>
        <div class="model-pull-input">
            <select id="model-pull-select" class="input model-pull-dropdown">
                <option value="">-- Choose a model --</option>
                ${options}
            </select>
            <button id="model-pull-btn" class="btn btn-primary btn-sm" onclick="pullModel()">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>
                Pull
            </button>
        </div>
        <div class="custom-pull-section">
            <label>Or enter a custom model name:</label>
            <div class="model-pull-input">
                <input type="text" id="model-pull-custom" class="input" placeholder="e.g. codellama:7b">
                <button class="btn btn-outline btn-sm" onclick="pullCustomModel()">Pull Custom</button>
            </div>
        </div>
    `;
}

async function pullCustomModel() {
    const input = document.getElementById('model-pull-custom');
    const modelName = input?.value?.trim();

    if (!modelName) {
        showToast('Enter a model name', 'warning');
        return;
    }

    await pullModelWithProgress(modelName);
    input.value = '';
}

function renderModelList() {
    const list = document.getElementById('model-list');
    const models = state.availableModels;

    if (!models.length) {
        list.innerHTML = '<p style="color: var(--color-text-muted); font-style: italic;">No models loaded. Pull a model to get started.</p>';
        return;
    }

    list.innerHTML = models.map(m => {
        const name = m.name || m;
        const size = m.size ? formatFileSize(m.size) : '';
        const isActive = name === state.activeModel;

        return `
      <div class="model-item">
        <div class="model-item-info">
          <div class="model-item-name">${name}</div>
          <div class="model-item-meta">${size}${m.modified_at ? ' • ' + new Date(m.modified_at).toLocaleDateString() : ''}</div>
        </div>
        <div class="model-item-actions">
          ${isActive ? '<span class="model-active-badge">Active</span>' : `<button class="btn btn-sm btn-outline" onclick="setActiveModel('${name}')">Use</button>`}
          <button class="btn-danger" onclick="deleteModel('${name}')" ${isActive ? 'disabled' : ''}>Delete</button>
        </div>
      </div>
    `;
    }).join('');
}

async function setActiveModel(name) {
    try {
        const res = await authFetch(`${API_BASE}/models/active`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: name }),
        });
        if (res.ok) {
            state.activeModel = name;
            renderModelDropdown();
            renderModelList();
            updateSystemInfoModel(name);
            showToast(`Active model: ${name}`, 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Failed: ${extractErrorMessage(err)}`, 'error');
        }
    } catch {
        showToast('Failed to set model', 'error');
    }
}

async function deleteModel(name) {
    if (!confirm(`Delete model "${name}"? This cannot be undone.`)) return;
    try {
        const res = await authFetch(`${API_BASE}/models`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: name }),
        });
        if (res.ok) {
            showToast(`${name} deleted`, 'info');
            await loadModels();
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Failed: ${extractErrorMessage(err)}`, 'error');
        }
    } catch {
        showToast('Failed to delete model', 'error');
    }
}

// ============================================
// Dynamic Layer Loading
// ============================================

async function loadLayers() {
    try {
        const res = await authFetch(`${API_BASE}/prompts/layers`);
        if (!res.ok) return;
        const data = unwrapApiResponse(await res.json());
        const layers = data.layers || [];

        // Rebuild LAYERS dict
        const newLayers = {};
        layers.forEach((l, i) => {
            newLayers[l.layer_name] = {
                label: l.label || l.layer_name,
                shortLabel: l.label ? l.label.split('—')[0]?.trim() || l.label.substring(0, 20) : l.layer_name,
                number: i + 1,
                is_builtin: l.is_builtin,
            };
        });

        if (Object.keys(newLayers).length > 0) {
            LAYERS = newLayers;
        }

        // Re-render dynamic UI elements
        renderDynamicPromptTabs();
        renderDynamicLiveOutputTabs();
        resetLiveOutput();
    } catch (err) {
        console.warn('Failed to load layers, using defaults:', err);
    }
}

function renderDynamicPromptTabs() {
    const container = document.getElementById('prompt-tabs-container');
    if (!container) return;

    const layerKeys = Object.keys(LAYERS);
    let html = '';

    layerKeys.forEach(key => {
        const layer = LAYERS[key];
        const isActive = key === state.activePromptLayer;
        const deleteBtn = (!layer.is_builtin && authState.user?.role === 'admin')
            ? `<span class="tab-delete-btn" onclick="event.stopPropagation(); deleteCustomLayer('${key}')" title="Delete layer">&times;</span>`
            : '';
        html += `<button class="prompt-tab ${isActive ? 'active' : ''}" data-layer="${key}" draggable="true" onclick="switchPromptTab('${key}')">${layer.shortLabel || key}${deleteBtn}</button>`;
    });

    // "Add Layer" button
    html += `<button class="prompt-tab add-layer-tab" onclick="showCreateLayerModal()" title="Create a new custom layer">+ Add Layer</button>`;

    container.innerHTML = html;

    // ── Drag-and-drop reorder ──
    const tabs = container.querySelectorAll('.prompt-tab[draggable="true"]');
    let dragSrcEl = null;

    tabs.forEach(tab => {
        tab.addEventListener('dragstart', (e) => {
            dragSrcEl = tab;
            tab.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', tab.dataset.layer);
        });
        tab.addEventListener('dragend', () => {
            tab.classList.remove('dragging');
            tabs.forEach(t => t.classList.remove('drag-over'));
        });
        tab.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            tab.classList.add('drag-over');
        });
        tab.addEventListener('dragleave', () => {
            tab.classList.remove('drag-over');
        });
        tab.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            tabs.forEach(t => t.classList.remove('drag-over'));
            if (!dragSrcEl || dragSrcEl === tab) return;

            const srcLayer = dragSrcEl.dataset.layer;
            const dstLayer = tab.dataset.layer;
            reorderLayersDrag(srcLayer, dstLayer);
        });
    });
}

/**
 * Reorder layers by moving srcLayer to the position of dstLayer.
 * Computes new display_order values and sends them to the backend.
 */
async function reorderLayersDrag(srcLayer, dstLayer) {
    const keys = Object.keys(LAYERS);
    const srcIdx = keys.indexOf(srcLayer);
    const dstIdx = keys.indexOf(dstLayer);
    if (srcIdx === -1 || dstIdx === -1 || srcIdx === dstIdx) return;

    // Reorder keys array
    keys.splice(srcIdx, 1);
    keys.splice(dstIdx, 0, srcLayer);

    // Build new LAYERS object in reordered sequence
    const newLayers = {};
    keys.forEach((k, i) => {
        newLayers[k] = { ...LAYERS[k], number: i + 1 };
    });
    LAYERS = newLayers;

    // Re-render all dynamic UIs
    renderDynamicPromptTabs();
    renderDynamicLiveOutputTabs();
    renderDynamicLayerSteps();

    // Send to backend
    const order = keys.map((k, i) => ({ layer_name: k, display_order: i }));
    try {
        const res = await authFetch(`${API_BASE}/prompts/reorder`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order }),
        });
        if (res.ok) {
            showToast('Layer order updated', 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Reorder failed: ${extractErrorMessage(err)}`, 'error');
            // Reload original order
            await loadLayers();
        }
    } catch (err) {
        showToast(`Reorder failed: ${err.message}`, 'error');
        await loadLayers();
    }
}

// ============================================
// Create Custom Layer
// ============================================

function showCreateLayerModal() {
    const existing = document.getElementById('create-layer-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'create-layer-modal';
    modal.className = 'modal-overlay';
    modal.innerHTML = `
        <div class="modal" style="max-width: 500px;">
            <div class="modal-header">
                <h3>Create Custom Layer</h3>
                <button class="modal-close" onclick="document.getElementById('create-layer-modal').remove()">&times;</button>
            </div>
            <div class="modal-body">
                <div style="margin-bottom: var(--space-md);">
                    <label class="section-label">Layer Name (lowercase, no spaces)</label>
                    <input type="text" id="new-layer-name" class="input" placeholder="e.g. layer4_summary" pattern="[a-z][a-z0-9_\\-]{2,49}">
                </div>
                <div style="margin-bottom: var(--space-md);">
                    <label class="section-label">Display Label</label>
                    <input type="text" id="new-layer-label" class="input" placeholder="e.g. Layer 4 — Clinical Summary">
                </div>
                <div style="margin-bottom: var(--space-md);">
                    <label class="section-label">Initial Prompt</label>
                    <textarea id="new-layer-prompt" class="text-input" rows="8" placeholder="Enter the system prompt for this layer..."></textarea>
                </div>
                <div style="display: flex; gap: var(--space-sm); justify-content: flex-end;">
                    <button class="btn btn-outline btn-sm" onclick="document.getElementById('create-layer-modal').remove()">Cancel</button>
                    <button class="btn btn-primary btn-sm" onclick="createCustomLayer()">Create Layer</button>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // Close on overlay click
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.remove();
    });
}

async function createCustomLayer() {
    const nameInput = document.getElementById('new-layer-name');
    const labelInput = document.getElementById('new-layer-label');
    const promptInput = document.getElementById('new-layer-prompt');

    const layer_name = nameInput?.value?.trim().toLowerCase();
    const label = labelInput?.value?.trim();
    const content = promptInput?.value?.trim();

    if (!layer_name || !label || !content) {
        showToast('All fields are required', 'warning');
        return;
    }

    if (content.length < 10) {
        showToast('Prompt must be at least 10 characters', 'warning');
        return;
    }

    try {
        const res = await authFetch(`${API_BASE}/prompts/layers`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ layer_name, label, content }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(extractErrorMessage(err));
        }

        const data = unwrapApiResponse(await res.json());
        showToast(data.message || `Layer '${label}' created`, 'success');

        // Remove modal
        document.getElementById('create-layer-modal')?.remove();

        // Reload layers and prompts
        await loadLayers();
        state.promptData = {};
        await loadAllPrompts();

        // Switch to the new layer
        switchPromptTab(layer_name);
    } catch (err) {
        showToast(`Failed to create layer: ${err.message}`, 'error');
    }
}

// ============================================
// Prompt Editor (with Versioning)
// ============================================

function initPromptEditor() {
    const editor = document.getElementById('prompt-editor');
    editor.addEventListener('input', () => {
        state.promptDirty = true;
        updateCharCount();
    });
}

function switchPromptTab(layer) {
    // Save current editor content to cache before switching
    const editor = document.getElementById('prompt-editor');
    const prevLayer = state.activePromptLayer;
    if (prevLayer && editor.value && !editor.disabled) {
        if (!state.promptData[prevLayer]) {
            state.promptData[prevLayer] = { content: '', source: 'default', version: '1.3' };
        }
        state.promptData[prevLayer].content = editor.value;
    }

    state.activePromptLayer = layer;
    document.querySelectorAll('.prompt-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.layer === layer);
    });

    const layerNameEl = document.getElementById('prompt-layer-name');
    const currentLabel = LAYERS[layer]?.label || layer;
    // Render editable label (double-click to rename)
    layerNameEl.innerHTML = `<span class="editable-label" title="Double-click to rename" ondblclick="startRenameLayer('${layer}')">${escapeHtml(currentLabel)}</span>`;

    // Update reset button visibility (only for built-in layers)
    const resetBtn = document.getElementById('prompt-reset-btn');
    const deleteBtn = document.getElementById('prompt-delete-btn');
    if (resetBtn) resetBtn.style.display = BUILTIN_LAYERS.includes(layer) ? '' : 'none';
    if (deleteBtn) deleteBtn.style.display = BUILTIN_LAYERS.includes(layer) ? 'none' : '';

    // Use cached prompt data if available
    if (state.promptData[layer] && state.promptData[layer].content) {
        displayPromptData(layer, state.promptData[layer]);
    } else {
        loadPrompt(layer);
    }

    // Load version history for this layer
    loadVersionHistory(layer);
}

/**
 * Start inline rename of a layer's display label.
 */
function startRenameLayer(layerKey) {
    const layerNameEl = document.getElementById('prompt-layer-name');
    const currentLabel = LAYERS[layerKey]?.label || layerKey;

    layerNameEl.innerHTML = `<input type="text" class="inline-rename-input" id="rename-layer-input" value="${escapeHtml(currentLabel)}" maxlength="200">`;
    const input = document.getElementById('rename-layer-input');
    input.focus();
    input.select();

    const finishRename = async () => {
        const newLabel = input.value.trim();
        if (!newLabel || newLabel === currentLabel) {
            // No change — restore display
            layerNameEl.innerHTML = `<span class="editable-label" title="Double-click to rename" ondblclick="startRenameLayer('${layerKey}')">${escapeHtml(currentLabel)}</span>`;
            return;
        }

        try {
            const res = await authFetch(`${API_BASE}/prompts/${layerKey}/rename`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label: newLabel }),
            });
            if (res.ok) {
                // Update local state
                if (LAYERS[layerKey]) {
                    LAYERS[layerKey].label = newLabel;
                    LAYERS[layerKey].shortLabel = newLabel.split('—')[0]?.trim() || newLabel.substring(0, 20);
                }
                renderDynamicPromptTabs();
                renderDynamicLiveOutputTabs();
                renderDynamicLayerSteps();
                showToast(`Renamed to "${newLabel}"`, 'success');
            } else {
                const err = await res.json().catch(() => ({}));
                showToast(`Rename failed: ${extractErrorMessage(err)}`, 'error');
            }
        } catch (err) {
            showToast(`Rename failed: ${err.message}`, 'error');
        }

        // Restore display with new or old label
        const displayLabel = LAYERS[layerKey]?.label || layerKey;
        layerNameEl.innerHTML = `<span class="editable-label" title="Double-click to rename" ondblclick="startRenameLayer('${layerKey}')">${escapeHtml(displayLabel)}</span>`;
    };

    input.addEventListener('blur', finishRename);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') {
            input.value = currentLabel; // reset
            input.blur();
        }
    });
}

async function loadAllPrompts() {
    // Pre-load all prompts at once using the list endpoint
    const editor = document.getElementById('prompt-editor');
    editor.value = 'Loading prompt templates...';
    editor.disabled = true;

    try {
        const res = await authFetch(`${API_BASE}/prompts`);
        if (!res.ok) throw new Error('Failed to load prompts');
        const data = unwrapApiResponse(await res.json());
        const prompts = data.prompts || [];

        if (prompts.length === 0) {
            throw new Error('No prompts returned from server');
        }

        // Cache all prompts
        prompts.forEach(p => {
            state.promptData[p.layer_name] = {
                content: p.content || '',
                source: p.source || 'default',
                version: p.version || '1.3',
                label: p.layer_label || p.label || '',
                is_builtin: p.is_builtin !== false,
                active_version_id: p.active_version_id || null,
                version_count: p.version_count || 0,
            };
        });

        // Re-render prompt tabs to reflect all loaded layers
        renderDynamicPromptTabs();

        // Ensure activePromptLayer is valid
        if (!state.promptData[state.activePromptLayer]) {
            state.activePromptLayer = Object.keys(LAYERS)[0] || 'layer1_ctp';
        }

        // Display the currently active tab
        if (state.promptData[state.activePromptLayer] && state.promptData[state.activePromptLayer].content) {
            displayPromptData(state.activePromptLayer, state.promptData[state.activePromptLayer]);
        } else {
            // Fallback: load individual prompt if content is empty
            await loadPrompt(state.activePromptLayer);
        }
    } catch (err) {
        console.error('Failed to load prompts:', err);
        // Fall back to loading individual prompts
        await loadPrompt(state.activePromptLayer);
    }
}

function displayPromptData(layer, promptInfo) {
    const editor = document.getElementById('prompt-editor');
    const badge = document.getElementById('prompt-source-badge');

    editor.value = promptInfo.content || '';
    editor.disabled = false;
    state.promptDirty = false;

    // Update badge with active version info
    const isCustom = promptInfo.source === 'custom';
    const versionStr = promptInfo.version || (isCustom ? 'custom' : '1.3');
    if (isCustom) {
        badge.textContent = `CUSTOM V${versionStr}`;
        badge.className = 'prompt-source-badge custom';
    } else {
        badge.textContent = `DEFAULT V${versionStr}`;
        badge.className = 'prompt-source-badge default';
    }

    // Show active version count if available
    if (promptInfo.version_count > 0) {
        badge.textContent += ` · ${promptInfo.version_count} version${promptInfo.version_count > 1 ? 's' : ''}`;
    }

    updateCharCount();
}

async function loadPrompt(layer) {
    const editor = document.getElementById('prompt-editor');

    editor.value = 'Loading prompt template...';
    editor.disabled = true;

    try {
        const res = await authFetch(`${API_BASE}/prompts/${layer}`);
        if (!res.ok) throw new Error('Failed to load prompt');
        const data = unwrapApiResponse(await res.json());

        // Extract prompt content correctly from response
        const prompt = data.prompt || {};
        const promptInfo = {
            content: prompt.content || '',
            source: data.source || 'default',
            version: prompt.version || '1.3',
        };

        state.promptData[layer] = promptInfo;
        displayPromptData(layer, promptInfo);
    } catch (err) {
        const msg = err?.message || 'Unknown error';
        editor.value = `Failed to load prompt template.\n\nError: ${msg}\n\nIf you just logged in, try refreshing the page.`;
        editor.disabled = false;
    }
}

async function savePrompt() {
    const editor = document.getElementById('prompt-editor');
    const layer = state.activePromptLayer;
    const content = editor.value;

    if (!content.trim()) {
        showToast('Prompt cannot be empty', 'warning');
        return;
    }

    try {
        const res = await authFetch(`${API_BASE}/prompts/${layer}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });

        if (res.ok) {
            const data = unwrapApiResponse(await res.json());
            const prompt = data.prompt || {};

            // Update cache with saved content
            state.promptData[layer] = {
                content: content,
                source: 'custom',
                version: prompt.version || 'custom',
            };
            state.promptDirty = false;

            const badge = document.getElementById('prompt-source-badge');
            badge.textContent = `Custom v${prompt.version || 1}`;
            badge.className = 'prompt-source-badge custom';

            // Refresh version history after save
            loadVersionHistory(layer);

            showToast(`Prompt for ${LAYERS[layer]?.shortLabel || layer} saved`, 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            throw new Error(extractErrorMessage(err));
        }
    } catch (err) {
        showToast(`Failed to save prompt: ${err.message}`, 'error');
    }
}

async function resetPrompt() {
    const layer = state.activePromptLayer;
    const layerLabel = LAYERS[layer]?.shortLabel || layer;

    if (!confirm(`Load the default prompt for ${layerLabel} into the editor?\n\nYou will need to click \"Save Prompt\" to apply it.`)) return;

    try {
        // Use the read-only /default endpoint — does NOT modify DB
        const res = await authFetch(`${API_BASE}/prompts/${layer}/default`);
        if (!res.ok) {
            // Fallback: try the old reset endpoint for backward compat
            const fallbackRes = await authFetch(`${API_BASE}/prompts/${layer}/reset`, { method: 'POST' });
            if (fallbackRes.ok) {
                delete state.promptData[layer];
                showToast('Prompt reset to default', 'info');
                await loadPrompt(layer);
            } else {
                showToast('Failed to load default prompt', 'error');
            }
            return;
        }

        const data = unwrapApiResponse(await res.json());
        const defaultContent = data.content || '';

        // Load default content into editor WITHOUT saving to DB
        const editor = document.getElementById('prompt-editor');
        editor.value = defaultContent;
        editor.disabled = false;
        state.promptDirty = true; // Mark as dirty — user must click Save

        // Update badge to indicate unsaved default
        const badge = document.getElementById('prompt-source-badge');
        badge.textContent = `Default v${data.version || '?'} (unsaved)`;
        badge.className = 'prompt-source-badge default';

        updateCharCount();
        showToast(`Default prompt loaded for ${layerLabel}. Click \"Save Prompt\" to apply.`, 'info');
    } catch {
        showToast('Failed to load default prompt', 'error');
    }
}

// deleteCustomLayer is defined once below (merged definition)

// ============================================
// Version History + Rollback
// ============================================

async function loadVersionHistory(layerName) {
    const panel = document.getElementById('version-history-panel');
    if (!panel) return;

    try {
        const res = await authFetch(`${API_BASE}/prompts/${layerName}/history`);
        if (!res.ok) { panel.innerHTML = '<p class="text-muted">No version history available.</p>'; return; }
        const data = unwrapApiResponse(await res.json());
        const versions = data.versions || [];

        if (versions.length === 0) {
            panel.innerHTML = '<p class="text-muted">No previous versions.</p>';
            return;
        }

        const activeVersionId = data.active_version_id || 'current';

        let html = '<div class="version-list">';
        versions.forEach(v => {
            const isCurrent = v.id === 'current';
            const isActive = v.id === activeVersionId;
            const date = v.created_at ? new Date(v.created_at).toLocaleString() : 'Current';
            const label = v.version_label || v.version || (isCurrent ? 'Current' : '?');
            const preview = (v.content || '').substring(0, 80).replace(/</g, '&lt;');
            html += `
                <div class="version-item ${isActive ? 'active-version' : ''}">
                    <div class="version-header">
                        <span class="version-label">${escapeHtml(label)}</span>
                        <span class="version-date">${date}</span>
                        ${isActive ? '<span class="version-active-badge">Active</span>' : ''}
                    </div>
                    <div class="version-preview">${escapeHtml(preview)}...</div>
                    <div class="version-actions">
                        ${isCurrent
                    ? (isActive ? '<span class="text-muted">Currently active</span>'
                        : `<button class="btn btn-xs btn-outline" onclick="setActiveVersion('${layerName}', null)">Use Current</button>`)
                    : `<button class="btn btn-xs btn-outline" onclick="setActiveVersion('${layerName}', '${v.id}')">Use This</button>
                               <button class="btn btn-xs btn-outline" onclick="rollbackToVersion('${layerName}', '${v.id}')">Rollback</button>`
                }
                    </div>
                </div>`;
        });
        html += '</div>';
        panel.innerHTML = html;
    } catch {
        panel.innerHTML = '<p class="text-muted">Failed to load history.</p>';
    }
}

async function setActiveVersion(layerName, versionId) {
    try {
        const res = await authFetch(`${API_BASE}/prompts/${layerName}/set-version`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ version_id: versionId }),
        });
        if (res.ok) {
            showToast(versionId ? 'Switched to selected version' : 'Switched to latest version', 'success');
            delete state.promptData[layerName];
            await loadPrompt(layerName);
            loadVersionHistory(layerName);
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Failed: ${extractErrorMessage(err)}`, 'error');
        }
    } catch (err) {
        showToast(`Version switch failed: ${err.message}`, 'error');
    }
}

async function rollbackToVersion(layerName, versionId) {
    if (!confirm('Rollback will copy this version\'s content as the new current prompt. Continue?')) return;
    try {
        const res = await authFetch(`${API_BASE}/prompts/${layerName}/rollback/${versionId}`, {
            method: 'POST',
        });
        if (res.ok) {
            showToast('Rolled back successfully', 'success');
            delete state.promptData[layerName];
            await loadPrompt(layerName);
            loadVersionHistory(layerName);
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Rollback failed: ${extractErrorMessage(err)}`, 'error');
        }
    } catch (err) {
        showToast(`Rollback failed: ${err.message}`, 'error');
    }
}

function updateCharCount() {
    const editor = document.getElementById('prompt-editor');
    const charCount = document.getElementById('prompt-char-count');
    charCount.textContent = `${editor.value.length} characters`;
}

// ============================================
// System Info Bar
// ============================================

async function loadSystemInfo() {
    const bar = document.getElementById('system-info-bar');
    if (!bar) return;

    try {
        // Fetch comprehensive system info
        const res = await authFetch(`${API_BASE}/system/info`);
        if (!res.ok) throw new Error('Failed to load system info');

        const data = unwrapApiResponse(await res.json());

        const backendLabel = (data.llm_backend || 'ollama').toUpperCase();
        const activeModel = data.active_model || 'Not set';
        const workerCount = data.worker_count || 1;
        const llmOk = data.llm_connected;
        const redisOk = data.redis_connected !== false; // default true for backward compat
        const temperature = data.temperature ?? 0.0;
        const promptVersion = data.prompt_version || '—';
        const supportsTemp = data.supports_temperature_override !== false;

        // Tri-state status logic: API+Redis+Model
        let statusClass, statusLabel, statusTooltip;
        if (llmOk && redisOk) {
            statusClass = 'connected';
            statusLabel = 'Backend Connected';
            statusTooltip = 'API, Redis, and Model backend all healthy';
        } else if (!llmOk && redisOk) {
            statusClass = 'reconnecting';
            statusLabel = 'Backend Degraded';
            statusTooltip = `Redis: OK | Model: Down`;
        } else {
            statusClass = 'disconnected';
            statusLabel = 'Backend Degraded';
            statusTooltip = `Redis: ${redisOk ? 'OK' : 'Down'} | Model: ${llmOk ? 'OK' : 'Down'}`;
        }

        // Read-only inference parameter display with tooltips
        const seedDisplay = data.seed != null ? data.seed : 'None';
        const paramHtml = `
            <div class="param-display-panel">
                <div class="param-item" title="Set to 0 for fully deterministic, reproducible outputs (greedy decoding).">
                    <span class="param-label">temp</span>
                    <span class="param-value">${temperature}</span>
                </div>
                <div class="param-item" title="Random seed for reproducible outputs. 'None' = non-deterministic.">
                    <span class="param-label">seed</span>
                    <span class="param-value">${seedDisplay}</span>
                </div>
                <div class="param-item" title="Nucleus sampling disabled (=1.0). Combined with temp=0 for deterministic output.">
                    <span class="param-label">top_p</span>
                    <span class="param-value">1.0</span>
                </div>
            </div>`;

        bar.innerHTML = `
            <div class="sys-info-item" id="connection-status-indicator">
                <span class="connection-indicator ${statusClass}" title="${statusTooltip}">
                    <span class="status-dot"></span>
                    ${statusLabel}
                </span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Backend:</span>
                <span class="sys-info-value sys-info-backend">${backendLabel}</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Model:</span>
                <span class="sys-info-value" id="sys-info-model">${activeModel}</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Params:</span>
                ${paramHtml}
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Prompt:</span>
                <span class="sys-info-value" title="Prompt version">${promptVersion}</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Workers:</span>
                <span class="sys-info-value" id="sys-info-workers">${workerCount}</span>
            </div>
            ${data.llm_error ? `
            <div class="sys-info-item sys-info-error">
                <span class="sys-info-label">Error:</span>
                <span class="sys-info-value" title="${escapeHtml(data.llm_error)}">${escapeHtml(data.llm_error).substring(0, 30)}...</span>
            </div>
            ` : ''}
        `;
        bar.classList.remove('hidden');

        // Store system info in state for later use
        state.systemInfo = data;
        updateConnectionStatus(statusClass);
        updateFooterStatus(redisOk, llmOk);

        // Populate inference parameter controls
        populateInferenceParams(data);
        // Populate execution mode controls (chained/comparison)
        populateExecutionMode(data);
    } catch (err) {
        console.error('System info load failed:', err);
        bar.innerHTML = `
            <div class="sys-info-item" id="connection-status-indicator">
                <span class="connection-indicator disconnected" title="Cannot reach backend API">
                    <span class="status-dot"></span>
                    Backend Offline
                </span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-value">Check that backend services are running</span>
            </div>
        `;
        bar.classList.remove('hidden');
        updateConnectionStatus('disconnected');
        updateFooterStatus(false, false);
    }
}

function updateSystemInfoModel(model) {
    const el = document.getElementById('sys-info-model');
    if (el) el.textContent = model;
}

function updateFooterStatus(redisOk, llmOk) {
    const footer = document.getElementById('footer-status');
    if (!footer) return;
    const apiOk = true; // if we got here, API was reachable
    footer.innerHTML = `
        <span class="footer-status-item">
            <span class="footer-status-dot ${apiOk ? 'ok' : 'error'}"></span>
            Backend: ${apiOk ? 'Healthy' : 'Down'}
        </span>
        <span class="footer-status-item">
            <span class="footer-status-dot ${redisOk ? 'ok' : 'error'}"></span>
            Redis: ${redisOk ? 'Connected' : 'Down'}
        </span>
        <span class="footer-status-item">
            <span class="footer-status-dot ${llmOk ? 'ok' : 'error'}"></span>
            Model: ${llmOk ? 'Ready' : 'Offline'}
        </span>
    `;
}

async function updateTemperature(value) {
    const temp = parseFloat(value);
    if (isNaN(temp) || temp < 0 || temp > 2) {
        showToast('Temperature must be 0.0 – 2.0', 'warning');
        return;
    }
    try {
        const res = await authFetch(`${API_BASE}/system/temperature`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ temperature: temp }),
        });
        if (res.ok) {
            showToast(`Temperature set to ${temp}`, 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Failed: ${extractErrorMessage(err)}`, 'error');
        }
    } catch (err) {
        showToast(`Temperature update failed: ${err.message}`, 'error');
    }
}

async function updateSeed(value) {
    const seed = value === '' || value == null ? null : parseInt(value, 10);
    if (seed !== null && isNaN(seed)) {
        showToast('Seed must be a whole number or empty', 'warning');
        return;
    }
    try {
        const res = await authFetch(`${API_BASE}/system/seed`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ seed }),
        });
        if (res.ok) {
            const msg = seed !== null ? `Seed set to ${seed}` : 'Seed cleared (non-deterministic)';
            showToast(msg, 'success');
        } else {
            const err = await res.json().catch(() => ({}));
            showToast(`Failed: ${extractErrorMessage(err)}`, 'error');
        }
    } catch (err) {
        showToast(`Seed update failed: ${err.message}`, 'error');
    }
}

function clearSeed() {
    const seedInput = document.getElementById('seed-input');
    if (seedInput) {
        seedInput.value = '';
        markParamsDirty();
    }
}

function populateInferenceParams(data) {
    const tempSlider = document.getElementById('temperature-slider');
    const tempInput = document.getElementById('temperature-input');
    const seedInput = document.getElementById('seed-input');
    const badge = document.getElementById('inference-params-badge');
    const saveBtn = document.getElementById('save-params-btn');

    if (tempSlider && tempInput) {
        const temp = data.temperature ?? 0.0;
        tempSlider.value = temp;
        tempInput.value = temp;

        // Sync slider <-> input
        tempSlider.addEventListener('input', () => {
            tempInput.value = tempSlider.value;
            markParamsDirty();
        });
        tempInput.addEventListener('input', () => {
            const v = parseFloat(tempInput.value);
            if (!isNaN(v) && v >= 0 && v <= 1) {
                tempSlider.value = v;
            }
            markParamsDirty();
        });
    }

    if (seedInput) {
        seedInput.value = data.seed != null ? data.seed : '';
        seedInput.addEventListener('input', markParamsDirty);
    }

    // Store initial values for dirty detection
    state._initialTemp = data.temperature ?? 0.0;
    state._initialSeed = data.seed;

    // Reset dirty state
    if (saveBtn) saveBtn.disabled = true;
    if (badge) {
        const isCustom = (data.temperature != null && data.temperature !== 0) || data.seed != null;
        badge.textContent = isCustom ? 'Custom' : 'Defaults';
        badge.className = `inference-params-badge${isCustom ? ' custom' : ''}`;
    }
}

function markParamsDirty() {
    const saveBtn = document.getElementById('save-params-btn');
    if (saveBtn) saveBtn.disabled = false;

    const badge = document.getElementById('inference-params-badge');
    if (badge) {
        badge.textContent = 'Unsaved';
        badge.className = 'inference-params-badge custom';
    }
}

async function saveInferenceParams() {
    const tempInput = document.getElementById('temperature-input');
    const seedInput = document.getElementById('seed-input');
    const saveBtn = document.getElementById('save-params-btn');

    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving...';
    }

    try {
        // Save temperature
        const temp = parseFloat(tempInput?.value ?? '0');
        await updateTemperature(temp);

        // Save seed
        const seedVal = seedInput?.value?.trim();
        await updateSeed(seedVal === '' ? null : seedVal);

        // Refresh system info to update status bar
        await loadSystemInfo();

        showToast('Inference parameters saved', 'success');
    } catch (err) {
        showToast(`Failed to save parameters: ${err.message}`, 'error');
    } finally {
        if (saveBtn) {
            saveBtn.innerHTML = '<i data-lucide="save" class="btn-icon"></i> Save Parameters';
            saveBtn.disabled = true;
            if (window.lucide) lucide.createIcons();
        }
    }
}

// ============================================
// Execution Mode Controls (Radio-based)
// ============================================

function populateExecutionMode(data) {
    // Read unified execution_mode from system info (default: 'independent')
    const mode = data.execution_mode || (
        data.comparison_mode ? 'comparison' :
        data.chained_mode ? 'chained' : 'independent'
    );

    // Set the correct radio button
    const radio = document.querySelector(`input[name="execution-mode"][value="${mode}"]`);
    if (radio) radio.checked = true;

    // Store initial value for dirty detection
    state._initialExecutionMode = mode;

    const saveBtn = document.getElementById('save-execution-mode-btn');
    if (saveBtn) saveBtn.disabled = true;

    // Update badge
    const badge = document.getElementById('execution-mode-badge');
    if (badge) {
        const badgeMap = {
            'comparison': { text: 'Comparison', cls: 'execution-mode-badge comparison' },
            'chained': { text: 'Chained', cls: 'execution-mode-badge chained' },
            'independent': { text: 'Independent', cls: 'execution-mode-badge' },
        };
        const b = badgeMap[mode] || badgeMap['independent'];
        badge.textContent = b.text;
        badge.className = b.cls;
    }
}

function markExecutionModeDirty() {
    const selected = document.querySelector('input[name="execution-mode"]:checked')?.value || 'independent';
    const saveBtn = document.getElementById('save-execution-mode-btn');

    // Only enable save if value changed from server state
    if (saveBtn) {
        saveBtn.disabled = selected === (state._initialExecutionMode || 'independent');
    }

    const badge = document.getElementById('execution-mode-badge');
    if (badge && selected !== (state._initialExecutionMode || 'independent')) {
        badge.textContent = 'Unsaved';
        badge.className = 'execution-mode-badge custom';
    } else if (badge) {
        // Restore badge to match selection
        const badgeMap = {
            'comparison': { text: 'Comparison', cls: 'execution-mode-badge comparison' },
            'chained': { text: 'Chained', cls: 'execution-mode-badge chained' },
            'independent': { text: 'Independent', cls: 'execution-mode-badge' },
        };
        const b = badgeMap[selected] || badgeMap['independent'];
        badge.textContent = b.text;
        badge.className = b.cls;
    }
}

async function saveExecutionMode() {
    const selected = document.querySelector('input[name="execution-mode"]:checked')?.value || 'independent';
    const saveBtn = document.getElementById('save-execution-mode-btn');

    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving...';
    }

    try {
        const res = await authFetch(`${API_BASE}/system/execution-mode`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: selected }),
        });
        if (!res.ok) throw new Error('Failed to save execution mode');

        // Refresh system info to sync state
        await loadSystemInfo();

        const modeLabel = { independent: 'Independent', chained: 'Chained', comparison: 'Comparison' };
        showToast(`Execution mode set to: ${modeLabel[selected] || selected}`, 'success');
    } catch (err) {
        showToast(`Failed to save execution mode: ${err.message}`, 'error');
    } finally {
        if (saveBtn) {
            saveBtn.innerHTML = '<i data-lucide="save" class="btn-icon"></i> Save Execution Mode';
            saveBtn.disabled = true;
            if (window.lucide) lucide.createIcons();
        }
    }
}

// ============================================
// Comparison Results Renderer
// ============================================

function renderComparisonResultCard(r, caseIndex) {
    const ind = r.independent || {};
    const chn = r.chained || {};
    const cmp = r.comparison || {};

    const indVerdict = ind.final_verdict ?? '—';
    const chnVerdict = chn.final_verdict ?? '—';
    const indCci = ind.final_cci ?? '—';
    const chnCci = chn.final_cci ?? '—';

    const cciDelta = cmp.cci_delta ?? 0;
    const cciDeltaClass = cciDelta > 0 ? 'delta-up' : (cciDelta < 0 ? 'delta-down' : 'delta-same');
    const cciDeltaIcon = cciDelta > 0 ? '↑' : (cciDelta < 0 ? '↓' : '=');

    const verdictChanged = cmp.verdict_changed ?? false;
    const gradesDifferent = cmp.grade_difference ?? false;

    const indComplications = (ind.layer2_output?.complications || []);
    const chnComplications = (chn.layer2_output?.complications || []);

    const indGrades = indComplications.map(c => c.cd_grade || '?');
    const chnGrades = chnComplications.map(c => c.cd_grade || '?');

    return `
    <div class="comparison-result-card">
        <div class="comparison-header">
            <span class="case-number">Case ${r.case_number ?? caseIndex + 1}</span>
            <span class="execution-mode-label comparison-label">Comparison Mode</span>
        </div>

        <div class="comparison-delta-bar">
            <div class="delta-item ${cciDeltaClass}">
                <span class="delta-label">CCI Δ</span>
                <span class="delta-value">${cciDeltaIcon} ${Math.abs(cciDelta).toFixed(1)}</span>
            </div>
            <div class="delta-item ${verdictChanged ? 'delta-changed' : 'delta-same'}">
                <span class="delta-label">Verdict</span>
                <span class="delta-value">${verdictChanged ? '⚠ Changed' : '✓ Same'}</span>
            </div>
            <div class="delta-item ${gradesDifferent ? 'delta-changed' : 'delta-same'}">
                <span class="delta-label">Grades</span>
                <span class="delta-value">${gradesDifferent ? '⚠ Different' : '✓ Same'}</span>
            </div>
        </div>

        <div class="comparison-panels">
            <div class="comparison-panel panel-independent">
                <h4 class="panel-title"><i data-lucide="split-square-horizontal"></i> Independent</h4>
                <div class="panel-stats">
                    <div class="stat-row">
                        <span class="stat-label">Verdict</span>
                        <span class="verdict-badge verdict-${(indVerdict + '').toLowerCase().replace(/\\s/g, '')}">${indVerdict}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">CCI</span>
                        <span class="cci-value">${indCci}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">Complications</span>
                        <span>${indComplications.length}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">CD Grades</span>
                        <div class="cd-grades">${indGrades.map(g => `<span class="cd-grade-badge">${g}</span>`).join('') || '<span class="no-grades">None</span>'}</div>
                    </div>
                </div>
                <details class="json-details">
                    <summary>Layer 2 JSON <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${caseIndex}]?.independent?.layer2_output ?? {}, this)">Copy</button></summary>
                    <pre class="json-tree">${escapeHtml(JSON.stringify(ind.layer2_output || {}, null, 2))}</pre>
                </details>
                <details class="json-details">
                    <summary>Layer 3 JSON <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${caseIndex}]?.independent?.layer3_output ?? {}, this)">Copy</button></summary>
                    <pre class="json-tree">${escapeHtml(JSON.stringify(ind.layer3_output || {}, null, 2))}</pre>
                </details>
            </div>

            <div class="comparison-panel panel-chained">
                <h4 class="panel-title"><i data-lucide="link"></i> Chained</h4>
                <div class="panel-stats">
                    <div class="stat-row">
                        <span class="stat-label">Verdict</span>
                        <span class="verdict-badge verdict-${(chnVerdict + '').toLowerCase().replace(/\\s/g, '')}">${chnVerdict}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">CCI</span>
                        <span class="cci-value">${chnCci}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">Complications</span>
                        <span>${chnComplications.length}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">CD Grades</span>
                        <div class="cd-grades">${chnGrades.map(g => `<span class="cd-grade-badge">${g}</span>`).join('') || '<span class="no-grades">None</span>'}</div>
                    </div>
                </div>
                <details class="json-details">
                    <summary>Layer 2 JSON <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${caseIndex}]?.chained?.layer2_output ?? {}, this)">Copy</button></summary>
                    <pre class="json-tree">${escapeHtml(JSON.stringify(chn.layer2_output || {}, null, 2))}</pre>
                </details>
                <details class="json-details">
                    <summary>Layer 3 JSON <button class="btn btn-xs btn-copy" onclick="event.stopPropagation(); copyToClipboard(window._resultData[${caseIndex}]?.chained?.layer3_output ?? {}, this)">Copy</button></summary>
                    <pre class="json-tree">${escapeHtml(JSON.stringify(chn.layer3_output || {}, null, 2))}</pre>
                </details>
            </div>
        </div>

        <details class="json-details comparison-diff-details">
            <summary>Comparison Diff JSON</summary>
            <pre class="json-tree">${escapeHtml(JSON.stringify(cmp, null, 2))}</pre>
        </details>
    </div>`;
}

// ============================================
// Utilities
// ============================================

function extractErrorMessage(err) {
    if (!err) return 'Unknown error';
    if (typeof err === 'string') return err;

    // FastAPI error response with detail field
    if (typeof err.detail === 'string') return err.detail;

    // FastAPI 422 validation errors: detail is an array
    if (Array.isArray(err.detail)) {
        const messages = err.detail.map(d => {
            const field = d.loc ? d.loc.filter(l => l !== 'body').join('.') : 'field';
            const msg = d.msg || 'validation error';
            return `${field}: ${msg}`;
        });
        return messages.length > 3
            ? `${messages.slice(0, 3).join('; ')} and ${messages.length - 3} more errors`
            : messages.join('; ');
    }

    // Standard error object
    if (err.message) return err.message;

    // Error with nested error object (middleware format: {error: {code, message}})
    if (err.error && typeof err.error === 'object') return err.error.message || err.error.code || JSON.stringify(err.error);
    if (err.error && typeof err.error === 'string') return err.error;

    // Fallback to JSON
    try {
        return JSON.stringify(err);
    } catch {
        return 'Unknown error';
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

const _escDiv = document.createElement('div');
function escapeHtml(text) {
    if (!text) return '';
    _escDiv.textContent = text;
    return _escDiv.innerHTML;
}

// ============================================
// Job Cancellation
// ============================================

async function cancelJob() {
    if (!state.currentJobId || !state.isProcessing) {
        showToast('No active job to cancel', 'warning');
        return;
    }

    if (!confirm('Are you sure you want to cancel the current job?')) return;

    // Disable cancel button immediately to prevent double-clicks
    setCancelButtonEnabled(false);

    try {
        console.log('[CANCEL] Requesting cancel for job:', state.currentJobId);
        const res = await authFetch(`${API_BASE}/jobs/${state.currentJobId}/cancel`, {
            method: 'POST',
        });

        const json = await res.json().catch(() => ({}));
        console.log('[CANCEL] Response:', res.status, json);

        if (res.ok) {
            closeStream();
            state.isProcessing = false;
            updateProcessButton();
            const cancelMode = json?.data?.cancel_mode || 'unknown';
            const completed = json?.data?.completed_cases ?? '?';
            const total = json?.data?.total_cases ?? '?';
            showToast(`Job cancelled (${completed}/${total} cases completed)`, 'info');
            // Load partial results if any cases completed
            if (state.currentJobId) {
                loadResults(state.currentJobId);
            }
            state.currentJobId = null;
        } else if (res.status === 409) {
            // Job already finished — clean up UI
            showToast(`Cannot cancel: ${extractErrorMessage(json)}`, 'warning');
            state.isProcessing = false;
            state.currentJobId = null;
            updateProcessButton();
        } else {
            // 500 or other error — show actual message
            const errMsg = json?.error?.message || json?.detail || extractErrorMessage(json) || `Server error (${res.status})`;
            setCancelButtonEnabled(true);
            showToast(`Failed to cancel: ${errMsg}`, 'error');
        }
    } catch (err) {
        console.error('[CANCEL] Exception:', err);
        setCancelButtonEnabled(true);
        showToast(`Cancel failed: ${err.message}`, 'error');
    }
}

/**
 * Resume an interrupted/partial batch job. Re-runs only the unfinished cases
 * (POST /jobs/{id}/resume); COMPLETED cases are skipped idempotently by the backend.
 * Safe to use after a crash, disconnect, or a run that completed with failures.
 */
async function resumeJob() {
    const jobId = state.currentJobId || state.lastResumableJobId || state.lastCompletedJobId;
    if (!jobId) {
        showToast('No job to resume', 'warning');
        return;
    }

    setResumeButtonEnabled(false);
    try {
        console.log('[RESUME] Requesting resume for job:', jobId);
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/resume`, { method: 'POST' });
        const json = await res.json().catch(() => ({}));
        console.log('[RESUME] Response:', res.status, json);

        if (res.ok) {
            const queued = json?.data?.cases_queued ?? '?';
            if (queued === 0) {
                showToast('Nothing to resume — all cases complete', 'info');
                return;
            }
            showToast(`Resuming ${queued} unfinished case(s)...`, 'info');
            // Re-enter the live processing view and reconnect the stream.
            state.currentJobId = jobId;
            state.isProcessing = true;
            updateProcessButton();
            showProgress();
            startStreaming(jobId);
        } else {
            const errMsg = json?.error?.message || json?.detail || extractErrorMessage(json) || `Server error (${res.status})`;
            setResumeButtonEnabled(true);
            showToast(`Failed to resume: ${errMsg}`, 'error');
        }
    } catch (err) {
        console.error('[RESUME] Exception:', err);
        setResumeButtonEnabled(true);
        showToast(`Resume failed: ${err.message}`, 'error');
    }
}

// ============================================
// Prompt Import/Export
// ============================================

async function exportPrompts() {
    try {
        const res = await authFetch(`${API_BASE}/prompts/export`);
        if (!res.ok) throw new Error('Failed to export prompts');

        const data = unwrapApiResponse(await res.json());

        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai-prompts-${new Date().toISOString().split('T')[0]}.json`;
        a.click();
        URL.revokeObjectURL(url);

        showToast('Prompts exported successfully', 'success');
    } catch (err) {
        showToast(`Export failed: ${err.message}`, 'error');
    }
}

async function importPrompts() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';

    input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        // File size limit: 5MB
        if (file.size > 5 * 1024 * 1024) {
            showToast('Import file too large (max 5MB)', 'error');
            return;
        }

        try {
            const text = await file.text();
            const data = JSON.parse(text);

            if (!data.prompts || typeof data.prompts !== 'object' || Array.isArray(data.prompts)) {
                throw new Error('Invalid format: expected { "prompts": { layerName: { content: "..." } } }');
            }

            // Validate individual prompts
            const entries = Object.entries(data.prompts);
            if (entries.length > 20) {
                throw new Error(`Too many prompts (${entries.length}). Maximum is 20.`);
            }
            for (const [name, prompt] of entries) {
                if (!prompt.content || typeof prompt.content !== 'string' || prompt.content.length < 10) {
                    throw new Error(`Prompt "${name}" has missing or too-short content (min 10 chars)`);
                }
            }

            const res = await authFetch(`${API_BASE}/prompts/import`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompts: data.prompts }),
            });

            if (res.ok) {
                const result = await res.json();
                showToast(result.data?.message || 'Prompts imported', 'success');
                // Reload prompts
                state.promptData = {};
                await loadAllPrompts();
            } else {
                const err = await res.json().catch(() => ({}));
                throw new Error(extractErrorMessage(err));
            }
        } catch (err) {
            showToast(`Import failed: ${err.message}`, 'error');
        }
    };

    input.click();
}

// ============================================
// Delete Custom Layer
// ============================================

/**
 * Delete a custom (non-builtin) layer.
 * Can be called with an explicit layerName (from the tab × button)
 * or without arguments (from the dedicated delete button — uses active layer).
 */
async function deleteCustomLayer(layerName) {
    const layer = layerName || state.activePromptLayer;
    if (BUILTIN_LAYERS.includes(layer) || LAYERS[layer]?.is_builtin) {
        showToast('Cannot delete built-in layers', 'warning');
        return;
    }
    if (!confirm(`Delete custom layer "${LAYERS[layer]?.label || layer}"?\n\nThis will remove the layer and its prompt history.`)) return;

    try {
        const res = await authFetch(`${API_BASE}/prompts/${layer}`, { method: 'DELETE' });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(extractErrorMessage(err));
        }

        showToast(`Layer "${LAYERS[layer]?.label || layer}" deleted`, 'info');
        delete state.promptData[layer];

        // Reload layers and switch to first layer
        await loadLayers();
        state.promptData = {};
        await loadAllPrompts();
        switchPromptTab(Object.keys(LAYERS)[0] || 'layer1_ctp');
    } catch (err) {
        showToast(`Delete failed: ${err.message}`, 'error');
    }
}

// ============================================
// vLLM Health Check
// ============================================

async function checkVllmHealth() {
    const banner = document.getElementById('vllm-status-banner');
    if (!banner) return;

    try {
        const res = await authFetch(`${API_BASE.replace('/api/v1', '')}/health/vllm`);
        if (res.ok) {
            banner.classList.add('hidden');
        } else {
            const data = await res.json().catch(() => ({}));
            const hint = data.hint || 'Start the GPU model service.';
            banner.innerHTML = `<i data-lucide="alert-triangle"></i> <strong>Model Offline</strong> — ${hint}`;
            banner.classList.remove('hidden');
            if (window.lucide) lucide.createIcons();
        }
    } catch {
        banner.innerHTML = '<i data-lucide="alert-triangle"></i> <strong>Model Offline</strong> — Cannot reach backend.';
        banner.classList.remove('hidden');
        if (window.lucide) lucide.createIcons();
    }
}

// ============================================
// Auth Functions
// ============================================

function showAuthOverlay() {
    document.getElementById('auth-overlay')?.classList.add('active');
}

function hideAuthOverlay() {
    document.getElementById('auth-overlay')?.classList.remove('active');
}

function showLogin(e) {
    if (e) e.preventDefault();
    document.getElementById('login-form')?.classList.remove('hidden');
    document.getElementById('signup-form')?.classList.add('hidden');
}

function showSignup(e) {
    if (e) e.preventDefault();
    document.getElementById('login-form')?.classList.add('hidden');
    document.getElementById('signup-form')?.classList.remove('hidden');
}

function updateUserDisplay() {
    if (authState.user) {
        const nameEl = document.getElementById('user-display-name');
        const roleEl = document.getElementById('user-role-badge');
        if (nameEl) nameEl.textContent = authState.user.name || authState.user.username;
        if (roleEl) {
            roleEl.textContent = authState.user.role;
            roleEl.className = `user-role-badge role-${authState.user.role}`;
        }
    }
}

async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('login-error');
    const btn = document.getElementById('login-btn');

    errorEl.classList.add('hidden');
    btn.disabled = true;
    btn.textContent = 'Signing in...';

    try {
        const res = await fetch(`${API_BASE}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });

        let data;
        try {
            data = await res.json();
        } catch {
            throw new Error(`Server returned non-JSON response (HTTP ${res.status}). Backend may be unavailable.`);
        }

        if (!res.ok) {
            const msg = data.error?.message || data.detail || 'Login failed';
            throw new Error(msg);
        }

        authState.token = data.data.token;
        authState.user = data.data.user;
        localStorage.setItem('snapai_token', authState.token);
        localStorage.setItem('snapai_user', JSON.stringify(authState.user));

        hideAuthOverlay();
        initApp();
        showToast(`Welcome, ${authState.user.name}!`, 'success');

        // Remove admin functions from window for non-admin users
        if (authState.user.role !== 'admin') {
            delete window.approveUser;
            delete window.rejectUser;
            delete window.loadAdminUsers;
        }
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sign In';
    }
}

async function handleSignup(e) {
    e.preventDefault();
    const name = document.getElementById('signup-name').value;
    const username = document.getElementById('signup-username').value;
    const password = document.getElementById('signup-password').value;
    const errorEl = document.getElementById('signup-error');
    const successEl = document.getElementById('signup-success');
    const btn = document.getElementById('signup-btn');

    errorEl.classList.add('hidden');
    successEl.classList.add('hidden');
    btn.disabled = true;
    btn.textContent = 'Creating account...';

    try {
        const res = await fetch(`${API_BASE}/auth/signup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, name, password }),
        });

        let data;
        try {
            data = await res.json();
        } catch {
            throw new Error(`Server returned non-JSON response (HTTP ${res.status}). Backend may be unavailable.`);
        }

        if (!res.ok) {
            const msg = data.error?.message || data.detail || 'Signup failed';
            throw new Error(msg);
        }

        successEl.textContent = 'Account created! Waiting for admin approval. You can sign in once approved.';
        successEl.classList.remove('hidden');
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create Account';
    }
}

function handleLogout() {
    const wasLoggedIn = !!authState.token;
    authState.token = null;
    authState.user = null;
    localStorage.removeItem('snapai_token');
    localStorage.removeItem('snapai_user');
    showAuthOverlay();
    if (wasLoggedIn) showToast('Logged out', 'info');
}

// ============================================
// Log Viewer
// ============================================

let logRefreshInterval = null;

function initLogViewer() {
    refreshLogs();
    toggleLogAutoRefresh();
}

async function refreshLogs() {
    const level = document.getElementById('log-level-filter')?.value || '';
    const search = document.getElementById('log-search')?.value || '';
    const container = document.getElementById('log-container');
    if (!container) return;

    try {
        let url = `${API_BASE}/logs?limit=200`;
        if (level) url += `&level=${level}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;

        const res = await authFetch(url);
        if (!res.ok) throw new Error('Failed to fetch logs');
        const data = unwrapApiResponse(await res.json());
        const logs = data.logs || [];

        if (logs.length === 0) {
            container.innerHTML = '<div class="log-empty">No logs found</div>';
            return;
        }

        container.innerHTML = logs.map(log => {
            const levelClass = `log-${(log.level || 'info').toLowerCase()}`;
            const time = log.timestamp ? new Date(log.timestamp).toLocaleTimeString() : '';
            return `<div class="log-entry ${levelClass}">
                <span class="log-time">${time}</span>
                <span class="log-level-badge">${log.level || 'INFO'}</span>
                <span class="log-component">${log.component || 'app'}</span>
                <span class="log-message">${escapeHtml(log.message || '')}</span>
            </div>`;
        }).join('');

    } catch (err) {
        container.innerHTML = `<div class="log-error">Error loading logs: ${err.message}</div>`;
    }
}

function toggleLogAutoRefresh() {
    const checked = document.getElementById('log-auto-refresh')?.checked;
    if (logRefreshInterval) clearInterval(logRefreshInterval);
    if (checked) {
        logRefreshInterval = setInterval(refreshLogs, 5000);
    }
}

// ============================================
// Operations Dashboard (Stage 5)
// ============================================

/**
 * Load the full operations dashboard.
 * Fires all 4 API calls in parallel for fast rendering.
 */
async function loadOperationsDashboard() {
    logger_info('OPERATIONS_VIEW_LOADED');
    // Fire all requests in parallel
    const [summaryP, runtimeP, failuresP, slowP, detP] = [
        authFetch(`${API_BASE}/system/operations-summary`).then(r => r.ok ? r.json() : null).catch(() => null),
        authFetch(`${API_BASE}/system/runtime-status`).then(r => r.ok ? r.json() : null).catch(() => null),
        authFetch(`${API_BASE}/system/failure-analytics`).then(r => r.ok ? r.json() : null).catch(() => null),
        authFetch(`${API_BASE}/system/slow-layers`).then(r => r.ok ? r.json() : null).catch(() => null),
        authFetch(`${API_BASE}/system/determinism`).then(r => r.ok ? r.json() : null).catch(() => null),
    ];
    const [summary, runtime, failures, slowLayers, determinism] = await Promise.all([summaryP, runtimeP, failuresP, slowP, detP]);

    // Section 1: Health Summary KPIs
    if (summary?.data) {
        const d = summary.data;
        setText('ops-active-jobs', d.active_jobs);
        setText('ops-jobs-today', d.jobs_today);
        setText('ops-success-rate', d.success_rate != null ? `${d.success_rate}%` : '—');
        setText('ops-failed-jobs', d.failed_jobs);
        setText('ops-total-jobs', d.total_jobs);
        setText('ops-avg-runtime', d.avg_runtime_ms != null ? opsFormatMs(d.avg_runtime_ms) : '—');
        // Color code success rate
        const srEl = document.getElementById('ops-success-rate');
        if (srEl) srEl.classList.add(d.success_rate >= 95 ? 'ops-good' : d.success_rate >= 80 ? 'ops-warn' : 'ops-bad');
    }

    // Section 2: Runtime Status
    if (runtime?.data) {
        const d = runtime.data;
        setText('ops-model', d.active_model || '—');
        setText('ops-backend', (d.inference_backend || '—').toUpperCase());
        const wEl = document.getElementById('ops-worker-status');
        if (wEl) {
            wEl.textContent = d.worker_alive ? 'Online' : 'Offline';
            wEl.classList.add(d.worker_alive ? 'ops-good' : 'ops-bad');
        }
        setText('ops-queue-size', d.queue_size ?? '—');
    }

    // Section 3: Failure Analytics
    renderFailureAnalytics(failures?.data);

    // Section 4: Slow Layers
    renderSlowLayers(slowLayers?.data);

    // Section 5: Determinism
    renderDeterminism(determinism?.data);
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function opsFormatMs(ms) {
    if (ms == null) return '—';
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
}

function logger_info() { /* lightweight client-side log stub */ }

function renderFailureAnalytics(data) {
    const container = document.getElementById('ops-failure-content');
    if (!container) return;
    if (!data || !data.categories?.length) {
        container.innerHTML = '<div class="ops-empty">No failures recorded.</div>';
        return;
    }
    let html = `<div class="ops-failure-total">Total failures: <strong>${data.total_failures}</strong></div>`;
    html += '<table class="ops-table"><thead><tr><th>Category</th><th>Count</th><th>%</th><th></th></tr></thead><tbody>';
    data.categories.forEach(c => {
        const barW = Math.max(c.percentage, 2);
        html += `<tr>
            <td class="ops-mono">${escapeHtml(c.category)}</td>
            <td>${c.count}</td>
            <td>${c.percentage}%</td>
            <td class="ops-bar-cell"><div class="ops-bar" style="width:${barW}%"></div></td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

function renderSlowLayers(data) {
    const container = document.getElementById('ops-slow-layers-content');
    if (!container) return;
    if (!data || !data.layers?.length) {
        container.innerHTML = '<div class="ops-empty">No layer metrics yet.</div>';
        return;
    }
    let html = '<table class="ops-table"><thead><tr><th>Layer</th><th>Avg Time</th><th>Runs</th><th>Failures</th></tr></thead><tbody>';
    data.layers.forEach(l => {
        const warn = l.avg_duration_ms > 30000;
        html += `<tr${warn ? ' class="ops-row-warn"' : ''}>
            <td class="ops-mono">${escapeHtml(l.layer_id)}</td>
            <td>${opsFormatMs(l.avg_duration_ms)}</td>
            <td>${l.run_count}</td>
            <td class="${l.failure_count > 0 ? 'ops-bad' : ''}">${l.failure_count}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

function renderDeterminism(data) {
    const container = document.getElementById('ops-determinism-content');
    if (!container) return;
    if (!data || data.total_replays === 0) {
        container.innerHTML = '<div class="ops-empty">No replay runs found. Run a replay to see determinism metrics.</div>';
        return;
    }
    const scoreCls = data.determinism_score >= 95 ? 'ops-good' : data.determinism_score >= 80 ? 'ops-warn' : 'ops-bad';
    let html = `<div class="ops-determinism-score">
        <span class="ops-det-number ${scoreCls}">${data.determinism_score}%</span>
        <span class="ops-det-label">Determinism Score</span>
    </div>
    <div class="ops-det-details">
        <span>Total Replays: <strong>${data.total_replays}</strong></span>
        <span>Matching: <strong>${data.matching_runs}</strong></span>
        <span>Mismatches: <strong class="${data.mismatch_runs > 0 ? 'ops-bad' : ''}">${data.mismatch_runs}</strong></span>
    </div>`;
    container.innerHTML = html;
}

// ============================================
// Admin Panel: User Management
// ============================================

/** Generate role badge HTML. */
function _roleBadge(role) {
    const cls = role === 'admin' ? 'role-admin' : role === 'pending' ? 'role-pending' : 'role-doctor';
    return `<span class="user-role-badge ${cls}">${role.toUpperCase()}</span>`;
}

/** Generate status badge HTML. */
function _statusBadge(isActive, role) {
    if (role === 'pending') return '<span class="status-badge status-pending">PENDING</span>';
    if (isActive) return '<span class="status-badge status-active">ACTIVE</span>';
    return '<span class="status-badge status-inactive">INACTIVE</span>';
}

/** Generate action buttons for a user row. */
function _userActions(u, isSelf) {
    const btns = [];
    const id = u.id;

    if (u.role === 'pending') {
        btns.push(`<button class="btn btn-xs btn-success" onclick="approveUser('${id}')">Approve</button>`);
        btns.push(`<button class="btn btn-xs btn-danger" onclick="rejectUser('${id}')">Reject</button>`);
    } else if (isSelf) {
        btns.push('<span class="admin-self-label">You</span>');
    } else {
        // Role actions
        if (u.role === 'doctor') {
            btns.push(`<button class="btn btn-xs btn-warning" onclick="changeUserRole('${id}', 'admin')">Make Admin</button>`);
        } else if (u.role === 'admin') {
            btns.push(`<button class="btn btn-xs btn-secondary" onclick="changeUserRole('${id}', 'doctor')">Remove Admin</button>`);
        }
        // Status toggle
        if (u.is_active) {
            btns.push(`<button class="btn btn-xs btn-danger" onclick="toggleUserStatus('${id}', false)">Deactivate</button>`);
        } else {
            btns.push(`<button class="btn btn-xs btn-success" onclick="toggleUserStatus('${id}', true)">Reactivate</button>`);
        }
    }
    return btns.join(' ');
}

async function loadAdminUsers() {
    const container = document.getElementById('admin-users-list');
    if (!container) return;

    container.innerHTML = '<div class="log-loading">Loading users…</div>';

    try {
        const res = await authFetch(`${API_BASE}/auth/users`);
        if (!res.ok) {
            const body = await tryJson(res);
            throw new Error(body?.error?.message || `Failed to load users (${res.status})`);
        }
        const json = await res.json();
        const users = json?.data?.users || [];
        const currentUserId = authState.user?.id;

        if (users.length === 0) {
            container.innerHTML = '<div class="ops-empty">No users found.</div>';
            return;
        }

        let html = `
            <table class="ops-table admin-users-table">
                <thead>
                    <tr>
                        <th>Username</th>
                        <th>Name</th>
                        <th>Role</th>
                        <th>Status</th>
                        <th>Created</th>
                        <th>Last Login</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>`;

        users.forEach(u => {
            const isSelf = u.id === currentUserId;
            html += `
                <tr>
                    <td class="ops-mono">${escapeHtml(u.username)}</td>
                    <td>${escapeHtml(u.name)}</td>
                    <td>${_roleBadge(u.role)}</td>
                    <td>${_statusBadge(u.is_active, u.role)}</td>
                    <td>${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
                    <td>${u.last_login ? new Date(u.last_login).toLocaleString() : '—'}</td>
                    <td class="admin-actions-cell">${_userActions(u, isSelf)}</td>
                </tr>`;
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div class="ops-empty" style="color:var(--red)">Error: ${escapeHtml(err.message)}</div>`;
    }
}

async function approveUser(userId) {
    if (!confirm('Approve this user?')) return;
    try {
        const res = await authFetch(`${API_BASE}/auth/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId, role: 'doctor' }),
        });
        const data = await tryJson(res);
        if (!res.ok) throw new Error(data?.error?.message || data?.detail || 'Approval failed');
        showToast(data?.data?.message || 'User approved', 'success');
        loadAdminUsers();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function rejectUser(userId) {
    if (!confirm('Reject and deactivate this user?')) return;
    try {
        const res = await authFetch(`${API_BASE}/auth/reject`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId }),
        });
        const data = await tryJson(res);
        if (!res.ok) throw new Error(data?.error?.message || data?.detail || 'Rejection failed');
        showToast(data?.data?.message || 'User rejected', 'success');
        loadAdminUsers();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function toggleUserStatus(userId, activate) {
    const action = activate ? 'reactivate' : 'deactivate';
    if (!confirm(`Are you sure you want to ${action} this user?`)) return;
    try {
        const res = await authFetch(`${API_BASE}/auth/users/${userId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_active: activate }),
        });
        const data = await tryJson(res);
        if (!res.ok) throw new Error(data?.error?.message || data?.detail || `${action} failed`);
        showToast(data?.data?.message || `User ${action}d`, 'success');
        loadAdminUsers();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function changeUserRole(userId, newRole) {
    const label = newRole === 'admin' ? 'promote this user to Admin' : 'demote this user to Doctor';
    if (!confirm(`Are you sure you want to ${label}?`)) return;
    try {
        const res = await authFetch(`${API_BASE}/auth/users/${userId}/role`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: newRole }),
        });
        const data = await tryJson(res);
        if (!res.ok) throw new Error(data?.error?.message || data?.detail || 'Role change failed');
        showToast(data?.data?.message || `Role changed to ${newRole}`, 'success');
        loadAdminUsers();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// ============================================
// Stage 6 — Regression, Audit Export, Locked Config
// ============================================

/**
 * Render the Locked Execution Config banner on the results page.
 * Shows model name, snapshot hash (SHA-256), and created_at timestamp.
 */
function renderLockedConfig(jobData) {
    const banner = document.getElementById('locked-config-banner');
    if (!banner) return;

    const snapshot = jobData?.pipeline_snapshot;
    if (!snapshot || !Array.isArray(snapshot) || snapshot.length === 0) {
        banner.classList.add('hidden');
        return;
    }

    // Compute SHA-256 hash of snapshot JSON (deterministic)
    const snapStr = JSON.stringify(snapshot, Object.keys(snapshot[0] || {}).sort());
    let hash = 0;
    for (let i = 0; i < snapStr.length; i++) {
        const chr = snapStr.charCodeAt(i);
        hash = ((hash << 5) - hash) + chr;
        hash |= 0;
    }
    const hashHex = Math.abs(hash).toString(16).padStart(8, '0');

    const modelEl = document.getElementById('locked-config-model');
    const hashEl = document.getElementById('locked-config-hash');
    const createdEl = document.getElementById('locked-config-created');
    const descEl = document.getElementById('locked-config-description');

    if (modelEl) modelEl.textContent = jobData?.model_name || '—';
    if (hashEl) {
        hashEl.textContent = hashHex;
        hashEl.title = 'Hash of the pipeline configuration used for this run. Ensures deterministic reproducibility.';
    }
    if (createdEl) createdEl.textContent = jobData?.created_at ? new Date(jobData.created_at).toLocaleString() : '—';
    if (descEl) descEl.classList.remove('hidden');

    banner.classList.remove('hidden');

    // Set baseline button state (both locked-config and bottom buttons)
    _updateBaselineButtonsUI(!!jobData?.is_regression_baseline);

    // Upgrade hash to real SHA-256 async
    if (window.crypto && window.crypto.subtle) {
        const enc = new TextEncoder();
        window.crypto.subtle.digest('SHA-256', enc.encode(JSON.stringify(snapshot, null, 0))).then(buf => {
            const arr = Array.from(new Uint8Array(buf));
            const hex = arr.map(b => b.toString(16).padStart(2, '0')).join('');
            if (hashEl) hashEl.textContent = hex.substring(0, 16);
        }).catch(() => { /* keep fallback */ });
    }
    if (window.lucide) lucide.createIcons();

    // Populate Run Info Banner
    const runInfoBanner = document.getElementById('run-info-banner');
    if (runInfoBanner) {
        const runInfoModel = document.getElementById('run-info-model');
        const runInfoHash = document.getElementById('run-info-hash');
        if (runInfoModel) runInfoModel.textContent = jobData?.model_name || '—';
        if (runInfoHash) runInfoHash.textContent = hashHex;
        runInfoBanner.classList.remove('hidden');

        // Upgrade with real SHA-256 if available
        if (window.crypto && window.crypto.subtle) {
            const enc2 = new TextEncoder();
            window.crypto.subtle.digest('SHA-256', enc2.encode(JSON.stringify(snapshot, null, 0))).then(buf2 => {
                const arr2 = Array.from(new Uint8Array(buf2));
                const hex2 = arr2.map(b => b.toString(16).padStart(2, '0')).join('');
                if (runInfoHash) runInfoHash.textContent = hex2.substring(0, 16);
            }).catch(() => { });
        }
    }
}

/**
 * Download the audit export package for the current job.
 */
async function downloadAuditPackage() {
    const jobId = state.currentJobId || localStorage.getItem('snapai_last_job_id');
    if (!jobId) {
        showToast('No job selected', 'warning');
        return;
    }

    const btn = document.getElementById('audit-export-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Exporting…'; }

    try {
        const res = await authFetch(`${API_BASE}/jobs/${jobId}/audit-export`);
        if (!res.ok) {
            const body = await tryJson(res);
            throw new Error(extractErrorMessage(body) || `Failed to generate audit package (${res.status})`);
        }
        const pkg = unwrapApiResponse(await res.json());
        const text = JSON.stringify(pkg, null, 2);
        const blob = new Blob([text], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai-audit-${jobId.substring(0, 8)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast('Audit package downloaded', 'success');
    } catch (err) {
        const msg = err instanceof Error ? err.message : extractErrorMessage(err);
        showToast(`Failed to export audit package. Reason: ${msg || 'Unknown error'}`, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Download Audit Package'; }
    }
}

// ============================================
// Save Case & Export Saved Cases
// ============================================

/**
 * Toggle save/unsave for an individual case.
 * Uses the new /saved-cases collection endpoint (immutable snapshot).
 */
async function toggleSaveCase(caseId, jobId, btn) {
    if (!caseId || !jobId) {
        showToast('Missing case or job identifier', 'warning');
        return;
    }

    const alreadySaved = btn && btn.classList.contains('case-saved');

    if (alreadySaved) {
        showToast('This case is already saved in your collection', 'info');
        return;
    }

    try {
        const result = await saveCaseToCollection(jobId, caseId);
        if (result && btn) {
            btn.classList.add('case-saved');
            btn.innerHTML = `
                <i data-lucide="bookmark-check" style="width:12px;height:12px;display:inline-block;vertical-align:middle;"></i>
                Saved
            `;
            if (window.lucide) lucide.createIcons();
        }
    } catch (err) {
        showToast(err.message || 'Save failed', 'error');
    }
}

/**
 * Export all saved cases as a downloadable JSON dataset.
 */
async function exportSavedCases() {
    try {
        const res = await authFetch(`${API_BASE}/jobs/export-saved`);
        if (!res.ok) throw new Error(`Export failed (${res.status})`);
        const data = unwrapApiResponse(await res.json());
        const text = JSON.stringify(data, null, 2);
        const blob = new Blob([text], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'snap-ai-saved-cases.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast('Saved cases exported', 'success');
    } catch (err) {
        showToast(err.message || 'Export failed', 'error');
    }
}

/**
 * Run the regression check from the Operations dashboard.
 */
async function runRegressionCheck() {
    const btn = document.getElementById('ops-run-regression-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Running…'; }

    try {
        const res = await authFetch(`${API_BASE}/system/run-regression-check`, { method: 'POST' });
        const json = await tryJson(res) || {};
        if (!res.ok || !json.success) {
            throw new Error(extractErrorMessage(json) || `Regression check failed (${res.status})`);
        }
        const data = unwrapApiResponse(json);
        if (!data.details || data.details.length === 0) {
            showToast('No baseline runs marked yet. Mark a run as baseline before running regression checks.', 'info');
        } else {
            const rate = data.total_tests > 0 ? Math.round((data.passed / data.total_tests) * 100) : 0;
            showToast(`Regression check complete — Pass rate: ${rate}% — Baseline runs tested: ${data.total_tests}`, data.failed > 0 ? 'warning' : 'success');
        }
        renderRegressionResults(data);
    } catch (err) {
        const msg = err instanceof Error ? err.message : extractErrorMessage(err);
        showToast(`Regression check failed: ${msg || 'Unknown error'}`, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Run Regression Check'; }
    }
}

/**
 * Render regression check results in the Operations dashboard.
 */
function renderRegressionResults(data) {
    // Update meta
    const tsEl = document.getElementById('ops-regression-timestamp');
    const prEl = document.getElementById('ops-regression-passrate');
    if (tsEl) tsEl.textContent = `Last run: ${data.ran_at ? new Date(data.ran_at).toLocaleString() : '—'}`;
    if (prEl) {
        const rate = data.total_tests > 0 ? Math.round((data.passed / data.total_tests) * 100) : 0;
        prEl.textContent = `Pass rate: ${rate}% (${data.passed}/${data.total_tests})`;
        prEl.className = rate === 100 ? 'ops-good' : rate >= 80 ? 'ops-warn' : 'ops-bad';
    }

    const container = document.getElementById('ops-regression-results');
    if (!container) return;

    if (!data.details || data.details.length === 0) {
        container.innerHTML = '<div class="ops-empty">No baseline jobs found. Mark completed jobs as baselines first.</div>';
        return;
    }

    let html = '<table class="ops-table"><thead><tr><th>Job</th><th>Model</th><th>Layers</th><th>Cases</th><th>Result</th><th>Issues</th></tr></thead><tbody>';
    data.details.forEach(d => {
        const cls = d.passed ? 'ops-good' : 'ops-bad';
        const status = d.passed ? '✓ PASS' : '✗ FAIL';
        html += `<tr>
            <td class="ops-mono" title="${escapeHtml(d.job_id)}">${escapeHtml(d.job_id.substring(0, 8))}…</td>
            <td>${escapeHtml(d.model_name || '—')}</td>
            <td>${d.layer_count}</td>
            <td>${d.case_count}</td>
            <td class="${cls}"><strong>${status}</strong></td>
            <td>${d.issues.length > 0 ? escapeHtml(d.issues.join('; ')) : '—'}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

// ============================================
// Cancelled Job State
// ============================================

function renderCancelledJobState() {
    const cancelledEl = document.getElementById('cancelled-job-state');
    if (cancelledEl) cancelledEl.classList.remove('hidden');

    // For cancelled jobs, KEEP results visible (partial results)
    // Only hide sections that don't apply
    const hideIds = [
        'results-timeline-wrap', 'locked-config-banner',
        'metrics-details-wrap', 'compare-details-wrap',
    ];
    hideIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.add('hidden');
    });

    // Hide snapshot viewer details
    const snapDetails = document.querySelector('.snapshot-details');
    if (snapDetails) snapDetails.classList.add('hidden');

    if (window.lucide) lucide.createIcons();
}

// ============================================
// Saved Cases Panel
// ============================================

async function renderSavedCasesPanel() {
    const panel = document.getElementById('saved-cases-panel');
    const list = document.getElementById('saved-cases-list');
    if (!panel || !list) return;

    try {
        const res = await authFetch(`${API_BASE}/jobs/export-saved`);
        if (!res.ok) { panel.classList.add('hidden'); return; }
        const data = unwrapApiResponse(await res.json());
        const cases = data?.cases || [];

        if (cases.length === 0) {
            panel.classList.add('hidden');
            return;
        }

        panel.classList.remove('hidden');

        list.innerHTML = cases.map(c => `
            <div class="saved-case-card">
                <div class="saved-case-header">
                    <strong>Case #${c.case_number || '\u2014'}</strong>
                    <span class="verdict-badge verdict-${(c.final_verdict || '').toLowerCase().replace(/\s/g, '')}">${escapeHtml(c.final_verdict || '\u2014')}</span>
                    <span class="cci-display">${c.final_cci != null ? c.final_cci.toFixed(2) : '\u2014'}</span>
                </div>
                <p class="saved-case-input">${escapeHtml((c.input_text || '').substring(0, 120))}${(c.input_text || '').length > 120 ? '\u2026' : ''}</p>
                <span class="saved-case-meta">Job: ${escapeHtml((c.job_id || '').substring(0, 8))}\u2026</span>
            </div>
        `).join('');

        if (window.lucide) lucide.createIcons();
    } catch (err) {
        panel.classList.add('hidden');
    }
}

// ============================================
// Role Helper
// ============================================

function isAdmin() {
    return authState.user?.role === 'admin';
}


// ============================================
// Live Pipeline Result Streaming
// ============================================

let _liveCaseThrottleTimer = null;
const _liveCaseQueue = [];

/**
 * Render a completed case card in the progress section as each case finishes.
 * Includes retry logic for race condition where DB write may not be committed yet.
 * Throttled: max 1 card appended per 300ms.
 */
function renderLiveCaseCard(data, jobId) {
    const container = document.getElementById('completed-cases-live');
    const list = document.getElementById('completed-cases-live-list');
    if (!container || !list) return;

    container.classList.remove('hidden');

    const caseNum = data.case_number || '?';
    const verdict = data.verdict || '—';
    const cci = data.cci != null ? parseFloat(data.cci).toFixed(2) : '—';
    const success = data.success !== false;

    const card = document.createElement('div');
    card.className = `live-case-card ${success ? 'success' : 'failed'} slide-in`;
    card.innerHTML = `
        <div class="live-case-card-header">
            <strong>Case #${caseNum}</strong>
            <span class="verdict-badge verdict-${(verdict).toLowerCase().replace(/\s/g, '')}">${escapeHtml(verdict)}</span>
            <span class="cci-display">${cci}</span>
        </div>
    `;

    // Throttle appending to avoid UI spam
    _liveCaseQueue.push(card);
    if (!_liveCaseThrottleTimer) {
        _liveCaseThrottleTimer = setInterval(() => {
            if (_liveCaseQueue.length > 0) {
                const c = _liveCaseQueue.shift();
                list.appendChild(c);
                if (window.lucide) lucide.createIcons();
            } else {
                clearInterval(_liveCaseThrottleTimer);
                _liveCaseThrottleTimer = null;
            }
        }, 300);
    }
}


// ============================================
// Saved Cases Collection (Full CRUD)
// ============================================

/**
 * Save a case to the collection (immutable snapshot).
 * Blocks if case is not completed. Warns on duplicate.
 */
async function saveCaseToCollection(jobId, caseId, caseName) {
    try {
        const res = await authFetch(`${API_BASE}/saved-cases`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_id: jobId, case_id: caseId, name: caseName || '' }),
        });

        if (res.status === 409) {
            const errData = await res.json();
            const msg = errData?.detail || errData?.error?.message || 'This case is already saved';
            showToast(msg, 'warning');
            return;
        }

        const data = await safeJson(res);
        showToast('Case saved!', 'success');
        return data;
    } catch (err) {
        showToast('Failed to save case: ' + err.message, 'error');
    }
}

/**
 * Load all saved cases into the Saved Cases page.
 */
async function loadSavedCases() {
    const container = document.getElementById('saved-cases-collection');
    if (!container) return;

    try {
        const res = await authFetch(`${API_BASE}/saved-cases`);
        const data = await safeJson(res);
        const cases = data?.cases || [];

        if (cases.length === 0) {
            container.innerHTML = `
                <div class="saved-cases-empty">
                    <i data-lucide="bookmark-plus"></i>
                    <p>No saved cases yet. Save a completed case from the results view.</p>
                </div>
            `;
            if (window.lucide) lucide.createIcons();
            return;
        }

        container.innerHTML = cases.map(c => `
            <div class="saved-case-collection-card" onclick="openSavedCase('${c.id}')">
                <div class="saved-case-collection-header">
                    <strong>${escapeHtml(c.name)}</strong>
                    <span class="saved-case-date">${c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}</span>
                </div>
                <p class="saved-case-preview">${escapeHtml(c.input_preview || '—')}</p>
            </div>
        `).join('');

        if (window.lucide) lucide.createIcons();
    } catch (err) {
        container.innerHTML = '<p class="text-muted">Failed to load saved cases.</p>';
    }
}

let _currentSavedCaseId = null;

/**
 * Open a saved case detail view.
 * Backend enforces role-based filtering — doctors won't see layer outputs.
 */
async function openSavedCase(id) {
    _currentSavedCaseId = id;
    const detailSection = document.getElementById('saved-case-detail');
    const nameEl = document.getElementById('saved-case-detail-name');
    const bodyEl = document.getElementById('saved-case-detail-body');
    if (!detailSection || !bodyEl) return;

    detailSection.classList.remove('hidden');

    try {
        const res = await authFetch(`${API_BASE}/saved-cases/${id}`);
        const data = await safeJson(res);

        nameEl.textContent = data.name || '—';

        const snap = data.results_snapshot || {};
        const inputText = data.input_text || '—';

        // Build result sections
        let html = `
            <div class="saved-case-section">
                <h4>Input Text</h4>
                <pre class="saved-case-input-full">${escapeHtml(inputText)}</pre>
            </div>
            <div class="saved-case-section">
                <h4>Final Results</h4>
                <div class="saved-case-results-grid">
                    <span><strong>Verdict:</strong> ${escapeHtml(snap.final_verdict || '—')}</span>
                    <span><strong>CCI:</strong> ${snap.final_cci != null ? snap.final_cci : '—'}</span>
                    <span><strong>Status:</strong> ${escapeHtml(snap.status || '—')}</span>
                </div>
            </div>
        `;

        // Admin-only: show layer outputs (backend already strips for doctors)
        if (snap.layer1_output || snap.layer2_output || snap.layer3_output) {
            html += `
                <details class="saved-case-section">
                    <summary><i data-lucide="layers"></i> Layer Outputs</summary>
                    <pre class="saved-case-raw">${escapeHtml(JSON.stringify({
                        layer1: snap.layer1_output,
                        layer2: snap.layer2_output,
                        layer3: snap.layer3_output,
                        extra: snap.extra_layer_outputs
                    }, null, 2))}</pre>
                </details>
            `;
        }

        if (snap.layer1_raw_output || snap.layer2_raw_output || snap.layer3_raw_output) {
            html += `
                <details class="saved-case-section">
                    <summary><i data-lucide="terminal"></i> Raw LLM Output</summary>
                    <pre class="saved-case-raw">${escapeHtml(
                        (snap.layer1_raw_output || '') + '\n---\n' +
                        (snap.layer2_raw_output || '') + '\n---\n' +
                        (snap.layer3_raw_output || '')
                    )}</pre>
                </details>
            `;
        }

        bodyEl.innerHTML = html;
        if (window.lucide) lucide.createIcons();
    } catch (err) {
        bodyEl.innerHTML = `<p class="text-muted">Failed to load case: ${err.message}</p>`;
    }
}

function closeSavedCaseDetail() {
    _currentSavedCaseId = null;
    document.getElementById('saved-case-detail')?.classList.add('hidden');
}

/**
 * Rename a saved case with Enter → save, Escape → cancel.
 */
function renameSavedCaseUI() {
    if (!_currentSavedCaseId) return;
    const nameEl = document.getElementById('saved-case-detail-name');
    if (!nameEl) return;

    const currentName = nameEl.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentName;
    input.className = 'saved-case-rename-input';
    nameEl.replaceWith(input);
    input.focus();
    input.select();

    async function save() {
        const newName = input.value.trim();
        if (!newName || newName === currentName) {
            cancel();
            return;
        }
        try {
            await authFetch(`${API_BASE}/saved-cases/${_currentSavedCaseId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName }),
            });
            showToast('Renamed!', 'success');
        } catch (err) {
            showToast('Rename failed: ' + err.message, 'error');
        }
        cancel(newName || currentName);
    }

    function cancel(name) {
        const h3 = document.createElement('h3');
        h3.id = 'saved-case-detail-name';
        h3.textContent = name || currentName;
        input.replaceWith(h3);
    }

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); save(); }
        if (e.key === 'Escape') { cancel(); }
    });
    input.addEventListener('blur', () => save());
}

/**
 * Soft-delete a saved case. Never hard-deletes.
 */
async function deleteSavedCaseUI() {
    if (!_currentSavedCaseId) return;
    if (!confirm('Remove this saved case? It will be hidden but never permanently deleted.')) return;

    try {
        await authFetch(`${API_BASE}/saved-cases/${_currentSavedCaseId}`, { method: 'DELETE' });
        showToast('Saved case removed', 'success');
        closeSavedCaseDetail();
        loadSavedCases();
    } catch (err) {
        showToast('Delete failed: ' + err.message, 'error');
    }
}



// ============================================
// Global expose for onclick handlers in HTML
// ============================================

window.toggleOutputFullscreen = toggleOutputFullscreen;
window.fetchCaseOutput = fetchCaseOutput;
window.updateOutputCharCount = updateOutputCharCount;
window.switchPage = switchPage;
window.switchView = switchView;
window.switchPromptTab = switchPromptTab;
window.savePrompt = savePrompt;
window.resetPrompt = resetPrompt;
window.toggleResult = toggleResult;
window.switchResultTab = switchResultTab;
window.copyRawOutput = copyRawOutput;
window.copyToClipboard = copyToClipboard;
window.downloadJSON = downloadJSON;
window.updateTemperature = updateTemperature;
window.updateSeed = updateSeed;
window.clearSeed = clearSeed;
window.saveInferenceParams = saveInferenceParams;
// Execution Mode
window.markExecutionModeDirty = markExecutionModeDirty;
window.saveExecutionMode = saveExecutionMode;
window.clearFile = clearFile;
window.openModelModal = openModelModal;
window.closeModelModal = closeModelModal;
window.pullModel = pullModel;
window.pullCustomModel = pullCustomModel;
window.pullModelWithProgress = pullModelWithProgress;
window.setActiveModel = setActiveModel;
window.deleteModel = deleteModel;
window.resetToUpload = resetToUpload;
window.cancelJob = cancelJob;
window.resumeJob = resumeJob;
window.exportPrompts = exportPrompts;
window.importPrompts = importPrompts;
window.showCreateLayerModal = showCreateLayerModal;
window.createCustomLayer = createCustomLayer;
window.deleteCustomLayer = deleteCustomLayer;
window.setActiveVersion = setActiveVersion;
window.rollbackToVersion = rollbackToVersion;
window.loadVersionHistory = loadVersionHistory;
window.loadLayers = loadLayers;
window.reorderLayersDrag = reorderLayersDrag;
window.startRenameLayer = startRenameLayer;
window.renderExecutionTimeline = renderExecutionTimeline;
window.renderSnapshotViewer = renderSnapshotViewer;
// Replay, Metrics & Compare
window.replayJob = replayJob;
window._executeReplay = _executeReplay;
window.loadJobMetrics = loadJobMetrics;
window.loadAggregateMetrics = loadAggregateMetrics;
window.runComparison = runComparison;
window.populateCompareSelect = populateCompareSelect;
// Auth
window.handleLogin = handleLogin;
window.handleSignup = handleSignup;
window.handleLogout = handleLogout;
window.showLogin = showLogin;
window.showSignup = showSignup;
// Logs
window.refreshLogs = refreshLogs;
window.toggleLogAutoRefresh = toggleLogAutoRefresh;
// Admin
window.approveUser = approveUser;
window.rejectUser = rejectUser;
window.loadAdminUsers = loadAdminUsers;
window.toggleUserStatus = toggleUserStatus;
window.changeUserRole = changeUserRole;
// Operations Dashboard
window.loadOperationsDashboard = loadOperationsDashboard;
// Stage 6 — Regression, Audit, Config
window.runRegressionCheck = runRegressionCheck;
window.downloadAuditPackage = downloadAuditPackage;
// Case tagging
window.toggleSaveCase = toggleSaveCase;
window.exportSavedCases = exportSavedCases;
window.markBaseline = markBaseline;
// Stream Downloads
window.downloadLayerRaw = downloadLayerRaw;
window.downloadLayerJson = downloadLayerJson;
window.downloadLayerFull = downloadLayerFull;
window.downloadStreamExport = downloadStreamExport;
// UI State
window.renderCancelledJobState = renderCancelledJobState;
window.renderSavedCasesPanel = renderSavedCasesPanel;
// Saved Cases Collection
window.loadSavedCases = loadSavedCases;
window.openSavedCase = openSavedCase;
window.closeSavedCaseDetail = closeSavedCaseDetail;
window.renameSavedCaseUI = renameSavedCaseUI;
window.deleteSavedCaseUI = deleteSavedCaseUI;
window.saveCaseToCollection = saveCaseToCollection;

// ============================================
// OpenRouter Integration
// ============================================

function initOpenRouterToggle() {
    const toggle = document.getElementById('openrouter-checkbox');
    const modal = document.getElementById('openrouter-modal');
    const cancelBtn = document.getElementById('openrouter-cancel-btn');
    const saveBtn = document.getElementById('openrouter-save-btn');
    const keyInput = document.getElementById('openrouter-key-input');
    
    toggle.addEventListener('change', (e) => {
        if (e.target.checked) {
            // Show modal if toggled on
            modal.classList.add('active');
            keyInput.focus();
        } else {
            // Disable OpenRouter
            state.openRouterEnabled = false;
            state.openRouterKey = null;
            updateOpenRouterStatus('idle');
            showToast('OpenRouter disabled. Reverting to vLLM.', 'info');
        }
    });

    cancelBtn.addEventListener('click', () => {
        modal.classList.remove('active');
        toggle.checked = false;
        state.openRouterEnabled = false;
        keyInput.value = '';
    });

    saveBtn.addEventListener('click', async () => {
        const key = keyInput.value.trim();
        if (!key) {
            showToast('API Key cannot be empty', 'warning');
            return;
        }
        if (!key.startsWith('sk-')) {
            showToast('API Key must start with "sk-"', 'error');
            return;
        }

        saveBtn.disabled = true;
        saveBtn.innerHTML = 'Testing...';
        updateOpenRouterStatus('connecting');

        const success = await testOpenRouterConnection(key);

        saveBtn.disabled = false;
        saveBtn.innerHTML = 'Connect & Test';

        if (success) {
            state.openRouterEnabled = true;
            state.openRouterKey = key;
            modal.classList.remove('active');
            keyInput.value = '';
            showToast('Connected to OpenRouter!', 'success');
            updateOpenRouterStatus('connected');
        } else {
            updateOpenRouterStatus('error');
            // Keep modal open to allow retry
        }
    });
}

function updateOpenRouterStatus(status) {
    const dot = document.getElementById('openrouter-status-dot');
    if (!dot) return;
    
    // Remove all status classes
    dot.classList.remove('status-idle', 'status-connecting', 'status-connected', 'status-error');
    
    // Add current status
    dot.classList.add(`status-${status}`);
    dot.title = status.charAt(0).toUpperCase() + status.slice(1);
}

async function testOpenRouterConnection(apiKey) {
    try {
        const response = await fetch('https://openrouter.ai/api/v1/chat/completions', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${apiKey}`,
                'Content-Type': 'application/json',
                'HTTP-Referer': window.location.origin,
                'X-Title': 'SNAP-AI'
            },
            body: JSON.stringify({
                model: 'openai/gpt-oss-120b',
                messages: [
                    { role: 'user', content: 'Hello' }
                ]
            })
        });

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            showToast(`Invalid API Key or connection failed (${response.status})`, 'error');
            return false;
        }

        return true;
    } catch (err) {
        showToast(`Connection failed: ${err.message}`, 'error');
        return false;
    }
}
