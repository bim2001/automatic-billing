// Tenant-only active navigation and sidebar state.
document.addEventListener('DOMContentLoaded', function () {
    const sidebar = document.querySelector('.tenant-sidebar');
    const toggle = document.querySelector('.tenant-sidebar-toggle');
    const closeButton = document.querySelector('.tenant-sidebar-close');
    const backdrop = document.querySelector('.tenant-sidebar-backdrop');
    const navMenu = sidebar && sidebar.querySelector('.nav-menu');
    const mobileViewport = window.matchMedia('(max-width: 768px)');
    if (!sidebar) return;

    const desktopScrollKey = 'tenant-sidebar-desktop-scroll';
    const mobileScrollKey = 'tenant-sidebar-mobile-nav-scroll';
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

    function setOpen(open, restoreToggleFocus = false) {
        if (open) lockBodyScroll();
        document.body.classList.toggle('tenant-sidebar-open', open);
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

    setOpen(false);

    if (toggle) toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('tenant-sidebar-open')));
    if (closeButton) closeButton.addEventListener('click', () => setOpen(false, true));
    if (backdrop) backdrop.addEventListener('click', () => setOpen(false, true));
    document.addEventListener('keydown', event => {
        if (!document.body.classList.contains('tenant-sidebar-open')) return;
        if (event.key === 'Escape') {
            setOpen(false, true);
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
        if (mobileViewport.matches) setOpen(false);
    }));

    const handleViewportChange = event => {
        restoreSidebarScroll();
        if (!event.matches) setOpen(false);
    };
    if (mobileViewport.addEventListener) mobileViewport.addEventListener('change', handleViewportChange);
    else mobileViewport.addListener(handleViewportChange);
});
