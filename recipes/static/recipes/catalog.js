document.addEventListener('DOMContentLoaded', () => {
    const catalogDataEl = document.getElementById('product-catalog-data');
    if (!catalogDataEl) return;
    const catalogData = JSON.parse(catalogDataEl.textContent);

    const searchInput = document.getElementById('catalog-search');
    const categoryBtns = document.querySelectorAll('.filter-btn');
    const productCards = document.querySelectorAll('.product-card');
    const selectedContainer = document.getElementById('catalog-selected');
    const countEl = document.getElementById('catalog-count');
    const clearBtn = document.getElementById('catalog-clear');
    const emptyMsg = document.getElementById('catalog-empty');
    const promptInput = document.getElementById('prompt');
    const suggestBtn = document.getElementById('suggest-btn');
    const suggestWaiting = document.getElementById('suggest-waiting');

    let currentCategory = 'all';
    let currentSearch = '';

    function updateCount() {
        const checked = document.querySelectorAll('input[name="selected_products"]:checked');
        if (countEl) {
            countEl.textContent = checked.length;
        }
    }

    function renderTags() {
        if (!selectedContainer) return;
        selectedContainer.innerHTML = '';
        const checked = document.querySelectorAll('input[name="selected_products"]:checked');
        
        checked.forEach(checkbox => {
            const product = catalogData.products.find(p => p.id === checkbox.value);
            if (!product) return;

            const tag = document.createElement('div');
            tag.className = 'tag';
            
            const nameSpan = document.createElement('span');
            nameSpan.textContent = product[catalogData.language] || product.kk;
            
            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.textContent = '×';
            removeBtn.setAttribute('aria-label', 'Remove');
            removeBtn.addEventListener('click', () => {
                checkbox.checked = false;
                checkbox.dispatchEvent(new Event('change', { bubbles: true }));
            });

            tag.appendChild(nameSpan);
            tag.appendChild(removeBtn);
            selectedContainer.appendChild(tag);
        });
        updateCount();
    }

    function applyFilter() {
        let visibleCount = 0;
        productCards.forEach(card => {
            const id = card.getAttribute('data-product-id');
            const category = card.getAttribute('data-category');
            const product = catalogData.products.find(p => p.id === id);
            
            let matchCategory = (currentCategory === 'all' || category === currentCategory);
            
            let matchSearch = false;
            if (!currentSearch) {
                matchSearch = true;
            } else if (product) {
                const nameKk = (product.kk || '').toLowerCase();
                const nameRu = (product.ru || '').toLowerCase();
                matchSearch = nameKk.includes(currentSearch) || nameRu.includes(currentSearch);
            }

            if (matchCategory && matchSearch) {
                card.hidden = false;
                visibleCount++;
            } else {
                card.hidden = true;
            }
        });

        if (emptyMsg) {
            emptyMsg.hidden = visibleCount > 0;
        }
    }

    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            currentSearch = e.target.value.trim().toLowerCase();
            applyFilter();
        });
    }

    categoryBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            categoryBtns.forEach(b => b.setAttribute('aria-pressed', 'false'));
            btn.setAttribute('aria-pressed', 'true');
            currentCategory = btn.getAttribute('data-category');
            applyFilter();
        });
    });

    document.querySelectorAll('input[name="selected_products"]').forEach(checkbox => {
        checkbox.addEventListener('change', () => {
            renderTags();
        });
    });

    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            document.querySelectorAll('input[name="selected_products"]:checked').forEach(checkbox => {
                checkbox.checked = false;
            });
            renderTags();
        });
    }

    // ── Сұрау өрісі: 3–300 таңба клиент жағында шектеу ──
    if (promptInput) {
        promptInput.addEventListener('input', () => {
            if (promptInput.value.length > 300) {
                promptInput.value = promptInput.value.slice(0, 300);
            }
        });
    }

    // ── «Ұсыныс алу»: күту хабары + батырманың уақытша сөнуі ──
    if (suggestBtn) {
        suggestBtn.addEventListener('click', (e) => {
            const value = promptInput ? promptInput.value.trim() : '';
            if (value.length < 3 || value.length > 300) {
                e.preventDefault();
                if (promptInput) promptInput.focus();
                return;
            }
            if (suggestWaiting) suggestWaiting.hidden = false;
            suggestBtn.style.opacity = '0.4';
            suggestBtn.style.pointerEvents = 'none';
            suggestBtn.disabled = true;
        });
    }

    renderTags();
    applyFilter();
});