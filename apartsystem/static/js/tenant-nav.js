// Tenant-only active navigation and sidebar state.
document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.querySelector('.tenant-sidebar');
    const toggle = document.querySelector('.tenant-sidebar-toggle');
    const closeButton = document.querySelector('.tenant-sidebar-close');
    const backdrop = document.querySelector('.tenant-sidebar-backdrop');
    if (!sidebar) return;

    const storageKey = 'tenant-sidebar-scroll';
    const savedScroll = sessionStorage.getItem(storageKey);
    if (savedScroll !== null) sidebar.scrollTop = Number(savedScroll) || 0;
    sidebar.addEventListener('scroll', function () {
        sessionStorage.setItem(storageKey, String(sidebar.scrollTop));
    }, { passive: true });

    const path = window.location.pathname.replace(/\/+$/, '') || '/';
    const tenantRoutes = {
        dashboard: /^\/tenant$/,
        payment: /^\/payment$/,
        notifications: /^\/tenant\/notifications$/,
        history: /^\/tenant\/billing-history$/,
        profile: /^\/edit-profile$/
    };
    const activeKey = Object.keys(tenantRoutes).find(key => tenantRoutes[key].test(path));
    sidebar.querySelectorAll('[data-tenant-nav]').forEach(link => {
        const active = link.dataset.tenantNav === activeKey;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    });

    function setOpen(open) {
        document.body.classList.toggle('tenant-sidebar-open', open);
        if (toggle) {
            toggle.setAttribute('aria-expanded', String(open));
            toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
        }
        if (open) sidebar.focus({ preventScroll: true });
    }

    if (toggle) toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('tenant-sidebar-open')));
    if (closeButton) closeButton.addEventListener('click', () => {
        setOpen(false);
        if (toggle) toggle.focus();
    });
    if (backdrop) backdrop.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && document.body.classList.contains('tenant-sidebar-open')) {
            setOpen(false);
            if (toggle) toggle.focus();
        }
    });
    sidebar.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
        if (window.matchMedia('(max-width: 768px)').matches) setOpen(false);
    }));
});
