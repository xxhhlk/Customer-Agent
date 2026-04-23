"""
火山引擎豆包多模态嵌入模型适配器

将纯文本输入转换为火山引擎多模态嵌入 API 格式
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import httpx
from loguru import logger

from agno.knowledge.embedder.base import Embedder


@dataclass
class VolcengineEmbedder(Embedder):
    """
    火山引擎豆包多模态嵌入模型适配器
    
    将纯文本转换为多模态格式：
    {
        "model": "doubao-embedding-vision-251215",
        "input": [{"type": "text", "text": "文本内容"}]
    }
    """
    id: str = "doubao-embedding-vision-251215"
    dimensions: Optional[int] = 1024  # 火山引擎多模态嵌入维度
    api_key: Optional[str] = None
    base_url: Optional[str] = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
    request_params: Optional[Dict[str, Any]] = None
    
    # 批处理参数
    batch_size: int = 4  # 火山引擎建议单次不超过4条文本
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
    
    def _build_request_body(self, texts: List[str]) -> Dict[str, Any]:
        """
        构建请求体
        
        将文本列表转换为多模态格式：
        [{"type": "text", "text": "文本1"}, {"type": "text", "text": "文本2"}]
        """
        input_data = [
            {"type": "text", "text": text}
            for text in texts
        ]
        
        body = {
            "model": self.id,
            "input": input_data,
        }
        
        if self.request_params:
            body.update(self.request_params)
        
        return body
    
    def get_embedding(self, text: str) -> List[float]:
        """获取单个文本的嵌入向量"""
        embeddings = self.get_embeddings_batch([text])
        return embeddings[0] if embeddings else []
    
    def get_embedding_and_usage(self, text: str) -> Tuple[List[float], Optional[Dict]]:
        """获取单个文本的嵌入向量和用量信息"""
        embeddings, usages = self.get_embeddings_batch_and_usage([text])
        if embeddings and usages:
            return embeddings[0], usages[0]
        return [], None
    
    def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """批量获取嵌入向量"""
        embeddings, _ = self.get_embeddings_batch_and_usage(texts)
        return embeddings
    
    def get_embeddings_batch_and_usage(
        self, texts: List[str]
    ) -> Tuple[List[List[float]], List[Optional[Dict]]]:
        """
        批量获取嵌入向量和用量信息
        
        Args:
            texts: 文本列表
            
        Returns:
            Tuple of (嵌入向量列表, 用量信息列表)
        """
        all_embeddings: List[List[float]] = []
        all_usage: List[Optional[Dict]] = []
        
        logger.info(f"正在获取 {len(texts)} 条文本的嵌入向量（批次大小: {self.batch_size}）")
        
        # 分批处理
        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i : i + self.batch_size]
            
            try:
                response = self._make_request(batch_texts)
                
                # 解析响应
                if response and "data" in response:
                    # 按 index 排序
                    sorted_data = sorted(response["data"], key=lambda x: x.get("index", 0))
                    batch_embeddings = [item["embedding"] for item in sorted_data]
                    all_embeddings.extend(batch_embeddings)
                    
                    # 用量信息
                    usage = response.get("usage")
                    usage_dict = usage if isinstance(usage, dict) else None
                    all_usage.extend([usage_dict] * len(batch_embeddings))
                else:
                    logger.warning(f"响应格式异常: {response}")
                    # 填充空结果
                    all_embeddings.extend([[] for _ in batch_texts])
                    all_usage.extend([None for _ in batch_texts])
                    
            except Exception as e:
                logger.error(f"获取嵌入向量失败: {e}")
                # 填充空结果
                all_embeddings.extend([[] for _ in batch_texts])
                all_usage.extend([None for _ in batch_texts])
        
        return all_embeddings, all_usage
    
    def _make_request(self, texts: List[str]) -> Optional[Dict[str, Any]]:
        """发送 HTTP 请求"""
        import json
        
        headers = self._get_headers()
        body = self._build_request_body(texts)
        
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    self.base_url,
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
                return response.json()
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP 错误: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None
    
    # 异步方法
    async def async_get_embedding(self, text: str) -> List[float]:
        """异步获取单个文本的嵌入向量"""
        embeddings = await self.async_get_embeddings_batch([text])
        return embeddings[0] if embeddings else []
    
    async def async_get_embedding_and_usage(self, text: str) -> Tuple[List[float], Optional[Dict]]:
        """异步获取单个文本的嵌入向量和用量信息"""
        embeddings, usages = await self.async_get_embeddings_batch_and_usage([text])
        if embeddings and usages:
            return embeddings[0], usages[0]
        return [], None
    
    async def async_get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """异步批量获取嵌入向量"""
        embeddings, _ = await self.async_get_embeddings_batch_and_usage(texts)
        return embeddings
    
    async def async_get_embeddings_batch_and_usage(
        self, texts: List[str]
    ) -> Tuple[List[List[float]], List[Optional[Dict]]]:
        """
        异步批量获取嵌入向量和用量信息
        """
        all_embeddings: List[List[float]] = []
        all_usage: List[Optional[Dict]] = []
        
        logger.info(f"正在异步获取 {len(texts)} 条文本的嵌入向量（批次大小: {self.batch_size}）")
        
        # 分批处理
        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i : i + self.batch_size]
            
            try:
                response = await self._async_make_request(batch_texts)
                
                if response and "data" in response:
                    sorted_data = sorted(response["data"], key=lambda x: x.get("index", 0))
                    batch_embeddings = [item["embedding"] for item in sorted_data]
                    all_embeddings.extend(batch_embeddings)
                    
                    usage = response.get("usage")
                    usage_dict = usage if isinstance(usage, dict) else None
                    all_usage.extend([usage_dict] * len(batch_embeddings))
                else:
                    logger.warning(f"响应格式异常: {response}")
                    all_embeddings.extend([[] for _ in batch_texts])
                    all_usage.extend([None for _ in batch_texts])
                    
            except Exception as e:
                logger.error(f"异步获取嵌入向量失败: {e}")
                all_embeddings.extend([[] for _ in batch_texts])
                all_usage.extend([None for _ in batch_texts])
        
        return all_embeddings, all_usage
    
    async def _async_make_request(self, texts: List[str]) -> Optional[Dict[str, Any]]:
        """异步发送 HTTP 请求"""
        headers = self._get_headers()
        body = self._build_request_body(texts)
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    self.base_url,
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
                return response.json()
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP 错误: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"异步请求失败: {e}")
            return None
