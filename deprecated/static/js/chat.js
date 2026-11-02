/**
 * 聊天消息模块
 * 负责消息发送（普通/流式）、消息渲染、滚动管理等聊天相关功能
 */
function createChatModule(app) {
    return {
        // 发送消息
        async sendMessage() {
            let message = '';
            if (app.messageInput) {
                message = app.messageInput.value.trim();
            }
            
            if (!message) {
                this.showNotification('请输入消息内容', 'warning');
                return;
            }

            if (app.isStreaming) {
                this.showNotification('请等待当前对话完成', 'warning');
                return;
            }

            // 显示用户消息
            this.addMessage('user', message);
            
            // 清空输入框
            if (app.messageInput) {
                app.messageInput.value = '';
            }

            // 设置发送状态
            app.isStreaming = true;
            app.updateUI();

            try {
                if (app.currentMode === 'quick') {
                    await this.sendQuickMessage(message);
                } else if (app.currentMode === 'stream') {
                    await this.sendStreamMessage(message);
                }
                
                // 通知会话模块刷新列表（更新消息计数）
                if (typeof app.onMessageSent === 'function') {
                    app.onMessageSent();
                }
            } catch (error) {
                console.error('发送消息失败:', error);
                this.addMessage('assistant', '抱歉，发送消息时出现错误：' + error.message);
            } finally {
                app.isStreaming = false;
                app.updateUI();
            }
        },

        // 发送快速消息（普通对话）
        async sendQuickMessage(message) {
            // 添加等待提示消息
            const loadingMessage = this.addLoadingMessage('正在思考...');
            
            try {
                // 构建请求体，如果 sessionId 为空则不传 Id 字段
                const requestBody = {
                    Question: message
                };
                if (app.sessionId) {
                    requestBody.Id = app.sessionId;
                }

                // 使用认证请求
                const response = await authManager.authFetch(`${app.apiBaseUrl}/chat/completion`, {
                    method: 'POST',
                    body: JSON.stringify(requestBody)
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                const data = await response.json();
                console.log('[sendQuickMessage] 响应数据:', JSON.stringify(data));
                
                // 移除等待提示消息
                if (loadingMessage && loadingMessage.parentNode) {
                    loadingMessage.parentNode.removeChild(loadingMessage);
                }
                
                // 统一响应格式：检查 data.code 或 data.message 判断请求是否成功
                if (data.code === 200 || data.message === 'success') {
                    // data.data 是 ChatResponse 对象
                    const chatResponse = data.data;
                    
                    if (chatResponse && chatResponse.success) {
                        // 保存后端返回的 session_id
                        if (chatResponse.session_id) {
                            app.sessionId = chatResponse.session_id;
                            console.log('[sendQuickMessage] 保存 session_id:', app.sessionId);
                        }
                        
                        // 成功：添加实际响应消息（即使 answer 为空也显示）
                        const answer = chatResponse.answer || '（无回复内容）';
                        this.addMessage('assistant', answer);
                    } else if (chatResponse && chatResponse.errorMessage) {
                        // 业务错误
                        throw new Error(chatResponse.errorMessage);
                    } else {
                        // 兜底：尝试显示任何可用内容
                        const fallbackAnswer = chatResponse?.answer || chatResponse?.errorMessage || '服务返回了空内容';
                        this.addMessage('assistant', fallbackAnswer);
                    }
                } else {
                    // HTTP 成功但业务失败
                    throw new Error(data.message || '请求失败');
                }
            } catch (error) {
                // 出错时也要移除等待提示消息
                if (loadingMessage && loadingMessage.parentNode) {
                    loadingMessage.parentNode.removeChild(loadingMessage);
                }
                throw error;
            }
        },

        // 发送流式消息
        async sendStreamMessage(message) {
            try {
                // 构建请求体，如果 sessionId 为空则不传 Id 字段
                const requestBody = {
                    Question: message
                };
                if (app.sessionId) {
                    requestBody.Id = app.sessionId;
                }

                // 使用认证请求
                const response = await authManager.authFetch(`${app.apiBaseUrl}/chat/stream`, {
                    method: 'POST',
                    body: JSON.stringify(requestBody)
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }
                
                // 创建助手消息元素
                const assistantMessageElement = this.addMessage('assistant', '', true);
                let fullResponse = '';

                // 处理流式响应
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                let currentEvent = '';

                try {
                    while (true) {
                        const { done, value } = await reader.read();
                        
                        if (done) {
                            // 流结束，使用统一的处理方法
                            this.handleStreamComplete(assistantMessageElement, fullResponse);
                            break;
                        }

                        // 解码数据并添加到缓冲区
                        buffer += decoder.decode(value, { stream: true });
                        
                        // 按行分割处理
                        const lines = buffer.split('\n');
                        // 保留最后一行（可能不完整）
                        buffer = lines.pop() || '';
                        
                        for (const line of lines) {
                            if (line.trim() === '') continue;
                            
                            console.log('[SSE调试] 收到行:', line);
                            
                            // 解析SSE格式
                            if (line.startsWith('id:')) {
                                console.log('[SSE调试] 解析到ID');
                                continue;
                            } else if (line.startsWith('event:')) {
                                // 兼容 "event:message" 和 "event: message" 两种格式
                                currentEvent = line.substring(6).trim();
                                console.log('[SSE调试] 解析到事件类型:', currentEvent);
                                // 注意：后端统一使用 "message" 事件名，真正的类型在 data 的 JSON 中
                                continue;
                            } else if (line.startsWith('data:')) {
                                // 兼容 "data:xxx" 和 "data: xxx" 两种格式
                                const rawData = line.substring(5).trim();
                                console.log('[SSE调试] 解析到数据, currentEvent:', currentEvent, ', rawData:', rawData);
                                
                                // 兼容旧格式 [DONE] 标记
                                if (rawData === '[DONE]') {
                                    // 流结束标记，将内容转换为Markdown渲染
                                    this.handleStreamComplete(assistantMessageElement, fullResponse);
                                    return;
                                }
                                
                                // 处理 SSE 数据
                                try {
                                    // 尝试解析为 SseMessage 格式的 JSON
                                    const sseMessage = JSON.parse(rawData);
                                    console.log('[SSE调试] 解析JSON成功:', sseMessage);
                                    
                                    if (sseMessage && typeof sseMessage.type === 'string') {
                                        if (sseMessage.type === 'session_created') {
                                            // 保存后端返回的 session_id
                                            if (sseMessage.data && sseMessage.data.session_id) {
                                                app.sessionId = sseMessage.data.session_id;
                                                console.log('[SSE调试] 保存 session_id:', app.sessionId);
                                            }
                                            continue;
                                        } else if (sseMessage.type === 'content') {
                                            const content = sseMessage.data || '';
                                            fullResponse += content;
                                            console.log('[SSE调试] 添加内容:', content);
                                            
                                            // 实时渲染 Markdown
                                            if (assistantMessageElement) {
                                                const messageContent = assistantMessageElement.querySelector('.message-content');
                                                messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                                // 高亮代码块
                                                this.highlightCodeBlocks(messageContent);
                                                this.scrollToBottom();
                                            }
                                        } else if (sseMessage.type === 'done') {
                                            console.log('[SSE调试] 收到done标记，流结束');
                                            this.handleStreamComplete(assistantMessageElement, fullResponse);
                                            return;
                                        } else if (sseMessage.type === 'error') {
                                            console.error('[SSE调试] 收到错误:', sseMessage.data);
                                            if (assistantMessageElement) {
                                                const messageContent = assistantMessageElement.querySelector('.message-content');
                                                messageContent.innerHTML = this.renderMarkdown('错误: ' + (sseMessage.data || '未知错误'));
                                            }
                                            return;
                                        }
                                    } else {
                                        // 不是标准 SseMessage 格式，尝试兼容处理
                                        console.log('[SSE调试] 非标准格式，尝试兼容处理');
                                        fullResponse += rawData;
                                        if (assistantMessageElement) {
                                            const messageContent = assistantMessageElement.querySelector('.message-content');
                                            messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                            this.highlightCodeBlocks(messageContent);
                                            this.scrollToBottom();
                                        }
                                    }
                                } catch (e) {
                                    // JSON 解析失败，尝试兼容旧格式
                                    console.log('[SSE调试] JSON解析失败，使用兼容模式:', e.message);
                                    if (rawData === '') {
                                        fullResponse += '\n';
                                    } else {
                                        fullResponse += rawData;
                                    }
                                    
                                    if (assistantMessageElement) {
                                        const messageContent = assistantMessageElement.querySelector('.message-content');
                                        messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                        this.highlightCodeBlocks(messageContent);
                                        this.scrollToBottom();
                                    }
                                }
                            }
                        }
                    }
                } finally {
                    reader.releaseLock();
                }
            } catch (error) {
                throw error;
            }
        },

        // 添加消息到聊天界面
        addMessage(type, content, isStreaming = false) {
            // 检查是否是第一条消息，如果是则移除居中样式
            const isFirstMessage = app.chatMessages && app.chatMessages.querySelectorAll('.message').length === 0;
            
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${type}${isStreaming ? ' streaming' : ''}`;

            // 如果是assistant消息，添加头像图标
            if (type === 'assistant') {
                const messageAvatar = document.createElement('div');
                messageAvatar.className = 'message-avatar';
                messageAvatar.setAttribute('aria-hidden', 'true');
                messageAvatar.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="currentColor"/>
                    </svg>
                `;
                messageDiv.appendChild(messageAvatar);
            }

            // 创建消息内容包装器
            const messageContentWrapper = document.createElement('div');
            messageContentWrapper.className = 'message-content-wrapper';

            const messageContent = document.createElement('div');
            messageContent.className = 'message-content';
            
            // 如果是assistant消息且不是流式消息，使用Markdown渲染
            if (type === 'assistant' && !isStreaming) {
                messageContent.innerHTML = this.renderMarkdown(content);
                // 高亮代码块
                this.highlightCodeBlocks(messageContent);
            } else {
                // 用户消息或流式消息使用纯文本
                messageContent.textContent = content;
            }

            messageContentWrapper.appendChild(messageContent);
            messageDiv.appendChild(messageContentWrapper);

            if (app.chatMessages) {
                app.chatMessages.appendChild(messageDiv);

                // 如果是第一条消息，移除居中样式
                if (isFirstMessage && app.chatContainer) {
                    app.chatContainer.classList.remove('centered');
                }

                this.scrollToBottom();
            }

            return messageDiv;
        },

        // 添加带加载动画的消息
        addLoadingMessage(content) {
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message assistant';

            // 添加头像图标
            const messageAvatar = document.createElement('div');
            messageAvatar.className = 'message-avatar';
            messageAvatar.setAttribute('aria-hidden', 'true');
            messageAvatar.innerHTML = `
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="currentColor"/>
                </svg>
            `;
            messageDiv.appendChild(messageAvatar);

            // 创建消息内容包装器
            const messageContentWrapper = document.createElement('div');
            messageContentWrapper.className = 'message-content-wrapper';

            const messageContent = document.createElement('div');
            messageContent.className = 'message-content loading-message-content';
            
            // 创建文本和动画容器
            const textSpan = document.createElement('span');
            textSpan.textContent = content;
            
            // 创建旋转动画图标
            const loadingIcon = document.createElement('span');
            loadingIcon.className = 'loading-spinner-icon';
            loadingIcon.innerHTML = `
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z" fill="currentColor" opacity="0.2"/>
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10c1.54 0 3-.36 4.28-1l-1.5-2.6C13.64 19.62 12.84 20 12 20c-4.41 0-8-3.59-8-8s3.59-8 8-8c.84 0 1.64.38 2.18 1l1.5-2.6C13 2.36 12.54 2 12 2z" fill="currentColor"/>
                </svg>
            `;
            
            messageContent.appendChild(textSpan);
            messageContent.appendChild(loadingIcon);
            messageContentWrapper.appendChild(messageContent);
            messageDiv.appendChild(messageContentWrapper);

            if (app.chatMessages) {
                app.chatMessages.appendChild(messageDiv);

                // 如果是第一条消息，移除居中样式
                const isFirstMessage = app.chatMessages.querySelectorAll('.message').length === 1;
                if (isFirstMessage && app.chatContainer) {
                    app.chatContainer.classList.remove('centered');
                }

                this.scrollToBottom();
            }

            return messageDiv;
        },
        
        // 检查并设置居中样式
        checkAndSetCentered() {
            if (app.chatMessages && app.chatContainer) {
                const hasMessages = app.chatMessages.querySelectorAll('.message').length > 0;
                if (!hasMessages) {
                    app.chatContainer.classList.add('centered');
                } else {
                    app.chatContainer.classList.remove('centered');
                }
            }
        },

        // 滚动到底部
        scrollToBottom() {
            if (app.chatMessages) {
                app.chatMessages.scrollTop = app.chatMessages.scrollHeight;
            }
        },

        // 处理流式传输完成
        handleStreamComplete(assistantMessageElement, fullResponse) {
            if (assistantMessageElement) {
                assistantMessageElement.classList.remove('streaming');
                const messageContent = assistantMessageElement.querySelector('.message-content');
                if (messageContent) {
                    messageContent.innerHTML = this.renderMarkdown(fullResponse);
                    // 高亮代码块
                    this.highlightCodeBlocks(messageContent);
                }
            }
        }
    };
}
