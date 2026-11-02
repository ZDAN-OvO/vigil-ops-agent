/**
 * 会话管理模块
 * 负责会话列表的加载、渲染、切换、编辑、删除等操作
 */
function createSessionModule(app) {
    return {
        // 会话列表数据
        sessions: [],
        // 当前会话 ID
        currentSessionId: null,
        // 事件是否已绑定（防止重复绑定）
        _eventsBound: false,

        /**
         * 初始化会话模块
         */
        initSessionModule() {
            this.currentSessionId = app.sessionId; // 可能为 null
            this.loadSessions();
        },

        /**
         * 从后端加载会话列表
         */
        async loadSessions() {
            try {
                const response = await authManager.authFetch(`${app.apiBaseUrl}/sessions?limit=50`, {
                    method: 'GET'
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                const data = await response.json();
                this.sessions = data.sessions || [];
                this.renderSessions();
            } catch (error) {
                console.error('加载会话列表失败:', error);
                this.sessions = [];
                this.renderSessions();
            }
        },

        /**
         * 渲染会话列表
         */
        renderSessions() {
            const sessionList = document.getElementById('sessionList');
            if (!sessionList) return;

            if (this.sessions.length === 0) {
                sessionList.innerHTML = `
                    <div class="session-list-empty">
                        <p>暂无历史会话</p>
                        <p>开始新对话吧</p>
                    </div>
                `;
                return;
            }

            sessionList.innerHTML = this.sessions.map(session => {
                const isActive = session.session_id === this.currentSessionId;
                const title = this.escapeSessionTitle(session.title || '未命名会话');
                const count = session.message_count || 0;

                return `
                    <div class="session-item ${isActive ? 'active' : ''}" 
                         data-session-id="${session.session_id}"
                         title="${title}">
                        <svg class="session-item-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <span class="session-item-title">${title}</span>
                        <span class="session-item-count">${count}</span>
                        <div class="session-item-actions">
                            <button class="session-action-btn edit" data-action="edit" title="重命名">
                                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                                </svg>
                            </button>
                            <button class="session-action-btn delete" data-action="delete" title="删除">
                                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                                    <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                                </svg>
                            </button>
                        </div>
                    </div>
                `;
            }).join('');

            // 绑定事件（只绑定一次，使用事件委托）
            if (!this._eventsBound) {
                this.bindSessionEvents();
                this._eventsBound = true;
            }
        },

        /**
         * 绑定会话列表事件
         */
        bindSessionEvents() {
            const sessionList = document.getElementById('sessionList');
            if (!sessionList) return;

            // 点击会话项
            sessionList.addEventListener('click', (e) => {
                const sessionItem = e.target.closest('.session-item');
                if (!sessionItem) return;

                const sessionId = sessionItem.dataset.sessionId;
                const actionBtn = e.target.closest('.session-action-btn');

                if (actionBtn) {
                    const action = actionBtn.dataset.action;
                    if (action === 'edit') {
                        e.stopPropagation();
                        this.startEditSession(sessionId);
                    } else if (action === 'delete') {
                        e.stopPropagation();
                        this.deleteSession(sessionId);
                    }
                } else {
                    this.switchSession(sessionId);
                }
            });
        },

        /**
         * 切换到指定会话
         */
        async switchSession(sessionId) {
            if (sessionId === this.currentSessionId) return;

            if (app.isStreaming) {
                this.showNotification('请等待当前对话完成后再切换会话', 'warning');
                return;
            }

            try {
                // 获取会话历史
                const response = await authManager.authFetch(`${app.apiBaseUrl}/chat/session/${sessionId}`, {
                    method: 'GET'
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                const data = await response.json();
                const history = data.history || [];

                // 更新当前会话 ID
                this.currentSessionId = sessionId;
                app.sessionId = sessionId;

                // 清空当前聊天区域
                if (app.chatMessages) {
                    app.chatMessages.innerHTML = '';
                }

                // 渲染历史消息
                if (history.length > 0) {
                    for (const msg of history) {
                        const role = msg.role || msg.type;
                        if (role === 'human' || role === 'user') {
                            app.addMessage('user', msg.content);
                        } else if (role === 'ai' || role === 'assistant') {
                            app.addMessage('assistant', msg.content);
                        }
                    }
                    // 移除居中样式
                    if (app.chatContainer) {
                        app.chatContainer.classList.remove('centered');
                    }
                } else {
                    // 无历史消息，显示欢迎页
                    app.checkAndSetCentered();
                }

                // 更新列表高亮
                this.renderSessions();

                // 关闭移动端侧边栏
                app.closeMobileSidebar();

            } catch (error) {
                console.error('切换会话失败:', error);
                this.showNotification('切换会话失败: ' + error.message, 'error');
            }
        },

        /**
         * 开始编辑会话标题
         */
        startEditSession(sessionId) {
            const sessionItem = document.querySelector(`.session-item[data-session-id="${sessionId}"]`);
            if (!sessionItem) return;

            const titleSpan = sessionItem.querySelector('.session-item-title');
            const currentTitle = titleSpan.textContent;

            // 替换为输入框
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'session-item-edit-input';
            input.value = currentTitle;
            input.maxLength = 50;

            titleSpan.replaceWith(input);
            input.focus();
            input.select();

            // 防止重复保存的标志位
            let saved = false;

            // 保存编辑
            const saveEdit = async () => {
                // 防止重复调用
                if (saved) return;
                saved = true;

                const newTitle = input.value.trim();
                if (newTitle && newTitle !== currentTitle) {
                    await this.updateSessionTitle(sessionId, newTitle);
                }
                this.renderSessions();
            };

            // 回车保存
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    saveEdit();
                } else if (e.key === 'Escape') {
                    saved = true; // 标记为已处理，防止 blur 再次触发
                    this.renderSessions();
                }
            });

            // 失焦保存
            input.addEventListener('blur', saveEdit);
        },

        /**
         * 更新会话标题
         */
        async updateSessionTitle(sessionId, title) {
            try {
                const response = await authManager.authFetch(`${app.apiBaseUrl}/sessions/${sessionId}`, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ title })
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                // 更新本地数据
                const session = this.sessions.find(s => s.session_id === sessionId);
                if (session) {
                    session.title = title;
                }

                this.showNotification('会话标题已更新', 'success');
            } catch (error) {
                console.error('更新会话标题失败:', error);
                this.showNotification('更新失败: ' + error.message, 'error');
            }
        },

        /**
         * 删除会话
         */
        async deleteSession(sessionId) {
            // 确认删除
            if (!confirm('确定要删除这个会话吗？')) {
                return;
            }

            try {
                const response = await authManager.authFetch(`${app.apiBaseUrl}/sessions/${sessionId}`, {
                    method: 'DELETE'
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                // 从列表中移除
                this.sessions = this.sessions.filter(s => s.session_id !== sessionId);

                // 如果删除的是当前会话，切换到新会话
                if (sessionId === this.currentSessionId) {
                    app.newChat();
                }

                this.renderSessions();
                this.showNotification('会话已删除', 'success');

            } catch (error) {
                console.error('删除会话失败:', error);
                this.showNotification('删除失败: ' + error.message, 'error');
            }
        },

        /**
         * 新建会话后刷新列表
         */
        onNewChat() {
            // sessionId 已重置为 null，由后端生成
            this.currentSessionId = null;
            // 重新加载列表
            this.loadSessions();
        },

        /**
         * 发送消息后刷新列表（更新消息计数）
         */
        onMessageSent() {
            // 延迟刷新，等待后端更新
            setTimeout(() => {
                this.loadSessions();
            }, 500);
        },

        /**
         * 转义会话标题（防止 XSS）
         */
        escapeSessionTitle(title) {
            const div = document.createElement('div');
            div.textContent = title;
            return div.innerHTML;
        }
    };
}
