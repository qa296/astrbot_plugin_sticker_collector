# 几行代码开发一个插件！ -> 好吧，可能不止几行 :)
# main.py

import asyncio
import os
import json
import random
import aiosqlite

# 导入 AstrBot 核心 API
from astrbot.api.star import Star, register, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import logger, message_components as Comp
from astrbot.api.config import AstrBotConfig
from astrbot.api.provider import ProviderRequest, LLMResponse

# 定义数据库文件名
DB_NAME = "sticker_collector.db"
# AI 分析的 Prompt
ANALYZE_PROMPT = """
你是一个专业的表情包（Meme/Sticker）鉴定师。
请判断我提供的图片是否是一个适合在聊天中使用的表情包。
你必须以一个严格的 JSON 格式回复，不要包含任何其他说明文字。
JSON 格式如下:
{
  "is_sticker": boolean, // 是否是表情包
  "confidence": float, // 你判断的置信度，范围 0.0 到 1.0
  "emotion": string, // 如果是表情包，它表达的主要情感。例如：高兴, 悲伤, 愤怒, 惊讶, 搞笑, 无语, 赞同, 反对, 吃瓜。如果不是，则为 null。
  "description": string // 如果是表情包，用简短的中文描述图片内容和文字，用于后续搜索。如果不是，则为 null。
}
"""


@register(
    "sticker_collector",
    "YourName",
    "一个AI驱动的表情包搜集与发送工具",
    "1.0.0",
    "https://github.com/YourName/astrabot_plugin_sticker_collector",
)
class StickerCollectorPlugin(Star):
    def init(self, context: Context, config: AstrBotConfig):
        super().init(context)
        self.config = config
        self.db_path = os.path.join(context.get_data_dir(), DB_NAME)
        # 使用 asyncio.create_task 在后台初始化数据库，不阻塞主流程
        asyncio.create_task(self._init_db())
        logger.info("Sticker Collector 插件已加载。")

    async def _init_db(self):
        """初始化数据库，创建表结构"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stickers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL UNIQUE,
                        emotion TEXT NOT NULL,
                        description TEXT NOT NULL,
                        source_platform TEXT,
                        source_group_id TEXT,
                        source_sender_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                await db.commit()
            logger.info("表情包数据库初始化完成。")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    # --- 特性 1 & 2 & 3: 自动搜集、AI审核、分析入库 ---
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def auto_collect_sticker(self, event: AstrMessageEvent):
        # 检查配置是否开启了自动搜集
        if not self.config.get("auto_collect_enabled", True):
            return

        # 提取消息中的图片
        image_comp = None
        for comp in event.message_obj.message:
            if isinstance(comp, Comp.Image):
                image_comp = comp
                break

        if not image_comp or not image_comp.url:
            return # 消息中没有图片或图片URL，直接返回

        # 调用多模态LLM进行分析
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("Sticker Collector: 未启用任何LLM Provider，无法分析图片。")
            return

        logger.info(f"检测到图片: {image_comp.url}，准备交由AI分析...")
        
        try:
            # 使用最底层的 text_chat 调用，因为它能处理多模态输入
            llm_response: LLMResponse = await provider.text_chat(
                prompt=ANALYZE_PROMPT,
                image_urls=[image_comp.url],
                system_prompt="你是一个专业的表情包（Meme/Sticker）鉴定师。",
                contexts=[]
            )

            # 解析AI返回的JSON
            analysis_result = json.loads(llm_response.completion_text)
            
            is_sticker = analysis_result.get("is_sticker", False)
            confidence = analysis_result.get("confidence", 0.0)
            min_confidence = self.config.get("min_confidence", 0.8)

            if is_sticker and confidence >= min_confidence:
                emotion = analysis_result.get("emotion")
                description = analysis_result.get("description")
                
                # 存入数据库
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO stickers (url, emotion, description, source_platform, source_group_id, source_sender_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            image_comp.url,
                            emotion,
                            description,
                            event.get_platform_name(),
                            event.get_group_id(),
                            event.get_sender_id(),
                        ),
                    )
                    await db.commit()
                logger.info(f"成功搜集表情包: [情感: {emotion}] [描述: {description}]")

        except json.JSONDecodeError:
            logger.warning(f"AI返回的不是有效的JSON: {llm_response.completion_text}")
        except Exception as e:
            logger.error(f"分析或存储表情包时出错: {e}")
        finally:
            # 停止事件传播，防止机器人对这张图片做出其他反应
            event.stop_event()

    # --- 特性 4: LLM 函数工具，用于发送表情包 ---
    @filter.llm_tool(name="send_sticker")
    async def send_sticker_tool(self, event: AstrMessageEvent, emotion: str, keywords: str = "") -> MessageEventResult:
        """
        根据情感和关键词发送一个合适的表情包。当用户想发表情时调用此工具。
        
        Args:
            emotion (string): 必要参数。表情包需要表达的情感，例如：'高兴', '悲伤', '愤怒', '惊讶', '搞笑', '无语', '赞同'。
            keywords (string): 可选参数。用于更精确搜索的关键词，描述表情包的内容或文字。
        """
        logger.info(f"LLM请求发送表情: [情感: {emotion}] [关键词: {keywords}]")
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row # 方便按列名访问
                query = "SELECT url FROM stickers WHERE emotion LIKE ?"
                params = [f"%{emotion}%"]
                
                if keywords:
                    query += " AND description LIKE ?"
                    params.append(f"%{keywords}%")
                
                query += " ORDER BY RANDOM() LIMIT 1" # 随机选择一个
                
                cursor = await db.execute(query, tuple(params))
                row = await cursor.fetchone()

                if row:
                    sticker_url = row["url"]
                    logger.info(f"找到表情包: {sticker_url}")
                    # 使用 image_result 发送图片
                    yield event.image_result(sticker_url)
                else:
                    logger.warning("未在数据库中找到匹配的表情包。")
                    yield event.plain_result(f"抱歉，我还没存有关于“{emotion} {keywords}”的表情包。")

        except Exception as e:
            logger.error(f"从数据库检索表情包时出错: {e}")
            yield event.plain_result("哎呀，我的表情包数据库出错了！")

    # --- 特性 5: 插件管理指令 ---
    @filter.command_group("sticker")
    async def sticker_cmd_group(self):
        """表情包管理指令组"""
        pass

    @sticker_cmd_group.command("search")
    async def search_sticker(self, event: AstrMessageEvent, keyword: str):
        """根据关键词搜索表情包"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT url, emotion, description FROM stickers WHERE description LIKE ? OR emotion LIKE ? LIMIT 5",
                    (f"%{keyword}%", f"%{keyword}%")
                )
                rows = await cursor.fetchall()
                if rows:
                    response = f"找到关于“{keyword}”的 {len(rows)} 个表情：\n"
                    for row in rows:
                        response += f"- [情感: {row['emotion']}] {row['description']}\n"
                    yield event.plain_result(response)
                else:
                    yield event.plain_result(f"未找到关于“{keyword}”的表情。")
        except Exception as e:
            logger.error(f"搜索表情包时出错: {e}")
            yield event.plain_result("搜索时出错。")
            
    @sticker_cmd_group.command("count")
    async def count_stickers(self, event: AstrMessageEvent):
        """统计当前数据库中有多少表情包"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM stickers")
                count = (await cursor.fetchone())[0]
                yield event.plain_result(f"我的表情包仓库里现在有 {count} 个表情啦！")
        except Exception as e:
            logger.error(f"统计表情包时出错: {e}")
            yield event.plain_result("统计时出错。")