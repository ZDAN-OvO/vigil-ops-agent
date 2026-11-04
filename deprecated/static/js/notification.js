/**
 * 通知提示模块
 * 负责显示临时通知消息（支持 info/success/warning/error 类型）
 */
function createNotificationModule() {
    return {
        // 显示通知
        showNotification(message, type = 'info') {
            // 创建通知元素
            const notification = document.createElement('div');
            notification.className = `notification ${type}`;
            notification.textContent = message;
            notification.setAttribute('role', 'alert');

            // 根据类型设置颜色
            const colors = {
                info: '#0071e3',
                success: '#22c55e',
                warning: '#f59e0b',
                error: '#ef4444'
            };
            notification.style.backgroundColor = colors[type] || colors.info;

            // 添加到页面
            document.body.appendChild(notification);

            // 3秒后自动移除
            setTimeout(() => {
                notification.style.animation = 'notification-slide-out 250ms cubic-bezier(0.4, 0, 0.2, 1)';
                setTimeout(() => {
                    if (notification.parentNode) {
                        notification.parentNode.removeChild(notification);
                    }
                }, 300);
            }, 3000);
        }
    };
}
