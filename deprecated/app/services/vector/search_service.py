"""向量检索服务模块 - 检索编排层

职责：组合粗排（Milvus 向量检索）和精排（Rerank API）策略，
提供统一的两阶段检索接口。

依赖链：knowledge_tool → vector_search_service → vector_store_manager (粗排)
                                           → vector_rerank_service (精排)
"""

from typing import List

from langchain_core.documents import Document
from loguru import logger

from app.services.vector.store_manager import vector_store_manager


class VectorSearchService:
    """
    向量检索服务 - 负责检索编排（粗排 + 精排）

    工作流程：
    1. 调用 vector_store_manager.similarity_search() 粗排召回候选文档
    2. 如果启用 Rerank，调用 vector_rerank_service.rerank() 精排
    3. 返回 List[Document]，score 存储在 metadata["score"] 中
    """

    def __init__(self):
        """初始化向量检索服务"""
        logger.info("向量检索服务初始化完成")

    def search(
            self, query: str, top_k: int = 5, rerank: bool = True, candidate_count: int = 50
    ) -> List[Document]:
        """
        搜索相似文档（两阶段检索：粗排 + Rerank 精排）

        当 rerank=True 时：
        1. 粗排：用 embedding 从 Milvus 召回 rerank_candidate_count 条候选
        2. 精排：用 DashScope Rerank API 重新打分，取 top_k 条

        当 rerank=False 时：
        直接用 embedding 搜索返回 top_k 条

        Args:
            query: 查询文本
            top_k: 返回最相似的 K 个结果
            rerank: 是否开启重排精排
            candidate_count: 候选文档最大数量，当 candidate_count > top_k * 10 的时候，此配置失效

        Returns:
            List[Document]: 搜索结果列表，score 存储在 metadata["score"] 中

        Raises:
            RuntimeError: 搜索失败时抛出
        """
        try:
            # 确定召回数量：启用 rerank 时召回更多候选，否则直接取 top_k
            if rerank:
                recall_count = min(top_k * 10, candidate_count)
            else:
                recall_count = top_k

            logger.info(
                f"开始搜索相似文档, 查询: {query[:50]}..., topK: {top_k}, "
                f"召回数: {recall_count}, rerank: {rerank}"
            )

            # 1. 粗排：调用 vector_store_manager 召回候选文档
            candidates = vector_store_manager.similarity_search(query, k=recall_count)

            if not candidates:
                logger.warning("粗排未检索到相关文档")
                return []

            # 2. 如果启用 Rerank，对候选文档进行精排
            if rerank and len(candidates) > top_k:
                results = self._rerank_documents(query, candidates, top_k)
            else:
                # 不启用 rerank 时，截取 top_k 条
                results = candidates[:top_k]
                # 为结果添加 score 字段（使用默认值 1.0 表示未精排）
                for doc in results:
                    doc.metadata["score"] = 1.0

            logger.info(f"搜索完成, 找到 {len(results)} 个相似文档")
            return results

        except Exception as e:
            logger.error(f"搜索相似文档失败: {e}")
            raise RuntimeError(f"搜索失败: {e}") from e

    def _rerank_documents(
            self,
            query: str,
            candidates: List[Document],
            top_k: int,
    ) -> List[Document]:
        """
        使用 DashScope Rerank API 对候选文档进行精排

        Args:
            query: 查询文本
            candidates: 候选文档列表
            top_k: 返回前 K 个最相关文档

        Returns:
            List[Document]: 精排后的文档列表，score 存储在 metadata["score"] 中
        """
        from app.services.vector.rerank_service import vector_rerank_service

        # 提取文档文本
        texts = [doc.page_content for doc in candidates]

        try:
            rerank_results = vector_rerank_service.rerank(
                query=query,
                documents=texts,
                top_k=top_k,
            )

            # 根据 rerank 结果的 index 重新排序 Document
            reranked = []
            for item in rerank_results:
                original_index = item["index"]
                if 0 <= original_index < len(candidates):
                    doc = candidates[original_index]
                    # 将 rerank 分数存储在 metadata 中
                    doc.metadata["score"] = item["relevance_score"]
                    reranked.append(doc)

            logger.info(f"Rerank 精排完成, {len(candidates)} -> {len(reranked)} 条")
            return reranked

        except Exception as e:
            # Rerank 失败时降级为原始排序，截取 top_k
            logger.warning(f"Rerank 失败, 降级为原始排序: {e}")
            results = candidates[:top_k]
            for doc in results:
                doc.metadata["score"] = 1.0
            return results


# 全局单例
vector_search_service = VectorSearchService()
