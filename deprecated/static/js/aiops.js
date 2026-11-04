/**
 * 智能运维（AIOps）模块
 * 负责 AIOps 诊断请求的 SSE 流式处理、消息更新和折叠详情展示
 */
function createAIOpsModule(app) {
    return {
        // 发送智能运维请求（SSE 流式模式）
        async sendAIOpsRequest(loadingMessageElement) {
            try {
                // 使用认证请求
                const response = await authManager.authFetch(`${app.apiBaseUrl}/aiops`, {
                    method: 'POST',
                    body: JSON.stringify({
                        session_id: app.sessionId
                    })
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                let fullResponse = '';

                // 处理 SSE 流式响应
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                let currentEvent = 'message'; // 默认事件类型为 message

                try {
                    while (true) {
                        const { done, value } = await reader.read();
                        
                        if (done) {
                            // 流结束，更新最终内容
                            if (fullResponse) {
                                console.log('AI Ops 流结束，更新最终内容，长度:', fullResponse.length);
                                this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                            }
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
                            
                            console.log('[AI Ops SSE] 收到行:', line);
                            
                            // 解析 SSE 格式
                            if (line.startsWith('id:')) {
                                continue;
                            } else if (line.startsWith('event:')) {
                                currentEvent = line.substring(6).trim();
                                console.log('[AI Ops SSE] 事件类型:', currentEvent);
                                continue;
                            } else if (line.startsWith('data:')) {
                                const rawData = line.substring(5).trim();
                                console.log('[AI Ops SSE] 数据:', rawData, ', currentEvent:', currentEvent);
                                
                                // 解析可能包含多个JSON对象的数据
                                const processJsonMessages = (data) => {
                                    const jsonPattern = /\{"type"\s*:\s*"[^"]+"\s*,\s*"data"\s*:\s*(?:"[^"]*"|null)\}/g;
                                    const matches = data.match(jsonPattern);
                                    
                                    if (matches && matches.length > 0) {
                                        console.log('[AI Ops SSE] 匹配到', matches.length, '个JSON对象');
                                        for (const jsonStr of matches) {
                                            try {
                                                const sseMessage = JSON.parse(jsonStr);
                                                if (sseMessage.type === 'content') {
                                                    fullResponse += sseMessage.data || '';
                                                } else if (sseMessage.type === 'plan') {
                                                    // 处理计划创建事件
                                                    const planText = `\n\n## 📋 执行计划\n${sseMessage.message}\n\n`;
                                                    fullResponse += planText;
                                                } else if (sseMessage.type === 'step_complete') {
                                                    // 处理步骤完成事件
                                                    const stepText = `\n✅ ${sseMessage.message}\n`;
                                                    fullResponse += stepText;
                                                } else if (sseMessage.type === 'status') {
                                                    // 处理状态更新事件
                                                    const statusText = `\n⏳ ${sseMessage.message}\n`;
                                                    fullResponse += statusText;
                                                } else if (sseMessage.type === 'report') {
                                                    // 处理最终报告事件 - 流式输出
                                                    console.log('AI Ops 最终报告生成');
                                                    const reportText = `\n\n## 🎯 诊断报告\n\n${sseMessage.report || ''}\n`;
                                                    fullResponse += reportText;
                                                } else if (sseMessage.type === 'complete') {
                                                    // 处理完成事件
                                                    console.log('AI Ops 诊断完成');
                                                    if (sseMessage.response) {
                                                        fullResponse += `\n\n${sseMessage.response}`;
                                                    }
                                                    this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                                    return true;
                                                } else if (sseMessage.type === 'done') {
                                                    console.log('AI Ops 流完成，最终内容长度:', fullResponse.length);
                                                    this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                                    return true;
                                                } else if (sseMessage.type === 'error') {
                                                    throw new Error(sseMessage.data || sseMessage.message || '智能运维分析失败');
                                                }
                                            } catch (e) {
                                                if (e.message.includes('智能运维')) throw e;
                                                console.log('[AI Ops SSE] 单个JSON解析失败:', jsonStr);
                                            }
                                        }
                                        if (loadingMessageElement) {
                                            this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                        }
                                        return false;
                                    }
                                    return null;
                                };
                                
                                const result = processJsonMessages(rawData);
                                if (result === true) {
                                    return; // 流结束
                                } else if (result === null) {
                                    // 没有匹配到多个JSON，尝试单个JSON解析
                                    try {
                                        const sseMessage = JSON.parse(rawData);
                                        if (sseMessage && sseMessage.type) {
                                            if (sseMessage.type === 'content') {
                                                fullResponse += sseMessage.data || '';
                                                if (loadingMessageElement) {
                                                    this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                                }
                                            } else if (sseMessage.type === 'plan') {
                                                // 处理计划创建事件
                                                const planText = `\n\n## 📋 执行计划\n${sseMessage.message}\n\n`;
                                                fullResponse += planText;
                                                if (loadingMessageElement) {
                                                    this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                                }
                                            } else if (sseMessage.type === 'step_complete') {
                                                // 处理步骤完成事件
                                                const stepText = `\n✅ ${sseMessage.message}\n`;
                                                fullResponse += stepText;
                                                if (loadingMessageElement) {
                                                    this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                                }
                                            } else if (sseMessage.type === 'status') {
                                                // 处理状态更新事件
                                                const statusText = `\n⏳ ${sseMessage.message}\n`;
                                                fullResponse += statusText;
                                                if (loadingMessageElement) {
                                                    this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                                }
                                            } else if (sseMessage.type === 'report') {
                                                // 处理最终报告事件 - 这是关键！
                                                console.log('AI Ops 最终报告生成，流式输出中...');
                                                const reportText = `\n\n## 🎯 诊断报告\n\n${sseMessage.report || ''}\n`;
                                                fullResponse += reportText;
                                                if (loadingMessageElement) {
                                                    this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                                }
                                            } else if (sseMessage.type === 'complete') {
                                                // 处理完成事件
                                                console.log('AI Ops 诊断完成，最终内容长度:', fullResponse.length);
                                                if (sseMessage.response) {
                                                    fullResponse += `\n\n${sseMessage.response}`;
                                                }
                                                // 使用最终的完整内容更新消息
                                                this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                                return;
                                            } else if (sseMessage.type === 'done') {
                                                console.log('AI Ops 流完成，最终内容长度:', fullResponse.length);
                                                this.updateAIOpsMessage(loadingMessageElement, fullResponse, []);
                                                return;
                                            } else if (sseMessage.type === 'error') {
                                                throw new Error(sseMessage.data || sseMessage.message || '智能运维分析失败');
                                            }
                                        } else {
                                            fullResponse += rawData;
                                            if (loadingMessageElement) {
                                                this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                            }
                                        }
                                    } catch (e) {
                                        if (e.message.includes('智能运维')) throw e;
                                        // 非 JSON 格式，直接追加原始数据
                                        fullResponse += rawData;
                                        if (loadingMessageElement) {
                                            this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                                        }
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

        // 更新智能运维流式内容（实时显示）
        updateAIOpsStreamContent(messageElement, content) {
            if (!messageElement) return;
            
            // 添加 aiops-message 类
            messageElement.classList.add('aiops-message');
            
            const messageContentWrapper = messageElement.querySelector('.message-content-wrapper');
            if (messageContentWrapper) {
                let messageContent = messageContentWrapper.querySelector('.message-content');
                if (!messageContent) {
                    messageContent = document.createElement('div');
                    messageContent.className = 'message-content';
                    messageContentWrapper.appendChild(messageContent);
                }
                // 流式显示时使用纯文本
                messageContent.textContent = content;
                this.scrollToBottom();
            }
        },

        // 更新智能运维消息（带折叠详情）
        updateAIOpsMessage(messageElement, response, details) {
            console.log('updateAIOpsMessage 被调用');
            console.log('messageElement:', messageElement);
            console.log('response:', response);
            console.log('response length:', response ? response.length : 0);
            console.log('details:', details);
            
            if (!messageElement) {
                // 如果没有传入消息元素，则创建新消息
                console.log('messageElement 为空，创建新消息');
                return this.addAIOpsMessage(response, details);
            }

            // 添加aiops-message类
            messageElement.classList.add('aiops-message');

            // 获取消息内容包装器
            const messageContentWrapper = messageElement.querySelector('.message-content-wrapper');
            if (!messageContentWrapper) {
                console.error('未找到 message-content-wrapper');
                return;
            }

            // 清空现有内容（保留消息内容容器）
            const messageContent = messageContentWrapper.querySelector('.message-content');
            if (!messageContent) {
                console.error('未找到 message-content');
                return;
            }

            // 移除加载动画相关的类和内容
            messageContent.classList.remove('loading-message-content');
            messageContent.textContent = '';
            
            // 移除加载图标（如果存在）
            const loadingIcon = messageContent.querySelector('.loading-spinner-icon');
            if (loadingIcon) {
                loadingIcon.remove();
            }

            // 详情部分（可折叠）- 先显示
            if (details && details.length > 0) {
                // 检查是否已存在详情容器
                let detailsContainer = messageElement.querySelector('.aiops-details');
                if (!detailsContainer) {
                    detailsContainer = document.createElement('div');
                    detailsContainer.className = 'aiops-details';
                    messageContentWrapper.insertBefore(detailsContainer, messageContent);
                } else {
                    // 清空现有详情
                    detailsContainer.innerHTML = '';
                }

                const detailsToggle = document.createElement('div');
                detailsToggle.className = 'details-toggle';
                detailsToggle.innerHTML = `
                    <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M9 18L15 12L9 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    <span>查看详细步骤 (${details.length}条)</span>
                `;

                const detailsContent = document.createElement('div');
                detailsContent.className = 'details-content';
                
                details.forEach((detail, index) => {
                    const detailItem = document.createElement('div');
                    detailItem.className = 'detail-item';
                    detailItem.innerHTML = `<strong>步骤 ${index + 1}:</strong> ${app.escapeHtml(detail)}`;
                    detailsContent.appendChild(detailItem);
                });

                // 点击切换折叠状态
                detailsToggle.addEventListener('click', () => {
                    detailsContent.classList.toggle('expanded');
                    detailsToggle.classList.toggle('expanded');
                });

                detailsContainer.appendChild(detailsToggle);
                detailsContainer.appendChild(detailsContent);
            }

            // 更新主要响应内容（使用Markdown渲染）
            console.log('开始渲染 Markdown');
            const renderedHtml = this.renderMarkdown(response);
            console.log('Markdown 渲染完成，HTML 长度:', renderedHtml ? renderedHtml.length : 0);
            messageContent.innerHTML = renderedHtml;
            console.log('innerHTML 已设置');
            // 高亮代码块
            this.highlightCodeBlocks(messageContent);
            console.log('代码块高亮完成');
            
            this.scrollToBottom();
            return messageElement;
        },

        // 添加智能运维消息（带折叠详情）- 保留用于兼容性
        addAIOpsMessage(response, details) {
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message assistant aiops-message';

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

            // 详情部分（可折叠）- 先显示
            if (details && details.length > 0) {
                const detailsContainer = document.createElement('div');
                detailsContainer.className = 'aiops-details';

                const detailsToggle = document.createElement('div');
                detailsToggle.className = 'details-toggle';
                detailsToggle.innerHTML = `
                    <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M9 18L15 12L9 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    <span>查看详细步骤 (${details.length}条)</span>
                `;

                const detailsContent = document.createElement('div');
                detailsContent.className = 'details-content';
                
                details.forEach((detail, index) => {
                    const detailItem = document.createElement('div');
                    detailItem.className = 'detail-item';
                    detailItem.innerHTML = `<strong>步骤 ${index + 1}:</strong> ${app.escapeHtml(detail)}`;
                    detailsContent.appendChild(detailItem);
                });

                // 点击切换折叠状态
                detailsToggle.addEventListener('click', () => {
                    detailsContent.classList.toggle('expanded');
                    detailsToggle.classList.toggle('expanded');
                });

                detailsContainer.appendChild(detailsToggle);
                detailsContainer.appendChild(detailsContent);
                messageContentWrapper.appendChild(detailsContainer);
            }

            // 主要响应内容 - 后显示（使用Markdown渲染）
            const messageContent = document.createElement('div');
            messageContent.className = 'message-content';
            messageContent.innerHTML = this.renderMarkdown(response);
            // 高亮代码块
            this.highlightCodeBlocks(messageContent);
            messageContentWrapper.appendChild(messageContent);
            messageDiv.appendChild(messageContentWrapper);
            
            if (app.chatMessages) {
                app.chatMessages.appendChild(messageDiv);
                this.scrollToBottom();
            }

            return messageDiv;
        }
    };
}
