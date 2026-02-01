/**
 * SNAP-AI Frontend Application
 * 
 * Handles file upload, progress tracking, and results display.
 */

// ============================================
// Configuration
// ============================================

const API_BASE = '/api/v1';
const POLL_INTERVAL_MS = 2000;

// ============================================
// State
// ============================================

const state = {
    selectedFile: null,
    textContent: '',
    currentJobId: null,
    pollInterval: null,
    isProcessing: false,
};

// ============================================
// DOM Elements
// ============================================

const elements = {
    // Tabs
    tabBtns: document.querySelectorAll('.tab-btn'),
    fileTab: document.getElementById('file-tab'),
    textTab: document.getElementById('text-tab'),

    // File Upload
    dropzone: document.getElementById('dropzone'),
    fileInput: document.getElementById('file-input'),
    fileInfo: document.getElementById('file-info'),

    // Text Input
    textInput: document.getElementById('text-input'),

    // Buttons
    processBtn: document.getElementById('process-btn'),
    exportBtn: document.getElementById('export-btn'),
    newAnalysisBtn: document.getElementById('new-analysis-btn'),

    // Sections
    uploadSection: document.getElementById('upload-section'),
    progressSection: document.getElementById('progress-section'),
    resultsSection: document.getElementById('results-section'),

    // Progress
    progressFill: document.getElementById('progress-fill'),
    progressText: document.getElementById('progress-text'),
    caseStatusList: document.getElementById('case-status-list'),

    // Results
    resultsSummary: document.getElementById('results-summary'),
    caseResults: document.getElementById('case-results'),
};

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info') {
    // Remove existing toast
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
    <span class="toast-icon">${type === 'error' ? '❌' : type === 'success' ? '✅' : 'ℹ️'}</span>
    <span class="toast-message">${message}</span>
    <button class="toast-close" onclick="this.parentElement.remove()">×</button>
  `;
    document.body.appendChild(toast);

    // Auto remove after 5 seconds
    setTimeout(() => toast.remove(), 5000);
}

// ============================================
// Tab Switching
// ============================================

elements.tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
        if (state.isProcessing) return; // Prevent tab switch during processing

        const tab = btn.dataset.tab;

        // Update active tab button
        elements.tabBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        // Show appropriate content
        elements.fileTab.classList.toggle('active', tab === 'file');
        elements.textTab.classList.toggle('active', tab === 'text');

        // Update process button state
        updateProcessButton();
    });
});

// ============================================
// File Upload
// ============================================

elements.dropzone.addEventListener('click', () => {
    if (!state.isProcessing) {
        elements.fileInput.click();
    }
});

elements.dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    if (!state.isProcessing) {
        elements.dropzone.classList.add('dragover');
    }
});

elements.dropzone.addEventListener('dragleave', () => {
    elements.dropzone.classList.remove('dragover');
});

elements.dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    elements.dropzone.classList.remove('dragover');

    if (state.isProcessing) return;

    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.docx')) {
        handleFileSelect(file);
    } else {
        showToast('Please upload a .docx file', 'error');
    }
});

elements.fileInput.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (file) {
        handleFileSelect(file);
    }
});

function handleFileSelect(file) {
    state.selectedFile = file;
    elements.fileInfo.classList.remove('hidden');
    elements.fileInfo.innerHTML = `
    <span>📄 ${file.name} (${formatFileSize(file.size)})</span>
    <button class="btn btn-outline" onclick="clearFile()">✕ Remove</button>
  `;
    updateProcessButton();
}

function clearFile() {
    state.selectedFile = null;
    elements.fileInput.value = '';
    elements.fileInfo.classList.add('hidden');
    updateProcessButton();
}

// ============================================
// Text Input
// ============================================

elements.textInput.addEventListener('input', () => {
    state.textContent = elements.textInput.value;
    updateProcessButton();
});

// ============================================
// Process Button
// ============================================

function updateProcessButton() {
    const activeTab = document.querySelector('.tab-btn.active').dataset.tab;

    if (state.isProcessing) {
        elements.processBtn.disabled = true;
        elements.processBtn.innerHTML = '<span class="btn-icon">⏳</span> Processing...';
        return;
    }

    elements.processBtn.innerHTML = '<span class="btn-icon">⚡</span> Process Cases';

    if (activeTab === 'file') {
        elements.processBtn.disabled = !state.selectedFile;
    } else {
        elements.processBtn.disabled = !state.textContent.trim();
    }
}

elements.processBtn.addEventListener('click', async () => {
    if (state.isProcessing) return;

    const activeTab = document.querySelector('.tab-btn.active').dataset.tab;

    if (activeTab === 'file' && state.selectedFile) {
        await processFile(state.selectedFile);
    } else if (activeTab === 'text' && state.textContent.trim()) {
        await processText(state.textContent);
    }
});

// ============================================
// API Functions
// ============================================

async function processFile(file) {
    state.isProcessing = true;
    updateProcessButton();
    showProgress();

    const formData = new FormData();
    formData.append('file', file);

    try {
        const response = await fetch(`${API_BASE}/upload`, {
            method: 'POST',
            body: formData,
        });

        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.error?.message || 'Upload failed');
        }

        state.currentJobId = result.data.job_id;
        showToast(`Processing ${result.data.case_count} case(s)...`, 'info');
        initializeProgressUI(result.data.case_count);
        startPolling(result.data.job_id);

    } catch (error) {
        showError(error.message);
    }
}

async function processText(text) {
    state.isProcessing = true;
    updateProcessButton();
    showProgress();

    const formData = new FormData();
    formData.append('text', text);

    try {
        const response = await fetch(`${API_BASE}/upload`, {
            method: 'POST',
            body: formData,
        });

        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.error?.message || 'Processing failed');
        }

        state.currentJobId = result.data.job_id;
        showToast(`Processing ${result.data.case_count} case(s)...`, 'info');
        initializeProgressUI(result.data.case_count);
        startPolling(result.data.job_id);

    } catch (error) {
        showError(error.message);
    }
}

function startPolling(jobId) {
    // Initial poll immediately
    pollJobStatus(jobId);

    state.pollInterval = setInterval(() => {
        pollJobStatus(jobId);
    }, POLL_INTERVAL_MS);
}

async function pollJobStatus(jobId) {
    try {
        const response = await fetch(`${API_BASE}/jobs/${jobId}`);
        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.error?.message || 'Failed to get job status');
        }

        updateProgressUI(result.data);

        if (result.data.status === 'completed' || result.data.status === 'failed') {
            clearInterval(state.pollInterval);
            state.pollInterval = null;

            if (result.data.status === 'completed') {
                showToast('Processing complete!', 'success');
                await loadResults(jobId);
            } else {
                showError('Processing failed. Some cases may have errors.');
                // Still try to load partial results
                await loadResults(jobId);
            }
        }
    } catch (error) {
        console.error('Polling error:', error);
        // Don't stop polling on transient errors
    }
}

async function loadResults(jobId) {
    try {
        const response = await fetch(`${API_BASE}/jobs/${jobId}/results`);
        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.error?.message || 'Failed to load results');
        }

        renderResults(result.data);
    } catch (error) {
        showError(error.message);
    }
}

// ============================================
// UI Functions
// ============================================

function showProgress() {
    elements.uploadSection.classList.add('hidden');
    elements.progressSection.classList.remove('hidden');
    elements.resultsSection.classList.add('hidden');
}

function initializeProgressUI(caseCount) {
    elements.progressFill.style.width = '0%';
    elements.progressText.textContent = `0 of ${caseCount} cases completed`;

    // Initialize case status list
    const cases = [];
    for (let i = 0; i < caseCount; i++) {
        cases.push({ case_number: i + 1, status: 'queued' });
    }
    renderCaseStatusList(cases);
}

function renderCaseStatusList(cases) {
    elements.caseStatusList.innerHTML = cases.map(c => {
        const statusLabel = c.status.charAt(0).toUpperCase() + c.status.slice(1);
        const label = c.case_label || `Case #${c.case_number}`;
        return `
      <div class="case-status-item">
        <span>${label}</span>
        <span class="status-badge status-${c.status}">${statusLabel}</span>
      </div>
    `;
    }).join('');
}

function updateProgressUI(data) {
    const completed = data.completed_count || 0;
    const failed = data.failed_count || 0;
    const total = data.case_count || 1;
    const done = completed + failed;
    const percent = Math.round((done / total) * 100);

    elements.progressFill.style.width = `${percent}%`;
    elements.progressText.textContent = `${done} of ${total} cases completed`;

    // Update per-case status
    if (data.cases && data.cases.length > 0) {
        renderCaseStatusList(data.cases);
    }
}

function renderResults(data) {
    state.isProcessing = false;
    updateProcessButton();

    elements.progressSection.classList.add('hidden');
    elements.resultsSection.classList.remove('hidden');

    // Summary
    const results = data.results || [];
    const totalCases = results.length;

    // Count verdicts
    let passCount = 0;
    results.forEach(r => {
        const verdict = r.final_verdict || r.layer3_output?.verdict;
        if (verdict === 'PASS' || verdict === 'PASS_WITH_WARNINGS') {
            passCount++;
        }
    });

    // Calculate average CCI
    let totalCCI = 0;
    let cciCount = 0;
    results.forEach(r => {
        const cci = r.final_cci ?? r.layer2_output?.cci_total;
        if (cci !== null && cci !== undefined) {
            totalCCI += cci;
            cciCount++;
        }
    });
    const avgCCI = cciCount > 0 ? totalCCI / cciCount : 0;

    elements.resultsSummary.innerHTML = `
    <div class="summary-card">
      <div class="summary-value">${totalCases}</div>
      <div class="summary-label">Total Cases</div>
    </div>
    <div class="summary-card">
      <div class="summary-value">${passCount}</div>
      <div class="summary-label">Verified</div>
    </div>
    <div class="summary-card">
      <div class="summary-value">${avgCCI.toFixed(1)}</div>
      <div class="summary-label">Avg CCI</div>
    </div>
  `;

    // Case results
    elements.caseResults.innerHTML = results.map((r, i) => {
        const verdict = r.final_verdict || r.layer3_output?.verdict || 'UNKNOWN';
        const verdictClass = verdict.includes('PASS')
            ? (verdict === 'PASS' ? 'verdict-pass' : 'verdict-warnings')
            : 'verdict-fail';
        const cci = r.final_cci ?? r.layer2_output?.cci_total ?? 0;
        const complications = r.layer2_output?.complications || [];
        const caseLabel = r.case_label || `Case #${r.case_number || i + 1}`;
        const hasError = r.status === 'failed' || r.error_message;

        return `
      <div class="case-result-card ${hasError ? 'has-error' : ''}">
        <div class="case-result-header" onclick="toggleResult(${i})">
          <div>
            <strong>${caseLabel}</strong>
            <span class="cci-display" style="margin-left: 16px;">CCI: ${cci.toFixed(1)}</span>
          </div>
          <div class="case-result-badges">
            ${hasError ? '<span class="verdict-badge verdict-fail">ERROR</span>' : ''}
            <span class="verdict-badge ${verdictClass}">${verdict}</span>
          </div>
        </div>
        <div class="case-result-body" id="result-body-${i}">
          ${hasError ? `
            <div class="error-message">
              <strong>Error:</strong> ${r.error_message || 'Unknown error'}
            </div>
          ` : ''}
          
          <h4>Complications (${complications.length})</h4>
          ${complications.length > 0 ? `
            <table class="complications-table">
              <thead>
                <tr>
                  <th>Complication</th>
                  <th>CD Grade</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                ${complications.map(c => `
                  <tr>
                    <td>${c.complication}</td>
                    <td><span class="cd-grade">CD ${c.cd_grade}</span></td>
                    <td>${c.evidence_quote || c.evidence || '-'}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          ` : '<p class="no-complications">No complications identified</p>'}
          
          <details class="json-details">
            <summary>View Raw JSON Output</summary>
            <pre class="json-tree">${syntaxHighlightJSON(r)}</pre>
          </details>
        </div>
      </div>
    `;
    }).join('');

    // Store results for export
    window.currentResults = data;
}

function syntaxHighlightJSON(obj) {
    const json = JSON.stringify(obj, null, 2);
    return json
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
        .replace(/: "([^"]*)"/g, ': <span class="json-string">"$1"</span>')
        .replace(/: (\d+\.?\d*)/g, ': <span class="json-number">$1</span>')
        .replace(/: (true|false)/g, ': <span class="json-boolean">$1</span>')
        .replace(/: null/g, ': <span class="json-null">null</span>');
}

function toggleResult(index) {
    const body = document.getElementById(`result-body-${index}`);
    body.classList.toggle('expanded');
}

function showError(message) {
    if (state.pollInterval) {
        clearInterval(state.pollInterval);
        state.pollInterval = null;
    }
    state.isProcessing = false;
    updateProcessButton();
    showToast(message, 'error');
}

function resetToUpload() {
    elements.uploadSection.classList.remove('hidden');
    elements.progressSection.classList.add('hidden');
    elements.resultsSection.classList.add('hidden');
    state.currentJobId = null;
    state.isProcessing = false;
    updateProcessButton();
}

// ============================================
// Export & New Analysis
// ============================================

elements.exportBtn.addEventListener('click', () => {
    if (window.currentResults) {
        const blob = new Blob([JSON.stringify(window.currentResults, null, 2)], {
            type: 'application/json',
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snap-ai-results-${new Date().toISOString().slice(0, 10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
        showToast('Results exported successfully', 'success');
    }
});

elements.newAnalysisBtn.addEventListener('click', () => {
    clearFile();
    elements.textInput.value = '';
    state.textContent = '';
    window.currentResults = null;
    resetToUpload();
});

// ============================================
// Utilities
// ============================================

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Make functions available globally
window.toggleResult = toggleResult;
window.clearFile = clearFile;

// ============================================
// Initialize
// ============================================

updateProcessButton();
console.log('SNAP-AI Frontend initialized');
