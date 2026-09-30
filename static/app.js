document.addEventListener("DOMContentLoaded", () => {
    setupSidebar();
    setupRateModal();
    setupDetailsPersistence();
    setupPricePreview();
    setupAuthPanels();
    setupDashboard();
    setupHistoryDetails();
});

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function omitPabiloNoise(value) {
    const hiddenKeys = new Set(["banco_origen", "cuenta_pagador", "cuentaPagador"]);
    if (Array.isArray(value)) {
        return value.map((item) => omitPabiloNoise(item));
    }
    if (!value || typeof value !== "object") {
        return value;
    }

    return Object.fromEntries(
        Object.entries(value)
            .filter(([key]) => !hiddenKeys.has(key))
            .map(([key, entryValue]) => [key, omitPabiloNoise(entryValue)]),
    );
}

function formatPabiloDateTime(rawValue, timeZone) {
    if (!rawValue) {
        return null;
    }

    const parsedDate = new Date(rawValue);
    if (Number.isNaN(parsedDate.getTime())) {
        return null;
    }

    return new Intl.DateTimeFormat("es-VE", {
        timeZone: timeZone || undefined,
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
    }).format(parsedDate);
}

function setupAuthPanels() {
    const root = document.querySelector("[data-auth-root]");
    if (!root) {
        return;
    }

    const defaultTab = root.dataset.defaultTab || "login";
    const tabs = root.querySelectorAll("[data-auth-tab]");
    const panels = root.querySelectorAll("[data-auth-panel]");

    function activate(tabName) {
        tabs.forEach((tab) => {
            tab.classList.toggle("is-active", tab.dataset.authTab === tabName);
        });
        panels.forEach((panel) => {
            panel.classList.toggle("is-active", panel.dataset.authPanel === tabName);
        });
    }

    tabs.forEach((tab) => {
        tab.addEventListener("click", () => activate(tab.dataset.authTab));
    });

    activate(defaultTab);
}

// Menú hamburguesa: el menú permanece oculto y se despliega hacia abajo al tocar el botón.
function setupSidebar() {
    const menu = document.querySelector("[data-menu]");
    const toggle = document.querySelector("[data-menu-toggle]");
    if (!menu || !toggle) {
        return;
    }

    function setOpen(open) {
        menu.classList.toggle("is-open", open);
        toggle.classList.toggle("is-open", open);
        toggle.setAttribute("aria-expanded", String(open));
        toggle.setAttribute("aria-label", open ? "Cerrar menú" : "Abrir menú");
    }

    toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        setOpen(!menu.classList.contains("is-open"));
    });

    document.addEventListener("click", (event) => {
        if (menu.classList.contains("is-open") && !menu.contains(event.target)) {
            setOpen(false);
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && menu.classList.contains("is-open")) {
            setOpen(false);
            toggle.focus();
        }
    });
}

// El botón "Tasa Bs X" del resumen abre una ventana emergente para cambiar la tasa.
function setupRateModal() {
    const openButton = document.getElementById("open_rate_modal");
    const modal = document.getElementById("rate_modal");
    const closeButton = document.getElementById("close_rate_modal");
    if (!openButton || !modal) {
        return;
    }
    const input = modal.querySelector("input[name=exchange_rate_bs]");
    const initialValue = input ? input.value : "";

    function close() {
        modal.classList.add("is-hidden");
        if (input) {
            input.value = initialValue;
        }
        openButton.focus();
    }

    openButton.addEventListener("click", () => {
        modal.classList.remove("is-hidden");
        if (input) {
            input.focus();
            input.select();
        }
    });
    if (closeButton) {
        closeButton.addEventListener("click", close);
    }
    modal.addEventListener("click", (event) => {
        if (event.target === modal) {
            close();
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !modal.classList.contains("is-hidden")) {
            close();
        }
    });
}

function roundMoney(value) {
    return Math.round((value + Number.EPSILON) * 100) / 100;
}

// Muestra en vivo el equivalente del precio de un paquete en la otra moneda,
// usando la tasa del usuario (la misma con la que se registran las ventas).
function setupPricePreview() {
    const rateHolder = document.querySelector("[data-exchange-rate]");
    const exchangeRate = rateHolder ? Number(rateHolder.dataset.exchangeRate) : 0;
    const previews = document.querySelectorAll("[data-price-preview]");
    if (!previews.length || !(exchangeRate > 0)) {
        return;
    }

    previews.forEach((preview) => {
        const form = preview.closest("form");
        const currencyInput = form.querySelector("[name=price_currency]");
        const priceInput = form.querySelector("[name=price_value]");
        if (!currencyInput || !priceInput) {
            return;
        }

        const update = () => {
            const value = Number(priceInput.value);
            if (!(value > 0)) {
                preview.textContent = `Tasa Bs ${exchangeRate.toFixed(2)}`;
                return;
            }
            preview.textContent = currencyInput.value === "BS"
                ? `≈ USD ${roundMoney(value / exchangeRate).toFixed(2)} (tasa Bs ${exchangeRate.toFixed(2)})`
                : `≈ Bs ${roundMoney(value * exchangeRate).toFixed(2)} (tasa Bs ${exchangeRate.toFixed(2)})`;
        };
        currencyInput.addEventListener("change", update);
        priceInput.addEventListener("input", update);
        update();
    });
}

// Al guardar un formulario del catálogo la página se recarga: recordamos qué
// desplegables estaban abiertos y la posición de scroll para volver al mismo sitio.
// El desplegable que contiene el formulario guardado se cierra si tiene data-collapse-on-save.
function setupDetailsPersistence() {
    const detailsList = document.querySelectorAll("details[data-details-key]");
    if (!detailsList.length) {
        return;
    }

    const storageKey = `antiduplic-open-details:${window.location.pathname}`;

    document.querySelectorAll("form").forEach((form) => {
        form.addEventListener("submit", () => {
            const container = form.closest("details[data-details-key]");
            const collapseKey = container && container.hasAttribute("data-collapse-on-save")
                ? container.dataset.detailsKey
                : null;
            const openKeys = Array.from(detailsList)
                .filter((item) => item.open && item.dataset.detailsKey !== collapseKey)
                .map((item) => item.dataset.detailsKey);
            try {
                sessionStorage.setItem(storageKey, JSON.stringify({ openKeys, scrollY: window.scrollY }));
            } catch (error) {}
        });
    });

    let saved = null;
    try {
        saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
        sessionStorage.removeItem(storageKey);
    } catch (error) {}
    if (!saved) {
        return;
    }

    detailsList.forEach((item) => {
        item.open = saved.openKeys.includes(item.dataset.detailsKey);
    });

    // Si hubo un error de validación, se muestra arriba: no movemos el scroll.
    if (!document.querySelector(".alert--error")) {
        window.scrollTo(0, saved.scrollY || 0);
    }
}

function setupDashboard() {
    const root = document.querySelector("[data-dashboard-root]");
    if (!root) {
        return;
    }

    const dashboardTimeZone = root.dataset.timezone || "UTC";

    const catalogElement = document.getElementById("catalog-data");
    const paymentInput = document.getElementById("payment_method_id");
    const serviceInput = document.getElementById("service_id");
    const packageList = document.getElementById("package_list");
    const cartItems = document.getElementById("cart_items");
    const totalUsd = document.getElementById("total_usd");
    const totalBs = document.getElementById("total_bs");
    const notesInput = document.getElementById("sale_notes");
    const registerButton = document.getElementById("register_sale_button");
    const feedback = document.getElementById("sale_feedback");
    const referenceInput = document.getElementById("reference_input");
    const lastSix = document.querySelector("[data-last-six]");
    const duplicateWarning = document.getElementById("duplicate_warning");
    const duplicateContent = document.querySelector("[data-duplicate-content]");
    const forceSevenButton = document.getElementById("force_seven_button");
    const duplicateModal = document.getElementById("duplicate_modal");
    const closeDuplicateModal = document.getElementById("close_duplicate_modal");
    const clearReferenceButton = document.getElementById("clear_reference_button");
    const recentSalesList = document.getElementById("recent_sales_list");
    const verifyPabiloButton = document.getElementById("verify_pabilo_button");
    const pabiloResultPanel = document.getElementById("pabilo_result_panel");
    const pabiloResultTitle = document.getElementById("pabilo_result_title");
    const pabiloResultBadge = document.getElementById("pabilo_result_badge");
    const pabiloResultMessage = document.getElementById("pabilo_result_message");
    const pabiloResultSummary = document.getElementById("pabilo_result_summary");
    const catalog = JSON.parse(catalogElement.textContent || "[]");

    const state = {
        selectedPaymentMethodId: Number(paymentInput.value),
        selectedServiceId: Number(serviceInput.value),
        cart: [],
        forceSevenValidation: false,
        duplicateStatus: null,
        pabiloResult: null,
    };

    // "Verificar en Pabilo" solo aparece si el método elegido lo tiene activado.
    function syncPabiloVisibility(methodId) {
        const methodButton = document.querySelector(`[data-select-group="payment-methods"] [data-value="${methodId}"]`);
        const enabled = Boolean(methodButton && methodButton.dataset.pabilo === "1");
        document.querySelectorAll("[data-pabilo-only]").forEach((element) => element.classList.toggle("is-hidden", !enabled));
        const shell = document.getElementById("reference_shell");
        if (shell) {
            shell.classList.toggle("reference-shell--with-pabilo", enabled && Boolean(document.getElementById("verify_pabilo_button")));
        }
    }

    bindChoiceGroup("payment-methods", paymentInput, (value) => {
        state.selectedPaymentMethodId = Number(value);
        syncPabiloVisibility(value);
        clearPabiloResult();
        if (referenceInput.value.trim()) {
            checkReference();
        }
    });

    function getServicesForSelectedMethod() {
        return catalog;
    }

    function renderServiceChoices() {
        const group = document.getElementById("service_choice_group");
        if (!group) {
            return;
        }

        const availableServices = getServicesForSelectedMethod();
        if (!availableServices.length) {
            state.selectedServiceId = 0;
            serviceInput.value = "";
            group.innerHTML = "<p class='empty-state'>No tienes servicios o paquetes asignados para este método.</p>";
            renderPackages();
            return;
        }

        if (!availableServices.some((service) => service.id === state.selectedServiceId)) {
            const defaultService = availableServices.find((service) => service.is_default) || availableServices[0];
            state.selectedServiceId = defaultService.id;
        }

        serviceInput.value = String(state.selectedServiceId);
        group.innerHTML = "";

        availableServices.forEach((service, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `choice-chip choice-chip--danger${service.id === state.selectedServiceId ? " is-selected" : ""}`;
            button.dataset.value = String(service.id);
            button.dataset.role = "choice-button";
            button.textContent = `${index + 1}. ${service.name}`;
            button.addEventListener("click", () => {
                state.selectedServiceId = service.id;
                serviceInput.value = String(service.id);
                renderServiceChoices();
                renderPackages();
            });
            group.appendChild(button);
        });

        renderPackages();
    }

    function renderPackages() {
        const activeService = getServicesForSelectedMethod().find((service) => service.id === state.selectedServiceId);
        if (!activeService) {
            packageList.innerHTML = "<p class='empty-state'>No hay paquetes activos.</p>";
            return;
        }

        packageList.innerHTML = "";
        activeService.packages.forEach((pkg) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "package-button";
            button.innerHTML = `<span class="package-button__name">${escapeHtml(pkg.name)}</span><strong>${escapeHtml(pkg.display_price)}</strong><small class="package-button__alt">≈ ${escapeHtml(secondaryPrice(pkg))}</small>`;
            button.addEventListener("click", () => addPackage(pkg, activeService, button));
            packageList.appendChild(button);
        });
    }

    // El precio principal es el que se definió en el paquete; el otro se calcula con la tasa.
    function secondaryPrice(pkg) {
        return pkg.display_currency === "BS" ? `USD ${pkg.usd_price}` : `Bs ${pkg.bs_price}`;
    }

    function addPackage(pkg, service, button) {
        state.cart.push({
            packageId: pkg.id,
            serviceName: service.name,
            packageName: pkg.name,
            usdPrice: Number(pkg.usd_price),
            bsPrice: Number(pkg.bs_price),
            displayPrice: pkg.display_price,
            secondaryPrice: secondaryPrice(pkg),
        });
        if (button) {
            button.classList.remove("is-just-added");
            window.requestAnimationFrame(() => button.classList.add("is-just-added"));
            window.setTimeout(() => button.classList.remove("is-just-added"), 260);
        }
        renderCart();
    }

    function removeFromCart(index) {
        state.cart.splice(index, 1);
        renderCart();
        renderPackages();
    }

    function renderCart() {
        if (!state.cart.length) {
            cartItems.innerHTML = "<p class='empty-state'>Agrega servicios y paquetes para armar el pedido.</p>";
            totalUsd.textContent = "0.00";
            totalBs.textContent = "0.00";
            return;
        }

        cartItems.innerHTML = "";
        // Se suman céntimos enteros para evitar errores de redondeo de JavaScript.
        const usdTotal = state.cart.reduce((sum, item) => sum + Math.round(item.usdPrice * 100), 0) / 100;
        const bsTotal = state.cart.reduce((sum, item) => sum + Math.round(item.bsPrice * 100), 0) / 100;

        state.cart.forEach((item, index) => {
            const article = document.createElement("article");
            article.className = "cart-item";
            article.innerHTML = `
                <div class="cart-item__meta">
                    <strong>${escapeHtml(item.serviceName)}</strong>
                    <span>${escapeHtml(item.packageName)}</span>
                </div>
                <div class="cart-item__meta cart-item__price">
                    <strong>${escapeHtml(item.displayPrice)}</strong>
                    <span>≈ ${escapeHtml(item.secondaryPrice)}</span>
                </div>
                <button type="button" class="icon-button remove-button" aria-label="Quitar item">×</button>
            `;
            article.querySelector(".remove-button").addEventListener("click", () => removeFromCart(index));
            cartItems.appendChild(article);
        });

        totalUsd.textContent = usdTotal.toFixed(2);
        totalBs.textContent = bsTotal.toFixed(2);
    }

    function normalizeReference() {
        const digits = (referenceInput.value.match(/\d/g) || []).join("");
        const suffix = digits.slice(-Math.min(digits.length, 6));
        lastSix.textContent = suffix || "------";
        if (clearReferenceButton) {
            clearReferenceButton.classList.toggle("is-hidden", !referenceInput.value.trim());
        }
        if (!digits) {
            if (duplicateWarning) {
                duplicateWarning.classList.add("is-hidden");
            }
            if (duplicateModal) {
                duplicateModal.classList.add("is-hidden");
            }
            if (forceSevenButton) {
                forceSevenButton.classList.add("is-hidden");
            }
            state.forceSevenValidation = false;
            clearPabiloResult();
        }
    }

    function clearPabiloResult() {
        state.pabiloResult = null;
        if (!pabiloResultPanel) {
            return;
        }
        pabiloResultPanel.classList.add("is-hidden");
        pabiloResultPanel.dataset.state = "";
        if (pabiloResultTitle) {
            pabiloResultTitle.textContent = "Consulta Pabilo";
        }
        if (pabiloResultBadge) {
            pabiloResultBadge.textContent = "Sin consulta";
        }
        if (pabiloResultMessage) {
            pabiloResultMessage.textContent = "";
        }
        if (pabiloResultSummary) {
            pabiloResultSummary.innerHTML = "";
        }
    }

    function renderPabiloResult(result) {
        if (!pabiloResultPanel) {
            return;
        }

        state.pabiloResult = result;
        pabiloResultPanel.classList.remove("is-hidden");
        pabiloResultPanel.dataset.state = result.ok ? (result.found ? (result.verified ? "success" : "warning") : "neutral") : "error";
        pabiloResultTitle.textContent = result.found ? "Pago consultado en Pabilo" : "Respuesta de Pabilo";
        pabiloResultBadge.textContent = result.found ? (result.verified ? "Verificado" : "Encontrado") : "Sin coincidencia";
        pabiloResultMessage.textContent = result.message || "";

        const payment = result.payment || {};
        const summaryLines = [];
        if (payment.reference) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Referencia</div><div class="pabilo-text-value">${escapeHtml(payment.reference)}</div></div>`);
        }
        if (payment.amount_paid_value) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Monto</div><div class="pabilo-text-value">${escapeHtml(payment.amount_paid_currency || "BS")} ${escapeHtml(payment.amount_paid_value)}</div></div>`);
        }
        if (payment.status) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Estado</div><div class="pabilo-text-value">${escapeHtml(payment.status)}</div></div>`);
        }
        const localizedPaymentDateTime = formatPabiloDateTime(payment.payment_datetime_raw, dashboardTimeZone);
        if (localizedPaymentDateTime) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Fecha/Hora</div><div class="pabilo-text-value">${escapeHtml(localizedPaymentDateTime)}</div></div>`);
        } else if (payment.payment_date && payment.payment_time) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Fecha/Hora</div><div class="pabilo-text-value">${escapeHtml(payment.payment_date)} ${escapeHtml(payment.payment_time)}</div></div>`);
        } else if (payment.payment_date) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Fecha</div><div class="pabilo-text-value">${escapeHtml(payment.payment_date)}</div></div>`);
        } else if (payment.payment_time) {
            summaryLines.push(`<div class="pabilo-text-item"><div class="pabilo-text-label">Hora</div><div class="pabilo-text-value">${escapeHtml(payment.payment_time)}</div></div>`);
        }
        if (!summaryLines.length) {
            summaryLines.push("<div class='pabilo-text-item'><div class='pabilo-text-label'>Resultado</div><div class='pabilo-text-value'>Sin datos normalizados para mostrar.</div></div>");
        }
        pabiloResultSummary.innerHTML = summaryLines.join("");
    }

    function resetReferenceField() {
        referenceInput.value = "";
        state.forceSevenValidation = false;
        state.duplicateStatus = null;
        normalizeReference();
        if (duplicateWarning) {
            duplicateWarning.classList.add("is-hidden");
        }
        if (duplicateModal) {
            duplicateModal.classList.add("is-hidden");
        }
        if (forceSevenButton) {
            forceSevenButton.classList.add("is-hidden");
            forceSevenButton.textContent = "Validar con 7 digitos";
            forceSevenButton.classList.remove("is-selected");
        }
    }

    function renderRecentSaleCard(sale) {
        return `
            <article class="history-card history-card--new">
                <div class="history-card__top">
                    <strong>#${escapeHtml(sale.id)}</strong>
                    <span>${escapeHtml(sale.created_at)}</span>
                </div>
                <p>${escapeHtml(sale.payment_method)} · Ref ${escapeHtml(sale.reference_short || sale.reference)}</p>
                <ul class="inline-list">
                    ${sale.items.map((item) => `<li>${escapeHtml(item.service)} / ${escapeHtml(item.package)}</li>`).join("")}
                </ul>
                <div class="history-card__totals">USD ${escapeHtml(sale.amount_paid_usd)} · Bs ${escapeHtml(sale.amount_paid_bs)}</div>
            </article>
        `;
    }

    function prependRecentSale(sale) {
        if (!recentSalesList) {
            return;
        }
        const emptyState = document.getElementById("recent_sales_empty");
        if (emptyState) {
            emptyState.remove();
        }
        recentSalesList.insertAdjacentHTML("afterbegin", renderRecentSaleCard(sale));
        const cards = recentSalesList.querySelectorAll(".history-card");
        cards.forEach((card, index) => {
            if (index >= 5) {
                card.remove();
            }
        });
    }

    let debounceTimer = null;
    referenceInput.addEventListener("input", () => {
        normalizeReference();
        clearPabiloResult();
        window.clearTimeout(debounceTimer);
        debounceTimer = window.setTimeout(checkReference, 350);
    });

    if (clearReferenceButton) {
        clearReferenceButton.addEventListener("click", resetReferenceField);
    }

    if (forceSevenButton) {
        forceSevenButton.addEventListener("click", () => {
            state.forceSevenValidation = !state.forceSevenValidation;
            forceSevenButton.textContent = state.forceSevenValidation ? "Validacion de 7 digitos activada" : "Validar con 7 digitos";
            forceSevenButton.classList.toggle("is-selected", state.forceSevenValidation);
        });
    }

    if (closeDuplicateModal && duplicateModal) {
        closeDuplicateModal.addEventListener("click", () => {
            duplicateModal.classList.add("is-hidden");
        });
    }

    async function checkReference() {
        const reference = referenceInput.value.trim();
        if (!reference) {
            if (duplicateWarning) {
                duplicateWarning.classList.add("is-hidden");
            }
            return;
        }

        const response = await fetch(`/api/reference-check?payment_method_id=${paymentInput.value}&reference=${encodeURIComponent(reference)}`);
        if (!response.ok) {
            return;
        }

        const payload = await response.json();
        state.duplicateStatus = payload;
        state.forceSevenValidation = false;
        if (forceSevenButton) {
            forceSevenButton.textContent = "Validar con 7 digitos";
        }
        if (!payload.duplicate || !payload.warning) {
            if (duplicateWarning) {
                duplicateWarning.classList.add("is-hidden");
            }
            if (duplicateModal) {
                duplicateModal.classList.add("is-hidden");
            }
            return;
        }

        if (duplicateWarning) {
            duplicateWarning.classList.remove("is-hidden");
        }
        if (duplicateModal) {
            duplicateModal.classList.remove("is-hidden");
        }
        duplicateContent.innerHTML = [
            `<p>Esta referencia <strong>#${payload.last6}</strong> ya ha sido registrada anteriormente en el sistema para este metodo de pago.</p>`,
            `<div class="duplicate-line"><strong>FECHA:</strong><span>${payload.warning.date}</span></div>`,
            `<div class="duplicate-line"><strong>HORA:</strong><span>${payload.warning.time}</span></div>`,
            `<div class="duplicate-line"><strong>MONTO:</strong><span>USD ${payload.warning.amount_paid_usd} · Bs ${payload.warning.amount_paid_bs}</span></div>`,
            ...payload.warning.items.map((item) => `<div class="duplicate-line"><strong>ITEM:</strong><span>${item.service} / ${item.package}</span></div>`),
        ].join("");

        if (forceSevenButton && payload.can_validate_with_7) {
            forceSevenButton.classList.remove("is-hidden");
        } else if (forceSevenButton) {
            forceSevenButton.classList.add("is-hidden");
        }
    }

    if (verifyPabiloButton) {
        verifyPabiloButton.addEventListener("click", async () => {
            const reference = referenceInput.value.trim();
            if (!reference) {
                showFeedback("Debes colocar la referencia antes de consultar Pabilo.", "error");
                return;
            }

            verifyPabiloButton.disabled = true;
            renderPabiloResult({ ok: true, found: false, verified: false, message: "Consultando Pabilo...", response: {} });
            try {
                const response = await fetch("/api/pabilo/verify-reference", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ reference, payment_method_id: Number(paymentInput.value) }),
                });
                const body = await response.json();
                if (!response.ok) {
                    renderPabiloResult({ ok: false, found: false, verified: false, message: body.detail || "No se pudo consultar Pabilo.", response: body });
                    showFeedback(body.detail || "No se pudo consultar Pabilo.", "error");
                    verifyPabiloButton.disabled = false;
                    return;
                }

                renderPabiloResult(body);
                if (body.payment && body.payment.amount_paid_value) {
                    showFeedback(`Monto capturado desde Pabilo: Bs ${body.payment.amount_paid_value}.`, body.verified ? "success" : "error");
                }
            } catch (error) {
                renderPabiloResult({ ok: false, found: false, verified: false, message: "No se pudo completar la consulta a Pabilo.", response: { error: String(error) } });
                showFeedback("No se pudo completar la consulta a Pabilo.", "error");
            }
            verifyPabiloButton.disabled = false;
        });
    }

    registerButton.addEventListener("click", async () => {
        if (!state.cart.length) {
            showFeedback("Debes agregar al menos un paquete al pedido.", "error");
            return;
        }
        if (!referenceInput.value.trim()) {
            showFeedback("Debes colocar la referencia.", "error");
            return;
        }
        registerButton.disabled = true;
        const payload = {
            payment_method_id: Number(paymentInput.value),
            reference: referenceInput.value.trim(),
            force_seven_validation: state.forceSevenValidation,
            notes: notesInput.value.trim() || null,
            items: state.cart.map((item) => ({ package_id: item.packageId })),
        };

        if (state.pabiloResult && state.pabiloResult.payment && state.pabiloResult.payment.reference === referenceInput.value.trim()) {
            if (state.pabiloResult.payment.amount_paid_value && state.pabiloResult.payment.amount_paid_currency) {
                payload.amount_paid_value = state.pabiloResult.payment.amount_paid_value;
                payload.amount_paid_currency = state.pabiloResult.payment.amount_paid_currency;
            }
            if (state.pabiloResult.payment.verification_id) {
                payload.notes = [payload.notes, `Pabilo ID: ${state.pabiloResult.payment.verification_id}`].filter(Boolean).join(" | ");
            }
        }

        const response = await fetch("/api/sales", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const body = await response.json();

        if (!response.ok) {
            showFeedback(body.detail || "No se pudo registrar la venta.", "error");
            registerButton.disabled = false;
            return;
        }

        state.cart = [];
        state.forceSevenValidation = false;
        clearPabiloResult();
        notesInput.value = "";
        resetReferenceField();
        renderCart();
        prependRecentSale(body.sale);
        showFeedback(`Venta #${body.sale.id} registrada correctamente.`, "success");
        showSaleToast(body.sale.id);
        registerButton.disabled = false;
    });

    function showSaleToast(saleId) {
        document.querySelectorAll(".sale-toast").forEach((item) => item.remove());
        const toast = document.createElement("div");
        toast.className = "sale-toast";
        toast.setAttribute("role", "status");
        toast.innerHTML = `
            <div class="sale-toast__icon">✓</div>
            <strong>Venta registrada con éxito</strong>
            <span>Venta #${escapeHtml(saleId)}</span>
        `;
        document.body.appendChild(toast);
        setTimeout(() => toast.classList.add("is-leaving"), 1700);
        setTimeout(() => toast.remove(), 2000);
    }

    function showFeedback(message, type) {
        feedback.className = `alert alert--${type}`;
        feedback.textContent = message;
        feedback.classList.remove("is-hidden");
    }

    renderServiceChoices();
    renderCart();
    normalizeReference();
    clearPabiloResult();
}

function bindChoiceGroup(groupName, input, callback) {
    const group = document.querySelector(`[data-select-group="${groupName}"]`);
    if (!group || !input) {
        return;
    }

    const buttons = group.querySelectorAll("[data-role='choice-button']");
    const initiallySelected = Array.from(buttons).find((button) => button.classList.contains("is-selected")) || buttons[0];

    buttons.forEach((button) => {
        button.addEventListener("click", () => {
            buttons.forEach((item) => item.classList.remove("is-selected"));
            button.classList.add("is-selected");
            input.value = button.dataset.value;
            if (callback) {
                callback(button.dataset.value);
            }
        });
    });

    if (initiallySelected) {
        buttons.forEach((item) => item.classList.toggle("is-selected", item === initiallySelected));
        input.value = initiallySelected.dataset.value;
        if (callback) {
            callback(initiallySelected.dataset.value);
        }
    }
}

function setupHistoryDetails() {
    const detailButtons = document.querySelectorAll("[data-history-detail-toggle]");
    const editButtons = document.querySelectorAll("[data-history-edit-toggle]");
    if (!detailButtons.length && !editButtons.length) {
        return;
    }

    function setDetailVisibility(saleDetailId, visible) {
        const detailRow = document.querySelector(`[data-sale-detail-row="${saleDetailId}"]`);
        if (!detailRow) {
            return null;
        }

        detailRow.classList.toggle("is-hidden", !visible);

        const detailButton = document.querySelector(`[data-history-detail-toggle][data-sale-detail-id="${saleDetailId}"]`);
        if (detailButton) {
            detailButton.setAttribute("aria-expanded", String(visible));
            detailButton.textContent = visible ? "Ocultar" : "Detalle";
        }

        return detailRow;
    }

    detailButtons.forEach((button) => {
        button.addEventListener("click", () => {
            const saleDetailId = button.dataset.saleDetailId;
            const detailRow = document.querySelector(`[data-sale-detail-row="${saleDetailId}"]`);
            if (!detailRow) {
                return;
            }

            const nextVisible = detailRow.classList.contains("is-hidden");
            setDetailVisibility(saleDetailId, nextVisible);

            if (!nextVisible) {
                detailRow.querySelectorAll("[data-sale-edit-row]").forEach((editRow) => {
                    editRow.classList.add("is-hidden");
                });
                const editButton = document.querySelector(`[data-history-edit-toggle][data-sale-detail-id="${saleDetailId}"]`);
                if (editButton) {
                    editButton.setAttribute("aria-expanded", "false");
                    editButton.textContent = "Editar";
                }
            }
        });
    });

    editButtons.forEach((button) => {
        button.addEventListener("click", () => {
            const saleDetailId = button.dataset.saleDetailId;
            const saleEditId = button.dataset.saleEditId;
            const detailRow = setDetailVisibility(saleDetailId, true);
            if (!detailRow) {
                return;
            }

            const editSection = detailRow.querySelector(`[data-sale-edit-row="${saleEditId}"]`);
            if (!editSection) {
                return;
            }

            const shouldShow = editSection.classList.contains("is-hidden");
            editSection.classList.toggle("is-hidden", !shouldShow);
            button.setAttribute("aria-expanded", String(shouldShow));
            button.textContent = shouldShow ? "Cancelar" : "Editar";
        });
    });
}