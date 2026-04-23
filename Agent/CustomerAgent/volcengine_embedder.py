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
    
    注意：火山引擎多模态嵌入 API 每次请求返回一个合并的 embedding，
    所以需要逐个文本请求。
    """
    id: str = "doubao-embedding-vision-251215"
    dimensions: Optional[int] = 2048  # 火山引擎多模态嵌入维度
    api_key: Optional[str] = None
    base_url: Optional[str] = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
    request_params: Optional[Dict[str, Any]] = None
    enable_batch: bool = True  # 支持批量嵌入
    
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
        logger.debug(f"get_embedding_and_usage() 被调用，文本长度: {len(text)}")
        embeddings, usages = self.get_embeddings_batch_and_usage([text])
        logger.debug(f"get_embeddings_batch_and_usage() 返回: embeddings 数量={len(embeddings)}, usages 数量={len(usages)}")
        if embeddings and usages:
            logger.debug(f"返回第一个嵌入，维度: {len(embeddings[0])}")
            return embeddings[0], usages[0]
        logger.warning(f"embeddings 或 usages 为空，返回 ([], None)")
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
        
        注意：火山引擎多模态嵌入 API 每次请求返回一个合并的 embedding，
        所以我们需要逐个文本请求（或每批一个文本）。
        
        Args:
            texts: 文本列表
            
        Returns:
            Tuple of (嵌入向量列表, 用量信息列表)
        """
        all_embeddings: List[List[float]] = []
        all_usage: List[Optional[Dict]] = []
        
        logger.info(f"正在获取 {len(texts)} 条文本的嵌入向量")
        
        # 火山引擎多模态嵌入 API 每次请求返回一个 embedding
        # 所以需要逐个处理
        for text in texts:
            try:
                response = self._make_request([text])
                
                # 解析响应 - data 是对象，不是数组
                if response and "data" in response:
                    data = response["data"]
                    logger.debug(f"响应 data 类型: {type(data).__name__}")
                    if isinstance(data, dict) and "embedding" in data:
                        embedding = data["embedding"]
                        logger.info(f"成功获取嵌入向量，维度: {len(embedding)}")
                        all_embeddings.append(embedding)
                        
                        # 用量信息
                        usage = response.get("usage")
                        usage_dict = usage if isinstance(usage, dict) else None
                        all_usage.append(usage_dict)
                    else:
                        logger.warning(f"响应 data 格式异常: {data}")
                        all_embeddings.append([])
                        all_usage.append(None)
                else:
                    logger.warning(f"响应格式异常: {response}")
                    all_embeddings.append([])
                    all_usage.append(None)
                    
            except Exception as e:
                logger.error(f"获取嵌入向量失败: {e}")
                all_embeddings.append([])
                all_usage.append(None)
        
        return all_embeddings, all_usage
    
    def _make_request(self, texts: List[str]) -> Optional[Dict[str, Any]]:
        """发送 HTTP 请求"""
        import json
        
        headers = self._get_headers()
        body = self._build_request_body(texts)
        
        logger.debug(f"请求 URL: {self.base_url}")
        logger.debug(f"请求 body: {json.dumps(body, ensure_ascii=False)[:200]}...")
        
        # 检查 base_url 是否为 None
        if self.base_url is None:
            raise ValueError("base_url 不能为 None")
        
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    self.base_url,
                    headers=headers,
                    json=body,
                )
                logger.debug(f"响应状态码: {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"响应内容: {response.text[:500]}")
                response.raise_for_status()
                
                # 打印完整响应用于调试
                response_json = response.json()
                logger.debug(f"响应包含 data: {'data' in response_json}, usage: {'usage' in response_json}")
                return response_json
                
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
        
        注意：火山引擎多模态嵌入 API 每次请求返回一个合并的 embedding，
        所以我们需要逐个文本请求。
        """
        all_embeddings: List[List[float]] = []
        all_usage: List[Optional[Dict]] = []
        
        logger.info(f"正在异步获取 {len(texts)} 条文本的嵌入向量")
        
        # 火山引擎多模态嵌入 API 每次请求返回一个 embedding
        # 所以需要逐个处理
        for text in texts:
            try:
                response = await self._async_make_request([text])
                
                # 解析响应 - data 是对象，不是数组
                if response and "data" in response:
                    data = response["data"]
                    logger.debug(f"异步响应 data 类型: {type(data).__name__}")
                    if isinstance(data, dict) and "embedding" in data:
                        embedding = data["embedding"]
                        logger.info(f"成功获取异步嵌入向量，维度: {len(embedding)}")
                        all_embeddings.append(embedding)
                        
                        # 用量信息
                        usage = response.get("usage")
                        usage_dict = usage if isinstance(usage, dict) else None
                        all_usage.append(usage_dict)
                    else:
                        logger.warning(f"异步响应 data 格式异常: {data}")
                        all_embeddings.append([])
                        all_usage.append(None)
                else:
                    logger.warning(f"异步响应格式异常: {response}")
                    all_embeddings.append([])
                    all_usage.append(None)
                    
            except Exception as e:
                logger.error(f"异步获取嵌入向量失败: {e}")
                all_embeddings.append([])
                all_usage.append(None)
        
        return all_embeddings, all_usage
    
    async def _async_make_request(self, texts: List[str]) -> Optional[Dict[str, Any]]:
        """异步发送 HTTP 请求"""
        headers = self._get_headers()
        body = self._build_request_body(texts)
        
        # 检查 base_url 是否为 None
        if self.base_url is None:
            raise ValueError("base_url 不能为 None")
        
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
