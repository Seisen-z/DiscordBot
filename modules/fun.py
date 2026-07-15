"""
modules/fun.py
Fun and entertainment commands + common utility commands for community interaction
"""

import discord
from discord import app_commands
from discord.ext import commands
import random
import aiohttp
import time
import re
from datetime import datetime

from modules.utils import load_json

FUN_CONFIG_FILE = "fun_configs"


def _load_fun_config() -> dict:
    try:
        data = load_json(FUN_CONFIG_FILE, {})
        return data if isinstance(data, dict) else {}
    except Exception as err:
        print(f"[Fun] Could not load {FUN_CONFIG_FILE}: {err}")
        return {}


def _merge_config_lines(default_lines: list[str], config: dict, key: str) -> list[str]:
    merged = list(default_lines)
    configured_lines = config.get(key, [])
    if not isinstance(configured_lines, list):
        return merged

    for line in configured_lines:
        if isinstance(line, str):
            cleaned = line.strip()
            if cleaned and cleaned not in merged:
                merged.append(cleaned)
    return merged


_FUN_KEYS = (
    "eight_ball",
    "roasts",
    "compliments",
    "jokes",
    "fun_facts",
    "riddles",
    "quotes",
    "hello_greetings",
)


def _fun_overlay_for_guild(guild_id: int | None) -> dict:
    root = _load_fun_config()
    if guild_id is None or not isinstance(root, dict):
        return {}
    gid = str(guild_id)
    guilds = root.get("guilds")
    if isinstance(guilds, dict):
        inner = guilds.get(gid)
        if isinstance(inner, dict):
            return inner
    if any(k in root for k in _FUN_KEYS):
        return {k: root[k] for k in _FUN_KEYS if k in root}
    return {}


def _fun_pick_lines(interaction: discord.Interaction, key: str, defaults: list) -> list:
    gid = interaction.guild.id if interaction.guild else None
    return _merge_config_lines(list(defaults), _fun_overlay_for_guild(gid), key)


# Fun responses and data
EIGHT_BALL_RESPONSES = [
    "🎱 It is certain",
    "🎱 Reply hazy, try again",
    "🎱 Don't count on it",
    "🎱 It is decidedly so",
    "🎱 Ask again later",
    "🎱 My reply is no",
    "🎱 Without a doubt",
    "🎱 Better not tell you now",
    "🎱 My sources say no",
    "🎱 Yes definitely",
    "🎱 Cannot predict now",
    "🎱 Outlook not so good",
    "🎱 You may rely on it",
    "🎱 Concentrate and ask again",
    "🎱 Very doubtful",
    "🎱 As I see it, yes",
    "🎱 Most likely",
    "🎱 Outlook good",
    "🎱 Yes",
    "🎱 Signs point to yes",
    "🎱 Trust your instincts on this one",
    "🎱 The odds are in your favor",
    "🎱 Not today, but soon",
    "🎱 Ask after a snack break",
    "🎱 This will make a great story",
    "🎱 Energy says yes",
    "🎱 Energy says no",
    "🎱 Plot twist: absolutely",
    "🎱 I'd prepare for both outcomes"
]

ROASTS = [
    "You're like a cloud... when you disappear, it's a beautiful day! ☁️",
    "I'd roast you, but my mom told me not to burn trash 🗑️",
    "You bring everyone so much joy... when you leave the room! 🚪",
    "If I wanted to kill myself, I'd climb your ego and jump to your IQ 📉",
    "You're not stupid, you just have bad luck thinking 🧠",
    "I'm not saying you're dumb, but you make me miss my ex 💔",
    "You're like a software update. When I see you, I think 'not now' 💻",
    "I would explain it to you, but I don't have any crayons 🖍️",
    "You're the human version of a typo in production 🧯",
    "You have something on your chin... no, the third one 🪞",
    "Your Wi-Fi signal has a stronger personality than you 📶",
    "You're like a loading screen: dramatic and slow ⏳",
    "If overthinking were cardio, you'd be an Olympian 🏅",
    "You bring chaos to a to-do list and call it strategy 📋",
    "You're proof that confidence can exist without directions 🧭",
    "You're not extra, you're the entire bonus round 🎰"
]

COMPLIMENTS = [
    "You're absolutely amazing! ✨",
    "Your smile could light up the whole world! 😊",
    "You have the best laugh! 😄",
    "You're incredibly talented! 🌟",
    "You make everything better just by being here! 💫",
    "You're one of the kindest people I know! 💖",
    "Your creativity knows no bounds! 🎨",
    "You have such a great sense of humor! 😂",
    "You're stronger than you know! 💪",
    "The world is a better place with you in it! 🌍",
    "You make hard things look easy! 🧠",
    "You're the kind of person people feel lucky to know 🤝",
    "Your energy lifts everyone around you ⚡",
    "You have elite main-character momentum today 🎬",
    "You are seriously underrated and over-delivering 📈",
    "Your presence makes any room better 🏠",
    "You are built for great things 🚀",
    "You're proof that kindness is powerful 💖",
    "You always find a way forward, and that is inspiring 🌟"
]

JOKES = [
    "Why don't scientists trust atoms? Because they make up everything! ⚛️",
    "Why did the scarecrow win an award? He was outstanding in his field! 🌾",
    "What do you call a fake noodle? An impasta! 🍝",
    "Why don't eggs tell jokes? They'd crack each other up! 🥚",
    "What do you call a dinosaur that crashes his car? Tyrannosaurus Wrecks! 🦕",
    "Why can't a bicycle stand up by itself? It's two-tired! 🚲",
    "What do you call a bear with no teeth? A gummy bear! 🐻",
    "Why don't skeletons fight each other? They don't have the guts! 💀",
    "Why did the math book look sad? It had too many problems! 📘",
    "What do you call cheese that isn't yours? Nacho cheese! 🧀",
    "Why did the coffee file a police report? It got mugged! ☕",
    "What do you call a fish wearing a bowtie? Sofishticated! 🎀",
    "Why did the computer catch a cold? It forgot to close windows! 🪟",
    "What did one wall say to the other wall? I'll meet you at the corner! 🧱",
    "Why don't programmers like nature? Too many bugs! 🐛"
]

FUN_FACTS = [
    "🐙 Octopuses have three hearts and blue blood!",
    "🍯 Honey never spoils - archaeologists found edible honey in Egyptian tombs!",
    "🦒 A giraffe's tongue is 20 inches long and black to prevent sunburn!",
    "🐧 Penguins propose to their mates with pebbles!",
    "🎵 'Happy Birthday' was the first song played on Mars by NASA's rover!",
    "🌙 The moon is moving away from Earth at about 1.5 inches per year!",
    "🐨 Koalas sleep 18-22 hours a day!",
    "🍌 Bananas are berries, but strawberries aren't!",
    "🦈 Sharks existed before trees did!",
    "🧊 Hot water can freeze faster than cold water in some conditions!",
    "🦩 A group of flamingos is called a flamboyance!",
    "🧠 Your brain uses about 20% of your body's total energy!",
    "🐬 Dolphins have unique names for each other using signature whistles!",
    "🌋 More people live near volcanoes than you might expect due to fertile soil!",
    "🍎 Apples float because about 25% of their volume is air!",
    "🦋 Butterflies can taste with their feet!",
    "🌲 The oldest known tree species can live for thousands of years!"
]

RIDDLES = [
    "🤔 What has keys but no locks, space but no room, and you can enter but not go inside? (Answer: A keyboard)",
    "🤔 What gets wetter the more it dries? (Answer: A towel)",
    "🤔 What has a head, a tail, but no body? (Answer: A coin)",
    "🤔 I'm tall when I'm young and short when I'm old. What am I? (Answer: A candle)",
    "🤔 What goes up but never comes down? (Answer: Your age)",
    "🤔 What has hands but can't clap? (Answer: A clock)",
    "🤔 The more you take, the more you leave behind. What am I? (Answer: Footsteps)",
    "🤔 What can travel all around the world without leaving its corner? (Answer: A stamp)",
    "🤔 What has one eye but cannot see? (Answer: A needle)",
    "🤔 What has many teeth but cannot bite? (Answer: A comb)",
    "🤔 What begins with T, ends with T, and has T in it? (Answer: A teapot)",
    "🤔 What has cities, but no houses; forests, but no trees; and water, but no fish? (Answer: A map)",
    "🤔 What comes once in a minute, twice in a moment, but never in a thousand years? (Answer: The letter M)",
    "🤔 What goes through towns and over hills but never moves? (Answer: A road)",
    "🤔 What has a ring but no finger? (Answer: A phone)"
]

QUOTES = [
    "The only way to do great work is to love what you do. - Steve Jobs",
    "Life is what happens to you while you're busy making other plans. - John Lennon",
    "The future belongs to those who believe in the beauty of their dreams. - Eleanor Roosevelt",
    "It is during our darkest moments that we must focus to see the light. - Aristotle",
    "The only impossible journey is the one you never begin. - Tony Robbins",
    "Success is not final, failure is not fatal: it is the courage to continue that counts. - Winston Churchill",
    "Do what you can, with what you have, where you are. - Theodore Roosevelt",
    "Small steps every day become big results over time. - Unknown",
    "Discipline is choosing what you want most over what you want now. - Abraham Lincoln",
    "Your future is created by what you do today, not tomorrow. - Robert Kiyosaki"
]

HELLO_GREETINGS = [
    "Hello there, {name}! 👋",
    "Hey {name}! How are you doing? 😊",
    "Greetings, {name}! Nice to see you! 🌟",
    "Hi {name}! Hope you're having a great day! ☀️",
    "Welcome, {name}! 🎉",
    "Yo {name}, good to see you here! 🔥",
    "What's up, {name}? Ready for some fun? 🎮",
    "Heyyy {name}! Hope your day is going great 😄",
    "Big welcome energy for {name}! ⚡",
    "Hi {name}! Let's make today awesome 💫"
]

# Command group
fun_group = app_commands.Group(name="fun", description="Fun and entertainment commands")

@fun_group.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="Your question for the magic 8-ball")
async def eight_ball(interaction: discord.Interaction, question: str):
    response = random.choice(_fun_pick_lines(interaction, "eight_ball", EIGHT_BALL_RESPONSES))
    embed = discord.Embed(
        title="🎱 Magic 8-Ball",
        description=f"**Question:** {question}\n**Answer:** {response}",
        color=0x000000
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    emoji = "🪙" if result == "Heads" else "🪙"
    embed = discord.Embed(
        title=f"{emoji} Coin Flip",
        description=f"The coin landed on **{result}**!",
        color=0xFFD700
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="dice", description="Roll a dice")
async def dice(interaction: discord.Interaction):
    result = random.randint(1, 6)
    embed = discord.Embed(
        title="🎲 Dice Roll",
        description=f"You rolled a **{result}**!",
        color=0xFF0000
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="roast", description="Get playfully roasted by the bot")
@app_commands.describe(target="Optional user to roast (default: yourself)")
async def roast(interaction: discord.Interaction, target: discord.Member = None):
    if target is None:
        target = interaction.user
    
    roast_text = random.choice(_fun_pick_lines(interaction, "roasts", ROASTS))
    embed = discord.Embed(
        title="🔥 Roasted!",
        description=f"{target.mention} {roast_text}",
        color=0xFF4500
    )
    embed.set_footer(text="Just kidding! You're awesome! 💖")
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="compliment", description="Receive a nice compliment")
@app_commands.describe(target="Optional user to compliment (default: yourself)")
async def compliment(interaction: discord.Interaction, target: discord.Member = None):
    if target is None:
        target = interaction.user
    
    compliment_text = random.choice(_fun_pick_lines(interaction, "compliments", COMPLIMENTS))
    embed = discord.Embed(
        title="💖 Compliment",
        description=f"{target.mention} {compliment_text}",
        color=0xFF69B4
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="joke", description="Get a random joke")
async def joke(interaction: discord.Interaction):
    joke_text = random.choice(_fun_pick_lines(interaction, "jokes", JOKES))
    embed = discord.Embed(
        title="😂 Here's a joke for you!",
        description=joke_text,
        color=0xFFFF00
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="fact", description="Learn a random fun fact")
async def fact(interaction: discord.Interaction):
    fact_text = random.choice(_fun_pick_lines(interaction, "fun_facts", FUN_FACTS))
    embed = discord.Embed(
        title="🧠 Fun Fact!",
        description=fact_text,
        color=0x00CED1
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="riddle", description="Get a riddle to solve")
async def riddle(interaction: discord.Interaction):
    riddle_text = random.choice(_fun_pick_lines(interaction, "riddles", RIDDLES))
    embed = discord.Embed(
        title="🧩 Riddle Time!",
        description=riddle_text,
        color=0x9932CC
    )
    embed.set_footer(text="Think you know the answer? 🤔")
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="quote", description="Get an inspirational quote")
async def quote(interaction: discord.Interaction):
    quote_text = random.choice(_fun_pick_lines(interaction, "quotes", QUOTES))
    embed = discord.Embed(
        title="💫 Inspirational Quote",
        description=quote_text,
        color=0x4169E1
    )
    await interaction.response.send_message(embed=embed)

@fun_group.command(name="meme", description="Get a random meme")
async def meme(interaction: discord.Interaction):
    await interaction.response.defer()
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://meme-api.com/gimme") as response:
                if response.status == 200:
                    data = await response.json()
                    embed = discord.Embed(
                        title=f"😂 {data['title']}",
                        color=0xFF6347
                    )
                    embed.set_image(url=data['url'])
                    embed.set_footer(text=f"👍 {data['ups']} upvotes on r/{data['subreddit']}")
                    await interaction.followup.send(embed=embed)
                else:
                    raise Exception("API Error")
    except:
        # Fallback if API fails
        embed = discord.Embed(
            title="😅 Meme Service Unavailable",
            description="Sorry, couldn't fetch a meme right now! Try again later.",
            color=0xFF0000
        )
        await interaction.followup.send(embed=embed)

# Common utility commands as individual commands (not in a group)

@app_commands.command(name="ping", description="Check bot latency and response time")
async def ping_command(interaction: discord.Interaction):
    start_time = time.time()
    await interaction.response.defer()
    end_time = time.time()
    
    latency = round(interaction.client.latency * 1000)
    response_time = round((end_time - start_time) * 1000)
    
    embed = discord.Embed(
        title="🏓 Pong!",
        color=0x00FF00
    )
    embed.add_field(name="Bot Latency", value=f"{latency}ms", inline=True)
    embed.add_field(name="Response Time", value=f"{response_time}ms", inline=True)
    embed.set_footer(text="Bot is online and responsive!")
    
    await interaction.followup.send(embed=embed)

@app_commands.command(name="hello", description="Get a friendly greeting from the bot")
@app_commands.describe(user="Optional user to greet")
async def hello_command(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    greeting_template = random.choice(_fun_pick_lines(interaction, "hello_greetings", HELLO_GREETINGS))
    try:
        greeting = greeting_template.format(name=target.display_name)
    except Exception:
        greeting = greeting_template

    embed = discord.Embed(
        title="👋 Hello!",
        description=greeting,
        color=0x00BFFF
    )
    await interaction.response.send_message(embed=embed)

@app_commands.command(name="say", description="Make the bot say something")
@app_commands.describe(message="The message for the bot to say")
async def say_command(interaction: discord.Interaction, message: str):
    # Basic filter for inappropriate content
    if any(word in message.lower() for word in ['@everyone', '@here', 'discord.gg', 'http']):
        await interaction.response.send_message("❌ That message contains restricted content.", ephemeral=True)
        return
    
    await interaction.response.send_message(message)

@app_commands.command(name="roll", description="Roll dice (e.g., 1d6, 2d20)")
@app_commands.describe(dice="Dice notation (e.g., 1d6, 2d20, 3d12)")
async def roll_command(interaction: discord.Interaction, dice: str = "1d6"):
    try:
        # Parse dice notation (NdS where N=number of dice, S=sides)
        if 'd' not in dice.lower():
            dice = f"1d{dice}"  # If just a number, assume 1dN
        
        parts = dice.lower().split('d')
        num_dice = int(parts[0]) if parts[0] else 1
        sides = int(parts[1])
        
        if num_dice > 20 or sides > 1000 or num_dice < 1 or sides < 2:
            await interaction.response.send_message("❌ Invalid dice! Use 1-20 dice with 2-1000 sides.", ephemeral=True)
            return
        
        rolls = [random.randint(1, sides) for _ in range(num_dice)]
        total = sum(rolls)
        
        embed = discord.Embed(
            title=f"🎲 Rolling {dice.upper()}",
            color=0xFF6347
        )
        
        if num_dice == 1:
            embed.description = f"**Result:** {total}"
        else:
            embed.description = f"**Rolls:** {', '.join(map(str, rolls))}\n**Total:** {total}"
        
        await interaction.response.send_message(embed=embed)
        
    except (ValueError, IndexError):
        await interaction.response.send_message("❌ Invalid dice format! Use formats like `1d6`, `2d20`, `3d12`.", ephemeral=True)

@app_commands.command(name="check", description="Check a user's profile by ID, username, or mention")
@app_commands.describe(query="The user ID, username, or mention to check (default: yourself)")
async def check_command(interaction: discord.Interaction, query: str = None):
    await interaction.response.defer()
    target = None
    
    if not query:
        target = interaction.user
    else:
        match = re.match(r'<@!?([0-9]+)>', query)
        if match:
            user_id = int(match.group(1))
        elif query.isdigit():
            user_id = int(query)
        else:
            user_id = None
            
        if user_id:
            target = interaction.guild.get_member(user_id)
            if not target:
                try:
                    target = await interaction.client.fetch_user(user_id)
                except discord.NotFound:
                    pass
        else:
            query_lower = query.lower()
            target = discord.utils.find(
                lambda m: m.name.lower() == query_lower or m.display_name.lower() == query_lower or (m.global_name and m.global_name.lower() == query_lower),
                interaction.guild.members
            )
            
    if not target:
        await interaction.followup.send(f"âŒ Could not find a user matching `{query}`.", ephemeral=True)
        return
        
    embed = discord.Embed(
        title=f"ðŸ‘¤ User Info - {target.display_name}",
        color=target.color if getattr(target, 'color', discord.Color.default()) != discord.Color.default() else 0x7289DA
    )
    
    embed.set_thumbnail(url=target.display_avatar.url)
    
    if hasattr(target, "discriminator") and target.discriminator != "0" and target.discriminator != "0000":
        embed.add_field(name="Username", value=f"{target.name}#{target.discriminator}", inline=True)
    else:
        embed.add_field(name="Username", value=target.name, inline=True)
        
    embed.add_field(name="Display Name", value=target.display_name, inline=True)
    embed.add_field(name="ID", value=target.id, inline=True)
    
    embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:F>", inline=True)
    
    if isinstance(target, discord.Member):
        embed.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:F>", inline=True)
        
    embed.add_field(name="Bot Account", value="Yes" if target.bot else "No", inline=True)
    
    if isinstance(target, discord.Member) and getattr(target, 'premium_since', None):
        embed.add_field(name="Boosting Since", value=f"<t:{int(target.premium_since.timestamp())}:F>", inline=True)
    
    if isinstance(target, discord.Member):
        roles = [role.mention for role in target.roles[1:]]  # Skip @everyone
        if roles:
            embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles) if len(" ".join(roles)) < 1024 else f"{len(roles)} roles", inline=False)
    
    await interaction.followup.send(embed=embed)

@app_commands.command(name="userinfo", description="Display information about a user")
@app_commands.describe(user="The user to get information about (default: yourself)")
async def userinfo_command(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    
    embed = discord.Embed(
        title=f"👤 User Info - {target.display_name}",
        color=target.color if target.color != discord.Color.default() else 0x7289DA
    )
    
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Username", value=f"{target.name}#{target.discriminator}", inline=True)
    embed.add_field(name="Display Name", value=target.display_name, inline=True)
    embed.add_field(name="ID", value=target.id, inline=True)
    
    embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:F>", inline=True)
    embed.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:F>", inline=True)
    embed.add_field(name="Bot Account", value="Yes" if target.bot else "No", inline=True)
    
    if target.premium_since:
        embed.add_field(name="Boosting Since", value=f"<t:{int(target.premium_since.timestamp())}:F>", inline=True)
    
    roles = [role.mention for role in target.roles[1:]]  # Skip @everyone
    if roles:
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles) if len(" ".join(roles)) < 1024 else f"{len(roles)} roles", inline=False)
    
    await interaction.response.send_message(embed=embed)

@app_commands.command(name="serverinfo", description="Display information about this server")
async def serverinfo_command(interaction: discord.Interaction):
    guild = interaction.guild
    
    embed = discord.Embed(
        title=f"🏰 Server Info - {guild.name}",
        color=0x7289DA
    )
    
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    embed.add_field(name="Server ID", value=guild.id, inline=True)
    embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=True)
    
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    
    embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
    embed.add_field(name="Boost Count", value=guild.premium_subscription_count or 0, inline=True)
    embed.add_field(name="Verification Level", value=guild.verification_level.name.title(), inline=True)
    
    if guild.description:
        embed.add_field(name="Description", value=guild.description, inline=False)
    
    await interaction.response.send_message(embed=embed)

@app_commands.command(name="avatar", description="Display a user's avatar")
@app_commands.describe(user="The user whose avatar to display (default: yourself)")
async def avatar_command(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    
    embed = discord.Embed(
        title=f"🖼️ Avatar - {target.display_name}",
        color=target.color if target.color != discord.Color.default() else 0x7289DA
    )
    
    embed.set_image(url=target.display_avatar.url)
    embed.add_field(name="Direct Link", value=f"[Click Here]({target.display_avatar.url})", inline=False)
    
    await interaction.response.send_message(embed=embed)

def setup(bot):
    """Add fun commands and common utility commands to the bot"""
    bot.tree.add_command(fun_group)
    bot.tree.add_command(ping_command)
    bot.tree.add_command(hello_command)
    bot.tree.add_command(say_command)
    bot.tree.add_command(roll_command)
    bot.tree.add_command(check_command)
    bot.tree.add_command(userinfo_command)
    bot.tree.add_command(serverinfo_command)
    bot.tree.add_command(avatar_command)
