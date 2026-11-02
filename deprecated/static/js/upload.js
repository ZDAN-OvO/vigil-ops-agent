/**
 * 文件上传模块
 * 负责文件选择、类型验证和上传到知识库
 */
function createUploadModule(app) {
    return {
        // 处理文件选择
        handleFileSelect(event) {
            const file = event.target.files[0];
            if (file) {
                // 验证文件格式
                if (!this.validateFileType(file)) {
                    this.showNotification('只支持上传 TXT 或 Markdown (.md) 格式的文件', 'error');
                    app.fileInput.value = '';
                    return;
                }
                this.uploadFile(file);
            }
        },

        // 验证文件类型
        validateFileType(file) {
            const fileName = file.name.toLowerCase();
            const allowedExtensions = ['.txt', '.md', '.markdown'];
            return allowedExtensions.some(ext => fileName.endsWith(ext));
        },

        // 上传文件到知识库
        async uploadFile(file) {
            // 再次验证文件类型（双重保险）
            if (!this.validateFileType(file)) {
                this.showNotification('只支持上传 TXT 或 Markdown (.md) 格式的文件', 'error');
                return;
            }

            // 验证文件大小（限制为50MB）
            const maxSize = 50 * 1024 * 1024;
            if (file.size > maxSize) {
                this.showNotification('文件大小不能超过50MB', 'error');
                return;
            }

            // 锁定前端并显示上传遮罩层
            app.isStreaming = true;
            app.updateUI();
            app.showUploadOverlay(true, file.name);

            try {
                // 创建 FormData
                const formData = new FormData();
                formData.append('file', file);

                // 发送上传请求（Cookie 自动携带）
                const response = await fetch(`${app.apiBaseUrl}/file/upload`, {
                    method: 'POST',
                    credentials: 'include',
                    body: formData
                });

                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status}`);
                }

                const data = await response.json();

                if ((data.code === 200 || data.message === 'success') && data.data) {
                    // 在聊天界面显示上传成功消息
                    const successMessage = `${file.name} 上传到知识库成功`;
                    app.addMessage('assistant', successMessage, false, true);
                } else {
                    throw new Error(data.message || '上传失败');
                }
            } catch (error) {
                console.error('文件上传失败:', error);
                this.showNotification('文件上传失败: ' + error.message, 'error');
            } finally {
                // 清空文件输入
                if (app.fileInput) {
                    app.fileInput.value = '';
                }
                // 解锁前端
                app.isStreaming = false;
                app.showUploadOverlay(false);
                app.updateUI();
            }
        },

        // 格式化文件大小
        formatFileSize(bytes) {
            if (bytes === 0) return '0 Bytes';
            const k = 1024;
            const sizes = ['Bytes', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
        }
    };
}
