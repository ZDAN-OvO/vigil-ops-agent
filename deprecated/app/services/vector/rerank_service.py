"""向量重排服务模块 - 基于 DashScope Rerank HTTP API 实现精排

使用 HTTP 请求方式调用 qwen3-rerank 模型，避免 SDK 版本限制。
接口地址：{dashscope_biz_space_api_base}/compatible-api/v1/reranks
"""

import json
from typing import List

import requests
from http import HTTPStatus
from loguru import logger

from app.config import config


class VectorRerankService:
    """
    向量重排服务 - 使用 DashScope Rerank HTTP API 对候选文档进行精排

    工作流程：
    1. 接收 query 和候选文档列表（由粗排阶段产生）
    2. 调用 DashScope Rerank HTTP API 计算每个文档与 query 的相关性分数
    3. 按分数降序排序，返回 top_k 个最相关的文档
    """

    def __init__(self):
        """初始化向量重排服务"""
        self.model = config.dashscope_rerank_model
        self.api_key = config.dashscope_api_key
        self.api_url = (
            f"{config.dashscope_biz_space_api_base}/services/rerank/text-rerank/text-rerank"
        )
        logger.info(
            f"向量重排服务初始化完成, 模型: {self.model}, API: {self.api_url[:11]}...{self.api_url[-11:]}"
        )

    def rerank(
        self,
        query: str,
        documents: List[str],
        instruct: str = "Retrieve semantically similar text.",
        top_k: int = 3,
    ) -> List[dict]:
        """
        对候选文档进行重排序

        Args:
            query: 查询文本
            documents: 候选文档文本列表
            top_k: 返回前 K 个最相关文档

        Returns:
            List[dict]: 重排后的文档列表，每项包含:
                - index: 原始文档在输入列表中的索引
                - relevance_score: 相关性分数（越高越相关）
        """
        if not documents:
            logger.warning("候选文档列表为空，跳过重排")
            return []

        if not query or not query.strip():
            logger.warning("查询文本为空，跳过重排")
            return []

        try:
            logger.info(
                f"开始 Rerank: query='{query[:50]}...', 候选数: {len(documents)}, top_k: {top_k}"
            )

            # 构建请求头
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            # 构建请求体
            # instruct: 自定义排序任务类型说明,是 rerank 模型的系统提示词，指导模型理解检索任务
            request_body = {
                "model": self.model,
                "input": {"query": query, "documents": documents},
                "parameters": {
                    "return_documents": True,
                    "top_n": top_k,
                    "instruct": instruct,
                },
            }

            logger.debug(f"Rerank 请求 URL: {self.api_url[:11]}...{self.api_url[-11:]}")
            logger.debug(
                f"Rerank 请求体: {json.dumps(request_body)[:25]}...{json.dumps(request_body)[-25:]}"
            )

            # 发送 HTTP 请求
            response = requests.post(
                self.api_url,
                json=request_body,
                headers=headers,
            )

            # 解析响应
            if response.status_code != HTTPStatus.OK:
                error_msg = response.text
                logger.error(
                    f"Rerank API 调用失败: status_code={response.status_code}, message={error_msg}"
                )
                raise RuntimeError(f"Rerank API 调用失败: {response.status_code} - {error_msg}")

            # 解析 JSON 响应
            response_data = response.json()

            # 提取重排结果
            # 响应格式: {"output": {"results": [{"document": {"text": "..."}, "index": 0, "relevance_score": 0.95}, ...]}}
            rerank_results = []
            for item in response_data.get("output", {}).get("results", []):
                rerank_results.append(
                    {
                        "index": item["index"],
                        "text": item["document"]["text"],
                        "relevance_score": item["relevance_score"],
                    }
                )

            logger.info(
                f"Rerank 完成, 返回 {len(rerank_results)} 个结果, "
                f"最高分: {rerank_results[0]['relevance_score']:.4f}"
                if rerank_results
                else "Rerank 完成, 无结果"
            )

            return rerank_results

        except requests.RequestException as e:
            logger.error(f"Rerank HTTP 请求失败: {e}")
            raise RuntimeError(f"Rerank HTTP 请求失败: {e}") from e
        except Exception as e:
            logger.error(f"Rerank 重排失败: {e}")
            raise RuntimeError(f"Rerank 重排失败: {e}") from e


# 全局单例
vector_rerank_service = VectorRerankService()

if __name__ == "__main__":
    vector_rerank_service.rerank(
        "什么是文本排序模型",
        [
            "文本排序模型广泛用于搜索引擎和推荐系统中，它们根据文本相关性对候选文本进行排序",
            "量子计算是计算科学的一个前沿领域",
            "预训练语言模型的发展给文本排序模型带来了新的进展",
        ],
    )
