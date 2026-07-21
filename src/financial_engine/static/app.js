const state = { data: null, selectedAccountCode: null, selectedTransactionId: null, reconciliationPreview: null, currentImportId: null };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const today = new Date();
const isoDate = today.toISOString().slice(0, 10);
const currentMonth = today.toISOString().slice(0, 7);

const money = (minor, currency = "USD") => minor == null
  ? "$—"
  : new Intl.NumberFormat("en-US", { style: "currency", currency }).format(minor / 100);
const shortDate = (iso) => new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(new Date(iso));
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const toMinor = (value) => Math.round(Number.parseFloat(value || "0") * 100);

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Request failed");
  return body;
}

async function loadOverview() {
  const month = $("#month-picker").value || currentMonth;
  setLoading(true);
  try {
    state.data = await api(`/api/overview?month=${encodeURIComponent(month)}`);
    render();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setLoading(false);
  }
}

function render() {
  const { accounts, spending, transactions, expense_categories: categories, review_count: reviewCount, currency } = state.data;
  $("#total-available").textContent = money(state.data.total_available_minor, currency);
  $("#monthly-spend").textContent = money(spending.spent_minor, currency);
  $("#monthly-average").textContent = money(spending.average_minor, currency);
  $("#budget-remaining").textContent = money(spending.remaining_minor, currency);
  $("#budget-context").textContent = spending.budget_minor == null ? "Set a plan to track this" : `${money(spending.budget_minor, currency)} monthly plan`;
  renderAccounts(accounts, currency);
  renderCategories(spending.categories, currency);
  renderInsights(spending.insights);
  renderTransactions(transactions, currency);
  populateSelects(accounts, categories);
  populateBudget(categories, spending.categories);
  const badge = $("#review-badge");
  badge.hidden = reviewCount === 0;
  badge.textContent = reviewCount;
  bindDetailButtons();
}

function renderAccounts(accounts, currency) {
  const cards = accounts.map(account => {
    const isCredit = account.kind === "CREDIT_CARD";
    const label = isCredit ? "Available credit" : account.kind === "INVESTMENT" ? "Current value" : account.kind === "GIFT_CARD" ? "Remaining balance" : account.kind === "CASH" ? "Cash on hand" : account.account_type === "LIABILITY" ? "Amount owed" : "Available balance";
    const balance = isCredit ? account.available_balance_minor : account.account_type === "LIABILITY" ? account.display_balance_minor : account.available_balance_minor;
    const detail = isCredit ? `${money(account.display_balance_minor, currency)} owed` : `Ledger ${money(account.display_balance_minor, currency)}`;
    const source = account.balance_source === "CALCULATED" ? "Calculated" : `${escapeHtml(account.balance_source)} snapshot`;
    return `<button class="account-card account-detail-card" data-account-code="${escapeHtml(account.code)}">
      <div class="account-card-top"><h3>${escapeHtml(account.name)}</h3><span class="account-kind">${escapeHtml(account.kind.replaceAll("_", " "))}</span></div>
      <span class="account-balance-label">${label}</span><strong class="account-balance">${money(balance, currency)}</strong>
      <div class="account-meta"><span>${detail}</span><span>${source}</span></div>
    </button>`;
  });
  if (!accounts.length) {
    cards.push(
      `<button class="account-card placeholder-card" data-kind="CHECKING" data-open="account-dialog"><div class="account-card-top"><h3>Checking account</h3><span class="account-kind">Placeholder</span></div><span class="account-balance-label">Available balance</span><strong class="account-balance">$—</strong><div class="account-meta"><span>Enter starting balance</span><span>＋ Add</span></div></button>`,
      `<button class="account-card placeholder-card" data-kind="SAVINGS" data-open="account-dialog"><div class="account-card-top"><h3>Savings account</h3><span class="account-kind">Placeholder</span></div><span class="account-balance-label">Available balance</span><strong class="account-balance">$—</strong><div class="account-meta"><span>Enter starting balance</span><span>＋ Add</span></div></button>`,
      `<button class="account-card placeholder-card" data-kind="CREDIT_CARD" data-open="account-dialog"><div class="account-card-top"><h3>Credit card</h3><span class="account-kind">Placeholder</span></div><span class="account-balance-label">Available credit</span><strong class="account-balance">$—</strong><div class="account-meta"><span>Enter amount owed</span><span>＋ Add</span></div></button>`
    );
  } else {
    cards.push(`<button class="account-card add-card" data-open="account-dialog"><span>＋</span>Add another account</button>`);
  }
  $("#account-grid").innerHTML = cards.join("");
  bindDialogButtons();
}

function renderCategories(categories, currency) {
  if (!categories.length || categories.every(item => item.spent_minor === 0 && item.budget_minor == null)) {
    $("#category-list").innerHTML = `<div class="empty-state compact">No spending recorded for this month.</div>`;
    return;
  }
  const max = Math.max(1, ...categories.map(item => Math.max(item.spent_minor, item.budget_minor || 0)));
  $("#category-list").innerHTML = categories.slice(0, 8).map(item => {
    const width = Math.min(100, Math.max(0, item.spent_minor / max * 100));
    const caption = item.budget_minor == null ? `${money(item.average_minor, currency)} recent average` : `${money(item.remaining_minor, currency)} remaining of ${money(item.budget_minor, currency)}`;
    return `<div class="category-row"><div class="category-row-head"><strong>${escapeHtml(item.name)}</strong><span>${money(item.spent_minor, currency)}</span></div><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div><div class="category-caption">${caption}</div></div>`;
  }).join("");
}

function renderInsights(insights) {
  $("#insight-list").innerHTML = insights.length ? insights.map(item => `<article class="insight ${item.severity}"><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.explanation)}</p></article>`).join("") : `<div class="empty-state compact">Insights appear as your history grows.</div>`;
}

function renderTransactions(transactions, currency) {
  $("#transaction-list").innerHTML = transactionRows(transactions, currency);
}

function transactionRows(transactions, currency) {
  return transactions.length ? transactions.map(item => {
    const amountClass = item.display_amount_minor > 0 ? "positive" : "";
    const initial = escapeHtml((item.merchant_name || item.description).slice(0, 1).toUpperCase());
    const context = [item.category, item.account_name, shortDate(item.effective_at)].filter(Boolean).join(" · ");
    return `<button class="transaction-row" data-transaction-id="${escapeHtml(item.id)}"><span class="transaction-icon">${initial}</span><div class="transaction-copy"><strong>${escapeHtml(item.merchant_name || item.description)}</strong><small>${escapeHtml(context)}</small></div><span class="transaction-amount ${amountClass}">${item.display_amount_minor > 0 ? "+" : ""}${money(item.display_amount_minor, currency)}</span></button>`;
  }).join("") : `<div class="empty-state">Add an account and your first transaction to begin.</div>`;
}

function populateSelects(accounts, categories) {
  const accountOptions = accounts.map(item => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.name)}</option>`).join("");
  ["#transaction-account", "#transfer-from", "#transfer-to"].forEach(selector => $(selector).innerHTML = accountOptions || `<option value="">Add an account first</option>`);
  $("#reconciliation-account").innerHTML = accountOptions || `<option value="">Add an account first</option>`;
  $("#import-account").innerHTML = accountOptions || `<option value="">Add an account first</option>`;
  setImportConvention();
  $("#transaction-category").innerHTML = categories.map(item => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.name)}</option>`).join("");
}

function populateBudget(categories, current) {
  const byCode = Object.fromEntries(current.map(item => [item.code, item.budget_minor]));
  $("#budget-fields").innerHTML = categories.map(item => `<label>${escapeHtml(item.name)}<div class="money-input"><span>$</span><input inputmode="decimal" data-budget-code="${escapeHtml(item.code)}" value="${byCode[item.code] == null ? "" : (byCode[item.code] / 100).toFixed(2)}" placeholder="0.00"></div></label>`).join("");
  $$('[data-budget-code]').forEach(input => input.addEventListener("input", updateBudgetTotal));
  updateBudgetTotal();
}

function bindDialogButtons() {
  $$('[data-open]').forEach(button => button.onclick = () => {
    if (button.dataset.kind) {
      $("#account-kind").value = button.dataset.kind;
      $("#account-kind").dispatchEvent(new Event("change"));
    }
    const dialog = document.getElementById(button.dataset.open);
    if (dialog && !dialog.open) dialog.showModal();
  });
  $$('[data-close]').forEach(button => button.onclick = () => button.closest("dialog")?.close());
}

function transactionMode() {
  const kind = new FormData($("#transaction-form")).get("kind");
  const transfer = kind === "transfer";
  $("#transfer-fields").hidden = !transfer;
  $("#account-field").hidden = transfer;
  $("#category-field").hidden = kind !== "expense";
  $("#merchant-field").hidden = transfer;
}

function bindDetailButtons() {
  $$('[data-account-code]').forEach(button => button.onclick = () => openAccountDetail(button.dataset.accountCode));
  $$('[data-transaction-id]').forEach(button => button.onclick = () => openTransactionDetail(button.dataset.transactionId));
}

async function openAccountDetail(accountCode) {
  try {
    const detail = await api(`/api/accounts/${encodeURIComponent(accountCode)}`);
    const account = detail.account;
    state.selectedAccountCode = account.code;
    $("#account-detail-kind").textContent = account.kind.replaceAll("_", " ");
    $("#account-detail-name").textContent = account.name;
    const credit = account.kind === "CREDIT_CARD";
    $("#account-primary-label").textContent = credit ? "Available credit" : account.account_type === "LIABILITY" ? "Amount owed" : account.kind === "INVESTMENT" ? "Current value" : "Available balance";
    $("#account-primary-balance").textContent = money(credit ? account.available_balance_minor : account.account_type === "LIABILITY" ? account.display_balance_minor : account.available_balance_minor, account.currency);
    $("#account-ledger-balance").textContent = money(account.display_balance_minor, account.currency);
    const latest = detail.reconciliations[0];
    $("#account-reconcile-status").textContent = latest ? `Matched through ${shortDate(`${latest.statement_date}T12:00:00Z`)}` : "Never reconciled";
    $("#account-activity-count").textContent = `${detail.transactions.length} shown`;
    $("#account-activity").innerHTML = transactionRows(detail.transactions, account.currency);
    bindDetailButtons();
    $("#account-detail-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function openTransactionDetail(transactionId) {
  try {
    if ($("#account-detail-dialog").open) $("#account-detail-dialog").close();
    const detail = await api(`/api/transactions/${encodeURIComponent(transactionId)}`);
    state.selectedTransactionId = detail.id;
    $("#transaction-detail-title").textContent = detail.merchant_name || detail.description;
    $("#transaction-detail-subtitle").textContent = `${shortDate(detail.effective_at)} · ${detail.description}`;
    $("#posting-list").innerHTML = detail.postings.map(posting => `<div class="posting-row"><div><strong>${escapeHtml(posting.account_name)}</strong><small>${escapeHtml(posting.account_code)}${posting.memo ? ` · ${escapeHtml(posting.memo)}` : ""}</small></div><span class="posting-amount ${posting.amount_minor > 0 ? "debit" : ""}">${posting.amount_minor > 0 ? "+" : ""}${money(posting.amount_minor, detail.currency)}</span></div>`).join("");
    const metadata = [detail.source_type && `Source: ${detail.source_type}`, detail.correlation_id && `Group: ${detail.correlation_id}`, detail.reversal_of_transaction_id && "Reversal entry", detail.reversed_by_transaction_id && "Already reversed"].filter(Boolean);
    $("#transaction-metadata").innerHTML = metadata.map(item => `<span class="metadata-pill">${escapeHtml(item)}</span>`).join("");
    $("#reversal-form").hidden = Boolean(detail.reversal_of_transaction_id || detail.reversed_by_transaction_id);
    $("#reversal-form input[name='effective_at']").value = isoDate;
    $("#transaction-detail-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function openReviewQueue() {
  const dialog = $("#review-dialog");
  dialog.showModal();
  $("#review-list").innerHTML = `<div class="empty-state">Loading review queue…</div>`;
  try {
    const result = await api("/api/review");
    renderReviewQueue(result.proposals, result.import_rows);
  } catch (error) { $("#review-list").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
}

function renderReviewQueue(proposals, importRows = []) {
  if (!proposals.length && !importRows.length) {
    $("#review-list").innerHTML = `<div class="empty-state">Nothing needs review. Verified transactions are already in the ledger.</div>`;
    return;
  }
  const proposalMarkup = proposals.length ? `<h3 class="review-section-title">Transaction proposals</h3>${proposals.map(item => `<article class="review-card"><div class="review-card-header"><div><h3>${escapeHtml(item.merchant_name || item.description)}</h3><p>${shortDate(item.effective_at)} · ${escapeHtml(item.source_type)}</p></div><span class="confidence">${item.confidence == null ? "Review required" : `${Math.round(item.confidence * 100)}% confidence`}</span></div><div class="proposal-postings">${item.postings.map(posting => `<div class="proposal-posting"><span>${escapeHtml(posting.account_code)}</span><strong>${money(posting.amount_minor, item.currency)}</strong></div>`).join("")}</div><div class="review-actions"><button class="button secondary" type="button" data-reject-proposal="${item.id}">Reject</button><button class="button primary" type="button" data-approve-proposal="${item.id}">Approve</button></div></article>`).join("")}` : "";
  const importMarkup = importRows.length ? `<h3 class="review-section-title">Statement rows</h3>${importRows.map(importRowMarkup).join("")}` : "";
  $("#review-list").innerHTML = proposalMarkup + importMarkup;
  $$('[data-approve-proposal]').forEach(button => button.onclick = () => reviewProposal(button.dataset.approveProposal, "approve"));
  $$('[data-reject-proposal]').forEach(button => button.onclick = () => reviewProposal(button.dataset.rejectProposal, "reject"));
  bindImportRowActions();
}

async function reviewProposal(proposalId, action) {
  const note = action === "reject" ? window.prompt("Why are you rejecting this proposal?") : "Approved in review queue";
  if (action === "reject" && !note) return;
  try {
    await api(`/api/proposals/${encodeURIComponent(proposalId)}/${action}`, { method: "POST", body: JSON.stringify({ reviewed_by: "local-user", note }) });
    const result = await api("/api/review"); renderReviewQueue(result.proposals, result.import_rows); await loadOverview(); toast(action === "approve" ? "Proposal approved and posted." : "Proposal rejected.");
  } catch (error) { toast(error.message, true); }
}

function updateBudgetTotal() {
  const total = $$('[data-budget-code]').reduce((sum, input) => sum + (Number.isFinite(toMinor(input.value)) ? toMinor(input.value) : 0), 0);
  $("#budget-total").textContent = money(total, state.data?.currency || "USD");
}

function counterpartOptions(row) {
  const expenses = state.data?.expense_categories || [];
  const incomes = state.data?.income_categories || [];
  const accounts = (state.data?.accounts || []).filter(item => item.code !== row.account_code);
  const options = (items, label) => items.length ? `<optgroup label="${label}">${items.map(item => `<option value="${escapeHtml(item.code)}" ${item.code === row.suggested_account_code ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</optgroup>` : "";
  const placeholder = row.suggested_account_code ? "" : `<option value="" selected>Choose a category or transfer account</option>`;
  return placeholder + options(expenses, "Expense categories") + options(incomes, "Income categories") + options(accounts, "Transfers and other accounts");
}

function importRowMarkup(row) {
  const statusClass = row.workflow_status.toLowerCase();
  const amountClass = row.account_amount_minor > 0 ? "inflow" : "";
  const controls = row.workflow_status === "NEEDS_REVIEW" ? `<div class="import-row-controls"><label>Counter-account<select data-counter-row="${row.id}">${counterpartOptions(row)}</select></label><button class="button secondary" type="button" data-ignore-import="${row.id}">Ignore</button><button class="button primary" type="button" data-post-import="${row.id}">Post</button></div>` : row.workflow_status === "PENDING" ? `<div class="import-row-controls"><span></span><button class="button secondary" type="button" data-ignore-import="${row.id}">Ignore pending</button></div>` : "";
  const statusText = row.workflow_status === "MATCHED" ? "Matched existing ledger transaction" : row.workflow_status === "PENDING" ? "Pending — not eligible to post" : row.workflow_status.replaceAll("_", " ");
  return `<article class="import-row ${statusClass}"><div class="import-row-head"><div><strong>${escapeHtml(row.description)}</strong><small>${escapeHtml(row.account_name)} · ${escapeHtml(row.effective_date)}</small></div><span class="import-amount ${amountClass}">${row.account_amount_minor > 0 ? "+" : ""}${money(row.account_amount_minor, row.currency)}</span></div><span class="import-status">${escapeHtml(statusText)}</span>${controls}</article>`;
}

function renderImportBatch(batch) {
  state.currentImportId = batch.id;
  $("#import-review-title").textContent = batch.filename;
  $("#import-review-subtitle").textContent = `${batch.account_name} · ${batch.row_count} statement rows`;
  const count = status => batch.rows.filter(row => row.workflow_status === status).length;
  $("#import-summary").innerHTML = `<div><strong>${count("NEEDS_REVIEW")}</strong><span>Need review</span></div><div><strong>${count("MATCHED")}</strong><span>Already matched</span></div><div><strong>${count("PENDING")}</strong><span>Pending</span></div><div><strong>${count("POSTED")}</strong><span>Posted</span></div>`;
  $("#import-row-list").innerHTML = batch.rows.map(importRowMarkup).join("");
  bindImportRowActions();
}

function bindImportRowActions() {
  $$('[data-post-import]').forEach(button => button.onclick = () => reviewImportRow(button.dataset.postImport, "post"));
  $$('[data-ignore-import]').forEach(button => button.onclick = () => reviewImportRow(button.dataset.ignoreImport, "ignore"));
}

async function reviewImportRow(rowId, action) {
  const select = document.querySelector(`[data-counter-row="${rowId}"]`);
  if (action === "post" && !select?.value) { toast("Choose a counter-account first.", true); return; }
  try {
    await api(`/api/import-rows/${encodeURIComponent(rowId)}/${action}`, { method: "POST", body: JSON.stringify({ category_code: select?.value, reviewed_by: "local-user" }) });
    if (state.currentImportId && $("#import-review-dialog").open) {
      const result = await api(`/api/imports/${encodeURIComponent(state.currentImportId)}`); renderImportBatch(result.import);
    }
    if ($("#review-dialog").open) {
      const review = await api("/api/review"); renderReviewQueue(review.proposals, review.import_rows);
    }
    await loadOverview(); toast(action === "post" ? "Statement row posted to the ledger." : "Statement row ignored.");
  } catch (error) { toast(error.message, true); }
}

async function submitStatementImport(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const file = form.get("statement_file");
  if (!(file instanceof File) || !file.name) { toast("Choose a CSV file.", true); return; }
  if (file.size > 4_000_000) { toast("CSV imports are limited to 4 MB.", true); return; }
  try {
    const result = await api("/api/imports", { method: "POST", body: JSON.stringify({ account_code: form.get("account_code"), filename: file.name, csv_text: await file.text(), amount_convention: form.get("amount_convention") }) });
    $("#statement-import-dialog").close(); renderImportBatch(result.import); $("#import-review-dialog").showModal(); await loadOverview(); toast("Statement staged. Nothing was posted without review.");
  } catch (error) { toast(error.message, true); }
}

function setImportConvention() {
  const account = state.data?.accounts.find(item => item.code === $("#import-account").value);
  $("#amount-convention").value = account?.kind === "CREDIT_CARD" ? "CHARGES_POSITIVE" : "ACCOUNT_SIGNED";
}

function prepareReconciliation(accountCode = null) {
  if (accountCode) $("#reconciliation-account").value = accountCode;
  state.reconciliationPreview = null;
  $("#reconciliation-result").hidden = true;
  $("#complete-reconciliation").disabled = true;
  updateStatementLabel();
  $("#reconciliation-dialog").showModal();
}

function updateStatementLabel() {
  const selected = state.data?.accounts.find(item => item.code === $("#reconciliation-account").value);
  $("#statement-balance-label").textContent = selected?.account_type === "LIABILITY" ? "Statement amount owed" : "Statement ending balance";
}

async function previewReconciliation(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const preview = await api("/api/reconciliations/preview", { method: "POST", body: JSON.stringify({ account_code: form.get("account_code"), statement_date: form.get("statement_date"), statement_balance_minor: toMinor(form.get("statement_balance")) }) });
    state.reconciliationPreview = preview;
    $("#reconciliation-result").hidden = false;
    $("#reconciliation-result").classList.toggle("balanced", preview.balanced);
    $("#reconciliation-result").classList.toggle("unbalanced", !preview.balanced);
    $("#reconcile-statement").textContent = money(preview.statement_balance_minor);
    $("#reconcile-ledger").textContent = money(preview.ledger_balance_minor);
    $("#reconcile-difference").textContent = money(preview.difference_minor);
    $("#reconcile-message").textContent = preview.balanced ? "Balances match. This statement can be completed." : "The difference must be resolved with missing or corrected transactions before completion.";
    $("#complete-reconciliation").disabled = !preview.balanced;
  } catch (error) { toast(error.message, true); }
}

async function completeReconciliation() {
  const preview = state.reconciliationPreview;
  if (!preview?.balanced) return;
  try {
    await api("/api/reconciliations/complete", { method: "POST", body: JSON.stringify({ account_code: preview.account_code, statement_date: preview.statement_date, statement_balance_minor: preview.statement_balance_minor, completed_by: "local-user" }) });
    $("#reconciliation-dialog").close(); await loadOverview(); toast("Statement reconciled exactly to the ledger.");
  } catch (error) { toast(error.message, true); }
}

async function submitAccount(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const kind = form.get("kind");
  try {
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ name: form.get("name"), kind, opening_balance_minor: toMinor(form.get("opening_balance")), credit_limit_minor: kind === "CREDIT_CARD" ? toMinor(form.get("credit_limit")) : null, balance_as_of: form.get("balance_as_of") }) });
    event.currentTarget.closest("dialog").close(); event.currentTarget.reset(); setFormDates(); await loadOverview(); toast("Account added with a balanced opening entry.");
  } catch (error) { toast(error.message, true); }
}

async function submitTransaction(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.amount_minor = toMinor(payload.amount); delete payload.amount;
  payload.request_id = crypto.randomUUID();
  try {
    await api("/api/transactions", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.closest("dialog").close(); event.currentTarget.reset(); setFormDates(); transactionMode(); await loadOverview(); toast("Transaction posted and balances updated.");
  } catch (error) { toast(error.message, true); }
}

async function submitBudget(event) {
  event.preventDefault();
  const allocations = {};
  $$('[data-budget-code]').forEach(input => { if (input.value !== "") allocations[input.dataset.budgetCode] = toMinor(input.value); });
  try {
    await api("/api/budgets", { method: "POST", body: JSON.stringify({ month: $("#budget-month").value, allocations }) });
    event.currentTarget.closest("dialog").close(); await loadOverview(); toast("Monthly budget saved.");
  } catch (error) { toast(error.message, true); }
}

async function submitReversal(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api(`/api/transactions/${encodeURIComponent(state.selectedTransactionId)}/reverse`, { method: "POST", body: JSON.stringify({ reason: form.get("reason"), effective_at: form.get("effective_at") }) });
    $("#transaction-detail-dialog").close(); event.currentTarget.reset(); await loadOverview(); toast("Reversal posted. Original history was preserved.");
  } catch (error) { toast(error.message, true); }
}

function setFormDates() {
  $$('input[type="date"]').forEach(input => input.value = isoDate);
  $("#budget-month").value = $("#month-picker").value || currentMonth;
}

function setLoading(active) { document.body.style.cursor = active ? "progress" : ""; }
let toastTimer;
function toast(message, error = false) {
  clearTimeout(toastTimer); const element = $("#toast"); element.textContent = message; element.style.background = error ? "#8e342b" : ""; element.hidden = false; toastTimer = setTimeout(() => element.hidden = true, 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  const hour = today.getHours(); $("#time-greeting").textContent = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  $("#today-label").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" }).format(today);
  $("#month-picker").value = currentMonth; setFormDates(); bindDialogButtons(); transactionMode();
  $("#month-picker").addEventListener("change", () => { setFormDates(); loadOverview(); });
  $("#account-kind").addEventListener("change", event => { const credit = event.target.value === "CREDIT_CARD"; $("#credit-limit-field").hidden = !credit; $("#balance-label").textContent = credit ? "Current amount owed" : event.target.value === "LOAN" || event.target.value === "CUSTOM_LIABILITY" ? "Current amount owed" : "Available balance"; });
  $$('input[name="kind"]').forEach(input => input.addEventListener("change", transactionMode));
  $("#account-form").addEventListener("submit", submitAccount); $("#transaction-form").addEventListener("submit", submitTransaction); $("#budget-form").addEventListener("submit", submitBudget);
  $("#statement-import-form").addEventListener("submit", submitStatementImport);
  $("#import-account").addEventListener("change", setImportConvention);
  $("#reversal-form").addEventListener("submit", submitReversal);
  $("#reconciliation-form").addEventListener("submit", previewReconciliation);
  $("#complete-reconciliation").addEventListener("click", completeReconciliation);
  $("#reconciliation-account").addEventListener("change", updateStatementLabel);
  $("#start-reconcile").addEventListener("click", () => { $("#account-detail-dialog").close(); prepareReconciliation(state.selectedAccountCode); });
  $("#budget-month").addEventListener("change", async event => { $("#month-picker").value = event.target.value; await loadOverview(); });
  $("#ai-nav").addEventListener("click", () => toast("AI is intentionally disabled. The complete core app works without it."));
  $("#review-nav").addEventListener("click", openReviewQueue);
  loadOverview();
});
