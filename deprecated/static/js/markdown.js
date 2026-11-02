/**
 * Markdown 渲染模块
 * 负责 Markdown 初始化、渲染和代码高亮
 */
function createMarkdownModule(app) {
    return {
        // 初始化Markdown配置
        initMarkdown() {
            // 等待 marked 库加载完成
            const checkMarked = () => {
                if (typeof marked !== 'undefined') {
                    try {
                        // 配置marked选项
                        marked.setOptions({
                            breaks: true,  // 支持GFM换行
                            gfm: true,     // 启用GitHub风格的Markdown
                            headerIds: false,
                            mangle: false
                        });

                        // 配置代码高亮
                        if (typeof hljs !== 'undefined') {
                            marked.setOptions({
                                highlight: function(code, lang) {
                                    if (lang && hljs.getLanguage(lang)) {
                                        try {
                                            return hljs.highlight(code, { language: lang }).value;
                                        } catch (err) {
                                            console.error('代码高亮失败:', err);
                                        }
                                    }
                                    return code;
                                }
                            });
                        }
                        console.log('Markdown 渲染库初始化成功');
                    } catch (e) {
                        console.error('Markdown 配置失败:', e);
                    }
                } else {
                    // 如果 marked 还没加载，等待一段时间后重试
                    setTimeout(checkMarked, 100);
                }
            };
            checkMarked();
        },

        // 安全地渲染 Markdown
        renderMarkdown(content) {
            if (!content) return '';
            
            // 检查 marked 是否可用
            if (typeof marked === 'undefined') {
                console.warn('marked 库未加载，使用纯文本显示');
                return app.escapeHtml(content);
            }
            
            try {
                const html = marked.parse(content);
                return html;
            } catch (e) {
                console.error('Markdown 渲染失败:', e);
                return app.escapeHtml(content);
            }
        },

        // 高亮代码块
        highlightCodeBlocks(container) {
            if (typeof hljs !== 'undefined' && container) {
                try {
                    container.querySelectorAll('pre code').forEach((block) => {
                        if (!block.classList.contains('hljs')) {
                            hljs.highlightElement(block);
                        }
                    });
                } catch (e) {
                    console.error('代码高亮失败:', e);
                }
            }
        }
    };
}
