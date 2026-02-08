import discord
from discord.ext import commands
from discord import app_commands
import asyncio
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

timers_active_focus = {}
timers_active_break = {}
streak_counter = {}
message_counter = {}

load_dotenv()
API_KEY = os.getenv('API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS streaks (
                user_id BIGINT PRIMARY KEY,
                day INT,
                month INT,
                year INT,
                value INT,
                reminded INT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                user_id BIGINT PRIMARY KEY,
                count INT
            )
        """)

async def load_user_data():
    global streak_counter, message_counter
    async with db_pool.acquire() as conn:

        rows = await conn.fetch("SELECT * FROM streaks")
        for row in rows:
            streak_counter[row['user_id']] = {
                "day": row['day'],
                "month": row['month'],
                "year": row['year'],
                "value": row['value'],
                "reminded": row['reminded']
            }

        
        rows = await conn.fetch("SELECT * FROM messages")
        for row in rows:
            message_counter[row['user_id']] = row['count']

test_guild = discord.Object(id=1468297881130897510)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class BotCreate(commands.Bot):
	def __init__(self):
		super().__init__(command_prefix="/", intents=intents)

bot = BotCreate()

async def save_all_data():
    async with db_pool.acquire() as conn:
        # Save messages
        for user_id, count in message_counter.items():
            await conn.execute("""
                INSERT INTO messages(user_id, count)
                VALUES($1,$2)
                ON CONFLICT(user_id) DO UPDATE
                SET count=$2
            """, user_id, count)
        
        # Save streaks
        for user_id, data in streak_counter.items():
            await conn.execute("""
                INSERT INTO streaks(user_id, day, month, year, value, reminded)
                VALUES($1,$2,$3,$4,$5,$6)
                ON CONFLICT(user_id) DO UPDATE
                SET day=$2, month=$3, year=$4, value=$5, reminded=$6
            """, user_id, data['day'], data['month'], data['year'], data['value'], data['reminded'])


async def periodic_save():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await save_all_data()
        except Exception as e:
            print(f"Error saving data periodically: {e}")
        await asyncio.sleep(60)

def shutdown_handler():
    print("Bot shutting down…")

    async def shutdown():
        print("Cancelling timers...")
        for task in timers_active_focus.values():
            task.cancel()
        for task in timers_active_break.values():
            task.cancel()

        print("Saving data...")
        await save_all_data()

        print("Closing database pool...")
        await db_pool.close()

        print("Closing bot...")
        await bot.close()

    loop = bot.loop

    if loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        try:
            fut.result(timeout=30)
        except Exception as e:
            print("Shutdown error:", e)
    else:
        asyncio.run(save_all_data())

    print("Shutdown complete.")
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
	global db_pool
	print(f"logged in as {bot.user}")
	await init_db()
	await load_user_data()
	try:
		synced = await bot.tree.sync()
		print(f"Synced {len(synced)} commands")
	except Exception as e:
		print(f"Failed to sync commands: {e}")
	bot.loop.create_task(streak_checker())
	bot.loop.create_task(periodic_save())


@bot.event
async def on_message(message):
	if message.author == bot.user:
		return

	user_id = message.author.id

	print(f"{message.author}:{message.content}")

	await bot.process_commands(message)

	message_counter[user_id] = message_counter.get(user_id, 0) + 1



@bot.tree.command(name="ping", description="check bot is working")
async def ping(ctx: discord.Interaction):
	await ctx.response.send_message("pong")

MAX_DISCORD_CHARS = 2000


def clean_ansi(text: str) -> str:
    """Remove ANSI escape sequences from Ollama output."""
    ansi_escape = re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

@bot.tree.command(name="chat", description="Chat to FlowBot!")
async def chat(ctx: discord.Interaction, *, message: str):
    await ctx.response.defer()
    process = None


    prompt = (
        f"You are FlowBot, a friendly and helpful Discord AI. "
        f"Always refer to yourself as FlowBot. Respond to the user in a clear and concise way.\n\n"
        f"User: {message}\nFlowBot:"
    )


    try:
        process = await asyncio.create_subprocess_exec(
            "ollama", "run", "mistral",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "OLLAMA_NO_SPINNER": "1", "OLLAMA_CLI_NO_SPINNER": "1"}
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode()),
            timeout=60
        )

        response = clean_ansi(stdout.decode().strip())

        
        if not response and stderr and stderr.strip():
            response = clean_ansi(stderr.decode().strip())

    except asyncio.TimeoutError:
        if process:
            process.kill()
        response = "⏱️ Model took too long to respond."

    except Exception as e:
        response = f"Error contacting Ollama: {e}"

    response = response.strip() or "⚠️ Model returned no output."

    
    if len(response) > MAX_DISCORD_CHARS:
        response = response[:MAX_DISCORD_CHARS - 3] + "..."

    await ctx.followup.send(response)



async def focus_timer(ctx: discord.Interaction, minutes: int):
	today = datetime.now()
	day = today.day
	month = today.month
	year = today.year
	user_id = ctx.user.id

	await asyncio.sleep(minutes*60)

	if user_id not in timers_active_focus:
		return
	if minutes == 1:
		await ctx.followup.send("timer ended")
		await ctx.followup.send(f"You have focused for {minutes} minute")
	elif minutes >= 20:
		await ctx.followup.send("timer ended")
		await ctx.followup.send(f"You have focused for {minutes} minutes! Wow thats Exceptional 🔥🔥🔥")
	elif minutes >= 10:
		await ctx.followup.send("timer ended")
		await ctx.followup.send(f"You have focused for {minutes} minutes! 🔥")

	if minutes >= 10:
		if user_id not in streak_counter:
			streak_counter[user_id] = {"day": day,"month": month,"year": year,"value": 1, "reminded": day}
			await ctx.followup.send("you have started a streak")
		else:
			last_date = date(streak_counter[user_id]["year"], streak_counter[user_id]["month"], streak_counter[user_id]["day"])
			today_date = date.today()
			days_diff = (today_date - last_date).days
			if days_diff == 0:
				pass
			elif days_diff >= 2:
				await ctx.followup.send("You gone for to long your streak has been reset to 1")
				streak_counter[user_id]["value"] = 1
			else:
				streak_counter[user_id]["value"] += 1
				streak_counter[user_id]["reminded"] = day
				await ctx.followup.send("You have added to you current streak!")
			
			streak_counter[user_id]["day"] = today_date.day
			streak_counter[user_id]["month"] = today_date.month
			streak_counter[user_id]["year"] = today_date.year
			streak_counter[user_id]["reminded"] = today_date.day

		data = streak_counter[user_id]
		async with db_pool.acquire() as conn:
			await conn.execute("""
        	INSERT INTO streaks(user_id, day, month, year, value, reminded)
        	VALUES($1,$2,$3,$4,$5,$6)
        	ON CONFLICT(user_id) DO UPDATE
        	SET day=$2, month=$3, year=$4, value=$5, reminded=$6
    	""", user_id, data['day'], data['month'], data['year'], data['value'], data['reminded'])

	timers_active_focus.pop(user_id, None)

@bot.tree.command(name="focus", description="start a focus timer")
async def focus(ctx: discord.Interaction, minutes: int):
	user_id = ctx.user.id
	if user_id in timers_active_focus:
		await ctx.response.send_message("You already have a timer started if you want to stop it type /stop_focus")
		return
	if minutes <= 0:
		await ctx.response.send_message("Please enter a positive value")
		return
	if minutes > 300:
		await ctx.response.send_message("Focus time cannot exceed 300 minutes")
		return
	
	await ctx.response.send_message("your focus timer has started")
	timers_active_focus[user_id] = asyncio.create_task(focus_timer(ctx, minutes))


async def break_timer(ctx: discord.Interaction, minutes: int):
	user_id = ctx.user.id
	
	await asyncio.sleep(minutes*60)

	if user_id not in timers_active_break:
		return

	await ctx.followup.send("your break timer has ended")

	timers_active_break.pop(user_id, None)


@bot.tree.command(name="break", description="start a Break timer")
async def rest(ctx: discord.Interaction, minutes: int):
	user_id = ctx.user.id
	if user_id in timers_active_break:
		await ctx.response.send_message("you already have a break timer started if you want to stop it type /stop_break")
		return
	if minutes <= 0:
		await ctx.response.send_message("Please enter a positive value")
		return
	if minutes > 300:
		await ctx.response.send_message("Break time cannot exceed 300 minutes")
		return

	timers_active_break[user_id] = asyncio.create_task(break_timer(ctx, minutes))


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
	await ctx.response.send_message(f"You have sent {count} messages")


@bot.tree.command(name="streak", description="check your focus streaks")
async def streak(ctx: discord.Interaction):
	if ctx.user.id not in streak_counter:
		await ctx.response.send_message("To start a streak start and finish a focus timer for 10 minutes or more.")
		return
	await ctx.response.send_message(f"Your daily focus streak is {streak_counter[ctx.user.id]['value']} 🔥")


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

					# Update DB with new reminded value
					async with db_pool.acquire() as conn:
						await conn.execute("""
							UPDATE streaks SET reminded=$2 WHERE user_id=$1
						""", user_id, day_now)
	
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


