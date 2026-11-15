"""Agent 运行期的确定性终止信号；由引擎转换成终止代码。"""


class UngroundedAction(RuntimeError):
    """连续两次动作请求缺少有效假设或支持观测。"""
