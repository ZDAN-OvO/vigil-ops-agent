/**
 * VigilOpsAgent 前端应用 — 主编排模块
 * 负责应用初始化、事件绑定、UI 状态管理，并将功能委托到各子模块
 *
 * 模块依赖（通过 <script> 标签顺序加载，全局工厂函数可用）：
 *   markdown.js   → createMarkdownModule
 *   particles.js  → createParticlesModule
 *   theme.js      → createThemeModule
 *   notification.js → createNotificationModule
 *   upload.js     → createUploadModule
 *   chat.js       → createChatModule
 *   aiops.js      → createAIOpsModule
 *   auth.js       → AuthManager / authManager（已有）
 */

// VigilOpsAgent 前端应用
class VigilOpsAgentApp {
    constructor() {
        // 认证检查
        if (typeof authManager !== 'undefined') {
            if (!authManager.checkAuth()) {
                return; // 未登录，已跳转
            }
        }
        
        this.apiBaseUrl = 'http://localhost:9999/api';
        this.currentMode = 'quick'; // 'quick' 或 'stream'
        this.sessionId = null; // 由后端生成
        this.isStreaming = false;

        // 混入功能模块（所有模块方法合并到 this，调用方式不变）
        Object.assign(this, createMarkdownModule(this));
        Object.assign(this, createParticlesModule());
        Object.assign(this, createThemeModule(this));
        Object.assign(this, createNotificationModule());
        Object.assign(this, createUploadModule(this));
        Object.assign(this, createChatModule(this));
        Object.assign(this, createAIOpsModule(this));
        Object.assign(this, createSessionModule(this));

        this.initializeElements();
        this.initTheme();
        this.initParticles();
        this.initTypewriter();
        this.bindEvents();
        this.updateUI();
        this.initMarkdown();
        this.checkAndSetCentered();
        
        // 显示用户信息
        this.updateUserInfo();
        
        // 初始化会话模块
        this.initSessionModule();
    }
    
    // 更新用户信息显示
    updateUserInfo() {
        if (typeof authManager === 'undefined') return;
        
        const user = authManager.getUser();
        if (user) {
            // 可以在侧边栏显示用户名
            const sidebarTitle = document.querySelector('.sidebar-title');
            if (sidebarTitle) {
                sidebarTitle.textContent = `${user.display_name || user.username}`;
            }
        }
    }

    // 初始化DOM元素
    initializeElements() {
        // 侧边栏元素
        this.sidebar = document.querySelector('.sidebar');
        this.newChatBtn = document.getElementById('newChatBtn');
        this.aiOpsSidebarBtn = document.getElementById('aiOpsSidebarBtn');
        this.mobileMenuBtn = document.getElementById('mobileMenuBtn');
        this.sidebarOverlay = document.getElementById('sidebarOverlay');
        this.themeToggleBtn = document.getElementById('themeToggleBtn');
        this.themeText = document.getElementById('themeText');

        // 输入区域元素
        this.messageInput = document.getElementById('messageInput');
        this.sendButton = document.getElementById('sendButton');
        this.toolsBtn = document.getElementById('toolsBtn');
        this.toolsMenu = document.getElementById('toolsMenu');
        this.uploadFileItem = document.getElementById('uploadFileItem');
        this.modeSelectorBtn = document.getElementById('modeSelectorBtn');
        this.modeDropdown = document.getElementById('modeDropdown');
        this.currentModeText = document.getElementById('currentModeText');
        this.fileInput = document.getElementById('fileInput');

        // 聊天区域元素
        this.chatMessages = document.getElementById('chatMessages');
        this.loadingOverlay = document.getElementById('loadingOverlay');
        this.chatContainer = document.querySelector('.chat-container');
        this.welcomeGreeting = document.getElementById('welcomeGreeting');
        // 快捷建议卡片
        this.suggestionCards = document.querySelectorAll('.suggestion-card');

        // 初始化时检查是否需要居中
        this.checkAndSetCentered();
    }

    // 初始化打字机效果
    initTypewriter() {
        this.typewriterTexts = [
            { element: 'welcomeTitle', text: '你好，我是 Vigil 智能运维小助手', speed: 80, delay: 500 },
            { element: 'welcomeSubtitle', text: '有什么可以帮你的？', speed: 60, delay: 200 }
        ];

        this.startTypewriter();
    }

    startTypewriter() {
        this.typewriterTexts.forEach((item, index) => {
            const element = document.getElementById(item.element);
            if (!element) return;

            setTimeout(() => {
                this.typeText(element, item.text, item.speed);
            }, item.delay * (index + 1));
        });
    }

    typeText(element, text, speed) {
        let i = 0;
        element.textContent = '';

        const timer = setInterval(() => {
            if (i < text.length) {
                element.textContent += text.charAt(i);
                i++;
            } else {
                clearInterval(timer);
            }
        }, speed);
    }

    // 绑定事件监听器
    bindEvents() {
        // 新建对话
        if (this.newChatBtn) {
            this.newChatBtn.addEventListener('click', () => this.newChat());
        }

        // AI Ops按钮
        if (this.aiOpsSidebarBtn) {
            this.aiOpsSidebarBtn.addEventListener('click', () => this.triggerAIOps());
        }

        // 主题切换
        if (this.themeToggleBtn) {
            this.themeToggleBtn.addEventListener('click', () => this.toggleTheme());
        }

        // 登出按钮
        this.logoutBtn = document.getElementById('logoutBtn');
        if (this.logoutBtn) {
            this.logoutBtn.addEventListener('click', async () => {
                if (typeof authManager !== 'undefined') {
                    const result = await authManager.logout();
                    if (result.success) {
                        this.showNotification('退出登录成功', 'success');
                    } else {
                        this.showNotification(result.error || '退出登录失败', 'error');
                    }
                    // 短暂延迟后跳转，让用户看到提示
                    setTimeout(() => {
                        window.location.href = '/login';
                    }, 1000);
                } else {
                    window.location.href = '/login';
                }
            });
        }

        // 移动端菜单
        if (this.mobileMenuBtn) {
            this.mobileMenuBtn.addEventListener('click', () => this.toggleMobileSidebar());
        }
        if (this.sidebarOverlay) {
            this.sidebarOverlay.addEventListener('click', () => this.closeMobileSidebar());
        }

        // 快捷建议卡片
        this.suggestionCards.forEach(card => {
            card.addEventListener('click', () => {
                const suggestion = card.getAttribute('data-suggestion');
                if (suggestion && this.messageInput) {
                    this.messageInput.value = suggestion;
                    this.messageInput.focus();
                }
            });
            card.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    card.click();
                }
            });
        });

        // 模式选择下拉菜单
        if (this.modeSelectorBtn) {
            this.modeSelectorBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleModeDropdown();
            });
        }

        // 下拉菜单项点击
        const dropdownItems = document.querySelectorAll('.dropdown-item');
        dropdownItems.forEach(item => {
            item.addEventListener('click', (e) => {
                const mode = item.getAttribute('data-mode');
                this.selectMode(mode);
                this.closeModeDropdown();
            });
            item.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    item.click();
                }
            });
        });

        // 点击外部关闭下拉菜单
        document.addEventListener('click', (e) => {
            if (!this.modeSelectorBtn.contains(e.target) &&
                !this.modeDropdown.contains(e.target)) {
                this.closeModeDropdown();
            }
        });

        // 发送消息
        if (this.sendButton) {
            this.sendButton.addEventListener('click', () => this.sendMessage());
        }

        if (this.messageInput) {
            this.messageInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });
        }

        // 工具按钮和菜单
        if (this.toolsBtn) {
            this.toolsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleToolsMenu();
            });
        }

        // 工具菜单项点击事件
        if (this.uploadFileItem) {
            this.uploadFileItem.addEventListener('click', () => {
                if (this.fileInput) {
                    this.fileInput.click();
                }
                this.closeToolsMenu();
            });
            this.uploadFileItem.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.uploadFileItem.click();
                }
            });
        }

        // 点击外部关闭工具菜单
        document.addEventListener('click', (e) => {
            if (this.toolsBtn && this.toolsMenu &&
                !this.toolsBtn.contains(e.target) &&
                !this.toolsMenu.contains(e.target)) {
                this.closeToolsMenu();
            }
        });

        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }
    }

    // 切换移动端侧边栏
    toggleMobileSidebar() {
        if (this.sidebar) {
            this.sidebar.classList.toggle('open');
        }
        if (this.sidebarOverlay) {
            this.sidebarOverlay.classList.toggle('active');
        }
    }

    // 关闭移动端侧边栏
    closeMobileSidebar() {
        if (this.sidebar) {
            this.sidebar.classList.remove('open');
        }
        if (this.sidebarOverlay) {
            this.sidebarOverlay.classList.remove('active');
        }
    }

    // 切换工具菜单显示/隐藏
    toggleToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭工具菜单
    closeToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 新建对话
    newChat() {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再新建对话', 'warning');
            return;
        }

        // 停止所有进行中的操作
        this.isStreaming = false;

        // 清空输入框
        if (this.messageInput) {
            this.messageInput.value = '';
        }

        // 清空聊天记录
        if (this.chatMessages) {
            this.chatMessages.innerHTML = '';
        }

        // 重置 sessionId 为 null（由后端生成）
        this.sessionId = null;

        // 重置模式为快速
        this.currentMode = 'quick';
        this.updateUI();

        // 重新设置居中样式（确保对话框居中显示）
        this.checkAndSetCentered();

        // 关闭移动端侧边栏
        this.closeMobileSidebar();
        
        // 通知会话模块新建对话
        if (typeof this.onNewChat === 'function') {
            this.onNewChat();
        }
    }
    
    // 切换模式下拉菜单
    toggleModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭模式下拉菜单
    closeModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 选择模式
    selectMode(mode) {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再切换模式', 'warning');
            return;
        }
        
        this.currentMode = mode;
        this.updateUI();
        
        const modeNames = {
            'quick': '快速',
            'stream': '流式'
        };
        
        this.showNotification(`已切换到${modeNames[mode]}模式`, 'info');
    }

    // 更新UI
    updateUI() {
        // 更新模式选择器显示
        if (this.currentModeText) {
            const modeNames = {
                'quick': '快速',
                'stream': '流式'
            };
            this.currentModeText.textContent = modeNames[this.currentMode] || '快速';
        }
        
        // 更新下拉菜单选中状态
        const dropdownItems = document.querySelectorAll('.dropdown-item');
        dropdownItems.forEach(item => {
            const mode = item.getAttribute('data-mode');
            if (mode === this.currentMode) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });
        
        // 更新发送按钮状态
        if (this.sendButton) {
            this.sendButton.disabled = this.isStreaming;
        }
        
        // 更新输入框状态
        if (this.messageInput) {
            this.messageInput.disabled = this.isStreaming;
            this.messageInput.placeholder = '问问 Vigil 智能运维助手';
        }
    }

    // HTML转义
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // 触发智能运维（点击智能运维按钮时直接调用）
    async triggerAIOps() {
        if (this.isStreaming) {
            this.showNotification('请等待当前操作完成', 'warning');
            return;
        }

        // 新建对话
        this.newChat();
        
        // 添加"分析中..."的消息（带旋转动画）
        const loadingMessage = this.addLoadingMessage('分析中...');
        this.currentAIOpsMessage = loadingMessage; // 保存消息引用用于后续更新
        
        // 设置发送状态
        this.isStreaming = true;
        this.updateUI();

        try {
            await this.sendAIOpsRequest(loadingMessage);
        } catch (error) {
            console.error('智能运维分析失败:', error);
            // 更新消息为错误信息
            if (loadingMessage) {
                const messageContent = loadingMessage.querySelector('.message-content');
                if (messageContent) {
                    messageContent.textContent = '抱歉，智能运维分析时出现错误：' + error.message;
                }
            }
        } finally {
            this.isStreaming = false;
            this.currentAIOpsMessage = null;
            this.updateUI();
        }
    }

    // 显示/隐藏加载遮罩层
    showLoadingOverlay(show) {
        if (this.loadingOverlay) {
            if (show) {
                this.loadingOverlay.style.display = 'flex';
                // 更新文字为智能运维
                const loadingText = this.loadingOverlay.querySelector('.loading-text');
                const loadingSubtext = this.loadingOverlay.querySelector('.loading-subtext');
                if (loadingText) loadingText.textContent = '智能运维分析中，请稍候...';
                if (loadingSubtext) loadingSubtext.textContent = '后端正在处理，请耐心等待';
                // 防止页面滚动
                document.body.style.overflow = 'hidden';
            } else {
                this.loadingOverlay.style.display = 'none';
                // 恢复页面滚动
                document.body.style.overflow = '';
            }
        }
    }

    // 显示/隐藏上传遮罩层
    showUploadOverlay(show, fileName = '') {
        if (this.loadingOverlay) {
            if (show) {
                this.loadingOverlay.style.display = 'flex';
                // 更新文字为上传中
                const loadingText = this.loadingOverlay.querySelector('.loading-text');
                const loadingSubtext = this.loadingOverlay.querySelector('.loading-subtext');
                if (loadingText) loadingText.textContent = '正在上传文件...';
                if (loadingSubtext) loadingSubtext.textContent = fileName ? `上传: ${fileName}` : '请稍候';
                // 防止页面滚动
                document.body.style.overflow = 'hidden';
            } else {
                this.loadingOverlay.style.display = 'none';
                // 恢复页面滚动
                document.body.style.overflow = '';
            }
        }
    }
}

// 初始化应用
document.addEventListener('DOMContentLoaded', () => {
    new VigilOpsAgentApp();
});
