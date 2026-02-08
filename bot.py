import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
from datetime import date
import random
import os
from dotenv import load_dotenv
import re
import asyncpg
import signal
import time
import sys
from typing import Dict, Set
import asyncio

timers_active_focus = {}
timers_active_break = {}
streak_counter = {}
message_counter = {}
chat_count = {}
active_chat = set()
pemium_users = {
     989639440358072371: True,
     931924643210752061: True
}

load_dotenv()
API_KEY = os.getenv('API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

db_pool = None

async def init_db():
	global db_pool
	db_pool = await asyncpg.create_pool(DATABASE_URL)
	
	async with db_pool.acquire() as conn:
		
		await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS streaks (
                user_id  BIGINT PRIMARY KEY,
                day      INT,
                month    INT,
                year     INT,
                value    INT,
                reminded INT
            )
            """
        )
		
		await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                user_id BIGINT PRIMARY KEY,
                count   INT
            )
            """
        )
		
		
		await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                user_id BIGINT PRIMARY KEY,
                count   INT,
                day     INT,
                month   INT
            )
            """
        )

async def load_user_data():
	global streak_counter, message_counter
	async with db_pool.acquire() as conn:
		for row in await conn.fetch("SELECT * FROM streaks"):
			streak_counter[row["user_id"]] = dict(row)
			
		for row in await conn.fetch("SELECT * FROM messages"):
			message_counter[row["user_id"]] = row["count"]
			
		for row in await conn.fetch("SELECT * FROM chats"):
			chat_count[row["user_id"]] = dict(row)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.messages = True

class FlowBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)

bot = FlowBot()

async def save_all_data():
    async with db_pool.acquire() as conn:

        for user_id, count in message_counter.items():
            await conn.execute(
                """
                INSERT INTO messages (user_id, count)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                SET count = $2
                """,
                user_id,
                count,
            )

        for user_id, data in streak_counter.items():
            await conn.execute(
                """
                INSERT INTO streaks (user_id, day, month, year, value, reminded)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE
                SET
                    day = $2,
                    month = $3,
                    year = $4,
                    value = $5,
                    reminded = $6
                """,
                user_id,
                data["day"],
                data["month"],
                data["year"],
                data["value"],
                data["reminded"],
            )

        for user_id, data in chat_count.items():
            await conn.execute(
                """
                INSERT INTO chats (user_id, count, day, month)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET
                    count = $2,
                    day = $3,
                    month = $4
                """,
                user_id,
                data["count"],
                data["day"],
                data["month"],
            )



async def periodic_save():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await save_all_data()
        except Exception as e:
            print("Periodic save error:", e)
        await asyncio.sleep(60)

def shutdown_handler():
    async def shutdown():
        for task in timers_active_focus.values():
            task.cancel()
        for task in timers_active_break.values():
            task.cancel()

        await save_all_data()
        await db_pool.close()
        await bot.close()

    loop = asyncio.get_running_loop()
    future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
    future.result(timeout=5) 
    sys.exit(0)

@bot.event
async def on_member_join(member: discord.Member):
	
	role_name = "Members"
	
	guild = member.guild
	role = discord.utils.get(guild.roles, name=role_name)
	
	if role is None:
		print(f"Role '{role_name}' not found in guild '{guild.name}'")
		return
	
	try:
		await member.add_roles(role)
		await member.send(f"Welcome {member} you are now a Member of {guild.name}")
		print(f"Assigned role '{role.name}' to {member.name}")
	
	except discord.Forbidden:
		print(f"Failed to assign role '{role.name}' to {member.name}: Missing permissions")



@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    await load_user_data()
    await bot.tree.sync()
    bot.loop.create_task(periodic_save())
    bot.loop.create_task(streak_checker())


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    message_counter[message.author.id] = message_counter.get(message.author.id, 0) + 1
    await bot.process_commands(message)



@bot.tree.command(name="ping", description="check bot is working")
async def ping(ctx: discord.Interaction):
	await ctx.response.send_message("pong")

MAX_DISCORD_CHARS = 2000


def clean_ansi(text: str) -> str:
    ansi = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
    return ansi.sub("", text)

@bot.tree.command(name="chat")
async def chat(ctx: discord.Interaction, *, message: str):
    user_id = ctx.user.id
    today = datetime.now()

    if user_id in active_chat:
        await ctx.response.send_message("You are already chatting.")
        return

    active_chat.add(user_id)

    try:
        day = today.day
        month = today.month

        if (
            user_id not in chat_count
            or chat_count[user_id]["day"] != day
            or chat_count[user_id]["month"] != month
        ):
            chat_count[user_id] = {"count": 0, "day": day, "month": month}
            

        if (user_id not in pemium_users) and chat_count[user_id]["count"] >= 3:
            await ctx.response.send_message("Daily chat limit reached. Only selected useres can use the chat command more than 3 times a day. because the bot is still in its early stages and we want to ensure a good experience for everyone. Please try again tomorrow!")
            return

        await ctx.response.defer()

        prompt = (
            "You are FlowBot, a helpful Discord assistant.\n"
            "You are apart of the FlowBot HQ server, where you assist users with their questions and provide helpful information.\n\n"
            
			f"{ctx.user.display_name}: {message}\nFlowBot:"
        )

        process = await asyncio.create_subprocess_exec(
            "ollama",
            "run",
            "mistral",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "OLLAMA_NO_SPINNER": "1"},
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode()), timeout=45
            )
            response = clean_ansi(stdout.decode().strip())
        except asyncio.TimeoutError:
            process.kill()
            response = "⏱️ Model timed out."

        if not response:
            response = "⚠️ No response from model."

        response = response[: MAX_DISCORD_CHARS - 3]

        chat_count[user_id]["count"] += 1
        await ctx.followup.send(response)

    finally:
        active_chat.discard(user_id)



async def focus_timer(ctx: discord.Interaction, minutes: int):
    await asyncio.sleep(minutes * 60)
    user_id = ctx.user.id

    if user_id not in timers_active_focus:
        return
    if minutes == 1:
        await ctx.followup.send(f"Focus session complete ({minutes} minute)")
    else:
        await ctx.followup.send(f"Focus session complete ({minutes} minutes)")

    if minutes >= 10:
        await ctx.followup.send("Your streak has been updated! Check it with /streak")
        today = date.today()

        if user_id not in streak_counter:
            streak_counter[user_id] = {
                "day": today.day,
                "month": today.month,
                "year": today.year,
                "value": 1,
                "reminded": today.day,
            }
        else:
            last = date(
                streak_counter[user_id]["year"],
                streak_counter[user_id]["month"],
                streak_counter[user_id]["day"],
            )
            diff = (today - last).days
            if diff == 1:
                streak_counter[user_id]["value"] += 1
            elif diff >= 2:
                streak_counter[user_id]["value"] = 1

            streak_counter[user_id].update(
                {
                    "day": today.day,
                    "month": today.month,
                    "year": today.year,
                    "reminded": today.day,
                }
            )

    timers_active_focus.pop(user_id, None)

@bot.tree.command(name="focus")
async def focus(ctx: discord.Interaction, minutes: int):
    user_id = ctx.user.id
    if user_id in timers_active_focus:
        await ctx.response.send_message("You already have an active focus timer.")
        return
    if minutes <= 0 or minutes > 300:
        await ctx.response.send_message("Focus timer cannot exceed 300 minutes or be less than 1 minute.")
        return

    timers_active_focus[user_id] = asyncio.create_task(
        focus_timer(ctx, minutes)
    )
    await ctx.response.send_message("Focus timer started.")

async def break_timer(ctx: discord.Interaction, minutes: int):
    await asyncio.sleep(minutes * 60)
    if ctx.user.id in timers_active_break:
        await ctx.followup.send("Break ended.")
        timers_active_break.pop(ctx.user.id, None)


@bot.tree.command(name="break")
async def rest(ctx: discord.Interaction, minutes: int):
    user_id = ctx.user.id
    if user_id in timers_active_focus:
        await ctx.response.send_message("You already have an active break timer.")
        return
    if minutes <= 0 or minutes > 300:
        await ctx.response.send_message("Break timer cannot exceed 300 minutes or be less than 1 minute.")
        return

    timers_active_break[user_id] = asyncio.create_task(
        break_timer(ctx, minutes)
    )
    await ctx.response.send_message("Break timer started.")


@bot.tree.command(name="stop_focus", description="stop focus timer")
async def stop_focus(ctx: discord.Interaction):
	user_id = ctx.user.id
	task = timers_active_focus.pop(user_id, None)
	if task:
		await ctx.response.send_message("Focus timer stopped")
		task.cancel()
	else:
		await ctx.response.send_message("You don't have an active focus timer")

@bot.tree.command(name="stop_break", description="stop break timer")
async def stop_break(ctx: discord.Interaction):
	user_id = ctx.user.id
	task = timers_active_break.pop(user_id, None)
	if task:
		await ctx.response.send_message("Break timer stopped")
		task.cancel()
	else:
		await ctx.response.send_message("You don't have an active break timer")

@bot.tree.command(name="track", description="check how many messages you have sent in the server")
async def track(ctx: discord.Interaction):
    user_id = ctx.user.id
    count = message_counter.get(user_id, 0)
    if count is None:
        await ctx.response.send_message("You haven’t sent any messages yet!")
    else:
        await ctx.response.send_message(f"You have sent {count} messages")


@bot.tree.command(name="streak")
async def streak(ctx: discord.Interaction):
    if ctx.user.id not in streak_counter:
        await ctx.response.send_message("No active streak. start a focus timer to build your streak!")
    else:
        await ctx.response.send_message(
            f"Your focus streak is {streak_counter[ctx.user.id]['value']} 🔥"
        )


async def streak_checker():
	await bot.wait_until_ready()
	while not bot.is_closed():
		hour_now = datetime.now().time().hour
		day_now = datetime.now().day
		month_now = datetime.now().month

		for user_id, data in list(streak_counter.items()):
			last_date = date(data["year"], data["month"], data["day"])
			today = date.today()
			days_diff = (today - last_date).days
			day_then = data["day"]
			month_then = data["month"]
			reminded = data["reminded"]
			if days_diff >= 2:
				try:
					user = await bot.fetch_user(user_id)
					await user.send("Your streak has ended, finish a focus timer to start a new streak")
					streak_counter.pop(user_id)
				except discord.Forbidden:
					print(f"Can't DM {user_id}")
			elif day_now == day_then and month_now == month_then:
				pass
			elif hour_now == 17:
				if reminded != day_now:
					reminders = [
						"Don’t forget your focus streak today! Start a session to keep the momentum going.",
						"Your streak is on the line! Finish a focus timer and keep it alive.",
						"Another day, another focus session! Let’s keep that streak shining.",
						"Consistency is key! Start a focus timer and maintain your streak.",
						"Time to focus! Your streak is waiting for you — don’t let it slip.",
						"Keep the streak going! A single focus session keeps the streak alive.",
						"Hit your focus target today and keep your streak strong!",
						"FlowMode ON! Start a focus timer and ride the streak wave.",
						"Your streak is valuable — don’t break it! Focus for a few minutes now.",
						"Little steps build big habits! Start a focus session to continue your streak."
					]
					user = await bot.fetch_user(user_id)
					await user.send(random.choice(reminders))
					streak_counter[user_id]["reminded"] = day_now
	
		await asyncio.sleep(60*60)


@bot.tree.command(name="quote", description="get a short motivational message")
async def quote(ctx: discord.Interaction):
	quotes = ["Don’t watch the clock; do what it does. Keep going.",
		   "Success is not final, failure is not fatal: It is the courage to continue that counts.",
		   "Believe you can and you’re halfway there.",
		   "The only way to do great work is to love what you do.",
		   "Dream big and dare to fail.",
		   "Your limitation—it’s only your imagination.",
		   "Push yourself, because no one else is going to do it for you.",
		   "Great things never come from comfort zones.",
		   "Don’t stop when you’re tired. Stop when you’re done.",
		   "Every day is a second chance."
		   ]
	await ctx.response.send_message(random.choice(quotes))

signal.signal(signal.SIGINT, lambda s,f: shutdown_handler())
signal.signal(signal.SIGTERM, lambda s,f: shutdown_handler())

bot.run(API_KEY)


