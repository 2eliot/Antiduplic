document.addEventListener("DOMContentLoaded", () => {
    setupSidebar();
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

function setupSidebar() {
    const shell = document.querySelector(".shell");
    const sidebar = document.querySelector("[data-sidebar]");
    const toggle = document.querySelector("[data-sidebar-toggle]");
    if (!sidebar || !toggle || !shell) {
        return;
    }

    function syncShellState() {
        shell.classList.toggle("shell--sidebar-collapsed", sidebar.classList.contains("is-collapsed"));
    }

    toggle.addEventListener("click", () => {
        sidebar.classList.toggle("is-collapsed");
        toggle.textContent = sidebar.classList.contains("is-collapsed") ? "⟩" : "⟨";
        syncShellState();
    });

    syncShellState();
}

function setupDashboard() {
    const root = document.querySelector("[data-dashboard-root]");
    if (!root) {
        return;
    }

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
    const catalog = JSON.parse(catalogElement.textContent || "[]");

    const state = {
        selectedServiceId: Number(serviceInput.value),
        cart: [],
        forceSevenValidation: false,
        duplicateStatus: null,
    };

    bindChoiceGroup("payment-methods", paymentInput, () => {
        if (referenceInput.value.trim()) {
            checkReference();
        }
    });
    bindChoiceGroup("services", serviceInput, (value) => {
        state.selectedServiceId = Number(value);
        renderPackages();
    });

    function renderPackages() {
        const activeService = catalog.find((service) => service.id === state.selectedServiceId) || catalog[0];
        if (!activeService) {
            packageList.innerHTML = "<p class='empty-state'>No hay paquetes activos.</p>";
            return;
        }

        packageList.innerHTML = "";
        activeService.packages.forEach((pkg) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "package-button";
            button.innerHTML = `<span class="package-button__name">${escapeHtml(pkg.name)}</span><strong>${escapeHtml(pkg.display_price)}</strong>`;
            button.addEventListener("click", () => addPackage(pkg, activeService, button));
            packageList.appendChild(button);
        });
    }

    function addPackage(pkg, service, button) {
        state.cart.push({
            packageId: pkg.id,
            serviceName: service.name,
            packageName: pkg.name,
            usdPrice: Number(pkg.usd_price),
            bsPrice: Number(pkg.bs_price),
            displayPrice: pkg.display_price,
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
    }

    function renderCart() {
        if (!state.cart.length) {
            cartItems.innerHTML = "<p class='empty-state'>Agrega servicios y paquetes para armar el pedido.</p>";
            totalUsd.textContent = "0.00";
            totalBs.textContent = "0.00";
            return;
        }

        cartItems.innerHTML = "";
        const usdTotal = state.cart.reduce((sum, item) => sum + item.usdPrice, 0);
        const bsTotal = state.cart.reduce((sum, item) => sum + item.bsPrice, 0);

        state.cart.forEach((item, index) => {
            const article = document.createElement("article");
            article.className = "cart-item";
            article.innerHTML = `
                <div class="cart-item__meta">
                    <strong>${item.serviceName}</strong>
                    <span>${item.packageName}</span>
                </div>
                <div class="cart-item__meta">
                    <strong>${item.displayPrice}</strong>
                    <button type="button" class="icon-button remove-button" aria-label="Quitar item">×</button>
                </div>
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
        }
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
        notesInput.value = "";
        resetReferenceField();
        renderCart();
        prependRecentSale(body.sale);
        showFeedback(`Venta #${body.sale.id} registrada correctamente.`, "success");
        registerButton.disabled = false;
    });

    function showFeedback(message, type) {
        feedback.className = `alert alert--${type}`;
        feedback.textContent = message;
        feedback.classList.remove("is-hidden");
    }

    renderPackages();
    renderCart();
    normalizeReference();
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
    const buttons = document.querySelectorAll("[data-history-detail-toggle]");
    if (!buttons.length) {
        return;
    }

    buttons.forEach((button) => {
        button.addEventListener("click", () => {
            const saleDetailId = button.dataset.saleDetailId;
            const detailRow = document.querySelector(`[data-sale-detail-row="${saleDetailId}"]`);
            if (!detailRow) {
                return;
            }
            const isHidden = detailRow.classList.toggle("is-hidden");
            button.setAttribute("aria-expanded", String(!isHidden));
            button.textContent = isHidden ? "Detalle" : "Ocultar";
        });
    });
}