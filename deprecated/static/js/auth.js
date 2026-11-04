/**
 * 认证模块
 * 基于 HttpOnly Cookie 的认证管理
 *
 * Token 由后端通过 Set-Cookie 设置，JS 无法读取（防 XSS）。
 * 所有 fetch 请求需携带 credentials: 'include' 以自动附带 Cookie。
 */

class AuthManager {
    constructor() {
        this.apiBaseUrl = '/api/auth';
        this.storageKeys = {
            user: 'user_info',
        };

        // Token 年龄追踪：上次成功获取/确认 token 有效的时间戳
        // 初始化为 0，确保首次请求时会向后端确认 token 状态
        this.tokenObtainedAt = 0;

        // 是否正在进行刷新请求（防止并发刷新）
        this._refreshing = false;
        this._refreshPromise = null;
    }

    /**
     * 登录
     * @param {string} username - 用户名
     * @param {string} password - 密码
     * @returns {Promise<Object>} 登录结果
     */
    async login(username, password) {
        try {
            const response = await fetch(`${this.apiBaseUrl}/login`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include',  // 接收 Set-Cookie
                body: JSON.stringify({ username, password })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '登录失败，请检查用户名和密码');
            }

            const data = await response.json();

            // 仅存储用户信息（token 由 Cookie 管理，JS 不可见）
            this.setUser(data.user);

            // 记录 token 获取时间，用于后续智能刷新判断
            this.tokenObtainedAt = Date.now();

            return {
                success: true,
                user: data.user
            };
        } catch (error) {
            console.error('登录失败:', error);
            return {
                success: false,
                error: error.message
            };
        }
    }

    /**
     * 登出
     * @returns {Promise<{success: boolean, error?: string}>} 登出结果
     */
    async logout() {
        try {
            const response = await fetch(`${this.apiBaseUrl}/logout`, {
                method: 'POST',
                credentials: 'include',  // 自动携带 Cookie
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                console.error('登出失败:', error);
                // 仍然清除本地状态并跳转，但返回失败信息
                this.clearUser();
                this.tokenObtainedAt = 0;
                return {
                    success: false,
                    error: error.detail || '退出登录失败，请稍后重试'
                };
            }
        } catch (error) {
            console.error('登出请求失败:', error);
            // 网络异常时仍清除本地状态并跳转，但返回失败信息
            this.clearUser();
            this.tokenObtainedAt = 0;
            return {
                success: false,
                error: '网络连接异常，退出登录失败'
            };
        }

        // 清除本地用户信息
        this.clearUser();

        // 重置 token 时间戳
        this.tokenObtainedAt = 0;

        return { success: true };
    }

    /**
     * 判断是否需要刷新 token
     * access_token 有效期 30 分钟，在距上次获取 25 分钟后触发刷新检查
     * @returns {boolean}
     */
    shouldRefreshToken() {
        // tokenObtainedAt 为 0 表示页面刚加载，尚未确认过 token 状态
        if (this.tokenObtainedAt === 0) return true;
        const elapsed = Date.now() - this.tokenObtainedAt;
        return elapsed >= 25 * 60 * 1000; // 25 分钟
    }

    /**
     * 刷新 access token（带并发去重）
     * 后端会检查当前 access_token 是否仍有效：
     * - 有效 → 返回 { refreshed: false }，不签发新 token
     * - 过期 → 用 refresh_token 签发新 access_token
     * - refresh_token 过期 → 返回 401，跳转登录页
     */
    async refreshToken() {
        // 并发去重：多个请求同时触发刷新时，只发一次刷新请求
        if (this._refreshing) {
            return this._refreshPromise;
        }

        this._refreshing = true;
        this._refreshPromise = this._doRefreshToken();

        try {
            const result = await this._refreshPromise;
            return result;
        } finally {
            this._refreshing = false;
            this._refreshPromise = null;
        }
    }

    /**
     * 内部刷新实现
     * @private
     */
    async _doRefreshToken() {
        try {
            const response = await fetch(`${this.apiBaseUrl}/refresh`, {
                method: 'POST',
                credentials: 'include',  // 自动携带 refresh_token Cookie
            });

            if (!response.ok) {
                // refresh_token 过期或无效，跳转登录页
                console.warn('刷新 token 失败，可能需要重新登录');
                this.handleAuthError();
                return false;
            }

            const data = await response.json().catch(() => ({}));

            // 无论后端是否实际签发了新 token，都重置计时器
            // refreshed=false 表示 token 仍有效，refreshed=true 表示已签发新 token
            this.tokenObtainedAt = Date.now();

            return true;
        } catch (error) {
            console.error('刷新 token 失败:', error);
            this.handleAuthError();
            return false;
        }
    }

    /**
     * 获取用户信息
     */
    getUser() {
        const userStr = localStorage.getItem(this.storageKeys.user);
        if (userStr) {
            try {
                return JSON.parse(userStr);
            } catch (e) {
                return null;
            }
        }
        return null;
    }

    /**
     * 设置用户信息
     */
    setUser(user) {
        localStorage.setItem(this.storageKeys.user, JSON.stringify(user));
    }

    /**
     * 清除用户信息
     */
    clearUser() {
        localStorage.removeItem(this.storageKeys.user);
    }

    /**
     * 检查是否已登录
     * Cookie 模式下，通过检查用户信息是否存在来判断
     */
    isLoggedIn() {
        const user = this.getUser();
        return user !== null;
    }

    /**
     * 检查认证状态，未登录则跳转
     */
    checkAuth() {
        if (!this.isLoggedIn()) {
            this.handleAuthError();
            return false;
        }
        return true;
    }

    /**
     * 处理认证错误
     */
    handleAuthError() {
        this.clearUser();
        window.location.href = '/login';
    }

    /**
     * 发送带认证的请求
     * Cookie 会自动携带，无需手动注入 Authorization header
     * @param {string} url - 请求 URL
     * @param {Object} options - fetch 选项
     */
    async authFetch(url, options = {}) {
        // 确保已登录
        if (!this.isLoggedIn()) {
            this.handleAuthError();
            throw new Error('未登录');
        }

        // 合并 headers，确保 Content-Type 存在
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers
        };

        try {
            // 请求前检查 token 年龄，接近过期时先刷新
            if (this.shouldRefreshToken()) {
                await this.refreshToken();
            }

            const response = await fetch(url, {
                ...options,
                headers,
                credentials: 'include',  // 自动携带 Cookie
            });

            // 如果返回 401（token 在服务端被吊销等边缘情况），尝试刷新后重试
            if (response.status === 401) {
                const refreshed = await this.refreshToken();
                if (refreshed) {
                    // 重试请求
                    return fetch(url, {
                        ...options,
                        headers,
                        credentials: 'include',
                    });
                } else {
                    this.handleAuthError();
                    throw new Error('认证失败');
                }
            }

            return response;
        } catch (error) {
            throw error;
        }
    }
}

// 创建全局实例
const authManager = new AuthManager();

// 导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { AuthManager, authManager };
}
