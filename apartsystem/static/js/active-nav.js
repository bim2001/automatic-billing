// Keep Owner navigation highlighting and its scroll position consistent across pages.
document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.querySelector('.sidebar');
    const toggle = document.querySelector('.sidebar-toggle');
    const closeButton = document.querySelector('.sidebar-close');
    const backdrop = document.querySelector('.sidebar-backdrop');

    if (!sidebar) return;

    const scrollKey = 'owner-sidebar-scroll';
    const savedScroll = sessionStorage.getItem(scrollKey);
    if (savedScroll !== null) {
        sidebar.scrollTop = Number(savedScroll) || 0;
    }

    sidebar.addEventListener('scroll', function () {
        sessionStorage.setItem(scrollKey, String(sidebar.scrollTop));
    }, { passive: true });

    const path = window.location.pathname.replace(/\/+$/, '') || '/';
    const routeMatchers = {
        dashboard: /^\/dashboard$/,
        monitoring: /^\/monitoring$/,
        tenants: /^\/tenants$/,
        billing: /^\/billing$/,
        history: /^\/billing\/history$/,
        alerts: /^\/alerts$/,
        settings: /^\/settings$/,
        health: /^\/(?:health|system-health)$/,
        activity: /^\/activity-log$/
    };
    let activeKey = Object.keys(routeMatchers).find(key => routeMatchers[key].test(path));
    if (/^\/rooms?$/.test(path) || /^\/room\/(?:add|edit|delete)(?:\/|$)/.test(path)) {
        activeKey = 'rooms';
    }

    sidebar.querySelectorAll('.nav-link[data-nav]').forEach(link => {
        const active = link.dataset.nav === activeKey;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    });

    function setMobileOpen(open) {
        document.body.classList.toggle('sidebar-open', open);
        if (toggle) {
            toggle.setAttribute('aria-expanded', String(open));
            toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
        }
        if (open) sidebar.focus({ preventScroll: true });
    }

    if (toggle) toggle.addEventListener('click', () => setMobileOpen(!document.body.classList.contains('sidebar-open')));
    if (closeButton) closeButton.addEventListener('click', () => {
        setMobileOpen(false);
        if (toggle) toggle.focus();
    });
    if (backdrop) backdrop.addEventListener('click', () => setMobileOpen(false));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
            setMobileOpen(false);
            if (toggle) toggle.focus();
        }
    });
    sidebar.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
        if (window.matchMedia('(max-width: 768px)').matches) setMobileOpen(false);
    }));
});
