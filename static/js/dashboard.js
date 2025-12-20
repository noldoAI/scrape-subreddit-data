// Dashboard JS - Version 1.0.0

const presets = {
    high: {
        posts_limit: 150,
        interval: 60,
        comment_batch: 12,
        sorting_methods: ['new', 'top', 'rising']
    },
    medium: {
        posts_limit: 100,
        interval: 60,
        comment_batch: 12,
        sorting_methods: ['new', 'top', 'rising']
    },
    low: {
        posts_limit: 80,
        interval: 60,
        comment_batch: 10,
        sorting_methods: ['new', 'top', 'rising']
    }
};

// Loading state management
function showButtonLoading(buttonId, text = 'Loading...') {
    const button = document.getElementById(buttonId) || document.querySelector(`[onclick*="${buttonId}"]`);
    if (button) {
        button.disabled = true;
        button.classList.add('loading');
        button.dataset.originalText = button.innerHTML;
        button.innerHTML = `<span class="spinner"></span>${text}`;
    }
}

function hideButtonLoading(buttonId) {
    const button = document.getElementById(buttonId) || document.querySelector(`[onclick*="${buttonId}"]`);
    if (button && button.dataset.originalText) {
        button.disabled = false;
        button.classList.remove('loading');
        button.innerHTML = button.dataset.originalText;
        delete button.dataset.originalText;
    }
}

function showGlobalLoading(text = 'Processing...') {
    document.getElementById('loadingText').textContent = text;
    document.getElementById('loadingOverlay').style.display = 'block';
}

function hideGlobalLoading() {
    document.getElementById('loadingOverlay').style.display = 'none';
}

// Button click handlers with loading states
function setButtonLoading(button, isLoading, loadingText = 'Loading...') {
    if (isLoading) {
        button.disabled = true;
        button.classList.add('loading');
        button.dataset.originalText = button.innerHTML;
        button.innerHTML = `<span class="spinner"></span>${loadingText}`;
    } else {
        button.disabled = false;
        button.classList.remove('loading');
        if (button.dataset.originalText) {
            button.innerHTML = button.dataset.originalText;
            delete button.dataset.originalText;
        }
    }
}

// Make credentials section collapsible (if exists)
const collapsible = document.querySelector('.collapsible');
if (collapsible) {
    collapsible.onclick = function() {
        const content = this.nextElementSibling;
        content.style.display = content.style.display === 'block' ? 'none' : 'block';
    };
}

document.getElementById('preset').onchange = function() {
    const preset = presets[this.value];
    if (preset) {
        document.getElementById('posts_limit').value = preset.posts_limit;
        document.getElementById('interval').value = preset.interval;
        document.getElementById('comment_batch').value = preset.comment_batch;

        // Update sorting method checkboxes
        if (preset.sorting_methods) {
            document.querySelectorAll('input[name="sorting"]').forEach(checkbox => {
                checkbox.checked = preset.sorting_methods.includes(checkbox.value);
            });
        }
    }
};

async function loadHealthStatus() {
    try {
        const response = await fetch('/health');
        const health = await response.json();
        const healthDiv = document.getElementById('health-status');

        const dbStatus = health.database_connected;
        const dockerStatus = health.docker_available;

        healthDiv.innerHTML = `
            <div class="health-grid">
                <div class="health-card ${dbStatus ? 'success' : 'error'}">
                    <div class="health-label">Database</div>
                    <div class="health-value ${dbStatus ? 'accent-green' : 'accent-red'}">${dbStatus ? 'Online' : 'Offline'}</div>
                    <div class="health-status-indicator">
                        <span class="status-dot ${dbStatus ? '' : 'offline'}"></span>
                        <span>${dbStatus ? 'MongoDB Connected' : 'Connection Failed'}</span>
                    </div>
                </div>

                <div class="health-card ${dockerStatus ? 'success' : 'error'}">
                    <div class="health-label">Docker</div>
                    <div class="health-value ${dockerStatus ? 'accent-green' : 'accent-red'}">${dockerStatus ? 'Ready' : 'Unavailable'}</div>
                    <div class="health-status-indicator">
                        <span class="status-dot ${dockerStatus ? '' : 'offline'}"></span>
                        <span>${dockerStatus ? 'Engine Running' : 'Not Available'}</span>
                    </div>
                </div>

                <div class="health-card">
                    <div class="health-label">Total Scrapers</div>
                    <div class="health-value accent-cyan">${health.total_scrapers || 0}</div>
                    <div class="health-status-indicator">
                        <span>Configured instances</span>
                    </div>
                </div>

                <div class="health-card success">
                    <div class="health-label">Running</div>
                    <div class="health-value accent-green">${health.running_containers || 0}</div>
                    <div class="health-status-indicator">
                        <span class="status-dot"></span>
                        <span>Active containers</span>
                    </div>
                </div>

                <div class="health-card ${health.failed_scrapers > 0 ? 'error' : ''}">
                    <div class="health-label">Failed</div>
                    <div class="health-value ${health.failed_scrapers > 0 ? 'accent-red' : ''}">${health.failed_scrapers || 0}</div>
                    <div class="health-status-indicator">
                        <span>${health.failed_scrapers > 0 ? 'Needs attention' : 'All healthy'}</span>
                    </div>
                </div>
            </div>
        `;
    } catch (error) {
        console.error('Error loading health status:', error);
        document.getElementById('health-status').innerHTML = `
            <div class="health-grid">
                <div class="health-card error">
                    <div class="health-label">System Status</div>
                    <div class="health-value accent-red">Error</div>
                    <div class="health-status-indicator">
                        <span class="status-dot offline"></span>
                        <span>Failed to load health status</span>
                    </div>
                </div>
            </div>
        `;
    }
}

async function loadAccountStats() {
    const container = document.getElementById('accounts');
    if (!container) return;

    try {
        const response = await fetch('/accounts/stats');
        const stats = await response.json();
        const accounts = Object.values(stats);

        container.innerHTML = `
            <div class="section-header">
                <h2 class="section-title">Reddit Accounts <span class="count">${accounts.length}</span></h2>
                <button onclick="showAccountManager()" class="stats">Manage</button>
            </div>
        `;

        if (accounts.length === 0) {
            container.innerHTML += `
                <div class="empty-state" style="padding: 40px 20px;">
                    <div class="empty-state-icon">🔑</div>
                    <p class="empty-state-text">No saved accounts</p>
                    <p class="empty-state-hint">Add an account to start scraping</p>
                </div>
            `;
            return;
        }

        accounts.forEach(account => {
            const statusColor = account.running_count > 0 ? 'var(--accent-green)' : 'var(--text-muted)';
            container.innerHTML += `
                <div class="account-card">
                    <div class="account-info">
                        <div>
                            <div class="account-name">${account.account_name}</div>
                            <div class="account-username">u/${account.username}</div>
                        </div>
                    </div>
                    <div class="account-stats">
                        <div class="account-stat">
                            <div class="account-stat-value" style="color: ${statusColor}">${account.running_count}</div>
                            <div class="account-stat-label">Active</div>
                        </div>
                        <div class="account-stat">
                            <div class="account-stat-value">${account.scraper_count}</div>
                            <div class="account-stat-label">Scrapers</div>
                        </div>
                        <div class="account-stat">
                            <div class="account-stat-value">${account.subreddit_count}</div>
                            <div class="account-stat-label">Subreddits</div>
                        </div>
                    </div>
                </div>
            `;
        });
    } catch (error) {
        console.error('Error loading account stats:', error);
    }
}

function toggleScraper(header) {
    const scraper = header.closest('.scraper');
    const details = scraper.querySelector('.scraper-details');
    const isExpanded = details.classList.contains('show');

    if (isExpanded) {
        details.classList.remove('show');
        scraper.classList.remove('expanded');
    } else {
        details.classList.add('show');
        scraper.classList.add('expanded');
    }
}

function expandAllScrapers() {
    document.querySelectorAll('.scraper').forEach(scraper => {
        const details = scraper.querySelector('.scraper-details');
        details.classList.add('show');
        scraper.classList.add('expanded');
    });
}

function collapseAllScrapers() {
    document.querySelectorAll('.scraper').forEach(scraper => {
        const details = scraper.querySelector('.scraper-details');
        details.classList.remove('show');
        scraper.classList.remove('expanded');
    });
}

async function loadScrapers() {
    const container = document.getElementById('scrapers');

    try {
        // Save expanded state before refresh
        const expandedScrapers = new Set();
        document.querySelectorAll('.scraper.expanded').forEach(el => {
            if (el.dataset.subreddit) {
                expandedScrapers.add(el.dataset.subreddit);
            }
        });

        // Show skeleton loading on first load
        if (!container.querySelector('.scraper')) {
            container.innerHTML = `
                <div class="section-header">
                    <h2 class="section-title">Active Scrapers</h2>
                </div>
                <div class="scrapers-loading">
                    <div class="skeleton-card"></div>
                    <div class="skeleton-card"></div>
                    <div class="skeleton-card"></div>
                </div>
            `;
        }

        const response = await fetch('/scrapers');
        const scrapers = await response.json();
        const scraperCount = Object.keys(scrapers).length;

        // Calculate totals across all scrapers
        let globalTotalPosts = 0;
        let globalTotalComments = 0;
        Object.values(scrapers).forEach(info => {
            globalTotalPosts += info.database_totals?.total_posts || 0;
            globalTotalComments += info.database_totals?.total_comments || 0;
        });

        container.innerHTML = `
            <div class="section-header">
                <h2 class="section-title">Active Scrapers <span class="count">${scraperCount}</span></h2>
                <div class="section-stats">
                    <div class="stat-item">
                        <span class="stat-value green">${globalTotalPosts.toLocaleString()}</span>
                        <span class="stat-label">posts</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-value blue">${globalTotalComments.toLocaleString()}</span>
                        <span class="stat-label">comments</span>
                    </div>
                    ${scraperCount > 0 ? `
                    <div class="section-actions">
                        <button onclick="expandAllScrapers()" class="stats">Expand All</button>
                        <button onclick="collapseAllScrapers()" class="stats">Collapse All</button>
                    </div>
                    ` : ''}
                </div>
            </div>
        `;

        if (scraperCount === 0) {
            container.innerHTML += `
                <div class="empty-state">
                    <div class="empty-state-icon">📡</div>
                    <p class="empty-state-text">No active scrapers</p>
                    <p class="empty-state-hint">Launch a new scraper using the form below</p>
                </div>
            `;
            return;
        }
        
        Object.entries(scrapers).forEach(([subreddit, info]) => {
            const statusClass = info.status || 'stopped';
            const badgeClass = `badge-${statusClass}`;
            const restartCount = info.restart_count || 0;
            const autoRestart = info.config?.auto_restart !== false;

            const totalPosts = (info.database_totals?.total_posts || 0).toLocaleString();
            const totalComments = (info.database_totals?.total_comments || 0).toLocaleString();
            const collectionRate = info.metrics ? `${(info.metrics.posts_per_hour || 0).toFixed(1)} posts/hr` : 'N/A';

            // Handle multi-subreddit display
            const allSubreddits = info.subreddits || [subreddit];
            const isMulti = allSubreddits.length > 1;
            const scraperName = info.name;
            let displayTitle;
            if (scraperName) {
                // Use custom name
                displayTitle = `${scraperName} <span class="text-muted" style="font-size: 0.85rem; font-weight: 400;">(${allSubreddits.length} sub${allSubreddits.length > 1 ? 's' : ''})</span>`;
            } else if (isMulti) {
                displayTitle = `r/${subreddit} <span class="text-muted" style="font-size: 0.85rem; font-weight: 400;">+${allSubreddits.length - 1} more</span>`;
            } else {
                displayTitle = `r/${subreddit}`;
            }
            const multiBadge = isMulti && !scraperName
                ? `<span class="mode-badge multi">${allSubreddits.length} subs</span>`
                : '';

            const div = document.createElement('div');
            div.className = `scraper ${statusClass}`;
            div.dataset.subreddit = subreddit;
            div.innerHTML = `
                <div class="scraper-header" onclick="toggleScraper(this)">
                    <div class="scraper-title">
                        <h3>${displayTitle}${multiBadge}</h3>
                        <span class="status-badge ${badgeClass}">${info.status?.toUpperCase() || 'UNKNOWN'}</span>
                    </div>
                    <div class="scraper-summary">
                        <div class="scraper-stat">
                            <span class="value">📊 ${totalPosts}</span>
                            <span>posts</span>
                        </div>
                        <div class="scraper-stat">
                            <span class="value blue">${totalComments}</span>
                            <span>comments</span>
                        </div>
                        <div class="scraper-stat">
                            <span>⚡ ${collectionRate}</span>
                        </div>
                        <span class="expand-icon">▼</span>
                    </div>
                </div>
                <div class="scraper-details">
                    <div class="scraper-content">
                        ${isMulti ? `
                        <div class="meta-item" style="margin-bottom: 16px;">
                            <span class="meta-label">Subreddits (${allSubreddits.length})</span>
                            <div class="subreddit-grid">
                                ${allSubreddits.map(s => {
                                    const stats = info.subreddit_stats?.[s] || { posts: 0, comments: 0 };
                                    return `<div class="subreddit-chip">
                                        <span class="name">r/${s}</span>
                                        <span class="stats">${stats.posts} / ${stats.comments}</span>
                                    </div>`;
                                }).join('')}
                            </div>
                        </div>
                        ` : ''}

                        <div class="scraper-meta-grid">
                            <div class="meta-item">
                                <span class="meta-label">Reddit User</span>
                                <span class="meta-value">${info.config?.credentials?.username || 'N/A'}</span>
                            </div>
                            <div class="meta-item">
                                <span class="meta-label">Container</span>
                                <span class="meta-value">${info.container_name || 'N/A'}</span>
                            </div>
                            <div class="meta-item">
                                <span class="meta-label">Posts Limit</span>
                                <span class="meta-value">${info.config?.posts_limit || 'N/A'}</span>
                            </div>
                            <div class="meta-item">
                                <span class="meta-label">Interval</span>
                                <span class="meta-value">${info.config?.interval || 'N/A'}s</span>
                            </div>
                        </div>

                        <div class="db-stats-box">
                            <div class="db-stats-title">📊 Database Totals</div>
                            <div class="db-stats-row">
                                <div class="db-stat">
                                    <span class="num green">${totalPosts}</span>
                                    <span class="label">posts</span>
                                </div>
                                <div class="db-stat">
                                    <span class="num blue">${totalComments}</span>
                                    <span class="label">comments</span>
                                </div>
                            </div>
                            ${info.metrics ? `
                            <div class="db-stats-meta">
                                Scraper collected: ${(info.metrics.total_posts_collected || 0).toLocaleString()} posts (${(info.metrics.posts_per_hour || 0).toFixed(1)}/hr), ${(info.metrics.total_comments_collected || 0).toLocaleString()} comments (${(info.metrics.comments_per_hour || 0).toFixed(1)}/hr)<br>
                                Last cycle: ${info.metrics.last_cycle_posts || 0} posts, ${info.metrics.last_cycle_comments || 0} comments
                                ${info.metrics.last_cycle_time ? ` at ${new Date(info.metrics.last_cycle_time).toLocaleTimeString()}` : ''}
                                ${info.metrics.total_cycles ? ` • ${info.metrics.total_cycles} cycles` : ''}
                            </div>
                            ` : ''}
                        </div>

                        <div class="scraper-meta-grid" style="margin-top: 16px;">
                            <div class="meta-item">
                                <span class="meta-label">Restarts</span>
                                <span class="meta-value">${restartCount}</span>
                            </div>
                            <div class="meta-item" style="width: 70px;">
                                <span class="meta-label">Auto-restart</span>
                                <label class="toggle">
                                    <input type="checkbox" ${autoRestart ? 'checked' : ''} onchange="toggleAutoRestart('${subreddit}', this.checked)">
                                    <span class="slider"></span>
                                </label>
                            </div>
                            ${info.started_at ? `
                            <div class="meta-item">
                                <span class="meta-label">Started</span>
                                <span class="meta-value">${new Date(info.started_at).toLocaleString()}</span>
                            </div>
                            ` : ''}
                            ${info.last_updated ? `
                            <div class="meta-item">
                                <span class="meta-label">Last Updated</span>
                                <span class="meta-value">${new Date(info.last_updated).toLocaleString()}</span>
                            </div>
                            ` : ''}
                        </div>

                        ${info.last_error ? `<p style="color: var(--accent-red); margin-top: 12px;"><strong>Error:</strong> ${info.last_error}</p>` : ''}

                        <div style="margin-top: 20px; display: flex; gap: 8px; flex-wrap: wrap;">
                            <button onclick="event.stopPropagation(); stopScraper(this, '${subreddit}')" class="stop">Stop</button>
                            <button onclick="event.stopPropagation(); restartScraper(this, '${subreddit}')" class="restart">Restart</button>
                            <button onclick="event.stopPropagation(); openSubredditModal('${subreddit}', JSON.parse(this.dataset.subs))" data-subs='${JSON.stringify(allSubreddits)}' class="stats">Edit Subs</button>
                            <button onclick="event.stopPropagation(); getStats(this, '${subreddit}')" class="stats">Stats</button>
                            <button onclick="event.stopPropagation(); getLogs(this, '${subreddit}')" class="stats">Logs</button>
                            <button onclick="event.stopPropagation(); deleteScraper(this, '${subreddit}')" class="delete">Delete</button>
                        </div>
                    </div>
                </div>
            `;
            container.appendChild(div);

            // Restore expanded state
            if (expandedScrapers.has(subreddit)) {
                div.classList.add('expanded');
                const details = div.querySelector('.scraper-details');
                if (details) details.classList.add('show');
            }
        });
    } catch (error) {
        console.error('Error loading scrapers:', error);
        container.innerHTML = `
            <div class="section-header">
                <h2 class="section-title">Active Scrapers</h2>
            </div>
            <div class="error-state">
                <span style="font-size: 2rem;">⚠️</span>
                <p>Failed to load scrapers</p>
                <button onclick="loadScrapers()" class="btn btn-secondary">Retry</button>
            </div>
        `;
    }
}

// Subreddit mode toggle
function toggleSubredditInput() {
    const mode = document.getElementById('scraper_mode').value;
    const singleInput = document.getElementById('single-subreddit-input');
    const multiInput = document.getElementById('multi-subreddit-input');
    const modeIndicator = document.getElementById('mode-indicator');

    if (mode === 'single') {
        singleInput.style.display = 'block';
        multiInput.style.display = 'none';
        modeIndicator.className = 'mode-badge single';
        modeIndicator.textContent = '1 subreddit';
    } else {
        singleInput.style.display = 'none';
        multiInput.style.display = 'block';
        modeIndicator.className = 'mode-badge multi';
        modeIndicator.textContent = 'up to 100';
    }
    updateMultiSubredditCount();
}

// Update count when typing in multi-subreddit textarea
function updateMultiSubredditCount() {
    const textarea = document.getElementById('subreddits');
    const modeIndicator = document.getElementById('mode-indicator');
    const mode = document.getElementById('scraper_mode').value;

    if (mode === 'multi' && textarea.value.trim()) {
        const count = textarea.value.split(',').filter(s => s.trim()).length;
        modeIndicator.textContent = count + ' subreddit' + (count !== 1 ? 's' : '');
    }
}

// Add event listener to textarea
document.addEventListener('DOMContentLoaded', function() {
    const textarea = document.getElementById('subreddits');
    if (textarea) {
        textarea.addEventListener('input', updateMultiSubredditCount);
    }
    // Fetch cost data on page load
    fetchCostData();
});

// Cost Tracker functions
let costPanelCollapsed = false;

function toggleCostPanel() {
    const content = document.getElementById('cost-content');
    const toggle = document.getElementById('cost-toggle');
    costPanelCollapsed = !costPanelCollapsed;

    if (costPanelCollapsed) {
        content.style.display = 'none';
        toggle.textContent = '▶';
    } else {
        content.style.display = 'block';
        toggle.textContent = '▼';
    }
}

let breakdownCollapsed = false;

function toggleBreakdownTable() {
    const table = document.querySelector('#subredditTable');
    const toggle = document.getElementById('breakdown-toggle');
    breakdownCollapsed = !breakdownCollapsed;

    if (breakdownCollapsed) {
        table.style.display = 'none';
        toggle.textContent = '▶';
    } else {
        table.style.display = 'table';
        toggle.textContent = '▼';
    }
}

async function fetchCostData() {
    try {
        const response = await fetch('/api/usage/cost');
        const data = await response.json();

        if (data.status !== 'ok') {
            console.error('Cost API error:', data);
            return;
        }

        // Today
        document.getElementById('costToday').textContent =
            '$' + data.today.cost_usd.toFixed(2);
        document.getElementById('reqsToday').textContent =
            formatNumber(data.today.requests) + ' reqs';

        // Last Hour
        document.getElementById('costHour').textContent =
            '$' + data.last_hour.cost_usd.toFixed(4);
        document.getElementById('reqsHour').textContent =
            formatNumber(data.last_hour.requests) + ' reqs';

        // Avg/Hour
        document.getElementById('costAvgHour').textContent =
            '$' + data.averages.hourly_cost_usd.toFixed(4);
        document.getElementById('reqsAvgHour').textContent =
            formatNumber(data.averages.hourly_requests) + ' reqs';

        // Avg/Day
        document.getElementById('costAvgDay').textContent =
            '$' + data.averages.daily_cost_usd.toFixed(2);
        document.getElementById('reqsAvgDay').textContent =
            formatNumber(data.averages.daily_requests) + ' reqs';

        // Monthly Projection
        document.getElementById('costMonthly').textContent =
            '$' + data.projections.monthly_cost_usd.toFixed(2);
        document.getElementById('reqsMonthly').textContent =
            formatNumber(data.projections.monthly_requests) + ' reqs';

        // Update timestamp
        document.getElementById('cost-updated').textContent =
            'Updated: ' + new Date().toLocaleTimeString();

        // Render subreddit breakdown table
        if (data.by_subreddit && Object.keys(data.by_subreddit).length > 0) {
            const tbody = document.querySelector('#subredditTable tbody');
            tbody.innerHTML = Object.entries(data.by_subreddit)
                .sort((a, b) => b[1].requests - a[1].requests)
                .map(([sub, stats]) =>
                    `<tr>
                        <td>r/${sub}</td>
                        <td>${stats.requests.toLocaleString()}</td>
                        <td>$${stats.cost_usd.toFixed(4)}</td>
                    </tr>`
                ).join('');
            document.getElementById('subredditBreakdown').style.display = 'block';
        } else {
            document.getElementById('subredditBreakdown').style.display = 'none';
        }

    } catch (error) {
        console.error('Failed to fetch cost data:', error);
    }
}

function formatNumber(num) {
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
    } else if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
    }
    return num.toLocaleString();
}

// Auto-refresh cost data every 60 seconds
setInterval(fetchCostData, 60000);

// Account management functions
function toggleAccountType() {
    const accountType = document.getElementById('account_type').value;
    const savedSection = document.getElementById('saved_account_section');
    const manualSection = document.getElementById('manual_credentials_section');

    if (accountType === 'saved') {
        savedSection.style.display = 'block';
        manualSection.style.display = 'none';
    } else {
        savedSection.style.display = 'none';
        manualSection.style.display = 'block';
    }
}

async function loadSavedAccounts() {
    try {
        const response = await fetch('/accounts');
        const accounts = await response.json();
        const select = document.getElementById('saved_account_name');
        
        // Clear existing options
        select.innerHTML = '<option value="">Select an account...</option>';
        
        // Add accounts
        Object.keys(accounts).forEach(accountName => {
            const option = document.createElement('option');
            option.value = accountName;
            option.textContent = `${accountName} (${accounts[accountName].username})`;
            select.appendChild(option);
        });
    } catch (error) {
        console.error('Error loading saved accounts:', error);
    }
}

function showAccountManager() {
    document.getElementById('accountManagerModal').style.display = 'block';
    loadAccountsInManager();
}

function hideAccountManager() {
    document.getElementById('accountManagerModal').style.display = 'none';
    // Clear form
    ['new_account_name', 'new_client_id', 'new_client_secret', 'new_username', 'new_password', 'new_user_agent'].forEach(id => {
        document.getElementById(id).value = '';
    });
}

async function saveNewAccount() {
    const accountName = document.getElementById('new_account_name').value;
    const credentials = {
        client_id: document.getElementById('new_client_id').value,
        client_secret: document.getElementById('new_client_secret').value,
        username: document.getElementById('new_username').value,
        password: document.getElementById('new_password').value,
        user_agent: document.getElementById('new_user_agent').value
    };

    // Validate
    if (!accountName) {
        alert('Please enter an account name');
        return;
    }

    if (!Object.values(credentials).every(v => v)) {
        alert('Please fill in all credential fields');
        return;
    }

    // Get the save button and show loading state
    const saveBtn = event.target;
    const originalText = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.textContent = 'Validating...';

    try {
        const response = await fetch(`/accounts?account_name=${encodeURIComponent(accountName)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(credentials)
        });

        if (response.ok) {
            alert('Account saved successfully! Credentials validated.');
            loadAccountsInManager();
            loadSavedAccounts();
            // Clear form
            ['new_account_name', 'new_client_id', 'new_client_secret', 'new_username', 'new_password', 'new_user_agent'].forEach(id => {
                document.getElementById(id).value = '';
            });
        } else {
            const error = await response.json();
            alert('Validation failed: ' + error.detail);
        }
    } catch (error) {
        alert('Error: ' + error.message);
    } finally {
        // Restore button state
        saveBtn.disabled = false;
        saveBtn.textContent = originalText;
    }
}

async function loadAccountsInManager() {
    try {
        const response = await fetch('/accounts');
        const accounts = await response.json();
        const container = document.getElementById('savedAccountsList');

        if (Object.keys(accounts).length === 0) {
            container.innerHTML = '<p style="color: var(--text-muted);">No saved accounts yet.</p>';
            return;
        }

        container.innerHTML = '';
        Object.entries(accounts).forEach(([accountName, account]) => {
            const div = document.createElement('div');
            div.className = 'subreddit-chip';
            div.style.cssText = 'display: flex; justify-content: space-between; align-items: center; padding: 14px 16px;';
            div.innerHTML = `
                <div>
                    <span class="name" style="color: var(--accent-cyan); font-weight: 600;">${accountName}</span><br>
                    <small style="color: var(--text-muted);">User: ${account.username} | Created: ${new Date(account.created_at).toLocaleDateString()}</small>
                </div>
                <button onclick="deleteAccount('${accountName}')" class="delete" style="padding: 6px 12px;">Delete</button>
            `;
            container.appendChild(div);
        });
    } catch (error) {
        console.error('Error loading accounts in manager:', error);
    }
}

async function deleteAccount(accountName) {
    if (confirm(`Are you sure you want to delete account "${accountName}"?`)) {
        try {
            const response = await fetch(`/accounts/${encodeURIComponent(accountName)}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                alert('Account deleted successfully!');
                loadAccountsInManager();
                loadSavedAccounts();
            } else {
                alert('Error deleting account');
            }
        } catch (error) {
            alert('Error deleting account: ' + error.message);
        }
    }
}

async function startScraper() {
    const button = document.getElementById('startScraperBtn');
    const accountType = document.getElementById('account_type').value;
    const scraperMode = document.getElementById('scraper_mode').value;
    const scraperType = document.getElementById('scraper_type').value;

    setButtonLoading(button, true, 'Starting...');

    try {
        // Collect sorting methods from checkboxes
        const sortingMethods = Array.from(document.querySelectorAll('input[name="sorting"]:checked'))
                                    .map(cb => cb.value);

        if (sortingMethods.length === 0) {
            alert('Please select at least one sorting method');
            setButtonLoading(button, false);
            return;
        }

        let requestData = {
            scraper_type: scraperType,
            posts_limit: parseInt(document.getElementById('posts_limit').value),
            interval: parseInt(document.getElementById('interval').value),
            comment_batch: parseInt(document.getElementById('comment_batch').value),
            sorting_methods: sortingMethods,
            auto_restart: document.getElementById('auto_restart').checked
        };

        // Add custom scraper name if provided
        const scraperName = document.getElementById('scraper_name').value.trim();
        if (scraperName) {
            requestData.name = scraperName;
        }

        // Handle single vs multi-subreddit mode
        if (scraperMode === 'single') {
            const subreddit = document.getElementById('subreddit').value.trim();
            if (!subreddit) {
                alert('Please enter a subreddit name');
                setButtonLoading(button, false);
                return;
            }
            requestData.subreddit = subreddit;
        } else {
            // Multi-subreddit mode
            const subredditsText = document.getElementById('subreddits').value;
            const subreddits = subredditsText.split(',').map(s => s.trim()).filter(s => s);
            if (subreddits.length === 0) {
                alert('Please enter at least one subreddit');
                setButtonLoading(button, false);
                return;
            }
            if (subreddits.length > 100) {
                alert('Maximum 100 subreddits per container');
                setButtonLoading(button, false);
                return;
            }
            requestData.subreddits = subreddits;
        }
        
        if (accountType === 'saved') {
            const savedAccountName = document.getElementById('saved_account_name').value;
            if (!savedAccountName) {
                alert('Please select a saved account');
                return;
            }
            requestData.saved_account_name = savedAccountName;
        } else {
            // Manual credentials
            const credentials = {
                client_id: document.getElementById('client_id').value,
                client_secret: document.getElementById('client_secret').value,
                username: document.getElementById('username').value,
                password: document.getElementById('password').value,
                user_agent: document.getElementById('user_agent').value
            };
            
            if (!Object.values(credentials).every(v => v)) {
                alert('Please fill in all credential fields');
                return;
            }
            
            requestData.credentials = credentials;
            
            // Optionally save account
            const saveAccountAs = document.getElementById('save_account_as').value;
            if (saveAccountAs) {
                requestData.save_account_as = saveAccountAs;
            }
        }
        
        const response = await fetch('/scrapers/start-flexible', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestData)
        });
        
        if (response.ok) {
            const result = await response.json();
            let message = 'Scraper started successfully!';
            if (result.saved_new_account) {
                message += ` Account saved as "${requestData.save_account_as}".`;
            }
            alert(message);
            
            // Clear sensitive fields
            if (accountType === 'manual') {
                ['client_id', 'client_secret', 'password', 'save_account_as'].forEach(id => {
                    document.getElementById(id).value = '';
                });
            }
            
            loadScrapers();
            loadHealthStatus();
            loadSavedAccounts(); // Refresh in case account was saved
        } else {
            const error = await response.json();
            alert('Error: ' + error.detail);
        }
    } catch (error) {
        alert('Error starting scraper: ' + error.message);
    } finally {
        setButtonLoading(button, false);
    }
}

async function stopScraper(button, subreddit) {
    setButtonLoading(button, true, 'Stopping...');
    
    try {
        const response = await fetch(`/scrapers/${subreddit}/stop`, { method: 'POST' });
        if (response.ok) {
            alert('Scraper stopped!');
            loadScrapers();
            loadHealthStatus();
        } else {
            alert('Error stopping scraper');
        }
    } catch (error) {
        alert('Error stopping scraper: ' + error.message);
    } finally {
        setButtonLoading(button, false);
    }
}

async function restartScraper(button, subreddit) {
    setButtonLoading(button, true, 'Restarting...');
    
    try {
        const response = await fetch(`/scrapers/${subreddit}/restart`, { method: 'POST' });
        if (response.ok) {
            alert('Scraper restarting!');
            loadScrapers();
            loadHealthStatus();
        } else {
            alert('Error restarting scraper');
        }
    } catch (error) {
        alert('Error restarting scraper: ' + error.message);
    } finally {
        setButtonLoading(button, false);
    }
}

async function deleteScraper(button, subreddit) {
    if (confirm(`Are you sure you want to permanently delete the scraper for r/${subreddit}?`)) {
        setButtonLoading(button, true, 'Deleting...');
        
        try {
            const response = await fetch(`/scrapers/${subreddit}`, { method: 'DELETE' });
            if (response.ok) {
                alert('Scraper deleted!');
                loadScrapers();
                loadHealthStatus();
            } else {
                alert('Error deleting scraper');
            }
        } catch (error) {
            alert('Error deleting scraper: ' + error.message);
        } finally {
            setButtonLoading(button, false);
        }
    }
}

async function toggleAutoRestart(subreddit, enabled) {
    showGlobalLoading(`${enabled ? 'Enabling' : 'Disabling'} auto-restart...`);
    
    try {
        const response = await fetch(`/scrapers/${subreddit}/auto-restart?auto_restart=${enabled}`, { method: 'PUT' });
        if (!response.ok) {
            alert('Error updating auto-restart setting');
            loadScrapers(); // Reload to reset toggle
        }
    } catch (error) {
        alert('Error updating auto-restart: ' + error.message);
        loadScrapers(); // Reload to reset toggle
    } finally {
        hideGlobalLoading();
    }
}

async function getStats(button, subreddit) {
    setButtonLoading(button, true, 'Loading...');
    
    try {
        const response = await fetch(`/scrapers/${subreddit}/stats`);
        const stats = await response.json();
        const statsText = `
r/${subreddit} Statistics:
━━━━━━━━━━━━━━━━━━━━━━
Posts: ${stats.total_posts.toLocaleString()}
Comments: ${stats.total_comments.toLocaleString()}
Initial Completion: ${stats.initial_completion_rate.toFixed(1)}%
Metadata: ${stats.subreddit_metadata_exists ? 'Yes' : 'No'}
Last Updated: ${stats.subreddit_last_updated ? new Date(stats.subreddit_last_updated).toLocaleString() : 'Never'}
        `;
        alert(statsText);
    } catch (error) {
        alert('Error loading stats: ' + error.message);
    } finally {
        setButtonLoading(button, false);
    }
}

async function getLogs(button, subreddit) {
    setButtonLoading(button, true, 'Loading...');
    
    try {
        const response = await fetch(`/scrapers/${subreddit}/logs`);
        const logs = await response.json();
        const logsText = `
r/${subreddit} Logs:
━━━━━━━━━━━━━━━━━━━━━━
${logs.logs}
        `;
        alert(logsText);
    } catch (error) {
        alert('Error loading logs: ' + error.message);
    } finally {
        setButtonLoading(button, false);
    }
}



// ===== Subreddit Management Modal Functions =====
let currentEditingScraper = null;
let currentEditingSubreddits = [];

// Track original subreddits and current state
let originalSubreddits = [];
let editedSubreddits = new Set();
let removedSubreddits = new Set();

function openSubredditModal(subreddit, subreddits) {
    currentEditingScraper = subreddit;
    currentEditingSubreddits = subreddits || [subreddit];

    // Initialize state
    originalSubreddits = [...currentEditingSubreddits];
    editedSubreddits = new Set(currentEditingSubreddits);
    removedSubreddits = new Set();

    document.getElementById('modalScraperName').textContent = `r/${subreddit}`;

    // Clear input
    document.getElementById('addSubredditInput').value = '';

    // Render chips and update stats
    renderSubredditChips();
    updateSubredditEditStats();
    document.getElementById('subredditModal').style.display = 'flex';

    // Focus input
    setTimeout(() => document.getElementById('addSubredditInput').focus(), 100);
}

function closeSubredditModal() {
    document.getElementById('subredditModal').style.display = 'none';
    currentEditingScraper = null;
    currentEditingSubreddits = [];
    originalSubreddits = [];
    editedSubreddits = new Set();
    removedSubreddits = new Set();
}

function renderSubredditChips() {
    const container = document.getElementById('subredditChipsContainer');

    // Combine all subreddits: existing (possibly removed) + newly added
    const allSubs = new Set([...originalSubreddits, ...editedSubreddits]);

    // Sort: active first (sorted), then removed (sorted)
    const activeSubs = [...allSubs].filter(s => editedSubreddits.has(s) && !removedSubreddits.has(s));
    const removedSubs = [...removedSubreddits];

    const sortedSubs = [...activeSubs.sort(), ...removedSubs.sort()];

    container.innerHTML = sortedSubs.map(sub => {
        const isOriginal = originalSubreddits.includes(sub);
        const isRemoved = removedSubreddits.has(sub);
        const isAdded = !isOriginal && editedSubreddits.has(sub);

        let chipClass = 'subreddit-chip';
        if (isRemoved) chipClass += ' removed';
        else if (isAdded) chipClass += ' added';
        else chipClass += ' existing';

        return `
            <span class="${chipClass}" data-sub="${sub}">
                r/${sub}
                <button class="chip-remove" onclick="removeSubreddit('${sub}')" title="Remove">&times;</button>
                <button class="chip-restore" onclick="restoreSubreddit('${sub}')" title="Restore">undo</button>
            </span>
        `;
    }).join('');

    updateChangeSummary();
}

function updateChangeSummary() {
    const added = [...editedSubreddits].filter(s => !originalSubreddits.includes(s));
    const removed = [...removedSubreddits];

    const summaryEl = document.getElementById('changeSummary');
    const addedEl = document.getElementById('addedSummary');
    const removedEl = document.getElementById('removedSummary');

    if (added.length > 0 || removed.length > 0) {
        summaryEl.style.display = 'flex';

        if (added.length > 0) {
            addedEl.style.display = 'flex';
            document.getElementById('addedCount').textContent = added.length;
        } else {
            addedEl.style.display = 'none';
        }

        if (removed.length > 0) {
            removedEl.style.display = 'flex';
            document.getElementById('removedCount').textContent = removed.length;
        } else {
            removedEl.style.display = 'none';
        }
    } else {
        summaryEl.style.display = 'none';
    }
}

function addSubredditFromInput() {
    const input = document.getElementById('addSubredditInput');
    const text = input.value.trim();

    if (!text) return;

    // Support comma-separated or single entry
    const newSubs = text.split(/[,\s]+/).map(s => s.trim().toLowerCase().replace(/^r\//, '')).filter(s => s);

    // Calculate effective count (excluding removed subs)
    const getEffectiveCount = () => [...editedSubreddits].filter(s => !removedSubreddits.has(s)).length;

    let addedCount = 0;
    let duplicateCount = 0;
    newSubs.forEach(sub => {
        // Check if already in active list
        if (editedSubreddits.has(sub) && !removedSubreddits.has(sub)) {
            duplicateCount++;
            return;
        }

        // Restoring a removed original sub
        if (removedSubreddits.has(sub)) {
            removedSubreddits.delete(sub);
            addedCount++;
            return;
        }

        // Adding new sub - check effective limit
        if (getEffectiveCount() < 100) {
            editedSubreddits.add(sub);
            addedCount++;
        }
    });

    if (addedCount > 0) {
        input.value = '';
        renderSubredditChips();
        updateSubredditEditStats();
    } else if (duplicateCount > 0 && newSubs.length === duplicateCount) {
        // All were duplicates - clear input silently
        input.value = '';
    } else if (newSubs.length > 0 && getEffectiveCount() >= 100) {
        alert('Maximum 100 subreddits per container');
    }
}

function removeSubreddit(sub) {
    if (originalSubreddits.includes(sub)) {
        // Mark as removed (will show with strikethrough)
        removedSubreddits.add(sub);
    } else {
        // Newly added - just remove entirely
        editedSubreddits.delete(sub);
    }
    renderSubredditChips();
    updateSubredditEditStats();
}

function restoreSubreddit(sub) {
    removedSubreddits.delete(sub);
    editedSubreddits.add(sub);
    renderSubredditChips();
    updateSubredditEditStats();
}

async function updateSubredditEditStats() {
    // Get final list (edited minus removed)
    const finalSubs = [...editedSubreddits].filter(s => !removedSubreddits.has(s));
    const count = finalSubs.length;

    document.getElementById('editSubCount').textContent = count;

    // Fetch rate limit preview
    if (count > 0) {
        try {
            const response = await fetch(`/scrapers/rate-limit-preview?subreddit_count=${count}`);
            const data = await response.json();

            const previewEl = document.getElementById('editRatePreview');
            previewEl.textContent = `~${data.estimated_calls_per_minute} API calls/min (${data.usage_percent}%)`;
            previewEl.className = `rate-preview ${data.warning_level}`;

            // Show/hide warning banner
            const warningBanner = document.getElementById('rateWarningBanner');
            if (data.warning_level === 'warning' || data.warning_level === 'critical') {
                warningBanner.style.display = 'flex';
                warningBanner.className = `rate-warning-banner ${data.warning_level}`;
                document.getElementById('rateWarningText').textContent =
                    data.recommendation || 'Approaching Reddit API rate limits';
            } else {
                warningBanner.style.display = 'none';
            }
        } catch (e) {
            console.error('Failed to fetch rate preview:', e);
        }
    } else {
        document.getElementById('editRatePreview').textContent = '';
        document.getElementById('rateWarningBanner').style.display = 'none';
    }
}

async function saveSubreddits() {
    if (!currentEditingScraper) return;

    const button = document.getElementById('saveSubredditsBtn');
    const finalSubs = [...editedSubreddits].filter(s => !removedSubreddits.has(s));

    if (finalSubs.length === 0) {
        alert('Please keep at least one subreddit');
        return;
    }

    if (finalSubs.length > 100) {
        alert('Maximum 100 subreddits per container');
        return;
    }

    setButtonLoading(button, true, 'Saving...');

    try {
        const response = await fetch(`/scrapers/${currentEditingScraper}/subreddits`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subreddits: finalSubs })
        });

        if (response.ok) {
            const result = await response.json();
            closeSubredditModal();
            loadScrapers();
        } else {
            const error = await response.json();
            alert('Error: ' + (error.detail || 'Failed to update subreddits'));
        }
    } catch (error) {
        alert('Error updating subreddits: ' + error.message);
    } finally {
        setButtonLoading(button, false, 'Save & Restart');
    }
}

// Event listeners that need DOM to be ready
document.addEventListener('DOMContentLoaded', function() {
    // Enter key handler for add subreddit input
    const addInput = document.getElementById('addSubredditInput');
    if (addInput) {
        addInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                addSubredditFromInput();
            }
        });
        // Handle paste event for comma-separated values
        addInput.addEventListener('paste', function(e) {
            setTimeout(() => {
                addSubredditFromInput();
            }, 10);
        });
    }

    // Close modal on backdrop click
    const modal = document.getElementById('subredditModal');
    if (modal) {
        modal.addEventListener('click', function(e) {
            if (e.target === this) {
                closeSubredditModal();
            }
        });
    }
});

// Close modal on escape key (can attach to document immediately)
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        const modal = document.getElementById('subredditModal');
        if (modal && modal.style.display === 'flex') {
            closeSubredditModal();
        }
    }
});

// Load scrapers, health, and accounts on page load and refresh every 15 seconds
loadScrapers();
loadHealthStatus();
loadSavedAccounts();
loadAccountStats();
setInterval(() => {
    loadScrapers();
    loadHealthStatus();
    loadAccountStats();
}, 15000);
