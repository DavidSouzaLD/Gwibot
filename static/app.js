(() => {
    // Search UI Elements
    const searchForm = document.getElementById("searchForm");
    const queryInput = document.getElementById("queryInput");
    const limitInput = document.getElementById("limitInput");
    const submitBtn = document.getElementById("submitBtn");
    const submitBtnText = document.getElementById("submitBtnText");

    const loadingSection = document.getElementById("loadingSection");
    const alertSection = document.getElementById("alertSection");
    const alertTitle = document.getElementById("alertTitle");
    const alertMessage = document.getElementById("alertMessage");

    const resultsWrapper = document.getElementById("resultsWrapper");
    const statQuery = document.getElementById("statQuery");
    const statTotal = document.getElementById("statTotal");
    const statAffiliates = document.getElementById("statAffiliates");
    const statTime = document.getElementById("statTime");

    const gridView = document.getElementById("gridView");
    const tableView = document.getElementById("tableView");
    const tableBody = document.getElementById("tableBody");
    const jsonView = document.getElementById("jsonView");
    const jsonPre = document.getElementById("jsonPre");
    const endpointUrlDisplay = document.getElementById("endpointUrlDisplay");

    const copyJsonBtn = document.getElementById("copyJsonBtn");
    const downloadJsonBtn = document.getElementById("downloadJsonBtn");

    const healthDot = document.getElementById("healthDot");
    const healthText = document.getElementById("healthText");
    const navActiveCyclesCount = document.getElementById("navActiveCyclesCount");

    // Auth UI
    const authDot = document.getElementById("authDot");
    const authStateText = document.getElementById("authStateText");
    const loginBtn = document.getElementById("loginBtn");
    const logoutBtn = document.getElementById("logoutBtn");
    const authFeedbackBanner = document.getElementById("authFeedbackBanner");
    const authFeedbackTitle = document.getElementById("authFeedbackTitle");
    const authFeedbackMessage = document.getElementById("authFeedbackMessage");

    // Affiliate & Outputs UI
    const affiliateConfigForm = document.getElementById("affiliateConfigForm");
    const affProviderIdInput = document.getElementById("affProviderId");
    const affProviderTitle = document.getElementById("affProviderTitle");
    const affProviderDesc = document.getElementById("affProviderDesc");
    const affProviderStatusBadge = document.getElementById("affProviderStatusBadge");
    const affiliateStoreTabs = document.getElementById("affiliateStoreTabs");
    const affMercadoLivreFields = document.getElementById("affMercadoLivreFields");
    const affDynamicFieldsContainer = document.getElementById("affDynamicFieldsContainer");
    const affEnabled = document.getElementById("affEnabled");
    const affMattWord = document.getElementById("affMattWord");
    const affMattTool = document.getElementById("affMattTool");
    const affUseLinkbuilder = document.getElementById("affUseLinkbuilder");
    const affiliateTestForm = document.getElementById("affiliateTestForm");
    const affTestUrlInput = document.getElementById("affTestUrlInput");
    const affTestResultBox = document.getElementById("affTestResultBox");

    const outputDestForm = document.getElementById("outputDestForm");
    const editingDestIdInput = document.getElementById("editingDestId");
    const destFormTitle = document.getElementById("destFormTitle");
    const destSubmitBtn = document.getElementById("destSubmitBtn");
    const cancelDestEditBtn = document.getElementById("cancelDestEditBtn");
    const destName = document.getElementById("destName");
    const destType = document.getElementById("destType");
    const destEnabled = document.getElementById("destEnabled");
    const telegramFieldsGroup = document.getElementById("telegramFieldsGroup");
    const tgBotToken = document.getElementById("tgBotToken");
    const verifyTgTokenBtn = document.getElementById("verifyTgTokenBtn");
    const tgTokenVerifyFeedback = document.getElementById("tgTokenVerifyFeedback");
    const tgChatId = document.getElementById("tgChatId");
    const destinationsListContainer = document.getElementById("destinationsListContainer");

    // Platform Templates UI
    const platformTemplateForm = document.getElementById("platformTemplateForm");
    const templatePlatformTabs = document.getElementById("templatePlatformTabs");
    const templatePlatformInput = document.getElementById("templatePlatformInput");
    const platformTemplateInput = document.getElementById("platformTemplateInput");
    const templateCharCounter = document.getElementById("templateCharCounter");
    const templateValidationStatus = document.getElementById("templateValidationStatus");
    const templateLivePreviewBox = document.getElementById("templateLivePreviewBox");
    const resetTemplateBtn = document.getElementById("resetTemplateBtn");

    let loadedProvidersMap = new Map();
    let loadedDestinationsMap = new Map();

    // Cycles UI
    const cycleForm = document.getElementById("cycleForm");
    const cycleFormTitle = document.getElementById("cycleFormTitle");
    const editingCycleIdInput = document.getElementById("editingCycleId");
    const cycleQueryInput = document.getElementById("cycleQuery");
    const cycleQueryTags = document.getElementById("cycleQueryTags");
    const cycleSubmitBtn = document.getElementById("cycleSubmitBtn");
    const cancelCycleEditBtn = document.getElementById("cancelCycleEditBtn");
    const cycleDestinationsCheckboxes = document.getElementById("cycleDestinationsCheckboxes");
    const cyclesListContainer = document.getElementById("cyclesListContainer");
    const refreshCyclesBtn = document.getElementById("refreshCyclesBtn");

    let loadedCyclesMap = new Map();

    // History UI
    const runsHistoryBody = document.getElementById("runsHistoryBody");
    const offersHistoryBody = document.getElementById("offersHistoryBody");
    const refreshHistoryBtn = document.getElementById("refreshHistoryBtn");

    // Navigation breadcrumb
    const crumbCurrentTitle = document.getElementById("crumbCurrentTitle");
    const sidebarViewsGroup = document.getElementById("sidebarViewsGroup");

    let currentPayload = null;
    let authPollTimer = null;

    const brlFormatter = new Intl.NumberFormat("pt-BR", {
        style: "currency",
        currency: "BRL",
    });

    function formatCurrency(val) {
        if (val === null || val === undefined) return null;
        return brlFormatter.format(val);
    }

    function formatDateShort(iso) {
        if (!iso) return "—";
        try {
            return new Date(iso).toLocaleString("pt-BR");
        } catch {
            return iso;
        }
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) return "";
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    const sectionTitles = {
        "sec-search": "Pesquisa & Extração",
        "sec-cycles": "Ciclos Automatizados",
        "sec-affiliates": "Fontes de Afiliado",
        "sec-config": "Destinos de Publicação",
        "sec-history": "Histórico & Métricas",
    };

    // Sidebar navigation
    document.querySelectorAll(".nav-pill").forEach((pill) => {
        pill.addEventListener("click", () => {
            document.querySelectorAll(".nav-pill").forEach((p) => p.classList.remove("active"));
            pill.classList.add("active");
            const targetId = pill.getAttribute("data-section");

            if (crumbCurrentTitle && sectionTitles[targetId]) {
                crumbCurrentTitle.textContent = sectionTitles[targetId];
            }

            // Toggle views group in sidebar only for search
            if (sidebarViewsGroup) {
                sidebarViewsGroup.classList.toggle("hidden", targetId !== "sec-search");
            }

            document.querySelectorAll(".app-section").forEach((sec) => {
                sec.classList.toggle("hidden", sec.id !== targetId);
            });

            if (targetId === "sec-cycles") {
                loadDestinationsAndCycles();
            } else if (targetId === "sec-affiliates") {
                loadAffiliateConfig();
            } else if (targetId === "sec-config") {
                loadDestinationsAndCycles();
                loadPlatformTemplate(templatePlatformInput ? templatePlatformInput.value : "telegram");
            } else if (targetId === "sec-history") {
                loadHistory();
            }
        });
    });

    async function checkHealth() {
        try {
            const res = await fetch("/api/health");
            if (!res.ok) throw new Error("Status " + res.status);
            const data = await res.json();
            healthDot.className = "dot online";
            healthText.textContent = `Online (${data.browser_channel || "chromium"})`;
            if (navActiveCyclesCount) {
                navActiveCyclesCount.textContent = String(data.active_cycles ?? 0);
            }
        } catch {
            healthDot.className = "dot offline";
            healthText.textContent = "Offline";
        }
    }

    function showAuthFeedback(title, message, variant = "info", autoHideMs = 0) {
        authFeedbackTitle.textContent = title;
        authFeedbackMessage.textContent = " " + message;
        authFeedbackBanner.className = `banner-notice ${variant}`;
        authFeedbackBanner.classList.remove("hidden");

        if (autoHideMs > 0) {
            setTimeout(() => {
                authFeedbackBanner.classList.add("hidden");
            }, autoHideMs);
        }
    }

    function applyAuthStatusUI(statusData) {
        const state = statusData.state || "logged_out";
        const msg = statusData.message || "";

        if (state === "logged_in") {
            authDot.className = "dot online";
            authStateText.textContent = "Conectado";
            loginBtn.disabled = false;
            loginBtn.textContent = "Reautenticar";
            logoutBtn.disabled = false;
            stopAuthPolling();
        } else if (state === "logging_in") {
            authDot.className = "dot pending";
            authStateText.textContent = "Aguardando...";
            loginBtn.disabled = true;
            loginBtn.textContent = "No Navegador...";
            logoutBtn.disabled = true;
            showAuthFeedback("Autenticação em andamento:", msg, "info");
            startAuthPolling();
        } else if (state === "expired") {
            authDot.className = "dot expired";
            authStateText.textContent = "Expirada";
            loginBtn.disabled = false;
            loginBtn.textContent = "Autenticar";
            logoutBtn.disabled = !statusData.has_saved_session;
            showAuthFeedback("Sessão Expirada:", msg, "warning");
            stopAuthPolling();
        } else if (state === "error") {
            authDot.className = "dot offline";
            authStateText.textContent = "Erro";
            loginBtn.disabled = false;
            loginBtn.textContent = "Autenticar";
            logoutBtn.disabled = !statusData.has_saved_session;
            showAuthFeedback("Falha na Sessão:", msg, "error");
            stopAuthPolling();
        } else {
            authDot.className = "dot offline";
            authStateText.textContent = "Desconectado";
            loginBtn.disabled = false;
            loginBtn.textContent = "Autenticar";
            logoutBtn.disabled = true;
            stopAuthPolling();
        }
    }

    async function fetchAuthStatus() {
        try {
            const res = await fetch("/api/auth/status");
            if (!res.ok) return null;
            const data = await res.json();
            const previousState = authStateText.dataset.lastState;
            authStateText.dataset.lastState = data.state;
            applyAuthStatusUI(data);

            if (previousState === "logging_in" && data.state === "logged_in") {
                showAuthFeedback(
                    "Autenticado com sucesso:",
                    "Sessão persistente do Mercado Livre pronta para extrações.",
                    "success",
                    5000
                );
            }
            return data;
        } catch {
            return null;
        }
    }

    function startAuthPolling() {
        if (authPollTimer) return;
        authPollTimer = setInterval(fetchAuthStatus, 2000);
    }

    function stopAuthPolling() {
        if (authPollTimer) {
            clearInterval(authPollTimer);
            authPollTimer = null;
        }
    }

    loginBtn.addEventListener("click", async () => {
        loginBtn.disabled = true;
        try {
            const res = await fetch("/api/auth/login", { method: "POST" });
            const data = await res.json();
            authStateText.dataset.lastState = data.state;
            applyAuthStatusUI(data);
        } catch {
            showAuthFeedback("Erro:", "Falha ao iniciar janela de autenticação no servidor.", "error");
            loginBtn.disabled = false;
        }
    });

    logoutBtn.addEventListener("click", async () => {
        logoutBtn.disabled = true;
        try {
            const res = await fetch("/api/auth/logout", { method: "POST" });
            const data = await res.json();
            if (!res.ok) {
                showAuthFeedback("Aviso:", data.detail || "Não foi possível encerrar a sessão.", "warning");
                return;
            }
            authStateText.dataset.lastState = data.state;
            applyAuthStatusUI(data);
            showAuthFeedback("Sessão Encerrada:", data.message, "info", 4000);
        } catch {
            showAuthFeedback("Erro:", "Falha ao comunicar logout ao servidor.", "error");
        }
    });

    function showAlert(title, message, isError = false) {
        alertTitle.textContent = title;
        alertMessage.textContent = " " + message;
        alertSection.className = isError ? "banner-notice error" : "banner-notice warning";
        alertSection.classList.remove("hidden");
    }

    function hideAlert() {
        alertSection.classList.add("hidden");
    }

    // =====================================================================
    // Search & Results Rendering (Linear Style)
    // =====================================================================

    function renderResults(data, endpointUrl) {
        currentPayload = data;
        const meta = data.metadata || {};
        const items = Array.isArray(data.results) ? data.results : [];

        statQuery.textContent = meta.query || "—";
        statTotal.textContent = `${meta.total_returned ?? items.length} / ${meta.requested_limit ?? items.length}`;
        statAffiliates.textContent = `${meta.affiliate_converted_count ?? 0} gerados (${Math.round(meta.affiliate_ms ?? 0)}ms)`;
        const stageBreakdown = meta.navigation_ms || meta.extraction_ms
            ? ` (Nav ${Math.round(meta.navigation_ms ?? 0)}ms · Extr ${Math.round(meta.extraction_ms ?? 0)}ms${meta.cache_hit ? " · CACHE" : ""})`
            : "";
        statTime.textContent = `${meta.pages_visited ?? 0}p / ${meta.elapsed_seconds ?? 0}s${stageBreakdown}`;

        if (meta.session_expired) fetchAuthStatus();

        if (meta.warning) {
            showAlert(
                meta.blocked_by_challenge ? "Bloqueio / Verificação do Site:" : "Aviso:",
                meta.warning,
                Boolean(meta.blocked_by_challenge)
            );
        } else if (items.length === 0) {
            showAlert("Nenhum item retornado para a busca informada.", "", false);
        } else {
            hideAlert();
        }

        // Render Linear Cards
        gridView.innerHTML = items
            .map((item) => {
                const idBadge = item.id
                    ? `<span class="badge-id">${escapeHtml(item.id)}</span>`
                    : `<span class="badge-id">null</span>`;

                const discountBadge =
                    item.discount_percentage !== null && item.discount_percentage !== undefined
                        ? `<span class="badge-discount">-${escapeHtml(item.discount_percentage)}%</span>`
                        : "";

                const imgHtml = item.image_url
                    ? `<img src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.name || "Produto")}" loading="lazy" />`
                    : `<span class="null-pill">Sem imagem</span>`;

                const origPriceHtml =
                    item.original_price !== null && item.original_price !== undefined
                        ? `<span class="price-original">${formatCurrency(item.original_price)}</span>`
                        : "";

                const promoPriceHtml =
                    item.promotional_price !== null && item.promotional_price !== undefined
                        ? `<span class="price-promo">${formatCurrency(item.promotional_price)}</span>`
                        : `<span class="price-promo null-pill">null</span>`;

                const ratingStr =
                    item.rating !== null && item.rating !== undefined
                        ? `<span class="product-rating"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>${item.rating}</span>`
                        : `<span class="null-pill">—</span>`;

                const affPill = item.affiliate_url
                    ? `<a href="${escapeHtml(item.affiliate_url)}" target="_blank" rel="noopener noreferrer" class="status-pill generated">Afiliado ↗</a>`
                    : `<span class="status-pill ${escapeHtml(item.affiliate_status || "not_configured")}">${escapeHtml(item.affiliate_status || "null")}</span>`;

                const targetHref = item.affiliate_url || item.url;
                const titleHtml = targetHref
                    ? `<a class="product-title" href="${escapeHtml(targetHref)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.name || "Sem título")}</a>`
                    : `<span class="product-title">${escapeHtml(item.name || "null")}</span>`;

                return `
                <article class="product-card">
                    <div class="product-thumb-wrap">
                        ${imgHtml}
                    </div>
                    <div class="product-info-wrap">
                        <div>
                            <div class="product-header-line">
                                ${idBadge}
                                ${discountBadge}
                            </div>
                            ${titleHtml}
                            <div class="price-row">
                                ${promoPriceHtml}
                                ${origPriceHtml}
                            </div>
                        </div>
                        <div class="product-meta-row">
                            ${ratingStr}
                            <div>${affPill}</div>
                        </div>
                    </div>
                </article>`;
            })
            .join("");

        // Render Linear Table
        tableBody.innerHTML = items
            .map((item) => {
                const affStatusHtml = `<span class="status-pill ${escapeHtml(item.affiliate_status || "not_configured")}">${escapeHtml(item.affiliate_status || "null")}</span>`;
                const linksHtml = [
                    item.url ? `<a class="table-product-link" href="${escapeHtml(item.url)}" target="_blank">Orig ↗</a>` : "",
                    item.affiliate_url ? `<a class="table-product-link" href="${escapeHtml(item.affiliate_url)}" target="_blank"><strong>Afiliado ↗</strong></a>` : "",
                ]
                    .filter(Boolean)
                    .join(" · ") || '<span class="null-pill">—</span>';

                return `
                <tr>
                    <td><code>${item.id ? escapeHtml(item.id) : '<span class="null-pill">null</span>'}</code></td>
                    <td style="max-width:320px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                        ${item.url ? `<a class="table-product-link" href="${escapeHtml(item.url)}" target="_blank">${escapeHtml(item.name || "null")}</a>` : escapeHtml(item.name || "null")}
                    </td>
                    <td>${item.original_price !== null ? formatCurrency(item.original_price) : '<span class="null-pill">—</span>'}</td>
                    <td><strong>${item.promotional_price !== null ? formatCurrency(item.promotional_price) : '<span class="null-pill">null</span>'}</strong></td>
                    <td>${item.discount_percentage !== null ? `<span class="badge-discount">-${item.discount_percentage}%</span>` : '<span class="null-pill">—</span>'}</td>
                    <td>${item.rating !== null ? `★ ${item.rating} (${item.reviews_count ?? "0"})` : '<span class="null-pill">—</span>'}</td>
                    <td>${affStatusHtml}</td>
                    <td>${linksHtml}</td>
                </tr>`;
            })
            .join("");

        endpointUrlDisplay.textContent = endpointUrl;
        jsonPre.textContent = JSON.stringify(data, null, 2);
        resultsWrapper.classList.remove("hidden");
    }

    function setView(viewName) {
        document.querySelectorAll(".btn-tab").forEach((b) => {
            b.classList.toggle("active", b.getAttribute("data-view") === viewName);
        });
        document.querySelectorAll(".sidebar-view-tab").forEach((b) => {
            b.classList.toggle("active", b.getAttribute("data-target-view") === viewName);
        });

        gridView.classList.toggle("hidden", viewName !== "grid");
        tableView.classList.toggle("hidden", viewName !== "table");
        jsonView.classList.toggle("hidden", viewName !== "json");
    }

    document.querySelectorAll(".btn-tab").forEach((btn) => {
        btn.addEventListener("click", () => setView(btn.getAttribute("data-view")));
    });

    document.querySelectorAll(".sidebar-view-tab").forEach((btn) => {
        btn.addEventListener("click", () => setView(btn.getAttribute("data-target-view")));
    });

    document.querySelectorAll(".chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            queryInput.value = chip.getAttribute("data-query") || "";
            queryInput.focus();
        });
    });

    // Keyboard Shortcuts: '/' or 'Ctrl+K'
    window.addEventListener("keydown", (e) => {
        if (
            (e.key === "/" &&
                document.activeElement !== queryInput &&
                document.activeElement.tagName !== "INPUT" &&
                document.activeElement.tagName !== "TEXTAREA") ||
            ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k")
        ) {
            e.preventDefault();
            // Switch to search view if not active
            const searchPill = document.getElementById("pillSearch");
            if (searchPill && !searchPill.classList.contains("active")) {
                searchPill.click();
            }
            queryInput.focus();
            queryInput.select();
        }
    });

    copyJsonBtn.addEventListener("click", async () => {
        if (!currentPayload) return;
        await navigator.clipboard.writeText(JSON.stringify(currentPayload, null, 2));
        const original = copyJsonBtn.innerHTML;
        copyJsonBtn.innerHTML = `<span>Copiado!</span>`;
        setTimeout(() => { copyJsonBtn.innerHTML = original; }, 1800);
    });

    downloadJsonBtn.addEventListener("click", () => {
        if (!currentPayload) return;
        const querySlug = (currentPayload.metadata?.query || "mercadolivre")
            .toLowerCase()
            .replace(/[^a-z0-9-_]+/gi, "-");
        const blob = new Blob([JSON.stringify(currentPayload, null, 2)], {
            type: "application/json;charset=utf-8",
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `gwibot-${querySlug}-${Date.now()}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    });

    searchForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const query = queryInput.value.trim();
        const limit = parseInt(limitInput.value, 10) || 20;
        if (!query) return;

        hideAlert();
        submitBtn.disabled = true;
        submitBtnText.textContent = "Buscando...";
        loadingSection.classList.remove("hidden");

        const endpointUrl = `/api/search?query=${encodeURIComponent(query)}&limit=${encodeURIComponent(limit)}`;
        try {
            const response = await fetch(endpointUrl);
            const data = await response.json();
            if (!response.ok) {
                showAlert("Erro:", data.message || data.detail || `HTTP ${response.status}`, true);
                return;
            }
            renderResults(data, endpointUrl);
        } catch {
            showAlert("Falha de conexão:", "Não foi possível comunicar com o servidor local.", true);
        } finally {
            loadingSection.classList.add("hidden");
            submitBtn.disabled = false;
            submitBtnText.textContent = "Buscar";
        }
    });

    // =====================================================================
    // Affiliate Configuration (Multi-Store Extensible) & Tester
    // =====================================================================

    async function loadAffiliateConfig(selectedProviderId) {
        const activeProvider = selectedProviderId || (affProviderIdInput ? affProviderIdInput.value : "mercadolivre");
        try {
            const provRes = await fetch("/api/affiliates/providers");
            if (provRes.ok) {
                const providers = await provRes.json();
                loadedProvidersMap = new Map(providers.map((p) => [p.provider_id, p]));
            }
        } catch {
            // ignore
        }

        const providerObj = loadedProvidersMap.get(activeProvider);
        if (providerObj) {
            renderAffiliateProviderForm(providerObj);
            return;
        }

        // Fallback direto para /api/affiliates/config
        try {
            const res = await fetch(`/api/affiliates/config?provider_id=${encodeURIComponent(activeProvider)}`);
            if (!res.ok) return;
            const cfg = await res.json();
            affEnabled.checked = Boolean(cfg.enabled);
            affMattWord.value = cfg.matt_word || "";
            affMattTool.value = cfg.matt_tool || "";
            affUseLinkbuilder.checked = Boolean(cfg.use_authenticated_linkbuilder);
        } catch {
            // ignore
        }
    }

    function renderAffiliateProviderForm(providerObj) {
        const pid = providerObj.provider_id;
        const cfg = providerObj.config || {};
        const creds = cfg.credentials || {};

        if (affProviderIdInput) affProviderIdInput.value = pid;
        if (affProviderTitle) affProviderTitle.textContent = `Fonte de Afiliado — ${providerObj.display_name}`;
        if (affProviderDesc) affProviderDesc.textContent = providerObj.description || "";
        if (affProviderStatusBadge) {
            affProviderStatusBadge.className = `status-pill ${providerObj.supported ? "running" : "paused"}`;
            affProviderStatusBadge.textContent = providerObj.supported ? "Ativo" : "Futuro";
        }

        if (affiliateStoreTabs) {
            affiliateStoreTabs.querySelectorAll("[data-provider-tab]").forEach((btn) => {
                btn.classList.toggle("active", btn.getAttribute("data-provider-tab") === pid);
            });
        }

        affEnabled.checked = Boolean(cfg.enabled);

        if (pid === "mercadolivre") {
            if (affMercadoLivreFields) affMercadoLivreFields.classList.remove("hidden");
            if (affDynamicFieldsContainer) {
                affDynamicFieldsContainer.classList.add("hidden");
                affDynamicFieldsContainer.innerHTML = "";
            }
            affMattWord.value = cfg.matt_word || creds.matt_word || "";
            affMattTool.value = cfg.matt_tool || creds.matt_tool || "";
            affUseLinkbuilder.checked = Boolean(cfg.use_authenticated_linkbuilder ?? creds.use_authenticated_linkbuilder);
        } else {
            if (affMercadoLivreFields) affMercadoLivreFields.classList.add("hidden");
            if (affDynamicFieldsContainer) {
                affDynamicFieldsContainer.classList.remove("hidden");
                const schema = providerObj.fields_schema || [];
                affDynamicFieldsContainer.innerHTML = schema
                    .map((field) => {
                        const val = creds[field.key] !== undefined ? creds[field.key] : "";
                        return `
                        <div class="field-group">
                            <label>${escapeHtml(field.label)}</label>
                            <input
                                type="${escapeHtml(field.type || "text")}"
                                class="linear-input"
                                data-aff-cred-key="${escapeHtml(field.key)}"
                                placeholder="${escapeHtml(field.placeholder || "")}"
                                value="${escapeHtml(val)}"
                            />
                        </div>`;
                    })
                    .join("");
            }
        }
    }

    if (affiliateStoreTabs) {
        affiliateStoreTabs.querySelectorAll("[data-provider-tab]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const pid = btn.getAttribute("data-provider-tab");
                loadAffiliateConfig(pid);
            });
        });
    }

    affiliateConfigForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const pid = affProviderIdInput ? affProviderIdInput.value : "mercadolivre";
        try {
            const credentials = {};
            if (pid === "mercadolivre") {
                credentials.matt_word = affMattWord.value.trim();
                credentials.matt_tool = affMattTool.value.trim();
                credentials.use_authenticated_linkbuilder = affUseLinkbuilder.checked;
            } else if (affDynamicFieldsContainer) {
                affDynamicFieldsContainer.querySelectorAll("[data-aff-cred-key]").forEach((inp) => {
                    credentials[inp.getAttribute("data-aff-cred-key")] = inp.value.trim();
                });
            }

            const payload = {
                provider_id: pid,
                enabled: affEnabled.checked,
                matt_word: pid === "mercadolivre" ? affMattWord.value.trim() : "",
                matt_tool: pid === "mercadolivre" ? affMattTool.value.trim() : "",
                use_authenticated_linkbuilder: pid === "mercadolivre" ? affUseLinkbuilder.checked : false,
                credentials,
            };
            const res = await fetch("/api/affiliates/config", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) {
                showAuthFeedback("Erro Afiliado:", data.detail || "Falha ao salvar configuração.", "error");
                return;
            }
            showAuthFeedback(
                "Fonte de Afiliado Salva:",
                pid === "mercadolivre"
                    ? `Mercado Livre configurado (matt_word='${data.matt_word}', matt_tool='${data.matt_tool}').`
                    : `Configuração da fonte '${pid}' salva com sucesso.`,
                "success",
                5000
            );
            loadAffiliateConfig(pid);
        } catch {
            showAuthFeedback("Erro:", "Falha ao salvar configuração de afiliado.", "error");
        }
    });

    affiliateTestForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const url = affTestUrlInput.value.trim();
        if (!url) return;
        const pid = affProviderIdInput ? affProviderIdInput.value : "mercadolivre";
        const res = await fetch("/api/affiliates/convert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, provider_id: pid }),
        });
        const data = await res.json();
        affTestResultBox.classList.remove("hidden");
        affTestResultBox.textContent = JSON.stringify(data, null, 2);
    });

    // =====================================================================
    // Output Destinations (Create & Edit) & Platform Templates
    // =====================================================================

    function updateVerifyTokenButtonState() {
        if (!verifyTgTokenBtn) return;
        const isTelegram = destType && destType.value === "telegram";
        const hasToken = tgBotToken && tgBotToken.value.trim().length > 0;
        const shouldEnable = isTelegram && hasToken;
        verifyTgTokenBtn.disabled = !shouldEnable;
        if (!isTelegram) {
            hideTgTokenFeedback();
        }
    }

    function showTgTokenFeedback(msg, isSuccess) {
        if (!tgTokenVerifyFeedback) return;
        tgTokenVerifyFeedback.classList.remove("hidden");
        tgTokenVerifyFeedback.textContent = msg;
        tgTokenVerifyFeedback.style.color = isSuccess ? "var(--success)" : "var(--danger)";
    }

    function hideTgTokenFeedback() {
        if (!tgTokenVerifyFeedback) return;
        tgTokenVerifyFeedback.classList.add("hidden");
        tgTokenVerifyFeedback.textContent = "";
    }

    if (tgBotToken) {
        tgBotToken.addEventListener("input", () => {
            updateVerifyTokenButtonState();
            hideTgTokenFeedback();
        });
    }

    if (verifyTgTokenBtn) {
        verifyTgTokenBtn.addEventListener("click", async () => {
            const rawToken = tgBotToken ? tgBotToken.value.trim() : "";
            if (!rawToken) return;

            verifyTgTokenBtn.disabled = true;
            const originalText = verifyTgTokenBtn.textContent;
            verifyTgTokenBtn.textContent = "Verificando...";
            hideTgTokenFeedback();

            try {
                const res = await fetch("/api/outputs/telegram/verify-token", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ bot_token: rawToken }),
                });
                const data = await res.json();
                if (data.valid) {
                    const extra = data.sample_chat_titles && data.sample_chat_titles.length > 0
                        ? ` (Chats encontrados: ${data.sample_chat_titles.join(", ")})`
                        : "";
                    showTgTokenFeedback(`✓ ${data.message}${extra}`, true);
                    showAuthFeedback("Token Telegram Válido:", data.message, "success", 5000);
                } else {
                    showTgTokenFeedback(`✕ ${data.message}`, false);
                    showAuthFeedback("Token Telegram Inválido:", data.message, "error", 6000);
                }
            } catch {
                showTgTokenFeedback("✕ Erro de conexão ao tentar validar o token.", false);
                showAuthFeedback("Erro de Conexão:", "Não foi possível contatar o serviço de validação.", "error");
            } finally {
                verifyTgTokenBtn.textContent = originalText;
                updateVerifyTokenButtonState();
            }
        });
    }

    destType.addEventListener("change", () => {
        telegramFieldsGroup.classList.toggle("hidden", destType.value !== "telegram");
        updateVerifyTokenButtonState();
    });

    function resetDestFormState() {
        outputDestForm.reset();
        if (editingDestIdInput) editingDestIdInput.value = "";
        if (destFormTitle) destFormTitle.textContent = "Destinos de Publicação (Output Sources)";
        if (destSubmitBtn) destSubmitBtn.textContent = "Adicionar Destino de Saída";
        if (cancelDestEditBtn) cancelDestEditBtn.classList.add("hidden");
        if (destEnabled) destEnabled.checked = true;
        telegramFieldsGroup.classList.toggle("hidden", destType.value !== "telegram");
        updateVerifyTokenButtonState();
        hideTgTokenFeedback();
        destinationsListContainer.querySelectorAll(".item-card.editing").forEach((el) => el.classList.remove("editing"));
    }

    function enterDestEditMode(dest) {
        if (!dest) return;
        if (editingDestIdInput) editingDestIdInput.value = dest.id;
        if (destFormTitle) destFormTitle.textContent = `Editando Destino: ${dest.name}`;
        if (destSubmitBtn) destSubmitBtn.textContent = "Salvar Alterações do Destino";
        if (cancelDestEditBtn) cancelDestEditBtn.classList.remove("hidden");

        destName.value = dest.name || "";
        destType.value = dest.output_type || "telegram";
        if (destEnabled) destEnabled.checked = dest.enabled !== undefined ? Boolean(dest.enabled) : true;
        telegramFieldsGroup.classList.toggle("hidden", destType.value !== "telegram");

        if (dest.output_type === "telegram") {
            tgBotToken.value = dest.config?.bot_token || "";
            tgChatId.value = dest.config?.chat_id || "";
        } else {
            tgBotToken.value = "";
            tgChatId.value = "";
        }

        updateVerifyTokenButtonState();
        hideTgTokenFeedback();

        destinationsListContainer.querySelectorAll(".item-card").forEach((el) => {
            el.classList.toggle("editing", el.getAttribute("data-card-dest-id") === dest.id);
        });
        outputDestForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    if (cancelDestEditBtn) {
        cancelDestEditBtn.addEventListener("click", () => {
            resetDestFormState();
        });
    }

    outputDestForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const editingId = editingDestIdInput ? editingDestIdInput.value.trim() : "";
        const type = destType.value;
        const config = type === "telegram"
            ? { bot_token: tgBotToken.value.trim(), chat_id: tgChatId.value.trim() }
            : {};

        const endpoint = editingId ? `/api/outputs/${encodeURIComponent(editingId)}` : "/api/outputs";
        const method = editingId ? "PUT" : "POST";

        const res = await fetch(endpoint, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: destName.value.trim(),
                output_type: type,
                enabled: destEnabled ? destEnabled.checked : true,
                config,
            }),
        });
        const data = await res.json();
        if (!res.ok) {
            const errMsg = extractApiErrorMessage(data, "Configuração inválida.");
            showAuthFeedback("Erro no Destino:", errMsg, "error");
            return;
        }
        resetDestFormState();
        showAuthFeedback(
            editingId ? "Destino Atualizado:" : "Destino Salvo:",
            editingId
                ? `Destino '${data.name}' atualizado com sucesso.`
                : `Destino '${data.name}' cadastrado com sucesso.`,
            "success",
            4000
        );
        loadDestinationsAndCycles();
    });

    async function loadDestinationsAndCycles() {
        try {
            const [destRes, cyclesRes] = await Promise.all([
                fetch("/api/outputs"),
                fetch("/api/cycles"),
            ]);
            const destinations = destRes.ok ? await destRes.json() : [];
            const cycles = cyclesRes.ok ? await cyclesRes.json() : [];

            renderDestinationsList(destinations);
            renderCycleDestinationsCheckboxes(destinations);
            renderCyclesList(cycles);
            checkHealth();
        } catch {
            // ignore
        }
    }

    function renderDestinationsList(destinations) {
        loadedDestinationsMap = new Map((destinations || []).map((d) => [d.id, d]));
        if (!destinations.length) {
            destinationsListContainer.innerHTML = `<span class="null-pill">Nenhum destino cadastrado ainda.</span>`;
            return;
        }
        const editingId = editingDestIdInput ? editingDestIdInput.value : "";

        destinationsListContainer.innerHTML = destinations
            .map((d) => {
                const chatInfo = d.config?.chat_id ? `Chat: <code>${escapeHtml(d.config.chat_id)}</code>` : "Integração futura";
                const isEditingClass = editingId === d.id ? " editing" : "";
                const stateLabel = d.enabled ? d.output_type : `${d.output_type} (inativo)`;
                return `
                <div class="item-card${isEditingClass}" data-card-dest-id="${escapeHtml(d.id)}">
                    <div class="item-card-header">
                        <strong>${escapeHtml(d.name)}</strong>
                        <span class="status-pill ${d.enabled && d.output_type === "telegram" ? "running" : "paused"}">${escapeHtml(stateLabel)}</span>
                    </div>
                    <div style="font-size: 11px; color: var(--text-muted);">${chatInfo}</div>
                    <div class="item-actions">
                        <button type="button" class="btn btn-sm btn-secondary" data-test-dest="${escapeHtml(d.id)}">Disparar Teste</button>
                        <button type="button" class="btn btn-sm btn-secondary" data-edit-dest="${escapeHtml(d.id)}">Editar</button>
                        <button type="button" class="btn btn-sm btn-danger" data-del-dest="${escapeHtml(d.id)}">Remover</button>
                    </div>
                </div>`;
            })
            .join("");

        destinationsListContainer.querySelectorAll("[data-test-dest]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const id = btn.getAttribute("data-test-dest");
                btn.disabled = true;
                btn.textContent = "Enviando...";
                try {
                    const res = await fetch(`/api/outputs/${id}/test`, { method: "POST" });
                    const data = await res.json();
                    if (data.success) {
                        showAuthFeedback("Teste Telegram OK:", `Mensagem enviada para '${data.destination_name}'!`, "success", 5000);
                    } else {
                        showAuthFeedback("Falha no Teste:", data.error || "Erro ao enviar.", "error");
                    }
                } finally {
                    btn.disabled = false;
                    btn.textContent = "Disparar Teste";
                }
            });
        });

        destinationsListContainer.querySelectorAll("[data-edit-dest]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-edit-dest");
                const dest = loadedDestinationsMap.get(id);
                if (dest) {
                    enterDestEditMode(dest);
                }
            });
        });

        destinationsListContainer.querySelectorAll("[data-del-dest]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const id = btn.getAttribute("data-del-dest");
                if (editingDestIdInput && editingDestIdInput.value === id) {
                    resetDestFormState();
                }
                await fetch(`/api/outputs/${id}`, { method: "DELETE" });
                loadDestinationsAndCycles();
            });
        });
    }

    // =====================================================================
    // Platform Message Template Editor & Live Preview
    // =====================================================================

    const samplePreviewData = {
        titulo: "Notebook Gamer Acer Nitro V15 RTX 4050 16GB SSD 512GB",
        preco: "R$ 4.199,90",
        preco_original: "R$ 5.499,90",
        desconto: "24% OFF",
        link_afiliado: "https://www.mercadolivre.com.br/p/MLB3948571234?matt_tool=84729103&matt_word=meucanal2026",
        avaliacao: "4.9/5.0 (348 avaliações)",
        vendedor: "Mercado Livre Oficial",
        ciclo: "Ofertas Tech Relâmpago",
    };

    function updateTemplateLivePreview() {
        if (!platformTemplateInput || !templateLivePreviewBox) return;
        const platform = templatePlatformInput ? templatePlatformInput.value : "telegram";
        const maxLen = platform === "whatsapp" ? 4096 : 1024;
        const raw = platformTemplateInput.value || "";

        if (templateCharCounter) {
            templateCharCounter.textContent = `${raw.length} / ${maxLen} chars`;
            templateCharCounter.style.color = raw.length > maxLen ? "var(--danger)" : "var(--text-muted)";
        }

        const mandatory = ["{titulo}", "{preco}", "{link_afiliado}"];
        const missing = mandatory.filter((v) => !raw.includes(v));

        if (templateValidationStatus) {
            if (missing.length > 0) {
                templateValidationStatus.textContent = `Faltando obrigatórias: ${missing.join(", ")}`;
                templateValidationStatus.style.color = "var(--danger)";
            } else if (raw.length > maxLen) {
                templateValidationStatus.textContent = `Limite de ${maxLen} caracteres excedido`;
                templateValidationStatus.style.color = "var(--danger)";
            } else {
                templateValidationStatus.textContent = `Template válido (${platform.toUpperCase()})`;
                templateValidationStatus.style.color = "var(--success)";
            }
        }

        let rendered = raw
            .replace(/\{titulo\}/g, samplePreviewData.titulo)
            .replace(/\{preco\}/g, samplePreviewData.preco)
            .replace(/\{preco_original\}/g, samplePreviewData.preco_original)
            .replace(/\{desconto\}/g, samplePreviewData.desconto)
            .replace(/\{link_afiliado\}/g, samplePreviewData.link_afiliado)
            .replace(/\{avaliacao\}/g, samplePreviewData.avaliacao)
            .replace(/\{vendedor\}/g, samplePreviewData.vendedor)
            .replace(/\{ciclo\}/g, samplePreviewData.ciclo);

        templateLivePreviewBox.textContent = rendered;
    }

    async function loadPlatformTemplate(platform = "telegram") {
        if (!platformTemplateInput) return;
        if (templatePlatformInput) templatePlatformInput.value = platform;
        if (templatePlatformTabs) {
            templatePlatformTabs.querySelectorAll("[data-tpl-platform]").forEach((btn) => {
                btn.classList.toggle("active", btn.getAttribute("data-tpl-platform") === platform);
            });
        }
        try {
            const res = await fetch(`/api/templates/${encodeURIComponent(platform)}`);
            if (!res.ok) return;
            const tpl = await res.json();
            platformTemplateInput.value = tpl.template_text || "";
            updateTemplateLivePreview();
        } catch {
            // ignore
        }
    }

    if (templatePlatformTabs) {
        templatePlatformTabs.querySelectorAll("[data-tpl-platform]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const platform = btn.getAttribute("data-tpl-platform");
                loadPlatformTemplate(platform);
            });
        });
    }

    if (platformTemplateInput) {
        platformTemplateInput.addEventListener("input", updateTemplateLivePreview);
    }

    document.querySelectorAll("[data-insert-var]").forEach((btn) => {
        btn.addEventListener("click", () => {
            if (!platformTemplateInput) return;
            const variable = btn.getAttribute("data-insert-var");
            const start = platformTemplateInput.selectionStart ?? platformTemplateInput.value.length;
            const end = platformTemplateInput.selectionEnd ?? platformTemplateInput.value.length;
            const current = platformTemplateInput.value;
            platformTemplateInput.value = current.slice(0, start) + variable + current.slice(end);
            platformTemplateInput.focus();
            platformTemplateInput.setSelectionRange(start + variable.length, start + variable.length);
            updateTemplateLivePreview();
        });
    });

    if (platformTemplateForm) {
        platformTemplateForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const platform = templatePlatformInput ? templatePlatformInput.value : "telegram";
            const parseMode = platform === "whatsapp" ? "Markdown" : "HTML";
            const res = await fetch(`/api/templates/${encodeURIComponent(platform)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    platform,
                    template_text: platformTemplateInput.value,
                    parse_mode: parseMode,
                }),
            });
            const data = await res.json();
            if (!res.ok) {
                const errMsg = extractApiErrorMessage(data, "Template inválido.");
                showAuthFeedback("Erro no Template:", errMsg, "error", 6000);
                return;
            }
            showAuthFeedback(
                "Template Salvo:",
                `Template padrão para '${platform.toUpperCase()}' salvo com sucesso!`,
                "success",
                4500
            );
            updateTemplateLivePreview();
        });
    }

    if (resetTemplateBtn) {
        resetTemplateBtn.addEventListener("click", async () => {
            const platform = templatePlatformInput ? templatePlatformInput.value : "telegram";
            const res = await fetch(`/api/templates/${encodeURIComponent(platform)}/reset`, {
                method: "POST",
            });
            if (res.ok) {
                const data = await res.json();
                platformTemplateInput.value = data.template_text || "";
                updateTemplateLivePreview();
                showAuthFeedback(
                    "Template Restaurado:",
                    `Template padrão oficial de '${platform.toUpperCase()}' restaurado.`,
                    "info",
                    4000
                );
            }
        });
    }

    function parseKeywords(raw) {
        if (!raw) return [];
        const parts = String(raw).split(/[,;\r\n]+/);
        const normalized = [];
        const seen = new Set();
        for (const p of parts) {
            const clean = p.replace(/\s+/g, " ").trim();
            if (!clean) continue;
            const key = clean.toLowerCase();
            if (!seen.has(key)) {
                seen.add(key);
                normalized.push(clean);
            }
        }
        return normalized;
    }

    function renderKeywordTags() {
        if (!cycleQueryInput || !cycleQueryTags) return;
        const keywords = parseKeywords(cycleQueryInput.value);
        if (!keywords.length) {
            cycleQueryTags.innerHTML = `<span class="null-pill">Digite palavras-chave acima para visualizar as tags...</span>`;
            return;
        }
        cycleQueryTags.innerHTML = keywords
            .map(
                (kw, idx) => `
                <span class="keyword-tag">
                    <span>${escapeHtml(kw)}</span>
                    <button type="button" class="keyword-tag-remove" data-remove-kw-idx="${idx}" title="Remover palavra-chave">&times;</button>
                </span>`
            )
            .join("");

        cycleQueryTags.querySelectorAll("[data-remove-kw-idx]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const idxToRemove = parseInt(btn.getAttribute("data-remove-kw-idx"), 10);
                const updated = keywords.filter((_, i) => i !== idxToRemove);
                cycleQueryInput.value = updated.join(", ");
                renderKeywordTags();
            });
        });
    }

    if (cycleQueryInput) {
        cycleQueryInput.addEventListener("input", renderKeywordTags);
        cycleQueryInput.addEventListener("blur", () => {
            const normalized = parseKeywords(cycleQueryInput.value);
            if (normalized.length) {
                cycleQueryInput.value = normalized.join(", ");
            }
            renderKeywordTags();
        });
        renderKeywordTags();
    }

    function renderCycleDestinationsCheckboxes(destinations) {
        if (!destinations.length) {
            cycleDestinationsCheckboxes.innerHTML = `<span class="null-pill">Cadastre um destino em "Afiliados & Destinos" primeiro.</span>`;
            return;
        }
        const editingId = editingCycleIdInput ? editingCycleIdInput.value : "";
        const editingCycle = editingId ? loadedCyclesMap.get(editingId) : null;
        const activeDestIds = editingCycle ? new Set(editingCycle.destination_ids || []) : null;

        cycleDestinationsCheckboxes.innerHTML = destinations
            .map((d) => {
                const isChecked = activeDestIds ? activeDestIds.has(d.id) : true;
                return `
                <label class="check-label">
                    <input type="checkbox" name="cycleDest" value="${escapeHtml(d.id)}" ${isChecked ? "checked" : ""} />
                    <span>${escapeHtml(d.name)} (${escapeHtml(d.output_type)})</span>
                </label>`;
            })
            .join("");
    }

    function resetCycleFormState() {
        cycleForm.reset();
        if (editingCycleIdInput) editingCycleIdInput.value = "";
        if (cycleFormTitle) cycleFormTitle.textContent = "Novo Ciclo de Campanha";
        if (cycleSubmitBtn) cycleSubmitBtn.textContent = "Salvar Ciclo Automatizado";
        if (cancelCycleEditBtn) cancelCycleEditBtn.classList.add("hidden");
        renderKeywordTags();
        cyclesListContainer.querySelectorAll(".item-card.editing").forEach((el) => el.classList.remove("editing"));
    }

    function enterCycleEditMode(cycle) {
        if (!cycle) return;
        if (editingCycleIdInput) editingCycleIdInput.value = cycle.id;
        if (cycleFormTitle) cycleFormTitle.textContent = `Editando Ciclo: ${cycle.name}`;
        if (cycleSubmitBtn) cycleSubmitBtn.textContent = "Salvar Alterações";
        if (cancelCycleEditBtn) cancelCycleEditBtn.classList.remove("hidden");

        document.getElementById("cycleName").value = cycle.name || "";
        document.getElementById("cycleQuery").value = cycle.query || "";
        document.getElementById("cycleSource").value = cycle.source_id || "mercadolivre";
        document.getElementById("cycleSearchLimit").value = cycle.search_limit ?? 20;
        document.getElementById("cycleMaxOffers").value = cycle.max_offers_per_run ?? 5;
        document.getElementById("cycleInterval").value = cycle.interval_minutes ?? 30;
        document.getElementById("cycleProductDelay").value = Math.round((cycle.product_delay_minutes ?? 0) * 60 * 10) / 10;
        document.getElementById("cycleDeduplicate").checked = Boolean(cycle.deduplicate);

        const f = cycle.filters || {};
        document.getElementById("filterMinPrice").value = f.min_price !== null && f.min_price !== undefined ? f.min_price : "";
        document.getElementById("filterMaxPrice").value = f.max_price !== null && f.max_price !== undefined ? f.max_price : "";
        document.getElementById("filterMinDiscount").value = f.min_discount !== null && f.min_discount !== undefined ? f.min_discount : "";
        document.getElementById("filterMinRating").value = f.min_rating !== null && f.min_rating !== undefined ? f.min_rating : "";
        document.getElementById("filterRequireAffiliate").checked =
            f.require_affiliate_link !== undefined ? Boolean(f.require_affiliate_link) : true;

        const destSet = new Set(cycle.destination_ids || []);
        cycleDestinationsCheckboxes.querySelectorAll('input[name="cycleDest"]').forEach((cb) => {
            cb.checked = destSet.has(cb.value);
        });

        renderKeywordTags();

        cyclesListContainer.querySelectorAll(".item-card").forEach((el) => {
            el.classList.toggle("editing", el.getAttribute("data-card-cycle-id") === cycle.id);
        });
        cycleForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    if (cancelCycleEditBtn) {
        cancelCycleEditBtn.addEventListener("click", () => {
            resetCycleFormState();
        });
    }

    function extractApiErrorMessage(data, fallbackMsg) {
        if (!data) return fallbackMsg;
        if (Array.isArray(data.details) && data.details.length > 0) {
            return data.details.map((d) => (d.msg || "").replace(/^Value error,\s*/i, "")).join(" | ");
        }
        if (Array.isArray(data.detail) && data.detail.length > 0) {
            return data.detail.map((d) => (d.msg || "").replace(/^Value error,\s*/i, "")).join(" | ");
        }
        if (typeof data.detail === "string") return data.detail;
        if (typeof data.message === "string") return data.message;
        return fallbackMsg;
    }

    cycleForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const editingId = editingCycleIdInput ? editingCycleIdInput.value.trim() : "";
        const keywords = parseKeywords(document.getElementById("cycleQuery").value);
        if (!keywords.length) {
            showAuthFeedback(
                "Palavras-chave inválidas:",
                "Informe pelo menos uma palavra-chave válida no campo Nicho / palavras-chave.",
                "error"
            );
            return;
        }

        const intervalMinutes = parseInt(document.getElementById("cycleInterval").value, 10) || 0;
        const maxOffersPerRun = parseInt(document.getElementById("cycleMaxOffers").value, 10) || 1;
        const rawDelay = document.getElementById("cycleProductDelay").value.trim();
        const productDelaySeconds = rawDelay !== "" ? parseFloat(rawDelay) : 0;
        const productDelayMinutes = productDelaySeconds / 60.0;

        if (isNaN(productDelaySeconds) || productDelaySeconds < 0) {
            showAuthFeedback(
                "Validação de Tempo:",
                "O 'Tempo entre produtos (seg)' não pode ser negativo.",
                "error"
            );
            return;
        }

        if (productDelayMinutes >= intervalMinutes) {
            showAuthFeedback(
                "Regra de Intervalo Violada:",
                `O 'Tempo entre produtos (${productDelaySeconds} seg / ${(productDelayMinutes).toFixed(2)} min)' deve ser MENOR que o 'Intervalo do ciclo (${intervalMinutes} min)'.`,
                "error",
                7000
            );
            return;
        }

        const totalDispatchDurationMin = maxOffersPerRun * productDelayMinutes;
        if (productDelayMinutes > 0 && totalDispatchDurationMin > intervalMinutes) {
            showAuthFeedback(
                "Janela de Disparo Excedida:",
                `O tempo total de envio (${maxOffersPerRun} produtos × ${productDelaySeconds} seg = ${(maxOffersPerRun * productDelaySeconds).toFixed(0)} seg / ${totalDispatchDurationMin.toFixed(2)} min) excede o 'Intervalo do ciclo (${intervalMinutes} min)'. Reduza a quantidade de produtos, o tempo entre produtos ou aumente o intervalo.`,
                "error",
                8000
            );
            return;
        }

        const selectedDests = Array.from(
            cycleDestinationsCheckboxes.querySelectorAll('input[name="cycleDest"]:checked')
        ).map((el) => el.value);

        const parseOptFloat = (id) => {
            const v = document.getElementById(id).value.trim();
            return v ? parseFloat(v) : null;
        };
        const parseOptInt = (id) => {
            const v = document.getElementById(id).value.trim();
            return v ? parseInt(v, 10) : null;
        };

        const payload = {
            name: document.getElementById("cycleName").value.trim(),
            query: keywords.join(", "),
            source_id: document.getElementById("cycleSource").value,
            search_limit: parseInt(document.getElementById("cycleSearchLimit").value, 10) || 20,
            max_offers_per_run: maxOffersPerRun,
            interval_minutes: intervalMinutes,
            product_delay_minutes: productDelayMinutes,
            destination_ids: selectedDests,
            deduplicate: document.getElementById("cycleDeduplicate").checked,
            filters: {
                min_price: parseOptFloat("filterMinPrice"),
                max_price: parseOptFloat("filterMaxPrice"),
                min_discount: parseOptInt("filterMinDiscount"),
                min_rating: parseOptFloat("filterMinRating"),
                require_affiliate_link: document.getElementById("filterRequireAffiliate").checked,
            },
        };

        const endpoint = editingId ? `/api/cycles/${encodeURIComponent(editingId)}` : "/api/cycles";
        const method = editingId ? "PUT" : "POST";

        const res = await fetch(endpoint, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            const errMsg = extractApiErrorMessage(data, "Verifique os campos informados.");
            showAuthFeedback(editingId ? "Erro ao Editar Ciclo:" : "Erro ao Criar Ciclo:", errMsg, "error", 7000);
            return;
        }
        resetCycleFormState();
        showAuthFeedback(
            editingId ? "Ciclo Atualizado:" : "Ciclo Criado:",
            editingId
                ? `Campanha '${data.name}' atualizada com sucesso!`
                : `Campanha '${data.name}' criada com sucesso!`,
            "success",
            4500
        );
        loadDestinationsAndCycles();
    });

    function renderCyclesList(cycles) {
        loadedCyclesMap = new Map((cycles || []).map((c) => [c.id, c]));
        if (!cycles.length) {
            cyclesListContainer.innerHTML = `<span class="null-pill">Nenhum ciclo automatizado cadastrado.</span>`;
            return;
        }
        const editingId = editingCycleIdInput ? editingCycleIdInput.value : "";

        cyclesListContainer.innerHTML = cycles
            .map((c) => {
                const f = c.filters || {};
                const filterDesc = [
                    f.min_price !== null && f.min_price !== undefined ? `Min R$${f.min_price}` : null,
                    f.max_price !== null && f.max_price !== undefined ? `Max R$${f.max_price}` : null,
                    f.min_discount ? `Desc >=${f.min_discount}%` : null,
                    f.min_rating ? `Nota >=${f.min_rating}` : null,
                ]
                    .filter(Boolean)
                    .join(" | ") || "Sem filtros";

                const kwTagsHtml = parseKeywords(c.query)
                    .map((kw) => `<span class="keyword-tag-sm">${escapeHtml(kw)}</span>`)
                    .join("");

                const delayValMin = c.product_delay_minutes ?? 0;
                const delaySec = Math.round(delayValMin * 60);
                const delayLabel = delaySec >= 60 && delaySec % 60 === 0
                    ? `${delaySec}s (${delayValMin} min)`
                    : `${delaySec}s`;
                const errLine = c.last_error
                    ? `<div style="color: var(--danger); font-size: 11px;">Aviso/erro: ${escapeHtml(c.last_error)}</div>`
                    : "";

                const lastDurStr = c.last_duration_seconds !== null && c.last_duration_seconds !== undefined
                    ? `${Number(c.last_duration_seconds).toFixed(2)}s`
                    : "—";
                const lastDelayNum = Number(c.last_schedule_delay_seconds ?? 0);
                const delayBadgeHtml = lastDelayNum > 5
                    ? `<span class="status-pill error" title="Atraso em relação ao horário previsto">+${lastDelayNum.toFixed(1)}s atraso</span>`
                    : `<span class="status-pill running" title="Agendamento pontual">${lastDelayNum.toFixed(1)}s atraso</span>`;
                const bottleneckHtml = c.last_bottleneck && c.last_bottleneck !== "nenhum"
                    ? `<span class="badge-id" title="Etapa de maior custo na última execução">Gargalo: ${escapeHtml(c.last_bottleneck)}</span>`
                    : "";

                const isEditingClass = editingId === c.id ? " editing" : "";

                return `
                <div class="item-card${isEditingClass}" data-card-cycle-id="${escapeHtml(c.id)}">
                    <div class="item-card-header">
                        <div>
                            <strong>${escapeHtml(c.name)}</strong>
                            <span class="cycle-tags-inline">${kwTagsHtml}</span>
                        </div>
                        <span class="status-pill ${escapeHtml(c.state)}">${c.is_running_now ? "EXECUTANDO" : escapeHtml(c.state)}</span>
                    </div>
                    <div style="font-size: 11px; color: var(--text-muted);">
                        Busca: <b>${c.search_limit}</b> &bull; Máx: <b>${c.max_offers_per_run}</b> &bull;
                        Ciclo: <b>${c.interval_minutes} min</b> &bull; Entre prod.: <b>${delayLabel}</b> &bull; ${escapeHtml(filterDesc)}
                    </div>
                    <div style="font-size: 11px; color: var(--text-muted);">
                        Execuções: <b>${c.total_runs}</b> &bull; Enviadas: <b>${c.total_offers_sent}</b> &bull;
                        Última: ${formatDateShort(c.last_run_at)} &bull; Próxima: ${formatDateShort(c.next_run_at)}
                    </div>
                    <div style="font-size: 11px; color: var(--text-muted); display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                        <span>Duração: <b>${lastDurStr}</b></span>
                        ${c.total_runs > 0 ? delayBadgeHtml : ""}
                        ${bottleneckHtml}
                    </div>
                    ${errLine}
                    <div class="item-actions">
                        <button type="button" class="btn btn-sm btn-primary" data-cycle-act="run-now" data-id="${escapeHtml(c.id)}">Executar</button>
                        <button type="button" class="btn btn-sm btn-accent" data-cycle-act="start" data-id="${escapeHtml(c.id)}">Iniciar</button>
                        <button type="button" class="btn btn-sm btn-secondary" data-cycle-act="pause" data-id="${escapeHtml(c.id)}">Pausar</button>
                        <button type="button" class="btn btn-sm btn-secondary" data-cycle-act="resume" data-id="${escapeHtml(c.id)}">Retomar</button>
                        <button type="button" class="btn btn-sm btn-secondary" data-cycle-act="stop" data-id="${escapeHtml(c.id)}">Encerrar</button>
                        <button type="button" class="btn btn-sm btn-secondary" data-cycle-edit="${escapeHtml(c.id)}">Editar</button>
                        <button type="button" class="btn btn-sm btn-danger" data-cycle-del="${escapeHtml(c.id)}">Excluir</button>
                    </div>
                </div>`;
            })
            .join("");

        cyclesListContainer.querySelectorAll("[data-cycle-act]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const id = btn.getAttribute("data-id");
                const act = btn.getAttribute("data-cycle-act");
                btn.disabled = true;
                try {
                    const res = await fetch(`/api/cycles/${id}/${act}`, { method: "POST" });
                    const data = await res.json();
                    if (!res.ok) {
                        showAuthFeedback("Aviso Ciclo:", data.detail || "Falha na operação.", "warning");
                    } else if (act === "run-now") {
                        showAuthFeedback(
                            "Execução Concluída:",
                            `Buscados: ${data.searched_count} | Filtrados: ${data.filtered_count} | Afiliados: ${data.affiliate_ok_count} | Enviados: ${data.sent_count}`,
                            data.status === "completed" ? "success" : "warning",
                            6000
                        );
                    }
                } finally {
                    btn.disabled = false;
                    loadDestinationsAndCycles();
                }
            });
        });

        cyclesListContainer.querySelectorAll("[data-cycle-edit]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-cycle-edit");
                const cycle = loadedCyclesMap.get(id);
                if (cycle) {
                    enterCycleEditMode(cycle);
                }
            });
        });

        cyclesListContainer.querySelectorAll("[data-cycle-del]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const id = btn.getAttribute("data-cycle-del");
                if (editingCycleIdInput && editingCycleIdInput.value === id) {
                    resetCycleFormState();
                }
                await fetch(`/api/cycles/${id}`, { method: "DELETE" });
                loadDestinationsAndCycles();
            });
        });
    }

    refreshCyclesBtn.addEventListener("click", loadDestinationsAndCycles);

    // =====================================================================
    // History & Monitoring
    // =====================================================================

    async function loadHistory() {
        try {
            const [runsRes, offersRes] = await Promise.all([
                fetch("/api/history/runs?limit=50"),
                fetch("/api/history/offers?limit=100"),
            ]);
            const runs = runsRes.ok ? await runsRes.json() : [];
            const offers = offersRes.ok ? await offersRes.json() : [];

            runsHistoryBody.innerHTML = runs.length
                ? runs
                      .map((r) => {
                          const durStr = r.duration_seconds !== null && r.duration_seconds !== undefined
                              ? `${Number(r.duration_seconds).toFixed(2)}s`
                              : "—";
                          const delayNum = Number(r.schedule_delay_seconds ?? 0);
                          const delayStr = delayNum > 5
                              ? `<span class="status-pill error">+${delayNum.toFixed(1)}s</span>`
                              : `<span class="status-pill running">${delayNum.toFixed(1)}s</span>`;
                          const stagesStr = `Nav ${Math.round(r.navigation_ms ?? 0)} · Extr ${Math.round(r.extraction_ms ?? 0)} · Proc ${Math.round(r.processing_ms ?? 0)} · Afil ${Math.round(r.affiliate_ms ?? 0)} · Env ${Math.round(r.dispatch_ms ?? 0)}`;
                          const bneck = r.bottleneck_stage && r.bottleneck_stage !== "nenhum"
                              ? `<span class="badge-id">${escapeHtml(r.bottleneck_stage)}</span>`
                              : "";
                          return `
                    <tr>
                        <td>
                            <div style="font-size: 11px; color: var(--text-muted);">Prev: ${formatDateShort(r.scheduled_at || r.started_at)}</div>
                            <div>Início: <strong>${formatDateShort(r.started_at)}</strong></div>
                        </td>
                        <td><strong>${escapeHtml(r.cycle_name)}</strong> (${escapeHtml(r.query)})</td>
                        <td><span class="status-pill ${escapeHtml(r.status)}">${escapeHtml(r.status)}</span></td>
                        <td><strong>${durStr}</strong> &bull; ${delayStr}</td>
                        <td>
                            <div style="font-size: 11px; color: var(--text-secondary);">${stagesStr}</div>
                            ${bneck}
                        </td>
                        <td>${r.searched_count}</td>
                        <td>${r.filtered_count}</td>
                        <td>${r.affiliate_ok_count} / ${r.affiliate_fail_count}</td>
                        <td>${r.deduplicated_new_count}</td>
                        <td><strong>${r.sent_count}</strong> (${r.failed_send_count} err)</td>
                        <td>${r.error_message ? escapeHtml(r.error_message) : '<span class="null-pill">OK</span>'}</td>
                    </tr>`;
                      })
                      .join("")
                : `<tr><td colspan="11"><span class="null-pill">Nenhuma execução registrada ainda.</span></td></tr>`;

            offersHistoryBody.innerHTML = offers.length
                ? offers
                      .map(
                          (o) => `
                    <tr>
                        <td>${formatDateShort(o.sent_at)}</td>
                        <td>${escapeHtml(o.cycle_name)}</td>
                        <td>${escapeHtml(o.destination_name)}</td>
                        <td><code>${escapeHtml(o.product_id || "—")}</code> ${escapeHtml(o.product_name)}</td>
                        <td><strong>${formatCurrency(o.promotional_price) || "—"}</strong></td>
                        <td>${o.discount_percentage ? `-${o.discount_percentage}%` : "—"}</td>
                        <td><a class="table-product-link" href="${escapeHtml(o.affiliate_url)}" target="_blank" rel="noopener noreferrer">Abrir Link ↗</a></td>
                        <td><span class="status-pill ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></td>
                    </tr>`
                      )
                      .join("")
                : `<tr><td colspan="8"><span class="null-pill">Nenhuma oferta enviada registrada ainda.</span></td></tr>`;
        } catch {
            // ignore
        }
    }

    refreshHistoryBtn.addEventListener("click", loadHistory);

    checkHealth();
    fetchAuthStatus();
    loadAffiliateConfig();
})();
