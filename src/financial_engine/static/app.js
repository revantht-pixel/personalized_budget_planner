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
const toMinor = (value) => Math.round(Number.parseFloat(String(value || "0").replace(/[$,\s]/g, "")) * 100);
const monthLabel = (month) => new Intl.DateTimeFormat("en-US", { month: "short", timeZone: "UTC" }).format(new Date(`${month}-01T12:00:00Z`));
const SOURCE_LABELS = { WEB_UI: "Added manually", STATEMENT_IMPORT: "From a bank statement", EDIT: "Edited", PROPOSAL: "From a suggestion", OPENING_BALANCE: "Starting balance", REVERSAL: "Correction" };

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
  renderCashFlow(state.data.cash_flow, currency);
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
    const detail = isCredit ? `${money(account.display_balance_minor, currency)} owed` : `Recorded ${money(account.display_balance_minor, currency)}`;
    const source = account.balance_source === "CALCULATED" ? "" : `Bank-reported`;
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

function renderCashFlow(months, currency) {
  const current = months[months.length - 1];
  $("#cash-in").textContent = money(current.income_minor, currency);
  $("#cash-out").textContent = money(current.spent_minor, currency);
  $("#cash-net").textContent = money(Math.abs(current.net_minor), currency);
  $("#cash-net-label").textContent = current.net_minor < 0 ? "Overspent" : "Left over";
  $(".cash-net").classList.toggle("negative", current.net_minor < 0);
  const rate = current.savings_rate;
  $("#savings-rate").hidden = rate == null;
  $("#savings-rate").textContent = rate == null ? "" : rate >= 0 ? `You kept ${Math.round(rate * 100)}% of your income` : `Spent ${Math.round(-rate * 100)}% more than you earned`;
  $("#savings-rate").classList.toggle("negative", rate != null && rate < 0);
  const max = Math.max(1, ...months.flatMap(item => [item.income_minor, item.spent_minor]));
  $("#cash-flow-chart").innerHTML = months.map(item => {
    const tip = `${monthLabel(item.month)}: in ${money(item.income_minor, currency)}, out ${money(item.spent_minor, currency)}`;
    return `<div class="flow-month${item === current ? " current" : ""}" title="${escapeHtml(tip)}"><div class="flow-bars"><span class="flow-bar in" style="height:${Math.max(0, item.income_minor) / max * 100}%"></span><span class="flow-bar out" style="height:${Math.max(0, item.spent_minor) / max * 100}%"></span></div><small>${monthLabel(item.month)}</small></div>`;
  }).join("");
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
  fillCategoryOptions();
}

function fillCategoryOptions() {
  const kind = new FormData($("#transaction-form")).get("kind");
  const items = (kind === "income" ? state.data?.income_categories : state.data?.expense_categories) || [];
  const select = $("#transaction-category");
  const previous = select.value;
  select.innerHTML = items.map(item => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.name)}</option>`).join("");
  if (items.some(item => item.code === previous)) select.value = previous;
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
    // Start every new entry from a clean form, even if an edit was abandoned.
    if (button.dataset.open === "transaction-dialog" && !dialog.open) resetTransactionForm();
    if (dialog && !dialog.open) dialog.showModal();
  });
  $$('[data-close]').forEach(button => button.onclick = () => button.closest("dialog")?.close());
}

function transactionMode() {
  const kind = new FormData($("#transaction-form")).get("kind");
  const transfer = kind === "transfer";
  $("#transfer-fields").hidden = !transfer;
  $("#account-field").hidden = transfer;
  $("#category-field").hidden = transfer;
  $("#account-field-label").textContent = kind === "income" ? "Deposited to" : "Paid from";
  fillCategoryOptions();
}

let suggestTimer;
function suggestCategory() {
  clearTimeout(suggestTimer);
  const select = $("#transaction-category");
  if (select.dataset.touched) return;
  const text = $("#transaction-description").value.trim();
  suggestTimer = setTimeout(async () => {
    if (!text) { $("#category-hint").hidden = true; return; }
    try {
      const { account_code: code } = await api(`/api/rules/suggest?text=${encodeURIComponent(text)}`);
      const known = code && [...select.options].some(option => option.value === code);
      if (known && !select.dataset.touched) select.value = code;
      $("#category-hint").hidden = !known;
    } catch { $("#category-hint").hidden = true; }
  }, 250);
}

function resetTransactionForm() {
  const form = $("#transaction-form");
  form.reset(); setFormDates();
  $("#editing-id").value = "";
  delete $("#transaction-category").dataset.touched;
  $("#category-hint").hidden = true;
  $("#transaction-eyebrow").textContent = "New";
  $("#transaction-dialog-title").textContent = "Add a transaction";
  $("#transaction-submit").textContent = "Add transaction";
  transactionMode();
}

function openEditTransaction(edit) {
  resetTransactionForm();
  const form = $("#transaction-form");
  form.querySelector(`input[name="kind"][value="${edit.kind}"]`).checked = true;
  transactionMode();
  $("#editing-id").value = state.selectedTransactionId;
  form.elements.description.value = edit.merchant_name || edit.description;
  form.elements.amount.value = (edit.amount_minor / 100).toFixed(2);
  form.elements.effective_at.value = edit.effective_at;
  if (edit.kind === "transfer") {
    form.elements.from_account_code.value = edit.from_account_code;
    form.elements.to_account_code.value = edit.to_account_code;
  } else {
    form.elements.account_code.value = edit.account_code;
    form.elements.category_code.value = edit.category_code;
    $("#transaction-category").dataset.touched = "1";
  }
  $("#transaction-eyebrow").textContent = "Edit";
  $("#transaction-dialog-title").textContent = "Edit transaction";
  $("#transaction-submit").textContent = "Save changes";
  $("#transaction-detail-dialog").close();
  $("#transaction-dialog").showModal();
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
    $("#account-reconcile-status").textContent = latest ? `Matches statement through ${shortDate(`${latest.statement_date}T12:00:00Z`)}` : "Not checked yet";
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
    $("#posting-list").innerHTML = detail.postings.map(posting => {
      const flow = posting.account_type === "EXPENSE" ? "Spent on" : posting.account_type === "INCOME" ? "Earned from" : posting.amount_minor > 0 ? "Into" : "Out of";
      return `<div class="posting-row"><div><strong>${escapeHtml(posting.account_name)}</strong><small>${flow}</small></div><span class="posting-amount">${money(Math.abs(posting.amount_minor), detail.currency)}</span></div>`;
    }).join("");
    const metadata = [SOURCE_LABELS[detail.source_type] || detail.source_type, detail.reversed_by_transaction_id && "Deleted or edited"].filter(Boolean);
    $("#transaction-metadata").innerHTML = metadata.map(item => `<span class="metadata-pill">${escapeHtml(item)}</span>`).join("");
    const locked = Boolean(detail.reversal_of_transaction_id || detail.reversed_by_transaction_id);
    state.selectedTransaction = detail;
    $("#edit-transaction").hidden = locked || !detail.edit;
    $("#delete-transaction").hidden = locked;
    $("#transaction-locked-note").hidden = !locked;
    $("#transaction-locked-note").textContent = detail.reversal_of_transaction_id ? "This entry records a correction, so it can't be changed." : "This transaction was deleted or replaced by an edit.";
    $("#transaction-detail-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function openReviewQueue() {
  const dialog = $("#review-dialog");
  dialog.showModal();
  $("#review-list").innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const result = await api("/api/review");
    renderReviewQueue(result.proposals, result.import_rows);
  } catch (error) { $("#review-list").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
}

function renderReviewQueue(proposals, importRows = []) {
  if (!proposals.length && !importRows.length) {
    $("#review-list").innerHTML = `<div class="empty-state">You're all caught up. Nothing needs review.</div>`;
    return;
  }
  const proposalMarkup = proposals.length ? `<h3 class="review-section-title">Suggested transactions</h3>${proposals.map(item => `<article class="review-card"><div class="review-card-header"><div><h3>${escapeHtml(item.merchant_name || item.description)}</h3><p>${shortDate(item.effective_at)} · ${escapeHtml(item.source_type)}</p></div><span class="confidence">${item.confidence == null ? "Review required" : `${Math.round(item.confidence * 100)}% confidence`}</span></div><div class="proposal-postings">${item.postings.map(posting => `<div class="proposal-posting"><span>${escapeHtml(posting.account_code)}</span><strong>${money(posting.amount_minor, item.currency)}</strong></div>`).join("")}</div><div class="review-actions"><button class="button secondary" type="button" data-reject-proposal="${item.id}">Reject</button><button class="button primary" type="button" data-approve-proposal="${item.id}">Approve</button></div></article>`).join("")}` : "";
  const importMarkup = importRows.length ? `<h3 class="review-section-title">From bank statements</h3>${bulkBar()}${importRows.map(importRowMarkup).join("")}` : "";
  $("#review-list").innerHTML = proposalMarkup + importMarkup;
  bindBulk($("#review-list"));
  $$('[data-approve-proposal]').forEach(button => button.onclick = () => reviewProposal(button.dataset.approveProposal, "approve"));
  $$('[data-reject-proposal]').forEach(button => button.onclick = () => reviewProposal(button.dataset.rejectProposal, "reject"));
  bindImportRowActions();
}

async function reviewProposal(proposalId, action) {
  const note = action === "reject" ? window.prompt("Why are you rejecting this proposal?") : "Approved in review queue";
  if (action === "reject" && !note) return;
  try {
    await api(`/api/proposals/${encodeURIComponent(proposalId)}/${action}`, { method: "POST", body: JSON.stringify({ reviewed_by: "local-user", note }) });
    const result = await api("/api/review"); renderReviewQueue(result.proposals, result.import_rows); await loadOverview(); toast(action === "approve" ? "Suggestion added." : "Suggestion dismissed.");
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
  const placeholder = row.suggested_account_code ? "" : `<option value="" selected>Choose a category</option>`;
  return placeholder + options(expenses, "Spending") + options(incomes, "Income") + options(accounts, "Transfer or card payment");
}

const FALLBACK_CATEGORIES = new Set(["Expenses:Other", "Income:Other"]);
const IMPORT_STATUS_TEXT = { MATCHED: "Already in your records", PENDING: "Pending: waits until it clears", POSTED: "Added", IGNORED: "Skipped", NEEDS_REVIEW: "Needs a category" };

function importRowMarkup(row) {
  const statusClass = row.workflow_status.toLowerCase();
  const amountClass = row.account_amount_minor > 0 ? "inflow" : "";
  const reviewable = row.workflow_status === "NEEDS_REVIEW";
  const suggested = Boolean(row.suggested_account_code) && !FALLBACK_CATEGORIES.has(row.suggested_account_code);
  const checkbox = reviewable ? `<input type="checkbox" class="row-check" data-bulk-row="${escapeHtml(row.id)}" aria-label="Select ${escapeHtml(row.description)}" ${suggested ? "checked" : ""}>` : "";
  const controls = reviewable ? `<div class="import-row-controls"><label>Category<select data-counter-row="${escapeHtml(row.id)}" data-merchant="${escapeHtml(row.merchant_key || "")}">${counterpartOptions(row)}</select></label><button class="button secondary" type="button" data-ignore-import="${escapeHtml(row.id)}">Skip</button><button class="button primary" type="button" data-post-import="${escapeHtml(row.id)}">Add</button></div>` : row.workflow_status === "PENDING" ? `<div class="import-row-controls"><span></span><button class="button secondary" type="button" data-ignore-import="${escapeHtml(row.id)}">Skip</button></div>` : "";
  return `<article class="import-row ${statusClass}"><div class="import-row-head">${checkbox}<div><strong>${escapeHtml(row.description)}</strong><small>${escapeHtml(row.account_name)} · ${escapeHtml(row.effective_date)}</small></div><span class="import-amount ${amountClass}">${row.account_amount_minor > 0 ? "+" : ""}${money(row.account_amount_minor, row.currency)}</span></div><span class="import-status">${escapeHtml(reviewable && suggested ? "Category suggested" : IMPORT_STATUS_TEXT[row.workflow_status] || row.workflow_status)}</span>${controls}</article>`;
}

function bulkBar() {
  return `<div class="bulk-bar"><label class="bulk-select"><input type="checkbox" data-bulk-all> Select all</label><span class="bulk-count" data-bulk-count></span><button class="button secondary" type="button" data-bulk-action="ignore">Skip selected</button><button class="button primary" type="button" data-bulk-action="post">Add selected</button></div>`;
}

function bindBulk(container) {
  const checks = () => [...container.querySelectorAll("[data-bulk-row]")];
  const all = container.querySelector("[data-bulk-all]");
  if (!all) return;
  const update = () => {
    const selected = checks().filter(check => check.checked).length;
    container.querySelector("[data-bulk-count]").textContent = `${selected} of ${checks().length} selected`;
    all.checked = selected > 0 && selected === checks().length;
    all.indeterminate = selected > 0 && selected < checks().length;
    container.querySelectorAll("[data-bulk-action]").forEach(button => button.disabled = selected === 0);
  };
  all.onchange = () => { checks().forEach(check => check.checked = all.checked); update(); };
  checks().forEach(check => check.onchange = update);
  container.querySelectorAll("[data-counter-row]").forEach(select => select.onchange = () => {
    select.dataset.touched = "1";
    container.querySelector(`[data-bulk-row="${CSS.escape(select.dataset.counterRow)}"]`).checked = Boolean(select.value);
    // Categorizing one row of a merchant fills in that merchant's other untouched rows.
    if (select.dataset.merchant) {
      container.querySelectorAll(`[data-counter-row][data-merchant="${CSS.escape(select.dataset.merchant)}"]`).forEach(other => {
        if (other === select || other.dataset.touched) return;
        other.value = select.value;
        container.querySelector(`[data-bulk-row="${CSS.escape(other.dataset.counterRow)}"]`).checked = Boolean(select.value);
      });
    }
    update();
  });
  container.querySelectorAll("[data-bulk-action]").forEach(button => button.onclick = () => submitBulk(container, button.dataset.bulkAction));
  update();
}

async function submitBulk(container, action) {
  const selected = [...container.querySelectorAll("[data-bulk-row]")].filter(check => check.checked).map(check => check.dataset.bulkRow);
  const rows = selected.map(id => ({ id, action, category_code: container.querySelector(`[data-counter-row="${CSS.escape(id)}"]`)?.value || null }));
  const missing = action === "post" ? rows.filter(row => !row.category_code).length : 0;
  if (missing) { toast(`Choose a category for ${missing} selected transaction${missing === 1 ? "" : "s"} first.`, true); return; }
  try {
    const result = await api("/api/import-rows/bulk", { method: "POST", body: JSON.stringify({ rows, reviewed_by: "local-user" }) });
    await refreshReviewViews();
    const done = action === "post" ? `Added ${result.posted} transaction${result.posted === 1 ? "" : "s"}.` : `Skipped ${result.ignored}.`;
    toast(result.failed.length ? `${done} ${result.failed.length} couldn't be saved: ${result.failed[0].error}` : done, result.failed.length > 0);
  } catch (error) { toast(error.message, true); }
}

async function refreshReviewViews() {
  if (state.currentImportId && $("#import-review-dialog").open) {
    const result = await api(`/api/imports/${encodeURIComponent(state.currentImportId)}`); renderImportBatch(result.import);
  }
  if ($("#review-dialog").open) {
    const review = await api("/api/review"); renderReviewQueue(review.proposals, review.import_rows);
  }
  await loadOverview();
}

function renderImportBatch(batch) {
  state.currentImportId = batch.id;
  $("#import-review-title").textContent = batch.filename;
  $("#import-review-subtitle").textContent = `${batch.account_name} · ${batch.row_count} statement rows`;
  const count = status => batch.rows.filter(row => row.workflow_status === status).length;
  $("#import-summary").innerHTML = `<div><strong>${count("NEEDS_REVIEW")}</strong><span>To review</span></div><div><strong>${count("MATCHED")}</strong><span>Already recorded</span></div><div><strong>${count("PENDING")}</strong><span>Pending</span></div><div><strong>${count("POSTED")}</strong><span>Added</span></div>`;
  $("#import-row-list").innerHTML = (count("NEEDS_REVIEW") ? bulkBar() : "") + batch.rows.map(importRowMarkup).join("");
  bindImportRowActions();
  bindBulk($("#import-row-list"));
}

function bindImportRowActions() {
  $$('[data-post-import]').forEach(button => button.onclick = () => reviewImportRow(button.dataset.postImport, "post"));
  $$('[data-ignore-import]').forEach(button => button.onclick = () => reviewImportRow(button.dataset.ignoreImport, "ignore"));
}

async function reviewImportRow(rowId, action) {
  const select = document.querySelector(`[data-counter-row="${rowId}"]`);
  if (action === "post" && !select?.value) { toast("Choose a category first.", true); return; }
  try {
    await api(`/api/import-rows/${encodeURIComponent(rowId)}/${action}`, { method: "POST", body: JSON.stringify({ category_code: select?.value, reviewed_by: "local-user" }) });
    await refreshReviewViews(); toast(action === "post" ? "Transaction added." : "Skipped.");
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
    $("#statement-import-dialog").close(); renderImportBatch(result.import); $("#import-review-dialog").showModal(); await loadOverview(); toast("Statement uploaded. Review the transactions below.");
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
    $("#reconcile-message").textContent = preview.balanced ? "Everything matches. Mark this statement as checked." : "Something's missing or different. Add or fix transactions until the difference is $0.00.";
    $("#complete-reconciliation").disabled = !preview.balanced;
  } catch (error) { toast(error.message, true); }
}

async function completeReconciliation() {
  const preview = state.reconciliationPreview;
  if (!preview?.balanced) return;
  try {
    await api("/api/reconciliations/complete", { method: "POST", body: JSON.stringify({ account_code: preview.account_code, statement_date: preview.statement_date, statement_balance_minor: preview.statement_balance_minor, completed_by: "local-user" }) });
    $("#reconciliation-dialog").close(); await loadOverview(); toast("Statement checked: your records match.");
  } catch (error) { toast(error.message, true); }
}

async function submitAccount(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(event.currentTarget);
  const kind = form.get("kind");
  try {
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ name: form.get("name"), kind, opening_balance_minor: toMinor(form.get("opening_balance")), credit_limit_minor: kind === "CREDIT_CARD" ? toMinor(form.get("credit_limit")) : null, balance_as_of: form.get("balance_as_of") }) });
    formElement.closest("dialog").close(); formElement.reset(); setFormDates(); await loadOverview(); toast("Account added.");
  } catch (error) { toast(error.message, true); }
}

async function submitTransaction(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.amount_minor = toMinor(payload.amount); delete payload.amount;
  const editingId = payload.editing_id; delete payload.editing_id;
  payload.request_id = crypto.randomUUID();
  try {
    const path = editingId ? `/api/transactions/${encodeURIComponent(editingId)}/edit` : "/api/transactions";
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    formElement.closest("dialog").close(); resetTransactionForm(); await loadOverview(); toast(editingId ? "Changes saved." : "Transaction added.");
  } catch (error) { toast(error.message, true); }
}

async function submitBudget(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const allocations = {};
  $$('[data-budget-code]').forEach(input => { if (input.value !== "") allocations[input.dataset.budgetCode] = toMinor(input.value); });
  try {
    await api("/api/budgets", { method: "POST", body: JSON.stringify({ month: $("#budget-month").value, allocations }) });
    formElement.closest("dialog").close(); await loadOverview(); toast("Monthly budget saved.");
  } catch (error) { toast(error.message, true); }
}

async function deleteTransaction() {
  const detail = state.selectedTransaction;
  if (!detail || !window.confirm(`Delete "${detail.merchant_name || detail.description}"? A record of the deletion is kept in your history.`)) return;
  try {
    await api(`/api/transactions/${encodeURIComponent(detail.id)}/reverse`, { method: "POST", body: JSON.stringify({ reason: "Deleted by user", effective_at: detail.effective_at.slice(0, 10) }) });
    $("#transaction-detail-dialog").close(); await loadOverview(); toast("Transaction deleted.");
  } catch (error) { toast(error.message, true); }
}

async function loadRules() {
  const list = $("#rules-list");
  list.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const { rules } = await api("/api/rules");
    list.innerHTML = rules.length ? rules.map(rule => `<div class="rule-row"><div><strong>${escapeHtml(rule.merchant_key)}</strong><small>→ ${escapeHtml(rule.account_name)} · used ${rule.times_used} time${rule.times_used === 1 ? "" : "s"}</small></div><button class="button secondary compact-button" type="button" data-delete-rule="${escapeHtml(rule.id)}">Remove</button></div>`).join("") : `<div class="empty-state">No rules yet. Categorize a few transactions and they'll show up here.</div>`;
    $$("[data-delete-rule]").forEach(button => button.onclick = async () => {
      try { await api(`/api/rules/${encodeURIComponent(button.dataset.deleteRule)}/delete`, { method: "POST", body: "{}" }); await loadRules(); toast("Rule removed."); }
      catch (error) { toast(error.message, true); }
    });
  } catch (error) { list.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
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
  $("#delete-transaction").addEventListener("click", deleteTransaction);
  $("#edit-transaction").addEventListener("click", () => openEditTransaction(state.selectedTransaction.edit));
  $("#transaction-description").addEventListener("input", suggestCategory);
  $("#transaction-category").addEventListener("change", event => { event.target.dataset.touched = "1"; $("#category-hint").hidden = true; });
  $("#rules-nav").addEventListener("click", loadRules);
  $("#reconciliation-form").addEventListener("submit", previewReconciliation);
  $("#complete-reconciliation").addEventListener("click", completeReconciliation);
  $("#reconciliation-account").addEventListener("change", updateStatementLabel);
  $("#start-reconcile").addEventListener("click", () => { $("#account-detail-dialog").close(); prepareReconciliation(state.selectedAccountCode); });
  $("#budget-month").addEventListener("change", async event => { $("#month-picker").value = event.target.value; await loadOverview(); });
  $("#review-nav").addEventListener("click", openReviewQueue);
  loadOverview();
});
