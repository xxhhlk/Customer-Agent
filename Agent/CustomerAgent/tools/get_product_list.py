from agno.tools import tool
from Channel.pinduoduo.utils.API.product_manager import ProductManager
from agno.run import RunContext
from utils.logger_loguru import get_logger
import json

logger = get_logger("GetProductListTool")

@tool(name="get_shop_products", description="获取店铺商品列表，用于客服主动推荐。")
def get_shop_products(run_context:RunContext) -> str:
    """
    获取店铺商品列表，用于客服主动推荐。

    Returns:
        str: 格式化的商品列表信息，包含商品名称、ID、价格、销量、库存等
    """
    try:
        deps = run_context.dependencies
        if deps is None:
            return "获取商品列表失败：缺少依赖信息"

        shop_id = deps.get("shop_id")
        user_id = deps.get("user_id")
        # 验证参数
        if not shop_id or not user_id:
            return "获取商品列表失败：缺少必要的shop_id或user_id参数"

        # 初始化ProductManager，传入shop_id和user_id以获取正确的cookies
        product_manager = ProductManager(shop_id=shop_id, user_id=user_id)

        # 调用API获取商品列表
        result = product_manager.get_product_list(
            page=1,
            size=10
        )

        if result.get("success"):
            products = result.get("products", [])
            total = result.get("total", 0)

            if not products:
                return f"店铺当前暂无商品 (shop_id: {shop_id})"

            return _format_products_output(products, total, page=1)

        else:
            error_msg = result.get("error_msg", "未知错误")
            logger.error(f"获取商品列表失败: {error_msg}")
            return f"获取商品列表失败: {error_msg}"

    except Exception as e:
        logger.error(f"工具执行异常: {str(e)}")
        return f"获取商品列表时发生异常: {str(e)}"

def _format_products_output(products, total, page):
    """格式化商品列表输出"""
    if not products:
        return "未找到商品"

    output = f"商品列表 (共{total}个商品，第{page}页):\n\n"

    for i, product in enumerate(products, 1):
        goods_id = product.get("goods_id", "未知ID")
        goods_name = product.get("goods_name", "未命名商品")
        price = product.get("price", "")
        sold_quantity = product.get("sold_quantity", 0)
        sold_quantity_30d = product.get("sold_quantity_30d", 0)
        quantity = product.get("quantity", 0)
        is_spike = product.get("is_spike", False)
        support_customize = product.get("support_customize", False)
        tag = product.get("tag", "")

        output += f"{i}. {goods_name} (ID: {goods_id})\n"

        if price:
            output += f"   价格: {price} 元\n"
        if sold_quantity:
            output += f"   已售: {sold_quantity}\n"
        if sold_quantity_30d:
            output += f"   30天销量: {sold_quantity_30d}\n"
        if quantity:
            output += f"   库存: {quantity}\n"
        if is_spike:
            output += f"   [秒杀商品]\n"
        if support_customize:
            output += f"   [支持定制]\n"
        if tag:
            output += f"   标签: {tag}\n"

        output += "\n"

    return output