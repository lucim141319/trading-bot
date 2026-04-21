import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ── 日志配置 ──────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 环境变量 ──────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN")
BINANCE_API_KEY       = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY    = os.environ.get("BINANCE_SECRET_KEY")
BINANCE_SQUARE_COOKIE = os.environ.get("BINANCE_SQUARE_COOKIE", "")

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# ── 对话状态 ──────────────────────────────────────────
(
    WAIT_SYMBOL, WAIT_AMOUNT,
    WAIT_TP_MODE, WAIT_TP_VALUE,
    WAIT_SL_MODE, WAIT_SL_VALUE,
    WAIT_POST_TEXT
) = range(7)

user_order_data = {}

# ══════════════════════════════════════════════════════
#  主菜单
# ══════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 查看余额",    callback_data="balance")],
        [InlineKeyboardButton("📈 查币价",      callback_data="price")],
        [InlineKeyboardButton("📊 我的持仓",    callback_data="positions")],
        [InlineKeyboardButton("🟢 做多下单",    callback_data="open_long"),
         InlineKeyboardButton("🔴 做空下单",    callback_data="open_short")],
        [InlineKeyboardButton("❌ 一键平仓",    callback_data="close_all")],
        [InlineKeyboardButton("✍️ 发币安广场",  callback_data="post_square")],
    ]
    msg = update.message or update.callback_query.message
    await msg.reply_text(
        "🤖 *币安合约交易机器人 Pro*\n\n请选择操作：",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════
#  查询功能
# ══════════════════════════════════════════════════════
async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        account = client.futures_account_balance()
        usdt = next((a for a in account if a['asset'] == 'USDT'), None)
        if usdt:
            msg = (f"💰 *账户余额*\n\n"
                   f"总余额：`{float(usdt['balance']):.2f} USDT`\n"
                   f"可用余额：`{float(usdt['withdrawAvailable']):.2f} USDT`")
        else:
            msg = "未找到 USDT 余额"
        await query.edit_message_text(msg, parse_mode="Markdown")
    except BinanceAPIException as e:
        await query.edit_message_text(f"❌ 错误：{e.message}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"]
        msg = "📈 *实时币价*\n\n"
        for symbol in symbols:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            msg += f"`{symbol}`：`${float(ticker['price']):,.4f}`\n"
        await query.edit_message_text(msg, parse_mode="Markdown")
    except BinanceAPIException as e:
        await query.edit_message_text(f"❌ 错误：{e.message}")

async def get_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        positions = client.futures_position_information()
        active = [p for p in positions if float(p['positionAmt']) != 0]
        if not active:
            await query.edit_message_text("📊 当前没有持仓")
            return
        msg = "📊 *当前持仓*\n\n"
        for p in active:
            amt   = float(p['positionAmt'])
            side  = "🟢 做多" if amt > 0 else "🔴 做空"
            pnl   = float(p['unRealizedProfit'])
            emoji = "✅" if pnl >= 0 else "❌"
            msg += (f"{side} `{p['symbol']}`\n"
                    f"数量：`{amt}`\n"
                    f"开仓价：`{float(p['entryPrice']):.4f}`\n"
                    f"未实现盈亏：{emoji} `{pnl:.2f} USDT`\n\n")
        await query.edit_message_text(msg, parse_mode="Markdown")
    except BinanceAPIException as e:
        await query.edit_message_text(f"❌ 错误：{e.message}")

# ══════════════════════════════════════════════════════
#  下单对话流程（含止盈止损）
# ══════════════════════════════════════════════════════
async def open_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    side = "LONG" if query.data == "open_long" else "SHORT"
    user_order_data[query.from_user.id] = {"side": side}
    emoji = "🟢" if side == "LONG" else "🔴"
    await query.edit_message_text(
        f"{emoji} *{side} 下单*\n\n请输入币种名称\n例如：`BTC` 或 `ETH`",
        parse_mode="Markdown"
    )
    return WAIT_SYMBOL

async def got_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    symbol = update.message.text.upper().strip() + "USDT"
    try:
        price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
        user_order_data[uid]["symbol"] = symbol
        user_order_data[uid]["price"]  = price
        await update.message.reply_text(
            f"当前 `{symbol}` 价格：`${price:,.4f}`\n\n请输入下单金额（USDT）\n例如：`50`",
            parse_mode="Markdown"
        )
        return WAIT_AMOUNT
    except Exception:
        await update.message.reply_text("❌ 找不到该币种，请重新输入（例如：BTC）")
        return WAIT_SYMBOL

async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        amount = float(update.message.text.strip())
        user_order_data[uid]["amount"] = amount
        keyboard = [
            [InlineKeyboardButton("📌 手动输入止盈价格",  callback_data="tp_manual")],
            [InlineKeyboardButton("📊 按百分比自动计算",  callback_data="tp_percent")],
            [InlineKeyboardButton("⏭ 跳过止盈",          callback_data="tp_skip")],
        ]
        await update.message.reply_text(
            "✅ 金额已记录\n\n*止盈设置*：请选择方式",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAIT_TP_MODE
    except ValueError:
        await update.message.reply_text("❌ 请输入数字，例如：50")
        return WAIT_AMOUNT

# ── 止盈 ──────────────────────────────────────────────
async def tp_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    mode = query.data

    if mode == "tp_skip":
        user_order_data[uid]["tp"] = None
        return await _ask_sl(query)

    user_order_data[uid]["tp_mode"] = mode
    hint = "请输入止盈价格：\n例如：`95000`" if mode == "tp_manual" else "请输入止盈百分比：\n例如：`2` 代表 +2%"
    await query.edit_message_text(hint, parse_mode="Markdown")
    return WAIT_TP_VALUE

async def tp_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = user_order_data[uid]
    try:
        val = float(update.message.text.strip())
        if data["tp_mode"] == "tp_percent":
            tp_price = data["price"] * (1 + val/100) if data["side"] == "LONG" else data["price"] * (1 - val/100)
        else:
            tp_price = val
        data["tp"] = round(tp_price, 4)
        await update.message.reply_text(f"✅ 止盈价格：`{data['tp']}`", parse_mode="Markdown")
        keyboard = [
            [InlineKeyboardButton("📌 手动输入止损价格",  callback_data="sl_manual")],
            [InlineKeyboardButton("📊 按百分比自动计算",  callback_data="sl_percent")],
            [InlineKeyboardButton("⏭ 跳过止损",          callback_data="sl_skip")],
        ]
        await update.message.reply_text(
            "*止损设置*：请选择方式",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAIT_SL_MODE
    except ValueError:
        await update.message.reply_text("❌ 请输入数字")
        return WAIT_TP_VALUE

async def _ask_sl(query):
    keyboard = [
        [InlineKeyboardButton("📌 手动输入止损价格",  callback_data="sl_manual")],
        [InlineKeyboardButton("📊 按百分比自动计算",  callback_data="sl_percent")],
        [InlineKeyboardButton("⏭ 跳过止损",          callback_data="sl_skip")],
    ]
    await query.edit_message_text(
        "*止损设置*：请选择方式",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return WAIT_SL_MODE

# ── 止损 ──────────────────────────────────────────────
async def sl_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    mode = query.data

    if mode == "sl_skip":
        user_order_data[uid]["sl"] = None
        return await _execute_order_from_query(query)

    user_order_data[uid]["sl_mode"] = mode
    hint = "请输入止损价格：\n例如：`88000`" if mode == "sl_manual" else "请输入止损百分比：\n例如：`1` 代表 -1%"
    await query.edit_message_text(hint, parse_mode="Markdown")
    return WAIT_SL_VALUE

async def sl_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = user_order_data[uid]
    try:
        val = float(update.message.text.strip())
        if data["sl_mode"] == "sl_percent":
            sl_price = data["price"] * (1 - val/100) if data["side"] == "LONG" else data["price"] * (1 + val/100)
        else:
            sl_price = val
        data["sl"] = round(sl_price, 4)
        await update.message.reply_text(f"✅ 止损价格：`{data['sl']}`\n\n⏳ 正在下单...", parse_mode="Markdown")
        await _place_order(update.message, data)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ 请输入数字")
        return WAIT_SL_VALUE

async def _execute_order_from_query(query):
    uid  = query.from_user.id
    data = user_order_data[uid]
    await query.edit_message_text("⏳ 正在下单，请稍候...")
    await _place_order(query.message, data)
    return ConversationHandler.END

# ── 核心下单逻辑 ──────────────────────────────────────
async def _place_order(message, data):
    try:
        symbol = data["symbol"]
        side_b = "BUY" if data["side"] == "LONG" else "SELL"
        qty    = round(data["amount"] / data["price"], 3)

        order = client.futures_create_order(
            symbol=symbol, side=side_b, type="MARKET", quantity=qty
        )

        msg = (f"✅ *下单成功！*\n\n"
               f"币种：`{symbol}`\n"
               f"方向：{'🟢 做多' if data['side'] == 'LONG' else '🔴 做空'}\n"
               f"数量：`{qty}`\n"
               f"订单ID：`{order['orderId']}`\n")

        if data.get("tp"):
            tp_side = "SELL" if data["side"] == "LONG" else "BUY"
            client.futures_create_order(
                symbol=symbol, side=tp_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=data["tp"],
                closePosition=True,
                timeInForce="GTE_GTC"
            )
            msg += f"🎯 止盈价：`{data['tp']}`\n"

        if data.get("sl"):
            sl_side = "SELL" if data["side"] == "LONG" else "BUY"
            client.futures_create_order(
                symbol=symbol, side=sl_side,
                type="STOP_MARKET",
                stopPrice=data["sl"],
                closePosition=True,
                timeInForce="GTE_GTC"
            )
            msg += f"🛡 止损价：`{data['sl']}`\n"

        await message.reply_text(msg, parse_mode="Markdown")

    except BinanceAPIException as e:
        await message.reply_text(f"❌ 下单失败：{e.message}")
    except Exception as e:
        await message.reply_text(f"❌ 错误：{str(e)}")

# ══════════════════════════════════════════════════════
#  一键平仓
# ══════════════════════════════════════════════════════
async def close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        positions = client.futures_position_information()
        active = [p for p in positions if float(p['positionAmt']) != 0]
        if not active:
            await query.edit_message_text("📊 当前没有持仓，无需平仓")
            return
        for p in active:
            amt  = float(p['positionAmt'])
            side = "SELL" if amt > 0 else "BUY"
            client.futures_create_order(
                symbol=p['symbol'], side=side,
                type="MARKET", quantity=abs(amt), reduceOnly=True
            )
        await query.edit_message_text("✅ *所有仓位已平仓！*", parse_mode="Markdown")
    except BinanceAPIException as e:
        await query.edit_message_text(f"❌ 平仓失败：{e.message}")

# ══════════════════════════════════════════════════════
#  币安广场发文
# ══════════════════════════════════════════════════════
async def post_square_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("✍️ 手动写内容发送",     callback_data="sq_manual")],
        [InlineKeyboardButton("🤖 根据行情自动生成",   callback_data="sq_auto")],
    ]
    await query.edit_message_text(
        "✍️ *币安广场发文*\n\n请选择发文方式：",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def sq_manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("请输入你想发布的内容：")
    return WAIT_POST_TEXT

async def got_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    result = _publish_to_square(text)
    if result:
        await update.message.reply_text("✅ *发布成功！*", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "⚠️ *自动发布失败*（Cookie 未配置）\n\n请手动复制以下内容到币安广场发布：\n\n" + text
        )
    return ConversationHandler.END

async def sq_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ 正在根据行情自动生成内容...")
    try:
        btc  = float(client.futures_symbol_ticker(symbol="BTCUSDT")['price'])
        eth  = float(client.futures_symbol_ticker(symbol="ETHUSDT")['price'])
        sol  = float(client.futures_symbol_ticker(symbol="SOLUSDT")['price'])
        doge = float(client.futures_symbol_ticker(symbol="DOGEUSDT")['price'])
        text = (f"📊 今日行情播报\n\n"
                f"🟠 BTC：${btc:,.2f}\n"
                f"🔵 ETH：${eth:,.2f}\n"
                f"🟣 SOL：${sol:,.2f}\n"
                f"🐶 DOGE：${doge:,.5f}\n\n"
                f"市场波动较大，注意风险管理，设好止盈止损！\n"
                f"#BTC #ETH #合约交易 #币安广场")
        result = _publish_to_square(text)
        if result:
            await query.message.reply_text(f"✅ *发布成功！*\n\n{text}", parse_mode="Markdown")
        else:
            await query.message.reply_text(
                f"⚠️ 自动发布失败（Cookie 未配置）\n\n请手动复制以下内容到币安广场发布：\n\n{text}"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ 生成失败：{str(e)}")
    return ConversationHandler.END

def _publish_to_square(text: str) -> bool:
    if not BINANCE_SQUARE_COOKIE:
        return False
    try:
        headers = {
            "cookie":       BINANCE_SQUARE_COOKIE,
            "content-type": "application/json",
            "user-agent":   "Mozilla/5.0",
        }
        payload = {"content": text, "contentType": 1}
        resp = requests.post(
            "https://www.binance.com/bapi/social/v1/private/social/post/create",
            json=payload, headers=headers, timeout=10
        )
        return resp.status_code == 200
    except Exception:
        return False

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 已取消，发送 /start 返回主菜单")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════
#  主程序
# ══════════════════════════════════════════════════════
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 下单对话
    order_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(open_order_start, pattern="^open_(long|short)$")],
        states={
            WAIT_SYMBOL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_symbol)],
            WAIT_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            WAIT_TP_MODE:  [CallbackQueryHandler(tp_mode,  pattern="^tp_")],
            WAIT_TP_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tp_value)],
            WAIT_SL_MODE:  [CallbackQueryHandler(sl_mode,  pattern="^sl_")],
            WAIT_SL_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sl_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    # 发文对话
    post_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sq_manual_start, pattern="^sq_manual$")],
        states={
            WAIT_POST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_post_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(get_balance,       pattern="^balance$"))
    app.add_handler(CallbackQueryHandler(get_price,         pattern="^price$"))
    app.add_handler(CallbackQueryHandler(get_positions,     pattern="^positions$"))
    app.add_handler(CallbackQueryHandler(close_all,         pattern="^close_all$"))
    app.add_handler(CallbackQueryHandler(post_square_start, pattern="^post_square$"))
    app.add_handler(CallbackQueryHandler(sq_auto,           pattern="^sq_auto$"))
    app.add_handler(order_conv)
    app.add_handler(post_conv)

    logger.info("🤖 机器人启动中...")
    app.run_polling()

if __name__ == "__main__":
    main()
