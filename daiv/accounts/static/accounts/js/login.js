(function () {
    var STORAGE_KEY = 'daiv:last-login-method';
    var BADGE_CLS = 'pointer-events-none absolute -top-2 right-3 rounded-full border border-white/[0.12] ' +
        'bg-[#1E2733] px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-gray-300';

    function readLastMethod() {
        try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
    }

    function rememberMethod(method) {
        try { localStorage.setItem(STORAGE_KEY, method); } catch (e) { /* storage blocked */ }
    }

    function markLastUsed() {
        var method = readLastMethod();
        if (!method) return;
        var el = document.querySelector('[data-login-method="' + CSS.escape(method) + '"]');
        if (el && el.tagName === 'FORM') el = el.querySelector('[type="submit"]');
        if (!el) return;
        var badge = document.createElement('span');
        badge.className = BADGE_CLS;
        badge.textContent = 'Last used';
        el.classList.add('relative');
        el.appendChild(badge);
    }

    function trackChoices() {
        document.querySelectorAll('[data-login-method]').forEach(function (el) {
            var method = el.dataset.loginMethod;
            el.addEventListener(el.tagName === 'FORM' ? 'submit' : 'click', function () {
                rememberMethod(method);
            });
        });
    }

    async function startPasskeyAutofill() {
        var form = document.getElementById('mfa_login');
        var credentialInput = document.getElementById('mfa_credential');
        var button = document.getElementById('passkey_login');
        if (!form || !credentialInput || !button || !window.webauthnJSON) return;
        if (!window.PublicKeyCredential || !PublicKeyCredential.isConditionalMediationAvailable) return;
        if (!(await PublicKeyCredential.isConditionalMediationAvailable())) return;

        var controller = new AbortController();
        // Capture phase runs before allauth's button handler: the browser allows only one
        // pending WebAuthn request, so the autofill one must be aborted before the modal one.
        document.addEventListener('click', function (e) {
            if (e.target.closest('#passkey_login')) controller.abort();
        }, true);

        try {
            var response = await fetch(form.action, { headers: { Accept: 'application/json' } });
            if (!response.ok) return;
            var data = await response.json();
            var credential = await window.webauthnJSON.get(Object.assign({}, data.request_options, {
                mediation: 'conditional',
                signal: controller.signal,
            }));
            credentialInput.value = JSON.stringify(credential);
            rememberMethod('passkey');
            form.submit();
        } catch (e) {
            if (e.name !== 'AbortError') console.error(e);
        }
    }

    markLastUsed();
    trackChoices();
    startPasskeyAutofill();
})();
