/**
 * 主题管理模块
 * 负责亮色/暗色主题切换、Logo 适配和 UI 状态更新
 */
function createThemeModule(app) {
    return {
        // 初始化主题
        initTheme() {
            const savedTheme = localStorage.getItem('theme');
            if (savedTheme) {
                document.documentElement.setAttribute('data-theme', savedTheme);
            }
            this.updateThemeUI();
            this.updateWelcomeLogo();
        },

        // 切换主题
        toggleTheme() {
            const current = document.documentElement.getAttribute('data-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('theme', next);
            this.updateThemeUI();
            this.updateWelcomeLogo();
        },

        // 根据当前主题更新欢迎页 Logo
        updateWelcomeLogo() {
            const logoEl = document.getElementById('welcomeLogo');
            if (!logoEl) return;
            const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
            const svgSrc = isDark
                ? '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80" width="100" height="100"><defs><radialGradient id="ec" cx="45%" cy="40%" r="50%"><stop offset="0%" stop-color="#FFFFFF"/><stop offset="15%" stop-color="#F5B8FF"/><stop offset="35%" stop-color="#c770db"/><stop offset="60%" stop-color="#0a84ff"/><stop offset="85%" stop-color="#0055b8"/><stop offset="100%" stop-color="#003d82"/></radialGradient><radialGradient id="eg" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="#f093fb" stop-opacity="0.6"/><stop offset="30%" stop-color="#c770db" stop-opacity="0.35"/><stop offset="60%" stop-color="#5ac8fa" stop-opacity="0.15"/><stop offset="100%" stop-color="#0a84ff" stop-opacity="0"/></radialGradient><radialGradient id="er" cx="50%" cy="50%" r="50%"><stop offset="70%" stop-color="#c770db" stop-opacity="0"/><stop offset="85%" stop-color="#f093fb" stop-opacity="0.4"/><stop offset="100%" stop-color="#0a84ff" stop-opacity="0"/></radialGradient><linearGradient id="rg1" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#f093fb"/><stop offset="40%" stop-color="#c770db"/><stop offset="100%" stop-color="#0a84ff"/></linearGradient><linearGradient id="rg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#f093fb"/><stop offset="50%" stop-color="#5c3a82"/><stop offset="100%" stop-color="#3a8ad4"/></linearGradient><linearGradient id="rg3" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#00c4cc"/><stop offset="50%" stop-color="#c770db"/><stop offset="100%" stop-color="#f093fb"/></linearGradient><linearGradient id="pl" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#f093fb" stop-opacity="0.2"/><stop offset="30%" stop-color="#c770db" stop-opacity="0.75"/><stop offset="50%" stop-color="#0a84ff" stop-opacity="0.95"/><stop offset="70%" stop-color="#c770db" stop-opacity="0.75"/><stop offset="100%" stop-color="#f093fb" stop-opacity="0.2"/></linearGradient><filter id="gl"><feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><filter id="sg"><feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><filter id="es"><feDropShadow dx="0" dy="0" stdDeviation="6" flood-color="#c770db" flood-opacity="0.5"/></filter></defs><circle cx="40" cy="40" r="36" fill="none" stroke="url(#rg2)" stroke-width="2" opacity="0.15"><animate attributeName="r" values="23;37" dur="4s" repeatCount="indefinite" begin="1s"/><animate attributeName="opacity" values="0.3;0" dur="4s" repeatCount="indefinite" begin="1s"/></circle><circle cx="40" cy="40" r="29" fill="none" stroke="url(#rg3)" stroke-width="3" opacity="0.25"><animate attributeName="r" values="18;31" dur="3.2s" repeatCount="indefinite" begin="0.5s"/><animate attributeName="opacity" values="0.45;0" dur="3.2s" repeatCount="indefinite" begin="0.5s"/></circle><circle cx="40" cy="40" r="22" fill="none" stroke="url(#rg1)" stroke-width="4" opacity="0.35"><animate attributeName="r" values="13;25" dur="2.6s" repeatCount="indefinite" begin="0s"/><animate attributeName="opacity" values="0.6;0" dur="2.6s" repeatCount="indefinite" begin="0s"/></circle><path d="M 11,40 L 33,40 L 37,32 L 41,56 L 45,36 L 49,52 L 53,40 L 89,40" fill="none" stroke="url(#pl)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" filter="url(#sg)"><animate attributeName="opacity" values="0.3;0.75;0.3" dur="2.6s" repeatCount="indefinite"/></path><circle cx="40" cy="40" r="18" fill="url(#eg)"><animate attributeName="r" values="16;19;16" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="14" fill="url(#er)"><animate attributeName="r" values="13;15;13" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="11" fill="url(#ec)" filter="url(#es)"><animate attributeName="r" values="10;11.5;10" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="7" fill="none" stroke="#FFFFFF" stroke-width="0.8" opacity="0.2"><animate attributeName="opacity" values="0.1;0.25;0.1" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="4" fill="#1a0525" opacity="0.8"><animate attributeName="r" values="3.5;4.5;3.5" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="2" fill="#c770db" opacity="0.4"><animate attributeName="opacity" values="0.2;0.5;0.2" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="36.5" cy="36.5" r="3" fill="#FFFFFF" opacity="0.75"><animate attributeName="opacity" values="0.5;0.9;0.5" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="43" cy="43.5" r="1.25" fill="#FFFFFF" opacity="0.4"><animate attributeName="opacity" values="0.2;0.5;0.2" dur="2.8s" repeatCount="indefinite" begin="0.4s"/></circle></svg>'
                : '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80" width="100" height="100"><defs><radialGradient id="ec" cx="45%" cy="40%" r="50%"><stop offset="0%" stop-color="#FFFFFF"/><stop offset="15%" stop-color="#F5B8FF"/><stop offset="35%" stop-color="#c770db"/><stop offset="60%" stop-color="#0071e3"/><stop offset="85%" stop-color="#005bb5"/><stop offset="100%" stop-color="#004080"/></radialGradient><radialGradient id="eg" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="#f093fb" stop-opacity="0.55"/><stop offset="30%" stop-color="#c770db" stop-opacity="0.3"/><stop offset="60%" stop-color="#4facfe" stop-opacity="0.12"/><stop offset="100%" stop-color="#0071e3" stop-opacity="0"/></radialGradient><radialGradient id="er" cx="50%" cy="50%" r="50%"><stop offset="70%" stop-color="#c770db" stop-opacity="0"/><stop offset="85%" stop-color="#f093fb" stop-opacity="0.35"/><stop offset="100%" stop-color="#0071e3" stop-opacity="0"/></radialGradient><linearGradient id="rg1" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#f093fb"/><stop offset="40%" stop-color="#c770db"/><stop offset="100%" stop-color="#0071e3"/></linearGradient><linearGradient id="rg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#f093fb"/><stop offset="50%" stop-color="#764ba2"/><stop offset="100%" stop-color="#4facfe"/></linearGradient><linearGradient id="rg3" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#00f2fe"/><stop offset="50%" stop-color="#f093fb"/><stop offset="100%" stop-color="#667eea"/></linearGradient><linearGradient id="pl" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#f093fb" stop-opacity="0.2"/><stop offset="30%" stop-color="#c770db" stop-opacity="0.7"/><stop offset="50%" stop-color="#0071e3" stop-opacity="0.9"/><stop offset="70%" stop-color="#c770db" stop-opacity="0.7"/><stop offset="100%" stop-color="#f093fb" stop-opacity="0.2"/></linearGradient><filter id="gl"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><filter id="sg"><feGaussianBlur stdDeviation="1.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter><filter id="es"><feDropShadow dx="0" dy="2" stdDeviation="5" flood-color="#c770db" flood-opacity="0.35"/></filter></defs><circle cx="40" cy="40" r="36" fill="none" stroke="url(#rg2)" stroke-width="2" opacity="0.18"><animate attributeName="r" values="23;37" dur="4s" repeatCount="indefinite" begin="1s"/><animate attributeName="opacity" values="0.35;0" dur="4s" repeatCount="indefinite" begin="1s"/></circle><circle cx="40" cy="40" r="29" fill="none" stroke="url(#rg3)" stroke-width="3" opacity="0.28"><animate attributeName="r" values="18;31" dur="3.2s" repeatCount="indefinite" begin="0.5s"/><animate attributeName="opacity" values="0.5;0" dur="3.2s" repeatCount="indefinite" begin="0.5s"/></circle><circle cx="40" cy="40" r="22" fill="none" stroke="url(#rg1)" stroke-width="4" opacity="0.38"><animate attributeName="r" values="13;25" dur="2.6s" repeatCount="indefinite" begin="0s"/><animate attributeName="opacity" values="0.65;0" dur="2.6s" repeatCount="indefinite" begin="0s"/></circle><path d="M 11,40 L 33,40 L 37,32 L 41,56 L 45,36 L 49,52 L 53,40 L 89,40" fill="none" stroke="url(#pl)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" filter="url(#sg)"><animate attributeName="opacity" values="0.3;0.75;0.3" dur="2.6s" repeatCount="indefinite"/></path><circle cx="40" cy="40" r="18" fill="url(#eg)"><animate attributeName="r" values="16;19;16" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="14" fill="url(#er)"><animate attributeName="r" values="13;15;13" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="11" fill="url(#ec)" filter="url(#es)"><animate attributeName="r" values="10;11.5;10" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="7" fill="none" stroke="#FFFFFF" stroke-width="0.8" opacity="0.22"><animate attributeName="opacity" values="0.12;0.28;0.12" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="4" fill="#1a0525" opacity="0.75"><animate attributeName="r" values="3.5;4.5;3.5" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="40" cy="40" r="2" fill="#c770db" opacity="0.35"><animate attributeName="opacity" values="0.15;0.45;0.15" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="36.5" cy="36.5" r="3" fill="#FFFFFF" opacity="0.8"><animate attributeName="opacity" values="0.55;0.95;0.55" dur="2.8s" repeatCount="indefinite"/></circle><circle cx="43" cy="43.5" r="1.25" fill="#FFFFFF" opacity="0.45"><animate attributeName="opacity" values="0.25;0.55;0.25" dur="2.8s" repeatCount="indefinite" begin="0.4s"/></circle></svg>';
            logoEl.innerHTML = svgSrc;
        },

        // 更新主题切换按钮 UI
        updateThemeUI() {
            const isDark = document.documentElement.getAttribute('data-theme') === 'dark' ||
                (!document.documentElement.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);

            if (app.themeText) {
                app.themeText.textContent = isDark ? '浅色模式' : '深色模式';
            }

            const lightIcon = document.querySelector('.theme-icon-light');
            const darkIcon = document.querySelector('.theme-icon-dark');
            if (lightIcon && darkIcon) {
                lightIcon.style.display = isDark ? 'none' : 'block';
                darkIcon.style.display = isDark ? 'block' : 'none';
            }

            // 更新 theme-color meta
            const metaTheme = document.querySelector('meta[name="theme-color"]');
            if (metaTheme) {
                metaTheme.setAttribute('content', isDark ? '#0d0d0d' : '#1a1a2e');
            }
        }
    };
}
