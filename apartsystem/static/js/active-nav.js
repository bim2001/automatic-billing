// active-nav.js - Automatic active highlight for sidebar navigation
document.addEventListener('DOMContentLoaded', function() {
    const currentPath = window.location.pathname;
    console.log("Current path:", currentPath); // Debug: tignan sa browser console
    
    const navLinks = document.querySelectorAll('.nav-link');
    
    // Remove active class from all links
    navLinks.forEach(link => {
        link.classList.remove('active');
    });
    
    // Map URL patterns - specific to each page
    // Gamitin ang exact na URL patterns
    const patterns = [
        { match: '/dashboard', selector: 'a[href*="/dashboard"]' },
        { match: '/monitoring', selector: 'a[href*="/monitoring"]' },
        { match: '/room/add', selector: 'a[href*="/room/add"]' },
        { match: '/room/add', selector: 'a[href*="add_room"]' }, // fallback
        { match: '/tenants', selector: 'a[href*="/tenants"]' },
        { match: '/tenant_list', selector: 'a[href*="tenant_list"]' }, // fallback
        { match: '/billing/history', selector: 'a[href*="/billing/history"]' },
        { match: '/billing/history', selector: 'a[href*="billing_history"]' }, // fallback
        { match: '/billing', selector: 'a[href*="/billing"]:not([href*="/billing/history"])' },
        { match: '/alerts', selector: 'a[href*="/alerts"]' },
        { match: '/settings', selector: 'a[href*="/settings"]' },
        { match: '/system-health', selector: 'a[href*="/system-health"]' },
        { match: '/health', selector: 'a[href*="health"]' } // fallback for health
    ];
    
    let found = false;
    
    for (const pattern of patterns) {
        if (currentPath.includes(pattern.match)) {
            console.log("Matching pattern:", pattern.match); // Debug
            const activeLink = document.querySelector(pattern.selector);
            if (activeLink) {
                console.log("Found link, adding active class"); // Debug
                activeLink.classList.add('active');
                found = true;
                break;
            }
        }
    }
    
    // Special cases
    if (!found) {
        // Rooms page (add_room)
        if (currentPath.includes('add_room') || currentPath.includes('room/add')) {
            const roomsLink = document.querySelector('a[href*="add_room"], a[href*="room/add"]');
            if (roomsLink) roomsLink.classList.add('active');
        }
        // Tenants page
        else if (currentPath.includes('tenants') || currentPath.includes('tenant_list')) {
            const tenantsLink = document.querySelector('a[href*="tenant_list"], a[href*="/tenants"]');
            if (tenantsLink) tenantsLink.classList.add('active');
        }
        // History page
        else if (currentPath.includes('billing_history') || currentPath.includes('billing/history')) {
            const historyLink = document.querySelector('a[href*="billing_history"], a[href*="/billing/history"]');
            if (historyLink) historyLink.classList.add('active');
        }
        // Health page
        else if (currentPath.includes('system-health') || currentPath.includes('health')) {
            const healthLink = document.querySelector('a[href*="health"], a[href*="system-health"]');
            if (healthLink) healthLink.classList.add('active');
        }
    }
    
    // Dashboard root path
    if (currentPath === '/' || currentPath === '/dashboard') {
        const dashboardLink = document.querySelector('a[href*="/dashboard"], a[href*="dashboard"]');
        if (dashboardLink) dashboardLink.classList.add('active');
    }
});