(function() {
    var CONTAINER_CLS = 'fixed top-5 right-5 z-50 flex flex-col gap-2';
    var LEVEL_CLS = {
        error: 'border-red-800/50 bg-red-950/80 text-red-200',
        success: 'border-emerald-800/50 bg-emerald-950/80 text-emerald-200',
        warning: 'border-amber-800/50 bg-amber-950/80 text-amber-200',
    };
    var BASE_CLS = 'animate-fade-up rounded-lg border px-4 py-3 text-sm shadow-lg backdrop-blur-sm';
    var DEFAULT_CLS = 'border-gray-800/50 bg-gray-900/80 text-gray-300';

    function dismissEl(el) {
        el.style.transition = 'opacity 0.3s, transform 0.3s';
        el.style.opacity = '0';
        el.style.transform = 'translateY(-8px)';
        setTimeout(function() { el.remove(); }, 300);
    }

    function getContainer() {
        var c = document.getElementById('messages');
        if (!c) {
            c = document.createElement('div');
            c.id = 'messages';
            c.className = CONTAINER_CLS;
            document.body.appendChild(c);
        }
        return c;
    }

    window.showToast = function(message, level) {
        var el = document.createElement('div');
        el.className = BASE_CLS + ' ' + (LEVEL_CLS[level] || DEFAULT_CLS);
        el.textContent = message;
        getContainer().appendChild(el);
        setTimeout(function() { dismissEl(el); }, 5000);
    };

    window.dismissToasts = function() {
        document.querySelectorAll('#messages > div').forEach(function(el, i) {
            setTimeout(function() { dismissEl(el); }, i * 100);
        });
    };
})();
