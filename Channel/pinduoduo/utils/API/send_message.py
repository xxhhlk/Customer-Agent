from ..base_request import BaseRequest
from typing import Dict, Any, Optional


class SendMessage(BaseRequest):
    def __init__(self, shop_id: str, user_id: str, channel_name: str = "pinduoduo"):
        super().__init__(shop_id, user_id, channel_name)
        
        # 检查账户信息是否正确加载
        if not hasattr(self, 'account_name'):
            self.logger.error(f"无法在数据库中找到账户: shop_id={shop_id}, user_id={user_id}")
            raise ValueError("找不到指定的账户信息")

    def send_text(self, recipient_uid, message_content):
        """
        发送文本消息
        """
        url = "https://mms.pinduoduo.com/plateau/chat/send_message"
        data = {
            "data": {
                "cmd": "send_message",
                "request_id": self.generate_request_id(),
                "message": {
                    "to": {
                        "role": "user",
                        "uid": recipient_uid
                    },
                    "from": {
                        "role": "mall_cs"
                    },
                    "content": message_content,
                    "msg_id": None,
                    "type": 0,
                    "is_aut": 0,
                    "manual_reply": 1,
                },
            },
            "client": "WEB"
        }

        result = self.post(url, json_data=data)
        if result and result.get("success") == True:
            if result.get("result", {}).get("error_code") == 10002:
                error_msg = result.get('result', {}).get('error')
                self.logger.error(f"发送文本消息失败: {error_msg}")
                return error_msg
            else:
                return result
        else:
            self.logger.error(f"发送文本消息失败: {result}")
            return None

 
        
    def send_image(self, recipient_uid, image_url):
        """
        发送图片消息
        """
        url = "https://mms.pinduoduo.com/plateau/chat/send_message"
        data = {
            "data": {
                "cmd": "send_message",
                "request_id": self.generate_request_id(),
                "message": {
                    "to": {
                        "role": "user",
                        "uid": recipient_uid
                    },
                    "from": {
                        "role": "mall_cs"
                    },
                    "content": image_url,
                    "msg_id": None,
                    "type": 1,
                    "is_aut": 0,
                    "manual_reply": 1,
                }
            },
            "client": "WEB"
        }

        result = self.post(url, json_data=data)
        if result:
            self.logger.debug(f"发送图片消息成功: {result}")
            return result

    def send_video(self, recipient_uid, video_url, info: Optional[dict] = None):
        """
        发送视频消息（type=14）

        Args:
            recipient_uid: 接收方 UID
            video_url: 视频 URL
            info: 视频元数据，需包含 preview.url / duration 等，
                  缺少该字段时 PDD 会返回 error_code=30000 系统错误
        """
        url = "https://mms.pinduoduo.com/plateau/chat/send_message"
        message = {
            "to": {"role": "user", "uid": recipient_uid},
            "from": {
                "role": "mall_cs"
            },
            "content": video_url,
            "msg_id": None,
            "type": 14,
            "is_aut": 0,
            "manual_reply": 1,
        }
        # 注意：不传 chat_type 字段，与 send_text 保持一致
        # 之前传 "chat_type": "cs" 时，API 返回 success:True 但消息实际未送达
        if info and isinstance(info, dict):
            message["info"] = info

        import json
        payload = {
            "data": {
                "cmd": "send_message",
                "request_id": self.generate_request_id(),
                "message": message,
            },
            "client": "WEB"
        }
        self.logger.info(f"[SEND_VIDEO] ===== 发送视频消息 PAYLOAD =====")
        self.logger.info(f"[SEND_VIDEO] recipient={recipient_uid}, "
                        f"video_url={video_url[:120]}..., "
                        f"video_url_len={len(video_url) if video_url else 0}, "
                        f"has_info={'YES' if info else 'NO'}")
        self.logger.info(f"[SEND_VIDEO] 完整 message: {json.dumps(message, ensure_ascii=False, default=str)}")
        self.logger.info(f"[SEND_VIDEO] 完整 payload: {json.dumps(payload, ensure_ascii=False, default=str)}")

        data = payload

        result = self.post(url, json_data=data)
        if result:
            self.logger.info(f"[SEND_VIDEO] ===== 发送视频消息 RESPONSE =====")
            self.logger.info(f"[SEND_VIDEO] 完整响应: {json.dumps(result, ensure_ascii=False, default=str)}")
            if result.get("success") == True:
                inner = result.get("result", {})
                # PDD 视频消息额外校验：result.result 为 "fail" 时表示参数错误等
                if inner.get("result") == "fail":
                    reason = inner.get("reason", "未知错误")
                    self.logger.error(f"[SEND_VIDEO] 发送视频消息失败: result=fail, reason={reason}")
                    return result
                error_code = inner.get("error_code")
                if error_code and error_code != 0:
                    error_msg = inner.get("error", "未知错误")
                    self.logger.error(f"[SEND_VIDEO] 发送视频消息失败: error_code={error_code}, error={error_msg}")
                    return result
                self.logger.info(f"[SEND_VIDEO] 发送视频消息成功: msg_id={inner.get('msg_id')}")
            else:
                self.logger.error(f"[SEND_VIDEO] 发送视频消息失败: success=False, result={result}")
            return result
        else:
            self.logger.error(f"[SEND_VIDEO] 发送视频消息失败: 请求返回None")
            return None


    def send_mallGoodsCard(self, recipient_uid, goods_id, biz_type: int = 2):
        """
        发送商城商品卡片消息

        Args:
            recipient_uid: 接收消息的用户UID
            goods_id: 商品ID
            biz_type: 业务类型，默认2（客服推荐商品）
        """
        url = "https://mms.pinduoduo.com/plateau/message/send/mallGoodsCard"
        data = {
            "uid": recipient_uid,
            "goods_id": goods_id,
            "biz_type": biz_type
        }

        # anti-content 从 cookies 中获取（由后端动态生成）
        anti_content = self.cookies.get('anti_content') or self.cookies.get('anti-content', '')

        # 构建完整请求头
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "anti-content": anti_content,
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://mms.pinduoduo.com",
            "priority": "u=1, i",
            "referer": "https://mms.pinduoduo.com/chat-merchant/index.html",
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        }

        result = self.post(url, json_data=data, headers=headers)
        if result:
            if result.get("success"):
                self.logger.info(f"商品卡片发送成功: goods_id={goods_id}, to={recipient_uid}, biz_type={biz_type}")
            else:
                self.logger.error(f"商品卡片发送失败: {result.get('error_msg', '未知错误')}")
            return result


    def getAssignCsList(self):
        """
        获取分配的客服列表
        """
        url = "https://mms.pinduoduo.com/latitude/assign/getAssignCsList"
        data = {"wechatCheck": True}
        
        result = self.post(url, json_data=data)
        if result and result.get('success'):
            return result['result']['csList']
        else:
            error_msg = result.get('result', {}).get('error') if result else "请求失败"
            self.logger.error(f"获取分配的客服列表失败: {error_msg}")
            return None


    def move_conversation(self, recipient_uid, cs_uid):
        """
        转移会话
        """
        url = "https://mms.pinduoduo.com/plateau/chat/move_conversation"
        data = {
            "data": {
                "cmd": "move_conversation",
                "request_id": self.generate_request_id(),
                "conversation": {
                    "csid": cs_uid,
                    "uid": recipient_uid,
                    "need_wx": False,
                    "remark": "无原因直接转移"
                }
            },
            "client": "WEB"
        }
        
        result = self.post(url, json_data=data)
        if result:
            self.logger.debug(f"转移会话成功: {result}")
            return result
