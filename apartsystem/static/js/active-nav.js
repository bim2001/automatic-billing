// Keep Owner navigation highlighting and its scroll position consistent across pages.
document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.querySelector('.sidebar');
    const toggle = document.querySelector('.sidebar-toggle');
    const closeButton = document.querySelector('.sidebar-close');
    const backdrop = document.querySelector('.sidebar-backdrop');
    const navMenu = sidebar && sidebar.querySelector('.nav-menu');
    const mobileViewport = window.matchMedia('(max-width: 768px)');

    if (!sidebar) return;

    const desktopScrollKey = 'owner-sidebar-desktop-scroll';
    const mobileScrollKey = 'owner-sidebar-mobile-nav-scroll';
    let bodyLockStyles = null;
    let lockedScrollY = 0;

    function lockBodyScroll() {
        if (bodyLockStyles !== null) return;
        lockedScrollY = window.scrollY || window.pageYOffset;
        bodyLockStyles = {
            position: document.body.style.position,
            top: document.body.style.top,
            left: document.body.style.left,
            right: document.body.style.right,
            width: document.body.style.width
        };
        document.body.style.position = 'fixed';
        document.body.style.top = `-${lockedScrollY}px`;
        document.body.style.left = '0';
        document.body.style.right = '0';
        document.body.style.width = '100%';
    }

    function unlockBodyScroll() {
        if (bodyLockStyles === null) return;
        document.body.style.position = bodyLockStyles.position;
        document.body.style.top = bodyLockStyles.top;
        document.body.style.left = bodyLockStyles.left;
        document.body.style.right = bodyLockStyles.right;
        document.body.style.width = bodyLockStyles.width;
        bodyLockStyles = null;
        window.scrollTo(0, lockedScrollY);
    }

    function activeScrollContainer() {
        return mobileViewport.matches && navMenu ? navMenu : sidebar;
    }

    function restoreSidebarScroll() {
        const scrollKey = mobileViewport.matches ? mobileScrollKey : desktopScrollKey;
        const savedScroll = sessionStorage.getItem(scrollKey);
        if (savedScroll !== null) activeScrollContainer().scrollTop = Number(savedScroll) || 0;
    }

    restoreSidebarScroll();

    sidebar.addEventListener('scroll', function () {
        if (!mobileViewport.matches) sessionStorage.setItem(desktopScrollKey, String(sidebar.scrollTop));
    }, { passive: true });
    if (navMenu) navMenu.addEventListener('scroll', function () {
        if (mobileViewport.matches) sessionStorage.setItem(mobileScrollKey, String(navMenu.scrollTop));
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

    function setMobileOpen(open, restoreToggleFocus = false) {
        if (open) lockBodyScroll();
        document.body.classList.toggle('sidebar-open', open);
        if (!open) unlockBodyScroll();
        if (toggle) {
            toggle.setAttribute('aria-expanded', String(open));
            toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
        }
        sidebar.setAttribute('aria-hidden', String(mobileViewport.matches && !open));
        if (open) {
            sidebar.focus({ preventScroll: true });
            if (closeButton) closeButton.focus({ preventScroll: true });
        } else if (restoreToggleFocus && toggle) {
            toggle.focus({ preventScroll: true });
        }
    }

    setMobileOpen(false);

    if (toggle) toggle.addEventListener('click', () => setMobileOpen(!document.body.classList.contains('sidebar-open')));
    if (closeButton) closeButton.addEventListener('click', () => setMobileOpen(false, true));
    if (backdrop) backdrop.addEventListener('click', () => setMobileOpen(false, true));
    document.addEventListener('keydown', event => {
        if (!document.body.classList.contains('sidebar-open')) return;
        if (event.key === 'Escape') {
            setMobileOpen(false, true);
            return;
        }
        if (event.key === 'Tab') {
            const focusable = sidebar.querySelectorAll('a[href], button:not([disabled])');
            if (!focusable.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) {
                event.preventDefault();
                first.focus();
            }
        }
    });
    sidebar.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
        if (mobileViewport.matches) setMobileOpen(false);
    }));

    const handleViewportChange = event => {
        restoreSidebarScroll();
        if (!event.matches) setMobileOpen(false);
    };
    if (mobileViewport.addEventListener) mobileViewport.addEventListener('change', handleViewportChange);
    else mobileViewport.addListener(handleViewportChange);
});
