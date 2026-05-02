// ---- API Helpers ----
const BASE = '';
let token = localStorage.getItem('ocf_token') || '';
let walletsCache = [];

async function api(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(BASE + path, opts);
  if (res.status === 204) return null;
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
  return data;
}

// ---- Auth ----
function showTab(tab) {
  document.getElementById('login-form').style.display = tab === 'login' ? '' : 'none';
  document.getElementById('register-form').style.display = tab === 'register' ? '' : 'none';
  document.getElementById('tab-login').classList.toggle('active', tab === 'login');
  document.getElementById('tab-register').classList.toggle('active', tab === 'register');
}

async function login(e) {
  e.preventDefault();
  const username = document.getElementById('login-username').value;
  const password = document.getElementById('login-password').value;
  try {
    const form = new URLSearchParams({ username, password });
    const res = await fetch('/api/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form,
    });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail); }
    const data = await res.json();
    token = data.access_token;
    localStorage.setItem('ocf_token', token);
    initApp();
  } catch (err) {
    document.getElementById('login-error').textContent = err.message;
  }
}

async function register(e) {
  e.preventDefault();
  try {
    await api('POST', '/api/auth/register', {
      username: document.getElementById('reg-username').value,
      email: document.getElementById('reg-email').value,
      password: document.getElementById('reg-password').value,
    });
    document.getElementById('reg-error').textContent = '';
    showTab('login');
    document.getElementById('login-username').value = document.getElementById('reg-username').value;
  } catch (err) {
    document.getElementById('reg-error').textContent = err.message;
  }
}

function logout() {
  token = '';
  localStorage.removeItem('ocf_token');
  document.getElementById('auth-section').style.display = '';
  document.getElementById('app-section').style.display = 'none';
  document.getElementById('btn-logout').style.display = 'none';
  document.getElementById('nav-user').textContent = '';
}

// ---- App Init ----
async function initApp() {
  try {
    const me = await api('GET', '/api/auth/me');
    document.getElementById('nav-user').textContent = '👤 ' + me.username;
    document.getElementById('btn-logout').style.display = '';
    document.getElementById('auth-section').style.display = 'none';
    document.getElementById('app-section').style.display = '';
    await loadSummary();
    await loadWallets();
  } catch {
    logout();
  }
}

// ---- Summary ----
async function loadSummary() {
  try {
    const s = await api('GET', '/api/summary/');
    document.getElementById('summary-banner').innerHTML = `
      <div class="summary-item"><div class="label">Wallet Balance</div><div class="value">${fmt(s.total_wallet_balance)}</div></div>
      <div class="summary-item"><div class="label">Credit Card Debt</div><div class="value">${fmt(s.total_credit_card_balance)}</div></div>
      <div class="summary-item"><div class="label">Loan Balance</div><div class="value">${fmt(s.total_loan_balance)}</div></div>
      <div class="summary-item"><div class="label">Monthly Income</div><div class="value">${fmt(s.total_monthly_income)}</div></div>
      <div class="summary-item"><div class="label">Net Cash Flow/mo</div><div class="value">${fmt(s.net_monthly_cash_flow)}</div></div>
    `;
  } catch {}
}

// ---- Page switching ----
function showPage(name, event) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.page-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.target.classList.add('active');
  const loaders = { wallets: loadWallets, transactions: loadTransactions, 'credit-cards': loadCreditCards, loans: loadLoans, income: loadIncomeSources };
  if (loaders[name]) loaders[name]();
}

// ---- Wallets ----
async function loadWallets() {
  const wallets = await api('GET', '/api/wallets/');
  walletsCache = wallets;
  const el = document.getElementById('wallets-list');
  el.innerHTML = wallets.length === 0 ? '<p style="color:var(--text-muted)">No wallets yet. Add one!</p>' : '';
  wallets.forEach(w => {
    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `
      <div class="card-tag">${w.wallet_type}</div>
      <div class="card-title">${esc(w.name)}</div>
      ${w.description ? `<div class="card-subtitle">${esc(w.description)}</div>` : ''}
      <div class="card-amount ${w.balance < 0 ? 'negative' : ''}">${w.currency} ${fmt(w.balance)}</div>
      <div class="card-actions">
        <button class="btn btn-sm btn-primary" onclick="editWallet(${w.id})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteWallet(${w.id})">Delete</button>
      </div>`;
    el.appendChild(div);
  });
  // Refresh wallet select in tx modal
  const sel = document.getElementById('tx-wallet-id');
  sel.innerHTML = wallets.map(w => `<option value="${w.id}">${esc(w.name)}</option>`).join('');
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

function openWalletModal(wallet = null) {
  document.getElementById('wallet-id').value = wallet ? wallet.id : '';
  document.getElementById('wallet-name').value = wallet ? wallet.name : '';
  document.getElementById('wallet-type').value = wallet ? wallet.wallet_type : 'cash';
  document.getElementById('wallet-currency').value = wallet ? wallet.currency : 'USD';
  document.getElementById('wallet-balance').value = wallet ? wallet.balance : 0;
  document.getElementById('wallet-desc').value = wallet ? (wallet.description || '') : '';
  openModal('wallet-modal');
}

async function editWallet(id) {
  const w = walletsCache.find(x => x.id === id);
  if (w) openWalletModal(w);
}

async function saveWallet(e) {
  e.preventDefault();
  const id = document.getElementById('wallet-id').value;
  const payload = {
    name: document.getElementById('wallet-name').value,
    wallet_type: document.getElementById('wallet-type').value,
    currency: document.getElementById('wallet-currency').value,
    balance: parseFloat(document.getElementById('wallet-balance').value),
    description: document.getElementById('wallet-desc').value || null,
  };
  try {
    if (id) await api('PUT', `/api/wallets/${id}`, payload);
    else await api('POST', '/api/wallets/', payload);
    closeModal('wallet-modal');
    await loadWallets();
    await loadSummary();
  } catch (err) { alert(err.message); }
}

async function deleteWallet(id) {
  if (!confirm('Delete this wallet and all its transactions?')) return;
  await api('DELETE', `/api/wallets/${id}`);
  await loadWallets();
  await loadSummary();
}

// ---- Transactions ----
async function loadTransactions() {
  const type = document.getElementById('tx-filter-type').value;
  let url = '/api/transactions/';
  if (type) url += '?transaction_type=' + type;
  const txs = await api('GET', url);
  const tbody = document.getElementById('transactions-body');
  tbody.innerHTML = '';
  if (!txs.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted)">No transactions yet.</td></tr>';
    return;
  }
  txs.forEach(t => {
    const walletName = walletsCache.find(w => w.id === t.wallet_id)?.name || t.wallet_id;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${t.transaction_date}</td>
      <td>${esc(walletName)}</td>
      <td><span class="tx-${t.transaction_type}">${t.transaction_type}</span></td>
      <td>${esc(t.category || '')}</td>
      <td>${esc(t.description || '')}</td>
      <td class="${t.transaction_type === 'income' ? 'tx-income' : t.transaction_type === 'expense' ? 'tx-expense' : 'tx-transfer'}">${fmt(t.amount)}</td>
      <td><button class="btn btn-sm btn-danger" onclick="deleteTransaction(${t.id})">Del</button></td>`;
    tbody.appendChild(tr);
  });
}

async function saveTransaction(e) {
  e.preventDefault();
  const payload = {
    wallet_id: parseInt(document.getElementById('tx-wallet-id').value),
    transaction_type: document.getElementById('tx-type').value,
    amount: parseFloat(document.getElementById('tx-amount').value),
    category: document.getElementById('tx-category').value || null,
    description: document.getElementById('tx-desc').value || null,
    transaction_date: document.getElementById('tx-date').value,
  };
  try {
    await api('POST', '/api/transactions/', payload);
    closeModal('tx-modal');
    await loadTransactions();
    await loadWallets();
    await loadSummary();
  } catch (err) { alert(err.message); }
}

async function deleteTransaction(id) {
  if (!confirm('Delete this transaction?')) return;
  await api('DELETE', `/api/transactions/${id}`);
  await loadTransactions();
  await loadWallets();
  await loadSummary();
}

// ---- Credit Cards ----
let ccCache = [];

async function loadCreditCards() {
  ccCache = await api('GET', '/api/credit-cards/');
  const el = document.getElementById('cc-list');
  el.innerHTML = ccCache.length === 0 ? '<p style="color:var(--text-muted)">No credit cards yet.</p>' : '';
  ccCache.forEach(c => {
    const pct = c.credit_limit > 0 ? Math.min(100, (c.current_balance / c.credit_limit) * 100) : 0;
    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `
      <div class="card-tag">${esc(c.bank || 'Card')}</div>
      <div class="card-title">${esc(c.name)}</div>
      <div class="card-amount">${c.currency} ${fmt(c.current_balance)} <span style="font-size:0.8rem;color:var(--text-muted)">/ ${fmt(c.credit_limit)}</span></div>
      <div class="card-progress">
        <div class="progress-bar-bg"><div class="progress-bar ${pct > 80 ? 'danger' : ''}" style="width:${pct}%"></div></div>
        <div class="card-meta">${pct.toFixed(1)}% used</div>
      </div>
      <div class="card-meta">Closing day: ${c.closing_day || '—'} | Due day: ${c.due_day || '—'}</div>
      <div class="card-actions">
        <button class="btn btn-sm btn-primary" onclick="editCreditCard(${c.id})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteCreditCard(${c.id})">Delete</button>
      </div>`;
    el.appendChild(div);
  });
}

async function editCreditCard(id) {
  const c = ccCache.find(x => x.id === id);
  if (!c) return;
  document.getElementById('cc-id').value = c.id;
  document.getElementById('cc-name').value = c.name;
  document.getElementById('cc-bank').value = c.bank || '';
  document.getElementById('cc-limit').value = c.credit_limit;
  document.getElementById('cc-balance').value = c.current_balance;
  document.getElementById('cc-currency').value = c.currency;
  document.getElementById('cc-closing').value = c.closing_day || '';
  document.getElementById('cc-due').value = c.due_day || '';
  openModal('cc-modal');
}

async function saveCreditCard(e) {
  e.preventDefault();
  const id = document.getElementById('cc-id').value;
  const payload = {
    name: document.getElementById('cc-name').value,
    bank: document.getElementById('cc-bank').value || null,
    credit_limit: parseFloat(document.getElementById('cc-limit').value),
    current_balance: parseFloat(document.getElementById('cc-balance').value),
    currency: document.getElementById('cc-currency').value,
    closing_day: parseInt(document.getElementById('cc-closing').value) || null,
    due_day: parseInt(document.getElementById('cc-due').value) || null,
  };
  try {
    if (id) await api('PUT', `/api/credit-cards/${id}`, payload);
    else await api('POST', '/api/credit-cards/', payload);
    closeModal('cc-modal');
    await loadCreditCards();
    await loadSummary();
  } catch (err) { alert(err.message); }
}

async function deleteCreditCard(id) {
  if (!confirm('Delete this credit card?')) return;
  await api('DELETE', `/api/credit-cards/${id}`);
  await loadCreditCards();
  await loadSummary();
}

// ---- Loans ----
let loansCache = [];

async function loadLoans() {
  loansCache = await api('GET', '/api/loans/');
  const el = document.getElementById('loans-list');
  el.innerHTML = loansCache.length === 0 ? '<p style="color:var(--text-muted)">No loans yet.</p>' : '';
  loansCache.forEach(lo => {
    const pct = lo.principal_amount > 0 ? Math.min(100, (lo.remaining_balance / lo.principal_amount) * 100) : 0;
    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `
      <div class="card-tag">${esc(lo.bank || 'Loan')}</div>
      <div class="card-title">${esc(lo.name)}</div>
      <div class="card-amount">${lo.currency} ${fmt(lo.remaining_balance)} remaining</div>
      <div class="card-progress">
        <div class="progress-bar-bg"><div class="progress-bar ${pct > 80 ? 'danger' : ''}" style="width:${pct}%"></div></div>
        <div class="card-meta">${(100-pct).toFixed(1)}% paid off</div>
      </div>
      <div class="card-meta">Rate: ${lo.interest_rate}% | Monthly: ${lo.currency} ${fmt(lo.monthly_payment)}</div>
      <div class="card-meta">${lo.start_date || ''} → ${lo.end_date || ''}</div>
      <div class="card-actions">
        <button class="btn btn-sm btn-primary" onclick="editLoan(${lo.id})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteLoan(${lo.id})">Delete</button>
      </div>`;
    el.appendChild(div);
  });
}

async function editLoan(id) {
  const lo = loansCache.find(x => x.id === id);
  if (!lo) return;
  document.getElementById('loan-id').value = lo.id;
  document.getElementById('loan-name').value = lo.name;
  document.getElementById('loan-bank').value = lo.bank || '';
  document.getElementById('loan-principal').value = lo.principal_amount;
  document.getElementById('loan-remaining').value = lo.remaining_balance;
  document.getElementById('loan-rate').value = lo.interest_rate;
  document.getElementById('loan-monthly').value = lo.monthly_payment;
  document.getElementById('loan-currency').value = lo.currency;
  document.getElementById('loan-start').value = lo.start_date || '';
  document.getElementById('loan-end').value = lo.end_date || '';
  openModal('loan-modal');
}

async function saveLoan(e) {
  e.preventDefault();
  const id = document.getElementById('loan-id').value;
  const payload = {
    name: document.getElementById('loan-name').value,
    bank: document.getElementById('loan-bank').value || null,
    principal_amount: parseFloat(document.getElementById('loan-principal').value),
    remaining_balance: parseFloat(document.getElementById('loan-remaining').value),
    interest_rate: parseFloat(document.getElementById('loan-rate').value),
    monthly_payment: parseFloat(document.getElementById('loan-monthly').value),
    currency: document.getElementById('loan-currency').value,
    start_date: document.getElementById('loan-start').value || null,
    end_date: document.getElementById('loan-end').value || null,
  };
  try {
    if (id) await api('PUT', `/api/loans/${id}`, payload);
    else await api('POST', '/api/loans/', payload);
    closeModal('loan-modal');
    await loadLoans();
    await loadSummary();
  } catch (err) { alert(err.message); }
}

async function deleteLoan(id) {
  if (!confirm('Delete this loan?')) return;
  await api('DELETE', `/api/loans/${id}`);
  await loadLoans();
  await loadSummary();
}

// ---- Income Sources ----
let incomeCache = [];

async function loadIncomeSources() {
  incomeCache = await api('GET', '/api/income-sources/');
  const el = document.getElementById('income-list');
  el.innerHTML = incomeCache.length === 0 ? '<p style="color:var(--text-muted)">No income sources yet.</p>' : '';
  incomeCache.forEach(src => {
    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `
      <div class="card-tag">${src.income_type}</div>
      <div class="card-title">${esc(src.name)}</div>
      <div class="card-amount">${src.currency} ${fmt(src.amount)} <span style="font-size:0.8rem;color:var(--text-muted)">/ ${src.frequency}</span></div>
      <div class="card-meta">${src.description ? esc(src.description) : ''}</div>
      <span class="badge ${src.is_active ? 'badge-active' : 'badge-inactive'}">${src.is_active ? 'Active' : 'Inactive'}</span>
      <div class="card-actions">
        <button class="btn btn-sm btn-primary" onclick="editIncomeSource(${src.id})">Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteIncomeSource(${src.id})">Delete</button>
      </div>`;
    el.appendChild(div);
  });
}

async function editIncomeSource(id) {
  const src = incomeCache.find(x => x.id === id);
  if (!src) return;
  document.getElementById('income-id').value = src.id;
  document.getElementById('income-name').value = src.name;
  document.getElementById('income-type').value = src.income_type;
  document.getElementById('income-amount').value = src.amount;
  document.getElementById('income-frequency').value = src.frequency;
  document.getElementById('income-currency').value = src.currency;
  document.getElementById('income-desc').value = src.description || '';
  document.getElementById('income-active').checked = src.is_active;
  openModal('income-modal');
}

async function saveIncomeSource(e) {
  e.preventDefault();
  const id = document.getElementById('income-id').value;
  const payload = {
    name: document.getElementById('income-name').value,
    income_type: document.getElementById('income-type').value,
    amount: parseFloat(document.getElementById('income-amount').value),
    frequency: document.getElementById('income-frequency').value,
    currency: document.getElementById('income-currency').value,
    description: document.getElementById('income-desc').value || null,
    is_active: document.getElementById('income-active').checked,
  };
  try {
    if (id) await api('PUT', `/api/income-sources/${id}`, payload);
    else await api('POST', '/api/income-sources/', payload);
    closeModal('income-modal');
    await loadIncomeSources();
    await loadSummary();
  } catch (err) { alert(err.message); }
}

async function deleteIncomeSource(id) {
  if (!confirm('Delete this income source?')) return;
  await api('DELETE', `/api/income-sources/${id}`);
  await loadIncomeSources();
  await loadSummary();
}

// ---- Utilities ----
function fmt(n) {
  return new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Pre-fill today's date in tx modal
document.addEventListener('DOMContentLoaded', () => {
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById('tx-date').value = today;
  if (token) initApp();
});

// Close modal on overlay click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.classList.remove('open');
  });
});
