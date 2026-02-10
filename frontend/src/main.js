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

// Layer metadata
const LAYERS = {
    layer1_ctp: { label: 'Layer 1 — Clinical Text Pre-Processor (CTP)', shortLabel: 'Layer 1: CTP', number: 1 },
    layer2_cie: { label: 'Layer 2 — Complication Info Extraction (CIE)', shortLabel: 'Layer 2: CIE', number: 2 },
    layer3_ccc: { label: 'Layer 3 — CCI Calculation & Cross-Check (CCC)', shortLabel: 'Layer 3: CCC', number: 3 },
};

// ============================================
// State
// ============================================

const state = {
    selectedFile: null,
    textContent: '',
    currentJobId: null,
    pollInterval: null,
    isProcessing: false,
    eventSource: null,
    currentView: 'table',

    // Streaming state
    liveOutputBuffers: {
        layer1_ctp: '',
        layer2_cie: '',
        layer3_ccc: '',
    },
    activeLiveTab: 'layer1_ctp',
    currentStreamingLayer: null,

    // Prompt state
    activePromptLayer: 'layer1_ctp',
    promptData: {},       // { layer: { content, source, version } }
    promptDirty: false,

    // Model state
    activeModel: null,
    availableModels: [],

    // System info
    systemInfo: null,
};

// ============================================
// DOM Ready
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initDropzone();
    initTextInput();
    initProcessButton();
    initExportActions();
    initPromptEditor();
    initLiveOutputTabs();
    loadModels();
    loadSystemInfo();
    loadAllPrompts();

    console.log('SNAP-AI Frontend v2 initialized');
});

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const icons = { info: 'ℹ️', success: '✅', error: '❌', warning: '⚠️' };

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <span class="toast-message">${message}</span>
    <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
  `;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

// ============================================
// Page Navigation
// ============================================

function switchPage(page) {
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
        const file = e.dataTransfer.files[0];
        if (file && file.name.endsWith('.docx')) {
            handleFileSelect(file);
        } else {
            showToast('Please upload a .docx file', 'error');
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files[0]) handleFileSelect(e.target.files[0]);
    });
}

function handleFileSelect(file) {
    state.selectedFile = file;
    const info = document.getElementById('file-info');
    info.classList.remove('hidden');
    info.innerHTML = `
    <span>📄 ${file.name} (${formatFileSize(file.size)})</span>
    <button class="btn btn-sm btn-outline" onclick="clearFile()">Remove</button>
  `;
    updateProcessButton();
}

function clearFile() {
    state.selectedFile = null;
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
        if (activeTab === 'file' && state.selectedFile) {
            await processFile(state.selectedFile);
        } else if (activeTab === 'text' && state.textContent.trim()) {
            await processText(state.textContent);
        }
    });
}

function updateProcessButton() {
    const btn = document.getElementById('process-btn');
    const activeTab = document.querySelector('.tab-btn.active').dataset.tab;
    const hasFile = activeTab === 'file' && state.selectedFile;
    const hasText = activeTab === 'text' && state.textContent.trim();
    btn.disabled = state.isProcessing || (!hasFile && !hasText);
}

// ============================================
// API: Process File / Text
// ============================================

async function processFile(file) {
    const formData = new FormData();
    formData.append('file', file);

    try {
        state.isProcessing = true;
        updateProcessButton();
        showProgress();

        const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: formData });
        if (!res.ok) throw new Error(`Upload failed: ${res.status}`);

        const response = await res.json();
        const data = response.data || response;
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

async function processText(text) {
    try {
        state.isProcessing = true;
        updateProcessButton();
        showProgress();

        const res = await fetch(`${API_BASE}/upload/text`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        if (!res.ok) throw new Error(`Processing failed: ${res.status}`);

        const response = await res.json();
        const data = response.data || response;
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
// SSE Streaming (Typewriter Effect)
// ============================================

function startStreaming(jobId) {
    closeStream();
    resetLiveOutput();

    const url = `${API_BASE}/stream/${jobId}`;
    state.eventSource = new EventSource(url);

    const statusEl = document.getElementById('live-output-status');
    statusEl.classList.add('streaming');
    statusEl.innerHTML = '<span class="live-dot"></span> Streaming...';

    state.eventSource.addEventListener('token', (e) => {
        try {
            const data = JSON.parse(e.data);
            appendToken(data.layer, data.token);
        } catch { /* ignore parse errors */ }
    });

    state.eventSource.addEventListener('layer_start', (e) => {
        try {
            const data = JSON.parse(e.data);
            const layerKey = data.layer;
            setActiveLayer(layerKey);
            updateCurrentCaseInfo(data.case_number, null, layerKey);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('layer_complete', (e) => {
        try {
            const data = JSON.parse(e.data);
            markLayerComplete(data.layer);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('case_complete', (e) => {
        try {
            const data = JSON.parse(e.data);
            resetLiveOutput();
            updateCaseProgress(data);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('progress', (e) => {
        try {
            const data = JSON.parse(e.data);
            updateProgressUI(data);
        } catch { /* ignore */ }
    });

    state.eventSource.addEventListener('complete', (e) => {
        closeStream();
        statusEl.classList.remove('streaming');
        statusEl.innerHTML = '<span class="live-dot"></span> Complete';

        try {
            const data = JSON.parse(e.data);
            loadResults(data.job_id || jobId);
        } catch {
            loadResults(jobId);
        }
        showToast('Processing complete!', 'success');
    });

    state.eventSource.addEventListener('error', () => {
        // SSE can auto-reconnect; only treat as fatal if readyState is CLOSED
        if (state.eventSource && state.eventSource.readyState === EventSource.CLOSED) {
            closeStream();
            statusEl.classList.remove('streaming');
            statusEl.innerHTML = '<span class="live-dot"></span> Disconnected';
            // Fall back to polling
            startLegacyPolling(jobId);
        }
    });

    // Also poll as fallback (less frequently)
    state.pollInterval = setInterval(() => pollJobStatus(jobId), POLL_INTERVAL_MS * 2);
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

// ============================================
// Live Output Panel (Typewriter)
// ============================================

function initLiveOutputTabs() {
    document.querySelectorAll('.live-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            state.activeLiveTab = tab.dataset.layer;
            document.querySelectorAll('.live-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            renderLiveOutputBuffer();
        });
    });
}

function appendToken(layer, token) {
    if (!layer || !token) return;

    // Map stream layer names to our keys
    const layerKey = normalizeLayerKey(layer);
    if (!layerKey || !state.liveOutputBuffers.hasOwnProperty(layerKey)) return;

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
    state.liveOutputBuffers = { layer1_ctp: '', layer2_cie: '', layer3_ccc: '' };
    state.currentStreamingLayer = null;
    renderLiveOutputBuffer();
}

function normalizeLayerKey(layer) {
    if (!layer) return null;
    const l = layer.toLowerCase();
    if (l.includes('layer1') || l.includes('ctp') || l.includes('layer_1')) return 'layer1_ctp';
    if (l.includes('layer2') || l.includes('cie') || l.includes('layer_2')) return 'layer2_cie';
    if (l.includes('layer3') || l.includes('ccc') || l.includes('layer_3')) return 'layer3_ccc';
    return null;
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
    resetLayerSteps();
}

function resetLayerSteps() {
    [1, 2, 3].forEach(n => {
        const el = document.getElementById(`layer-step-${n}`);
        if (el) el.className = 'layer-step';
    });
}

function setActiveLayer(layerKey) {
    const num = LAYERS[layerKey]?.number;
    if (!num) return;

    [1, 2, 3].forEach(n => {
        const el = document.getElementById(`layer-step-${n}`);
        if (!el) return;
        if (n < num) el.className = 'layer-step completed';
        else if (n === num) el.className = 'layer-step active';
        else el.className = 'layer-step';
    });
}

function markLayerComplete(layer) {
    const layerKey = normalizeLayerKey(layer);
    const num = LAYERS[layerKey]?.number;
    if (!num) return;
    const el = document.getElementById(`layer-step-${num}`);
    if (el) el.className = 'layer-step completed';
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
        const res = await fetch(`${API_BASE}/jobs/${jobId}`);
        if (!res.ok) return;
        const response = await res.json();
        const data = response.data || response;

        if (data.status === 'completed') {
            closeStream();
            loadResults(jobId);
            showToast('Processing complete!', 'success');
        } else if (data.status === 'failed') {
            closeStream();
            const errMsg = data.error || data.message || 'Processing failed';
            showProcessingError(errMsg, data);
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
        const res = await fetch(`${API_BASE}/jobs/${jobId}/results`);
        if (!res.ok) throw new Error('Failed to load results');
        const json = await res.json();
        const data = json.data || json;
        window.currentResults = data;
        renderResults(data);

        document.getElementById('progress-section').classList.add('hidden');
        document.getElementById('results-section').classList.remove('hidden');
        state.isProcessing = false;
        updateProcessButton();
    } catch (err) {
        showError(err.message);
    }
}

function renderResults(data) {
    const results = data.results || data.cases || [];

    // Summary stats
    const total = results.length;
    const completed = results.filter(r => r.status === 'completed' || r.layer3).length;
    const avgCci = results.reduce((acc, r) => {
        const cci = r.layer3?.cci_score ?? r.cci_score ?? 0;
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
}

function renderResultsTable(results) {
    const container = document.getElementById('results-table-container');
    if (!results.length) {
        container.innerHTML = '<p>No results available.</p>';
        return;
    }

    const rows = results.map((r, i) => {
        const cci = r.layer3?.cci_score ?? r.cci_score ?? '—';
        const verdict = r.layer3?.verdict ?? r.verdict ?? '—';
        const complications = r.layer2?.complications ?? r.complications ?? [];
        const grades = complications.map(c => c.cd_grade || '?').filter(Boolean);

        return `<tr class="${r.error ? 'row-error' : ''}">
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
    container.innerHTML = results.map((r, i) => {
        const caseNum = r.case_number ?? i + 1;
        const verdict = r.layer3?.verdict ?? r.verdict ?? '—';
        const cci = r.layer3?.cci_score ?? r.cci_score ?? '—';

        return `
      <div class="case-result-card ${r.error ? 'has-error' : ''}">
        <div class="case-result-header" onclick="toggleResult(${i})">
          <span><strong>Case #${caseNum}</strong></span>
          <div class="case-result-badges">
            <span class="cci-display">${cci}</span>
            <span class="verdict-badge verdict-${(verdict + '').toLowerCase().replace(/\s/g, '')}">${verdict}</span>
          </div>
        </div>
        <div class="case-result-body" id="case-result-body-${i}">
          ${r.error ? `<div class="error-message">${r.error}</div>` : ''}
          <details class="json-details">
            <summary>View Full JSON Output</summary>
            <pre class="json-tree">${syntaxHighlightJSON(r)}</pre>
          </details>
        </div>
      </div>
    `;
    }).join('');
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
        const res = await fetch(`${API_BASE}/models`);
        if (!res.ok) throw new Error('Failed to load models');
        const json = await res.json();
        const data = json.data || json;

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
                showToast(`Pulling ${model}... This may take a while.`, 'info');
                try {
                    const res = await fetch(`${API_BASE}/models/pull`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ model }),
                    });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        throw new Error(extractErrorMessage(err));
                    }
                    showToast(`${model} installed! Setting as active...`, 'success');
                    await setActiveModel(model);
                    await loadModels();
                } catch (err) {
                    showToast(`Failed to pull ${model}: ${err.message}`, 'error');
                    await loadModels(); // Reset dropdown
                }
            } else {
                await loadModels(); // Reset dropdown
            }
            return;
        }

        // Set active model
        try {
            const res = await fetch(`${API_BASE}/models/active`, {
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
    const statusEl = document.getElementById('model-pull-status');
    const modelName = selectEl?.value?.trim();

    if (!modelName) {
        showToast('Select a model to pull', 'warning');
        return;
    }

    statusEl.className = 'model-pull-status loading';
    statusEl.innerHTML = `
        <span class="spinner-small"></span>
        Pulling ${modelName}... This may take several minutes for large models.
    `;
    statusEl.classList.remove('hidden');

    try {
        const res = await fetch(`${API_BASE}/models/pull`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: modelName }),
        });

        if (res.ok) {
            statusEl.className = 'model-pull-status success';
            statusEl.textContent = `✅ ${modelName} pulled successfully! You can now select it as active.`;
            await loadModels();
            renderModelPullDropdown();
        } else {
            const err = await res.json().catch(() => ({}));
            throw new Error(extractErrorMessage(err));
        }
    } catch (err) {
        statusEl.className = 'model-pull-status error';
        statusEl.innerHTML = `
            <strong>❌ Failed to pull ${modelName}</strong><br>
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
    const statusEl = document.getElementById('model-pull-status');

    if (!modelName) {
        showToast('Enter a model name', 'warning');
        return;
    }

    statusEl.className = 'model-pull-status loading';
    statusEl.innerHTML = `<span class="spinner-small"></span> Pulling ${modelName}...`;
    statusEl.classList.remove('hidden');

    try {
        const res = await fetch(`${API_BASE}/models/pull`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: modelName }),
        });

        if (res.ok) {
            statusEl.className = 'model-pull-status success';
            statusEl.textContent = `✅ ${modelName} pulled successfully!`;
            input.value = '';
            await loadModels();
            renderModelPullDropdown();
        } else {
            const err = await res.json().catch(() => ({}));
            throw new Error(extractErrorMessage(err));
        }
    } catch (err) {
        statusEl.className = 'model-pull-status error';
        statusEl.textContent = `❌ ${err.message}`;
    }
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
        const res = await fetch(`${API_BASE}/models/active`, {
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
        const res = await fetch(`${API_BASE}/models`, {
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

    const layerName = document.getElementById('prompt-layer-name');
    layerName.textContent = LAYERS[layer]?.label || layer;

    // Use cached prompt data if available
    if (state.promptData[layer] && state.promptData[layer].content) {
        displayPromptData(layer, state.promptData[layer]);
    } else {
        loadPrompt(layer);
    }
}

async function loadAllPrompts() {
    // Pre-load all prompts at once using the list endpoint
    const editor = document.getElementById('prompt-editor');
    editor.value = 'Loading prompt templates...';
    editor.disabled = true;

    try {
        const res = await fetch(`${API_BASE}/prompts`);
        if (!res.ok) throw new Error('Failed to load prompts');
        const json = await res.json();
        const data = json.data || json;
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
            };
        });

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

    // Update badge
    const isCustom = promptInfo.source === 'custom';
    badge.textContent = isCustom ? `Custom v${promptInfo.version || 1}` : `Default v${promptInfo.version || '1.3'}`;
    badge.className = `prompt-source-badge ${isCustom ? 'custom' : 'default'}`;

    updateCharCount();
}

async function loadPrompt(layer) {
    const editor = document.getElementById('prompt-editor');

    editor.value = 'Loading prompt template...';
    editor.disabled = true;

    try {
        const res = await fetch(`${API_BASE}/prompts/${layer}`);
        if (!res.ok) throw new Error('Failed to load prompt');
        const json = await res.json();
        const data = json.data || json;

        // Extract prompt content correctly from response
        const prompt = data.prompt || {};
        const promptInfo = {
            content: prompt.content || '',
            source: data.source || 'default',
            version: prompt.version || '1.3',
        };

        state.promptData[layer] = promptInfo;
        displayPromptData(layer, promptInfo);
    } catch {
        editor.value = 'Failed to load prompt template.\n\nMake sure the backend is running and accessible.';
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
        const res = await fetch(`${API_BASE}/prompts/${layer}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });

        if (res.ok) {
            const json = await res.json();
            const data = json.data || json;
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
    if (!confirm(`Reset ${LAYERS[layer]?.shortLabel || layer} to the default prompt? Your custom version will be removed.`)) return;

    try {
        const res = await fetch(`${API_BASE}/prompts/${layer}/reset`, { method: 'POST' });
        if (res.ok) {
            // Clear cache so it reloads from file
            delete state.promptData[layer];
            showToast('Prompt reset to default', 'info');
            await loadPrompt(layer);
        }
    } catch {
        showToast('Failed to reset prompt', 'error');
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
        const res = await fetch(`${API_BASE}/system/info`);
        if (!res.ok) throw new Error('Failed to load system info');
        
        const json = await res.json();
        const data = json.data || json;

        const backendLabel = (data.llm_backend || 'ollama').toUpperCase();
        const activeModel = data.active_model || 'Not set';
        const workerCount = data.worker_count || 1;
        const isConnected = data.llm_connected;
        const statusClass = isConnected ? 'sys-info-connected' : 'sys-info-disconnected';
        const statusText = isConnected ? 'Connected' : 'Disconnected';

        bar.innerHTML = `
            <div class="sys-info-item">
                <span class="sys-info-label">Backend:</span>
                <span class="sys-info-value sys-info-backend">${backendLabel}</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Model:</span>
                <span class="sys-info-value" id="sys-info-model">${activeModel}</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Workers:</span>
                <span class="sys-info-value">${workerCount}</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Status:</span>
                <span class="sys-info-value ${statusClass}">${statusText}</span>
            </div>
            ${data.llm_error ? `
            <div class="sys-info-item sys-info-error">
                <span class="sys-info-label">Error:</span>
                <span class="sys-info-value">${escapeHtml(data.llm_error).substring(0, 50)}...</span>
            </div>
            ` : ''}
        `;
        bar.classList.remove('hidden');

        // Store system info in state for later use
        state.systemInfo = data;
    } catch (err) {
        console.error('System info load failed:', err);
        bar.innerHTML = `
            <div class="sys-info-item">
                <span class="sys-info-label">Status:</span>
                <span class="sys-info-value sys-info-disconnected">Backend Unavailable</span>
            </div>
            <div class="sys-info-item">
                <span class="sys-info-label">Action:</span>
                <span class="sys-info-value">Check that backend services are running</span>
            </div>
        `;
        bar.classList.remove('hidden');
    }
}

function updateSystemInfoModel(model) {
    const el = document.getElementById('sys-info-model');
    if (el) el.textContent = model;
}

// ============================================
// Utilities
// ============================================

function extractErrorMessage(err) {
    if (!err) return 'Unknown error';
    if (typeof err === 'string') return err;
    if (typeof err.detail === 'string') return err.detail;
    // FastAPI 422 errors: detail is an array of validation errors
    if (Array.isArray(err.detail)) {
        return err.detail.map(d => `${d.loc?.join('.')}: ${d.msg}`).join('; ');
    }
    if (err.message) return err.message;
    return JSON.stringify(err);
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================
// Global expose for onclick handlers in HTML
// ============================================

window.switchPage = switchPage;
window.switchView = switchView;
window.switchPromptTab = switchPromptTab;
window.savePrompt = savePrompt;
window.resetPrompt = resetPrompt;
window.toggleResult = toggleResult;
window.clearFile = clearFile;
window.openModelModal = openModelModal;
window.closeModelModal = closeModelModal;
window.pullModel = pullModel;
window.pullCustomModel = pullCustomModel;
window.setActiveModel = setActiveModel;
window.deleteModel = deleteModel;
window.resetToUpload = resetToUpload;
